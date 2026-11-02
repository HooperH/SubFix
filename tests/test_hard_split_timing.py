"""A character limit must not invent overlapping or out-of-range subtitles."""

from copy import deepcopy
import random

import pytest

from test_v5_onset_protection import v5, write_audio


@pytest.mark.parametrize("starts,end", [
    ([100, 100, 100, 102, 104, 106, 108, 110], 112),
    ([100, 100, 100, 100, 100, 100, 100, 100], 101),
    ([100, 101, 102, 103, 104, 105, 106, 107], 104),
    ([100, 102, 104, 104, 104, 104, 108, 110], 112),
])
@pytest.mark.parametrize("limit", [1, 2, 3])
def test_hard_split_only_uses_distinct_interior_frame_boundaries(tmp_path, starts, end, limit):
    text = "甲乙丙丁戊己庚辛"
    units = [dict(text=char, start_frame=start, end_frame=start + 1, track_index=1)
             for char, start in zip(text, starts)]
    rows = [dict(text=text, start_frame=100, end_frame=end, speaker_track_index=1)]
    original = deepcopy((rows, units))
    output, _ = v5.v4.enforce_hard_char_limit(rows, units, limit, tokenizer=list)

    assert "".join(row["text"] for row in output) == text
    assert output[0]["start_frame"] == 100 and output[-1]["end_frame"] == end
    assert all(100 <= row["start_frame"] < row["end_frame"] <= end for row in output)
    assert all(left["end_frame"] == right["start_frame"] for left, right in zip(output, output[1:]))
    assert (rows, units) == original
    audio = tmp_path / "audio.wav"
    write_audio(audio, [(1.0, 0)])
    refined, _ = v5.refine_subtitle_boundaries(output, audio, 100, 30.0)
    refined, _ = v5.preserve_refined_row_order(refined)
    assert "".join(row["text"] for row in refined) == text


def test_unrepresentable_split_keeps_one_frame_row_intact():
    row = dict(text="甲乙丙丁", start_frame=0, end_frame=1, speaker_track_index=1)
    units = [dict(text=char, start_frame=0, end_frame=1) for char in row["text"]]
    output, split_count = v5.v4.enforce_hard_char_limit([row], units, 1, tokenizer=list)
    assert output == [{**row, "index": 1}]
    assert split_count == 0


def test_normal_hard_split_still_obeys_character_limit():
    text = "甲乙丙丁戊己庚辛"
    units = [dict(text=char, start_frame=100 + i * 3, end_frame=103 + i * 3)
             for i, char in enumerate(text)]
    rows = [dict(text=text, start_frame=100, end_frame=124)]
    output, count = v5.v4.enforce_hard_char_limit(rows, units, 2, tokenizer=list)
    assert [row["text"] for row in output] == ["甲乙", "丙丁", "戊己", "庚辛"]
    assert [(row["start_frame"], row["end_frame"]) for row in output] == [
        (100, 106), (106, 112), (112, 118), (118, 124),
    ]
    assert count == 1


@pytest.mark.parametrize("stage", ["audio", "tracks"])
def test_real_overlap_reports_ranges_without_disclosing_transcript(tmp_path, stage):
    rows = [dict(text="private first", start_frame=10, end_frame=35, speaker_track_index=1),
            dict(text="private second", start_frame=30, end_frame=50, speaker_track_index=2)]
    audio = tmp_path / "audio.wav"
    write_audio(audio, [(2.0, 5000)])
    with pytest.raises(ValueError, match="原始对齐范围重叠") as error:
        if stage == "audio":
            v5.refine_subtitle_boundaries(rows, audio, 0, 30.0)
        else:
            for row in rows:
                row.update(original_start_frame=row["start_frame"], original_end_frame=row["end_frame"])
            v5.preserve_refined_row_order(rows)
    message = str(error.value)
    for expected in ("10-35", "30-50", "轨道=1", "轨道=2", "重叠=5 帧"):
        assert expected in message
    assert "private" not in message


def test_word_boundary_move_does_not_create_zero_duration_row():
    rows = [dict(text="甲乙", start_frame=0, end_frame=5, speaker_track_index=1),
            dict(text="丙丁", start_frame=5, end_frame=10, speaker_track_index=1),
            dict(text="戊己", start_frame=10, end_frame=20, speaker_track_index=1)]
    units = [dict(text=char, start_frame=start, end_frame=start + 1)
             for char, start in zip("甲乙丙丁戊己", [0, 0, 0, 0, 10, 12])]
    output, _ = v5.v4.protect_word_boundaries(rows, units, 30, tokenizer=lambda _: ["甲乙丙", "丁", "戊己"])
    assert "".join(row["text"] for row in output) == "甲乙丙丁戊己"
    assert [(row["start_frame"], row["end_frame"]) for row in output] == [(0, 10), (10, 20)]


def test_same_frame_segmentation_keeps_text_and_positive_duration():
    rows = [dict(text="甲乙", start_frame=10, end_frame=11, speaker_track_index=1),
            dict(text="丙丁", start_frame=10, end_frame=12, speaker_track_index=1),
            dict(text="戊己", start_frame=20, end_frame=30, speaker_track_index=1)]
    output, _ = v5.v4._finalize_segment_rows(rows, 30, "narration")
    assert "".join(row["text"] for row in output) == "甲乙丙丁戊己"
    assert all(row["start_frame"] < row["end_frame"] for row in output)
    assert all(left["end_frame"] <= right["start_frame"] for left, right in zip(output, output[1:]))


def test_deterministic_timing_edge_cases_pass_complete_segmentation_chain(tmp_path):
    rng = random.Random(338)
    audio = tmp_path / "audio.wav"
    write_audio(audio, [(4.0, 0)])
    for case in range(120):
        cursor = 100
        units = []
        for index, char in enumerate("甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥"):
            cursor += rng.choice([0, 1, 2, 3, 4])
            units.append(dict(text=char, start_frame=cursor, end_frame=cursor + rng.randint(1, 4),
                              track_index=1, asr_punctuation_strength=1.0 if index % 7 == 6 else 0.0))
        units.sort(key=lambda unit: (unit["start_frame"], unit["end_frame"]))
        expected = "".join(unit["text"] for unit in units)
        rows, _ = v5.v4.segment_canonical_units(units, "narration", None, 30, max_chars=6)
        rows, _ = v5.v4.reclaim_overlong_unit_tails(rows, units, 30)
        rows, _ = v5.v4.protect_word_boundaries(rows, units, 30, tokenizer=list)
        rows, _ = v5.v4.enforce_hard_char_limit(rows, units, 6, tokenizer=list)
        assert all(row["start_frame"] < row["end_frame"] for row in rows), case
        rows, _ = v5.refine_subtitle_boundaries(rows, audio, 100, 30)
        rows, _ = v5.preserve_refined_row_order(rows)
        assert "".join(row["text"] for row in rows) == expected, case
        assert all(left["end_frame"] <= right["start_frame"] for left, right in zip(rows, rows[1:])), case
