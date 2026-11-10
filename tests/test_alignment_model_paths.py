from pathlib import Path

import pytest

from test_normalize_length_options import run_lua  # noqa: F401


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "SubFix.lua").read_text(encoding="utf-8")
PATH_FUNCTIONS = SOURCE[SOURCE.index("function SUBFIX_AUDIO_ALIGN.get_asr_paths()"):SOURCE.index("function SUBFIX_AUDIO_ALIGN.asr_temp_output_path()")]
HARNESS = '''
    local env = {HOME = "/home/editor"}
    local files = {
        ["/bundle/.subfix_support/subfix_asr_transcribe.py"] = true,
        ["/bundle/.subfix_support/runtime/python/bin/python3"] = true,
        ["/bundle/.subfix_support/bin/qwen3-asr-cli"] = true
    }
    os.getenv = function(name) return env[name] end
    SUBFIX_AUDIO_ALIGN = {
        get_script_dir = function() return "/bundle/SubFix" end,
        resolve_support_root = function() return "/bundle" end,
        file_exists = function(path) return files[path] == true end
    }
'''


def test_fresh_install_alignment_uses_bundled_python(run_lua):
    run_lua(HARNESS + PATH_FUNCTIONS + '''
        local paths = SUBFIX_AUDIO_ALIGN.get_asr_paths()
        assert(paths.python == "/bundle/.subfix_support/runtime/python/bin/python3", paths.python)
    ''')


@pytest.mark.parametrize("data_root", ["/home/editor/Library/Application Support/SubFix", "/custom/models", "~/custom-models"])
def test_alignment_finds_model_center_data_directory(run_lua, data_root):
    expanded = data_root.replace("~", "/home/editor")
    run_lua(HARNESS + PATH_FUNCTIONS + f'''
        env.SUBFIX_DATA_ROOT = "{data_root}"
        local model = "{expanded}/models/qwen3-forced-aligner-0.6b-f16.gguf"
        files[model] = true
        local paths = SUBFIX_AUDIO_ALIGN.get_asr_paths()
        assert(SUBFIX_AUDIO_ALIGN.resolve_normalize_align_engine(paths) == "qwen3_cpp")
        assert(paths.aligner_model == model)
    ''')


def test_alignment_preserves_legacy_environment_and_model(run_lua):
    run_lua(HARNESS + PATH_FUNCTIONS + '''
        local python = "/bundle/.subfix_support/.subfix_asr_env/bin/python"
        local model = "/bundle/.subfix_support/models/qwen3-forced-aligner-0.6b-f16.gguf"
        files[python] = true; files[model] = true
        local paths = SUBFIX_AUDIO_ALIGN.get_asr_paths()
        assert(paths.python == python)
        assert(SUBFIX_AUDIO_ALIGN.qwen3_cpp_available(paths))
        assert(paths.aligner_model == model)
    ''')


def test_explicit_missing_aligner_does_not_fall_back_to_another_model(run_lua):
    run_lua(HARNESS + PATH_FUNCTIONS + '''
        files["/bundle/.subfix_support/models/qwen3-forced-aligner-0.6b-f16.gguf"] = true
        env.SUBFIX_QWEN3_ALIGNER_GGUF = "/missing.gguf"
        assert(not SUBFIX_AUDIO_ALIGN.qwen3_cpp_available(SUBFIX_AUDIO_ALIGN.get_asr_paths()))
    ''')


def test_alignment_command_passes_resolved_model_path(run_lua):
    start = SOURCE.index("function SUBFIX_AUDIO_ALIGN.run_qwen_forced_alignment_batches(")
    body = SOURCE[start:SOURCE.index("\nfunction ", start + 1)]
    run_lua(HARNESS + PATH_FUNCTIONS + body + '''
        local model = "/home/editor/Library/Application Support/SubFix/models/qwen3-forced-aligner-0.6b-f16.gguf"
        files[model] = true
        SUBFIX_AUDIO_ALIGN.resolve_ffmpeg_binary = function() return "/ffmpeg" end
        SUBFIX_AUDIO_ALIGN.asr_temp_output_path = function() return "/tmp/output.json" end
        SUBFIX_AUDIO_ALIGN.alignment_rows_temp_path = function() return "/tmp/batches.json" end
        SUBFIX_AUDIO_ALIGN.write_ctc_batch_plan_json = function() return true end
        SUBFIX_AUDIO_ALIGN.default_ctc_model = "unused"
        SUBFIX_AUDIO_ALIGN.default_asr_language = "zh"
        ensure_backup_directory = function() end
        shell_quote = function(value) return "'" .. tostring(value) .. "'" end
        run_subfix_background_command = function(cmd)
            assert(cmd:find("SUBFIX_QWEN3_ALIGNER_GGUF='" .. model .. "'", 1, true), cmd)
            assert(cmd:find("runtime/python/bin/python3", 1, true), cmd)
            return false, "cancelled", "cancelled"
        end
        local result, diagnostic = SUBFIX_AUDIO_ALIGN.run_qwen_forced_alignment_batches({batches = {{}}}, 25, 0, {})
        assert(result == nil and diagnostic.cancelled == true)
    ''')
