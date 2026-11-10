"""Run real Lua writeback paths with a locked root and a selected personal bin."""

from pathlib import Path

import pytest

from test_normalize_length_options import run_lua


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "SubFix.lua").read_text(encoding="utf-8")
CORE = (ROOT / ".subfix_support/subfix_generate_selection_core.lua").read_text(encoding="utf-8")
UPDATE = SOURCE[SOURCE.index("update_timeline = function()"):SOURCE.index("local function do_ai_fix()")]
IMPORT = CORE[CORE.index("local function import_rebuild_srt_with_retry("):CORE.index("local function rebuild_target_subtitle_track_from_rows(")]

SETUP = '''
    print = function() end
    LogMsg = function() end
    local imported, deleted, appended, cleaned = 0, 0, 0, 0
    local root = {GetClipList = function() return {} end}
    local personal = {}
    local selected = personal
    local media_item = {}
    local pool = {
        GetRootFolder = function() return root end,
        GetCurrentFolder = function()
            if mode == "no_folder" then return nil end
            if mode == "folder_error" then error("folder unavailable") end
            return selected
        end,
        SetCurrentFolder = function(_, folder)
            if mode == "switch_error" then error("switch unavailable") end
            if mode == "switch_false" then return false end
            selected = folder
            return true
        end,
        ImportMedia = function()
            imported = imported + 1
            if mode == "import_error" then error("import unavailable") end
            if selected == root or mode == "empty" then return {} end
            if mode == "nil" then return nil end
            if mode == "false" then return false end
            if mode == "retry" and imported == 1 then return {} end
            return {media_item}
        end,
        DeleteClips = function() cleaned = cleaned + 1; return true end,
        AppendToTimeline = function(_, items)
            assert(items[1] == media_item)
            appended = appended + 1
            return {media_item}
        end
    }
'''

UPDATE_SETUP = '''
    current_rows = {{start_frame = 100, end_frame = 125, text = "test"}}
    current_subtitle_target_track = 1
    current_backup_path = "/mock"
    current_work_scope = nil
    WORK_SCOPE_MODE_SELECTION = "selection"
    local timeline = {
        GetStartFrame = function() return 100 end,
        GetSetting = function() return "25" end
    }
    local project = {
        GetMediaPool = function() return pool end,
        GetCurrentTimeline = function() return timeline end
    }
    function get_resolve()
        return {
            GetProjectManager = function() return {GetCurrentProject = function() return project end} end,
            GetVersion = function() return {21, 1} end
        }
    end
    local status_text, playhead_restored
    win = {Find = function() return {Set = function(_, _, text) status_text = text end} end}
    function capture_timeline_playhead_timecode() return "01:00:00:00" end
    function restore_timeline_playhead_timecode() playhead_restored = true end
    function apply_approved_pending_changes_to_rows() return 0 end
    function sort_rows_by_timing() end
    function milliseconds_to_srt_time() return "00:00:00,000" end
    io.open = function() return {write = function() end, close = function() end} end
    function ensure_subtitle_track_exists() return {} end
    SUBFIX_SUBTITLE_FADES = {
        prepare = function() return {} end,
        save = function() return true end,
        restore = function() return true, 0 end
    }
    function get_subtitle_track_state_snapshot() return nil end
    function unlock_all_subtitle_tracks() return true end
    function isolate_subtitle_target_track()
        if mode == "isolate_failure" then return false, "cannot isolate" end
        return true, "ready", false
    end
    function clear_subtitle_track_clips()
        deleted = deleted + 1
        assert(imported > 0 and selected == personal, "old subtitles cleared before successful import")
        return true, 1, 0
    end
    function snapshot_subtitle_tracks() return {tracks = {[1] = 0}} end
    function cleanup_imported_subtitle_media_item(_, item)
        assert(item == media_item)
        cleaned = cleaned + 1
    end
    function detect_subtitle_track_delta() return {reused_target_track = true, detected_track = 1} end
    function format_subtitle_track_delta_summary() return "ok" end
    function get_subtitle_track_items() return {} end
'''


@pytest.mark.parametrize("mode", ["empty", "nil", "false", "import_error", "no_folder", "folder_error", "switch_false", "switch_error"])
def test_failed_update_preserves_subtitles_and_reports_next_step(run_lua, mode):
    run_lua(f'mode = "{mode}"\n' + SETUP + UPDATE_SETUP + UPDATE + '''
        assert(update_timeline() == false, "failure must be propagated to selection writeback")
        assert(deleted == 0 and appended == 0, "failed import changed the timeline")
        assert(selected == personal, "must not fall back to the locked root")
        assert(status_text:find("媒体夹") and status_text:find("重试"), "missing actionable error")
        assert(playhead_restored)
        if mode:find("folder") or mode:find("switch") then assert(imported == 0) end
    ''')


def test_successful_update_uses_selected_bin_and_imports_before_clear(run_lua):
    run_lua('mode = "success"\n' + SETUP + UPDATE_SETUP + UPDATE + '''
        update_timeline()
        assert(imported == 1 and deleted == 1 and appended == 1 and cleaned == 1)
        assert(selected == personal and playhead_restored)
    ''')


def test_update_cleans_import_if_track_preparation_fails(run_lua):
    run_lua('mode = "isolate_failure"\n' + SETUP + UPDATE_SETUP + UPDATE + '''
        update_timeline()
        assert(imported == 1 and cleaned == 1)
        assert(deleted == 0 and appended == 0)
    ''')


@pytest.mark.parametrize("mode", ["success", "retry", "empty", "nil", "false", "import_error", "no_folder", "folder_error", "switch_false", "switch_error"])
def test_generator_import_stays_in_selected_bin(run_lua, mode):
    run_lua(f'mode = "{mode}"\n' + SETUP + '''
        os.execute = function() end
    ''' + IMPORT + '''
        local items, err = import_rebuild_srt_with_retry(pool, "/mock/test.srt")
        assert(selected == personal, "generator must not switch to the root")
        if mode == "success" or mode == "retry" then
            assert(items and items[1] == media_item and err == nil)
            assert(imported == (mode == "retry" and 2 or 1))
        else
            assert(items == nil and err:find("媒体夹") and err:find("重试"))
            if mode:find("folder") or mode:find("switch") then assert(imported == 0) end
        end
    ''')


@pytest.mark.parametrize("path", [ROOT / "SubFix.lua", ROOT / ".subfix_support/subfix_generate_selection_core.lua"])
def test_full_lua_file_compiles(run_lua, path):
    run_lua(f'assert(loadfile([[{path}]]))')
