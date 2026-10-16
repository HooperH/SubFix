from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "SubFix.lua").read_text(encoding="utf-8")
GENERATOR_SOURCE_PATH = ROOT / "生成选区字幕.lua"
GENERATOR_SOURCE = GENERATOR_SOURCE_PATH.read_text(encoding="utf-8") if GENERATOR_SOURCE_PATH.exists() else ""
GENERATOR_CORE_SOURCE_PATH = ROOT / ".subfix_support" / "subfix_generate_selection_core.lua"
GENERATOR_CORE_SOURCE = GENERATOR_CORE_SOURCE_PATH.read_text(encoding="utf-8") if GENERATOR_CORE_SOURCE_PATH.exists() else ""
SYNC_SCRIPT = (ROOT / "sync_to_plugin.sh").read_text(encoding="utf-8")


def body_between(start_marker: str, end_marker: str) -> str:
    start = SOURCE.index(start_marker)
    end = SOURCE.index(end_marker, start)
    return SOURCE[start:end]


def test_removed_auto_align_entry_only_reports_its_replacement():
    run_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.run(target_window)",
        "-- ========== 刷新字幕列表 ==========",
    )

    assert "generate_reference_rows" not in run_body
    assert "CreateSubtitlesFromAudio" not in run_body
    assert '"自动对齐声音已移除，请使用规整字幕长度"' in run_body
    assert "run_stable_ts_alignment" not in run_body
    assert "run_asr_alignment" not in run_body
    assert "detect_speech_segments" not in run_body
    assert 'alignment_mode = "text_progress"' not in run_body
    assert 'alignment_mode = "global_shift"' not in run_body


def test_audio_source_detection_uses_timeline_audio_items():
    item_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.audio_source_from_item",
        "function SUBFIX_AUDIO_ALIGN.copy_row_for_audio_source",
    )
    batch_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.find_primary_audio_track_batches",
        "function SUBFIX_AUDIO_ALIGN.detect_speech_segments",
    )

    assert 'timeline:GetTrackCount("audio")' in batch_body
    assert 'timeline:GetItemListInTrack("audio", track_index)' in batch_body
    assert "GetMediaPoolItem" in item_body
    assert 'GetClipProperty("File Path")' in item_body
    assert "GetLeftOffset" in item_body
    assert "source_timecode_start_seconds" in item_body
    assert "parse_source_audio_channel_mapping(item, media_item" in item_body
    assert "source_start_seconds = tonumber(mapped_audio.source_start_seconds) or (source_offset_frames / effective_fps)" in item_body


def test_primary_audio_track_excludes_non_dialogue_sources_before_ranking():
    batch_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.find_primary_audio_track_batches",
        "function SUBFIX_AUDIO_ALIGN.detect_speech_segments",
    )

    assert "function SUBFIX_AUDIO_ALIGN.is_non_dialogue_audio_source" in SOURCE
    assert '"musicbed"' in SOURCE
    assert '"bgm"' in SOURCE
    assert '"sfx"' in SOURCE
    assert "is_non_dialogue_audio_source(source)" in batch_body
    assert "未找到可用对白音轨" in batch_body


def test_ffmpeg_silencedetect_path_and_parsing_are_present():
    body = body_between("SUBFIX_AUDIO_ALIGN = SUBFIX_AUDIO_ALIGN or {}", "function SUBFIX_AUDIO_ALIGN.match_rows_to_speech_segments")

    assert 'support_root .. "/.subfix_support/bin/ffmpeg"' in body
    assert "silencedetect=noise=-35dB:d=0.08" in body
    assert "silencedetect=noise=-50dB:d=0.08" in body
    assert "silence_start" in body
    assert "silence_end" in body
    assert "merge_speech_segments" in body


def test_alignment_uses_stable_ts_starts_and_gapless_ends():
    match_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.match_rows_to_stable_ts",
        "function SUBFIX_AUDIO_ALIGN.apply_matches",
    )
    apply_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.apply_matches",
        "function SUBFIX_AUDIO_ALIGN.run(target_window)",
    )
    run_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.run(target_window)",
        "-- ========== 刷新字幕列表 ==========",
    )

    assert "aligned_row.start_frame" in match_body
    assert "end_frame = start_frame + original_duration" in match_body
    assert "next_aligned_row" in match_body
    assert "end_frame = next_start_frame" in match_body
    assert "text_progress" not in match_body
    assert "local_onset_for_row" not in run_body
    assert "match_rows_to_asr_segments" not in run_body
    assert "fill_gaps_after_match(current_rows, results)" in apply_body


def test_removed_auto_align_entry_does_not_prepare_audio_batches():
    run_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.run(target_window)",
        "-- ========== 刷新字幕列表 ==========",
    )

    assert "find_primary_audio_track_batches" not in run_body
    assert "run_stable_ts_alignment_batches" not in run_body
    assert "return false" in run_body


def test_only_qwen_forced_alignment_is_wired_for_normalize_length():
    helper = (ROOT / "subfix_asr_transcribe.py").read_text(encoding="utf-8")
    setup = (ROOT / "setup_asr_env.sh").read_text(encoding="utf-8")
    sync = (ROOT / "sync_to_plugin.sh").read_text(encoding="utf-8")
    paths_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.get_asr_paths",
        "function SUBFIX_AUDIO_ALIGN.asr_temp_output_path",
    )

    assert "qwen_forced_align_text_batches" in helper
    assert "qwen3_cpp_forced_align_text_rows" in helper
    assert "stable-ts[mlx]" not in setup
    assert "whisperx" not in setup
    assert "transformers" not in setup
    assert "SUBFIX_INSTALL_QWEN_ASR" in setup
    assert 'USER_VENV_DIR="$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/.subfix_support/.subfix_asr_env"' in setup
    assert 'SUBFIX_INSTALL_QWEN_ASR:-1' in setup
    assert "qwen-asr" in setup
    parser_body = helper[helper.index("def main("):helper.index("args = parser.parse_args")]
    assert '"whisperx_align_text"' not in parser_body
    assert '"ctc_align_text"' not in parser_body
    assert '"ctc_align_text_batches"' not in parser_body
    assert "subfix_asr_transcribe.py" in sync
    assert "setup_asr_env.sh" in sync
    assert 'helper = helper_dir .. "/subfix_asr_transcribe.py"' in paths_body
    assert 'legacy_helper = script_dir .. "/subfix_asr_transcribe.py"' in paths_body
    assert 'legacy_python = script_dir .. "/.subfix_asr_env/bin/python"' in paths_body
    assert 'user_python = home_dir .. "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/.subfix_support/.subfix_asr_env/bin/python"' in paths_body
    assert "python = legacy_python" in paths_body
    assert 'HELPER_DIR="$RESOLVE_DIR/.subfix_support"' in sync
    assert '"$HELPER_DIR/subfix_asr_transcribe.py"' in sync
    assert 'SUBFIX_MENU_DIR="$RESOLVE_DIR/SubFix"' in sync
    assert '"$SUBFIX_MENU_DIR/SubFix.lua"' in sync
    assert 'rm -f "$RESOLVE_DIR/SubFix.lua"' in sync
    assert 'rm -rf "$RESOLVE_DIR/SubFix"' not in sync

    assert 'user_support_dir = home_dir ~= "" and (home_dir .. "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/.subfix_support") or helper_dir' in GENERATOR_CORE_SOURCE
    assert 'user_python = user_support_dir .. "/.subfix_asr_env/bin/python"' in GENERATOR_CORE_SOURCE


def test_legacy_audio_alignment_runners_are_hard_disabled():
    auto_align_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.run(target_window)",
        "-- ========== 刷新字幕列表 ==========",
    )

    assert not re.search(r"function SUBFIX_AUDIO_ALIGN\.run_ctc_text_alignment_batches\(", SOURCE)
    assert '"自动对齐声音已移除，请使用规整字幕长度"' in auto_align_body
    assert "run_stable_ts_alignment_batches" not in auto_align_body
    assert "正在用 stable-ts 对齐现有字幕文本" not in auto_align_body


def test_asr_ffmpeg_path_is_logged_and_actionable():
    run_asr_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.run_asr_alignment",
        "function SUBFIX_AUDIO_ALIGN.format_frame_range",
    )

    assert "未找到内置 ffmpeg 或系统 ffmpeg" in SOURCE
    assert "ffmpeg_missing_message(\"截取音频\")" in run_asr_body
    assert "ASR helper ffmpeg" in run_asr_body


def test_audio_align_default_bias_starts_on_audio():
    assert "SUBFIX_AUDIO_ALIGN.default_bias_frames = -4" in SOURCE
    assert "SUBFIX_AUDIO_ALIGN.default_asr_language = \"zh\"" in SOURCE


def test_audio_align_ui_removed_from_tool_panel():
    assert 'ID = "AudioAlignBtn"' not in SOURCE
    assert 'Text = "自动对齐声音"' not in SOURCE
    assert "function win.On.AudioAlignBtn.Clicked" not in SOURCE
    assert '"AudioAlignBtn"' not in body_between("local GATED_ACTION_IDS = {", "}")


def test_provider_list_contains_only_public_and_custom_providers():
    provider_defs = body_between("AI_PROVIDER_DEFS = {", "AI_PROVIDER_BY_ID = {}")

    assert set(re.findall(r'\bid = "([^"]+)"', provider_defs)) == {
        "siliconflow", "deepseek", "openai", "gemini", "custom_openai",
    }


def test_gap_fill_button_is_renamed_to_normalize_subtitle_length():
    assert 'ID = "BtnStep3", Text = "规整字幕长度"' in SOURCE
    normalize_body = body_between(
        "function run_normalize_subtitle_length",
        "-- 1️⃣ 修改英文排版",
    )
    assert "prepare_mutation_snapshot(\"规整字幕长度\")" in normalize_body
    assert "规整字幕长度完成" in normalize_body


def test_refine_tool_buttons_use_merged_english_typography_and_final_check():
    tool_tab_body = body_between(
        'ID = "ToolTabPage"',
        'ID = "AITabPage"',
    )

    normalize_pos = tool_tab_body.index('ID = "BtnStep3", Text = "规整字幕长度"')
    english_pos = tool_tab_body.index('ID = "BtnStep1", Text = "修改英文排版"')
    final_check_pos = tool_tab_body.index('ID = "BtnStep4", Text = "最终交付检查"')
    assert normalize_pos < english_pos
    assert english_pos < final_check_pos
    assert "修改英文格式" not in tool_tab_body
    assert "中英间加空格" not in tool_tab_body


def test_chinese_number_conversion_uses_direction_dialog():
    assert 'ID = "BtnStep2", Text = "中文数字互转"' in SOURCE
    click_body = body_between(
        "function win.On.BtnStep2.Clicked",
        "-- 3️⃣ 规整字幕长度",
    )
    dialog_body = body_between(
        "function show_chinese_number_conversion_direction_dialog",
        "function win.On.BtnStep2.Clicked",
    )
    run_body = body_between(
        "function run_chinese_number_conversion",
        "function show_chinese_number_conversion_direction_dialog",
    )

    assert "show_chinese_number_conversion_direction_dialog(win)" in click_body
    assert 'WindowTitle = "中文数字互转"' in dialog_body
    assert 'Text = "中文数字 → 123"' in dialog_body
    assert 'Text = "123 → 中文数字"' in dialog_body
    assert 'Text = "取消"' in dialog_body
    assert 'run_chinese_number_conversion("to_arabic", win)' in dialog_body
    assert 'run_chinese_number_conversion("to_chinese", win)' in dialog_body
    assert 'chosen_direction = "to_arabic"' in run_body
    assert "function is_ascii_letter" in run_body
    assert "function is_ascii_digit_char" in run_body
    assert "function is_ascii_letter_space_number_context" in run_body
    assert "function is_protected_spec_unit" in run_body
    assert "function is_arabic_decimal_token" in run_body
    assert "function is_time_ratio_or_fraction_token" in run_body
    assert "function arabic_number_should_be_protected" in run_body
    assert "is_ascii_letter(chars[start_idx - 1])" in run_body
    assert "is_ascii_letter(chars[end_idx + 1])" in run_body
    assert "is_ascii_letter_space_number_context(chars, start_idx)" in run_body
    assert "is_protected_spec_unit(chars, end_idx + 1)" in run_body
    assert "is_arabic_decimal_token(chars, start_idx, end_idx)" in run_body
    assert "is_time_ratio_or_fraction_token(chars, start_idx, end_idx)" in run_body
    assert "arabic_number_should_be_protected(chars, i, j - 1)" in run_body
    assert 'chars[j] == "." or chars[j] == "点"' in run_body
    assert "function chinese_decimal_to_arabic" in run_body
    assert "function is_fuzzy_chinese_number" in run_body
    assert "chinese_decimal_to_arabic(token)" in run_body
    assert "is_fuzzy_chinese_number(token)" in run_body
    assert "跳过保护项" not in run_body
    assert 'win.NumToggleState = (chosen_direction == "to_arabic") and "to_chinese" or "to_arabic"' not in run_body
    assert "下次将执行" not in run_body


def test_preview_trees_support_click_diagnostics_and_double_click_jump():
    mini_tree_decl = re.search(
        r'ID = "MiniSubtitleTree".{0,600}?Events = \{([^}]*)\}',
        SOURCE,
        re.S,
    )
    full_tree_decl = re.search(
        r'ID = "SubtitleTree".{0,600}?Events = \{([^}]*)\}',
        SOURCE,
        re.S,
    )

    assert mini_tree_decl
    assert full_tree_decl
    assert "ItemDoubleClicked = true" in mini_tree_decl.group(1)
    assert "ItemClicked = true" in mini_tree_decl.group(1)
    assert "ItemRightClicked = true" not in mini_tree_decl.group(1)
    assert "ItemDoubleClicked = true" in full_tree_decl.group(1)
    assert "ItemClicked = true" in full_tree_decl.group(1)
    assert "ItemRightClicked = true" not in full_tree_decl.group(1)
    assert 'ID = "MiniPreviewEditPanel"' not in SOURCE
    assert 'ID = "PreviewEditPanel"' not in SOURCE
    assert 'ID = "MiniPreviewEditBtn"' not in SOURCE
    assert 'ID = "PreviewEditBtn"' not in SOURCE


def test_preview_tree_edit_column_opens_dialog_without_jump():
    mini_edit_body = body_between(
        "function mini_win.On.MiniSubtitleTree.ItemClicked",
        "function mini_win.On.MiniSubtitleTree.ItemDoubleClicked",
    )
    full_edit_body = body_between(
        "function win.On.SubtitleTree.ItemClicked",
        "-- 双击字幕条目跳转",
    )

    assert "is_preview_tree_edit_column_event(ev)" in mini_edit_body
    assert "open_preview_edit_dialog(mini_win, ev, row)" in mini_edit_body
    assert "is_preview_tree_edit_column_event(ev)" in full_edit_body
    assert "open_preview_edit_dialog(win, ev, row)" in full_edit_body
    assert "go_to_subtitle" not in mini_edit_body
    assert "go_to_subtitle" not in full_edit_body


def test_preview_tree_uses_edit_column_layout():
    assert "apply_preview_tree_layout" in SOURCE
    assert "set_preview_tree_node_display_text" in SOURCE
    assert "tree.ColumnCount = 2" in SOURCE
    assert "tree.ColumnWidth[0] = 30" in SOURCE
    assert "tree.RootIsDecorated = false" in SOURCE
    assert "tree.Indentation = 0" in SOURCE
    assert 'set_tree_item_text(node, 0, "  ✎")' in SOURCE
    assert "set_tree_item_text(node, 1, safe_display_text)" in SOURCE


def test_preview_tree_has_per_window_item_maps_for_full_window_warmup():
    render_body = body_between(
        "render_rows_to_window = function",
        "-- 全局函数（避免主 chunk local 数量逼近 Lua 5.1 的 200 上限）",
    )
    selection_body = body_between(
        "local function get_row_from_tree_selection",
        "render_rows_to_window = function",
    )
    click_body = body_between(
        "function handle_preview_tree_item_clicked",
        "function open_preview_edit_dialog",
    )
    full_open_body = body_between(
        "local function open_full_window",
        "mini_win.On[WINDOW_META.mini_window_id].Close",
    )

    assert "subtitle_data_maps_by_window" in SOURCE
    assert "subtitle_row_id_node_maps_by_window" in SOURCE
    assert "get_subtitle_data_map_for_window" in SOURCE
    assert "get_subtitle_row_id_node_map_for_window" in SOURCE
    assert "set_preview_tree_maps_for_window(window, next_map, next_row_id_map)" in render_body
    assert "get_subtitle_data_map_for_window(target_window)" in selection_body
    assert "get_subtitle_data_map_for_window(window)" in click_body
    assert "activate_preview_tree_maps_for_window(win)" in full_open_body


def test_startup_defers_full_window_creation_and_tree_warmup_until_requested():
    startup_body = body_between(
        "-- ========== 启动 ==========" ,
        "-- 必须进入事件循环",
    )
    full_open_body = body_between(
        "local function open_full_window",
        "mini_win.On[WINDOW_META.mini_window_id].Close",
    )
    assert "mini_win = create_mini_window()" in SOURCE
    assert SOURCE.index("mini_win = create_mini_window()") < SOURCE.index("-- ========== 启动 ==========")
    assert "win = create_full_window()" not in startup_body
    assert "ensure_full_window_initialized()" in full_open_body
    assert "win = create_full_window()" in SOURCE
    assert "full_window_warmup_timer" not in SOURCE
    assert "if is_mini_window(window) and win then" not in SOURCE
    assert "if current_window and is_mini_window(current_window) and win then" not in SOURCE
    assert "if is_mini_window(window) then\n        full_window_tree_dirty = true" in SOURCE
    assert "if current_window and is_mini_window(current_window) then\n        full_window_tree_dirty = true" in SOURCE


def test_preview_tree_double_click_uses_event_row_for_jump():
    mini_double_body = body_between(
        "function mini_win.On.MiniSubtitleTree.ItemDoubleClicked",
        "-- 轨道选择变化",
    )
    full_double_body = body_between(
        "function win.On.SubtitleTree.ItemDoubleClicked",
        "handle_main_window_close",
    )

    assert "handle_preview_tree_item_clicked(mini_win, ev)" in mini_double_body
    assert "go_to_subtitle(mini_win, row)" in mini_double_body
    assert "handle_preview_tree_item_clicked(win, ev)" in full_double_body
    assert "go_to_subtitle(win, row)" in full_double_body


def test_preview_editor_save_only_updates_memory_not_timeline():
    save_body = body_between(
        "function save_preview_edit_dialog_changes",
        "function handle_preview_tree_item_clicked",
    )

    assert "prepare_mutation_snapshot" in save_body
    assert "commit_mutation_snapshot" in save_body
    assert "sync_current_preview_tree" in save_body
    assert "update_timeline" not in save_body
    assert "SetCurrentTimecode" not in save_body
    assert "go_to_subtitle" not in save_body


def test_preview_edit_dialog_uses_compact_size():
    dialog_body = body_between(
        'ID = "PreviewEditDialog"',
        "function edit_win.On.PreviewEditDialog.Close",
    )

    assert "Geometry = SUBFIX_WINDOW_GEOMETRY.centered_geometry({460, 220, 360, 170})" in dialog_body
    assert "MinimumSize = {0, 70}" in dialog_body


def test_preview_edit_dialog_reads_textedit_content_on_save():
    dialog_body = body_between(
        "function open_preview_edit_dialog",
        "function build_snapshot_record",
    )

    assert "set_textedit_content(editor, tostring(row.text or \"\"))" in dialog_body
    assert "get_textedit_content(editor)" in dialog_body
    assert "editor and editor.Text or \"\"" not in dialog_body


def test_preview_edit_dialog_prevents_overlapping_windows_and_releases_on_every_exit():
    dialog_body = body_between(
        "function open_preview_edit_dialog",
        "function build_snapshot_record",
    )

    assert "preview_edit_window" in SOURCE
    assert "if preview_edit_window then" in dialog_body
    assert "return false" in dialog_body
    assert "local function close_preview_edit_dialog()" in dialog_body
    assert "preview_edit_window = nil" in dialog_body
    assert "close_preview_edit_dialog()" in dialog_body


def test_gap_fill_button_shows_status_before_running_work():
    step3_body = body_between(
        "function win.On.BtnStep3.Clicked",
        "-- 1️⃣ 修改英文排版",
    )
    start_body = body_between(
        "function NormalizeLengthConfigWin.On.NormalizeLengthStartBtn.Clicked",
        "function NormalizeLengthConfigWin.On.NormalizeLengthCancelBtn.Clicked",
    )
    timer_body = body_between(
        "register_ui_timer(normalize_length_timer",
        "function win.On.TrackSpin.TextChanged",
    )

    assert "show_normalize_length_config_dialog(win)" in step3_body
    assert "update_shared_status(target, \"正在规整字幕长度...\")" in start_body
    assert "restart_ui_timer(normalize_length_timer)" in start_body
    assert "run_normalize_subtitle_length(target_window, options)" in timer_body


def test_normalize_length_opens_config_dialog_and_passes_bias_frames():
    step3_body = body_between(
        "function win.On.BtnStep3.Clicked",
        "-- 1️⃣ 修改英文排版",
    )
    start_body = body_between(
        "function NormalizeLengthConfigWin.On.NormalizeLengthStartBtn.Clicked",
        "function NormalizeLengthConfigWin.On.NormalizeLengthCancelBtn.Clicked",
    )
    normalize_body = body_between(
        "function run_normalize_subtitle_length",
        "-- 1️⃣ 修改英文排版",
    )

    assert 'WindowTitle = "规整字幕长度配置"' in SOURCE
    assert "Geometry = SUBFIX_WINDOW_GEOMETRY.centered_geometry({360, 240, 360, 100})" in SOURCE
    assert 'ID = "NormalizeLengthAlignAudioCheckbox", Text = "字幕音频对齐", Checked = true' in SOURCE
    assert 'ID = "NormalizeLengthFillGapsCheckbox", Text = "消除字幕空隙", Checked = true' in SOURCE
    assert 'ID = "NormalizeLengthBiasInput"' not in SOURCE
    assert 'ID = "NormalizeLengthStartModeCombo"' not in SOURCE
    assert "start_mode_combo" not in SOURCE
    assert 'ID = "NormalizeLengthSelectionHint"' not in SOURCE
    assert 'Text = "字幕对齐音频偏移"' not in SOURCE
    assert "Spacing = 8" in body_between(
        'ID = "NormalizeLengthConfigWin"',
        "function NormalizeLengthConfigWin.On.NormalizeLengthConfigWin.Close",
    )
    assert "MinimumSize = {0, 28}" in body_between(
        'ID = "NormalizeLengthConfigWin"',
        "function NormalizeLengthConfigWin.On.NormalizeLengthConfigWin.Close",
    )
    assert 'ui:HGap(0, 1)' not in body_between(
        'ID = "NormalizeLengthConfigWin"',
        "function NormalizeLengthConfigWin.On.NormalizeLengthConfigWin.Close",
    )
    assert "SUBFIX_AUDIO_ALIGN.parse_normalize_length_bias_input" in SOURCE
    assert "show_normalize_length_config_dialog(win)" in step3_body
    assert "SUBFIX_AUDIO_ALIGN.parse_normalize_length_bias_input" not in start_body
    assert "pending_normalize_length_options" in start_body
    assert 'bias_mode = "manual"' in start_body
    assert 'start_mode = "balanced"' in start_body
    assert "bias_mode = options.bias_mode" in normalize_body
    assert 'local normalize_start_mode = tostring(options.start_mode or "balanced")' in normalize_body
    assert 'if align_audio then' in normalize_body
    assert 'if fill_gaps then' in normalize_body
    assert "start_mode = normalize_start_mode" in normalize_body
    assert "apply_protected_audio_alignment_for_gap_fill(current_rows, fps, {progress = normalize_progress, bias_frames = normalize_bias_frames, bias_mode = options.bias_mode, start_mode = normalize_start_mode})" in normalize_body
    assert "NormalizeLengthConfigWin = nil" in SOURCE


def test_english_typography_dialog_contains_required_options_and_defaults():
    dialog_body = body_between(
        "function show_english_typography_config_dialog",
        "function win.On.BtnStep1.Clicked",
    )

    assert 'WindowTitle = "修改英文排版"' in dialog_body
    assert "Geometry = SUBFIX_WINDOW_GEOMETRY.centered_geometry({390, 260, 380, 130})" in dialog_body
    assert 'ui:ComboBox{ID = "EnglishTypographyCaseMode"' in dialog_body
    assert 'case_combo:AddItem("不改大小写")' in dialog_body
    assert 'case_combo:AddItem("全部大写（MACBOOK）")' in dialog_body
    assert 'case_combo:AddItem("全部小写（macbook）")' in dialog_body
    assert 'case_combo:AddItem("首字母大写（Macbook）")' in dialog_body
    assert 'ui:CheckBox{ID = "EnglishTypographySpacingCheckbox", Text = "中英/数字之间加空格"' in dialog_body
    assert 'Text = "开始处理"' in dialog_body
    assert 'Text = "取消"' in dialog_body
    assert 'ui:VGap(2)' in dialog_body
    assert '''ui:HGroup{
                Weight = 0,
                Spacing = 6,
                ui:Label{Text = "大小写：", Weight = 0, MinimumSize = {0, 26}},
                ui:ComboBox{ID = "EnglishTypographyCaseMode", Weight = 1, MinimumSize = {0, 26}}
            }''' in dialog_body
    assert '''ui:HGroup{
                Weight = 0,
                Spacing = 6,
                MinimumSize = {0, 26},
                ui:Label{Text = "间距：", Weight = 0, MinimumSize = {0, 26}},
                ui:CheckBox{ID = "EnglishTypographySpacingCheckbox", Text = "中英/数字之间加空格", Checked = true, Weight = 0}
            }''' in dialog_body
    assert 'ui:HGap(0, 1)' not in dialog_body
    assert 'ID = "EnglishTypographyCancelBtn", Text = "取消", Weight = 1, MinimumSize = {0, 28}' in dialog_body
    assert 'ID = "EnglishTypographyStartBtn", Text = "开始处理", Weight = 1, MinimumSize = {0, 28}' in dialog_body
    assert "MinimumSize = {0, 28}" in dialog_body
    assert "case_combo.CurrentIndex = 0" in dialog_body
    assert 'set_checkbox_checked(spacing_checkbox, true)' in dialog_body


def test_chinese_number_dialog_uses_emphasized_lower_hint_and_balanced_actions():
    dialog_body = body_between(
        "function show_chinese_number_conversion_direction_dialog",
        "function win.On.BtnStep2.Clicked",
    )

    assert "ContentsMargins = 10" in dialog_body
    assert "ui:VGap(2)" in dialog_body
    assert 'Text = "选择转换方向"' in dialog_body
    assert "MinimumSize = {0, 28}" in dialog_body
    assert "Font = ui:Font{PixelSize = 18}" in dialog_body
    assert '''ui:HGroup {
                Weight = 0,
                Spacing = 8,
                MinimumSize = {0, 28},''' in dialog_body
    assert 'Weight = 1, MinimumSize = {0, 28}' in dialog_body


def test_sync_blocks_deploy_when_confirmed_dialog_layouts_regress():
    assert "validate_confirmed_dialog_layouts()" in SYNC_SCRIPT
    assert 'Geometry = SUBFIX_WINDOW_GEOMETRY.centered_geometry({360, 240, 360, 100})' in SYNC_SCRIPT
    assert 'ID = "NormalizeLengthAlignAudioCheckbox"' in SYNC_SCRIPT
    assert 'ID = "NormalizeLengthFillGapsCheckbox"' in SYNC_SCRIPT
    assert 'Geometry = SUBFIX_WINDOW_GEOMETRY.centered_geometry({390, 260, 380, 130})' in SYNC_SCRIPT
    assert 'Geometry = SUBFIX_WINDOW_GEOMETRY.centered_geometry({420, 320, 300, 130})' in SYNC_SCRIPT
    assert "validate_confirmed_dialog_layouts" in SYNC_SCRIPT.split("# 创建目标目录", 1)[0]


def test_english_typography_combination_branches_are_independent():
    transform_body = body_between(
        "function apply_english_typography_to_text",
        "function run_english_typography",
    )
    run_body = body_between(
        "function run_english_typography",
        "function show_english_typography_config_dialog",
    )

    assert 'if case_mode and case_mode ~= "none" then' in transform_body
    assert "after_case = apply_english_case_text(original_text, case_mode)" in transform_body
    assert "if add_spacing == true then" in transform_body
    assert "final_text = add_chinese_english_number_spacing(after_case)" in transform_body
    assert 'case_changed = case_mode and case_mode ~= "none" and after_case ~= original_text' in transform_body
    assert "spacing_changed = add_spacing == true and final_text ~= after_case" in transform_body
    assert 'if (not case_mode or case_mode == "none") and add_spacing ~= true then' in run_body
    assert 'update_shared_status(window, "未选择任何处理项")' in run_body
    assert 'prepare_mutation_snapshot("修改英文排版")' in run_body


def test_english_typography_uses_single_snapshot_and_final_report_entries():
    run_body = body_between(
        "function run_english_typography",
        "function show_english_typography_config_dialog",
    )

    assert run_body.count('prepare_mutation_snapshot("修改英文排版")') == 1
    assert run_body.count("commit_mutation_snapshot(mutation_snapshot)") == 1
    assert run_body.count("format_batch_change_report_line(") == 1
    assert '"大小写修改数量：%d\\n加空格修改数量：%d\\n总修改数量：%d"' in run_body
    assert 'show_batch_result_report("修改英文排版", report_entries, total_count, {summary_text = summary_text})' in run_body
    assert "refresh_text_batch_preview_after_mutation(window, dirty_row_ids)" in run_body
    assert "force_rebuild = true" not in run_body


def test_translation_batches_and_retries_missing_lines():
    ai_body = body_between(
        "local function do_ai_fix()",
        "-- ========== 导出 SRT ==========",
    )
    translation_body = ai_body[
        ai_body.index("    elseif is_particle_correction_task then") : ai_body.index(
            '    print("[Hooper AI 2.0] 自动应用 '
        )
    ]

    assert "execute_ai_request(subtitle_list, task_name)" not in translation_body
    assert "local translation_batch_size = 20" in translation_body
    assert "translation_missing_indices" in translation_body
    assert "translation_retry_list = build_subtitle_list(missing_idx, missing_idx)" in translation_body
    assert "parse_ai_line_payload(retry_ai_content, 1, missing_idx, false)" in translation_body


def test_ai_tasks_use_shared_long_task_progress_window():
    ai_body = body_between(
        "local function do_ai_fix()",
        "-- ========== 导出 SRT ==========",
    )
    progress_body = body_between(
        "function show_long_task_progress_window",
        "function update_long_task_progress_window",
    )
    finish_body = body_between(
        "function finish_long_task_progress_window",
        "function normalize_progress_elapsed_text",
    )

    assert "function show_long_task_progress_window" in SOURCE
    assert "function update_long_task_progress_window" in SOURCE
    assert "function finish_long_task_progress_window" in SOURCE
    assert "local LONG_TASK_PROGRESS_BAR_WIDTH = 36" in SOURCE
    assert 'ID = "LongTaskProgressWindow"' in progress_body
    assert 'ID = "LongTaskProgressStatusLabel"' in progress_body
    assert 'ID = "LongTaskProgressBarLabel"' in progress_body
    assert 'ID = "LongTaskProgressMetaLabel"' in progress_body
    assert 'ID = "LongTaskProgressCancelBtn"' in progress_body
    assert 'ID = "LongTaskProgressCloseBtn"' not in progress_body
    assert 'Text = "ᗧ" .. string.rep("□", LONG_TASK_PROGRESS_BAR_WIDTH - 1) .. "⚑"' in progress_body
    assert "Geometry = SUBFIX_WINDOW_GEOMETRY.centered_geometry({520, 380, 430, 200})" in progress_body
    assert "ui:TextEdit" not in progress_body
    assert "progress_state.on_cancel" in progress_body
    assert 'if status == "done" then' in finish_body
    assert "progress_window:Hide()" in finish_body
    assert 'items.LongTaskProgressCancelBtn.Text = "关闭"' in finish_body
    assert finish_body.index('if status == "done" then') < finish_body.index('items.LongTaskProgressCancelBtn.Text = "关闭"')

    assert "local ai_progress = start_ai_progress(task_name, #sorted_list)" in ai_body
    assert "update_ai_progress(" in ai_body
    assert "finish_ai_progress(ai_progress, \"cancelled\"" in ai_body
    assert "finish_ai_progress(ai_progress, \"failed\"" in ai_body
    assert "finish_ai_progress(ai_progress, \"done\"" in ai_body
    assert "AI_CANCEL_REQUESTED = true" in ai_body
    assert "kill_ai_curl_process()" in ai_body
    assert "重试漏回字幕 %d/%d" in ai_body
    assert "boundary_fix_batch_" in ai_body
    assert "progress_index = batch_idx * 2" in ai_body
    assert "progress_total = total_batches * 2" in ai_body


def test_text_batch_preview_refresh_avoids_full_rebuild_without_search():
    helper_body = body_between(
        "function refresh_text_batch_preview_after_mutation",
        "function apply_english_case_text",
    )

    assert "sync_current_preview_tree(window, dirty_row_ids)" in helper_body
    assert "current_search_query" in helper_body
    assert "SEARCH_VIEW.render_current_view(window)" in helper_body
    assert "force_rebuild" not in helper_body


def test_pre_delivery_final_check_marks_quality_issues_without_mutating_subtitles():
    final_check_body = body_between(
        "function run_pre_delivery_final_check",
        "-- [6] 敏感词替换",
    )
    summary_body = body_between(
        "function pre_delivery_issue_summary_text",
        "function timeline_has_pre_delivery_marker",
    )

    assert "collect_pre_delivery_final_check_issues(current_rows" in final_check_body
    assert "timeline:GetStartFrame()" in final_check_body
    assert "add_pre_delivery_check_marker(timeline, issue, tl_start_frame)" in final_check_body
    assert "最终交付检查完成" in final_check_body
    assert "marker_failed_count" in final_check_body
    assert "marker_existing_count" not in final_check_body
    assert "空字幕" in SOURCE
    assert "阅读速度过快" in SOURCE
    assert "字幕显示过久" in SOURCE
    assert "单条字幕过长" in SOURCE
    assert "字幕尾部空格" in SOURCE
    assert "字幕边界未贴剪辑点" in SOURCE
    assert "字幕间异常短缝" in SOURCE
    assert "相邻重复字幕" in SOURCE
    assert "show_batch_result_report" not in final_check_body
    assert "prepare_mutation_snapshot" not in final_check_body
    assert "commit_mutation_snapshot" not in final_check_body
    assert "collect_pre_delivery_speech_consistency_issues(current_rows, timeline, fps, {window = window})" not in final_check_body
    assert 'update_shared_status(window, "正在最终交付检查：正在读取时间线...")' in final_check_body
    assert final_check_body.index('update_shared_status(window, "正在最终交付检查：正在读取时间线...")') < final_check_body.index("project:GetCurrentTimeline()")
    assert "正在最终交付检查：正在检查字幕与口播一致性" not in final_check_body
    assert "speech_summary" not in final_check_body
    assert "speech_backend" in SOURCE
    assert "speech_model" in SOURCE
    assert "口播字幕不一致" in SOURCE
    assert "口播缺字幕" in SOURCE
    assert "字幕无对应口播音频" in SOURCE
    assert "口播字幕不一致" not in summary_body
    assert "口播字幕疑似字词替换" not in summary_body
    assert "口播字幕小差异" not in summary_body
    assert "口播缺字幕" not in summary_body
    assert "字幕无对应口播音频" not in summary_body


def test_pre_delivery_speech_consistency_uses_transcribe_backend_without_replacing_alignment():
    run_asr_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.run_asr_alignment",
        "function SUBFIX_AUDIO_ALIGN.alignment_rows_temp_path",
    )
    final_check_body = body_between(
        "function run_pre_delivery_final_check",
        "-- [6] 敏感词替换",
    )
    protected_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.apply_protected_audio_alignment_for_gap_fill",
        "function SUBFIX_AUDIO_ALIGN.apply_lightweight_audio_alignment_for_gap_fill",
    )

    assert '"--mode", "transcribe"' in run_asr_body
    assert '"--backend", shell_quote(SUBFIX_AUDIO_ALIGN.default_transcribe_backend)' in run_asr_body
    assert 'SUBFIX_AUDIO_ALIGN.default_transcribe_backend = "auto"' in SOURCE
    assert "ASR backend:" in run_asr_body
    assert "collect_pre_delivery_speech_consistency_issues" not in final_check_body
    assert "SUBFIX_AUDIO_ALIGN.run_asr_review_windows(review_windows, fps, {" in SOURCE
    assert "run_local_speech_review_asr(review_window.audio_source, fps, {" in SOURCE
    assert "build_speech_consistency_review_windows(batch.rows or {}, batch.audio_source, rate)" in SOURCE
    assert "join_speech_check_rows_text(window_rows)" in SOURCE
    assert "select_best_speech_review_result(row, review_results_by_row[row])" in SOURCE
    assert "local_speech_text" in SOURCE
    assert "SUBFIX_AUDIO_ALIGN.text_score(subtitle_group_text, asr_segment.text)" not in SOURCE
    assert "match_rows_to_asr_segments(batch.rows or {}, reference_info.rows or {}" not in SOURCE
    assert "局部口播：" in SOURCE
    assert "speech_summary.failed_batch_count" not in final_check_body
    assert "speech_summary.cancelled" not in final_check_body
    assert "speech_summary.skipped_batch_count" not in final_check_body
    assert 'update_shared_status(window, "最终交付检查已取消")' not in final_check_body
    assert "口播一致性失败" not in final_check_body
    assert "speech_failure_reason" not in final_check_body
    assert "PRE_DELIVERY_SPEECH_CTC_DIAGNOSTIC_ENABLED = false" in SOURCE
    assert "PRE_DELIVERY_SPEECH_SKIP_SINGLE_TRACK_PROBE = true" in SOURCE
    assert "run_qwen_forced_alignment_batches" in protected_body
    assert "PRE_DELIVERY_SPEECH_CTC_MIN_CONFIDENCE = 0.35" in SOURCE
    assert "run_qwen_forced_alignment_batches" in SOURCE
    assert "run_asr_alignment" not in protected_body


def test_pre_delivery_speech_consistency_uses_local_asr_review_as_primary_signal():
    speech_body = body_between(
        "function collect_pre_delivery_speech_consistency_issues",
        "function format_pre_delivery_issue_report_entries",
    )

    assert "function build_speech_consistency_review_windows(rows, audio_source, fps)" in SOURCE
    assert "function run_local_speech_review_asr(audio_source, fps, options)" in SOURCE
    assert "function select_best_speech_review_result(row, review_results)" in SOURCE
    assert "SUBFIX_AUDIO_ALIGN.review_audio_source_for_window(audio_source, window_start_frame, window_end_frame, fps)" in SOURCE
    assert 'SUBFIX_AUDIO_ALIGN.default_asr_model = "large-v3-turbo"' in SOURCE
    assert "PRE_DELIVERY_SPEECH_TIMELINE_EXPORT_PADDING_SECONDS = 1.0" in SOURCE
    assert "PRE_DELIVERY_SPEECH_LOCAL_PADDING_SECONDS = 0.0" in SOURCE
    assert "PRE_DELIVERY_SPEECH_MERGE_MAX_GAP_SECONDS = 0.25" in SOURCE
    assert "PRE_DELIVERY_SPEECH_MERGE_MAX_CHARS = 36" in SOURCE
    assert 'append_window({row, next_row}, "merged_pair")' in SOURCE
    assert 'append_window({row}, "single_row")' in SOURCE
    assert 'string.format("口播一致性｜局部复核 %d/%d｜%s｜字幕 %s｜用时 %s"' in speech_body
    assert "SUBFIX_AUDIO_ALIGN.run_asr_review_windows(review_windows, fps, {" in speech_body
    assert "run_local_speech_review_asr(review_window.audio_source, fps, {" in speech_body
    assert "local_speech_text = speech_review_payload_text(review_info)" in speech_body
    assert "speech_text_consistency_failed(subtitle_group_text, local_speech_text, min_score, review_window.context_text)" in speech_body
    assert "speech_digit_mismatch(subtitle_group_text, local_speech_text)" in speech_body


def test_pre_delivery_speech_markers_are_placed_inside_subtitle_rows():
    speech_body = body_between(
        "function collect_pre_delivery_speech_consistency_issues",
        "function format_pre_delivery_issue_report_entries",
    )
    window_body = body_between(
        "function build_speech_consistency_review_windows",
        "function speech_review_payload_text",
    )
    midpoint_body = body_between(
        "function subtitle_row_middle_frame",
        "function split_raw_text_chars",
    )

    assert "function subtitle_row_middle_frame(row, fallback_frame)" in SOURCE
    assert "return math.floor(((start_frame + end_frame) / 2) + 0.5)" in midpoint_body
    assert "marker_frame = subtitle_row_middle_frame(first_row, review_audio_source.start_frame)" in window_body
    assert 'add_issue(row, "字幕无对应口播音频", "字幕未落在主讲音频片段内", nil, 0, subtitle_row_middle_frame(row, row.start_frame))' in speech_body
    assert "subtitle_row_middle_frame(first_row, best_review.marker_frame)" in speech_body


def test_pre_delivery_local_asr_review_window_uses_exact_subtitle_time_range():
    window_body = body_between(
        "function build_speech_consistency_review_windows",
        "function speech_review_payload_text",
    )
    render_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.render_timeline_audio_mix_for_speech_check",
        "function SUBFIX_AUDIO_ALIGN.map_speech_segments_to_timeline",
    )

    assert "speech_rows_frame_window(window_rows)" in window_body
    assert "review_audio_source_for_window(audio_source, window_start_frame, window_end_frame, fps)" in window_body
    assert "window_start_frame = window_start_frame -" not in window_body
    assert "window_end_frame = window_end_frame +" not in window_body
    assert "PRE_DELIVERY_SPEECH_LOCAL_PADDING_SECONDS" not in window_body
    assert "PRE_DELIVERY_SPEECH_TIMELINE_EXPORT_PADDING_SECONDS" in render_body


def test_pre_delivery_speech_consistency_never_executes_removed_ctc_diagnostic():
    speech_body = body_between(
        "function collect_pre_delivery_speech_consistency_issues",
        "function format_pre_delivery_issue_report_entries",
    )
    assert "if PRE_DELIVERY_SPEECH_CTC_DIAGNOSTIC_ENABLED == true then" in speech_body
    assert "CTC 诊断已移除，不再执行旧对齐链路" in speech_body
    assert "run_ctc_text_alignment_batches" not in speech_body
    assert "collect_pre_delivery_ctc_consistency_issues" not in speech_body


def test_pre_delivery_local_asr_merge_window_prevents_split_sentence_false_positive():
    speech_body = body_between(
        "function collect_pre_delivery_speech_consistency_issues",
        "function format_pre_delivery_issue_report_entries",
    )
    select_body = body_between(
        "function select_best_speech_review_result",
        "function collect_pre_delivery_ctc_consistency_issues",
    )

    assert "select_best_speech_review_result(row, review_results_by_row[row])" in speech_body
    assert "single_row_review" in select_body
    assert "function speech_review_failure_requires_marker(review, review_results)" in SOURCE
    assert "function speech_review_has_confirming_small_difference(review, review_results)" in SOURCE
    assert "function speech_review_find_single_row_result(review_results)" in SOURCE
    assert "speech_review_failure_requires_marker(single_row_review, review_results)" in select_body
    assert "if single_row_review and single_row_review.passed == true then" in select_body
    assert select_body.index("speech_review_failure_requires_marker(single_row_review, review_results)") < select_body.index("if single_row_review and single_row_review.passed == true then")
    assert select_body.index("if single_row_review and single_row_review.passed == true then") < select_body.index("if passed_merged_pair_review then")
    assert select_body.index("if passed_merged_pair_review then") < select_body.index("if single_row_review and not single_row_review.skipped then")
    assert 'fail_reason == "extra_tail"' in SOURCE
    assert 'fail_reason == "asr_coverage"' in SOURCE
    assert 'fail_reason == "small_diff"' in SOURCE
    assert "speech_review_has_confirming_small_difference(review, review_results)" in SOURCE
    assert "review.digit_mismatch == true" in SOURCE
    assert 'return false, "单行已通过"' in SOURCE
    assert 'return false, "单行已确认不一致"' in SOURCE
    assert "return single_row_review" in select_body
    assert "merged_pair" in select_body
    assert "single_row" in SOURCE
    assert "merged_pair" in SOURCE
    assert "那第一次看到这个界面" in SOURCE
    assert "你可能会愣一下" in SOURCE
    assert "那第一次看到这个界面你可能会愣一下" in SOURCE
    assert "保护自己" in SOURCE
    assert "保护自己的" in SOURCE


def test_pre_delivery_local_asr_digit_mismatch_is_checked_from_local_window_text():
    speech_body = body_between(
        "function collect_pre_delivery_speech_consistency_issues",
        "function format_pre_delivery_issue_report_entries",
    )

    assert "speech_digit_mismatch(subtitle_group_text, local_speech_text)" in speech_body
    assert "字幕数字与局部口播数字不一致" in speech_body
    assert "2026" in SOURCE
    assert "二零一六" in SOURCE


def test_pre_delivery_local_asr_detects_subtitle_missing_asr_tail_text():
    speech_body = body_between(
        "function collect_pre_delivery_speech_consistency_issues",
        "function format_pre_delivery_issue_report_entries",
    )
    consistency_body = body_between(
        "function speech_text_consistency_failed",
        "function speech_check_effective_char_count",
    )

    assert "PRE_DELIVERY_SPEECH_EXTRA_TAIL_MIN_CHARS = 1" in SOURCE
    assert "function speech_asr_has_extra_tail_after_subtitle(subtitle_text, asr_text, context_text)" in SOURCE
    assert "speech_asr_has_extra_tail_after_subtitle(subtitle_text, asr_text, context_text)" in consistency_body
    assert 'return true, score, coverage, "extra_tail"' in consistency_body
    assert 'elseif fail_reason == "extra_tail" then' in speech_body
    assert "review_window.context_text" in speech_body
    assert "function collect_speech_context_rows_for_window(rows, window_start_frame, window_end_frame)" in SOURCE
    assert "context_text = join_speech_check_rows_text" in SOURCE
    assert "字幕缺少局部口播后续内容" in speech_body
    assert "保护自己" in SOURCE
    assert "保护自己的" in SOURCE
    assert "来减少对应的" in SOURCE
    assert "攻击性" in SOURCE


def test_pre_delivery_local_asr_skips_known_whisper_hallucination_credit_text_as_unreliable_not_mismatch():
    speech_body = body_between(
        "function collect_pre_delivery_speech_consistency_issues",
        "function format_pre_delivery_issue_report_entries",
    )
    hallucination_body = body_between(
        "function speech_local_asr_hallucination_reason",
        "function run_local_speech_review_asr",
    )

    assert "function speech_local_asr_hallucination_reason(asr_text, subtitle_text)" in SOURCE
    assert "字幕製作" in hallucination_body
    assert "字幕制作" in hallucination_body
    assert "zither%s*harp" in hallucination_body
    assert "点赞" in hallucination_body
    assert "订阅" in hallucination_body
    assert "打赏" in hallucination_body
    assert "明镜" in hallucination_body
    assert "speech_local_asr_hallucination_reason(local_speech_text, subtitle_group_text)" in speech_body
    assert "review_result.reason = hallucination_reason" in speech_body
    assert "review_result.skipped = true" in speech_body
    assert "review_result.unreliable = true" in speech_body
    assert "elseif best_review and best_review.unreliable == true then" not in speech_body
    assert "疑似 Whisper 幻觉字幕署名" in SOURCE


def test_pre_delivery_local_asr_skips_short_filler_as_unreliable_not_mismatch():
    speech_body = body_between(
        "function collect_pre_delivery_speech_consistency_issues",
        "function format_pre_delivery_issue_report_entries",
    )
    hallucination_body = body_between(
        "function speech_local_asr_hallucination_reason",
        "function run_local_speech_review_asr",
    )

    assert "speech_local_asr_filler_only(raw_text)" in hallucination_body
    assert "嗯" in hallucination_body
    assert "呃" in hallucination_body
    assert "啊" in hallucination_body
    assert "局部 ASR 只有语气词" in SOURCE
    assert "review_result.skipped = true" in speech_body
    assert "review_result.unreliable = true" in speech_body


def test_pre_delivery_audio_track_probe_requires_text_similarity_not_any_asr_output():
    probe_body = body_between(
        "function probe_speech_consistency_audio_track_candidate",
        "function score_speech_consistency_audio_track_candidate",
    )
    find_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.find_speech_consistency_audio_track_batches",
        "function collect_pre_delivery_ctc_consistency_issues",
    )

    assert "speech_text_consistency_failed(review_window.text, probe_text, min_probe_score)" in probe_body
    assert "probe_score_total" in probe_body
    assert "probe_coverage_total" in probe_body
    assert "track_info.speech_probe_score" in probe_body
    assert "track_info.speech_probe_coverage" in probe_body
    assert "reliable_count = reliable_count + 1" in probe_body
    assert "track_info.speech_probe_score" in body_between(
        "function score_speech_consistency_audio_track_candidate",
        "function SUBFIX_AUDIO_ALIGN.find_speech_consistency_audio_track_batches",
    )
    assert "if #candidates == 1 and PRE_DELIVERY_SPEECH_SKIP_SINGLE_TRACK_PROBE == true then" in find_body
    assert "最终交付检查主讲轨唯一候选，跳过 ASR 探测" in find_body
    assert "tonumber(track_info.speech_reliable_count) or 0" in find_body
    assert "主讲轨探测未匹配字幕" in find_body


def test_pre_delivery_speech_mismatch_marker_hides_internal_diagnostics():
    speech_body = body_between(
        "function collect_pre_delivery_speech_consistency_issues",
        "function format_pre_delivery_issue_report_entries",
    )
    add_marker_body = body_between(
        "local function add_issue(row, kind, reason, reference, score, marker_frame)",
        "for _, row in ipairs(batch_plan.unassigned_rows or {}) do",
    )

    assert 'note = string.format("字幕：%s\\n局部口播：%s", subtitle_text, asr_text)' in add_marker_body
    assert "\\naudio=" not in add_marker_body
    assert "\\nwindow=" not in add_marker_body
    assert "backend/model=" not in add_marker_body
    assert "asr_coverage=%.2f" not in add_marker_body
    assert '"timeline_mix"' in speech_body
    assert "review_window.audio_source and review_window.audio_source.timeline_audio_mix == true" in speech_body
    assert "review_window.audio_source and review_window.audio_source.file_name" in speech_body
    assert "audio_label = best_review.audio_label" in speech_body


def test_pre_delivery_asr_transcribe_synthesizes_full_window_segment_from_text_only_payload():
    run_asr_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.run_asr_alignment",
        "function SUBFIX_AUDIO_ALIGN.alignment_rows_temp_path",
    )

    assert "local payload_text_only = trim_text(payload.text or \"\")" in run_asr_body
    assert "if #rows == 0 and payload_text_only ~= \"\" then" in run_asr_body
    assert "text = payload_text_only" in run_asr_body
    assert "mapping_mode = (#rows > 0) and \"local_onset_asr_assisted\" or \"empty_asr\"" in run_asr_body
    assert "empty_reason = (#rows == 0 and #speech_onsets == 0) and \"empty_asr\"" in run_asr_body


def test_pre_delivery_speech_empty_local_asr_is_diagnostic_not_mismatch_marker():
    speech_body = body_between(
        "function collect_pre_delivery_speech_consistency_issues",
        "function format_pre_delivery_issue_report_entries",
    )
    final_check_body = body_between(
        "function run_pre_delivery_final_check",
        "-- [6] 敏感词替换",
    )

    assert "skipped_batch_count = 0" in speech_body
    assert "reference_info.empty_reason == \"empty_asr\"" in speech_body
    assert "summary.skipped_batch_count = summary.skipped_batch_count + 1" in speech_body
    assert "review_result.unreliable = true" in speech_body
    assert "局部 ASR 无可用口播" in speech_body
    assert "review_result.skipped = true" in speech_body
    assert "elseif best_review and best_review.unreliable == true then" not in speech_body
    assert "口播一致性跳过 %d 批" not in final_check_body


def test_pre_delivery_speech_consistency_keeps_legacy_asr_grouping_helpers_out_of_final_mismatch_decision():
    group_body = body_between(
        "function collect_speech_consistency_groups",
        "function collect_pre_delivery_speech_consistency_issues",
    )
    speech_body = body_between(
        "function collect_pre_delivery_speech_consistency_issues",
        "function format_pre_delivery_issue_report_entries",
    )

    assert "function collect_asr_segments_overlapping_row(asr_segments, row, tolerance_frames)" in SOURCE
    assert "function collect_speech_consistency_groups(rows, asr_segments, fps)" in SOURCE
    assert "PRE_DELIVERY_SPEECH_OVERLAP_TOLERANCE_SECONDS = 0.08" in SOURCE
    assert "PRE_DELIVERY_SPEECH_MAX_GROUP_ROWS = 2" in SOURCE
    assert "local tolerance_frames = math.max(1" in group_body
    assert "local max_group_rows = math.max(1" in group_body
    assert "pending_rows" in group_body
    assert "pending_segments" in group_body
    assert "visited_rows" in group_body
    assert "visited_segments" in group_body
    assert "component_rows" in group_body
    assert "component_segments" in group_body
    assert "while row_index <= #component_rows do" in group_body
    assert "math.min(#component_rows, row_index + max_group_rows - 1)" in group_body
    assert "chunk_rows" in group_body
    assert "chunk_segments" in group_body
    assert "collect_rows_overlapping_asr_segment(row_list, segment, tolerance_frames)" in group_body
    assert "collect_asr_segments_overlapping_row(asr_list, row, tolerance_frames)" in group_body
    assert "collect_speech_consistency_groups(batch.rows or {}, reference_info.rows or {}, rate)" not in speech_body
    assert "join_speech_check_asr_text_for_rows(group_rows, group_segments, rate)" not in speech_body
    assert "speech_text_consistency_failed(subtitle_group_text, local_speech_text, min_score, review_window.context_text)" in speech_body
    assert "speech_digit_mismatch(subtitle_group_text, local_speech_text)" in speech_body
    assert "局部口播" in speech_body


def test_pre_delivery_speech_consistency_uses_local_text_score_for_long_asr_segments():
    score_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.local_text_score",
        "function SUBFIX_AUDIO_ALIGN.build_reference_rows",
    )
    normalize_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.normalize_text",
        "function SUBFIX_AUDIO_ALIGN.levenshtein_ratio",
    )

    assert "function SUBFIX_AUDIO_ALIGN.local_text_score(source_text, reference_text)" in SOURCE
    assert "longer:find(shorter, 1, true)" in score_body
    assert "best_score" in score_body
    assert "window = longer:sub" in score_body
    assert "return math.max(SUBFIX_AUDIO_ALIGN.text_score(source_text, reference_text), best_score)" in score_body
    assert '["薦"] = "荐"' in normalize_body
    assert '["課"] = "课"' in normalize_body
    assert '["會"] = "会"' in normalize_body
    assert '["蟄"] = "蛰"' in normalize_body
    assert '["螫"] = "蛰"' in normalize_body
    assert "value:gsub(\"[,.!?;:" in normalize_body


def test_reference_script_input_supports_keyword_list_ui_and_parser():
    parser_body = body_between(
        "local function split_reference_script_keywords",
        "local function format_reference_script_context",
    )

    assert "参考文稿 / 关键词（可选）" in SOURCE
    assert "每行一个关键词" in SOURCE
    assert "local delimiter_pattern" in parser_body
    assert "\",\"" in parser_body
    assert "\"，\"" in parser_body
    assert "\"、\"" in parser_body
    assert "\";\"" in parser_body
    assert "\"；\"" in parser_body
    assert "seen_keywords" in parser_body
    assert "keywords[#keywords + 1]" in parser_body


def test_reference_script_clear_button_clears_and_persists_text_without_changing_assist_toggle():
    config_body = body_between(
        'ID = "AIConfigPopWin"',
        "function show_normalize_length_config_dialog",
    )
    clear_body = body_between(
        "function AIConfigPopWin.On.ClearReferenceScriptBtn.Clicked",
        "function AIConfigPopWin.On.ReferenceScriptInput.TextChanged",
    )

    assert 'ID = "ClearReferenceScriptBtn", Text = "清空"' in config_body
    assert "ReferenceScriptRiskLabel" in config_body
    assert "AIConfigPopWin.On.ClearReferenceScriptBtn.Clicked" in config_body
    assert 'set_textedit_content(find_ui_item("ReferenceScriptInput"), "")' in clear_body
    assert "save_ai_popup_config_state()" in clear_body
    assert 'update_reference_script_risk_label("")' in clear_body
    assert "set_checkbox_checked" not in clear_body


def test_reference_script_prompt_treats_content_as_script_or_keyword_dictionary():
    context_body = body_between(
        "if request_use_script_context and request_script_context ~= \"\" then",
        "if is_full_correction_task then",
    )

    assert "format_reference_script_context(request_script_context)" in context_body
    assert "参考文稿 / 关键词字典" in context_body
    assert "不得为了用上某个关键词而新增字幕内容" in context_body
    assert "严禁把关键词强行加入字幕" in context_body
    assert "严禁按文稿或关键词强行对齐" in context_body
    assert "关键词只是低优先级弱提示" in context_body
    assert "参考内容的唯一用途是修正 ASR 识别错误或误听错字" in context_body
    assert "不是事实校对表、不是术语统一表、不是改名依据、不是润色依据" in context_body
    assert "只要不能证明原词是 ASR 听错，就必须原样保留" in context_body
    assert "原字幕若已经表达了可成立的本意" in context_body
    assert "临场发挥" in context_body
    assert "不得因为参考内容更常见、更像标准词、更完整或出现在词库里就替换原词" in context_body
    assert "例如不要把“小酷”改成“小鑫”，不要把“元仔”改成“二蛋”" in context_body


def test_reference_context_still_only_applies_to_full_correction():
    run_body = body_between(
        "local is_full_correction_task = task_type == \"full_fix\"",
        "local base_sys_prompt = sys_prompt",
    )
    particle_body = body_between(
        "elseif is_particle_correction_task then",
        "if not ai_content then",
    )

    assert "local use_script_context = is_full_correction_task and shared_config.is_script_enabled and script_context ~= \"\"" in run_body
    assert "script_context = \"\"" in particle_body
    assert "use_script_context = false" in particle_body


def test_pre_delivery_speech_consistency_uses_character_coverage_not_only_edit_distance():
    coverage_body = body_between(
        "function split_normalized_text_chars",
        "function collect_pre_delivery_speech_consistency_issues",
    )
    add_issue_body = body_between(
        "local function add_issue(row, kind, reason, reference, score, marker_frame)",
        "for _, row in ipairs(batch_plan.unassigned_rows or {}) do",
    )

    assert "function split_normalized_text_chars(text)" in SOURCE
    assert "function SUBFIX_AUDIO_ALIGN.char_lcs_ratio(source_text, reference_text)" in SOURCE
    assert "function speech_text_consistency_failed(subtitle_text, asr_text, min_score, context_text)" in SOURCE
    assert "if SUBFIX_AUDIO_ALIGN.normalize_text(subtitle_text) == SUBFIX_AUDIO_ALIGN.normalize_text(asr_text) then" in coverage_body
    assert 'return false, 1, 1, "", 1' in coverage_body
    assert "local coverage = SUBFIX_AUDIO_ALIGN.char_lcs_ratio(subtitle_text, asr_text)" in coverage_body
    assert "local asr_coverage = SUBFIX_AUDIO_ALIGN.char_lcs_ratio(asr_text, subtitle_text)" in coverage_body
    assert "function speech_text_has_small_character_difference(subtitle_text, asr_text)" in SOURCE
    assert "function speech_text_has_equal_length_small_substitution(subtitle_text, asr_text)" in SOURCE
    assert "function speech_review_issue_kind(review)" in SOURCE
    assert "speech_review_issue_kind(best_review)" in SOURCE
    assert "speech_text_has_equal_length_small_substitution(subtitle_text, asr_text)" in coverage_body
    assert 'return true, score, coverage, "small_substitution", asr_coverage' in coverage_body
    assert coverage_body.index("speech_text_has_equal_length_small_substitution(subtitle_text, asr_text)") < coverage_body.index("if score < (tonumber(min_score) or 0.72) then")
    assert "speech_text_has_small_character_difference(subtitle_text, asr_text)" in coverage_body
    assert "return true, score, coverage, \"small_diff\", asr_coverage" in coverage_body
    assert "return true, score, coverage, \"coverage\"" in coverage_body
    assert "return true, score, coverage, \"asr_coverage\", asr_coverage" in coverage_body
    assert "PRE_DELIVERY_SPEECH_MIN_CHAR_COVERAGE = 0.86" in SOURCE
    assert "PRE_DELIVERY_SPEECH_MIN_ASR_CHAR_COVERAGE = 0.96" in SOURCE
    assert "score, coverage, fail_reason, asr_coverage = speech_text_consistency_failed" in SOURCE
    assert "review_result.asr_coverage = asr_coverage or 0" in SOURCE
    assert "字幕与局部口播疑似字词替换" in SOURCE
    assert "字幕与局部口播存在小字差异" in SOURCE
    assert "局部口播包含字幕未覆盖内容" in SOURCE
    assert "coverage=%.2f" in SOURCE
    assert "asr_coverage=%.2f" in SOURCE
    assert 'note = string.format("字幕：%s\\n局部口播：%s", subtitle_text, asr_text)' in add_issue_body
    assert "backend/model=" not in add_issue_body
    assert "window=" not in add_issue_body
    assert "audio=" not in add_issue_body
    assert "口播字幕小差异" in SOURCE
    assert "口播字幕疑似字词替换" in SOURCE
    assert "目的就是为了压制蜂群" in SOURCE
    assert "目目的就是为了压制蜂群" in SOURCE
    assert "比如草" in SOURCE
    assert "比如杂草" in SOURCE


def test_pre_delivery_speech_consistency_keeps_long_asr_segment_slicing_as_legacy_helper_only():
    slice_body = body_between(
        "function slice_asr_segment_text_for_frame_window",
        "function collect_pre_delivery_speech_consistency_issues",
    )
    speech_body = body_between(
        "function collect_pre_delivery_speech_consistency_issues",
        "function format_pre_delivery_issue_report_entries",
    )

    assert "function speech_rows_frame_window(rows)" in SOURCE
    assert "function slice_asr_segment_text_for_frame_window(segment, window_start_frame, window_end_frame)" in SOURCE
    assert "function split_raw_text_chars(text)" in SOURCE
    assert "local text_chars = split_raw_text_chars(text)" in slice_body
    assert "local text_len = #text_chars" in slice_body
    assert "local start_ratio = math.max(0, math.min(1" in slice_body
    assert "local end_ratio = math.max(start_ratio, math.min(1" in slice_body
    assert "local start_char = math.max(1, math.floor(text_len * start_ratio" in slice_body
    assert "local end_char = math.min(text_len, math.ceil(text_len * end_ratio" in slice_body
    assert "for index = start_char, end_char do" in slice_body
    assert "return table.concat(sliced)" in slice_body
    assert "function join_speech_check_asr_text_for_rows(rows, asr_segments, fps)" in SOURCE
    assert "speech_rows_frame_window(rows)" in slice_body
    assert "slice_asr_segment_text_for_frame_window(segment, window_start, window_end)" in slice_body
    assert "local asr_group_text = join_speech_check_asr_text_for_rows(group_rows, group_segments, rate)" not in speech_body
    assert "local local_speech_text = speech_review_payload_text(review_info)" in speech_body


def test_pre_delivery_speech_digit_mismatch_handles_spoken_chinese_years():
    digit_body = body_between(
        "function extract_speech_digit_sequences",
        "function collect_pre_delivery_speech_consistency_issues",
    )

    assert "function chinese_digit_sequence_to_arabic(sequence)" in SOURCE
    assert '"[零一二三四五六七八九〇两二]+"' in digit_body
    assert "for chinese_digits in tostring(normalized or \"\"):gmatch" in digit_body
    assert "#chinese_digits >= 2" in digit_body
    assert "chinese_digit_sequence_to_arabic(chinese_digits)" in digit_body
    assert "sequences[#sequences + 1] = converted" in digit_body
    assert "local asr_digits = extract_speech_digit_sequences(asr_text)" in digit_body
    assert "local asr_digit_seen = {}" in digit_body
    assert "asr_digit_seen[tostring(asr_value)] = true" in digit_body
    assert "if not asr_digit_seen[tostring(digits)] then" in digit_body


def test_pre_delivery_speech_digit_mismatch_uses_local_asr_windows():
    speech_body = body_between(
        "function collect_pre_delivery_speech_consistency_issues",
        "function format_pre_delivery_issue_report_entries",
    )

    assert "local digit_issue_row_seen = {}" not in speech_body
    assert "local digit_check_tolerance_frames = math.max" not in speech_body
    assert "local subtitle_group_text = tostring(review_window.text or \"\")" in speech_body
    assert "speech_digit_mismatch(subtitle_group_text, local_speech_text)" in speech_body
    assert "字幕数字与局部口播数字不一致" in speech_body


def test_pre_delivery_speech_consistency_reports_batch_progress_and_supports_cancel():
    run_asr_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.run_asr_alignment",
        "function SUBFIX_AUDIO_ALIGN.alignment_rows_temp_path",
    )
    speech_body = body_between(
        "function collect_pre_delivery_speech_consistency_issues",
        "function format_pre_delivery_issue_report_entries",
    )
    final_check_body = body_between(
        "function run_pre_delivery_final_check",
        "-- [6] 敏感词替换",
    )
    background_body = body_between(
        "function run_subfix_background_command",
        "local function parse_fps",
    )
    force_quit_body = body_between(
        "function force_quit_subfix",
        "-- 强制退出按钮",
    )

    assert "function SUBFIX_AUDIO_ALIGN.run_asr_alignment(audio_source, fps, options)" in SOURCE
    assert '"--progress-json", shell_quote(progress_path)' in run_asr_body
    assert "run_subfix_background_command(table.concat(cmd_parts, \" \"), {" in run_asr_body
    assert "run_shell_capture(table.concat(cmd_parts" not in run_asr_body
    assert "status_window = options.status_window" in run_asr_body
    assert "status_prefix = options.status_prefix" in run_asr_body
    assert "status_started_at = options.status_started_at" in run_asr_body
    assert 'if status == "cancelled" then' in run_asr_body
    assert 'return nil, "已取消", "cancelled"' in run_asr_body

    assert "function update_background_status(payload)" in background_body
    assert "status_prefix .. \"｜用时 \" .. background_status_elapsed_text()" in background_body
    assert "update_shared_status(options.status_window, status_text)" in background_body
    assert "update_background_status(payload)" in background_body

    assert "function collect_pre_delivery_speech_consistency_issues(rows, timeline, fps, options)" in SOURCE
    assert 'string.format("口播一致性｜批次 %d/%d｜%s｜字幕 %d 条｜用时 %s"' in speech_body
    assert 'string.format("口播一致性｜局部复核 %d/%d｜%s｜字幕 %s｜用时 %s"' in speech_body
    assert "update_shared_status(window, review_progress_text)" in speech_body
    assert "SUBFIX_AUDIO_ALIGN.run_asr_review_windows(review_windows, fps, {" in speech_body
    assert speech_body.index("SUBFIX_AUDIO_ALIGN.run_asr_review_windows(review_windows, fps, {") < speech_body.index("for _, review_window in ipairs(review_windows) do")
    assert "批量局部复核" in speech_body
    assert "回落单窗口" in speech_body
    assert "run_asr_alignment(batch.audio_source, fps, {" in speech_body
    assert "batch_index = review_index" in speech_body
    assert "total_batches = total_review_windows" in speech_body
    assert "status_window = window" in speech_body
    assert "status_prefix = review_progress_prefix" in speech_body
    assert "status_started_at = speech_check_started_at" in speech_body
    assert "summary.cancelled = true" in speech_body

    assert "collect_pre_delivery_speech_consistency_issues(current_rows, timeline, fps, {window = window})" not in final_check_body
    assert "if speech_summary and speech_summary.cancelled then" not in final_check_body
    assert 'update_shared_status(window, "最终交付检查已取消")' not in final_check_body
    assert "kill_normalize_background_process()" in force_quit_body


def test_pre_delivery_final_check_clears_stale_subfix_markers_before_adding_current_issues():
    final_check_body = body_between(
        "function run_pre_delivery_final_check",
        "-- [6] 敏感词替换",
    )
    clear_body = body_between(
        "function clear_all_pre_delivery_markers",
        "function pre_delivery_marker_frame",
    )

    assert "function clear_all_pre_delivery_markers(timeline)" in SOURCE
    assert "subfix-pre-delivery" in clear_body
    assert "custom_data:sub(1, #prefix) == prefix" in clear_body
    assert 'custom_data == "" and marker_name:sub(1, #name_prefix) == name_prefix' in clear_body
    assert 'local name_prefix = "SubFix终检："' in clear_body
    assert "marker_name:sub(1, #name_prefix) == name_prefix" in clear_body
    assert "timeline:DeleteMarkerAtFrame(tonumber(frame) or frame)" in clear_body
    assert "return removed, failed" in clear_body
    assert "clear_all_pre_delivery_markers(timeline)" in final_check_body
    assert final_check_body.index("clear_all_pre_delivery_markers(timeline)") < final_check_body.index("add_pre_delivery_check_marker(timeline, issue, tl_start_frame)")


def test_pre_delivery_final_check_click_defers_work_so_status_can_paint():
    click_body = body_between(
        "function win.On.BtnStep4.Clicked",
        "function win.On.BtnStep5.Clicked",
    )
    timer_body = body_between(
        "register_ui_timer(pre_delivery_final_check_timer",
        "function win.On.BtnStep1.Clicked",
    )

    assert 'update_shared_status(target_window, "正在最终交付检查：正在准备...")' in click_body
    assert "restart_ui_timer(pre_delivery_final_check_timer)" in click_body
    assert click_body.index('update_shared_status(target_window, "正在最终交付检查：正在准备...")') < click_body.index("restart_ui_timer(pre_delivery_final_check_timer)")
    assert "run_pre_delivery_final_check(target_window)" in timer_body
    assert "pre_delivery_final_check_timer = ui:Timer" in SOURCE


def test_pre_delivery_final_check_uses_video_cut_points_and_default_thresholds():
    collect_body = body_between(
        "function collect_pre_delivery_final_check_issues",
        "function format_pre_delivery_issue_report_entries",
    )
    cut_body = body_between(
        "function collect_timeline_video_cut_frames",
        "function pre_delivery_issue_key",
    )
    marker_body = body_between(
        "function add_pre_delivery_check_marker",
        "function run_pre_delivery_final_check",
    )
    marker_frame_body = body_between(
        "function pre_delivery_marker_frame",
        "function add_pre_delivery_check_marker",
    )

    assert "PRE_DELIVERY_FAST_READING_CHARS_PER_SECOND = 11" in SOURCE
    assert "PRE_DELIVERY_MAX_SUBTITLE_DURATION_SECONDS = 6" in SOURCE
    assert "PRE_DELIVERY_MAX_SUBTITLE_CHARS = 24" in SOURCE
    assert "PRE_DELIVERY_SUBTITLE_SHORT_GAP_MAX_SECONDS = 0.5" in SOURCE
    assert "PRE_DELIVERY_CUT_ALIGNMENT_TOLERANCE_FRAMES = 6" in SOURCE
    assert "function count_effective_subtitle_chars" in SOURCE
    assert "function subtitle_has_trailing_space" in SOURCE
    assert 'function collect_timeline_video_cut_frames(timeline, track_indices)' in cut_body
    assert "local indices = type(track_indices) == \"table\" and track_indices or {tonumber(track_indices) or 1}" in cut_body
    assert 'timeline:GetItemListInTrack("video", target_track_index)' in cut_body
    assert "function collect_visible_timeline_cut_frames" in SOURCE
    assert "collect_timeline_video_cut_frames(timeline, 1)" in SOURCE
    assert 'timeline:GetTrackCount("video")' in SOURCE
    assert 'timeline:GetIsTrackEnabled("video", track_index)' in SOURCE
    assert "timeline_video_cut_is_covered_by_higher_track" in SOURCE
    assert "collect_timeline_video_track_segments" not in SOURCE
    assert "frame_inside_video_segments" not in SOURCE
    assert "collect_visible_timeline_cut_frames(timeline)" in collect_body
    assert "local cut_tolerance_frames" in collect_body
    assert "local boundary_alignment_cut_seen = {}" in collect_body
    assert "add_boundary_alignment_issue(row, i, row_start, \"起点\")" in collect_body
    assert "add_boundary_alignment_issue(row, i, row_end, \"终点\")" in collect_body
    assert "delta > 0 and delta <= cut_tolerance_frames" in collect_body
    assert "if boundary_alignment_cut_seen[nearest_cut] then return end" in collect_body
    assert "boundary_alignment_cut_seen[nearest_cut] = true" in collect_body
    assert "marker_frame = tonumber(marker_frame) or cut_frame or tonumber(row.start_frame) or 0" in collect_body
    assert 'add_issue(row, i, "空字幕"' in collect_body
    assert "local effective_chars = count_effective_subtitle_chars(row_text)" in collect_body
    assert "local max_subtitle_duration_seconds = tonumber(PRE_DELIVERY_MAX_SUBTITLE_DURATION_SECONDS) or 6" in collect_body
    assert 'add_issue(row, i, "阅读速度过快"' in collect_body
    assert 'add_issue(row, i, "字幕显示过久"' in collect_body
    assert "duration_seconds > max_subtitle_duration_seconds" in collect_body
    assert 'add_issue(row, i, "单条字幕过长"' in collect_body
    assert 'add_issue(row, i, "字幕尾部空格"' in collect_body
    assert "local previous_row = nil" in collect_body
    assert "local previous_text = nil" in collect_body
    assert "local previous_end = nil" in collect_body
    assert '"字幕间异常短缝",' in collect_body
    assert 'add_issue(row, i, "相邻重复字幕"' in collect_body
    assert "gap_frames > 0 and gap_frames <= subtitle_gap_max_frames" in collect_body
    assert 'add_issue(row, i, "字幕间隔过短"' not in collect_body
    assert "gap_marker_frame" in collect_body
    assert "trim_text(row_text) == trim_text(previous_text or \"\")" in collect_body
    assert 'add_issue(row, i, "极短字幕"' not in collect_body
    assert 'add_issue(row, i, "显示太久"' not in collect_body
    assert 'issue.kind == "字幕显示过久"' in marker_body
    assert 'issue.kind == "字幕间异常短缝"' in marker_body
    assert "字幕间异常短缝：%d" in body_between(
        "function pre_delivery_issue_summary_text",
        "function timeline_has_pre_delivery_marker",
    )
    assert "字幕显示过久：%d" in body_between(
        "function pre_delivery_issue_summary_text",
        "function timeline_has_pre_delivery_marker",
    )
    assert '"字幕边界未贴剪辑点"' in collect_body
    assert "source_frame - (tonumber(timeline_start_frame) or 0)" in marker_frame_body
    assert "pre_delivery_marker_frame(issue, timeline_start_frame)" in marker_body
    assert "local duration = 1" in marker_body
    assert 'local note = ""' in marker_body
    assert "string.format(\n        \"行 %s｜%s｜%s\\n%s\"" not in marker_body
    assert "timeline:AddMarker" in marker_body
    assert 'custom_data = pre_delivery_issue_key(issue)' in marker_body
    assert "clear_pre_delivery_marker_by_custom_data(timeline, custom_data)" in marker_body
    assert "timeline:DeleteMarkerByCustomData(custom_data)" in SOURCE
    assert 'return false, "exists"' not in marker_body
    assert "issue.marker_error" in marker_body


def test_pre_delivery_cut_collection_ignores_transition_bridge_edges():
    cut_body = body_between(
        "function is_timeline_transition_bridge",
        "function pre_delivery_issue_key",
    )

    assert "function is_timeline_transition_bridge" in cut_body
    assert "local shared_cut = previous_item.end_frame" in cut_body
    assert "shared_cut == next_item.start_frame" in cut_body
    assert "current_item.start_frame <= shared_cut" in cut_body
    assert "shared_cut <= current_item.end_frame" in cut_body
    assert "current_item.start_frame < current_item.end_frame" in cut_body
    assert "not is_timeline_transition_bridge(item_ranges, item_index)" in cut_body
    assert "elseif start_frame then" in cut_body
    assert "elseif end_frame then" in cut_body
    assert 'item.name == "推移"' not in cut_body
    assert 'item.name == "平滑剪接"' not in cut_body


def test_pre_delivery_transition_alignment_accepts_matching_audio_transition_edge():
    transition_body = body_between(
        "function collect_timeline_transition_alignment_cuts",
        "function pre_delivery_issue_key",
    )
    collect_body = body_between(
        "function collect_pre_delivery_final_check_issues",
        "function format_pre_delivery_issue_report_entries",
    )

    assert '{"video", "audio"}' in transition_body
    assert "timeline:GetTrackCount(track_type)" in transition_body
    assert "timeline:GetIsTrackEnabled(track_type, track_index)" in transition_body
    assert "is_timeline_transition_bridge(item_ranges, item_index)" in transition_body
    assert "alignment_cuts[item_range.start_frame][shared_cut] = true" in transition_body
    assert "alignment_cuts[item_range.end_frame][shared_cut] = true" in transition_body
    assert "local transition_alignment_cuts = collect_timeline_transition_alignment_cuts(timeline)" in collect_body
    assert "transition_alignment_cuts[normalized_boundary_frame]" in collect_body
    assert "acceptable_cuts[nearest_cut]" in collect_body
    assert 'item.name == "交叉淡变 0dB"' not in transition_body


def test_pre_delivery_ignores_lower_video_cuts_covered_by_upper_video():
    visible_body = body_between(
        "function collect_visible_timeline_cut_frames",
        "function collect_timeline_transition_alignment_cuts",
    )

    assert "timeline_video_cut_is_covered_by_higher_track" in visible_body
    assert "track_index + 1" in visible_body
    assert "covered_before" in visible_body
    assert "covered_after" in visible_body


def test_track_switch_keeps_timeline_update_target_in_sync():
    track_up_body = body_between(
        "function win.On.TrackSpinUp.Clicked",
        "function win.On.TrackSpinDown.Clicked",
    )
    track_down_body = body_between(
        "function win.On.TrackSpinDown.Clicked",
        "function win.On.TargetTrackSpin.TextChanged",
    )

    assert "set_target_track_value(current_track, false)" in track_up_body
    assert "set_target_track_value(current_track, false)" in track_down_body


def test_update_timeline_allows_explicit_different_target_track():
    update_body = body_between(
        "update_timeline = function()",
        "local rootFolder = mediaPool:GetRootFolder()",
    )
    srt_pos = update_body.index("local unique_id")

    assert "current_track ~= current_subtitle_target_track" not in update_body[:srt_pos]
    assert "ensure_subtitle_track_exists(current_subtitle_target_track" in SOURCE
    assert "isolate_subtitle_target_track(current_subtitle_target_track" in SOURCE
    assert "detect_subtitle_track_delta(before_snapshot, final_after_snapshot)" in SOURCE


def test_work_scope_reads_timeline_mark_in_out_and_defaults_to_full():
    scope_body = body_between(
        "function build_default_work_scope",
        "function sync_work_scope_ui",
    )

    assert "timeline:GetMarkInOut()" in scope_body
    assert "serialize_lua_value_for_log(marks)" in scope_body
    assert "Resolve 未返回有效 In/Out，已按全片模式加载" in scope_body
    assert "extract_timeline_mark_candidate" in scope_body
    assert '"markIn"' in scope_body
    assert '"markOut"' in scope_body
    assert '"timeline", mark = marks' in scope_body
    assert 'marks.video' in scope_body
    assert 'marks.audio' in scope_body
    assert 'mode = WORK_SCOPE_MODE_FULL' in scope_body
    assert 'mode = WORK_SCOPE_MODE_SELECTION' in scope_body
    assert 'safe_writeback_supported = false' in scope_body
    assert 'writeback_mode = "composite_full_track"' in scope_body


def test_work_scope_accepts_single_sided_timeline_marks():
    scope_body = body_between(
        "function build_default_work_scope",
        "function sync_work_scope_ui",
    )

    assert "mark_in = tl_start_frame" in scope_body
    assert "mark_out = tl_end_frame" in scope_body
    assert "mark_in ~= nil or mark_out ~= nil" in scope_body
    assert "mark_in ~= nil and mark_out ~= nil" not in scope_body


def test_selection_intersection_uses_boundary_overlap_rule():
    intersection_body = body_between(
        "function range_intersects_selection",
        "function work_scope_summary_text",
    )

    assert "scope_start = tonumber(scope.start_frame)" in intersection_body
    assert "scope_end = tonumber(scope.end_frame)" in intersection_body
    assert "item_end > scope_start" in intersection_body
    assert "item_start < scope_end" in intersection_body
    assert "one frame past the visible boundary" in intersection_body


def test_refresh_selection_scope_filters_before_reading_subtitle_text():
    refresh_body = body_between(
        "local function refresh_subtitles",
        "-- ========== 全局刷新函数",
    )
    item_loop_body = body_between(
        "    for i, item in ipairs(items) do\n        local start_frame = item:GetStart() or 0",
        "sort_rows_by_timing(rows)",
    )

    assert "read_timeline_work_scope(timeline" in refresh_body
    assert "current_work_scope = work_scope" in refresh_body
    assert "range_intersects_selection(start_frame, end_frame, current_work_scope)" in item_loop_body
    assert item_loop_body.index("range_intersects_selection(start_frame, end_frame, current_work_scope)") < item_loop_body.index("item:GetName()")
    assert "sync_work_scope_ui(window)" in refresh_body
    assert "reset_pending_review_session()" in refresh_body


def test_selection_update_composes_then_delegates_to_full_track_rebuild():
    selection_body = body_between(
        "function update_timeline_selection_scope",
        "update_timeline = function()",
    )

    assert "clear_subtitle_track_clips" not in selection_body
    assert "delete_selection_overlapping_items" not in selection_body
    assert "filter_new_subtitle_items_for_track" not in selection_body
    assert "ensure_subtitle_track_exists(current_subtitle_target_track, timeline)" in selection_body
    assert "build_selection_composite_rows" in selection_body
    assert selection_body.index("ensure_subtitle_track_exists(current_subtitle_target_track, timeline)") < selection_body.index("build_selection_composite_rows")
    assert "选区合成整轨写回失败: 无法准备目标字幕轨" in selection_body
    assert "current_rows = composite_rows" in selection_body
    assert "current_work_scope = build_default_work_scope()" in selection_body
    assert "update_timeline()" in selection_body
    assert "work_scopes_match" in selection_body
    assert "选区已变化，请先刷新字幕" in selection_body
    assert "将重建字幕轨" in selection_body
    assert "target_track_was_empty" in SOURCE
    assert "if #full_rows == 0 then" in SOURCE


def test_full_timeline_update_still_uses_whole_track_rebuild():
    update_body = body_between(
        "update_timeline = function()",
        "-- ========== AI 处理引擎",
    )

    assert "update_timeline_selection_scope()" in update_body
    assert "current_work_scope.mode == WORK_SCOPE_MODE_SELECTION" in update_body
    assert "clear_subtitle_track_clips(current_subtitle_target_track, timeline)" in update_body


def test_update_timeline_restores_playhead_after_rebuild():
    assert "function capture_timeline_playhead_timecode" in SOURCE
    assert "function restore_timeline_playhead_timecode" in SOURCE

    capture_body = body_between(
        "function capture_timeline_playhead_timecode",
        "function restore_timeline_playhead_timecode",
    )
    restore_body = body_between(
        "function restore_timeline_playhead_timecode",
        "function update_timeline_selection_scope",
    )
    update_body = body_between(
        "update_timeline = function()",
        "-- ========== AI 处理引擎",
    )

    append_pos = update_body.index("mediaPool:AppendToTimeline({mediaPoolItem})")
    capture_pos = update_body.index("local original_playhead_timecode = capture_timeline_playhead_timecode(timeline)")

    assert capture_pos < append_pos
    assert "pcall(function() return timeline:GetCurrentTimecode() end)" in capture_body
    assert "pcall(function() return timeline:SetCurrentTimecode(timecode) end)" in restore_body
    assert "restore_timeline_playhead_timecode(timeline, original_playhead_timecode" in update_body
    assert "finish_timeline_update(" in update_body
    assert "timeline:SetCurrentTimecode(start_tc)" not in update_body


def test_update_timeline_removes_imported_srt_media_item_after_append():
    cleanup_body = body_between(
        "function cleanup_imported_subtitle_media_item",
        "function capture_timeline_playhead_timecode",
    )
    update_body = body_between(
        "update_timeline = function()",
        "-- ========== AI 处理引擎",
    )

    append_pos = update_body.index("mediaPool:AppendToTimeline({mediaPoolItem})")
    cleanup_pos = update_body.index(
        'cleanup_imported_subtitle_media_item(mediaPool, mediaPoolItem, "更新时间线")'
    )
    snapshot_pos = update_body.index("local final_after_snapshot", append_pos)

    assert append_pos < cleanup_pos < snapshot_pos
    assert "pcall(function() return media_pool:DeleteClips({media_pool_item}) end)" in cleanup_body
    assert "return false" in cleanup_body


def test_generate_selection_subtitles_empty_state_entry_is_in_mini_window_ui():
    toolbar_body = SOURCE[
        SOURCE.index('ID = "MiniTrackSpinWrap"') : SOURCE.index('ID = "MiniSearchRow"')
    ]
    placeholder_body = SOURCE[
        SOURCE.index('ID = "MiniSubtitlePlaceholder"') : SOURCE.index('ID = "MiniSubtitleTreeWrap"')
    ]

    assert 'ID = "MiniGenerateSelectionSubtitlesBtn"' in SOURCE
    assert 'Text = "生成选区字幕"' in SOURCE
    assert 'ID = "MiniGenerateSelectionSubtitlesBtn"' not in toolbar_body
    assert 'ID = "MiniGenerateSelectionSubtitlesBtn"' in placeholder_body
    assert "时间线上还没有字幕哦" in SOURCE
    assert "run_generate_selection_subtitles_from_subfix" in SOURCE
    assert "function mini_win.On.MiniGenerateSelectionSubtitlesBtn.Clicked" in SOURCE
    assert 'ID = "GenerateSelectionSubtitlesBtn"' not in SOURCE
    assert "function win.On.GenerateSelectionSubtitlesBtn.Clicked" not in SOURCE


def test_generate_selection_subtitles_subplugin_exists_and_reads_generation_scope():
    assert GENERATOR_CORE_SOURCE_PATH.exists()
    assert "function generate_selection_subtitles" in GENERATOR_CORE_SOURCE

    body = GENERATOR_CORE_SOURCE[
        GENERATOR_CORE_SOURCE.index("function generate_selection_subtitles") : GENERATOR_CORE_SOURCE.index("local ok, err = pcall")
    ]
    guard_pos = body.index("scope.mode ~= WORK_SCOPE_MODE_SELECTION")
    helper_pos = body.index("collect_audio_sources_for_scope")

    assert guard_pos < helper_pos
    assert "read_generation_scope(timeline, fps)" in body
    assert "使用范围来源=%s" in body
    assert "collect_audio_sources_for_scope(timeline, scope, fps)" in body


def test_generate_selection_subtitles_subplugin_prefers_shifted_mark_frames_for_one_hour_timelines():
    normalize_body = GENERATOR_CORE_SOURCE[
        GENERATOR_CORE_SOURCE.index("local function normalize_mark_frame") : GENERATOR_CORE_SOURCE.index("local function read_timeline_in_out_scope")
    ]
    scope_body = GENERATOR_CORE_SOURCE[
        GENERATOR_CORE_SOURCE.index("local function read_timeline_in_out_scope") : GENERATOR_CORE_SOURCE.index("local function read_generation_scope")
    ]

    assert "local shifted_frame = frame + start_frame" in normalize_body
    assert "timecode_to_frame(value, fps)" in normalize_body
    assert "parsed_from_timecode" in normalize_body
    assert "start_frame > 0 and shifted_frame >= start_frame and shifted_frame <= end_frame" in normalize_body
    assert "return math.floor(shifted_frame + 0.5)" in normalize_body
    assert "In/Out raw=%s-%s timeline_start=%s normalized=%s-%s" in scope_body


def test_generate_selection_subtitles_subplugin_accepts_single_sided_timeline_marks():
    scope_body = GENERATOR_CORE_SOURCE[
        GENERATOR_CORE_SOURCE.index("local function read_timeline_in_out_scope") : GENERATOR_CORE_SOURCE.index("local function read_generation_scope")
    ]

    assert "read_timeline_in_out_scope(timeline, fps)" in GENERATOR_CORE_SOURCE
    assert "local mark_candidates = {" in scope_body
    assert "for _, mark in pairs(mark_candidates) do" not in scope_body
    assert "for _, mark in ipairs(mark_candidates) do" in scope_body
    assert "mark_in = timeline_start" in scope_body
    assert "mark_out = timeline_end" in scope_body
    assert "GENERATE_ONE_SIDED_IO_MAX_SECONDS" not in GENERATOR_CORE_SOURCE
    assert "单侧 I/O 范围过长" not in scope_body
    assert '"In/Out (Out only)"' in scope_body
    assert '"In/Out (In only)"' in scope_body
    assert "未读取到 O 点；请按 O 设置结束点后再生成" in scope_body


def test_generate_selection_subtitles_subplugin_requires_real_in_out_scope():
    body = GENERATOR_CORE_SOURCE[
        GENERATOR_CORE_SOURCE.index("function generate_selection_subtitles") : GENERATOR_CORE_SOURCE.index("local ok, err = pcall")
    ]
    scope_body = GENERATOR_CORE_SOURCE[
        GENERATOR_CORE_SOURCE.index("local function read_generation_scope") : GENERATOR_CORE_SOURCE.index("local function get_audio_item_source_offset_frames")
    ]
    assert "read_timeline_in_out_scope(timeline, fps)" in scope_body
    assert "read_selected_timeline_item_scope(timeline)" not in scope_body
    assert "read_playhead_timeline_item_scope(timeline, fps)" not in scope_body
    assert "灰色时间线选择或鼠标选中片段 Resolve 脚本 API 读不到" in scope_body
    assert "使用范围来源=%s" in body


def test_generate_selection_subtitles_subplugin_defaults_to_source_audio_without_render_queue():
    assert "function collect_audio_sources_for_scope" in GENERATOR_CORE_SOURCE
    assert 'timeline:GetItemListInTrack("audio", track_index)' in GENERATOR_CORE_SOURCE
    assert 'GetClipProperty("File Path")' in GENERATOR_CORE_SOURCE
    assert "GetLeftOffset" in GENERATOR_CORE_SOURCE
    assert "function parse_source_audio_channel_mapping" in GENERATOR_CORE_SOURCE
    assert "GetSourceAudioChannelMapping" in GENERATOR_CORE_SOURCE
    assert "GetSourceStartFrame" in GENERATOR_CORE_SOURCE
    assert "+ (linked_offset_samples / math.max(1, sample_rate))" in GENERATOR_CORE_SOURCE
    assert "audio_mapping_source = \"linked_audio\"" in GENERATOR_CORE_SOURCE
    assert "audio_channel_index" in GENERATOR_CORE_SOURCE
    assert "audio_mapping_muted" in GENERATOR_CORE_SOURCE
    assert "function render_timeline_audio_mix_for_selection_generation" not in GENERATOR_CORE_SOURCE
    assert "SetRenderSettings" not in GENERATOR_CORE_SOURCE
    assert "AddRenderJob" not in GENERATOR_CORE_SOURCE
    assert "StartRendering" not in GENERATOR_CORE_SOURCE
    assert "IsRenderingInProgress" not in GENERATOR_CORE_SOURCE
    assert '"audio_source_mode": "source_audio"' in GENERATOR_CORE_SOURCE
    assert "build_timeline_mix_source_for_generation" not in GENERATOR_CORE_SOURCE


def test_linked_polywav_mapping_accumulates_channels_for_repeated_file_paths():
    start = GENERATOR_CORE_SOURCE.index("local function parse_source_audio_channel_mapping")
    end = GENERATOR_CORE_SOURCE.index("local range_intersects_selection", start)
    mapping_body = GENERATOR_CORE_SOURCE[start:end]

    assert "linked_path_channel_index" in mapping_body
    assert "linked_path_channel_count" in mapping_body
    assert "candidate.path == linked_info.path" in mapping_body
    assert "linked_path_channel_count > 1" in mapping_body
    assert "linked_channels > 1 and math.max(1, tonumber(linked_local_channel_index) or 1)" not in mapping_body


def test_generate_selection_subtitles_subplugin_prompts_for_audio_track():
    assert "function show_audio_track_selection_dialog" in GENERATOR_CORE_SOURCE
    assert 'ID = "GenerateAudioTrackTree"' in GENERATOR_CORE_SOURCE
    assert "TRACK_CHECKED_MARK" in GENERATOR_CORE_SOURCE
    assert "TRACK_UNCHECKED_MARK" in GENERATOR_CORE_SOURCE
    assert "track_rows[index] = {source = source, checked = checked, item = item}" in GENERATOR_CORE_SOURCE
    assert 'ID = "GenerateAudioTrackCombo"' not in GENERATOR_CORE_SOURCE
    assert 'WindowTitle = "SubFix · 生成选区字幕"' in GENERATOR_CORE_SOURCE
    # Window height includes the 字幕长度、识别模型与热词行；合并豆包主入口
    # 不应回退热词布局所需的现有高度。
    assert "Geometry = SUBFIX_WINDOW_GEOMETRY.centered_geometry({460, 250, 420, 316})" in GENERATOR_CORE_SOURCE
    assert "selected_audio_sources" in GENERATOR_CORE_SOURCE
    assert "local checked = index == 1" in GENERATOR_CORE_SOURCE
    assert "collect_audio_sources_for_scope(timeline, scope, fps)" in GENERATOR_CORE_SOURCE
    assert "collect_video_match_tokens_for_scope" not in GENERATOR_CORE_SOURCE
    assert 'timeline:GetTrackCount("video")' not in GENERATOR_CORE_SOURCE
    assert "video_match_score" not in GENERATOR_CORE_SOURCE
    assert "匹配画面" not in GENERATOR_CORE_SOURCE
    assert "candidate.is_selected" in GENERATOR_CORE_SOURCE
    assert "candidate.audio_mapping_muted" in GENERATOR_CORE_SOURCE
    assert "a_muted ~= b_muted" in GENERATOR_CORE_SOURCE
    assert "return a_muted == false" in GENERATOR_CORE_SOURCE
    assert "return a_track < b_track" in GENERATOR_CORE_SOURCE
    assert 'timeline:GetTrackName("audio", track_index)' in GENERATOR_CORE_SOURCE
    assert "function build_audio_track_options_for_dialog" in GENERATOR_CORE_SOURCE
    assert "track_sources" in GENERATOR_CORE_SOURCE
    assert "best_by_track[track_index]" not in GENERATOR_CORE_SOURCE
    assert "source.display_label = label" in GENERATOR_CORE_SOURCE
    assert "return tostring(audio_source.display_label)" in GENERATOR_CORE_SOURCE
    assert "track_name" in GENERATOR_CORE_SOURCE
    assert "音频候选已按轨道折叠" in GENERATOR_CORE_SOURCE


def test_generate_selection_subtitles_dialog_has_no_program_type_dropdown_and_fixes_live_mode():
    # Product decision: narration mode has no trained model/calibration data and
    # degrades the engine; all user content is multi-mic live recording. The
    # "节目类型" dropdown is removed from the UI and subtitle_mode is hardcoded
    # to "live". The Python-side narration code paths remain untouched so the
    # dropdown can be restored cheaply later if needed.
    dialog_body = GENERATOR_CORE_SOURCE[
        GENERATOR_CORE_SOURCE.index("local function show_audio_track_selection_dialog") : GENERATOR_CORE_SOURCE.index("local function progress_elapsed_text")
    ]
    command_body = GENERATOR_CORE_SOURCE[
        GENERATOR_CORE_SOURCE.index("local function build_asr_helper_batch_command") : GENERATOR_CORE_SOURCE.index("local function kill_background_process")
    ]

    assert 'ID = "GenerateSubtitleModeCombo"' not in dialog_body
    assert "叙述模式" not in dialog_body
    assert "节目类型" not in dialog_body
    assert "apply_subtitle_mode_selection" not in dialog_body
    assert 'local subtitle_mode = "live"' in dialog_body
    assert "现场模式至少选择一条音频轨道" in dialog_body
    assert 'set_track_checked(track_rows[row_index], not track_rows[row_index].checked)' in dialog_body
    assert '"--subtitle-mode", shell_quote(subtitle_mode)' in command_body


def test_generate_selection_subtitles_dialog_has_subtitle_length_controls_defaulting_to_standard():
    # New feature: 字幕长度 is chosen via two mutually-exclusive, compact
    # checkbox-style controls (not a dropdown, and not full-row stretched
    # buttons) controlling segmentation granularity (max chars per subtitle
    # line), independent from the removed 节目类型 dropdown. They reuse the
    # same ☑/☐ mark style as the audio track list above, sized small
    # (Weight = 0, MinimumSize height 20) instead of spanning the row.
    # Standard (<=25 chars) is the default; short video (<=10 chars) is
    # selectable. The choice is threaded to the Python helper via --max-chars.
    dialog_body = GENERATOR_CORE_SOURCE[
        GENERATOR_CORE_SOURCE.index("local function show_audio_track_selection_dialog") : GENERATOR_CORE_SOURCE.index("local function progress_elapsed_text")
    ]
    command_body = GENERATOR_CORE_SOURCE[
        GENERATOR_CORE_SOURCE.index("local function build_asr_helper_batch_command") : GENERATOR_CORE_SOURCE.index("local function kill_background_process")
    ]

    # The old dropdown is gone; it's now two compact checkbox-style controls.
    assert 'ID = "GenerateSubtitleLengthCombo"' not in dialog_body
    assert 'ui:ComboBox{ID = "GenerateSubtitleLengthCombo"' not in dialog_body
    assert 'ID = "GenerateSubtitleLengthStandardBtn"' in dialog_body
    assert 'ID = "GenerateSubtitleLengthShortBtn"' in dialog_body
    assert "字幕长度" in dialog_body
    assert "标准（≤25字）" in dialog_body
    assert "短视频（≤10字）" in dialog_body
    # Compact sizing: no longer stretched full-row buttons (Weight = 1,
    # MinimumSize height 26); now small, non-stretching checkbox-style
    # controls matching the audio track list's compactness.
    assert 'ui:Button{ID = "GenerateSubtitleLengthStandardBtn", Text = "标准（≤25字）", Weight = 0, MinimumSize = {0, 20}}' in dialog_body
    assert 'ui:Button{ID = "GenerateSubtitleLengthShortBtn", Text = "短视频（≤10字）", Weight = 0, MinimumSize = {0, 20}}' in dialog_body
    assert 'Weight = 1, MinimumSize = {0, 26}' not in dialog_body
    # The selected/unselected prefixes reuse the same ☑/☐ marks as the
    # audio track list, not the old "●" / full-width-space markers.
    assert "local SUBTITLE_LENGTH_SELECTED_PREFIX = TRACK_CHECKED_MARK .. \" \"" in dialog_body
    assert "local SUBTITLE_LENGTH_UNSELECTED_PREFIX = TRACK_UNCHECKED_MARK .. \" \"" in dialog_body
    assert 'SUBTITLE_LENGTH_SELECTED_PREFIX = "● "' not in dialog_body
    # Mutually-exclusive selection state, defaulting to standard (index 1).
    assert "local selected_length_index = 1" in dialog_body
    assert "function selection_window.On.GenerateSubtitleLengthStandardBtn.Clicked" in dialog_body
    assert "function selection_window.On.GenerateSubtitleLengthShortBtn.Clicked" in dialog_body
    assert "select_subtitle_length(1)" in dialog_body
    assert "select_subtitle_length(2)" in dialog_body
    assert "SUBTITLE_LENGTH_OPTIONS[1].max_chars" in dialog_body
    assert "local selected_max_chars = SUBTITLE_LENGTH_OPTIONS[selected_length_index].max_chars" in dialog_body
    # Return signature carries both the chosen ASR backend and the optional
    # hotword configuration; the UI merge must not break the hotword chain.
    assert "return selected_audio_sources, subtitle_mode, selected_max_chars, selected_backend, selected_hotwords_json, nil" in dialog_body
    assert '"--max-chars"' in command_body
    assert "max_chars = tonumber(max_chars) or 25" in command_body


def test_generate_selection_subtitles_persists_sanitized_last_run_diagnostic():
    helper = (ROOT / "subfix_asr_transcribe.py").read_text(encoding="utf-8")
    command_body = GENERATOR_CORE_SOURCE[
        GENERATOR_CORE_SOURCE.index("local function build_asr_helper_batch_command") : GENERATOR_CORE_SOURCE.index("local function kill_background_process")
    ]

    assert 'diagnostic = user_support_dir .. "/last_generate_diagnostic.json"' in GENERATOR_CORE_SOURCE
    assert '"--diagnostic-output", shell_quote(paths.diagnostic)' in command_body
    assert 'parser.add_argument("--diagnostic-output")' in helper
    assert "sanitize_generate_diagnostic_payload" in helper


def test_generate_selection_live_mode_preserves_speaker_fields_and_skips_track_priority_merge():
    parse_body = GENERATOR_CORE_SOURCE[
        GENERATOR_CORE_SOURCE.index("local function parse_generated_json_subtitle_rows") : GENERATOR_CORE_SOURCE.index("local function subtract_existing_ranges")
    ]
    generate_body = GENERATOR_CORE_SOURCE[
        GENERATOR_CORE_SOURCE.index("local function generate_selection_subtitles") : GENERATOR_CORE_SOURCE.index("function SubFixGenerateSelectionCore.run")
    ]

    assert "speaker_track_index" in parse_body
    assert "speaker_score_db" in parse_body
    assert "speaker_decision" in parse_body
    assert "payload.subtitle_rows" in parse_body
    assert "payload.diagnostic" not in parse_body
    assert 'subtitle_mode == "live"' in generate_body
    assert "merge_generated_rows_for_live_writeback" in generate_body
    assert "merge_generated_rows_with_track_priority" in generate_body


def test_generate_selection_subtitles_subplugin_uses_checked_audio_if_dialog_loop_exits_unexpectedly():
    dialog_body = GENERATOR_CORE_SOURCE[
        GENERATOR_CORE_SOURCE.index("local function show_audio_track_selection_dialog") : GENERATOR_CORE_SOURCE.index("local function progress_elapsed_text")
    ]

    assert "local dialog_cancelled = false" in dialog_body
    assert "local function collect_checked_audio_sources()" in dialog_body
    assert "selected_audio_sources = collect_checked_audio_sources()" in dialog_body
    assert "dialog_cancelled = true" in dialog_body
    assert "if not selected_audio_sources and not dialog_cancelled then" in dialog_body
    assert "音频轨选择窗口提前退出，使用默认勾选轨道" in dialog_body


def test_generate_selection_subtitles_subplugin_uses_selected_tracks_batch_plan():
    build_options_body = GENERATOR_CORE_SOURCE[
        GENERATOR_CORE_SOURCE.index("local function build_audio_track_options_for_dialog") : GENERATOR_CORE_SOURCE.index("function range_intersects_selection")
    ]
    generate_body = GENERATOR_CORE_SOURCE[
        GENERATOR_CORE_SOURCE.index("function generate_selection_subtitles()") : GENERATOR_CORE_SOURCE.index("function SubFixGenerateSelectionCore.run(options)")
    ]

    assert "sources_by_track" in build_options_body
    assert "track_sources = sources" in build_options_body
    assert "table.sort(sources, function(a, b)" in build_options_body
    assert "selected_audio_sources" in generate_body
    assert "collect_selected_track_sources(selected_audio_sources)" in generate_body
    assert "build_timeline_mix_source_for_generation" not in generate_body
    assert "write_generate_batch_plan" in GENERATOR_CORE_SOURCE
    assert "run_asr_helper_batch_with_progress" in generate_body
    assert "run_asr_helper_with_progress" not in generate_body
    assert "merge_generated_rows_with_track_priority" in GENERATOR_CORE_SOURCE
    assert "merge_generated_rows_for_writeback" in GENERATOR_CORE_SOURCE
    assert generate_body.count("backup_target_track(") == 1
    assert generate_body.count("rebuild_target_subtitle_track_from_rows(project, timeline") == 1


def test_generate_backup_snapshots_target_track_once_and_reuses_rows():
    backup_body = GENERATOR_CORE_SOURCE[
        GENERATOR_CORE_SOURCE.index("local function backup_target_track") : GENERATOR_CORE_SOURCE.index("local function get_subtitle_track_type_and_count")
    ]
    composite_body = GENERATOR_CORE_SOURCE[
        GENERATOR_CORE_SOURCE.index("local function build_composite_rows_for_writeback") : GENERATOR_CORE_SOURCE.index("local function write_rows_to_rebuild_srt")
    ]
    generate_body = GENERATOR_CORE_SOURCE[
        GENERATOR_CORE_SOURCE.index("function generate_selection_subtitles()") : GENERATOR_CORE_SOURCE.index("function SubFixGenerateSelectionCore.run(options)")
    ]

    assert "progress_callback" in backup_body
    assert "snapshot_rows" in backup_body
    assert "GENERATE_BACKUP_PROGRESS_CHUNK_SIZE" in backup_body
    assert "progress_callback(completed, #items)" in backup_body
    assert "读取目标字幕轨第" in backup_body
    assert "local function build_composite_rows_for_writeback(existing_rows, scope, generated_rows)" in composite_body
    assert "GetItemListInTrack" not in composite_body
    assert ":GetStart()" not in composite_body
    assert ":GetEnd()" not in composite_body
    assert ":GetName()" not in composite_body
    assert "existing_target_rows" in generate_body
    assert "if backup_err then" in generate_body
    assert "build_composite_rows_for_writeback(existing_target_rows, scope, generated_rows)" in generate_body


def test_generate_selection_subtitles_subplugin_always_prompts_even_for_single_audio_candidate():
    # 行为变更：即便只有 1 个音频候选，也始终弹出选择对话框，让用户能选识别模型
    # （Qwen/豆包）与字幕长度，不再对单候选自动跳过弹窗、直接用默认引擎生成。
    generate_body = GENERATOR_CORE_SOURCE[
        GENERATOR_CORE_SOURCE.index("function generate_selection_subtitles()") : GENERATOR_CORE_SOURCE.index("function SubFixGenerateSelectionCore.run(options)")
    ]

    # 自动跳过分支已移除。
    assert "if #audio_sources == 1 then" not in generate_body
    assert "selected_audio_sources = audio_sources" not in generate_body
    # 始终无条件调用对话框，backend/长度/勾选都由它返回。
    assert "show_audio_track_selection_dialog(audio_sources, scope, fps)" in generate_body
    assert "selected_audio_sources, subtitle_mode, max_chars, backend, hotwords_json, selection_err = show_audio_track_selection_dialog(audio_sources, scope, fps)" in generate_body
    assert re.search(
        r"run_asr_helper_batch_with_progress\(.*?\bbackend,\s*hotwords_json,\s*effective_audio_seconds\s*\)",
        generate_body,
        re.DOTALL,
    )


def test_generate_selection_subtitles_subplugin_writes_source_audio_batch_plan_by_default():
    generate_body = GENERATOR_CORE_SOURCE[
        GENERATOR_CORE_SOURCE.index("function generate_selection_subtitles()") : GENERATOR_CORE_SOURCE.index("function SubFixGenerateSelectionCore.run(options)")
    ]

    assert "collect_selected_track_sources(selected_audio_sources)" in generate_body
    assert "optimize_selected_track_sources_for_generation(selected_track_sources, fps)" in generate_body
    assert "write_generate_batch_plan(batch_plan_path, selected_track_sources, fps)" in generate_body
    assert "selected_track_sources = {timeline_mix_source}" not in generate_body
    assert "使用时间线混音" not in generate_body


def test_generate_selection_subtitles_subplugin_keeps_fast_source_audio_path_available():
    generate_body = GENERATOR_CORE_SOURCE[
        GENERATOR_CORE_SOURCE.index("function generate_selection_subtitles()") : GENERATOR_CORE_SOURCE.index("function SubFixGenerateSelectionCore.run(options)")
    ]
    diagnostic_body = GENERATOR_CORE_SOURCE[
        GENERATOR_CORE_SOURCE.index("local function write_selected_audio_source_diagnostic") : GENERATOR_CORE_SOURCE.index("local function parse_generated_json_subtitle_rows")
    ]

    assert "optimize_selected_track_sources_for_generation(selected_track_sources, fps)" in generate_body
    assert generate_body.index("optimize_selected_track_sources_for_generation") < generate_body.index("write_generate_batch_plan")
    assert "selected_track_sources = optimized_track_sources" in generate_body
    assert "audio_sources_can_merge" not in GENERATOR_CORE_SOURCE
    assert "GENERATE_SOURCE_MERGE_MAX_SOURCE_GAP_SECONDS" not in GENERATOR_CORE_SOURCE
    assert "GENERATE_SOURCE_MERGE_MAX_TIMELINE_GAP_FRAMES" not in GENERATOR_CORE_SOURCE
    assert "raw_source_count" in diagnostic_body
    assert "deduped_source_count" in diagnostic_body
    assert "merged_source_count" in diagnostic_body
    assert "duplicate_source_count" in diagnostic_body
    assert "merged_source_group_count" in diagnostic_body
    assert "source_optimization" in diagnostic_body


def test_generate_selection_subtitles_subplugin_does_not_mutate_audio_track_state_for_source_audio():
    assert "function save_audio_track_enable_state" not in GENERATOR_CORE_SOURCE
    assert "function restore_audio_track_enable_state" not in GENERATOR_CORE_SOURCE
    assert "function enable_only_selected_audio_tracks_for_mix" not in GENERATOR_CORE_SOURCE
    assert 'timeline:SetTrackEnable("audio"' not in GENERATOR_CORE_SOURCE


def test_generate_selection_subtitles_subplugin_shows_progress_and_runs_helper_in_background():
    assert "function show_generate_progress_window" in GENERATOR_CORE_SOURCE
    assert "local GENERATE_PROGRESS_BAR_WIDTH = 36" in GENERATOR_CORE_SOURCE
    assert 'ID = "GenerateProgressWindow"' in GENERATOR_CORE_SOURCE
    assert 'ID = "GenerateProgressStatusLabel"' in GENERATOR_CORE_SOURCE
    assert 'ID = "GenerateProgressBarLabel"' in GENERATOR_CORE_SOURCE
    assert 'ID = "GenerateProgressMetaLabel"' in GENERATOR_CORE_SOURCE
    assert 'ID = "GenerateProgressHintLabel"' in GENERATOR_CORE_SOURCE
    assert "Geometry = SUBFIX_WINDOW_GEOMETRY.centered_geometry({520, 380, 430, 200})" in GENERATOR_CORE_SOURCE
    assert "ContentsMargins = 20" in GENERATOR_CORE_SOURCE
    assert 'Text = "准备中"' in GENERATOR_CORE_SOURCE
    assert 'Text = "ᗧ" .. string.rep("□", GENERATE_PROGRESS_BAR_WIDTH - 1) .. "⚑"' in GENERATOR_CORE_SOURCE
    assert 'Text = "去摸个鱼吧🐟～\\n::)"' in GENERATOR_CORE_SOURCE
    assert "Alignment = {AlignHCenter = true, AlignVCenter = true}" in GENERATOR_CORE_SOURCE
    assert "ui:VGap(4)" in GENERATOR_CORE_SOURCE
    progress_window_body = GENERATOR_CORE_SOURCE[
        GENERATOR_CORE_SOURCE.index("local function show_generate_progress_window") : GENERATOR_CORE_SOURCE.index("local function update_generate_progress_window")
    ]
    assert progress_window_body.count("ui:HGap(0, 1)") >= 6
    assert 'ID = "GenerateProgressCancelBtn"' in progress_window_body
    assert 'ID = "GenerateProgressCloseBtn"' not in progress_window_body
    assert progress_window_body.count('ui:Button{ID = "GenerateProgress') == 1
    assert "progress_state.finished" in progress_window_body
    assert "function show_writeback_progress_overlay(progress_state)" in GENERATOR_CORE_SOURCE
    assert "progress_window:SetAttrs({Geometry = SUBFIX_WINDOW_GEOMETRY.centered_geometry(GENERATE_WRITEBACK_OVERLAY_GEOMETRY)})" in GENERATOR_CORE_SOURCE
    assert "progress_window:Hide()" in progress_window_body
    assert "GenerateProgressStageLabel" not in GENERATOR_CORE_SOURCE
    assert "GenerateProgressMessageLabel" not in GENERATOR_CORE_SOURCE
    assert "GenerateProgressElapsedLabel" not in GENERATOR_CORE_SOURCE
    assert "GenerateProgressAudioLabel" not in GENERATOR_CORE_SOURCE
    assert "GenerateProgressLog" not in GENERATOR_CORE_SOURCE
    assert "ui:TextEdit" not in progress_window_body
    assert '"--progress-json", shell_quote(progress_path)' in GENERATOR_CORE_SOURCE
    assert "run_asr_helper_with_progress" in GENERATOR_CORE_SOURCE
    assert "run_background_command_with_progress(cmd, progress_path" in GENERATOR_CORE_SOURCE
    helper_progress_body = GENERATOR_CORE_SOURCE[
        GENERATOR_CORE_SOURCE.index("local function run_background_command_with_progress") : GENERATOR_CORE_SOURCE.index("local function run_asr_helper_with_progress")
    ]
    assert "run_shell_capture(cmd)" not in helper_progress_body
    assert "local timer_registered = register_ui_timer(poll_timer, function()" in GENERATOR_CORE_SOURCE
    assert "无法启动进度轮询" in GENERATOR_CORE_SOURCE


def test_generate_selection_subtitles_subplugin_uses_single_monotonic_progress_bar():
    progress_body = GENERATOR_CORE_SOURCE[
        GENERATOR_CORE_SOURCE.index("local function parse_progress_payload") : GENERATOR_CORE_SOURCE.index("local function build_asr_helper_command")
    ]
    generate_body = GENERATOR_CORE_SOURCE[
        GENERATOR_CORE_SOURCE.index("function generate_selection_subtitles") : GENERATOR_CORE_SOURCE.index("local ok, err = pcall")
    ]

    assert 'number_field("progress_index")' in progress_body
    assert 'number_field("progress_total")' in progress_body
    assert "progress_state.progress_fraction = math.max" in progress_body
    assert "progress_state.progress_total = 100" in progress_body
    assert "local width = GENERATE_PROGRESS_BAR_WIDTH" in progress_body
    assert 'cells[#cells + 1] = "ᗧ"' in progress_body
    assert 'cells[#cells + 1] = "■"' in progress_body
    assert 'cells[#cells + 1] = "□"' in progress_body
    assert 'string.rep("■", width) .. "⚑"' in progress_body
    assert 'table.concat(cells) .. "⚑"' in progress_body
    assert 'items.GenerateProgressMetaLabel.Text = "进度 " .. tostring(percent) .. "%  ·  用时 " .. elapsed' in progress_body
    assert "local pulse =" not in progress_body
    assert 'payload.progress_index = 100' in progress_body
    assert '"解析结果", "正在解析生成结果", 96' in generate_body
    assert '"合并字幕", "正在合并识别结果", 97' in generate_body
    assert '"备份字幕", "正在备份目标字幕轨", 98' in generate_body
    assert '"写回时间线", "正在写回字幕", 99' in generate_body
    assert "show_writeback_progress_overlay(progress_state)" in generate_body
    assert generate_body.index("show_writeback_progress_overlay(progress_state)") < generate_body.index("rebuild_target_subtitle_track_from_rows")


def test_generate_selection_subtitles_subplugin_keeps_progress_state_off_ui_object():
    assert "progress_state = {" in GENERATOR_CORE_SOURCE
    assert "progress_state.log_lines" not in GENERATOR_CORE_SOURCE
    assert "progress_window.log_lines" not in GENERATOR_CORE_SOURCE
    assert "progress_window.started_at" not in GENERATOR_CORE_SOURCE
    assert "progress_window.cancel_requested" not in GENERATOR_CORE_SOURCE


def test_generate_selection_subtitles_subplugin_command_uses_generate_mode_and_srt_output():
    assert '"--mode", "generate_subtitles_batch"' in GENERATOR_CORE_SOURCE
    assert "--batch-plan-json" in GENERATOR_CORE_SOURCE
    assert "--timeline-start-frame" in GENERATOR_CORE_SOURCE
    assert "--srt-output" in GENERATOR_CORE_SOURCE
    assert "--srt-base-frame" in GENERATOR_CORE_SOURCE


def test_generate_selection_subtitles_subplugin_command_uses_v5():
    helper = (ROOT / "subfix_asr_transcribe.py").read_text(encoding="utf-8")
    engine_body = GENERATOR_CORE_SOURCE[
        GENERATOR_CORE_SOURCE.index("local function resolve_generate_engine") : GENERATOR_CORE_SOURCE.index("local function resolve_segmentation_profile_path")
    ]
    assert 'return "v5"' in engine_body
    assert 'os.getenv("SUBFIX_GENERATE_ENGINE")' not in engine_body

    command_bodies = [
        GENERATOR_CORE_SOURCE[
            GENERATOR_CORE_SOURCE.index("local function build_asr_helper_command") : GENERATOR_CORE_SOURCE.index("local function build_asr_helper_batch_command")
        ],
        GENERATOR_CORE_SOURCE[
            GENERATOR_CORE_SOURCE.index("local function build_asr_helper_batch_command") : GENERATOR_CORE_SOURCE.index("local function kill_background_process")
        ],
    ]

    for command_body in command_bodies:
        assert "local generate_engine = resolve_generate_engine()" in command_body
        assert '"--generate-engine", shell_quote(generate_engine)' in command_body

    batch_body = command_bodies[1]
    assert "resolve_segmentation_profile_path(paths, generate_engine)" in batch_body
    assert '"--segmentation-profile"' in batch_body

    plan_body = GENERATOR_CORE_SOURCE[
        GENERATOR_CORE_SOURCE.index("local function write_generate_batch_plan") : GENERATOR_CORE_SOURCE.index("local function parse_generated_json_subtitle_rows")
    ]
    assert '"timeline_end_frame"' in plan_body
    assert '"timeline_end_frame"' in helper


def test_generate_selection_subtitles_subplugin_rebuilds_composite_target_track_for_writeback():
    rebuild_body = GENERATOR_CORE_SOURCE[
        GENERATOR_CORE_SOURCE.index("local function rebuild_target_subtitle_track_from_rows") : GENERATOR_CORE_SOURCE.index("local function generate_selection_subtitles")
    ]

    assert "function parse_generated_srt_rows" in GENERATOR_CORE_SOURCE
    assert "function build_composite_rows_for_writeback" in GENERATOR_CORE_SOURCE
    assert "function write_rows_to_rebuild_srt" in GENERATOR_CORE_SOURCE
    assert "function append_rebuild_srt_to_timeline" in GENERATOR_CORE_SOURCE
    assert "function rebuild_target_subtitle_track_from_rows" in GENERATOR_CORE_SOURCE
    assert "ensure_subtitle_track_exists(TARGET_SUBTITLE_TRACK, timeline)" in GENERATOR_CORE_SOURCE
    assert "unlock_all_subtitle_tracks(timeline)" in GENERATOR_CORE_SOURCE
    assert "isolate_subtitle_target_track(TARGET_SUBTITLE_TRACK, timeline)" in GENERATOR_CORE_SOURCE
    assert "snapshot_subtitle_tracks(timeline)" not in GENERATOR_CORE_SOURCE
    assert "detect_subtitle_track_delta(before_snapshot, final_after_snapshot)" not in GENERATOR_CORE_SOURCE
    assert "function capture_timeline_playhead_timecode(timeline)" in GENERATOR_CORE_SOURCE
    assert "function restore_timeline_playhead_timecode(timeline, timecode)" in GENERATOR_CORE_SOURCE
    assert "pcall(function() return timeline:GetCurrentTimecode() end)" in GENERATOR_CORE_SOURCE
    assert "pcall(function() return timeline:SetCurrentTimecode(timecode) end)" in GENERATOR_CORE_SOURCE
    assert "local original_playhead_timecode = capture_timeline_playhead_timecode(timeline)" in rebuild_body
    assert "local function finish_rebuild(result, err)" in rebuild_body
    assert "restore_timeline_playhead_timecode(timeline, original_playhead_timecode)" in rebuild_body
    assert "return finish_rebuild(true)" in rebuild_body
    assert "set_playhead_to_timeline_start(timeline, base_frame, fps)" not in GENERATOR_CORE_SOURCE
    assert "使用稳定模式追加字幕到时间线" in GENERATOR_CORE_SOURCE
    assert "media_pool:AppendToTimeline({media_pool_item})" in GENERATOR_CORE_SOURCE
    assert "media_pool:AppendToTimeline({append_info})" not in GENERATOR_CORE_SOURCE
    assert "使用 clipInfo 追加字幕到时间线" not in GENERATOR_CORE_SOURCE
    assert "clipInfo 追加失败" not in GENERATOR_CORE_SOURCE
    assert "format_writeback_position_diagnostic" not in GENERATOR_CORE_SOURCE
    assert "clear_subtitle_track_clips(timeline, TARGET_SUBTITLE_TRACK)" in GENERATOR_CORE_SOURCE
    assert "function import_rebuild_srt_with_retry" in GENERATOR_CORE_SOURCE
    assert "重试导入重建字幕 SRT" in GENERATOR_CORE_SOURCE
    assert 'os.execute("sleep 0.25")' in GENERATOR_CORE_SOURCE
    assert "local media_pool_item = items[1]" in rebuild_body
    assert "append_rebuild_srt_to_timeline(media_pool, media_pool_item)" in rebuild_body
    assert "parse_generated_json_subtitle_rows(json_path)" in GENERATOR_CORE_SOURCE
    assert "merge_generated_rows_with_track_priority(raw_generated_rows, scope)" in GENERATOR_CORE_SOURCE
    assert "merge_generated_rows_for_writeback({priority_rows}, scope)" in GENERATOR_CORE_SOURCE
    assert "build_composite_rows_for_writeback(existing_target_rows, scope, generated_rows)" in GENERATOR_CORE_SOURCE

    import_pos = rebuild_body.index("import_rebuild_srt_with_retry(media_pool, rebuild_srt_path)")
    delete_pos = rebuild_body.index("clear_subtitle_track_clips(timeline, TARGET_SUBTITLE_TRACK)")
    capture_pos = rebuild_body.index("local original_playhead_timecode = capture_timeline_playhead_timecode(timeline)")
    append_pos = rebuild_body.index("append_rebuild_srt_to_timeline(media_pool, media_pool_item)")
    success_return_pos = rebuild_body.index("return finish_rebuild(true)")
    assert capture_pos < append_pos < success_return_pos
    assert import_pos < delete_pos
    assert "保留选区外字幕" in GENERATOR_CORE_SOURCE

    backup_body = GENERATOR_CORE_SOURCE[
        GENERATOR_CORE_SOURCE.index("local function backup_target_track") : GENERATOR_CORE_SOURCE.index("local function get_subtitle_track_type_and_count")
    ]
    assert "get_subtitle_track_items(track_index, timeline)" in backup_body
    assert 'finish_generate_progress_window(progress_state, "失败", backup_err)' in GENERATOR_CORE_SOURCE


def test_generate_selection_subtitles_removes_imported_srt_media_item_after_append():
    cleanup_start = GENERATOR_CORE_SOURCE.index(
        "local function cleanup_imported_subtitle_media_item"
    )
    cleanup_end = GENERATOR_CORE_SOURCE.index(
        "local function append_rebuild_srt_to_timeline", cleanup_start
    )
    cleanup_body = GENERATOR_CORE_SOURCE[cleanup_start:cleanup_end]
    rebuild_body = GENERATOR_CORE_SOURCE[
        GENERATOR_CORE_SOURCE.index("local function rebuild_target_subtitle_track_from_rows") :
        GENERATOR_CORE_SOURCE.index("local function generate_selection_subtitles", cleanup_end)
    ]

    append_pos = rebuild_body.index(
        "append_rebuild_srt_to_timeline(media_pool, media_pool_item)"
    )
    cleanup_pos = rebuild_body.index(
        'cleanup_imported_subtitle_media_item(media_pool, media_pool_item, "生成选区字幕")'
    )
    success_pos = rebuild_body.index("return finish_rebuild(true)")

    assert append_pos < cleanup_pos < success_pos
    assert "pcall(function() return media_pool:DeleteClips({media_pool_item}) end)" in cleanup_body
    assert "return false" in cleanup_body


def test_generate_selection_subtitles_subplugin_limits_asr_to_selected_overlap():
    command_body = GENERATOR_CORE_SOURCE[
        GENERATOR_CORE_SOURCE.index("local function build_asr_helper_command") : GENERATOR_CORE_SOURCE.index("local function kill_background_process")
    ]
    assert '"--timeline-start-frame", shell_quote(tostring(audio_source.start_frame))' in command_body
    assert '"--source-start", shell_quote(string.format("%.3f", tonumber(audio_source.source_start_seconds) or 0))' in command_body
    assert '"--source-end", shell_quote(string.format("%.3f", tonumber(audio_source.source_end_seconds) or 0))' in command_body
    assert '"--audio-channel-index"' in command_body
    assert "scope.end_frame - scope.start_frame" not in command_body


def test_generate_selection_subtitles_subplugin_writes_audio_source_diagnostic():
    assert "function write_selected_audio_source_diagnostic" in GENERATOR_CORE_SOURCE
    assert 'json_path .. ".audio_source.json"' in GENERATOR_CORE_SOURCE
    assert '"sources": [' in GENERATOR_CORE_SOURCE
    assert '"source_start_seconds"' in GENERATOR_CORE_SOURCE
    assert '"source_end_seconds"' in GENERATOR_CORE_SOURCE
    assert '"linked_offset_samples"' in GENERATOR_CORE_SOURCE
    assert '"audio_mapping_muted"' in GENERATOR_CORE_SOURCE
    assert '"audio_source_mode"' in GENERATOR_CORE_SOURCE
    assert '"timeline_mix_audio_path"' not in GENERATOR_CORE_SOURCE
    assert '"render_start_frame"' not in GENERATOR_CORE_SOURCE
    assert '"render_end_frame"' not in GENERATOR_CORE_SOURCE
    assert '"track_restore_ok"' not in GENERATOR_CORE_SOURCE
    assert "write_selected_audio_source_diagnostic(audio_diag_path, selected_audio_sources, scope, source_optimization)" in GENERATOR_CORE_SOURCE


def test_sync_script_installs_generate_selection_subplugin():
    assert "SOURCE_GENERATOR_LUA" in SYNC_SCRIPT
    assert "生成选区字幕.lua" in SYNC_SCRIPT
    assert "cp \"$SOURCE_GENERATOR_LUA\"" in SYNC_SCRIPT
    assert '"$SUBFIX_MENU_DIR/SubFix.lua"' in SYNC_SCRIPT
    assert '"$SUBFIX_MENU_DIR/生成选区字幕.lua"' in SYNC_SCRIPT
    assert '"$SUBFIX_MENU_DIR/SubFix_GenerateSelectionSubtitles.lua"' in SYNC_SCRIPT
    assert '"$RESOLVE_DIR/SubFix_GenerateSelectionSubtitles.lua"' in SYNC_SCRIPT


def test_backup_and_undo_snapshots_include_work_scope_metadata():
    backup_body = body_between(
        "function DoBackup",
        "local function persist_timeline_update_backup",
    )
    snapshot_body = body_between(
        "function build_snapshot_record",
        "function update_undo_redo_button_states",
    )
    manifest_write_body = body_between(
        "function write_backup_manifest_entries",
        "function refresh_backup_history_cache",
    )
    manifest_read_body = body_between(
        "function load_backup_manifest_records",
        "function write_backup_manifest_entries",
    )

    assert "clone_work_scope(current_work_scope)" in snapshot_body
    assert "scope_mode" in backup_body
    assert "scope_start_frame" in backup_body
    assert "scope_end_frame" in backup_body
    assert "scope_start_tc" in manifest_write_body
    assert "scope_end_tc" in manifest_write_body
    assert "#fields >= 11" in manifest_read_body


def test_work_scope_ui_does_not_add_visible_layout_labels():
    source = SOURCE

    assert 'ID = "WorkScopeLabel"' not in source
    assert 'ID = "MiniWorkScopeLabel"' not in source
    assert "选区字幕数量" not in source
    assert 'return string.format("选区｜%d 条", count)' in source
    assert 'return string.format("全片｜%d 条", count)' in source
    load_label_body = body_between(
        "local function set_load_status_label",
        "local function set_subtitle_loaded_state",
    )
    assert 'html_text:find("全片｜", 1, true)' in load_label_body
    assert 'html_text:find("选区｜", 1, true)' in load_label_body
    assert "✅ 已加载" in load_label_body


def test_selection_composite_reuses_full_srt_timeline_start_logic():
    selection_body = body_between(
        "function update_timeline_selection_scope",
        "update_timeline = function()",
    )
    write_body = body_between(
        "function write_rows_to_update_srt",
        "function update_timeline_selection_scope",
    )

    assert "build_selection_composite_rows" in selection_body
    assert "current_rows = composite_rows" in selection_body
    assert "update_timeline()" in selection_body
    assert "timeline:SetCurrentTimecode(frames_to_timecode(current_work_scope.start_frame, fps))" not in selection_body
    assert "local srt_base_frame = tonumber(base_frame)" in write_body
    assert "rel_start = math.max(0, start_f - srt_base_frame)" in write_body
    assert "rel_end = math.max(0, end_f - srt_base_frame)" in write_body


def test_selection_update_composes_full_track_and_reuses_full_update_flow():
    selection_body = body_between(
        "function update_timeline_selection_scope",
        "update_timeline = function()",
    )

    assert "safe_writeback_supported = false" in SOURCE
    assert "build_selection_composite_rows" in SOURCE
    assert "合成整轨写回" in selection_body
    assert "current_rows = composite_rows" in selection_body
    assert "current_work_scope = build_default_work_scope()" in selection_body
    assert "update_timeline()" in selection_body
    assert "current_rows = original_rows" in selection_body
    assert "current_work_scope = original_scope" in selection_body
    assert "mediaPool:ImportMedia({srt_path})" not in selection_body
    assert "append_srt_to_timeline_with_clipinfo" not in selection_body
    assert "mediaPool:AppendToTimeline({mediaPoolItem})" in SOURCE


def test_selection_update_logs_build_and_empty_mark_in_out_failure_to_file():
    selection_body = body_between(
        "function update_timeline_selection_scope",
        "update_timeline = function()",
    )

    assert "SUBFIX_SCRIPT_BUILD" in SOURCE
    assert "SubFix_debug.log" in SOURCE
    assert "append_subfix_debug_log_line(line)" in SOURCE
    assert "脚本版本 " in selection_body
    assert "Resolve 未返回有效 In/Out，未写回；请重新设置 I/O 后刷新字幕" in selection_body


def test_batch_replace_forces_tree_rebuild_after_current_rows_mutation():
    replace_body = body_between(
        "local function do_replace()",
        "function work_scopes_match",
    )

    assert "invalidate_search_cache(\"batch_replace\")" in replace_body
    assert "SEARCH_VIEW.render_current_view(win, {force_rebuild = true})" in replace_body
    assert replace_body.index("data.text = new_text") < replace_body.index("SEARCH_VIEW.render_current_view(win, {force_rebuild = true})")


def test_selection_mismatch_diagnostic_reports_delta_and_srt_first_time():
    source = SOURCE
    diagnostic_body = body_between(
        "function build_selection_writeback_diagnostic",
        "function collect_selection_overlapping_items",
    )

    assert "parse_first_srt_timing_line" in source
    assert "first_srt_start" in diagnostic_body
    assert "delta" in diagnostic_body
    assert "playhead_timecode" in diagnostic_body
    assert "timeline_start_frame" in diagnostic_body
    assert "mark_raw" in diagnostic_body


def test_normalize_length_progress_uses_inline_status():
    assert "NormalizeProgress" in SOURCE
    assert "start_normalize_progress" in SOURCE
    assert "finish_normalize_progress" in SOURCE
    assert "cancel_normalize_progress" in SOURCE
    assert "render_normalize_progress_status" in SOURCE
    assert "show_long_task_progress_window" in SOURCE
    assert "update_long_task_progress_window" in SOURCE

    step3_body = body_between(
        "function win.On.BtnStep3.Clicked",
        "-- 1️⃣ 修改英文排版",
    )
    normalize_body = body_between(
        "function run_normalize_subtitle_length",
        "-- 1️⃣ 修改英文排版",
    )

    assert "start_normalize_progress(window" in normalize_body
    assert "finish_normalize_progress(" in normalize_body
    assert "cancel_normalize_progress" in SOURCE
    normalize_window_body = body_between(
        "function show_normalize_progress_window(target_window)",
        "function start_normalize_progress(target_window, total_rows)",
    )
    assert "show_long_task_progress_window" in normalize_window_body
    assert 'title = "SubFix · 规整字幕长度"' in normalize_window_body
    assert "NormalizeProgress.window = progress_state.window" in normalize_window_body
    assert 'ID = "NormalizeProgressLog"' not in normalize_window_body
    assert "NormalizeProgressCloseBtn" not in normalize_window_body
    assert "NormalizeProgressAudioLabel" not in normalize_window_body
    assert "ui:TextEdit" not in normalize_window_body
    assert "render_normalize_progress_status()" in SOURCE
    assert "update_shared_status(NormalizeProgress.target_window, status_text)" in SOURCE
    assert '"规整字幕长度｜批次 %d/%d｜用时 %s"' in SOURCE
    assert '"规整字幕长度｜%s｜批次 %d/%d｜字幕 %d/%d｜用时 %s"' not in SOURCE
    render_body = body_between(
        "function render_normalize_progress_status()",
        "function append_normalize_progress_log(message)",
    )
    assert "字幕 %d/%d" not in render_body
    assert "NormalizeProgress.running == false" in render_body
    assert "update_shared_status(NormalizeProgress.target_window, NormalizeProgress.message)" in render_body
    assert "NormalizeProgress.stage" not in render_body
    assert 'progress_index = 98, progress_total = 100' in normalize_body
    assert "点“更新时间线”写回" in normalize_body
    assert "set_normalize_action_running(true)" in SOURCE
    assert "set_normalize_action_running(false)" in SOURCE
    assert "local function set_normalize_action_running" not in SOURCE
    assert "function set_normalize_action_running" in SOURCE
    assert 'item.Text = running and "取消规整" or "规整字幕长度"' in SOURCE
    assert "NormalizeProgress and NormalizeProgress.running" in step3_body
    assert 'cancel_normalize_progress("用户取消规整字幕长度")' in step3_body


def test_lua_main_chunk_local_slots_stay_within_lua51_limit():
    slots = 0
    for line in SOURCE.splitlines():
        if not line.startswith("local "):
            continue
        declaration = line[len("local ") :].strip()
        if declaration.startswith("function "):
            slots += 1
            continue
        names = declaration.split("=", 1)[0]
        slots += len([name for name in names.split(",") if name.strip()])

    assert slots <= 199


def test_ctc_alignment_uses_background_runner_with_progress_and_cancel():
    ctc_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.run_ctc_text_alignment",
        "function SUBFIX_AUDIO_ALIGN.run_lightweight_onset_detection",
    )
    runner_body = body_between(
        "function run_subfix_background_command",
        "local function parse_fps",
    )

    assert "run_shell_capture(table.concat(cmd_parts" not in ctc_body
    assert "run_subfix_background_command" in ctc_body
    assert '"--progress-json"' in ctc_body
    assert "progress_path" in ctc_body
    assert "pid_file" in runner_body
    assert "done_file" in runner_body
    assert "progress_file" in runner_body
    assert "payload.batch_index" in runner_body
    assert "payload.total_batches" in runner_body
    assert 'tostring(payload.batch_index or "") .. "/" .. tostring(payload.total_batches or "")' in runner_body
    assert "dispatcher:RunLoop()" in runner_body
    assert "kill_normalize_background_process" in runner_body
    assert "pkill -P" in SOURCE
    assert "NORMALIZE_CANCEL_REQUESTED" in runner_body


def test_qwen_batch_alignment_uses_single_helper_process_without_legacy_fallback():
    batch_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.run_qwen_forced_alignment_batches",
        "function SUBFIX_AUDIO_ALIGN.run_ctc_text_alignment_batches_fallback",
    )

    assert '"--batch-plan-json"' in batch_body
    assert '"qwen_forced_align_text_batches"' in batch_body
    assert "run_subfix_background_command" in batch_body
    assert "run_ctc_text_alignment_batches_fallback" not in batch_body


def test_qwen_batch_alignment_keeps_successful_batches_when_one_batch_fails():
    batch_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.run_qwen_forced_alignment_batches",
        "function SUBFIX_AUDIO_ALIGN.run_ctc_text_alignment_batches_fallback",
    )

    assert "local has_batch_payloads" in batch_body
    assert "if payload.ok == false and not has_batch_payloads then" in batch_body
    assert "if not ok and not has_batch_payloads then" in batch_body
    assert batch_body.index("if payload.ok == false and not has_batch_payloads then") < batch_body.index("local summary = {")
    assert batch_body.index("if not ok and not has_batch_payloads then") < batch_body.index("local summary = {")
    assert "if batch_payload.ok == true" in batch_body
    assert "reference_err = tostring(batch_payload.error" in batch_body


def test_normalize_alignment_propagates_progress_context_and_cancel():
    apply_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.apply_protected_audio_alignment_for_gap_fill",
        "function SUBFIX_AUDIO_ALIGN.apply_lightweight_audio_alignment_for_gap_fill",
    )
    batch_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.run_stable_ts_alignment_batches",
        "function SUBFIX_AUDIO_ALIGN.run_text_alignment_batches",
    )

    assert "progress = normalize_progress" in apply_body
    assert "options.progress" in batch_body
    assert "is_normalize_progress_cancelled" in batch_body
    assert "return nil, summary" in batch_body
    assert "cancelled" in apply_body


def test_audio_time_mapping_uses_source_start_and_timeline_start():
    body = body_between(
        "function SUBFIX_AUDIO_ALIGN.map_speech_segments_to_timeline",
        "function SUBFIX_AUDIO_ALIGN.detect_speech_segments",
    )

    assert "source_start_seconds" in body
    assert "audio_source.start_frame" in body
    assert "segment_start - source_start" in body
    assert "* fps" in body


def test_empty_mapping_uses_item_start_fallback_and_diagnostics():
    body = body_between(
        "function SUBFIX_AUDIO_ALIGN.map_speech_segments_to_timeline_fallback",
        "function SUBFIX_AUDIO_ALIGN.detect_speech_segments",
    )
    detect_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.detect_speech_segments",
        "function SUBFIX_AUDIO_ALIGN.match_rows_to_speech_segments",
    )

    assert "audio_source.start_frame" in body
    assert "segment_start_seconds * fps" in body
    assert "mapping_mode = \"item_start_fallback\"" in detect_body
    assert "diagnostic" in detect_body


def test_removed_auto_align_entry_cannot_mutate_rows():
    run_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.run(target_window)",
        "-- ========== 刷新字幕列表 ==========",
    )
    assert "SUBFIX_AUDIO_ALIGN.apply_matches" not in run_body
    assert "return false" in run_body


def test_normalize_length_uses_primary_track_batches():
    run_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.run(target_window)",
        "-- ========== 刷新字幕列表 ==========",
    )
    batch_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.find_primary_audio_track_batches",
        "function SUBFIX_AUDIO_ALIGN.parse_ffmpeg_duration",
    )
    copy_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.copy_row_for_audio_source",
        "function SUBFIX_AUDIO_ALIGN.find_primary_audio_track_batches",
    )
    batch_run_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.run_stable_ts_alignment_batches",
        "function SUBFIX_AUDIO_ALIGN.format_frame_range",
    )

    assert "find_primary_audio_track_batches" not in run_body
    assert "run_stable_ts_alignment_batches" not in run_body
    assert "track_info.overlap_frames" in batch_body
    assert "copy_row_for_audio_source" in batch_body
    assert "source_row_ref" in copy_body
    assert "未找到覆盖此字幕的有效对白音频" in batch_run_body
    assert "stable_ts_primary_track_batches_gapless" in batch_run_body


def test_pre_delivery_speech_consistency_prefers_timeline_audio_item_then_falls_back_to_mix():
    speech_body = body_between(
        "function collect_pre_delivery_speech_consistency_issues",
        "function format_pre_delivery_issue_report_entries",
    )
    selector_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.find_speech_consistency_audio_track_batches",
        "function collect_pre_delivery_speech_consistency_issues",
    )

    render_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.render_timeline_audio_mix_for_speech_check",
        "function SUBFIX_AUDIO_ALIGN.map_speech_segments_to_timeline",
    )

    assert "function SUBFIX_AUDIO_ALIGN.find_speech_consistency_audio_track_batches(timeline, rows, fps, options)" in SOURCE
    assert "score_speech_consistency_audio_track_candidate" in selector_body
    assert "probe_speech_consistency_audio_track_candidate" in selector_body
    assert "speech_reliable_count" in selector_body
    assert "speech_hallucination_count" in selector_body
    assert "speech_empty_count" in selector_body
    assert "find_speech_consistency_audio_track_batches(timeline, row_list, rate" in speech_body
    assert "batch_plan.audio_source_mode = \"timeline_audio_item\"" in speech_body
    assert "时间线音频 item 不可信，准备导出 timeline mix" in speech_body
    assert "render_timeline_audio_mix_for_speech_check(timeline, row_list, rate" in speech_body
    assert "audio_source_mode = \"timeline_mix\"" in speech_body
    assert "timeline_audio_mix = true" in render_body
    assert "TargetDir = target_dir" in render_body
    assert "MarkIn = render_start" in render_body
    assert "MarkOut = mark_out" in render_body
    assert "project:SetRenderSettings" in render_body
    assert "project:AddRenderJob()" in render_body
    assert "wait_for_timeline_render_job(project, job_id" in render_body
    assert "convert_rendered_audio_to_mono_wav" in render_body
    assert "source_start_seconds = 0" in render_body
    assert "find_primary_audio_track_batches(timeline, row_list, rate)" not in speech_body


def test_pre_delivery_speech_consistency_fails_closed_when_audio_item_and_mix_both_fail():
    speech_body = body_between(
        "function collect_pre_delivery_speech_consistency_issues",
        "function format_pre_delivery_issue_report_entries",
    )
    final_check_body = body_between(
        "function run_pre_delivery_final_check",
        "-- [6] 敏感词替换",
    )

    assert "if not batch_plan then" in speech_body
    assert "if not timeline_audio_source then" in speech_body
    assert "speech_check_skipped = true" in speech_body
    assert 'update_shared_status(window, "口播一致性跳过：无法导出时间线音频")' in speech_body
    assert "return issues, {" in speech_body
    assert "find_speech_consistency_audio_track_batches" in speech_body
    assert "口播一致性跳过" not in final_check_body


def test_normalize_length_splits_large_batches_for_accuracy():
    split_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.split_alignment_batch_plan_for_accuracy",
        "function SUBFIX_AUDIO_ALIGN.parse_ffmpeg_duration",
    )
    protected_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.apply_protected_audio_alignment_for_gap_fill",
        "function SUBFIX_AUDIO_ALIGN.apply_lightweight_audio_alignment_for_gap_fill",
    )
    run_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.run(target_window)",
        "-- ========== 刷新字幕列表 ==========",
    )

    assert "alignment_max_rows_per_batch" in SOURCE
    assert "alignment_max_batch_seconds" in SOURCE
    assert "alignment_max_chars_per_batch" in SOURCE
    assert "alignment_batch_context_frames" in SOURCE
    assert "count_utf8_chars" in split_body
    assert "exceeds_chars" in split_body
    assert "review_audio_source_for_window" in split_body
    assert "copy_row_for_audio_source" in split_body
    assert "accuracy_split_enabled" in split_body
    assert "batch_plan = SUBFIX_AUDIO_ALIGN.split_alignment_batch_plan_for_accuracy(batch_plan, fps)" in protected_body
    assert protected_body.index("split_alignment_batch_plan_for_accuracy") < protected_body.index("run_qwen_forced_alignment_batches")
    assert "split_alignment_batch_plan_for_accuracy" not in run_body
    assert "Qwen3 对齐返回无效时间戳" in SOURCE
    assert "CTC 返回 %d 条" not in SOURCE


def test_unmatched_batch_rows_preserve_original_end():
    fill_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.fill_gaps_after_match",
        "function SUBFIX_AUDIO_ALIGN.apply_matches",
    )

    assert "result and result.matched == false" in fill_body
    assert "row.end_frame = tonumber(result.old_end_frame)" in fill_body


def test_audio_align_gap_fill_uses_same_threshold_as_gap_tool():
    fill_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.fill_gaps_after_match",
        "function SUBFIX_AUDIO_ALIGN.apply_matches",
    )
    normalize_body = body_between(
        "function run_normalize_subtitle_length",
        "-- 1️⃣ 修改英文排版",
    )

    assert "math.floor(fps * 2 + 0.5)" in fill_body
    assert "gap > 0 and gap <= gap_threshold" in fill_body
    assert "row.end_frame = current_end" in fill_body
    assert "math.floor(fps * 2 + 0.5)" in normalize_body


def test_audio_align_has_local_onset_correction_after_stable_ts():
    source = SOURCE
    batch_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.run_stable_ts_alignment_batches",
        "function SUBFIX_AUDIO_ALIGN.format_frame_range",
    )
    align_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.run_stable_ts_alignment",
        "function SUBFIX_AUDIO_ALIGN.unmatched_result_for_row",
    )
    match_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.match_rows_to_stable_ts",
        "function SUBFIX_AUDIO_ALIGN.show_report",
    )

    assert "SUBFIX_AUDIO_ALIGN.local_onset_pullback_frames = 8" in source
    assert "SUBFIX_AUDIO_ALIGN.local_onset_push_frames = 2" in source
    assert "local_onset_frames" in batch_body
    assert "map_stable_ts_onsets_to_frames" in align_body
    assert "correct_start_with_local_onset" in match_body
    assert "onset_corrected" in match_body
    assert "local_onset_pullback_frames" in match_body
    assert "local_onset_push_frames" in match_body


def test_audio_align_applies_bias_after_local_onset_correction():
    match_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.match_rows_to_stable_ts",
        "function SUBFIX_AUDIO_ALIGN.show_report",
    )

    assert "corrected_start_frame + bias_frames" in match_body
    assert "stable_ts_start_frame" in match_body
    assert "local_onset_delta_frames" in match_body
    assert "stable_ts_start_frame + bias_frames" in match_body
    assert "onset_corrected = false" in match_body


def test_audio_align_guards_large_stable_ts_moves():
    match_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.match_rows_to_stable_ts",
        "function SUBFIX_AUDIO_ALIGN.show_report",
    )
    source = SOURCE

    assert "SUBFIX_AUDIO_ALIGN.max_stable_ts_move_frames = 18" in source
    assert "stable_ts_move_frames" in match_body
    assert "max_stable_ts_move_frames" in match_body
    assert "stable_ts_large_move_preserved" in match_body
    assert "stable_ts_large_move" in match_body


def test_gap_fill_runs_protected_audio_alignment_without_lightweight_fallback():
    normalize_body = body_between(
        "function run_normalize_subtitle_length",
        "-- 1️⃣ 修改英文排版",
    )
    protected_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.apply_protected_audio_alignment_for_gap_fill",
        "function SUBFIX_AUDIO_ALIGN.apply_lightweight_audio_alignment_for_gap_fill",
    )
    light_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.apply_lightweight_audio_alignment_for_gap_fill",
        "function SUBFIX_AUDIO_ALIGN.fill_gaps_after_match",
    )

    assert "apply_protected_audio_alignment_for_gap_fill" in normalize_body
    assert "apply_lightweight_audio_alignment_for_gap_fill" not in normalize_body
    assert "audio_aligned_count" in normalize_body
    assert "音频修正" in normalize_body
    assert "保持原位" in normalize_body
    assert "run_qwen_forced_alignment_batches" in protected_body
    assert "run_whisperx_text_alignment_batches" not in protected_body
    assert "run_text_alignment_batches" not in protected_body
    assert "apply_matches" not in protected_body
    assert "run_lightweight_onset_detection" in light_body
    assert "lightweight_onset_window_frames = 6" in SOURCE
    assert "run_stable_ts_alignment" not in light_body
    assert "stable_whisper" not in light_body
    assert "轻量音频对齐失败，继续执行纯消除空隙" in light_body


def test_qwen3_cpp_alignment_engine_is_env_gated_for_normalize_length():
    qwen_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.run_qwen_forced_alignment_batches",
        "function SUBFIX_AUDIO_ALIGN.apply_ctc_review_results",
    )
    engine_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.resolve_normalize_align_engine",
        "function SUBFIX_AUDIO_ALIGN.asr_temp_output_path",
    )
    diagnostic_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.write_gap_fill_alignment_diagnostics",
        "function SUBFIX_AUDIO_ALIGN.run_stable_ts_alignment_batches",
    )
    record_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.build_gap_fill_alignment_diagnostic_record",
        "function SUBFIX_AUDIO_ALIGN.write_gap_fill_alignment_diagnostics",
    )

    assert 'os.getenv("SUBFIX_ALIGN_ENGINE")' in engine_body
    assert "resolve_normalize_align_engine(paths)" in qwen_body
    assert 'return "qwen3_cpp"' in engine_body
    assert 'return "ctc"' not in engine_body
    assert "qwen3_cpp_available(paths)" in engine_body
    assert "qwen3-asr.cpp/build/qwen3-asr-cli" in SOURCE
    assert '"/bin/qwen3-asr-cli"' in SOURCE
    assert "qwen3-forced-aligner-0.6b-f16.gguf" in SOURCE
    assert '"qwen_forced_align_text_batches"' in qwen_body
    assert 'mapping_mode = "qwen3_forced_align_text_batches"' in qwen_body
    assert '"ctc_align_text_batches"' not in qwen_body
    assert "align_engine" in qwen_body
    assert "qwen_item_count" in qwen_body
    assert "qwen_output_path" in qwen_body
    assert "qwen_start_frame" in record_body
    assert "qwen_end_frame" in record_body
    assert "move_frames" in record_body
    assert "row_remap_score" in record_body
    assert "row_remap_decision" in record_body
    assert "remap_text_candidate" in record_body
    assert '"align_engine"' in diagnostic_body
    assert '"qwen_item_count"' in diagnostic_body
    assert '"qwen_output_path"' in diagnostic_body
    assert '"row_remap_score"' in diagnostic_body
    assert '"row_remap_decision"' in diagnostic_body
    assert '"remap_text_candidate"' in diagnostic_body


def test_qwen3_alignment_writes_back_only_globally_validated_candidates():
    protected_start_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.protected_ctc_candidate_start",
        "function SUBFIX_AUDIO_ALIGN.protected_ctc_candidate_end",
    )
    protected_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.apply_protected_audio_alignment_for_gap_fill",
        "function SUBFIX_AUDIO_ALIGN.apply_lightweight_audio_alignment_for_gap_fill",
    )
    match_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.match_rows_to_stable_ts",
        "function SUBFIX_AUDIO_ALIGN.show_report",
    )

    assert "SUBFIX_AUDIO_ALIGN.normalize_length_qwen_remap_min_score = 0.86" in SOURCE
    assert "SUBFIX_AUDIO_ALIGN.normalize_length_qwen_remap_large_move_min_score = 0.92" in SOURCE
    assert "SUBFIX_AUDIO_ALIGN.normalize_length_qwen_display_lead_frames = 0" in SOURCE
    assert 'local alignment_mode = tostring(options.alignment_mode or "")' in protected_start_body
    assert 'alignment_mode == "qwen3_forced_aligner"' in protected_start_body
    assert 'return original_start, false, "preserved_qwen_timing"' in protected_start_body
    assert "alignment_mode = result.alignment_mode" in protected_body
    assert "row_remap_score = result.row_remap_score" in protected_body
    assert "row_remap_decision = result.row_remap_decision" in protected_body
    assert "qwen_remap_min_score = SUBFIX_AUDIO_ALIGN.normalize_length_qwen_remap_min_score" in protected_body
    assert "qwen_remap_large_move_min_score = SUBFIX_AUDIO_ALIGN.normalize_length_qwen_remap_large_move_min_score" in protected_body
    assert 'preserve_original_onset = normalize_start_mode == "conservative"' in protected_body
    assert 'require_onset_improvement = normalize_start_mode == "conservative"' in protected_body
    assert '(normalize_start_mode == "balanced" and not is_qwen_alignment)' not in protected_body
    assert 'local can_use_ctc_end = result.alignment_mode ~= "qwen3_forced_aligner"' in protected_body
    assert "row_remap_score = row_remap_score" in match_body
    assert "row_remap_decision = row_remap_decision" in match_body
    assert "remap_text_candidate = remap_text_candidate" in match_body


def test_qwen3_global_writeback_uses_only_validated_timing_candidates():
    protected_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.apply_protected_audio_alignment_for_gap_fill",
        "function SUBFIX_AUDIO_ALIGN.apply_lightweight_audio_alignment_for_gap_fill",
    )
    record_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.build_gap_fill_alignment_diagnostic_record",
        "function SUBFIX_AUDIO_ALIGN.write_gap_fill_alignment_diagnostics",
    )
    diagnostic_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.write_gap_fill_alignment_diagnostics",
        "function SUBFIX_AUDIO_ALIGN.run_stable_ts_alignment_batches",
    )

    assert "qwen_global_plan_by_row = SUBFIX_AUDIO_ALIGN.build_qwen_global_writeback_plan(results, rows, row_position, fps, normalize_bias_frames, cut_frames)" in protected_body
    assert "local qwen_global_plan = qwen_global_plan_by_row[row]" in protected_body
    assert "if qwen_global_plan then" in protected_body
    assert "result.qwen_global_writeback = can_move" in protected_body
    assert "result.qwen_global_next_start_frame = qwen_global_plan.next_qwen_start_frame" in protected_body
    assert "result.qwen_display_lead_frames = qwen_global_plan.qwen_display_lead_frames" in protected_body
    assert protected_body.index("local qwen_global_plan = qwen_global_plan_by_row[row]") < protected_body.index("protected_ctc_candidate_start")
    assert "qwen_global_writeback" in record_body
    assert "qwen_global_next_start_frame" in record_body
    assert "qwen_display_lead_frames" in record_body
    assert '"qwen_global_writeback"' in diagnostic_body
    assert '"qwen_global_next_start_frame"' in diagnostic_body
    assert '"qwen_display_lead_frames"' in diagnostic_body


def test_gap_fill_protected_alignment_uses_medium_move_limit_and_origin_guard():
    source = SOURCE
    protected_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.apply_protected_audio_alignment_for_gap_fill",
        "function SUBFIX_AUDIO_ALIGN.apply_lightweight_audio_alignment_for_gap_fill",
    )
    candidate_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.protected_audio_candidate_start",
        "function SUBFIX_AUDIO_ALIGN.unmatched_result_for_row",
    )
    batch_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.run_stable_ts_alignment_batches",
        "function SUBFIX_AUDIO_ALIGN.format_frame_range",
    )

    assert "SUBFIX_AUDIO_ALIGN.max_stable_ts_move_frames = 18" in source
    assert "SUBFIX_AUDIO_ALIGN.normalize_length_max_audio_move_frames = 24" in source
    assert "SUBFIX_AUDIO_ALIGN.normalize_length_max_ctc_move_frames = 90" in source
    assert "SUBFIX_AUDIO_ALIGN.min_ctc_confidence = 0.30" in source
    assert "SUBFIX_AUDIO_ALIGN.normalize_length_ctc_move_min_confidence = 0.55" in source
    assert "SUBFIX_AUDIO_ALIGN.normalize_length_ctc_large_move_frames = 12" in source
    assert "SUBFIX_AUDIO_ALIGN.normalize_length_ctc_large_move_min_confidence = 0.70" in source
    assert "SUBFIX_AUDIO_ALIGN.normalize_length_original_onset_guard_frames = 6" in source
    assert "SUBFIX_AUDIO_ALIGN.normalize_length_ctc_onset_override_min_confidence = 0.90" in source
    assert "SUBFIX_AUDIO_ALIGN.normalize_length_origin_guard_frames = 2" in source
    assert "SUBFIX_AUDIO_ALIGN.normalize_length_min_improvement_frames = 3" in source
    assert "SUBFIX_AUDIO_ALIGN.normalize_length_forward_search_frames = 24" in source
    assert "SUBFIX_AUDIO_ALIGN.normalize_length_backward_search_frames = 6" in source
    assert "SUBFIX_AUDIO_ALIGN.normalize_length_start_bias_frames = 0" in source
    assert "SUBFIX_AUDIO_ALIGN.normalize_length_ctc_min_onset_improvement_frames = 1" in source
    assert "normalize_length_candidate_pullback_frames" not in source
    assert "normalize_length_candidate_push_frames" not in source
    assert "normalize_length_max_stable_ts_move_frames = 120" not in source
    assert "max_stable_ts_move_frames = SUBFIX_AUDIO_ALIGN.normalize_length_max_ctc_move_frames" in protected_body
    assert "protected_ctc_candidate_start" in protected_body
    assert 'local normalize_start_mode = tostring(options.start_mode or "balanced")' in protected_body
    assert 'local ctc_min_confidence = normalize_start_mode == "aggressive"' in protected_body
    assert "or SUBFIX_AUDIO_ALIGN.normalize_length_ctc_move_min_confidence" in protected_body
    assert 'local ctc_large_move_frames = normalize_start_mode == "aggressive"' in protected_body
    assert "or SUBFIX_AUDIO_ALIGN.normalize_length_ctc_large_move_frames" in protected_body
    assert 'local ctc_large_move_min_confidence = normalize_start_mode == "aggressive"' in protected_body
    assert "or SUBFIX_AUDIO_ALIGN.normalize_length_ctc_large_move_min_confidence" in protected_body
    assert "min_confidence = ctc_min_confidence" in protected_body
    assert "large_move_frames = ctc_large_move_frames" in protected_body
    assert "large_move_min_confidence = ctc_large_move_min_confidence" in protected_body
    assert 'preserve_original_onset = normalize_start_mode == "conservative"' in protected_body
    assert '(normalize_start_mode == "balanced" and not is_qwen_alignment)' not in protected_body
    assert "local_onset_frames = result.local_onset_frames" in protected_body
    assert "nearest_onset_distance" in candidate_body
    assert "first_onset_after_frame" in candidate_body
    assert "nearest_onset_before_frame" in candidate_body
    assert "stable_candidate < original_start" in candidate_body
    assert "rejected_direction" in candidate_body
    assert "protected_audio_candidate_start" in protected_body
    assert "preserved_already_aligned" in protected_body
    assert "moved_forward_better" in protected_body
    assert "moved_backward_better" in protected_body
    assert "rejected_no_onset" in protected_body
    assert "rejected_large_move" in protected_body
    assert "rejected_not_better" in protected_body
    assert "candidate_pullback_frames" not in protected_body
    assert "candidate_push_frames" not in protected_body
    assert "options.max_stable_ts_move_frames" in batch_body


def test_normalize_length_rejects_weak_ctc_start_moves():
    protected_start_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.protected_ctc_candidate_start",
        "function SUBFIX_AUDIO_ALIGN.protected_ctc_candidate_end",
    )
    protected_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.apply_protected_audio_alignment_for_gap_fill",
        "function SUBFIX_AUDIO_ALIGN.apply_lightweight_audio_alignment_for_gap_fill",
    )

    assert "ctc_confidence < min_confidence" in protected_start_body
    assert "ctc_confidence > 0 and ctc_confidence < min_confidence" not in protected_start_body
    assert "move_distance > large_move_frames and ctc_confidence < large_move_min_confidence" in protected_start_body
    assert "nearest_onset_distance(original_start, local_onset_frames" in protected_start_body
    assert "nearest_onset_distance(ctc_candidate, local_onset_frames" in protected_start_body
    assert "require_onset_improvement and preserve_original_onset" in protected_start_body
    assert "candidate_onset_distance" in protected_start_body
    assert "onset_improvement < min_onset_improvement_frames" in protected_start_body
    assert "preserve_original_onset = options.preserve_original_onset ~= false" in protected_start_body
    assert "preserve_original_onset and move_distance > large_move_frames and original_onset_distance and ctc_confidence < onset_override_min_confidence" in protected_start_body
    assert protected_start_body.index("preserve_original_onset and move_distance > large_move_frames and original_onset_distance and ctc_confidence < onset_override_min_confidence") < protected_start_body.index("if ctc_confidence < min_confidence")
    assert "local can_apply_user_bias = should_move or (normalize_bias_mode ~= \"auto\" and decision == \"preserved_already_aligned\")" in protected_body


def test_gap_fill_audio_source_uses_resolve_linked_audio_mapping():
    source_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.audio_source_from_item",
        "function SUBFIX_AUDIO_ALIGN.copy_row_for_audio_source",
    )
    plan_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.write_ctc_batch_plan_json",
        "function SUBFIX_AUDIO_ALIGN.run_stable_ts_alignment",
    )
    record_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.build_gap_fill_alignment_diagnostic_record",
        "function SUBFIX_AUDIO_ALIGN.write_gap_fill_alignment_diagnostics",
    )

    assert "function SUBFIX_AUDIO_ALIGN.parse_source_audio_channel_mapping" in SOURCE
    assert "GetSourceAudioChannelMapping" in SOURCE
    assert "parse_source_audio_channel_mapping(item, media_item" in source_body
    assert "linked_audio" in SOURCE
    assert "track_mapping" in SOURCE
    assert "linked_offset_samples" in SOURCE
    assert "linked_local_channel_index" in SOURCE
    assert "remaining_channel = remaining_channel - candidate_channels" in SOURCE
    assert "audio_mapping_source = \"linked_audio\"" in SOURCE
    assert "audio_mapping_source = \"media_pool_file\"" in SOURCE
    assert "audio_mapping_fallback_reason" in SOURCE
    assert "audio_channel_index" in source_body
    assert "audio_channel_index = tonumber(audio_source.audio_channel_index)" in plan_body
    assert "audio_mapping_source = tostring(audio_source.audio_mapping_source or \"\")" in plan_body
    assert "linked_offset_samples = tonumber(audio_source.linked_offset_samples)" in plan_body
    assert "audio_mapping_source" in record_body
    assert "resolved_audio_path" in record_body
    assert "audio_channel_index" in record_body


def test_gap_fill_rejects_isolated_neighbor_gap_outliers():
    source = SOURCE
    protected_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.apply_protected_audio_alignment_for_gap_fill",
        "function SUBFIX_AUDIO_ALIGN.apply_lightweight_audio_alignment_for_gap_fill",
    )
    guard_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.reject_neighbor_gap_outlier",
        "function SUBFIX_AUDIO_ALIGN.unmatched_result_for_row",
    )
    record_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.build_gap_fill_alignment_diagnostic_record",
        "function SUBFIX_AUDIO_ALIGN.write_gap_fill_alignment_diagnostics",
    )

    assert "normalize_length_neighbor_original_gap_frames = 8" in source
    assert "normalize_length_neighbor_max_gap_frames = 36" in source
    assert "function SUBFIX_AUDIO_ALIGN.reject_neighbor_gap_outlier" in source
    assert "original_prev_gap <= original_gap_limit" in guard_body
    assert "candidate_prev_gap > max_new_gap" in guard_body
    assert "original_next_gap <= original_gap_limit" in guard_body
    assert "candidate_next_gap > max_new_gap" in guard_body
    assert "rejected_neighbor_gap" in guard_body
    assert "reject_neighbor_gap_outlier" in protected_body
    assert "decision = \"rejected_neighbor_gap\"" in protected_body
    assert "neighbor_gap_outlier" in record_body
    assert "邻接空洞" in protected_body


def test_gap_fill_applies_start_bias_before_order_guards():
    protected_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.apply_protected_audio_alignment_for_gap_fill",
        "function SUBFIX_AUDIO_ALIGN.apply_lightweight_audio_alignment_for_gap_fill",
    )

    bias_pos = protected_body.index("normalize_bias_frames")
    can_move_pos = protected_body.index("local can_move = new_start > 0")
    prev_end_guard_pos = protected_body.index("(not prev_end or new_start >= prev_end)")

    assert "start_bias_frames" in protected_body
    assert "result.start_bias_frames" in protected_body
    assert bias_pos < can_move_pos < prev_end_guard_pos
    assert "(not audio_start or new_start >= audio_start)" in protected_body
    assert "(not next_start or new_start < next_start)" in protected_body
    assert 'decision == "preserved_already_aligned"' in protected_body
    assert "can_apply_user_bias" in protected_body


def test_normalize_length_auto_bias_calibration_exists():
    source = SOURCE
    protected_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.apply_protected_audio_alignment_for_gap_fill",
        "function SUBFIX_AUDIO_ALIGN.apply_lightweight_audio_alignment_for_gap_fill",
    )

    assert "normalize_length_auto_bias_min_confidence" in source
    assert "normalize_length_auto_bias_max_abs_frames" in source
    assert "normalize_length_auto_bias_min_samples" in source
    assert "function SUBFIX_AUDIO_ALIGN.resolve_normalize_length_auto_bias" in source
    assert "table.sort(samples)" in source
    assert "return 0, #samples, true" in source
    assert "auto_bias_frames" in protected_body
    assert "auto_bias_sample_count" in protected_body
    assert "auto_bias_fallback" in protected_body
    assert "normalize_bias_mode == \"auto\"" in protected_body


def test_normalize_length_uses_ctc_end_for_protected_end_correction():
    source = SOURCE
    protected_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.apply_protected_audio_alignment_for_gap_fill",
        "function SUBFIX_AUDIO_ALIGN.apply_lightweight_audio_alignment_for_gap_fill",
    )

    assert "normalize_length_end_tail_padding_frames" in source
    assert "normalize_length_end_min_confidence" in source
    assert "function SUBFIX_AUDIO_ALIGN.protected_ctc_candidate_end" in source
    assert "result.ctc_end_frame" in protected_body
    assert "end_corrected_count" in protected_body
    assert "end_decision" in protected_body
    assert "new_end = corrected_end" in protected_body
    assert protected_body.index("result.biased_candidate_start_frame") < protected_body.index("protected_ctc_candidate_end")


def test_normalize_length_keeps_standard_gap_fill_after_audio_alignment():
    normalize_body = body_between(
        "function run_normalize_subtitle_length",
        "-- 1️⃣ 修改英文排版",
    )

    assert "normalize_length_audio_effective_gap_frames" not in SOURCE
    assert "local active_gap_threshold = gap_threshold" in normalize_body
    assert "gap <= active_gap_threshold" in normalize_body
    assert "curr.target_abs_frame = math.floor((curr.start_frame + curr.end_frame) / 2)" in normalize_body


def test_qwen_review_pass_for_suspicious_rows_uses_qwen_runner():
    source = SOURCE
    protected_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.apply_protected_audio_alignment_for_gap_fill",
        "function SUBFIX_AUDIO_ALIGN.apply_lightweight_audio_alignment_for_gap_fill",
    )

    assert "function SUBFIX_AUDIO_ALIGN.is_ctc_review_candidate" in source
    assert "function SUBFIX_AUDIO_ALIGN.build_ctc_review_batch_plan" in source
    assert "function SUBFIX_AUDIO_ALIGN.apply_ctc_review_results" in source
    assert "review_pass" in source
    assert "fast_pass" in source
    assert "review_adopted_count" in source
    assert "run_qwen_forced_alignment_batches(review_plan" in protected_body
    assert "review_rejected_reason" in source


def test_gap_fill_reports_forward_and_backward_audio_corrections():
    protected_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.apply_protected_audio_alignment_for_gap_fill",
        "function SUBFIX_AUDIO_ALIGN.apply_lightweight_audio_alignment_for_gap_fill",
    )

    assert "moved_forward_better" in protected_body
    assert "moved_backward_better" in protected_body
    assert "后移修正" in protected_body
    assert "前移修正" in protected_body


def test_gap_fill_rejects_candidate_inside_previous_subtitle():
    protected_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.apply_protected_audio_alignment_for_gap_fill",
        "function SUBFIX_AUDIO_ALIGN.apply_lightweight_audio_alignment_for_gap_fill",
    )

    assert "prev_end" in protected_body
    assert "(not prev_end or new_start >= prev_end)" in protected_body
    assert "prev_row.end_frame = math.max" not in protected_body
    assert "rejected_direction" in protected_body


def test_gap_fill_uses_qwen_alignment_and_reports_when_qwen_alignment_is_ineffective():
    normalize_body = body_between(
        "function run_normalize_subtitle_length",
        "-- 1️⃣ 修改英文排版",
    )
    protected_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.apply_protected_audio_alignment_for_gap_fill",
        "function SUBFIX_AUDIO_ALIGN.apply_lightweight_audio_alignment_for_gap_fill",
    )
    assert "run_qwen_forced_alignment_batches" in protected_body
    assert "run_whisperx_text_alignment_batches" not in protected_body
    assert "run_text_alignment_batches" not in protected_body
    assert "aligned_count == 0" in protected_body
    assert "Qwen3 对齐返回无效时间戳或没有安全候选" in protected_body
    assert "audio_alignment_effective" in normalize_body
    assert "仅规整空隙，Qwen3 对齐未生效" in normalize_body


def test_gap_fill_surfaces_batch_failure_diagnostics():
    protected_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.apply_protected_audio_alignment_for_gap_fill",
        "function SUBFIX_AUDIO_ALIGN.apply_lightweight_audio_alignment_for_gap_fill",
    )

    assert "first_diagnostic_line" in protected_body
    assert "reference_info.diagnostic" in protected_body
    assert "Qwen3 对齐未生效：批次全部失败或未覆盖字幕" in protected_body
    assert "diagnostic_suffix" in protected_body


def test_gap_fill_writes_per_row_alignment_diagnostics():
    source = SOURCE
    normalize_body = body_between(
        "function run_normalize_subtitle_length",
        "-- 1️⃣ 修改英文排版",
    )
    protected_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.apply_protected_audio_alignment_for_gap_fill",
        "function SUBFIX_AUDIO_ALIGN.apply_lightweight_audio_alignment_for_gap_fill",
    )
    record_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.build_gap_fill_alignment_diagnostic_record",
        "function SUBFIX_AUDIO_ALIGN.write_gap_fill_alignment_diagnostics",
    )
    write_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.write_gap_fill_alignment_diagnostics",
        "function SUBFIX_AUDIO_ALIGN.apply_protected_audio_alignment_for_gap_fill",
    )

    assert "function SUBFIX_AUDIO_ALIGN.build_gap_fill_alignment_diagnostic_record" in source
    assert "function SUBFIX_AUDIO_ALIGN.write_gap_fill_alignment_diagnostics" in source
    assert "diagnostic_records" in protected_body
    assert "build_gap_fill_alignment_diagnostic_record" in protected_body
    assert "write_gap_fill_alignment_diagnostics" in protected_body
    assert "diagnostic_json_path" in protected_body
    assert "diagnostic_csv_path" in protected_body
    assert "diagnostic_json_path" in normalize_body
    assert "诊断:" in normalize_body
    assert "old_start_frame" in record_body
    assert "old_end_frame" in record_body
    assert "stable_ts_candidate_frame" in record_body
    assert "ctc_end_frame" in record_body
    assert "final_start_frame" in record_body
    assert "final_end_frame" in record_body
    assert "end_decision" in record_body
    assert "previous_onset_frame" in record_body
    assert "next_onset_frame" in record_body
    assert "audio_item_start_frame" in record_body
    assert "audio_item_end_frame" in record_body
    assert "auto_bias_frames" in write_body
    assert "auto_bias_sample_count" in write_body
    assert "end_corrected_count" in write_body
    assert "already_aligned" in record_body
    assert "no_onset" in record_body
    assert "candidate_too_far" in record_body
    assert "direction_rejected" in record_body
    assert "order_blocked" in record_body
    assert ".json" in write_body
    assert ".csv" in write_body
    assert "io.open" in write_body


def test_gap_fill_skips_non_monotonic_candidates_instead_of_failing_batch():
    protected_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.apply_protected_audio_alignment_for_gap_fill",
        "function SUBFIX_AUDIO_ALIGN.apply_lightweight_audio_alignment_for_gap_fill",
    )
    match_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.match_rows_to_stable_ts",
        "function SUBFIX_AUDIO_ALIGN.show_report",
    )
    text_run_body = body_between(
        "function SUBFIX_AUDIO_ALIGN.run_text_alignment",
        "function SUBFIX_AUDIO_ALIGN.run_text_alignment_batches",
    )

    assert "allow_non_monotonic_candidates = true" in protected_body
    assert "allow_non_monotonic_candidates" in match_body
    assert "stable_ts_non_monotonic_candidate" in match_body
    assert "stable-ts 文本对齐返回非单调时间" not in text_run_body


if __name__ == "__main__":
    test_default_audio_align_no_longer_uses_resolve_auto_captions()
    test_audio_source_detection_uses_timeline_audio_items()
    test_ffmpeg_silencedetect_path_and_parsing_are_present()
    test_alignment_uses_stable_ts_starts_and_gapless_ends()
    test_alignment_defaults_to_stable_ts_forced_alignment()
    test_asr_helper_and_setup_are_wired()
    test_asr_ffmpeg_path_is_logged_and_actionable()
    test_audio_align_default_bias_starts_on_audio()
    test_audio_align_ui_removed_from_tool_panel()
    test_gap_fill_button_is_renamed_to_normalize_subtitle_length()
    test_refine_tool_buttons_use_merged_english_typography_and_final_check()
    test_gap_fill_button_shows_status_before_running_work()
    test_normalize_length_opens_config_dialog_and_passes_bias_frames()
    test_english_typography_dialog_contains_required_options_and_defaults()
    test_english_typography_combination_branches_are_independent()
    test_english_typography_uses_single_snapshot_and_final_report_entries()
    test_translation_batches_and_retries_missing_lines()
    test_text_batch_preview_refresh_avoids_full_rebuild_without_search()
    test_pre_delivery_final_check_marks_quality_issues_without_mutating_subtitles()
    test_pre_delivery_final_check_uses_video_cut_points_and_default_thresholds()
    test_work_scope_reads_timeline_mark_in_out_and_defaults_to_full()
    test_work_scope_accepts_single_sided_timeline_marks()
    test_selection_intersection_uses_boundary_overlap_rule()
    test_selection_update_composes_then_delegates_to_full_track_rebuild()
    test_full_timeline_update_still_uses_whole_track_rebuild()
    test_audio_time_mapping_uses_source_start_and_timeline_start()
    test_empty_mapping_uses_item_start_fallback_and_diagnostics()
    test_failure_paths_return_before_mutating_rows()
    test_audio_align_uses_primary_track_batches()
    test_audio_align_splits_large_batches_for_accuracy()
    test_unmatched_batch_rows_preserve_original_end()
    test_audio_align_gap_fill_uses_same_threshold_as_gap_tool()
    test_audio_align_has_local_onset_correction_after_stable_ts()
    test_audio_align_applies_bias_after_local_onset_correction()
    test_audio_align_guards_large_stable_ts_moves()
    test_normalize_length_rejects_weak_ctc_start_moves()
    test_gap_fill_runs_protected_audio_alignment_without_lightweight_fallback()
    test_qwen3_cpp_alignment_engine_is_env_gated_for_normalize_length()
    test_qwen3_remap_score_guards_start_writes()
    test_qwen3_global_writeback_resolves_adjacent_boundaries()
    test_gap_fill_protected_alignment_uses_medium_move_limit_and_origin_guard()
    test_gap_fill_rejects_isolated_neighbor_gap_outliers()
    test_gap_fill_uses_align_text_and_reports_when_audio_alignment_is_ineffective()
    test_gap_fill_surfaces_batch_failure_diagnostics()
    test_gap_fill_writes_per_row_alignment_diagnostics()
    test_normalize_length_auto_bias_calibration_exists()
    test_normalize_length_uses_ctc_end_for_protected_end_correction()
    test_normalize_length_keeps_standard_gap_fill_after_audio_alignment()
    test_gap_fill_skips_non_monotonic_candidates_instead_of_failing_batch()
    test_gap_fill_reports_forward_and_backward_audio_corrections()
    test_generate_selection_subtitles_dialog_has_no_program_type_dropdown_and_fixes_live_mode()
    test_generate_selection_subtitles_dialog_has_subtitle_length_buttons_defaulting_to_standard()
    test_generate_selection_live_mode_preserves_speaker_fields_and_skips_track_priority_merge()
    print("audio alignment static checks passed")
