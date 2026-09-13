"""
Adaptive Quantizer Module for AudioScoreTranscriber.
Implements the engraving principles of Elaine Gould (Behind Bars):
- Chord clumping for attacks within delta_t < 35 ms.
- Binary grid (1/4, 1/8, 1/16, 1/32) vs. Ternary grid (triplets, sextuplets)
  arbitrated by minimum complexity penalty.
- Rigorous nested rests reconstruction (soupirs, demi-soupirs, quarts de soupir)
  respecting the "invisible half-bar" rule and metric beat hierarchies.
"""

import math
import logging
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)


# Standard note duration names according to Gould
DURATION_NAMES = {
    4.0: ("whole", "pause"),
    3.0: ("dotted-half", "point-demi-pause"),
    2.0: ("half", "demi-pause"),
    1.5: ("dotted-quarter", "soupir-pointe"),
    1.0: ("quarter", "soupir"),
    0.75: ("dotted-eighth", "demi-soupir-pointe"),
    0.5: ("eighth", "demi-soupir"),
    0.375: ("dotted-16th", "quart-de-soupir-pointe"),
    0.25: ("16th", "quart-de-soupir"),
    0.125: ("32nd", "huitieme-de-soupir"),
    # Ternary / Triplet durations (in beats)
    2.0 / 3.0: ("quarter-triplet", "soupir-triolet"),
    1.0 / 3.0: ("eighth-triplet", "demi-soupir-triolet"),
    1.0 / 6.0: ("16th-triplet", "quart-soupir-triolet"),
}


class GouldAdaptiveQuantizer:
    """
    Adaptive music score quantizer implementing Elaine Gould's Behind Bars rules.
    """

    def __init__(
        self,
        time_signature: Tuple[int, int] = (4, 4),
        default_bpm: float = 120.0,
        chord_clump_window_sec: float = 0.035,  # 35 ms chord clumping
        ternary_penalty: float = 0.25,          # Gould simplicity penalty against triplets
        min_duration_beats: float = 0.125       # 32nd note resolution
    ):
        self.time_signature = time_signature
        self.default_bpm = default_bpm
        self.chord_clump_window_sec = chord_clump_window_sec
        self.ternary_penalty = ternary_penalty
        self.min_duration_beats = min_duration_beats

        self.beats_per_measure = float(self.time_signature[0])
        self.beat_unit = float(self.time_signature[1])  # 4 = quarter note is 1 beat

    def quantize(
        self,
        raw_events: List[Dict[str, Any]],
        bpm: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Quantize continuous physical audio events into a structured measure-by-measure score.

        Args:
            raw_events: List of transcribed note events with start_time, duration, pitch, velocity.
            bpm: Tempo in BPM (estimated or specified).

        Returns:
            Dictionary containing measures, items (notes/chords/rests), tempo, and time signature.
        """
        if not raw_events:
            return self._empty_score(bpm or self.default_bpm)

        active_bpm = bpm if (bpm is not None and bpm > 0) else self.default_bpm
        sec_per_beat = 60.0 / active_bpm

        # 1. Chord Clumping (window < 35 ms)
        clumped_events = self._clump_chords(raw_events, self.chord_clump_window_sec)

        # 2. Convert physical seconds to continuous beats
        beat_events = []
        for ev in clumped_events:
            start_beat = ev["start_time"] / sec_per_beat
            duration_beats = max(self.min_duration_beats, ev["duration"] / sec_per_beat)
            beat_events.append({
                "start_beat": start_beat,
                "duration_beats": duration_beats,
                "pitches": ev["pitches"],
                "velocities": ev["velocities"],
                "type": "chord" if len(ev["pitches"]) > 1 else "note"
            })

        # 3. Grid arbitration (Binary vs Ternary) per event
        quantized_events = []
        for ev in beat_events:
            q_start, q_dur, is_triplet = self._quantize_event_gould(
                ev["start_beat"],
                ev["duration_beats"]
            )
            quantized_events.append({
                "start_beat": q_start,
                "end_beat": round(q_start + q_dur, 4),
                "duration_beats": q_dur,
                "pitches": ev["pitches"],
                "velocities": ev["velocities"],
                "type": ev["type"],
                "is_triplet": is_triplet
            })

        # Sort chronologically
        quantized_events.sort(key=lambda x: x["start_beat"])

        # 4. Partition events into Measures and reconstruct Gould nested rests
        measures = self._build_measures_with_rests(quantized_events, active_bpm)

        return {
            "tempo_bpm": active_bpm,
            "time_signature": list(self.time_signature),
            "total_measures": len(measures),
            "measures": measures
        }

    def _clump_chords(
        self,
        events: List[Dict[str, Any]],
        delta_t: float
    ) -> List[Dict[str, Any]]:
        """
        Group note attacks occurring within delta_t < 35 ms into a single chord.
        """
        if not events:
            return []

        sorted_events = sorted(events, key=lambda x: x["start_time"])
        clumped = []
        current_cluster = [sorted_events[0]]

        for ev in sorted_events[1:]:
            cluster_base_time = current_cluster[0]["start_time"]
            if (ev["start_time"] - cluster_base_time) <= delta_t:
                current_cluster.append(ev)
            else:
                clumped.append(self._merge_cluster(current_cluster))
                current_cluster = [ev]

        if current_cluster:
            clumped.append(self._merge_cluster(current_cluster))

        return clumped

    def _merge_cluster(self, cluster: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Merge a group of notes into a single chord event."""
        base_time = float(min(n["start_time"] for n in cluster))
        # Gould duration: median or maximum duration in cluster
        max_duration = float(max(n["duration"] for n in cluster))

        # Deduplicate pitches and preserve velocities
        pitches = []
        velocities = []
        for n in cluster:
            p = int(n["pitch"])
            v = int(n.get("velocity", 80))
            if p not in pitches:
                pitches.append(p)
                velocities.append(v)

        return {
            "start_time": base_time,
            "duration": max_duration,
            "pitches": pitches,
            "velocities": velocities
        }

    def _quantize_event_gould(
        self,
        start_beat: float,
        duration_beats: float
    ) -> Tuple[float, float, bool]:
        """
        Arbitrate between binary grid and ternary grid according to Gould rules.
        """
        # Candidate binary grid points inside a beat
        # 1/16 = 0.25, 1/32 = 0.125
        grid_binary_start = round(start_beat / 0.125) * 0.125
        err_bin_start = abs(start_beat - grid_binary_start)

        # Candidate ternary grid points inside a beat: triplets (1/3, 1/6)
        grid_triplet_start = round(start_beat * 3.0) / 3.0
        grid_sextuplet_start = round(start_beat * 6.0) / 6.0

        err_triplet_start = min(abs(start_beat - grid_triplet_start), abs(start_beat - grid_sextuplet_start))

        # Gould arbitration: Triplet must win over binary with a margin exceeding ternary_penalty
        is_triplet = (err_triplet_start + self.ternary_penalty) < err_bin_start

        if is_triplet:
            best_start = grid_triplet_start if abs(start_beat - grid_triplet_start) <= abs(start_beat - grid_sextuplet_start) else grid_sextuplet_start
            q_dur = self._snap_duration_ternary(duration_beats)
        else:
            best_start = grid_binary_start
            q_dur = self._snap_duration_binary(duration_beats)

        return round(best_start, 4), round(q_dur, 4), is_triplet

    def _snap_duration_binary(self, dur: float) -> float:
        """Snap duration to standard binary musical lengths."""
        binary_candidates = [0.125, 0.25, 0.375, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0]
        # Find closest candidate
        best = min(binary_candidates, key=lambda c: abs(c - dur))
        return best

    def _snap_duration_ternary(self, dur: float) -> float:
        """Snap duration to standard ternary musical lengths."""
        ternary_candidates = [1.0 / 6.0, 1.0 / 3.0, 2.0 / 3.0, 1.0, 4.0 / 3.0, 2.0]
        best = min(ternary_candidates, key=lambda c: abs(c - dur))
        return best

    def _build_measures_with_rests(
        self,
        events: List[Dict[str, Any]],
        bpm: float
    ) -> List[Dict[str, Any]]:
        """
        Organize notes into measures, handling ties across bar lines and reconstructing
        Gould-compliant rests respecting beat divisions and the half-bar line.
        """
        measure_len = self.beats_per_measure
        half_bar = measure_len / 2.0  # 2.0 beats in 4/4

        # Determine total measures needed
        max_end_beat = max((ev["end_beat"] for ev in events), default=measure_len)
        total_measures = max(1, int(math.ceil(max_end_beat / measure_len)))

        measures = []
        for m_idx in range(total_measures):
            m_start_beat = m_idx * measure_len
            m_end_beat = (m_idx + 1) * measure_len

            # Find events that fall in or overlap this measure
            measure_items = []
            current_pos = m_start_beat

            # Filter and clip events to this measure
            m_events = []
            for ev in events:
                ev_start = ev["start_beat"]
                ev_end = ev["end_beat"]

                # Overlap test
                if ev_end > m_start_beat and ev_start < m_end_beat:
                    # Clip to measure boundaries
                    item_start = max(m_start_beat, ev_start)
                    item_end = min(m_end_beat, ev_end)
                    item_dur = item_end - item_start

                    # Check for ties
                    tie_start = ev_end > m_end_beat
                    tie_stop = ev_start < m_start_beat

                    m_events.append({
                        "beat_in_measure": round(item_start - m_start_beat, 4),
                        "duration_beats": round(item_dur, 4),
                        "pitches": ev["pitches"],
                        "velocities": ev["velocities"],
                        "type": ev["type"],
                        "is_triplet": ev["is_triplet"],
                        "tie_start": tie_start,
                        "tie_stop": tie_stop
                    })

            # Sort events inside measure
            m_events.sort(key=lambda x: x["beat_in_measure"])

            # Reconstruct rests and notes sequentially
            meas_cursor = 0.0

            for ev in m_events:
                ev_pos = ev["beat_in_measure"]

                # If there is a silence before this event, reconstruct Gould rests
                if ev_pos > meas_cursor + 0.05:
                    rest_gap = ev_pos - meas_cursor
                    reconstructed_rests = self._decompose_rests_gould(meas_cursor, rest_gap, half_bar, measure_len)
                    measure_items.extend(reconstructed_rests)
                    meas_cursor = ev_pos

                # Add note/chord item
                ev["duration_type"] = self._get_duration_name(ev["duration_beats"], ev["is_triplet"])
                measure_items.append(ev)
                meas_cursor = round(ev_pos + ev["duration_beats"], 4)

            # Trailing rest until end of measure
            if meas_cursor < measure_len - 0.05:
                rest_gap = measure_len - meas_cursor
                reconstructed_rests = self._decompose_rests_gould(meas_cursor, rest_gap, half_bar, measure_len)
                measure_items.extend(reconstructed_rests)

            # If measure is completely empty, it is a whole rest (pause)
            if not measure_items:
                measure_items = [{
                    "beat_in_measure": 0.0,
                    "duration_beats": measure_len,
                    "type": "rest",
                    "duration_type": "whole",
                    "gould_name": "pause",
                    "pitches": [],
                    "velocities": []
                }]

            measures.append({
                "measure_number": m_idx + 1,
                "items": measure_items
            })

        return measures

    def _decompose_rests_gould(
        self,
        start_beat: float,
        duration: float,
        half_bar: float,
        measure_len: float
    ) -> List[Dict[str, Any]]:
        """
        Decompose an empty silence span into nested rests complying with Gould's Behind Bars:
        - Never cross the half-bar (e.g. beat 2.0 in 4/4) with a single rest.
        - Respect whole beats (soupirs) and subdivide into 8th (demi-soupir) and 16th (quart de soupir).
        """
        rests = []
        end_beat = start_beat + duration

        # 1. Respect the Half-Bar line (Beat 2 in 4/4)
        if start_beat < half_bar and end_beat > half_bar:
            # Split into pre-half-bar and post-half-bar segments
            dur1 = half_bar - start_beat
            dur2 = end_beat - half_bar
            rests.extend(self._decompose_rests_gould(start_beat, dur1, half_bar, measure_len))
            rests.extend(self._decompose_rests_gould(half_bar, dur2, half_bar, measure_len))
            return rests

        # 2. Entire half-bar silence on beats 0-2 or 2-4 -> Half rest (demi-pause)
        if (abs(start_beat - 0.0) < 0.05 and abs(duration - half_bar) < 0.05) or \
           (abs(start_beat - half_bar) < 0.05 and abs(duration - half_bar) < 0.05):
            return [{
                "beat_in_measure": round(start_beat, 4),
                "duration_beats": half_bar,
                "type": "rest",
                "duration_type": "half",
                "gould_name": "demi-pause",
                "pitches": [],
                "velocities": []
            }]

        # 3. Subdivide along integer beat boundaries
        curr = start_beat
        rem_dur = duration

        while rem_dur > 0.04:
            # Distance to next integer beat
            next_int_beat = math.floor(curr) + 1.0
            dist_to_next_beat = next_int_beat - curr

            if dist_to_next_beat <= 0.01:
                dist_to_next_beat = 1.0

            step = min(rem_dur, dist_to_next_beat)

            # Snap step to standard rest sizes (1.0, 0.5, 0.25, 0.125)
            step_snapped = self._snap_rest_duration(step)

            rests.append({
                "beat_in_measure": round(curr, 4),
                "duration_beats": round(step_snapped, 4),
                "type": "rest",
                "duration_type": self._get_duration_name(step_snapped, False),
                "gould_name": self._get_rest_gould_name(step_snapped),
                "pitches": [],
                "velocities": []
            })

            curr = round(curr + step_snapped, 4)
            rem_dur = round(rem_dur - step_snapped, 4)

        return rests

    def _snap_rest_duration(self, dur: float) -> float:
        """Snap rest duration to standard rests: 1.0 (soupir), 0.5 (demi), 0.25 (quart), 0.125 (huitieme)."""
        if dur >= 0.90:
            return 1.0
        elif dur >= 0.40:
            return 0.5
        elif dur >= 0.20:
            return 0.25
        else:
            return 0.125

    def _get_duration_name(self, duration_beats: float, is_triplet: bool) -> str:
        """Return standardized note duration name."""
        if is_triplet:
            if abs(duration_beats - 2.0 / 3.0) < 0.05:
                return "quarter-triplet"
            elif abs(duration_beats - 1.0 / 3.0) < 0.05:
                return "eighth-triplet"
            else:
                return "16th-triplet"

        for dur, (name, _) in DURATION_NAMES.items():
            if abs(duration_beats - dur) < 0.04:
                return name

        if duration_beats >= 3.5:
            return "whole"
        elif duration_beats >= 1.75:
            return "half"
        elif duration_beats >= 0.85:
            return "quarter"
        elif duration_beats >= 0.40:
            return "eighth"
        elif duration_beats >= 0.20:
            return "16th"
        else:
            return "32nd"

    def _get_rest_gould_name(self, duration_beats: float) -> str:
        """French classical engraving names for rests according to Gould."""
        if duration_beats >= 3.5:
            return "pause"
        elif duration_beats >= 1.75:
            return "demi-pause"
        elif duration_beats >= 0.85:
            return "soupir"
        elif duration_beats >= 0.40:
            return "demi-soupir"
        elif duration_beats >= 0.20:
            return "quart-de-soupir"
        else:
            return "huitieme-de-soupir"

    def _empty_score(self, bpm: float) -> Dict[str, Any]:
        """Return an empty 1-measure score with a whole rest."""
        return {
            "tempo_bpm": bpm,
            "time_signature": list(self.time_signature),
            "total_measures": 1,
            "measures": [{
                "measure_number": 1,
                "items": [{
                    "beat_in_measure": 0.0,
                    "duration_beats": self.beats_per_measure,
                    "type": "rest",
                    "duration_type": "whole",
                    "gould_name": "pause",
                    "pitches": [],
                    "velocities": []
                }]
            }]
        }
