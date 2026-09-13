"""
Audio Capture Module for AudioScoreTranscriber.
Provides low-latency microphone audio streaming (WASAPI/ASIO),
circular recording buffer, clipping detection, and Round-Trip Latency (RTL) compensation.
"""

import sys
import threading
import logging
from typing import Optional, Tuple, Dict, Any
import numpy as np

try:
    import sounddevice as sd
except ImportError:
    sd = None

logger = logging.getLogger(__name__)


class AudioCaptureEngine:
    """
    Low-latency audio capture manager using sounddevice with WASAPI/ASIO support,
    circular monitoring buffer, clipping tracking, and RTL latency compensation.
    """

    def __init__(
        self,
        sample_rate: int = 44100,
        channels: int = 1,
        block_size: int = 512,
        buffer_duration_sec: float = 60.0,
        rtl_latency_ms: float = 15.0,
        prefer_wasapi: bool = True
    ):
        self.sample_rate = sample_rate
        self.channels = channels
        self.block_size = block_size
        self.buffer_duration_sec = buffer_duration_sec
        self.rtl_latency_ms = rtl_latency_ms
        self.prefer_wasapi = prefer_wasapi

        self._lock = threading.Lock()
        self._is_recording = False
        self._stream: Optional[Any] = None

        # Circular buffer allocation
        self._max_buffer_samples = int(self.sample_rate * self.buffer_duration_sec)
        self._circular_buffer = np.zeros(self._max_buffer_samples, dtype=np.float32)
        self._write_pos = 0
        self._total_samples_written = 0

        # Linear session recording buffer
        self._recorded_chunks = []

        # Clipping metrics
        self._clipping_detected = False
        self._clipping_count = 0
        self._peak_amplitude = 0.0

        # Selected input device index
        self._device_index = self._find_optimal_device()

    def _find_optimal_device(self) -> Optional[int]:
        """Find the best input device, prioritizing WASAPI or ASIO on Windows."""
        if sd is None:
            logger.warning("sounddevice not available, running in mock/offline mode")
            return None

        try:
            devices = sd.query_devices()
            hostapis = sd.query_hostapis()
        except Exception as exc:
            logger.error("Error querying sound devices: %s", exc)
            return None

        wasapi_api_idx = None
        asio_api_idx = None
        for idx, api in enumerate(hostapis):
            name = api.get("name", "").lower()
            if "wasapi" in name:
                wasapi_api_idx = idx
            elif "asio" in name:
                asio_api_idx = idx

        # Prioritize ASIO, then WASAPI, then default
        target_api = asio_api_idx if asio_api_idx is not None else wasapi_api_idx

        if target_api is not None and self.prefer_wasapi:
            # Check default input device for that host API
            default_dev = hostapis[target_api].get("default_input_device")
            if default_dev is not None and default_dev >= 0:
                logger.info("Selected optimal host API device #%d (%s)",
                            default_dev, hostapis[target_api].get("name"))
                return default_dev

            # Otherwise look for any input device matching this host API
            for dev_idx, dev in enumerate(devices):
                if dev.get("hostapi") == target_api and dev.get("max_input_channels", 0) > 0:
                    logger.info("Found input device #%d on low-latency host API", dev_idx)
                    return dev_idx

        # Fallback to system default input device
        try:
            default_input = sd.default.device[0]
            if default_input is not None and default_input >= 0:
                return default_input
        except Exception:
            pass

        return None

    def _audio_callback(self, indata, frames, time_info, status):
        """High-priority sounddevice audio input stream callback."""
        if status:
            logger.warning("Audio capture stream status: %s", status)

        # Convert to mono 1D float32
        if indata.ndim > 1:
            chunk = np.mean(indata, axis=1).astype(np.float32)
        else:
            chunk = indata.flatten().astype(np.float32)

        # Clipping detection
        chunk_max = float(np.max(np.abs(chunk))) if len(chunk) > 0 else 0.0
        is_clipped = chunk_max >= 0.999

        with self._lock:
            if chunk_max > self._peak_amplitude:
                self._peak_amplitude = chunk_max

            if is_clipped:
                self._clipping_detected = True
                self._clipping_count += 1

            if self._is_recording:
                self._recorded_chunks.append(chunk.copy())

            # Update circular buffer
            num_samples = len(chunk)
            if num_samples <= self._max_buffer_samples:
                end_pos = self._write_pos + num_samples
                if end_pos <= self._max_buffer_samples:
                    self._circular_buffer[self._write_pos:end_pos] = chunk
                else:
                    first_part = self._max_buffer_samples - self._write_pos
                    second_part = num_samples - first_part
                    self._circular_buffer[self._write_pos:] = chunk[:first_part]
                    self._circular_buffer[:second_part] = chunk[first_part:]
                self._write_pos = (self._write_pos + num_samples) % self._max_buffer_samples
                self._total_samples_written += num_samples

    def start(self) -> bool:
        """Start audio ingestion stream."""
        with self._lock:
            if self._is_recording:
                logger.warning("Audio capture already active")
                return True

            self._recorded_chunks = []
            self._clipping_detected = False
            self._clipping_count = 0
            self._peak_amplitude = 0.0
            self._is_recording = True

        if sd is None:
            logger.info("Audio capture started in simulated/offline mode")
            return True

        try:
            extra_settings = None
            # Under Windows WASAPI, configure exclusive or shared mode with low latency
            if sys.platform == "win32" and self._device_index is not None:
                try:
                    dev_info = sd.query_devices(self._device_index)
                    host_api = sd.query_hostapis(dev_info["hostapi"])["name"]
                    if "WASAPI" in host_api:
                        extra_settings = sd.WasapiSettings(exclusive=False)
                except Exception as ex:
                    logger.debug("Could not apply WASAPI extra settings: %s", ex)

            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                blocksize=self.block_size,
                device=self._device_index,
                channels=self.channels,
                dtype="float32",
                latency="low",
                extra_settings=extra_settings,
                callback=self._audio_callback
            )
            self._stream.start()
            logger.info("Audio stream started (device=%s, sr=%d, block=%d)",
                        self._device_index, self.sample_rate, self.block_size)
            return True
        except Exception as exc:
            logger.error("Failed to start sounddevice stream: %s", exc)
            # Still keep recording state active so simulation or fallback can proceed
            return False

    def stop(self) -> np.ndarray:
        """Stop audio stream and return captured audio with RTL latency compensation."""
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as exc:
                logger.warning("Error closing audio stream: %s", exc)
            self._stream = None

        with self._lock:
            self._is_recording = False
            if self._recorded_chunks:
                full_audio = np.concatenate(self._recorded_chunks, axis=0)
            else:
                full_audio = np.array([], dtype=np.float32)

        # Apply RTL Latency compensation
        return self._apply_rtl_compensation(full_audio)

    def _apply_rtl_compensation(self, audio: np.ndarray) -> np.ndarray:
        """
        Compensate for hardware Round-Trip Latency (RTL).
        Trims initial latency delay samples so note onsets align with true acoustic timing.
        """
        if len(audio) == 0:
            return audio

        offset_samples = int((self.rtl_latency_ms / 1000.0) * self.sample_rate)
        if offset_samples <= 0:
            return audio

        if len(audio) > offset_samples:
            return audio[offset_samples:]
        else:
            return np.zeros(0, dtype=np.float32)

    def get_latest_circular_buffer(self, duration_sec: Optional[float] = None) -> np.ndarray:
        """Return the latest samples from the circular ring buffer."""
        with self._lock:
            if duration_sec is None:
                samples_to_read = min(self._total_samples_written, self._max_buffer_samples)
            else:
                samples_to_read = min(int(duration_sec * self.sample_rate),
                                      self._total_samples_written,
                                      self._max_buffer_samples)

            if samples_to_read == 0:
                return np.zeros(0, dtype=np.float32)

            out = np.zeros(samples_to_read, dtype=np.float32)
            start_pos = (self._write_pos - samples_to_read) % self._max_buffer_samples
            if start_pos + samples_to_read <= self._max_buffer_samples:
                out[:] = self._circular_buffer[start_pos:start_pos + samples_to_read]
            else:
                part1 = self._max_buffer_samples - start_pos
                part2 = samples_to_read - part1
                out[:part1] = self._circular_buffer[start_pos:]
                out[part1:] = self._circular_buffer[:part2]

            return out

    def get_clipping_stats(self) -> Dict[str, Any]:
        """Return current clipping metrics and peak audio levels."""
        with self._lock:
            return {
                "clipping_detected": self._clipping_detected,
                "clipping_count": self._clipping_count,
                "peak_amplitude": float(self._peak_amplitude),
                "peak_db": float(20.0 * np.log10(max(self._peak_amplitude, 1e-5)))
            }

    def set_rtl_latency_ms(self, latency_ms: float):
        """Update the RTL latency compensation value in milliseconds."""
        with self._lock:
            self.rtl_latency_ms = max(0.0, float(latency_ms))
            logger.info("RTL latency set to %.2f ms", self.rtl_latency_ms)

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._is_recording
