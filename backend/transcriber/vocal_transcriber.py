"""
Vocal Transcriber Module for AudioScoreTranscriber.
Mode 3: "Chant Melodique".
Estimates F0 using pYIN, applies a 220 ms temporal median filter to cancel
5-7 Hz vocal vibrato while preserving note attack transitions, detects stable plateaux
(>= 100 ms), and eliminates glissandos, breath, and attack noise.
"""

import logging
from typing import List, Dict, Any, Optional, Tuple, Callable
import numpy as np
import scipy.signal
import scipy.ndimage
import librosa

logger = logging.getLogger(__name__)


class VocalTranscriber:
    """
    Monophonic vocal melody transcriber with anti-vibrato smoothing
    and stable plateau segmentation.
    """

    def __init__(
        self,
        sample_rate: int = 22050,
        hop_length: int = 256,
        frame_length: int = 2048,
        fmin_note: str = "C2",   # ~65 Hz
        fmax_note: str = "C7",   # ~2093 Hz
        vibrato_window_sec: float = 0.220,  # 220 ms anti-vibrato median filter
        min_plateau_sec: float = 0.100,     # 100 ms stability threshold
        max_pitch_drift_st: float = 0.65,   # Max semitone variation inside a plateau
        min_rms_threshold: float = 0.015,
        progress_callback: Optional[Callable[[int, str], None]] = None
    ):
        self.sample_rate = sample_rate
        self.hop_length = hop_length
        self.frame_length = frame_length
        self.fmin = librosa.note_to_hz(fmin_note)
        self.fmax = librosa.note_to_hz(fmax_note)
        self.vibrato_window_sec = vibrato_window_sec
        self.min_plateau_sec = min_plateau_sec
        self.max_pitch_drift_st = max_pitch_drift_st
        self.min_rms_threshold = min_rms_threshold
        self.progress_callback = progress_callback

    def _report_progress(
        self,
        cb: Optional[Callable[[int, str], None]],
        percent: int,
        stage: str
    ):
        """Dispatch progress update to local or instance callback."""
        active_cb = cb or self.progress_callback
        if active_cb is not None:
            try:
                active_cb(int(percent), str(stage))
            except Exception as exc:
                logger.debug("Progress callback exception: %s", exc)

    def transcribe(
        self,
        audio: np.ndarray,
        sr: Optional[int] = None,
        progress_callback: Optional[Callable[[int, str], None]] = None
    ) -> List[Dict[str, Any]]:
        """
        Transcribe singing voice audio into structured melodic note events.

        Args:
            audio: 1D float32 audio array.
            sr: Input sample rate.
            progress_callback: Optional callback(percent: int, stage: str) reporting progress.

        Returns:
            List of note dicts: [{"pitch": int, "start_time": float, "end_time": float,
                                 "duration": float, "velocity": int, "cents_deviation": float}]
        """
        if len(audio) < self.frame_length:
            logger.warning("Audio buffer too short for vocal transcription")
            self._report_progress(progress_callback, 100, "Audio trop court")
            return []

        orig_sr = sr or self.sample_rate
        if orig_sr != self.sample_rate:
            audio = librosa.resample(audio, orig_sr=orig_sr, target_sr=self.sample_rate)

        # Normalize audio
        peak = np.max(np.abs(audio))
        if peak < self.min_rms_threshold:
            logger.info("Vocal signal below noise floor")
            self._report_progress(progress_callback, 100, "Signal en-dessous du seuil")
            return []

        # 1. Fundamental frequency estimation via pYIN
        self._report_progress(progress_callback, 10, "Début extraction pitch pYIN")
        self._report_progress(progress_callback, 20, "Inférence pYIN en cours")
        try:
            f0, voiced_flag, voiced_probs = librosa.pyin(
                audio,
                sr=self.sample_rate,
                fmin=self.fmin,
                fmax=self.fmax,
                frame_length=self.frame_length,
                hop_length=self.hop_length,
                fill_na=None
            )
        except Exception as exc:
            logger.error("pYIN F0 extraction failed: %s", exc)
            self._report_progress(progress_callback, 100, "Erreur extraction pYIN")
            return []

        self._report_progress(progress_callback, 60, "Inférence pYIN terminée")

        if f0 is None or len(f0) == 0:
            self._report_progress(progress_callback, 100, "Aucun F0 détecté")
            return []

        num_frames = len(f0)
        times = librosa.frames_to_time(np.arange(num_frames), sr=self.sample_rate, hop_length=self.hop_length)

        # Frame RMS energy to eliminate breaths and mic handling
        rms = librosa.feature.rms(y=audio, frame_length=self.frame_length, hop_length=self.hop_length)[0]
        if len(rms) < num_frames:
            rms = np.pad(rms, (0, num_frames - len(rms)), mode="edge")
        elif len(rms) > num_frames:
            rms = rms[:num_frames]

        # 2. Convert F0 to continuous MIDI pitch
        midi_pitches = np.full(num_frames, np.nan, dtype=np.float32)
        valid_mask = (f0 > 0) & (~np.isnan(f0)) & voiced_flag & (rms >= self.min_rms_threshold)

        for i in range(num_frames):
            if valid_mask[i]:
                midi_pitches[i] = 69.0 + 12.0 * np.log2(f0[i] / 440.0)

        # 3. Apply 220 ms Temporal Median Filter (Anti-Vibrato)
        # Neutralizes 5-7 Hz vocal vibrato without blurring note attack transitions
        self._report_progress(progress_callback, 70, "Post-traitement & filtrage vibrato/glissando")
        frame_rate = self.sample_rate / self.hop_length
        median_kernel_size = int(round(self.vibrato_window_sec * frame_rate))
        if median_kernel_size % 2 == 0:
            median_kernel_size += 1
        median_kernel_size = max(3, median_kernel_size)

        filtered_midi = self._apply_nan_median_filter(midi_pitches, median_kernel_size)

        # 4. Stable Plateau Detection (>= 100 ms) and Glissando/Transient Elimination
        self._report_progress(progress_callback, 85, "Segmentation en notes")
        min_plateau_frames = max(2, int(round(self.min_plateau_sec * frame_rate)))
        notes = self._extract_plateaux(
            filtered_midi,
            times,
            rms,
            min_frames=min_plateau_frames,
            max_drift=self.max_pitch_drift_st
        )

        logger.info("Transcribed %d vocal melodic notes", len(notes))
        self._report_progress(progress_callback, 100, "Finalisation")
        return notes

    def _apply_nan_median_filter(self, signal: np.ndarray, kernel_size: int) -> np.ndarray:
        """
        Apply 1D median filter ignoring NaN values, smoothing vibrato
        across continuous voiced segments.
        """
        half = kernel_size // 2
        n = len(signal)
        output = np.copy(signal)

        for i in range(n):
            if np.isnan(signal[i]):
                continue
            start = max(0, i - half)
            end = min(n, i + half + 1)
            window = signal[start:end]
            valid = window[~np.isnan(window)]
            if len(valid) >= (len(window) // 3 + 1):
                output[i] = float(np.median(valid))
            else:
                output[i] = np.nan

        return output

    def _extract_plateaux(
        self,
        midi_pitches: np.ndarray,
        times: np.ndarray,
        rms: np.ndarray,
        min_frames: int,
        max_drift: float
    ) -> List[Dict[str, Any]]:
        """
        Identify stable pitch plateaux (>= 100 ms) and eliminate glissandos
        and transient attack/breath noises.
        """
        n = len(midi_pitches)
        raw_plateaux = []
        in_plateau = False
        start_idx = 0
        current_pitches = []

        # Local derivative to detect glissando / pitch slides
        pitch_diff = np.zeros(n)
        for i in range(1, n):
            if not np.isnan(midi_pitches[i]) and not np.isnan(midi_pitches[i - 1]):
                pitch_diff[i] = abs(midi_pitches[i] - midi_pitches[i - 1])
            else:
                pitch_diff[i] = 0.0

        for i in range(n):
            val = midi_pitches[i]
            is_valid = (not np.isnan(val)) and (pitch_diff[i] < 0.8)  # Reject rapid glissando frames

            if is_valid:
                if not in_plateau:
                    in_plateau = True
                    start_idx = i
                    current_pitches = [val]
                else:
                    # Check if pitch is still within plateau tolerance
                    local_median = np.median(current_pitches)
                    if abs(val - local_median) <= max_drift:
                        current_pitches.append(val)
                    else:
                        # Plateau boundary reached due to note change
                        if len(current_pitches) >= min_frames:
                            raw_plateaux.append((start_idx, i - 1, current_pitches))
                        # Start new plateau with the new pitch
                        start_idx = i
                        current_pitches = [val]
            else:
                if in_plateau:
                    if len(current_pitches) >= min_frames:
                        raw_plateaux.append((start_idx, i - 1, current_pitches))
                    in_plateau = False
                    current_pitches = []

        if in_plateau and len(current_pitches) >= min_frames:
            raw_plateaux.append((start_idx, n - 1, current_pitches))

        # Convert plateaux to notes
        notes = []
        for s_idx, e_idx, p_vals in raw_plateaux:
            med_pitch = float(np.median(p_vals))
            nominal_midi = int(round(med_pitch))
            cents_deviation = float(round((med_pitch - nominal_midi) * 100.0, 1))

            start_t = float(times[s_idx])
            end_t = float(times[min(e_idx + 1, len(times) - 1)])
            duration = max(0.08, end_t - start_t)

            # RMS velocity
            avg_rms = float(np.mean(rms[s_idx:e_idx + 1])) if e_idx >= s_idx else 0.05
            velocity = int(np.clip(45 + (avg_rms * 250), 45, 127))

            notes.append({
                "pitch": nominal_midi,
                "start_time": round(start_t, 3),
                "end_time": round(start_t + duration, 3),
                "duration": round(duration, 3),
                "velocity": velocity,
                "cents_deviation": cents_deviation
            })

        # Merge adjacent identical notes with minimal gap (< 60 ms)
        merged_notes = []
        for note in notes:
            if merged_notes:
                prev = merged_notes[-1]
                if prev["pitch"] == note["pitch"] and (note["start_time"] - prev["end_time"]) < 0.060:
                    prev["end_time"] = note["end_time"]
                    prev["duration"] = round(prev["end_time"] - prev["start_time"], 3)
                    continue
            merged_notes.append(note)

        return merged_notes
