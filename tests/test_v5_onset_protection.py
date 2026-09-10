"""Amplitude-only refinement must not remove aligned quiet leading speech."""

from array import array
import importlib.util
import math
from pathlib import Path
import wave

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("v5_onset_tests", ROOT / "subfix_generate_v5.py")
v5 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v5)


def write_audio(path, sections):
    sample_rate = 16000
    samples = array("h")
    for duration, amplitude in sections:
        samples.extend(int(amplitude * math.sin(i * 0.15)) for i in range(round(duration * sample_rate)))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(samples.tobytes())


@pytest.mark.parametrize("fps", [24.0, 25.0, 29.97003, 30.0, 50.0, 60.0])
def test_quiet_leading_speech_keeps_aligned_start(tmp_path, fps):
    audio = tmp_path / "quiet-onset.wav"
    write_audio(audio, [(0.5, 0), (0.45, 300), (1.05, 5000), (0.5, 0)])
    offset = 90000
    row = {"text": "测试字幕", "start_frame": offset + round(0.5 * fps),
           "end_frame": offset + round(2.0 * fps), "speaker_track_index": 1}
    output, _ = v5.refine_subtitle_boundaries([row], audio, offset, fps)
    output, _ = v5.preserve_refined_row_order(output)
    assert output[0]["start_frame"] == row["start_frame"], "quiet words were moved to the louder syllable"
    assert output[0]["text"] == row["text"]
    assert output[0]["end_frame"] > output[0]["start_frame"]
    assert "original_start_frame" not in row, "caller data must not be changed"


def test_silence_between_rows_is_preserved_with_quiet_second_onset(tmp_path):
    audio = tmp_path / "pause.wav"
    write_audio(audio, [(0.5, 0), (0.5, 5000), (0.5, 0), (0.4, 300), (1.1, 5000), (0.5, 0)])
    rows = [{"text": "前句", "start_frame": 15, "end_frame": 30, "speaker_track_index": 1},
            {"text": "后句", "start_frame": 45, "end_frame": 90, "speaker_track_index": 1}]
    output, _ = v5.refine_subtitle_boundaries(rows, audio, 0, 30.0)
    output, _ = v5.preserve_refined_row_order(output)
    assert output[1]["start_frame"] == 45
    assert output[0]["end_frame"] < output[1]["start_frame"]


def test_existing_early_speech_correction_stays_within_search_limit(tmp_path):
    audio = tmp_path / "early.wav"
    write_audio(audio, [(0.4, 0), (0.5, 5000), (0.5, 0)])
    row = {"text": "较晚的原始起点", "start_frame": 15, "end_frame": 25, "speaker_track_index": 1}
    output, _ = v5.refine_subtitle_boundaries([row], audio, 0, 30.0)
    assert 11 <= output[0]["start_frame"] <= 13
    assert abs(output[0]["start_frame"] - row["start_frame"]) <= 12


def test_neighbor_adjustments_never_delay_aligned_starts(tmp_path):
    audio = tmp_path / "neighbors.wav"
    write_audio(audio, [(0.5, 0), (0.4, 300), (0.6, 5000), (0.1, 0),
                        (0.3, 300), (0.7, 5000), (0.5, 0)])
    rows = [{"text": "第一句", "start_frame": 15, "end_frame": 48, "speaker_track_index": 1},
            {"text": "第二句", "start_frame": 48, "end_frame": 78, "speaker_track_index": 1}]
    output, _ = v5.refine_subtitle_boundaries(rows, audio, 0, 30.0)
    output, _ = v5.preserve_refined_row_order(output)
    assert all(row["start_frame"] <= row["original_start_frame"] for row in output)
    assert output[0]["end_frame"] <= output[1]["start_frame"]
