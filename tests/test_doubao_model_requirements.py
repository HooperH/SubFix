"""Cloud generation must work on an installation with no Qwen models."""

import json
from pathlib import Path

import pytest

from test_asr_helper import load_helper_module
from test_generate_progress_lifecycle import run_lua  # noqa: F401


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / ".subfix_support/subfix_generate_selection_core.lua").read_text()
BUILD_COMMAND = SOURCE[SOURCE.index("local function build_asr_helper_batch_command("):
                       SOURCE.index("local function kill_background_process(")]


@pytest.fixture
def command_builder(run_lua):
    run_lua('''
        paths = {helper = '/plugin/helper.py', python = '/qwen/python',
            runtime_python = '/plugin/python', diagnostic = '/tmp/diagnostic.json',
            plugin_source = '/plugin/main.lua', generation_core_source = '/plugin/core.lua'}
        present = {['/plugin/helper.py'] = true, ['/plugin/python'] = true}
        model_status = {ready = false, aligner = ''}
        function resolve_asr_paths() return paths end
        function inspect_local_qwen()
            assert(not forbid_model_inspection, 'cloud command inspected Qwen models')
            return model_status
        end
        function file_exists(path) return present[path] == true end
        function shell_quote(value) return "'" .. value .. "'" end
        function resolve_generate_engine() return 'v5' end
        function resolve_segmentation_profile_path() return nil end
        DEFAULT_ASR_BACKEND = 'auto'; DEFAULT_ASR_MODEL = 'model'; DEFAULT_ASR_LANGUAGE = 'zh'
    ''' + BUILD_COMMAND + '''
        function build(backend)
            return build_asr_helper_batch_command('/tmp/plan.json', '/tmp/out.srt',
                '/tmp/out.json', 0, 24, '/tmp/progress.json', 'live', 25, backend, nil)
        end
    ''')
    return run_lua


@pytest.mark.parametrize("backend", ["doubao_asr", "doubao_asr_v2"])
def test_cloud_command_uses_bundled_runtime_without_any_qwen_models(command_builder, backend):
    command_builder("backend = " + json.dumps(backend))
    command_builder('''
        forbid_model_inspection = true
        local command, err = build(backend)
        assert(command, err)
        assert(command:find("'/plugin/python' '/plugin/helper.py'", 1, true))
        assert(command:find("--backend '" .. backend .. "'", 1, true))
        assert(not command:find('SUBFIX_QWEN3_', 1, true))
        present['/plugin/python'] = false
        local missing, reason = build(backend)
        assert(missing == nil and reason:find('内置 Python 缺失', 1, true))
    ''')


@pytest.mark.parametrize("backend", ["auto", "qwen3_asr"])
def test_local_command_requires_aligner_and_asr_environment(command_builder, backend):
    command_builder("backend = " + json.dumps(backend))
    command_builder('''
        local command, err = build(backend)
        assert(command == nil and err:find('缺少对齐模型', 1, true))
        model_status.aligner = '/models/aligner.gguf'
        present[model_status.aligner] = true
        command, err = build(backend)
        assert(command == nil and err:find('本地模型尚未就绪', 1, true))
        model_status.ready = true; model_status.python = '/qwen/python'
        model_status.model = '/models/asr'; present[model_status.python] = true
        command, err = build(backend)
        assert(command, err)
        assert(command:find("SUBFIX_QWEN3_ALIGNER_GGUF='/models/aligner.gguf'", 1, true))
        assert(command:find("SUBFIX_QWEN3_ASR_MODEL='/models/asr'", 1, true))
        assert(command:find("--backend 'qwen3_asr'", 1, true))
    ''')


@pytest.mark.parametrize("backend", ["doubao_asr", "doubao_asr_v2"])
@pytest.mark.parametrize("timestamps", ["words", "segments", "missing"])
def test_cloud_alignment_never_loads_qwen(monkeypatch, tmp_path, backend, timestamps):
    helper = load_helper_module()
    monkeypatch.setattr(helper, "qwen3_force_align_items", lambda *_: pytest.fail("Qwen was invoked"))
    monkeypatch.setattr(helper, "audio_duration_seconds", lambda _: 1.0)
    segment = {"text": "测试", "start": 0.0, "end": 1.0}
    if timestamps == "words":
        segment["words"] = [{"word": "测试", "start": 0.0, "end": 1.0}]
    payload = {"backend": backend, "text": "测试",
               "segments": [] if timestamps == "missing" else [segment]}
    units, _ = helper.generate_v4.require_aligned_units(
        tmp_path / "audio.wav", payload,
        lambda path, text, language: helper.align_v4_retry_audio(path, text, language, backend=backend),
        "Chinese", 24, 0,
    )
    assert "".join(unit["text"] for unit in units) == "测试"
