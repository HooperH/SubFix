import json
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


def test_generate_subtitles_batch_fixture_outputs_merged_rows_and_diagnostics(tmp_path, monkeypatch):
    fixture_path = tmp_path / "fixture.json"
    output = tmp_path / "generated_batch.json"
    progress = tmp_path / "progress.json"
    diagnostic_output = tmp_path / "diagnostic.json"
    batch_plan = tmp_path / "batch_plan.json"
    fixture_path.write_text(
        json.dumps(
            {
                "segments": [
                    {"start": 0.0, "end": 1.0, "text": "你看这个可以吗，我觉得还行"},
                ],
                "text": "你看这个可以吗，我觉得还行",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    batch_plan.write_text(
        json.dumps(
            {
                "batches": [
                    {
                        "batch_id": "A1-10",
                        "audio": "fixture-a.wav",
                        "source_start": 10.0,
                        "source_end": 11.0,
                        "timeline_start_frame": 1000,
                        "fps": 25,
                        "track_index": 1,
                        "track_name": "Dialogue 1",
                        "item_index": 10,
                    },
                    {
                        "batch_id": "A2-20",
                        "audio": "fixture-b.wav",
                        "source_start": 20.0,
                        "source_end": 21.0,
                        "timeline_start_frame": 2000,
                        "fps": 25,
                        "track_index": 2,
                        "track_name": "Dialogue 2",
                        "item_index": 20,
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    helper = load_helper_module()
    # Isolate the acoustic producer while exercising the public v5 CLI's real
    # diagnostic, provenance, progress and compact writeback serialization.
    monkeypatch.setattr(helper, "run_generate_subtitles_batch_plan", helper.run_generate_subtitles_batch_plan_v3)
    result = helper.main(
        [
            "--mode",
            "generate_subtitles_batch",
            "--generate-engine",
            "v5",
            "--fixture-json",
            str(fixture_path),
            "--batch-plan-json",
            str(batch_plan),
            "--output",
            str(output),
            "--diagnostic-output",
            str(diagnostic_output),
            "--progress-json",
            str(progress),
        ],
    )

    assert result == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    progress_payload = json.loads(progress.read_text(encoding="utf-8"))
    diagnostic_payload = json.loads(diagnostic_output.read_text(encoding="utf-8"))
    assert set(payload) == {"ok", "subtitle_rows", "generation_trace", "helper_version"}
    public_fields = ("index", "start_frame", "end_frame", "text", "track_index")
    assert diagnostic_payload["subtitle_rows"] == [
        {key: row[key] for key in public_fields} for row in payload["subtitle_rows"]
    ]
    assert diagnostic_payload["generation_trace"] == payload["generation_trace"]
    assert payload["ok"] is True
    assert diagnostic_payload["diagnostic"]["requested_mode"] == "generate_subtitles_batch"
    assert diagnostic_payload["diagnostic"]["batch_count"] == 2
    assert list(dict.fromkeys(row["batch_id"] for row in payload["subtitle_rows"])) == ["A1-10", "A2-20"]
    assert diagnostic_payload["batches"][0]["track_index"] == 1
    assert payload["subtitle_rows"][2]["item_index"] == 20
    assert payload["subtitle_rows"][0]["start_frame"] == 1000
    assert payload["subtitle_rows"][2]["start_frame"] == 2000
    assert all(len(row["text"]) <= 18 for row in payload["subtitle_rows"])
    assert not any(any(char in row["text"] for char in "，。！？、；：,.!?;:") for row in payload["subtitle_rows"])
    assert progress_payload["batch_index"] == 2
    assert progress_payload["total_batches"] == 2
    assert progress_payload["progress_index"] == 6
    assert progress_payload["progress_total"] == 6


def test_generate_subtitles_batch_splits_long_sources_into_windows(tmp_path, monkeypatch):
    fixture_path = tmp_path / "fixture.json"
    output = tmp_path / "generated_batch_split.json"
    progress = tmp_path / "progress.json"
    diagnostic_output = tmp_path / "diagnostic.json"
    batch_plan = tmp_path / "batch_plan.json"
    fixture_path.write_text(
        json.dumps(
            {"segments": [{"start": 0.0, "end": 1.0, "text": "测试"}], "text": "测试"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    batch_plan.write_text(
        json.dumps(
            {
                "batches": [
                    {
                        "batch_id": "long",
                        "audio": "fixture-long.wav",
                        "source_start": 10.0,
                        "source_end": 460.0,
                        "timeline_start_frame": 1000,
                        "fps": 25,
                        "track_index": 1,
                        "track_name": "Dialogue 1",
                        "item_index": 10,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    helper = load_helper_module()
    # Isolate the acoustic producer while exercising the public v5 CLI's real
    # diagnostic, provenance, progress and compact writeback serialization.
    monkeypatch.setattr(helper, "run_generate_subtitles_batch_plan", helper.run_generate_subtitles_batch_plan_v3)
    result = helper.main(
        [
            "--mode",
            "generate_subtitles_batch",
            "--generate-engine",
            "v5",
            "--fixture-json",
            str(fixture_path),
            "--batch-plan-json",
            str(batch_plan),
            "--output",
            str(output),
            "--diagnostic-output",
            str(diagnostic_output),
            "--progress-json",
            str(progress),
        ],
    )

    assert result == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    progress_payload = json.loads(progress.read_text(encoding="utf-8"))
    diagnostic_payload = json.loads(diagnostic_output.read_text(encoding="utf-8"))
    assert set(payload) == {"ok", "subtitle_rows", "generation_trace", "helper_version"}
    public_fields = ("index", "start_frame", "end_frame", "text", "track_index")
    assert diagnostic_payload["subtitle_rows"] == [
        {key: row[key] for key in public_fields} for row in payload["subtitle_rows"]
    ]
    assert diagnostic_payload["generation_trace"] == payload["generation_trace"]
    assert diagnostic_payload["diagnostic"]["source_batch_count"] == 1
    assert diagnostic_payload["diagnostic"]["batch_count"] == 10
    assert diagnostic_payload["diagnostic"]["batch_max_seconds"] == 45.0
    assert [row["batch_id"] for row in payload["subtitle_rows"]] == [f"long_part{index:02d}" for index in range(1, 11)]
    assert [row["start_frame"] for row in payload["subtitle_rows"]] == [
        1000,
        2125,
        3250,
        4375,
        5500,
        6625,
        7750,
        8875,
        10000,
        11125,
    ]
    assert progress_payload["batch_index"] == 10
    assert progress_payload["total_batches"] == 10
    assert progress_payload["progress_index"] == 22
    assert progress_payload["progress_total"] == 22
