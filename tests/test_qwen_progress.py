import json
import sys

import pytest

from test_qwen_memory_safety import load_helper_module, healthy_snapshot


def test_timeout_keeps_short_underlying_log(monkeypatch):
    helper = load_helper_module()
    monkeypatch.setattr(helper, "read_local_resource_snapshot", lambda: healthy_snapshot(helper))
    with pytest.raises(helper.LocalQwenSafetyError) as error:
        helper.run_guarded_local_qwen_command(
            [sys.executable, "-c", "import time; print('Metal diagnostic before timeout', flush=True); time.sleep(30)"],
            "gguf", timeout_seconds=0.2,
        )
    assert "超时" in str(error.value)
    assert "Metal diagnostic before timeout" in str(error.value)


def test_local_progress_preserves_batch_percent_and_reports_real_phase(monkeypatch, tmp_path):
    helper = load_helper_module()
    monkeypatch.setattr(helper, "read_local_resource_snapshot", lambda: healthy_snapshot(helper))
    progress = tmp_path / "progress.json"
    worker_progress = tmp_path / "worker.json"
    helper.write_progress(progress, "generate_subtitle_batch", "v5 Qwen 转写 1/2",
                          progress_index=25, progress_total=100, batch_index=1, total_batches=2)
    events = []
    original_write = helper.write_progress

    def record(path, stage, message, **extra):
        events.append({"stage": stage, "message": message, **extra})
        original_write(path, stage, message, **extra)

    monkeypatch.setattr(helper, "write_progress", record)
    worker_progress.write_text(json.dumps({"stage": "run_qwen_asr"}))
    code, _ = helper.run_guarded_local_qwen_command(
        [sys.executable, "-c", "import time; time.sleep(1.1)"], "asr",
        timeout_seconds=5, worker_progress_path=worker_progress,
    )
    assert code == 0
    assert any(e["stage"] == "run_qwen_asr" and "识别语音" in e["message"] for e in events)
    assert all(e["progress_index"] == 25 and e["batch_index"] == 1 for e in events)
    assert any(e.get("elapsed_seconds", 0) >= 1 for e in events)
    assert "1/2" in events[-1]["message"]


def test_worker_reports_load_before_model_and_inference_after(monkeypatch, tmp_path):
    helper = load_helper_module()
    progress = tmp_path / "worker.json"
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"audio": "test.wav", "progress_json": str(progress)}))
    phases = []

    def load(_model):
        phases.append(json.loads(progress.read_text())["stage"])
        return object(), "model", "aligner", "mps"

    def infer(*_args):
        phases.append(json.loads(progress.read_text())["stage"])
        return [{"text": "你好"}], "not_requested"

    monkeypatch.setattr(helper, "load_qwen3_asr_model", load)
    monkeypatch.setattr(helper, "_transcribe_qwen3_model", infer)
    helper.run_qwen3_asr_worker_mode(request, tmp_path / "output.json")
    assert phases == ["load_qwen_model", "run_qwen_asr"]


def test_gguf_progress_does_not_nest_previous_asr_message(monkeypatch, tmp_path):
    helper = load_helper_module()
    monkeypatch.setattr(helper, "read_local_resource_snapshot", lambda: healthy_snapshot(helper))
    progress = tmp_path / "progress.json"
    helper.write_progress(progress, "generate_subtitle_batch", "v5 Qwen 转写 1/2", progress_index=25, progress_total=100)
    for stage in ("asr", "gguf"):
        helper.run_guarded_local_qwen_command([sys.executable, "-c", "pass"], stage, timeout_seconds=5)
    payload = json.loads(progress.read_text())
    assert payload["stage"] == "load_qwen_aligner"
    assert "对齐" in payload["message"]
    assert "加载语音模型" not in payload["message"]
    assert payload["message"].count("v5 Qwen 转写 1/2") == 1


def test_success_still_returns_short_log(monkeypatch):
    helper = load_helper_module()
    monkeypatch.setattr(helper, "read_local_resource_snapshot", lambda: healthy_snapshot(helper))
    code, log = helper.run_guarded_local_qwen_command(
        [sys.executable, "-c", "print('done')"], "gguf", timeout_seconds=5,
    )
    assert code == 0
    assert log.strip() == "done"


def test_gguf_short_log_switches_to_alignment_while_process_is_alive(monkeypatch, tmp_path):
    helper = load_helper_module()
    monkeypatch.setattr(helper, "read_local_resource_snapshot", lambda: healthy_snapshot(helper))
    progress = tmp_path / "progress.json"
    helper.write_progress(progress, "generate_subtitle_batch", "batch", progress_index=25, progress_total=100)
    events = []
    original_write = helper.write_progress

    def record(path, stage, message, **extra):
        events.append(stage)
        original_write(path, stage, message, **extra)

    monkeypatch.setattr(helper, "write_progress", record)
    code, _ = helper.run_guarded_local_qwen_command(
        [sys.executable, "-c", "import time; print('Model loaded. Running alignment...', flush=True); time.sleep(1.2)"],
        "gguf", timeout_seconds=5,
    )
    assert code == 0
    assert events[0] == "load_qwen_aligner"
    assert events[-1] == "qwen_align"


@pytest.mark.parametrize("disk_error", [False, True])
def test_failure_diagnostic_is_local_bounded_and_disk_failure_keeps_error(monkeypatch, tmp_path, disk_error):
    helper = load_helper_module()
    monkeypatch.setattr(helper, "read_local_resource_snapshot", lambda: healthy_snapshot(helper))
    progress = tmp_path / "progress.json"
    helper.write_progress(progress, "generate_subtitle_batch", "batch")
    log_path = progress.with_name(progress.name + ".qwen-gguf.log")
    if disk_error:
        log_path.mkdir()
    code, log = helper.run_guarded_local_qwen_command(
        [sys.executable, "-c", "import sys; print('x' * 400000 + 'Metal failure'); sys.exit(134)"],
        "gguf", timeout_seconds=5,
    )
    assert code == 134
    with pytest.raises(helper.LocalQwenSafetyError) as error:
        helper.raise_for_local_qwen_returncode(code, "gguf", log)
    assert "Metal failure" in str(error.value)
    if disk_error:
        assert "诊断日志:" not in log
    else:
        assert log_path.stat().st_size <= helper.LOCAL_QWEN_LOG_LIMIT_BYTES + 100
        assert "exit code: 134" in log_path.read_text()
        assert str(log_path) in str(error.value)


def test_worker_safety_payload_keeps_native_diagnostics(monkeypatch, tmp_path):
    helper = load_helper_module()
    monkeypatch.setattr(helper, "read_local_resource_snapshot", lambda: healthy_snapshot(helper))

    def fake_worker(cmd, *_args, **_kwargs):
        output = helper.Path(cmd[cmd.index("--output") + 1])
        output.write_text(json.dumps({"ok": False, "error_type": "LocalQwenSafetyError", "error": "low memory"}))
        return 1, "native details\n诊断日志: /tmp/qwen-test.log"

    monkeypatch.setattr(helper, "run_guarded_local_qwen_command", fake_worker)
    with pytest.raises(helper.LocalQwenSafetyError) as error:
        helper.run_qwen3_asr_worker(tmp_path / "audio.wav", "model", "Chinese")
    assert "low memory" in str(error.value)
    assert "native details" in str(error.value)
    assert "/tmp/qwen-test.log" in str(error.value)
