"""
Piano Transcriber Module for AudioScoreTranscriber.
Mode 2: "Piano Polyphonique".
Integrates ByteDance Piano Transcription / Onsets and Frames with an intelligent,
zero-dependency polyphonic spectral fallback (CQT / harmonic comb filtering).
Extracts MIDI pitches (21-108), velocities, real note durations, and sustain zones.
"""

import os
import logging
from typing import List, Dict, Any, Optional, Tuple
import numpy as np
import scipy.signal
import librosa

logger = logging.getLogger(__name__)


class PianoTranscriber:
    """
    Polyphonic piano transcriber.
    Supports ByteDance AI model if present, with an intelligent CQT harmonic
    spectral fallback ensuring immediate autonomous operation without GPU or downloaded weights.
    """

    def __init__(
        self,
        sample_rate: int = 16000,  # ByteDance uses 16kHz; fallback works natively at 16k or 44.1k
        hop_length: int = 512,
        min_midi: int = 21,   # A0 (27.5 Hz)
        max_midi: int = 108,  # C8 (4186 Hz)
        energy_threshold: float = 0.12,
        sustain_decay_ratio: float = 0.75
    ):
        self.sample_rate = sample_rate
        self.hop_length = hop_length
        self.min_midi = min_midi
        self.max_midi = max_midi
        self.num_pitches = max_midi - min_midi + 1  # 88 piano keys
        self.energy_threshold = energy_threshold
        self.sustain_decay_ratio = sustain_decay_ratio

        self._has_bytedance_model = self._check_bytedance_availability()

    def _check_bytedance_availability(self) -> bool:
        """Check if ByteDance piano_transcription_inference is available."""
        try:
            import piano_transcription_inference
            logger.info("ByteDance piano_transcription_inference package detected")
            return True
        except ImportError:
            logger.info("ByteDance package not installed; polyphonic spectral harmonic engine enabled")
            return False

    def transcribe(
        self,
        audio: np.ndarray,
        sr: Optional[int] = None,
        use_fallback_override: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Transcribe polyphonic piano audio into structured note events.

        Args:
            audio: 1D float32 numpy array.
            sr: Audio sample rate.
            use_fallback_override: If True, forces the spectral harmonic engine.

        Returns:
            List of notes: [{"pitch": int, "start_time": float, "end_time": float,
                             "duration": float, "velocity": int, "sustain": bool}]
        """
        if len(audio) == 0:
            return []

        orig_sr = sr or self.sample_rate
        if orig_sr != self.sample_rate:
            audio = librosa.resample(audio, orig_sr=orig_sr, target_sr=self.sample_rate)

        # Normalize audio input
        max_val = np.max(np.abs(audio))
        if max_val > 1e-4:
            audio = audio / max_val
        else:
            return []

        if self._has_bytedance_model and not use_fallback_override:
            try:
                return self._transcribe_bytedance(audio)
            except Exception as exc:
                logger.warning("ByteDance model inference failed (%s); switching to spectral fallback", exc)

        return self._transcribe_spectral_polyphonic(audio)

    def _transcribe_bytedance(self, audio: np.ndarray) -> List[Dict[str, Any]]:
        """Inference using ByteDance piano transcription model."""
        import piano_transcription_inference
        from piano_transcription_inference import PianoTranscription, sample_rate as bd_sr

        if self.sample_rate != bd_sr:
            audio_16k = librosa.resample(audio, orig_sr=self.sample_rate, target_sr=bd_sr)
        else:
            audio_16k = audio

        device = "cuda" if os.environ.get("CUDA_VISIBLE_DEVICES") else "cpu"
        transcriptor = PianoTranscription(device=device, checkpoint_path=None)
        transcribed_dict = transcriptor.transcribe(audio_16k, None)

        raw_notes = transcribed_dict.get("est_note", [])
        events = []
        for note in raw_notes:
            pitch = int(note["pitch"])
            if self.min_midi <= pitch <= self.max_midi:
                start = float(note["onset_time"])
                end = float(note["offset_time"])
                dur = max(0.05, end - start)
                vel = int(note.get("velocity", 80))
                events.append({
                    "pitch": pitch,
                    "start_time": start,
                    "end_time": end,
                    "duration": dur,
                    "velocity": vel,
                    "sustain": False
                })

        events.sort(key=lambda x: (x["start_time"], x["pitch"]))
        return events

    def _transcribe_spectral_polyphonic(self, audio: np.ndarray) -> List[Dict[str, Any]]:
        """
        Intelligent standalone polyphonic spectral transcription engine.
        Uses 88-bin Constant-Q Transform (CQT), harmonic comb summation,
        pitch-wise onset picking, note duration tracking, and sustain pedal resonance analysis.
        """
        fmin = librosa.midi_to_hz(self.min_midi)  # A0 ~ 27.5 Hz

        # Compute Constant-Q Transform (88 bins, 12 bins per octave matching piano keys exactly)
        try:
            cqt_spec = np.abs(librosa.cqt(
                audio,
                sr=self.sample_rate,
                hop_length=self.hop_length,
                fmin=fmin,
                n_bins=self.num_pitches,
                bins_per_octave=12
            ))
        except Exception as ex:
            logger.error("CQT computation failed: %s", ex)
            return []

        num_bins, num_frames = cqt_spec.shape
        if num_frames < 2:
            return []

        # 1. Harmonic Comb Enhancement (Comb Filter / Harmonic Summation)
        # For each pitch bin m, reinforce the fundamental using upper harmonics (+12 st = oct 1, +19 st = fifth, +24 st = oct 2)
        salience = np.copy(cqt_spec)
        harmonic_intervals = [
            (12, 0.40),   # 2nd harmonic (octave)
            (19, 0.25),   # 3rd harmonic (perfect fifth)
            (24, 0.15)    # 4th harmonic (double octave)
        ]

        for shift, weight in harmonic_intervals:
            if shift < num_bins:
                salience[:num_bins - shift, :] += weight * cqt_spec[shift:, :]

        # Normalize salience matrix
        max_sal = np.max(salience)
        if max_sal > 0:
            salience /= max_sal

        # 2. Sustain Pedal Detection (Resonance over time)
        sustain_frames = self._detect_sustain_zones(cqt_spec)

        # 3. Multi-Pitch Onset Detection and Note Segmentation
        # First positive difference along time (spectral novelty per pitch) with prepended zeros
        diff_sal = np.diff(salience, axis=1, prepend=np.zeros((num_bins, 1)))
        diff_sal = np.maximum(0, diff_sal)  # Half-wave rectification

        time_axis = librosa.frames_to_time(
            np.arange(num_frames),
            sr=self.sample_rate,
            hop_length=self.hop_length
        )

        detected_notes = []

        for bin_idx in range(num_bins):
            pitch = self.min_midi + bin_idx
            pitch_curve = salience[bin_idx, :]
            onset_curve = diff_sal[bin_idx, :]

            # Adaptive threshold for this pitch based on baseline floor and min energy threshold
            min_floor = float(np.min(pitch_curve))
            local_thresh = max(self.energy_threshold, min_floor + 0.05)

            # Find onset peaks with boundary padding
            pad_onset = np.pad(onset_curve, (1, 1), mode="constant")
            peaks_pad, properties = scipy.signal.find_peaks(
                pad_onset,
                height=local_thresh * 0.3,
                distance=max(2, int(0.08 * self.sample_rate / self.hop_length))
            )
            peaks = [p - 1 for p in peaks_pad if 0 <= p - 1 < num_frames]

            for start_frame in peaks:
                # Check amplitude at/around attack frame
                window_end = min(num_frames, start_frame + 3)
                onset_amp = float(np.max(pitch_curve[start_frame:window_end]))
                if onset_amp < local_thresh:
                    continue

                # Trace note offset / duration
                end_frame = start_frame + 1
                decay_thresh = onset_amp * 0.25

                while end_frame < num_frames:
                    is_sustained = sustain_frames[end_frame]
                    current_amp = pitch_curve[end_frame]

                    # If sustain pedal is down, tolerate lower amplitude decay
                    effective_thresh = decay_thresh * (0.6 if is_sustained else 1.0)

                    # Stop if note decays below threshold
                    if current_amp < effective_thresh:
                        break

                    # Stop if a new onset for the same pitch occurs
                    if end_frame - 1 in peaks and (end_frame - 1) > start_frame:
                        break

                    end_frame += 1

                start_t = float(time_axis[start_frame])
                end_t = float(time_axis[min(end_frame, num_frames - 1)])
                duration = max(0.06, end_t - start_t)

                # Map amplitude to velocity [35, 127]
                velocity = int(np.clip(35 + (onset_amp ** 0.5) * 92, 35, 127))

                # Check sustain zone coverage
                sustain_coverage = np.mean(sustain_frames[start_frame:end_frame]) if end_frame > start_frame else 0.0

                detected_notes.append({
                    "pitch": pitch,
                    "start_time": round(start_t, 3),
                    "end_time": round(start_t + duration, 3),
                    "duration": round(duration, 3),
                    "velocity": velocity,
                    "sustain": bool(sustain_coverage > 0.5)
                })

        # Sort chronologically, then by pitch ascending
        detected_notes.sort(key=lambda n: (n["start_time"], n["pitch"]))

        # Remove duplicate overlaps for the same pitch within 40ms
        pruned_notes = []
        for note in detected_notes:
            if pruned_notes:
                prev = pruned_notes[-1]
                if prev["pitch"] == note["pitch"] and abs(note["start_time"] - prev["start_time"]) < 0.040:
                    continue
            pruned_notes.append(note)

        logger.info("Transcribed %d polyphonic piano notes (pitches %d-%d)",
                    len(pruned_notes),
                    min((n["pitch"] for n in pruned_notes), default=0),
                    max((n["pitch"] for n in pruned_notes), default=0))
        return pruned_notes

    def _detect_sustain_zones(self, cqt_spec: np.ndarray) -> np.ndarray:
        """
        Detect sustain pedal zones by tracking prolonged spectral energy
        and low-frequency sympathetic resonance.
        """
        num_bins, num_frames = cqt_spec.shape
        # Low frequency resonance (bins 0 to 24, corresponding to low piano strings)
        low_res = np.mean(cqt_spec[:24, :], axis=0)
        overall_energy = np.mean(cqt_spec, axis=0)

        # Rolling standard deviation / decay rate of overall energy
        # Sustain pedal maintains high energy with very slow decay (low negative derivative)
        diff_energy = np.diff(overall_energy)
        diff_energy = np.pad(diff_energy, (0, 1), mode="edge")

        mean_energy = np.mean(overall_energy)
        sustain_mask = (overall_energy > mean_energy * 0.8) & (diff_energy > -0.05) & (low_res > np.mean(low_res) * 0.9)

        return sustain_mask.astype(bool)
