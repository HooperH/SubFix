"""Exercise Resolve subtitle fade preservation with the actual Lua helpers."""

from pathlib import Path

import pytest

from test_generate_progress_lifecycle import run_lua


SOURCE = (Path(__file__).resolve().parents[1] / "SubFix.lua").read_text(encoding="utf-8")
START = SOURCE.find("SUBFIX_SUBTITLE_FADES = {}")
HELPERS = SOURCE[START:SOURCE.index("local function refresh_subtitles(")] if START >= 0 else ""
SETUP = '''
    function item(id, start_frame, end_frame, text, fade_in, fade_out)
        return {
            GetUniqueId = function() return id end,
            GetStart = function() return start_frame end,
            GetEnd = function() return end_frame end,
            GetName = function() return text end,
            GetFades = function() return {FadeIn = fade_in, FadeOut = fade_out} end,
            SetFades = function(self, fades)
                self.writes = (self.writes or 0) + 1
                fade_in, fade_out = fades.FadeIn, fades.FadeOut
                return true
            end
        }
    end
'''


def test_fades_follow_identity_after_text_and_timing_edits(run_lua):
    run_lua(SETUP + HELPERS + '''
        local old = item("old", 100, 200, "before", 16, 8)
        local rows = {{source_subtitle_id = "old", start_frame = 300, end_frame = 400, text = "after"}}
        local plan, err = SUBFIX_SUBTITLE_FADES.prepare({old}, rows, true)
        assert(plan, err)
        assert(#plan == 1 and plan[1].FadeIn == 16 and plan[1].FadeOut == 8)
        local new = item("new", 300, 400, "after", 0, 0)
        local ok, count = SUBFIX_SUBTITLE_FADES.restore(plan, rows, {new})
        assert(ok and count == 1)
        assert(new:GetFades().FadeIn == 16 and new:GetFades().FadeOut == 8)
        assert(rows[1].source_subtitle_id == "new")
        new:SetFades({FadeIn = 7, FadeOut = 2})
        rows[1].start_frame = 500; rows[1].end_frame = 600
        local second = assert(SUBFIX_SUBTITLE_FADES.prepare({new}, rows, true))
        assert(second[1].FadeIn == 7 and second[1].FadeOut == 2, "must read latest live fades")
    ''')


def test_existing_rows_without_ids_match_by_exact_timing_not_list_order(run_lua):
    run_lua(SETUP + HELPERS + '''
        local rows = {{start_frame = 100, end_frame = 200, text = "edited"},
            {start_frame = 300, end_frame = 400, text = "second"}}
        local old = {item("b", 300, 400, "second", 0, 9), item("a", 100, 200, "old", 4, 0)}
        local plan = assert(SUBFIX_SUBTITLE_FADES.prepare(old, rows, true))
        local a, b = item("new-a", 100, 200, "edited", 0, 0), item("new-b", 300, 400, "second", 0, 0)
        assert(SUBFIX_SUBTITLE_FADES.restore(plan, rows, {b, a}))
        assert(a:GetFades().FadeIn == 4 and a:GetFades().FadeOut == 0)
        assert(b:GetFades().FadeIn == 0 and b:GetFades().FadeOut == 9)
    ''')


def test_ambiguous_or_missing_faded_source_stops_before_rebuild(run_lua):
    run_lua(SETUP + HELPERS + '''
        local old = item("old", 100, 200, "old", 5, 6)
        for _, rows in ipairs({
            {{start_frame = 500, end_frame = 600, text = "unrelated"}},
            {{start_frame = 100, end_frame = 200, text = "a"}, {start_frame = 100, end_frame = 200, text = "b"}}
        }) do
            local plan, err = SUBFIX_SUBTITLE_FADES.prepare({old}, rows, true)
            assert(plan == nil and err and #err > 0)
            assert(old.writes == nil)
        end
    ''')


def test_older_resolve_without_fade_api_remains_supported(run_lua):
    run_lua(SETUP + HELPERS + '''
        local old = item("old", 100, 200, "old", 0, 0)
        old.GetFades = nil
        local rows = {{start_frame = 100, end_frame = 200, text = "old"}}
        local plan = assert(SUBFIX_SUBTITLE_FADES.prepare({old}, rows, false))
        assert(#plan == 0)
        local blocked, err = SUBFIX_SUBTITLE_FADES.prepare({old}, rows, true)
        assert(blocked == nil and err)
    ''')


@pytest.mark.parametrize("failure", ["false", "throw", "readback"])
def test_restore_failure_is_reported(run_lua, failure):
    run_lua(SETUP + HELPERS + f'failure = "{failure}"\n' + '''
        local rows = {{start_frame = 100, end_frame = 200, text = "text"}}
        local plan = assert(SUBFIX_SUBTITLE_FADES.prepare({item("old", 100, 200, "text", 5, 6)}, rows, true))
        local new = item("new", 100, 200, "text", 0, 0)
        new.SetFades = function()
            if failure == "throw" then error("unsupported") end
            return failure == "readback"
        end
        local ok, count, err = SUBFIX_SUBTITLE_FADES.restore(plan, rows, {new})
        assert(not ok and count == 0 and err)
        assert(rows[1].source_subtitle_id == nil, "failed restore must not advance identity")
    ''')


def test_shortened_clip_scales_fades_to_duration_minus_one_and_tolerates_rounding(run_lua):
    run_lua(SETUP + HELPERS + '''
        local rows = {{start_frame = 100, end_frame = 106, text = "text"}}
        local plan = assert(SUBFIX_SUBTITLE_FADES.prepare({item("old", 100, 106, "text", 16, 8)}, rows, true))
        local new = item("new", 100, 105, "text", 0, 0)
        assert(SUBFIX_SUBTITLE_FADES.restore(plan, rows, {new}))
        assert(new:GetFades().FadeIn == 3 and new:GetFades().FadeOut == 1)
        assert(plan[1].FadeIn == 16 and plan[1].FadeOut == 8, "recovery data must retain original fades")
    ''')


def test_restore_refuses_unrelated_or_ambiguous_imports(run_lua):
    run_lua(SETUP + HELPERS + '''
        local rows = {{start_frame = 100, end_frame = 200, text = "text"}}
        local plan = assert(SUBFIX_SUBTITLE_FADES.prepare({item("old", 100, 200, "text", 5, 6)}, rows, true))
        local a, b = item("a", 100, 200, "text", 0, 0), item("b", 100, 200, "text", 0, 0)
        assert(not SUBFIX_SUBTITLE_FADES.restore(plan, rows, {a, b}))
        assert(a.writes == nil and b.writes == nil)
        local unrelated = item("c", 500, 600, "text", 0, 0)
        assert(not SUBFIX_SUBTITLE_FADES.restore(plan, rows, {unrelated}))
        assert(unrelated.writes == nil)
        assert(not SUBFIX_SUBTITLE_FADES.restore(plan, rows, {a, unrelated}))
        assert(a.writes == nil)
    ''')


def test_writeback_captures_and_saves_fades_before_deleting_then_restores():
    body = SOURCE[SOURCE.index("update_timeline = function()"):SOURCE.index("local function do_ai_fix()")]
    assert body.index("SUBFIX_SUBTITLE_FADES.prepare(") < body.index("clear_subtitle_track_clips(")
    assert body.index("SUBFIX_SUBTITLE_FADES.save(") < body.index("clear_subtitle_track_clips(")
    assert body.index("detect_subtitle_track_delta(") < body.index("SUBFIX_SUBTITLE_FADES.restore(")
    assert 'return finish_timeline_update(false)' in body


def test_refresh_and_selection_composite_keep_original_clip_identity():
    refresh = SOURCE[SOURCE.index("local function refresh_subtitles("):SOURCE.index("function load_generate_selection_core_for_subfix(")]
    composite = SOURCE[SOURCE.index("function build_selection_composite_rows("):SOURCE.index("function write_rows_to_update_srt(")]
    assert "source_subtitle_id = SUBFIX_SUBTITLE_FADES.item_id(item)" in refresh
    assert "source_subtitle_id = SUBFIX_SUBTITLE_FADES.item_id(item)" in composite
    assert "row.source_subtitle_id = full_row.source_subtitle_id" in composite


def test_selection_round_trip_preserves_outside_and_inside_fades(run_lua):
    composite = SOURCE[SOURCE.index("function build_selection_composite_rows("):SOURCE.index("function write_rows_to_update_srt(")]
    run_lua(SETUP + HELPERS + '''
        WORK_SCOPE_MODE_SELECTION = "selection"
        current_fps = 30
        function clone_table(value)
            if type(value) ~= "table" then return value end
            local copy = {}; for k, v in pairs(value) do copy[k] = clone_table(v) end
            return copy
        end
        function sort_rows_by_timing(rows)
            table.sort(rows, function(a, b) return a.start_frame < b.start_frame end)
        end
        function range_intersects_selection(start_frame, end_frame, scope)
            return start_frame < scope.end_frame and end_frame > scope.start_frame
        end
        local old = {item("outside", 100, 200, "outside", 0, 10), item("inside", 300, 400, "inside", 8, 4)}
        function get_subtitle_track_items() return old end
    ''' + composite + '''
        local rows = assert(build_selection_composite_rows({}, 1,
            {mode = "selection", start_frame = 300, end_frame = 400},
            {{source_subtitle_id = "stale-id", start_frame = 310, end_frame = 420, text = "edited"}}, 30))
        local plan = assert(SUBFIX_SUBTITLE_FADES.prepare(old, rows, true))
        local outside, inside = item("new-out", 100, 200, "outside", 0, 0), item("new-in", 310, 420, "edited", 0, 0)
        assert(SUBFIX_SUBTITLE_FADES.restore(plan, rows, {outside, inside}))
        assert(outside:GetFades().FadeOut == 10)
        assert(inside:GetFades().FadeIn == 8 and inside:GetFades().FadeOut == 4)
    ''')


@pytest.mark.parametrize("failure", ["open", "write", "close"])
def test_recovery_backup_io_failure_is_reported(run_lua, failure):
    run_lua(HELPERS + f'failure = "{failure}"\n' + '''
        function json_encode_value() return "[]" end
        io.open = function()
            if failure == "open" then return nil, "denied" end
            return {write = function(self) if failure == "write" then return nil, "full" end; return self end,
                close = function() return failure ~= "close" end}
        end
        local ok, err = SUBFIX_SUBTITLE_FADES.save("unused", {{FadeIn = 4, FadeOut = 0}})
        assert(not ok and err)
    ''')


def test_one_frame_subtitle_has_no_room_for_fades(run_lua):
    run_lua(SETUP + HELPERS + '''
        local rows = {{start_frame = 100, end_frame = 101, text = "text"}}
        local plan = {{row_index = 1, FadeIn = 8, FadeOut = 4}}
        local new = item("new", 100, 101, "text", 0, 0)
        assert(SUBFIX_SUBTITLE_FADES.restore(plan, rows, {new}))
        assert(new:GetFades().FadeIn == 0 and new:GetFades().FadeOut == 0)
    ''')


def test_entire_script_compiles_in_resolve_luajit(run_lua):
    run_lua("assert(loadfile(" + repr(str(Path(__file__).resolve().parents[1] / "SubFix.lua")) + "))")
