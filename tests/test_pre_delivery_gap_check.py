"""Flag brief subtitle flashes without treating normal pauses as errors."""

from pathlib import Path

import pytest

from test_normalize_length_options import run_lua


SOURCE = (Path(__file__).resolve().parents[1] / "SubFix.lua").read_text(encoding="utf-8")
COLLECTOR = SOURCE[SOURCE.index("function collect_pre_delivery_final_check_issues("):SOURCE.index("function collect_rows_overlapping_asr_segment(")]


@pytest.mark.parametrize("fps", [23.976, 24, 25, 29.97, 30, 50, 59.94, 60])
def test_pre_delivery_gap_warning_only_marks_short_gaps(run_lua, fps):
    run_lua(f"test_fps = {fps}")
    run_lua('''
        collect_visible_timeline_cut_frames = function() return {} end
        collect_timeline_transition_alignment_cuts = function() return {} end
        count_effective_subtitle_chars = function(text) return #text end
        trim_text = function(text) return text:match("^%s*(.-)%s*$") end
        subtitle_has_trailing_space = function() return false end
    ''' + COLLECTOR + '''
        local max_gap = math.floor(test_fps * 0.5)
        for _, gap in ipairs({-1, 0, 1, 2, 3, 4, max_gap, max_gap + 1,
            math.floor(test_fps * 0.8), math.floor(test_fps * 1.3),
            math.floor(test_fps * 2), math.floor(test_fps * 10)}) do
            local rows = {{text = "a", start_frame = 100, end_frame = 200},
                {text = "b", start_frame = 200 + gap, end_frame = 300 + gap}}
            local issues = collect_pre_delivery_final_check_issues(rows, nil, test_fps)
            local gap_count = 0
            for _, issue in ipairs(issues) do
                assert(issue.kind ~= "字幕间隔过长", "obsolete warning label")
                if issue.kind == "字幕间异常短缝" then
                    gap_count = gap_count + 1
                    assert(issue.marker_frame == math.floor(200 + gap / 2 + 0.5))
                end
            end
            local expected = gap > 0 and gap <= max_gap and 1 or 0
            assert(gap_count == expected, "incorrect gap warning at " .. gap .. " frames")
            assert(rows[1].end_frame == 200 and rows[2].start_frame == 200 + gap,
                "checking must not modify subtitle timing")
        end
        local issues = collect_pre_delivery_final_check_issues({
            {text = "", start_frame = 100, end_frame = 200},
            {text = "b", start_frame = 2000, end_frame = 2100}}, nil, test_fps)
        local found_empty = false
        for _, issue in ipairs(issues) do
            assert(issue.kind ~= "字幕间隔过长")
            assert(issue.kind ~= "字幕间异常短缝")
            if issue.kind == "空字幕" then found_empty = true end
        end
        assert(found_empty, "other checks must still run across long pauses")
    ''')
