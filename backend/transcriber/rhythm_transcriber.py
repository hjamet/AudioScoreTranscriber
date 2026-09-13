"""
Rhythm Transcriber Module for AudioScoreTranscriber.
Mode 1: "Rythme Seul".
Detects percussive onsets (table tapping, handclaps, "tac-tac" onomatopoeia)
using High Frequency Content (HFC) ODF with attack backtracking,
adaptive noise thresholding, and a 30 ms refractory window.
"""

import logging
from typing import List, Dict, Any, Optional, Tuple
import numpy as np
import scipy.signal
import librosa

logger = logging.getLogger(__name__)


class RhythmTranscriber:
    """
    High-precision percussive onset and rhythm transcriber.
    Specialized in acoustic taps, clicks, handclaps and spoken rhythm beats.
    """

    def __init__(
        self,
        sample_rate: int = 44100,
        hop_length: int = 256,
        n_fft: int = 1024,
        refractory_sec: float = 0.030,  # 30 ms refractory window
        adaptive_delta: float = 0.05,
        min_rms_threshold: float = 0.01
    ):
        self.sample_rate = sample_rate
        self.hop_length = hop_length
        self.n_fft = n_fft
        self.refractory_sec = refractory_sec
        self.adaptive_delta = adaptive_delta
        self.min_rms_threshold = min_rms_threshold

    def compute_hfc_odf(self, y: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """
        Compute High Frequency Content (HFC) Onset Detection Function.
        Weights higher frequency bins linearly and takes positive first difference (flux)
        to produce sharp, localized attack peaks.
        Returns:
            odf_norm: normalized onset detection novelty function across frames.
            rms: frame-level root-mean-square energy.
        """
        stft = librosa.stft(y, n_fft=self.n_fft, hop_length=self.hop_length)
        magnitude = np.abs(stft)

        # High Frequency Content: sum(k * |X(k, t)|^2)
        freq_weights = np.arange(magnitude.shape[0], dtype=np.float32)[:, np.newaxis]
        hfc = np.sum(freq_weights * (magnitude ** 2), axis=0)

        # First positive difference (half-wave rectified novelty)
        hfc_novelty = np.maximum(0, np.diff(hfc, prepend=hfc[0]))

        # Spectral flux via librosa onset_strength
        flux = librosa.onset.onset_strength(
            y=y,
            sr=self.sample_rate,
            hop_length=self.hop_length,
            n_fft=self.n_fft
        )
        if len(flux) < len(hfc_novelty):
            flux = np.pad(flux, (0, len(hfc_novelty) - len(flux)), mode="edge")
        elif len(flux) > len(hfc_novelty):
            flux = flux[:len(hfc_novelty)]

        # Normalize both novelty signals
        max_hfc = np.max(hfc_novelty)
        if max_hfc > 0:
            hfc_novelty /= max_hfc

        max_flux = np.max(flux)
        if max_flux > 0:
            flux /= max_flux

        # Blended HFC onset novelty
        combined_odf = 0.65 * hfc_novelty + 0.35 * flux

        # Frame RMS energy
        rms = librosa.feature.rms(y=y, frame_length=self.n_fft, hop_length=self.hop_length)[0]
        if len(rms) < len(combined_odf):
            rms = np.pad(rms, (0, len(combined_odf) - len(rms)), mode="edge")
        elif len(rms) > len(combined_odf):
            rms = rms[:len(combined_odf)]

        return combined_odf, rms

    def transcribe(
        self,
        audio: np.ndarray,
        sr: Optional[int] = None,
        default_pitch: int = 60
    ) -> List[Dict[str, Any]]:
        """
        Transcribe rhythm-only audio into a list of timed percussive note events.

        Args:
            audio: 1D float32 numpy array.
            sr: Audio sample rate (defaults to self.sample_rate).
            default_pitch: MIDI pitch to assign to unpitched percussive beats (default 60 = Middle C).

        Returns:
            List of note event dictionaries with exact physical timestamps and velocities.
        """
        if sr is not None and sr != self.sample_rate:
            # Resample if needed
            audio = librosa.resample(audio, orig_sr=sr, target_sr=self.sample_rate)

        if len(audio) < self.n_fft:
            logger.warning("Audio buffer too short for rhythm transcription (%d samples)", len(audio))
            return []

        # Peak amplitude check
        peak_amp = np.max(np.abs(audio))
        if peak_amp < self.min_rms_threshold:
            logger.info("Audio signal below ambient noise floor (peak=%.4f)", peak_amp)
            return []

        # 1. Compute HFC Onset Detection Function
        odf, rms = self.compute_hfc_odf(audio)

        # 2. Adaptive thresholding and peak picking
        # Convert refractory window to frames
        wait_frames = max(1, int(self.refractory_sec * self.sample_rate / self.hop_length))

        # Backtracking: locate the true attack start preceding the energy peak
        raw_onsets = librosa.onset.onset_detect(
            onset_envelope=odf,
            sr=self.sample_rate,
            hop_length=self.hop_length,
            backtrack=True,
            energy=rms,
            delta=self.adaptive_delta,
            wait=wait_frames
        )

        if len(raw_onsets) == 0:
            return []

        # Convert frame indices to physical time (seconds)
        onset_times = librosa.frames_to_time(raw_onsets, sr=self.sample_rate, hop_length=self.hop_length)

        # 3. Post-filtering with strict 30 ms refractory lockout and noise floor check
        filtered_events = []
        last_time = -1.0

        for frame_idx, t in zip(raw_onsets, onset_times):
            # Check refractory window
            if last_time >= 0 and (t - last_time) < self.refractory_sec:
                continue

            # Check local RMS energy in the attack burst window [t, t + 40ms]
            local_start = max(0, int(t * self.sample_rate))
            local_end = min(len(audio), local_start + int(0.040 * self.sample_rate))
            local_slice = audio[local_start:local_end]
            local_rms = float(np.sqrt(np.mean(local_slice ** 2))) if len(local_slice) > 0 else 0.0
            local_peak = float(np.max(np.abs(local_slice))) if len(local_slice) > 0 else 0.0

            # Reject if the attack burst RMS does not exceed ambient noise threshold
            if local_rms < self.min_rms_threshold or local_peak < (self.min_rms_threshold * 2):
                continue

            # Dynamic velocity scaled between 40 and 127
            velocity = int(np.clip(40 + (local_peak / max(peak_amp, 1e-4)) * 87, 40, 127))

            filtered_events.append({
                "time": float(t),
                "frame": int(frame_idx),
                "peak_amplitude": float(local_peak),
                "velocity": velocity,
                "pitch": default_pitch,
                "type": "percussive"
            })
            last_time = t

        if not filtered_events:
            return []

        # 4. Calculate Inter-Onset Intervals (IOI) and assign event durations
        audio_duration = float(len(audio)) / float(self.sample_rate)
        events_with_duration = []

        for i, ev in enumerate(filtered_events):
            t_curr = ev["time"]
            if i + 1 < len(filtered_events):
                t_next = filtered_events[i + 1]["time"]
                ioi = float(t_next - t_curr)
                # Durations for percussive hits are typically short or fill up to 80% of IOI
                duration = min(0.20, ioi * 0.85)
            else:
                ioi = max(0.25, audio_duration - t_curr)
                duration = min(0.20, ioi * 0.85)

            events_with_duration.append({
                "pitch": ev["pitch"],
                "start_time": float(t_curr),
                "end_time": float(t_curr + duration),
                "duration": float(duration),
                "ioi": float(ioi),
                "velocity": ev["velocity"],
                "type": "percussive"
            })

        logger.info("Transcribed %d percussive onsets from %.2fs of audio",
                    len(events_with_duration), audio_duration)
        return events_with_duration

    def estimate_tempo_bpm(self, events: List[Dict[str, Any]], fallback_bpm: float = 120.0) -> float:
        """
        Estimate dominant tempo in BPM from IOI clustering.
        """
        if len(events) < 3:
            return fallback_bpm

        iois = [ev["ioi"] for ev in events if 0.15 <= ev["ioi"] <= 2.0]
        if not iois:
            return fallback_bpm

        # Median IOI is robust against outliers and syncopation
        median_ioi = float(np.median(iois))

        # Convert to BPM
        bpm = 60.0 / median_ioi

        # Normalize to standard musical tempo range [60, 180]
        while bpm < 65.0:
            bpm *= 2.0
        while bpm > 180.0:
            bpm /= 2.0

        return round(bpm, 1)
