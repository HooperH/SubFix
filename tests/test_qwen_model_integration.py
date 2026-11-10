import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def helper(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("subfix_asr_model_integration", ROOT / "subfix_asr_transcribe.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setenv("SUBFIX_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hf" / "hub"))
    monkeypatch.delenv("SUBFIX_QWEN3_ASR_MODEL", raising=False)
    monkeypatch.setattr(module, "qwen3_support_roots", lambda: [tmp_path / "legacy"])
    monkeypatch.setattr(module, "resolve_qwen3_cpp_paths", lambda: (Path("qwen3-asr-cli"), Path("aligner.gguf")))
    return module


def make_model(path):
    path.mkdir(parents=True)
    (path / "config.json").write_text('{"model_type":"qwen3_asr"}')
    (path / "model.safetensors").write_bytes(b"test-model")
    return path


def fake_model_module(monkeypatch, result=None):
    calls = []

    class Model:
        @classmethod
        def from_pretrained(cls, name, **kwargs):
            calls.append((name, kwargs))
            return cls()

        def transcribe(self, **kwargs):
            calls.append(kwargs)
            return result or [types.SimpleNamespace(text="测试", language="Chinese")]

    monkeypatch.setitem(sys.modules, "qwen_asr", types.SimpleNamespace(Qwen3ASRModel=Model))
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(bfloat16="bfloat16"))
    return calls


def test_selected_small_model_loads_locally_without_python_aligner(helper, monkeypatch, tmp_path):
    model = make_model(tmp_path / "data" / "models" / "qwen3-asr-0.6b")
    (tmp_path / "data" / "model-selection.json").write_text(json.dumps({"selected_model_id": "qwen3-asr-0.6b"}))
    calls = fake_model_module(monkeypatch)

    loaded, name, aligner, _device = helper.load_qwen3_asr_model("small")

    assert loaded is not None
    assert name == str(model)
    assert aligner == "aligner.gguf"
    assert calls[0][0] == str(model)
    assert calls[0][1]["local_files_only"] is True
    assert calls[0][1]["max_inference_batch_size"] == 1
    assert "forced_aligner" not in calls[0][1]
    assert "forced_aligner_kwargs" not in calls[0][1]


def test_explicit_missing_model_fails_before_package_load(helper, monkeypatch, tmp_path):
    calls = fake_model_module(monkeypatch)
    monkeypatch.setenv("SUBFIX_QWEN3_ASR_MODEL", str(tmp_path / "missing"))

    with pytest.raises(RuntimeError, match="模型中心"):
        helper.load_qwen3_asr_model("small")

    assert calls == []


def test_explicit_small_model_reuses_huggingface_cache_without_network(helper, monkeypatch, tmp_path):
    model = make_model(tmp_path / "hf" / "hub" / "models--Qwen--Qwen3-ASR-0.6B" / "snapshots" / "revision")
    monkeypatch.setenv("SUBFIX_QWEN3_ASR_MODEL", "Qwen/Qwen3-ASR-0.6B")
    calls = fake_model_module(monkeypatch)

    assert helper.load_qwen3_asr_model("small")[1] == str(model)
    assert calls[0][1]["local_files_only"] is True


def test_batch_transcription_restores_each_windows_word_timing(helper, monkeypatch, tmp_path):
    model = make_model(tmp_path / "data" / "models" / "qwen3-asr-1.7b")
    monkeypatch.setenv("SUBFIX_QWEN3_ASR_MODEL", str(model))
    calls = []
    results = iter(["甲", "乙"])

    def worker(path, requested_model, language, context=None):
        calls.append((path, requested_model, language, context))
        return {"text": next(results), "language": "Chinese", "model": str(model), "device_map": "cpu"}

    monkeypatch.setattr(helper, "run_qwen3_asr_worker", worker)
    aligned = []

    def align(path, text, language):
        aligned.append((path, text, language))
        return [{"text": text, "start": 0.12, "end": 0.4}]

    monkeypatch.setattr(helper, "qwen3_force_align_items", align)
    audio_paths = [tmp_path / "a.wav", tmp_path / "b.wav"]
    payloads = helper.transcribe_qwen3_asr_batch(audio_paths, "small", "zh")

    assert [call[0] for call in calls] == audio_paths
    assert len(aligned) == 2
    assert [entry[0] for entry in aligned] == audio_paths
    assert [payload["segments"][0]["words"][0]["word"] for payload in payloads] == ["甲", "乙"]
    assert all(payload["diagnostic"]["align_engine"] == "qwen3_cpp" for payload in payloads)


def test_missing_gguf_blocks_before_asr_weight_load(helper, monkeypatch, tmp_path):
    model = make_model(tmp_path / "data" / "models" / "qwen3-asr-0.6b")
    monkeypatch.setenv("SUBFIX_QWEN3_ASR_MODEL", str(model))
    calls = fake_model_module(monkeypatch)

    def missing():
        raise RuntimeError("请到模型中心下载对齐模型")

    monkeypatch.setattr(helper, "resolve_qwen3_cpp_paths", missing)
    with pytest.raises(RuntimeError, match="模型中心"):
        helper.load_qwen3_asr_model("small")
    assert calls == []


def test_empty_transcription_does_not_start_aligner(helper, monkeypatch, tmp_path):
    model = make_model(tmp_path / "data" / "models" / "qwen3-asr-0.6b")
    monkeypatch.setenv("SUBFIX_QWEN3_ASR_MODEL", str(model))
    monkeypatch.setattr(helper, "run_qwen3_asr_worker", lambda *_args: {"text": "", "language": "Chinese"})
    monkeypatch.setattr(helper, "qwen3_force_align_items", lambda *_args: pytest.fail("silent audio should not align"))

    payload = helper.transcribe_qwen3_asr(tmp_path / "silence.wav", "small", "zh")

    assert payload["text"] == ""
    assert payload["segments"] == []


def test_aligner_discovery_does_not_pick_incompatible_cstr_variant(helper, tmp_path):
    models = tmp_path / "legacy" / "models"
    models.mkdir(parents=True)
    (models / "qwen3-forced-aligner-0.6b-f16.cstr.gguf").write_bytes(b"GGUF")

    assert helper.discover_qwen3_aligner_model() is None


def test_selected_model_is_not_silently_replaced_by_other_installed_model(helper, monkeypatch, tmp_path):
    make_model(tmp_path / "data" / "models" / "qwen3-asr-1.7b")
    (tmp_path / "data" / "model-selection.json").write_text(json.dumps({"selected_model_id": "qwen3-asr-0.6b"}))
    calls = fake_model_module(monkeypatch)

    with pytest.raises(RuntimeError, match="模型中心"):
        helper.load_qwen3_asr_model("small")

    assert calls == []
