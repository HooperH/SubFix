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
    support = section("local function shell_quote(", "-- 模块可独立加载")
    support += section("local function trim_text(", "local function file_exists(")
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
        function resolve_asr_paths() return {hotwords = 'hotwords.json', reference_base = prefs_path .. '.reference'} end
        local function load_generate_hotword_entries() return entries or {{term = 'SubFix'}} end
        local function inspect_local_qwen()
            assert(not forbid_model_inspection, 'cloud generation inspected local models')
            return model_status or {ready = true, aligner = 'aligner.gguf'}
        end
        local function file_exists(path) return path == 'aligner.gguf' end
        local runtime_options
        function reference_text() return runtime_options.reference_text end
        function new_generation(fallback, project_id, project_name)
            local project = {GetUniqueId = function() return project_id end,
                GetName = function() return project_name or '同名项目' end}
            if project_id == nil then project_id = 'A' end
            runtime_options = {correction_reference = fallback, project_reference_path = project_reference_path(project)}
        end
        new_generation()
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
        function open_dialog(track_indices, scope)
            controls = {}; windows = {}
            local sources = {}
            for _, index in ipairs(track_indices or {1}) do
                sources[#sources + 1] = {track_index = index}
            end
            return show_audio_track_selection_dialog(sources, scope or {}, 24)
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


@pytest.mark.parametrize("use_return", [True, False])
def test_hotword_return_and_button_add_validate_and_save(dialog, use_return):
    run, _ = dialog
    run("use_return = " + str(use_return).lower())
    run('''
        entries = {}; save_count = 0
        function save_generate_hotword_entries(values)
            save_count = save_count + 1
            saved_entries = values
            return true
        end
        interaction = function(w, items)
            w.On.GenerateHotwordManageBtn.Clicked()
            local library = windows.GenerateHotwordLibraryWindow
            local input = items.GenerateHotwordTermInput
            local tree = items.GenerateHotwordTree
            assert(input.Events.ReturnPressed == true)
            local add = use_return and library.On.GenerateHotwordTermInput.ReturnPressed
                or library.On.GenerateHotwordAddBtn.Clicked
            input.Text = '   '
            add()
            assert(#tree.rows == 0)
            assert(items.GenerateHotwordStatusLabel.Text == '请填写标准词')
            input.Text = '  DaVinci Resolve  '
            add()
            assert(#tree.rows == 1 and tree.rows[1].Text[0] == 'DaVinci Resolve')
            assert(input.Text == '')
            add()
            assert(#tree.rows == 1, 'empty repeated submit added another word')
            input.Text = '达芬奇'
            add()
            assert(#tree.rows == 2 and tree.rows[2].Text[0] == '达芬奇')
            assert(input.Text == '')
            assert(items.GenerateHotwordToggleBtn.Text:find('2 条', 1, true))
            assert(save_count == 0, 'addition changed the save-on-completion behavior')
            library.On.GenerateHotwordCloseBtn.Clicked()
            assert(save_count == 1 and #saved_entries == 2)
            assert(saved_entries[1].term == 'DaVinci Resolve')
            assert(saved_entries[2].term == '达芬奇')
            w.On.GenerateSelectionCancelBtn.Clicked()
        end
        open_dialog()
    ''')


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


@pytest.mark.parametrize("backend", ["doubao_asr", "doubao_asr_v2"])
def test_cloud_generation_does_not_require_local_models(dialog, backend):
    run, _ = dialog
    run("cloud_backend = " + json.dumps(backend))
    run('''
        model_status = {ready = false, aligner = ''}
        forbid_model_inspection = true
        assert(save_doubao(cloud_backend))
        assert(save_engine(cloud_backend))
        interaction = function(w) w.On.GenerateSelectionConfirmBtn.Clicked() end
        local sources, _, _, backend, _, err = open_dialog()
        assert(sources and backend == cloud_backend and err == nil)
    ''')


def test_missing_aligner_routes_local_generation_to_model_center(dialog):
    run, _ = dialog
    run('''
        model_status = {ready = false, aligner = ''}
        assert(save_engine('auto'))
        interaction = function(w)
            w.On.GenerateSelectionConfirmBtn.Clicked()
            windows.GenerateQwenDownloadWindow.On.GenerateQwenDownloadConfirmBtn.Clicked()
        end
        local sources, _, _, _, _, err = open_dialog()
        assert(sources == nil and err == '__subfix_model_center__')
    ''')


def test_model_center_round_trip_preserves_unsaved_generation_choices(dialog):
    run, prefs = dialog
    run('''
        assert(save_engine('auto'))
        interaction = function(w, items)
            w.On.GenerateSubtitleLengthShortBtn.Clicked()
            click_track(w, items, 1)
            click_track(w, items, 2)
            items.GenerateReferenceText.PlainText = '项目内草稿'
            w.On.GenerateReferenceToggleBtn.Clicked()
            model_status = {ready = false, aligner = ''}
            w.On.GenerateSelectionConfirmBtn.Clicked()
            windows.GenerateQwenDownloadWindow.On.GenerateQwenDownloadConfirmBtn.Clicked()
        end
        local _, _, _, _, _, err = open_dialog({1, 2})
        assert(err == '__subfix_model_center__')
        interaction = function(w, items)
            assert(items.GenerateSubtitleEngineQwenBtn.Text:match('^yes'))
            assert(items.GenerateSubtitleLengthShortBtn.Text:match('^yes'))
            assert_checked_tracks(items, {2})
            assert(items.GenerateReferenceText.PlainText == '项目内草稿')
            assert(items.GenerateReferenceToggleBtn.Text:match('^no'))
            w.On.GenerateSelectionCancelBtn.Clicked()
        end
        open_dialog({1, 2})
    ''')
    assert json.loads(prefs.read_text())["last_engine_backend"] == "auto"


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


def test_full_timeline_selects_all_tracks_and_keeps_marked_range_preferences(dialog):
    run, prefs = dialog
    prefs.write_text(json.dumps({"audio_track_count": 3, "selected_audio_tracks": [2]}))
    run("""
        interaction = function(w, items)
            assert_checked_tracks(items, {1, 2, 3})
            click_track(w, items, 2)
            w.On.GenerateSelectionConfirmBtn.Clicked()
        end
        local sources = open_dialog({1, 2, 4}, {source='full_timeline'})
        assert(#sources == 2 and sources[1].track_index == 1 and sources[2].track_index == 4)
    """)
    assert json.loads(prefs.read_text())["selected_audio_tracks"] == [2]

@pytest.mark.parametrize("extension", ["txt", "md", "docx"])
def test_generation_can_import_edit_and_clear_reference(dialog, tmp_path, extension):
    import subprocess
    run, _ = dialog
    src = tmp_path / "小林's 采访.txt"
    src.write_text("小林与演播嘉宾", encoding="utf-8")
    target = src.with_suffix("." + extension)
    if extension == "docx":
        subprocess.run(["/usr/bin/textutil", "-convert", "docx", "-output", str(target), str(src)], check=True)
    elif extension == "md":
        target.write_bytes(src.read_bytes())
    run("document_path = " + json.dumps(str(target), ensure_ascii=False))
    run("""
        fusion_app = {RequestFile = function() return document_path end}
        interaction = function(w, items)
            w.On.GenerateReferenceManageBtn.Clicked()
            local editor = windows.GenerateReferenceWindow
            assert(items.GenerateReferenceToggleBtn.Text == 'no 0 字')
            editor.On.GenerateReferenceImport.Clicked()
            assert(items.GenerateReferenceToggleBtn.Text == 'yes 7 字')
            assert(items.GenerateReferenceText.PlainText == '小林与演播嘉宾')
            editor.On.GenerateReferenceClear.Clicked()
            assert(items.GenerateReferenceText.PlainText == '')
            items.GenerateReferenceText.PlainText = '剪辑采访'
            editor.On.GenerateReferenceDone.Clicked()
            w.On.GenerateReferenceToggleBtn.Clicked()
            assert(items.GenerateReferenceToggleBtn.Text == 'no 4 字')
            assert(items.GenerateReferenceText.PlainText == '剪辑采访')
            w.On.GenerateReferenceToggleBtn.Clicked()
            w.On.GenerateSelectionConfirmBtn.Clicked()
        end
        open_dialog()
        assert(reference_text() == '剪辑采访')
    """)


def test_disabling_reference_skips_it_for_generation(dialog):
    run, _ = dialog
    run("""
        interaction = function(w, items)
            w.On.GenerateReferenceManageBtn.Clicked()
            local editor = windows.GenerateReferenceWindow
            items.GenerateReferenceText.PlainText = '现场采访'
            editor.On.GenerateReferenceText.TextChanged()
            editor.On.GenerateReferenceWindow.Close()
            assert(items.GenerateReferenceToggleBtn.Text == 'yes 4 字')
            w.On.GenerateReferenceToggleBtn.Clicked()
            w.On.GenerateReferenceManageBtn.Clicked()
            assert(items.GenerateReferenceText.PlainText == '现场采访')
            editor.On.GenerateReferenceDone.Clicked()
            w.On.GenerateSelectionConfirmBtn.Clicked()
        end
        assert(open_dialog())
        assert(reference_text() == '')
    """)


def test_reference_text_and_toggle_survive_fresh_generation_dialogs(dialog):
    run, prefs = dialog
    sample = '采访 "小林" 🎬\n第二行\\人名\t关键词\f末尾'
    run("sample = " + json.dumps(sample, ensure_ascii=False))
    run("""
        new_generation('矫正文稿')
        interaction = function(w, items)
            assert(items.GenerateReferenceText.PlainText == '')
            assert(items.GenerateReferencePrevious.Visible and items.GenerateReferencePrevious.Text == '载入矫正文稿')
            windows.GenerateReferenceWindow.On.GenerateReferencePrevious.Clicked()
            assert(items.GenerateReferenceText.PlainText == '矫正文稿')
            items.GenerateReferenceText.PlainText = sample
            windows.GenerateReferenceWindow.On.GenerateReferenceDone.Clicked()
            w.On.GenerateReferenceToggleBtn.Clicked()
            w.On.GenerateSelectionCancelBtn.Clicked()
        end
        open_dialog()
        new_generation('更新后的矫正文稿')
        interaction = function(w, items)
            assert(items.GenerateReferenceText.PlainText == sample)
            assert(items.GenerateReferenceToggleBtn.Text:match('^no'))
            w.On.GenerateSelectionConfirmBtn.Clicked()
        end
        assert(open_dialog() and reference_text() == '')
        new_generation(nil)
        interaction = function(w)
            w.On.GenerateReferenceToggleBtn.Clicked()
            w.On.GenerateSelectionWindow.Close()
        end
        open_dialog()
        new_generation(nil)
        interaction = function(w, items)
            assert(items.GenerateReferenceToggleBtn.Text:match('^yes'))
            w.On.GenerateSelectionConfirmBtn.Clicked()
        end
        assert(open_dialog() and reference_text() == sample)
    """)
    assert json.loads(Path(str(prefs) + ".reference.41.json").read_text()) == {"text": sample, "enabled": True}


@pytest.mark.parametrize("initial", ["", "上次文稿"])
def test_explicitly_cleared_reference_stays_empty(dialog, initial):
    run, _ = dialog
    run("initial = " + json.dumps(initial, ensure_ascii=False))
    run("""
        new_generation()
        interaction = function(w)
            controls.GenerateReferenceText.PlainText = initial
            windows.GenerateReferenceWindow.On.GenerateReferenceClear.Clicked()
            w.On.GenerateSelectionWindow.Close()
        end
        open_dialog()
        new_generation('新的矫正文稿')
        interaction = function(w, items)
            assert(items.GenerateReferenceText.PlainText == '')
            assert(items.GenerateReferenceToggleBtn.Text == 'no 0 字')
            w.On.GenerateSelectionConfirmBtn.Clicked()
        end
        assert(open_dialog() and reference_text() == '')
    """)


def test_failed_reference_save_preserves_previous_copy(dialog):
    run, prefs = dialog
    run("""
        interaction = function(w, items)
            local editor = windows.GenerateReferenceWindow
            items.GenerateReferenceText.PlainText = '已保存'
            editor.On.GenerateReferenceDone.Clicked()
            fail_write = true
            items.GenerateReferenceText.PlainText = '新文稿'
            editor.On.GenerateReferenceDone.Clicked()
            assert(items.GenerateReferenceStatus.Text:find('保存失败'))
            w.On.GenerateSelectionConfirmBtn.Clicked()
        end
        assert(open_dialog() and reference_text() == '新文稿')
    """)
    path = Path(str(prefs) + ".reference.41.json")
    assert json.loads(path.read_text()) == {"text": "已保存", "enabled": True}
    assert not list(path.parent.glob(path.name + ".*"))


def test_switching_projects_restores_each_text_and_toggle(dialog):
    run, _ = dialog
    run("""
        local function check(project_id, name, text, checked, edit)
            new_generation('全局矫正文稿', project_id, name)
            interaction = function(w, items)
                assert(items.GenerateReferenceText.PlainText == text)
                assert(items.GenerateReferenceToggleBtn.Text:match(checked and '^yes' or '^no'))
                if edit then edit(w, items, windows.GenerateReferenceWindow) end
                w.On.GenerateSelectionConfirmBtn.Clicked()
            end
            assert(open_dialog())
            return reference_text()
        end
        assert(check('A', '采访', '', false, function(w, items, editor)
            items.GenerateReferenceText.PlainText = '甲稿'
            editor.On.GenerateReferenceDone.Clicked()
            w.On.GenerateReferenceToggleBtn.Clicked()
        end) == '')
        assert(check('a', '采访', '', false, function(w, items, editor)
            items.GenerateReferenceText.PlainText = '乙稿'
            editor.On.GenerateReferenceWindow.Close()
        end) == '乙稿')
        assert(check('A', '改名后的采访', '甲稿', false) == '')
        assert(check('a', '采访', '乙稿', true, function(w, items, editor)
            editor.On.GenerateReferenceClear.Clicked()
        end) == '')
        assert(check('A', '改名后的采访', '甲稿', false) == '')
        assert(check('a', '采访', '', false) == '')
    """)


def test_legacy_reference_can_be_adopted_explicitly_by_one_project(dialog):
    run, prefs = dialog
    legacy = Path(str(prefs) + ".reference.json")
    legacy.write_text(json.dumps({"text": "旧文稿", "enabled": False}, ensure_ascii=False))
    run("""
        interaction = function(w, items)
            assert(items.GenerateReferenceText.PlainText == '')
            assert(items.GenerateReferencePrevious.Visible and items.GenerateReferencePrevious.Text == '载入旧版文稿')
            windows.GenerateReferenceWindow.On.GenerateReferencePrevious.Clicked()
            assert(items.GenerateReferenceToggleBtn.Text == 'no 3 字')
            windows.GenerateReferenceWindow.On.GenerateReferenceDone.Clicked()
            w.On.GenerateSelectionCancelBtn.Clicked()
        end
        open_dialog()
        new_generation(nil, 'B')
        interaction = function(w, items)
            assert(items.GenerateReferenceText.PlainText == '')
            w.On.GenerateSelectionConfirmBtn.Clicked()
        end
        assert(open_dialog() and reference_text() == '')
        new_generation(nil, 'A')
        interaction = function(w, items)
            assert(items.GenerateReferenceText.PlainText == '旧文稿')
            assert(items.GenerateReferenceToggleBtn.Text == 'no 3 字')
            assert(not items.GenerateReferencePrevious.Visible)
            w.On.GenerateSelectionCancelBtn.Clicked()
        end
        open_dialog()
    """)
    assert json.loads(legacy.read_text())["text"] == "旧文稿"


def test_missing_project_identity_keeps_reference_session_local(dialog):
    run, prefs = dialog
    run("""
        new_generation(nil, false)
        interaction = function(w, items)
            assert(items.GenerateReferenceStatus.Text:find('仅用于本次'))
            items.GenerateReferenceText.PlainText = '临时使用'
            w.On.GenerateSelectionConfirmBtn.Clicked()
        end
        assert(open_dialog() and reference_text() == '临时使用')
    """)
    assert not list(prefs.parent.glob("*.reference*"))
