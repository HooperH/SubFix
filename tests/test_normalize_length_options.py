"""Run normalization and dialog logic in LuaJIT without touching Resolve data."""

import ctypes
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "SubFix.lua").read_text(encoding="utf-8")


@pytest.fixture
def run_lua():
    library = Path("/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/libluajit-5.1.2.dylib")
    if not library.exists():
        pytest.skip("Resolve LuaJIT runtime is not installed")
    lua = ctypes.CDLL(str(library))
    lua.luaL_newstate.restype = ctypes.c_void_p
    lua.luaL_openlibs.argtypes = [ctypes.c_void_p]
    lua.luaL_loadstring.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lua.lua_pcall.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    lua.lua_tolstring.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    lua.lua_tolstring.restype = ctypes.c_char_p
    lua.lua_close.argtypes = [ctypes.c_void_p]
    state = lua.luaL_newstate()
    assert state
    lua.luaL_openlibs(state)

    def run(code):
        status = lua.luaL_loadstring(state, code.encode("utf-8"))
        if status == 0:
            status = lua.lua_pcall(state, 0, 0, 0)
        assert status == 0, (lua.lua_tolstring(state, -1, None) or b"Lua failure").decode("utf-8")

    try:
        yield run
    finally:
        lua.lua_close(state)


@pytest.mark.parametrize("align_audio,fill_gaps", [(True, True), (True, False), (False, True), (False, False)])
@pytest.mark.parametrize("alignment_fails", [False, True])
def test_normalize_runs_only_selected_operations(run_lua, align_audio, fill_gaps, alignment_fails):
    body = SOURCE[SOURCE.index("function run_normalize_subtitle_length("):SOURCE.index("function win.On.BtnStep3.Clicked")]
    run_lua(f"align_audio = {str(align_audio).lower()}; fill_gaps = {str(fill_gaps).lower()}; alignment_fails = {str(alignment_fails).lower()}")
    run_lua('''
        print = function() end
        win = {}; current_fps = 24
        current_rows = {{start_frame = 0, end_frame = 10}, {start_frame = 20, end_frame = 30},
            {start_frame = 100, end_frame = 110}}
        local align_calls, commits, starts, snapshots, gap_stages = 0, 0, 0, 0, 0
        resolve_window = function(window) return window end
        update_shared_status = function(_, message) final_message = message end
        start_normalize_progress = function() starts = starts + 1; return {} end
        finish_normalize_progress = function(status) final_status = status end
        update_normalize_progress = function(payload)
            if payload.stage == "填补空隙" then gap_stages = gap_stages + 1 end
        end
        prepare_mutation_snapshot = function() snapshots = snapshots + 1; return {} end
        commit_mutation_snapshot = function() commits = commits + 1 end
        sort_rows_by_timing = function() end
        rebuild_tree_from_rows = function() end
        is_normalize_progress_cancelled = function() return false end
        LogMsg = function() end
        report_helpers = {format_batch_change_report_line = function() return {} end}
        SUBFIX_AUDIO_ALIGN = {
            get_alignment_cut_frames = function() return {} end,
            clamp_normalize_length_bias_frames = function() return 0 end,
            apply_protected_audio_alignment_for_gap_fill = function(rows)
                align_calls = align_calls + 1
                if alignment_fails then return nil, false, "没有可用音频源" end
                rows[1].start_frame = 1; rows[1].end_frame = 11
                return 1, true, nil, {moved_forward_better = 1}
            end
        }
    ''' + SOURCE[SOURCE.index('function SUBFIX_AUDIO_ALIGN.can_fill_aligned_gap('):SOURCE.index('function SUBFIX_AUDIO_ALIGN.build_qwen_global_writeback_plan(')] + body + '''
        run_normalize_subtitle_length(win, {align_audio = align_audio, fill_gaps = fill_gaps})
        assert(align_calls == (align_audio and 1 or 0), "unselected alignment ran")
        local aligned = align_audio and not alignment_fails
        assert(current_rows[1].start_frame == (aligned and 1 or 0))
        assert(current_rows[1].end_frame == (fill_gaps and 20 or (aligned and 11 or 10)), "gap selection ignored")
        assert(current_rows[2].end_frame == 30, "long gap must remain")
        assert(gap_stages == (fill_gaps and 1 or 0), "unselected gap progress shown")
        assert(commits == ((aligned or fill_gaps) and 1 or 0))
        assert(starts == ((align_audio or fill_gaps) and 1 or 0))
        assert(snapshots == starts)
        if not align_audio and fill_gaps then
            assert(not final_message:find("未生效"), "disabled alignment reported as failure")
        elseif align_audio and alignment_fails and not fill_gaps then
            assert(final_status == "failed")
            assert(not final_message:find("仅规整空隙"))
        end
    ''')


def test_normalize_dialog_options_and_validation(run_lua):
    body = SOURCE[SOURCE.index("function show_normalize_length_config_dialog("):SOURCE.index("function get_selected_backup_entry()")]
    run_lua('''
        win = {}; items = {}; current_options = nil
        ui = setmetatable({}, {__index = function() return function(_, value)
            if type(value) == "table" and value.ID then items[value.ID] = value end
            return value
        end end})
        dispatcher = {AddWindow = function()
            return {On = setmetatable({}, {__index = function(self, key)
                    local handlers = {}; rawset(self, key, handlers); return handlers end}),
                Find = function(_, id) return items[id] end,
                Show = function() end, Hide = function() end, SetAttrs = function() end}
        end}
        SUBFIX_WINDOW_GEOMETRY = {centered_geometry = function(value) return value end}
        SUBFIX_AUDIO_ALIGN = {parse_normalize_length_bias_input = function() return "auto", 0 end}
        resolve_window = function(value) return value end
        update_shared_status = function() end
        restart_ui_timer = function() return false end
        run_normalize_subtitle_length = function(_, options) current_options = options end
    ''' + body + '''
        local dialog = show_normalize_length_config_dialog(win)
        local align = assert(items.NormalizeLengthAlignAudioCheckbox, "missing alignment checkbox")
        local gaps = assert(items.NormalizeLengthFillGapsCheckbox, "missing gap checkbox")
        assert(not items.NormalizeLengthBiasInput and not items.NormalizeLengthSelectionHint)
        assert(align.Checked and gaps.Checked, "defaults must preserve existing behavior")
        for _, pair in ipairs({{false, true}, {true, false}, {true, true}, {false, false}}) do
            align.Checked = pair[1]; gaps.Checked = pair[2]
            dialog.On.NormalizeLengthAlignAudioCheckbox.Clicked()
            dialog.On.NormalizeLengthFillGapsCheckbox.Clicked()
            assert(items.NormalizeLengthStartBtn.Enabled == (pair[1] or pair[2]))
            current_options = nil
            dialog.On.NormalizeLengthStartBtn.Clicked()
            if pair[1] or pair[2] then
                assert(current_options.align_audio == pair[1])
                assert(current_options.fill_gaps == pair[2])
                assert(current_options.bias_mode == "manual" and current_options.bias_frames == 0)
            else
                assert(current_options == nil, "empty selection must not start a task")
            end
        end
    ''')
