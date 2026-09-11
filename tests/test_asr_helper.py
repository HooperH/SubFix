import importlib.util
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "subfix_asr_transcribe.py"


def load_helper_module():
    spec = importlib.util.spec_from_file_location("subfix_asr_transcribe_test", HELPER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_qwen_asr_loader_does_not_initialize_python_forced_aligner(monkeypatch):
    helper = load_helper_module()
    calls = []

    class FakeQwen3ASRModel:
        @classmethod
        def from_pretrained(cls, model_name, **kwargs):
            calls.append((model_name, kwargs))
            return object()

    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(bfloat16="bfloat16"))
    monkeypatch.setitem(sys.modules, "qwen_asr", types.SimpleNamespace(Qwen3ASRModel=FakeQwen3ASRModel))
    helper.load_qwen3_asr_model("Qwen/Qwen3-ASR-1.7B")

    assert len(calls) == 1
    assert calls[0][1]["max_inference_batch_size"] == 1
    assert "forced_aligner" not in calls[0][1]
    assert "forced_aligner_kwargs" not in calls[0][1]


def test_qwen_asr_uses_local_alignment_when_native_word_timing_is_missing(monkeypatch, tmp_path):
    helper = load_helper_module()
    audio = tmp_path / "single.wav"
    audio.touch()
    aligned = []
    events = []
    monkeypatch.setattr(
        helper,
        "run_qwen3_asr_worker",
        lambda path, model, language, context=None: events.append("worker_exited") or {
            "text": "本地对齐", "language": None, "model": "asr", "device_map": "cpu",
            "hotword_context_status": "not_requested",
        },
    )
    monkeypatch.setattr(
        helper,
        "qwen3_force_align_items",
        lambda path, text, language: events.append("gguf_started") or aligned.append((path, text, language))
        or [
            {"text": "本地", "start": 0.10, "end": 0.30},
            {"text": "对齐", "start": 0.35, "end": 0.60},
        ],
    )

    payload = helper.transcribe_qwen3_asr(audio, "Qwen/Qwen3-ASR-1.7B", "zh")

    assert aligned == [(audio, "本地对齐", "Chinese")]
    assert events == ["worker_exited", "gguf_started"]
    assert payload["segments"] == [
        {
            "start": 0.10,
            "end": 0.60,
            "text": "本地对齐",
            "words": [
                {"word": "本地", "start": 0.10, "end": 0.30},
                {"word": "对齐", "start": 0.35, "end": 0.60},
            ],
        }
    ]
    assert payload["diagnostic"]["uses_word_timing"] is True


def test_qwen_asr_uses_detected_language_for_local_alignment_in_auto_mode(monkeypatch, tmp_path):
    helper = load_helper_module()
    audio = tmp_path / "english.wav"
    audio.touch()
    aligned = []

    monkeypatch.setattr(
        helper,
        "run_qwen3_asr_worker",
        lambda *_args, **_kwargs: {
            "text": "offline", "language": "English", "model": "asr", "device_map": "cpu",
            "hotword_context_status": "not_requested",
        },
    )
    monkeypatch.setattr(
        helper,
        "qwen3_force_align_items",
        lambda path, text, language: aligned.append((path, text, language))
        or [{"text": "offline", "start": 0.10, "end": 0.60}],
    )

    helper.transcribe_qwen3_asr(audio, "Qwen/Qwen3-ASR-1.7B", "auto")

    assert aligned == [(audio, "offline", "English")]


def test_qwen_asr_batch_uses_local_alignment_for_each_matching_audio(monkeypatch, tmp_path):
    helper = load_helper_module()
    first_audio = tmp_path / "first.wav"
    second_audio = tmp_path / "second.wav"
    first_audio.touch()
    second_audio.touch()
    aligned = []

    def fake_align(path, text, language):
        aligned.append((path, text, language))
        return [{"text": text, "start": 0.20, "end": 0.80}]

    calls = []
    def fake_worker(path, model, language, context=None):
        calls.append((path, model, language, context))
        return {
            "text": "第一段" if path == first_audio else "第二段",
            "language": None, "model": "asr", "device_map": "cpu",
            "hotword_context_status": "used" if context else "not_requested",
        }
    monkeypatch.setattr(helper, "run_qwen3_asr_worker", fake_worker)
    monkeypatch.setattr(helper, "qwen3_force_align_items", fake_align)

    payloads = helper.transcribe_qwen3_asr_batch(
        [first_audio, second_audio], "Qwen/Qwen3-ASR-1.7B", "zh", context="术语"
    )

    assert aligned == [
        (first_audio, "第一段", "Chinese"),
        (second_audio, "第二段", "Chinese"),
    ]
    assert [payload["segments"][0]["words"][0]["word"] for payload in payloads] == ["第一段", "第二段"]
    assert [call[3] for call in calls] == ["术语", "术语"]


def test_qwen_empty_worker_result_does_not_start_gguf(monkeypatch, tmp_path):
    helper = load_helper_module()
    audio = tmp_path / "empty.wav"
    audio.touch()
    monkeypatch.setattr(
        helper,
        "run_qwen3_asr_worker",
        lambda *_args, **_kwargs: {
            "text": "", "language": "Chinese", "model": "asr", "device_map": "cpu",
            "hotword_context_status": "not_requested",
        },
    )
    monkeypatch.setattr(
        helper, "qwen3_force_align_items",
        lambda *_args: (_ for _ in ()).throw(AssertionError("GGUF must not start")),
    )

    payload = helper.transcribe_qwen3_asr(audio, "model", "auto")

    assert payload["text"] == ""
    assert payload["segments"] == []


def test_qwen_force_align_items_uses_the_bundled_gguf_helper(monkeypatch, tmp_path):
    helper = load_helper_module()
    audio = tmp_path / "alignment.wav"
    audio.touch()
    calls = []

    def fake_cpp_align(path, text, language, work_dir):
        calls.append((path, text, language, work_dir))
        return [{"word": "离线", "start": 0.10, "end": 0.40}]

    monkeypatch.setattr(helper, "qwen3_cpp_force_align_items", fake_cpp_align)

    assert helper.qwen3_force_align_items(audio, "离线", "Chinese") == [
        {"text": "离线", "start": 0.10, "end": 0.40}
    ]
    assert calls[0][:3] == (audio, "离线", "Chinese")


def test_v4_alignment_fallback_expands_real_gguf_parser_shape(monkeypatch, tmp_path):
    helper = load_helper_module()
    audio = tmp_path / "alignment.wav"
    audio.touch()
    monkeypatch.setattr(
        helper,
        "qwen3_cpp_force_align_items",
        lambda *_args: [{"word": "离线", "start": 0.10, "end": 0.40}],
    )

    units, diagnostic = helper.generate_v4.require_aligned_units(
        audio,
        {"text": "离线", "segments": []},
        helper.qwen3_force_align_items,
        "Chinese",
        30.0,
        0,
    )

    assert "".join(unit["text"] for unit in units) == "离线"
    assert diagnostic["forced_align_retry_count"] == 1
