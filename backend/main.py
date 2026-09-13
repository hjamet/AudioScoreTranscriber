"""
Backend WebSocket Server and Orchestrator for AudioScoreTranscriber.
Runs on ws://127.0.0.1:8085.
Features:
- Command protocol: START_RECORDING, STOP_RECORDING, GET_STATUS, SET_MODE, SET_LATENCY, TRANSCRIBE_AUDIO
- Anti-zombie watchdog: terminates if no client is connected for 10 seconds.
- Windows single-instance lock (%TEMP%/audioscoretranscriber.lock via msvcrt.locking).
- Seamless pipeline: Audio Ingestion -> Multi-Mode Transcriber -> Gould Adaptive Quantizer -> MusicXML/QML JSON.
"""

import sys
import os
import tempfile
import asyncio
import json
import logging
import argparse
from typing import Set, Optional, Dict, Any
import numpy as np

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] (%(name)s) %(message)s"
)
logger = logging.getLogger("AudioScoreTranscriber.Backend")

# Ensure backend directory is in sys.path for direct or module execution
backend_dir = os.path.dirname(os.path.abspath(__file__))
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

# Internal modules
from audio_capture import AudioCaptureEngine
from transcriber.rhythm_transcriber import RhythmTranscriber
from transcriber.piano_transcriber import PianoTranscriber
from transcriber.vocal_transcriber import VocalTranscriber
from transcriber.quantizer import GouldAdaptiveQuantizer
from transcriber.musicxml_writer import MusicXMLScoreWriter


class SingleInstanceLock:
    """Windows-specific single-instance lockfile mechanism via msvcrt.locking."""

    def __init__(self, lock_filename: str = "audioscoretranscriber.lock"):
        self.lock_path = os.path.join(tempfile.gettempdir(), lock_filename)
        self.file_handle = None

    def acquire(self) -> bool:
        if sys.platform != "win32":
            return True

        import msvcrt
        try:
            self.file_handle = open(self.lock_path, "w")
            msvcrt.locking(self.file_handle.fileno(), msvcrt.LK_NBLCK, 1)
            logger.info("Acquired single-instance lock: %s", self.lock_path)
            return True
        except (IOError, OSError) as exc:
            logger.warning("Another instance of AudioScoreTranscriber is already running (%s). Exiting.", exc)
            if self.file_handle:
                try:
                    self.file_handle.close()
                except Exception:
                    pass
                self.file_handle = None
            return False

    def release(self):
        if sys.platform != "win32" or self.file_handle is None:
            return

        import msvcrt
        try:
            msvcrt.locking(self.file_handle.fileno(), msvcrt.LK_UNLCK, 1)
            self.file_handle.close()
            logger.info("Released single-instance lock.")
        except Exception as exc:
            logger.debug("Error releasing lock: %s", exc)
        finally:
            self.file_handle = None


class ServerOrchestrator:
    """
    Main audio transcription server orchestrator managing WebSocket clients,
    audio capture, AI transcription modes, and anti-zombie watchdog.
    """

    def __init__(
        self,
        port: int = 8085,
        enable_watchdog: bool = True,
        watchdog_timeout_sec: float = 10.0
    ):
        self.port = port
        self.enable_watchdog = enable_watchdog
        self.watchdog_timeout_sec = watchdog_timeout_sec

        self.mode = "rhythm"  # "rhythm", "piano", or "vocal"
        self.status = "idle"  # "idle", "recording", "transcribing"
        self.active_bpm = 120.0

        # Engines
        self.audio_engine = AudioCaptureEngine(sample_rate=44100, rtl_latency_ms=15.0)
        self.rhythm_transcriber = RhythmTranscriber(sample_rate=44100)
        self.piano_transcriber = PianoTranscriber(sample_rate=16000)
        self.vocal_transcriber = VocalTranscriber(sample_rate=22050)
        self.quantizer = GouldAdaptiveQuantizer(time_signature=(4, 4), default_bpm=120.0)
        self.musicxml_writer = MusicXMLScoreWriter()

        self.connected_clients: Set[Any] = set()
        self._watchdog_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    def start_watchdog(self):
        """Schedule anti-zombie shutdown if no client is connected."""
        if not self.enable_watchdog:
            return

        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()

        self._watchdog_task = asyncio.create_task(self._watchdog_countdown())

    def cancel_watchdog(self):
        """Cancel anti-zombie shutdown when a client connects."""
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            self._watchdog_task = None
            logger.info("Watchdog canceled: active client connected.")

    async def _watchdog_countdown(self):
        try:
            logger.info("Watchdog started: auto-shutdown in %.1fs if no client connects.",
                        self.watchdog_timeout_sec)
            await asyncio.sleep(self.watchdog_timeout_sec)
            if len(self.connected_clients) == 0:
                logger.warning("Anti-zombie watchdog triggered: 0 clients connected for %.1fs. Shutting down.",
                               self.watchdog_timeout_sec)
                self._stop_event.set()
        except asyncio.CancelledError:
            pass

    async def register_client(self, websocket):
        self.connected_clients.add(websocket)
        logger.info("Client connected (%s). Total clients: %d",
                    websocket.remote_address if hasattr(websocket, 'remote_address') else 'client',
                    len(self.connected_clients))
        self.cancel_watchdog()

    async def unregister_client(self, websocket):
        self.connected_clients.discard(websocket)
        logger.info("Client disconnected. Remaining clients: %d", len(self.connected_clients))
        if len(self.connected_clients) == 0:
            self.start_watchdog()

    async def handle_message(self, websocket, message_str: str):
        """Process incoming WebSocket JSON commands."""
        try:
            req = json.loads(message_str)
        except Exception as exc:
            await self._send_error(websocket, f"Invalid JSON payload: {exc}")
            return

        cmd = req.get("command", "").upper()
        logger.info("Received command: %s", cmd)

        if cmd == "GET_STATUS":
            await self._handle_get_status(websocket)
        elif cmd == "SET_MODE":
            await self._handle_set_mode(websocket, req)
        elif cmd == "SET_LATENCY":
            await self._handle_set_latency(websocket, req)
        elif cmd == "START_RECORDING":
            await self._handle_start_recording(websocket, req)
        elif cmd == "STOP_RECORDING":
            await self._handle_stop_recording(websocket, req)
        elif cmd == "TRANSCRIBE_AUDIO":
            await self._handle_transcribe_audio(websocket, req)
        else:
            await self._send_error(websocket, f"Unknown command: '{cmd}'")

    async def _handle_get_status(self, websocket):
        clipping_info = self.audio_engine.get_clipping_stats()
        resp = {
            "event": "STATUS",
            "status": self.status,
            "mode": self.mode,
            "sample_rate": self.audio_engine.sample_rate,
            "latency_ms": self.audio_engine.rtl_latency_ms,
            "is_recording": self.audio_engine.is_recording,
            "clipping": clipping_info
        }
        await websocket.send(json.dumps(resp))

    async def _handle_set_mode(self, websocket, req: Dict[str, Any]):
        new_mode = str(req.get("mode", "")).lower()
        if new_mode in ["rhythm", "piano", "vocal"]:
            self.mode = new_mode
            logger.info("Active mode set to: %s", self.mode)
            await websocket.send(json.dumps({
                "event": "MODE_SET",
                "mode": self.mode,
                "status": "success"
            }))
        else:
            await self._send_error(websocket, f"Invalid mode '{new_mode}'. Expected 'rhythm', 'piano', or 'vocal'.")

    async def _handle_set_latency(self, websocket, req: Dict[str, Any]):
        try:
            latency_ms = float(req.get("latency_ms", 15.0))
            self.audio_engine.set_rtl_latency_ms(latency_ms)
            await websocket.send(json.dumps({
                "event": "LATENCY_SET",
                "latency_ms": self.audio_engine.rtl_latency_ms,
                "status": "success"
            }))
        except (ValueError, TypeError) as exc:
            await self._send_error(websocket, f"Invalid latency value: {exc}")

    async def _handle_start_recording(self, websocket, req: Dict[str, Any]):
        if self.status == "recording":
            await self._send_error(websocket, "Already recording.")
            return

        success = self.audio_engine.start()
        if success:
            self.status = "recording"
            logger.info("Started recording audio (mode=%s)", self.mode)
            await websocket.send(json.dumps({
                "event": "RECORDING_STARTED",
                "status": "recording",
                "mode": self.mode
            }))
        else:
            await self._send_error(websocket, "Failed to initialize microphone audio stream.")

    async def _handle_stop_recording(self, websocket, req: Dict[str, Any]):
        if self.status != "recording":
            await self._send_error(websocket, "Cannot stop: engine is not currently recording.")
            return

        # Stop audio capture and retrieve latency-compensated audio
        audio_buffer = self.audio_engine.stop()
        self.status = "transcribing"

        await websocket.send(json.dumps({
            "event": "RECORDING_STOPPED",
            "status": "transcribing",
            "sample_count": len(audio_buffer),
            "duration_sec": round(len(audio_buffer) / self.audio_engine.sample_rate, 3)
        }))

        # Run transcription pipeline asynchronously
        export_musicxml = req.get("export_musicxml", False)
        bpm_override = req.get("bpm")

        loop = asyncio.get_running_loop()
        result_payload = await loop.run_in_executor(
            None,
            self._execute_transcription_pipeline,
            audio_buffer,
            self.audio_engine.sample_rate,
            self.mode,
            bpm_override,
            export_musicxml
        )

        self.status = "idle"
        await websocket.send(json.dumps({
            "event": "TRANSCRIPTION_RESULT",
            "data": result_payload["qml_json"],
            "musicxml": result_payload.get("musicxml", "")
        }))

    async def _handle_transcribe_audio(self, websocket, req: Dict[str, Any]):
        """Transcribe directly from submitted audio array (for tests / direct processing)."""
        raw_samples = req.get("audio_data", [])
        mode = req.get("mode", self.mode)
        sr = req.get("sample_rate", 44100)
        bpm = req.get("bpm")
        export_xml = req.get("export_musicxml", False)

        if not raw_samples:
            await self._send_error(websocket, "Missing audio_data array in TRANSCRIBE_AUDIO request.")
            return

        audio = np.array(raw_samples, dtype=np.float32)
        self.status = "transcribing"

        loop = asyncio.get_running_loop()
        result_payload = await loop.run_in_executor(
            None,
            self._execute_transcription_pipeline,
            audio,
            sr,
            mode,
            bpm,
            export_xml
        )

        self.status = "idle"
        await websocket.send(json.dumps({
            "event": "TRANSCRIPTION_RESULT",
            "data": result_payload["qml_json"],
            "musicxml": result_payload.get("musicxml", "")
        }))

    def _execute_transcription_pipeline(
        self,
        audio: np.ndarray,
        sr: int,
        mode: str,
        bpm_override: Optional[float] = None,
        export_musicxml: bool = False
    ) -> Dict[str, Any]:
        """
        Execute the end-to-end transcription pipeline:
        Audio -> Multi-Mode Transcriber -> Gould Quantizer -> QML JSON & MusicXML.
        """
        logger.info("Executing transcription pipeline: %d samples, sr=%d, mode=%s", len(audio), sr, mode)

        # 1. Multi-mode AI transcription
        if mode == "rhythm":
            raw_events = self.rhythm_transcriber.transcribe(audio, sr=sr)
            detected_bpm = self.rhythm_transcriber.estimate_tempo_bpm(raw_events, fallback_bpm=self.active_bpm)
        elif mode == "piano":
            raw_events = self.piano_transcriber.transcribe(audio, sr=sr)
            detected_bpm = self.active_bpm
        elif mode == "vocal":
            raw_events = self.vocal_transcriber.transcribe(audio, sr=sr)
            detected_bpm = self.active_bpm
        else:
            raw_events = []
            detected_bpm = self.active_bpm

        effective_bpm = bpm_override if (bpm_override is not None and bpm_override > 0) else detected_bpm

        # 2. Elaine Gould adaptive quantization
        quantized_score = self.quantizer.quantize(raw_events, bpm=effective_bpm)

        # 3. Serialization to QML JSON
        qml_json = self.musicxml_writer.to_qml_json(quantized_score, mode=mode)

        # 4. Optional MusicXML generation
        musicxml_str = ""
        if export_musicxml:
            musicxml_str = self.musicxml_writer.to_musicxml_string(
                quantized_score,
                title=f"AudioScore - {mode.capitalize()} Transcription",
                mode=mode
            )

        return {
            "qml_json": qml_json,
            "musicxml": musicxml_str,
            "raw_notes_count": len(raw_events)
        }

    async def _send_error(self, websocket, error_msg: str):
        logger.warning("Sending error to client: %s", error_msg)
        await websocket.send(json.dumps({
            "event": "ERROR",
            "message": error_msg
        }))

    async def ws_handler(self, websocket, *args, **kwargs):
        """WebSocket connection entry point."""
        await self.register_client(websocket)
        try:
            async for message in websocket:
                await self.handle_message(websocket, message)
        except Exception as exc:
            logger.info("WebSocket connection closed with: %s", exc)
        finally:
            await self.unregister_client(websocket)

    async def run(self):
        """Run the WebSocket server."""
        import websockets

        # Start initial anti-zombie watchdog countdown
        self.start_watchdog()

        async with websockets.serve(self.ws_handler, "127.0.0.1", self.port):
            logger.info("AudioScoreTranscriber WebSocket server listening on ws://127.0.0.1:%d", self.port)
            await self._stop_event.wait()
            logger.info("Stopping WebSocket server...")


def main():
    parser = argparse.ArgumentParser(description="AudioScoreTranscriber Python Backend")
    parser.add_argument("--port", type=int, default=8085, help="WebSocket port (default: 8085)")
    parser.add_argument("--no-watchdog", action="store_true", help="Disable 10s anti-zombie auto-termination")
    parser.add_argument("--watchdog-timeout", type=float, default=10.0, help="Watchdog timeout in seconds (default: 10.0)")
    args = parser.parse_args()

    # Single-instance lock on Windows
    lock = SingleInstanceLock()
    if not lock.acquire():
        sys.exit(0)

    # Respect DISABLE_WATCHDOG env var or CLI arg
    enable_watchdog = not args.no_watchdog and os.environ.get("DISABLE_WATCHDOG", "0") != "1"

    orchestrator = ServerOrchestrator(
        port=args.port,
        enable_watchdog=enable_watchdog,
        watchdog_timeout_sec=args.watchdog_timeout
    )

    try:
        asyncio.run(orchestrator.run())
    except KeyboardInterrupt:
        logger.info("Server terminated by user.")
    finally:
        lock.release()


if __name__ == "__main__":
    main()
