"""Exercise generation preferences and dialog events in the Resolve Lua runtime."""

import json
import os
from pathlib import Path

import pytest

from test_generate_progress_lifecycle import run_lua  # noqa: F401


SOURCE_PATH = Path(os.environ.get("SUBFIX_TEST_GENERATE_CORE", str(
    Path(__file__).resolve().parents[1] / ".subfix_support/subfix_generate_selection_core.lua"
)))
SOURCE = SOURCE_PATH.read_text()


def section(start, end):
    return SOURCE[SOURCE.index(start):SOURCE.index(end)]


def test_complete_generate_module_compiles(run_lua):
    run_lua("assert(loadfile(" + json.dumps(str(SOURCE_PATH)) + "))")


@pytest.fixture
def dialog(run_lua, tmp_path):
    prefs = tmp_path / "subfix_generate_prefs.json"
    run_lua("prefs_path = " + json.dumps(str(prefs)))
    support = section("local function trim_text(", "local function file_exists(")
    support += section("local function json_escape(", "local function get_checkbox_checked(")
    support += """
        local function read_text_file(path)
            local f = io.open(path, 'rb'); if not f then return nil end
            local text = f:read('*a'); f:close(); return text
        end
        local function write_text_file(path, text)
            if fail_write then return false end
            local f = assert(io.open(path, 'wb')); f:write(text); f:close(); return true
        end
        local function generate_prefs_file_path() return prefs_path end
    """
    support += section("local function normalize_doubao_backend(", "local function load_generate_hotword_entries(")
    run_lua(support + """
        save_engine = save_last_engine_backend
        save_doubao = save_doubao_backend_preference
        local TRACK_CHECKED_MARK, TRACK_UNCHECKED_MARK = 'yes', 'no'
        local function resolve_asr_paths() return {hotwords = 'hotwords.json'} end
        local function load_generate_hotword_entries() return entries or {{term = 'SubFix'}} end
        local function inspect_local_qwen() return {ready = true} end
        local function format_audio_source_label() return 'A1' end
        local function set_tree_item_text(item, column, text)
            item.Text = item.Text or {}; item.Text[column] = text
        end
        local function get_tree_event_value(event, keys)
            for _, key in ipairs(keys) do if event[key] then return event[key] end end
        end
        local function get_selected_tree_node() return nil end
        local function safe_refresh_tree_widget() end
        local function doubao_credentials_configured() return true end
        SUBFIX_WINDOW_GEOMETRY = {centered_geometry = function(v) return v end}
        local ui = setmetatable({}, {__index = function() return function(_, v)
            if type(v) == 'table' and v.ID then
                v.ColumnWidth = {}; v.AddItem = function() end
                v.NewItem = function() return {} end
                v.rows = {}
                v.AddTopLevelItem = function(_, item) v.rows[#v.rows + 1] = item end
                controls[v.ID] = v
            end
            return v
        end end})
        local dispatcher = {
            AddWindow = function(_, spec)
                local window = {GetItems = function() return controls end,
                    Show = function() end, Hide = function() end,
                    On = setmetatable({}, {__index = function(t, k)
                        local events = {}; rawset(t, k, events); return events
                    end})}
                windows[spec.ID] = window
                return window
            end,
            RunLoop = function() interaction(windows.GenerateSelectionWindow, controls) end,
            ExitLoop = function() end,
        }
    """ + section("local function show_audio_track_selection_dialog(", "local function progress_elapsed_text(") + """
        function open_dialog(track_indices)
            controls = {}; windows = {}
            local sources = {}
            for _, index in ipairs(track_indices or {1}) do
                sources[#sources + 1] = {track_index = index}
            end
            return show_audio_track_selection_dialog(sources, {}, 24)
        end
        function click_track(window, items, index)
            window.On.GenerateAudioTrackTree.ItemClicked({item = items.GenerateAudioTrackTree.rows[index]})
        end
        function assert_checked_tracks(items, expected)
            local selected = {}
            for _, index in ipairs(expected) do selected[index] = true end
            for index, item in ipairs(items.GenerateAudioTrackTree.rows) do
                assert(item.Text[0] == (selected[index] and 'yes' or 'no'),
                    'unexpected selection on track row ' .. index)
            end
        end
    """)
    return run_lua, prefs


def test_generation_restores_length_and_disabled_hotwords_after_model_changes(dialog):
    run, prefs = dialog
    run("""
        interaction = function(w)
            w.On.GenerateSubtitleLengthShortBtn.Clicked()
            w.On.GenerateHotwordToggleBtn.Clicked()
            w.On.GenerateSelectionConfirmBtn.Clicked()
        end
        local _, _, length, _, hotwords = open_dialog()
        assert(length == 10 and hotwords == nil)
        assert(save_doubao('doubao_asr_v2'))
        assert(save_engine('doubao_asr_v2'))
        interaction = function(w, items)
            assert(items.GenerateSubtitleLengthShortBtn.Text:match('^yes'), 'short length was not restored')
            assert(items.GenerateHotwordToggleBtn.Text:match('^no'), 'disabled hotwords were not restored')
            w.On.GenerateSubtitleLengthStandardBtn.Clicked()
            w.On.GenerateHotwordToggleBtn.Clicked()
            w.On.GenerateSelectionConfirmBtn.Clicked()
        end
        local _, _, length, backend, hotwords = open_dialog()
        assert(length == 25 and backend == 'doubao_asr_v2' and hotwords == 'hotwords.json')
    """)
    saved = json.loads(prefs.read_text())
    assert saved["max_chars"] == 25
    assert saved["hotwords_enabled"] is True


@pytest.mark.parametrize("seed", [{}, {"max_chars": 999, "hotwords_enabled": "false"}, {"last_engine_backend": "doubao_asr_v2"}])
def test_legacy_or_invalid_preferences_keep_defaults_and_cancel_does_not_save(dialog, seed):
    run, prefs = dialog
    prefs.write_text(json.dumps(seed))
    run("""
        assert(save_engine('auto'))
        interaction = function(w, items)
            assert(items.GenerateSubtitleLengthStandardBtn.Text:match('^yes'))
            assert(items.GenerateHotwordToggleBtn.Text:match('^yes'))
            w.On.GenerateSubtitleLengthShortBtn.Clicked()
            w.On.GenerateHotwordToggleBtn.Clicked()
            w.On.GenerateSelectionCancelBtn.Clicked()
        end
        open_dialog()
        interaction = function(w, items)
            assert(items.GenerateSubtitleLengthStandardBtn.Text:match('^yes'))
            assert(items.GenerateHotwordToggleBtn.Text:match('^yes'))
            w.On.GenerateSelectionWindow.Close()
        end
        open_dialog()
    """)


def test_empty_library_cannot_be_enabled_by_stale_preference(dialog):
    run, prefs = dialog
    prefs.write_text('{"hotwords_enabled": true, "max_chars": 10}')
    run("""
        entries = {}
        interaction = function(w, items)
            assert(items.GenerateHotwordToggleBtn.Text:match('^no'))
            w.On.GenerateSelectionConfirmBtn.Clicked()
        end
        local _, _, length, _, hotwords = open_dialog()
        assert(length == 10 and hotwords == nil)
    """)


def test_track_selection_survives_reopening_and_model_preference_updates(dialog):
    run, prefs = dialog
    run("""
        interaction = function(w, items)
            assert_checked_tracks(items, {1})
            click_track(w, items, 1)
            click_track(w, items, 2)
            click_track(w, items, 3)
            w.On.GenerateSelectionConfirmBtn.Clicked()
        end
        local selected = open_dialog({1, 2, 4})
        assert(#selected == 2 and selected[1].track_index == 2 and selected[2].track_index == 4)
        assert(save_doubao('doubao_asr_v2'))
        assert(save_engine('doubao_asr_v2'))
        interaction = function(w, items)
            assert_checked_tracks(items, {2, 3})
            w.On.GenerateSelectionConfirmBtn.Clicked()
        end
        open_dialog({1, 2, 4})
    """)
    saved = json.loads(prefs.read_text())
    assert saved["audio_track_count"] == 3
    assert saved["selected_audio_tracks"] == [2, 4]


@pytest.mark.parametrize("tracks", [[1, 2], [1, 2, 3, 4]])
def test_track_count_change_resets_selection_and_remembers_new_choice(dialog, tracks):
    run, prefs = dialog
    prefs.write_text(json.dumps({"audio_track_count": 3, "selected_audio_tracks": [2, 3]}))
    run("new_tracks = {" + ",".join(map(str, tracks)) + "}")
    run("""
        interaction = function(w, items)
            assert_checked_tracks(items, {1})
            click_track(w, items, 2)
            w.On.GenerateSelectionConfirmBtn.Clicked()
        end
        open_dialog(new_tracks)
        interaction = function(w, items)
            assert_checked_tracks(items, {1, 2})
            w.On.GenerateSelectionConfirmBtn.Clicked()
        end
        open_dialog(new_tracks)
    """)
    saved = json.loads(prefs.read_text())
    assert saved["audio_track_count"] == len(tracks)
    assert saved["selected_audio_tracks"] == [1, 2]


@pytest.mark.parametrize("exit_event", ["GenerateSelectionCancelBtn", "GenerateSelectionWindow"])
def test_cancel_or_close_does_not_overwrite_saved_tracks(dialog, exit_event):
    run, prefs = dialog
    seed = {"audio_track_count": 3, "selected_audio_tracks": [2]}
    prefs.write_text(json.dumps(seed))
    event = "Close" if exit_event == "GenerateSelectionWindow" else "Clicked"
    run("""
        interaction = function(w, items)
            assert_checked_tracks(items, {2})
            click_track(w, items, 1)
            w.On.""" + exit_event + "." + event + """()
        end
        local selected = open_dialog({1, 2, 3})
        assert(selected == nil)
        interaction = function(w, items)
            assert_checked_tracks(items, {2})
            w.On.GenerateSelectionCancelBtn.Clicked()
        end
        open_dialog({1, 2, 3})
    """)
    assert json.loads(prefs.read_text()) == seed


def test_confirming_empty_selection_does_not_overwrite_saved_tracks(dialog):
    run, prefs = dialog
    seed = {"audio_track_count": 2, "selected_audio_tracks": [2]}
    prefs.write_text(json.dumps(seed))
    run("""
        interaction = function(w, items)
            assert_checked_tracks(items, {2})
            click_track(w, items, 2)
            w.On.GenerateSelectionConfirmBtn.Clicked()
            assert(items.GenerateSelectionInfoLabel.Text:find('至少选择'))
            w.On.GenerateSelectionCancelBtn.Clicked()
        end
        open_dialog({1, 2})
    """)
    assert json.loads(prefs.read_text()) == seed


@pytest.mark.parametrize("saved", [None, "invalid", [], [99], [False, -1, 1.5]])
def test_missing_or_invalid_saved_tracks_default_to_first_available_track(dialog, saved):
    run, prefs = dialog
    prefs.write_text(json.dumps({"audio_track_count": 2, "selected_audio_tracks": saved}))
    run("""
        interaction = function(w, items)
            assert_checked_tracks(items, {1})
            w.On.GenerateSelectionConfirmBtn.Clicked()
        end
        local selected = open_dialog({3, 5})
        assert(#selected == 1 and selected[1].track_index == 3)
    """)
