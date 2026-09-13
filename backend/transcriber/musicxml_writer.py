"""
MusicXML and QML JSON Score Serializer for AudioScoreTranscriber.
Converts quantized musical scores into:
1. High-fidelity QML WebSocket JSON payloads for direct front-end display.
2. Compliant MusicXML 3.1 partwise XML documents (with divisions=24 for exact triplet resolution).
"""

import os
import xml.etree.ElementTree as ET
from xml.dom import minidom
import logging
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

# MIDI Pitch to Note Name mappings
STEP_NAMES = ["C", "C", "D", "D", "E", "F", "F", "G", "G", "A", "A", "B"]
ALTERATIONS = [0, 1, 0, 1, 0, 0, 1, 0, 1, 0, 1, 0]


def midi_to_step_alter_octave(midi_pitch: int) -> Tuple[str, int, int, str]:
    """
    Convert MIDI pitch (e.g. 60) to (step='C', alter=0, octave=4, note_name='C4').
    """
    midi_pitch = int(np_clip_pitch(midi_pitch))
    semitone = midi_pitch % 12
    step = STEP_NAMES[semitone]
    alter = ALTERATIONS[semitone]
    octave = (midi_pitch // 12) - 1
    accidental = "#" if alter == 1 else ""
    note_name = f"{step}{accidental}{octave}"
    return step, alter, octave, note_name


def np_clip_pitch(p: int) -> int:
    return max(0, min(127, int(p)))


def duration_beats_to_fraction(duration_beats: float, is_triplet: bool = False) -> Tuple[int, int]:
    """
    Convert musical duration in quarter beats to fraction (num, denom) relative to whole note.
    Compatible with MuseScore cursor.setDuration(num, denom).
    """
    import math

    if is_triplet:
        if abs(duration_beats - 2.0 / 3.0) < 0.05:
            return 1, 6
        elif abs(duration_beats - 1.0 / 3.0) < 0.05:
            return 1, 12
        elif abs(duration_beats - 1.0 / 6.0) < 0.05:
            return 1, 24
        elif abs(duration_beats - 4.0 / 3.0) < 0.05:
            return 1, 3
        elif abs(duration_beats - 2.0) < 0.05:
            return 1, 2

    if abs(duration_beats - 4.0) < 0.05:
        return 1, 1
    elif abs(duration_beats - 3.0) < 0.05:
        return 3, 4
    elif abs(duration_beats - 2.0) < 0.05:
        return 1, 2
    elif abs(duration_beats - 1.5) < 0.05:
        return 3, 8
    elif abs(duration_beats - 1.0) < 0.05:
        return 1, 4
    elif abs(duration_beats - 0.75) < 0.05:
        return 3, 16
    elif abs(duration_beats - 0.5) < 0.05:
        return 1, 8
    elif abs(duration_beats - 0.375) < 0.05:
        return 3, 32
    elif abs(duration_beats - 0.25) < 0.05:
        return 1, 16
    elif abs(duration_beats - 0.125) < 0.05:
        return 1, 32
    else:
        whole_fraction = duration_beats / 4.0
        denom = 64
        num = max(1, int(round(whole_fraction * denom)))
        g = math.gcd(num, denom)
        return num // g, denom // g


class MusicXMLScoreWriter:
    """
    Score serialization engine supporting QML JSON format and MusicXML 3.1.
    """

    DIVISIONS_PER_QUARTER = 24  # Divisible by 2, 3, 4, 6, 8 (handles 1/32 and triplets perfectly)

    def __init__(self):
        pass

    def to_qml_json(
        self,
        quantized_score: Dict[str, Any],
        mode: str = "rhythm"
    ) -> Dict[str, Any]:
        """
        Format quantized score data into a clean JSON structure for QML / WebSocket client.
        Always includes the complete flat events list under the 'events' key.
        """
        bpm = quantized_score.get("tempo_bpm", 120.0)
        time_sig = quantized_score.get("time_signature", [4, 4])
        raw_measures = quantized_score.get("measures", [])

        # Choose appropriate clef based on mode and pitch range
        clef = self._determine_clef(raw_measures, mode)

        flat_events_list: List[Dict[str, Any]] = []
        formatted_measures = []

        for meas in raw_measures:
            m_num = meas["measure_number"]
            m_items = []

            for item in meas.get("items", []):
                item_type = item["type"]
                beat = item.get("beat_in_measure", 0.0)
                duration_beats = item.get("duration_beats", 1.0)
                dur_type = item.get("duration_type", "quarter")
                is_triplet = item.get("is_triplet", False)
                tie_start = item.get("tie_start", False)
                tie_stop = item.get("tie_stop", False)

                num, denom = duration_beats_to_fraction(duration_beats, is_triplet)

                if item_type == "rest":
                    item_dict = {
                        "type": "rest",
                        "isRest": True,
                        "pitch": None,
                        "pitches": [],
                        "beat": beat,
                        "beat_in_measure": beat,
                        "measure_number": m_num,
                        "duration_beats": duration_beats,
                        "duration_type": dur_type,
                        "num": num,
                        "denom": denom,
                        "duration": {"num": num, "denom": denom},
                        "gould_name": item.get("gould_name", "soupir"),
                        "is_triplet": is_triplet
                    }
                    m_items.append(item_dict)
                    flat_events_list.append(dict(item_dict))
                else:
                    # Note or chord
                    pitches = item.get("pitches", [60])
                    velocities = item.get("velocities", [80] * len(pitches))

                    notes_info = []
                    for p, v in zip(pitches, velocities):
                        step, alter, oct_num, name = midi_to_step_alter_octave(p)
                        notes_info.append({
                            "midi": p,
                            "pitch": p,
                            "name": name,
                            "step": step,
                            "alter": alter,
                            "octave": oct_num,
                            "velocity": v
                        })

                    item_dict = {
                        "type": item_type,
                        "isRest": False,
                        "pitch": pitches[0] if pitches else 60,
                        "pitches": pitches,
                        "velocities": velocities,
                        "velocity": velocities[0] if velocities else 80,
                        "beat": beat,
                        "beat_in_measure": beat,
                        "measure_number": m_num,
                        "duration_beats": duration_beats,
                        "duration_type": dur_type,
                        "num": num,
                        "denom": denom,
                        "duration": {"num": num, "denom": denom},
                        "is_triplet": is_triplet,
                        "tie_start": tie_start,
                        "tie_stop": tie_stop,
                        "notes": notes_info
                    }
                    m_items.append(item_dict)
                    flat_events_list.append(dict(item_dict))

            formatted_measures.append({
                "measure_number": m_num,
                "items": m_items
            })

        return {
            "status": "success",
            "mode": mode,
            "tempo_bpm": bpm,
            "time_signature": time_sig,
            "clef": clef,
            "total_measures": len(formatted_measures),
            "measures": formatted_measures,
            "events": flat_events_list
        }

    def to_musicxml_string(
        self,
        quantized_score: Dict[str, Any],
        title: str = "AudioScore Transcription",
        mode: str = "piano"
    ) -> str:
        """
        Generate a fully compliant MusicXML 3.1 XML string.
        """
        bpm = quantized_score.get("tempo_bpm", 120.0)
        time_sig = quantized_score.get("time_signature", [4, 4])
        measures = quantized_score.get("measures", [])
        clef_info = self._determine_clef(measures, mode)

        # Root element
        root = ET.Element("score-partwise", version="3.1")

        # Movement title
        work = ET.SubElement(root, "work")
        work_title = ET.SubElement(work, "work-title")
        work_title.text = title

        # Part list
        part_list = ET.SubElement(root, "part-list")
        score_part = ET.SubElement(part_list, "score-part", id="P1")
        part_name = ET.SubElement(score_part, "part-name")
        part_name.text = f"AudioScore ({mode.capitalize()})"

        # Part container
        part = ET.SubElement(root, "part", id="P1")

        for m_idx, meas in enumerate(measures):
            m_elem = ET.SubElement(part, "measure", number=str(meas.get("measure_number", m_idx + 1)))

            # Measure 1 attributes: divisions, key, time signature, clef, tempo
            if m_idx == 0:
                attrs = ET.SubElement(m_elem, "attributes")
                divs = ET.SubElement(attrs, "divisions")
                divs.text = str(self.DIVISIONS_PER_QUARTER)

                key = ET.SubElement(attrs, "key")
                fifths = ET.SubElement(key, "fifths")
                fifths.text = "0"  # C Major / neutral

                time = ET.SubElement(attrs, "time")
                beats = ET.SubElement(time, "beats")
                beats.text = str(time_sig[0])
                beat_type = ET.SubElement(time, "beat-type")
                beat_type.text = str(time_sig[1])

                clef = ET.SubElement(attrs, "clef")
                sign = ET.SubElement(clef, "sign")
                line = ET.SubElement(clef, "line")
                if clef_info == "F":
                    sign.text = "F"
                    line.text = "4"
                elif clef_info == "percussion":
                    sign.text = "percussion"
                    line.text = "2"
                else:
                    sign.text = "G"
                    line.text = "2"

                # Metronome marking
                direction = ET.SubElement(m_elem, "direction", placement="above")
                dir_type = ET.SubElement(direction, "direction-type")
                metronome = ET.SubElement(dir_type, "metronome")
                b_unit = ET.SubElement(metronome, "beat-unit")
                b_unit.text = "quarter"
                per_min = ET.SubElement(metronome, "per-minute")
                per_min.text = str(int(round(bpm)))

                sound = ET.SubElement(direction, "sound", tempo=str(int(round(bpm))))

            # Items in measure
            for item in meas.get("items", []):
                item_type = item["type"]
                dur_beats = item.get("duration_beats", 1.0)
                dur_xml = max(1, int(round(dur_beats * self.DIVISIONS_PER_QUARTER)))
                xml_type = self._map_xml_type(dur_beats)
                is_triplet = item.get("is_triplet", False)
                tie_start = item.get("tie_start", False)
                tie_stop = item.get("tie_stop", False)

                if item_type == "rest":
                    note_elem = ET.SubElement(m_elem, "note")
                    ET.SubElement(note_elem, "rest")
                    dur_elem = ET.SubElement(note_elem, "duration")
                    dur_elem.text = str(dur_xml)
                    type_elem = ET.SubElement(note_elem, "type")
                    type_elem.text = xml_type
                    if is_triplet:
                        self._add_triplet_tags(note_elem)
                else:
                    pitches = item.get("pitches", [60])
                    for p_idx, pitch in enumerate(pitches):
                        note_elem = ET.SubElement(m_elem, "note")
                        if p_idx > 0:
                            # Additional notes in a chord have <chord/>
                            ET.SubElement(note_elem, "chord")

                        # Pitch tag
                        step, alter, octave, _ = midi_to_step_alter_octave(pitch)
                        pitch_elem = ET.SubElement(note_elem, "pitch")
                        s_elem = ET.SubElement(pitch_elem, "step")
                        s_elem.text = step
                        if alter != 0:
                            alt_elem = ET.SubElement(pitch_elem, "alter")
                            alt_elem.text = str(alter)
                        oct_elem = ET.SubElement(pitch_elem, "octave")
                        oct_elem.text = str(octave)

                        dur_elem = ET.SubElement(note_elem, "duration")
                        dur_elem.text = str(dur_xml)

                        if tie_stop:
                            ET.SubElement(note_elem, "tie", type="stop")
                        if tie_start:
                            ET.SubElement(note_elem, "tie", type="start")

                        type_elem = ET.SubElement(note_elem, "type")
                        type_elem.text = xml_type

                        # Accidental display if sharp
                        if alter == 1:
                            acc_elem = ET.SubElement(note_elem, "accidental")
                            acc_elem.text = "sharp"

                        if is_triplet:
                            self._add_triplet_tags(note_elem)

                        # Notations for ties
                        if tie_start or tie_stop:
                            notations = ET.SubElement(note_elem, "notations")
                            if tie_stop:
                                ET.SubElement(notations, "tied", type="stop")
                            if tie_start:
                                ET.SubElement(notations, "tied", type="start")

        # Pretty print XML string
        rough_string = ET.tostring(root, encoding="utf-8")
        parsed = minidom.parseString(rough_string)
        return parsed.toprettyxml(indent="  ")

    def export_musicxml_file(
        self,
        quantized_score: Dict[str, Any],
        output_filepath: str,
        title: str = "AudioScore Transcription",
        mode: str = "piano"
    ) -> str:
        """Write MusicXML document to disk."""
        xml_str = self.to_musicxml_string(quantized_score, title=title, mode=mode)
        os.makedirs(os.path.dirname(os.path.abspath(output_filepath)), exist_ok=True)
        with open(output_filepath, "w", encoding="utf-8") as f:
            f.write(xml_str)
        logger.info("Saved MusicXML to %s", output_filepath)
        return output_filepath

    def _determine_clef(self, measures: List[Dict[str, Any]], mode: str) -> str:
        """Select appropriate clef (G treble, F bass, or percussion)."""
        if mode == "rhythm":
            return "percussion"

        pitches = []
        for m in measures:
            for item in m.get("items", []):
                pitches.extend(item.get("pitches", []))

        if not pitches:
            return "G"

        median_pitch = sum(pitches) / len(pitches)
        # Below Middle C (60), choose bass clef F
        if median_pitch < 55:
            return "F"
        return "G"

    def _map_xml_type(self, duration_beats: float) -> str:
        """Map duration in beats to MusicXML note type string."""
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
        elif duration_beats >= 0.10:
            return "32nd"
        else:
            return "64th"

    def _add_triplet_tags(self, note_elem: ET.Element):
        """Add time-modification tags for tuplet/triplet representation."""
        t_mod = ET.SubElement(note_elem, "time-modification")
        act = ET.SubElement(t_mod, "actual-notes")
        act.text = "3"
        norm = ET.SubElement(t_mod, "normal-notes")
        norm.text = "2"
