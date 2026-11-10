from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_qwen_ready_status_is_checked_synchronously_before_showing_or_starting_install():
    source = (ROOT / ".subfix_support" / "subfix_generate_selection_core.lua").read_text(encoding="utf-8")

    dialog_start = source.index("local function open_qwen_download_dialog()")
    dialog_end = source.index("function qwen_download_window.On.GenerateQwenDownloadConfirmBtn.Clicked", dialog_start)
    dialog_body = source[dialog_start:dialog_end]
    assert "show_qwen_status(inspect_local_qwen(resolve_asr_paths()))" in dialog_body
    assert "GenerateQwenStatusPollTimer_" not in dialog_body

    assert '"__subfix_model_center__"' in source
    assert "local function open_model_center(focus_model_id)" in source
    assert "install_local_qwen_with_progress" not in source
