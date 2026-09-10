"""Preserve aligned words and pauses through the complete timing postprocess."""

from pathlib import Path
import textwrap
from types import SimpleNamespace

import pytest

from test_v5_onset_protection import ROOT, v5, write_audio


def refine(rows, audio, fps=30.0):
    output, diagnostic = v5.refine_subtitle_boundaries(rows, audio, 0, fps)
    output, _ = v5.preserve_refined_row_order(output)
    return output, diagnostic


def assert_word_coverage(output, original):
    assert len(output) == len(original)
    for row, before in zip(output, original):
        assert row["text"] == before["text"]
        assert row["start_frame"] <= before["start_frame"]
        assert row["end_frame"] >= before["end_frame"], "aligned trailing words were cropped"
    assert all(a["end_frame"] <= b["start_frame"] for a, b in zip(output, output[1:]))


@pytest.mark.parametrize("fps", [24.0, 25.0, 29.97003, 30.0, 50.0, 60.0])
def test_quiet_trailing_speech_is_not_cut_to_loud_region(tmp_path, fps):
    audio = tmp_path / "quiet-tail.wav"
    write_audio(audio, [(0.5, 0), (0.6, 5000), (0.9, 300), (0.5, 0)])
    rows = [{"text": "完整的句尾", "start_frame": round(0.5 * fps), "end_frame": round(2 * fps)}]
    output, _ = refine(rows, audio, fps)
    assert_word_coverage(output, rows)


def test_adjacent_rows_do_not_gain_gaps_or_lose_words(tmp_path):
    audio = tmp_path / "continuous.wav"
    write_audio(audio, [(0.5, 0), (0.2, 5000), (0.8, 300), (0.2, 5000), (0.8, 300), (0.5, 0)])
    rows = [{"text": "前半句", "start_frame": 15, "end_frame": 45},
            {"text": "后半句", "start_frame": 45, "end_frame": 75}]
    output, _ = refine(rows, audio)
    assert_word_coverage(output, rows)
    assert output[0]["end_frame"] == output[1]["start_frame"] == 45


def test_short_gap_boundary_stays_between_aligned_words(tmp_path):
    audio = tmp_path / "short-gap.wav"
    write_audio(audio, [(0.5, 0), (0.2, 5000), (0.8, 300), (0.1, 0), (0.3, 300), (0.6, 5000), (0.5, 0)])
    rows = [{"text": "前句尾音", "start_frame": 15, "end_frame": 45},
            {"text": "后句轻音", "start_frame": 48, "end_frame": 75}]
    output, _ = refine(rows, audio)
    assert_word_coverage(output, rows)
    assert 45 <= output[0]["end_frame"] == output[1]["start_frame"] <= 48


def test_long_pause_is_preserved_without_claiming_silence_detection(tmp_path):
    audio = tmp_path / "pause.wav"
    write_audio(audio, [(0.5, 0), (0.5, 5000), (0.5, 0), (0.5, 5000), (0.5, 0)])
    rows = [{"text": "第一句", "start_frame": 15, "end_frame": 30},
            {"text": "第二句", "start_frame": 45, "end_frame": 60}]
    output, diagnostic = refine(rows, audio)
    assert_word_coverage(output, rows)
    assert (output[0]["end_frame"], output[1]["start_frame"]) == (30, 45)
    assert diagnostic["confirmed_silence_preserved_count"] == 0
    assert diagnostic["original_gap_preserved_count"] == 1


def test_unrelated_sound_does_not_expand_across_long_original_pause(tmp_path):
    audio = tmp_path / "background.wav"
    write_audio(audio, [(0.5, 0), (0.5, 5000), (0.5, 5000), (0.5, 5000), (0.5, 0)])
    rows = [{"text": "暂停前", "start_frame": 15, "end_frame": 30},
            {"text": "暂停后", "start_frame": 45, "end_frame": 60}]
    output, _ = refine(rows, audio)
    assert (output[0]["end_frame"], output[1]["start_frame"]) == (30, 45)


def test_global_track_reconciliation_preserves_word_spans_and_pause():
    rows = [{"text": "甲", "start_frame": 12, "end_frame": 28, "original_start_frame": 10,
             "original_end_frame": 30, "speaker_track_index": 1},
            {"text": "乙", "start_frame": 36, "end_frame": 66, "original_start_frame": 40,
             "original_end_frame": 60, "speaker_track_index": 2}]
    output, _ = v5.preserve_refined_row_order(rows)
    assert output[0]["start_frame"] <= 10 and output[0]["end_frame"] == 30
    assert output[1]["start_frame"] == 40 and output[1]["end_frame"] >= 60


def test_overlapping_original_spans_fail_without_silently_cropping_words(tmp_path):
    audio = tmp_path / "overlap.wav"
    write_audio(audio, [(2.0, 5000)])
    rows = [{"text": "第一句", "start_frame": 10, "end_frame": 35},
            {"text": "第二句", "start_frame": 30, "end_frame": 50}]
    with pytest.raises(ValueError, match="原始对齐范围重叠"):
        v5.refine_subtitle_boundaries(rows, audio, 0, 30.0)
    for row in rows:
        row.update(original_start_frame=row["start_frame"], original_end_frame=row["end_frame"])
    with pytest.raises(ValueError, match="原始对齐范围重叠"):
        v5.preserve_refined_row_order(rows)


@pytest.mark.parametrize("use_v5", [True, False])
def test_pipeline_extends_display_only_after_refinement_and_keeps_long_pauses(tmp_path, use_v5):
    audio = tmp_path / "pipeline.wav"
    write_audio(audio, [(0.5, 0), (0.5, 5000), (0.5, 0), (0.5, 5000), (0.5, 0)])
    events = []

    def refine_record(*args):
        events.append("refine")
        return v5.refine_subtitle_boundaries(*args)

    def extend_record(*args):
        events.append("extend")
        return v5.v4.extend_subtitle_row_tails(*args)

    source = (ROOT / "subfix_asr_transcribe.py").read_text()
    start = source.index("        subtitle_rows, hard_char_split_count = generate_v4.enforce_hard_char_limit(")
    end = source.index('        diagnostic["hotword_replacement_count"] +=', start)
    rows = [{"text": "前句", "start_frame": 15, "end_frame": 30, "speaker_track_index": 1},
            {"text": "后句", "start_frame": 45, "end_frame": 60, "speaker_track_index": 1}]
    namespace = dict(
        subtitle_rows=rows, canonical_units=[], Path=Path, args=SimpleNamespace(fps=30.0, max_chars=20),
        diagnostic={}, v5_mode=use_v5, v5_writeback="live",
        prepared_tracks=[{"track_index": 1, "audio_path": audio, "timeline_start_frame": 0, "fps": 30.0}],
        generate_v4=SimpleNamespace(
            enforce_hard_char_limit=lambda rows, *_: (rows, 0),
            restore_display_spacing=lambda rows, *_: rows,
            extend_subtitle_row_tails=extend_record,
            SUBTITLE_ROW_TAIL_EXTENSION_GAP_SECONDS=v5.v4.SUBTITLE_ROW_TAIL_EXTENSION_GAP_SECONDS),
        generate_textnorm=SimpleNamespace(normalize_subtitle_rows=lambda rows: (rows, {"textnorm_changed_row_count": 0})),
        generate_v5=SimpleNamespace(refine_subtitle_boundaries=refine_record,
                                   preserve_refined_row_order=v5.preserve_refined_row_order),
    )
    exec(compile(textwrap.dedent(source[start:end]), "timing_pipeline", "exec"), namespace)
    assert events == (["refine", "extend"] if use_v5 else ["extend"])
    result = namespace["subtitle_rows"]
    if use_v5:
        assert (result[0]["end_frame"], result[1]["start_frame"]) == (30, 45)
    else:
        assert result[0]["end_frame"] == 38, "legacy display extension should remain unchanged"
