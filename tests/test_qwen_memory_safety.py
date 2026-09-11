import importlib.util
import os
import signal
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "subfix_asr_transcribe.py"
GIB = 1024 ** 3


def load_helper_module():
    name = f"subfix_asr_memory_test_{os.getpid()}_{id(object())}"
    spec = importlib.util.spec_from_file_location(name, HELPER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def healthy_snapshot(helper, *, total=64 * GIB, free_percent=50, disk_free=100 * GIB):
    return helper.LocalResourceSnapshot(total, free_percent, disk_free, disk_free)


def test_memory_pressure_parser_is_strict():
    helper = load_helper_module()
    parsed = helper.parse_memory_pressure_output(
        "The system has 68719476736 (4194304 pages with a page size of 16384).\n"
        "System-wide memory free percentage: 42%\n"
    )
    assert parsed == (68719476736, 42)
    with pytest.raises(helper.LocalQwenSafetyError, match="无法解析"):
        helper.parse_memory_pressure_output("System-wide memory free percentage: unknown")


@pytest.mark.parametrize(
    ("snapshot", "stage", "message"),
    [
        ((15 * GIB, 80, 100 * GIB, 100 * GIB), "asr", "16 GiB"),
        ((64 * GIB, 10, 100 * GIB, 100 * GIB), "asr", "可用内存"),
        ((64 * GIB, 80, 9 * GIB, 100 * GIB), "asr", "磁盘"),
    ],
)
def test_resource_policy_rejects_unsafe_start(snapshot, stage, message):
    helper = load_helper_module()
    with pytest.raises(helper.LocalQwenSafetyError, match=message):
        helper.assert_local_qwen_resources(helper.LocalResourceSnapshot(*snapshot), stage)


def test_gguf_does_not_require_sixteen_gib_total_memory():
    helper = load_helper_module()
    helper.assert_local_qwen_resources(
        helper.LocalResourceSnapshot(12 * GIB, 60, 100 * GIB, 100 * GIB), "gguf"
    )


def test_guarded_process_timeout_terminates_and_waits(monkeypatch):
    helper = load_helper_module()
    proc = helper.subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with pytest.raises(helper.LocalQwenSafetyError, match="超时"):
            helper.monitor_local_qwen_process(
                proc,
                "gguf",
                timeout_seconds=0.03,
                probe=lambda: healthy_snapshot(helper),
                rss_reader=lambda _pid: 1,
                poll_interval=0.01,
            )
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_guarded_process_rss_limit_terminates_child():
    helper = load_helper_module()
    proc = helper.subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with pytest.raises(helper.LocalQwenSafetyError, match="RSS"):
            helper.monitor_local_qwen_process(
                proc,
                "gguf",
                timeout_seconds=5,
                probe=lambda: healthy_snapshot(helper),
                rss_reader=lambda _pid: 9 * GIB,
                poll_interval=0.01,
            )
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_guarded_process_probe_failure_terminates_child():
    helper = load_helper_module()
    proc = helper.subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with pytest.raises(helper.LocalQwenSafetyError, match="probe failed"):
            helper.monitor_local_qwen_process(
                proc,
                "asr",
                timeout_seconds=5,
                probe=lambda: (_ for _ in ()).throw(helper.LocalQwenSafetyError("probe failed")),
                poll_interval=0.01,
            )
        assert proc.poll() is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.mark.parametrize("returncode", (-9, 137))
def test_abnormal_memory_kill_is_a_safety_error(returncode):
    helper = load_helper_module()
    with pytest.raises(helper.LocalQwenSafetyError, match="异常终止"):
        helper.raise_for_local_qwen_returncode(returncode, "asr", "")


@pytest.mark.parametrize(
    "message", ("MPS backend out of memory", "can't allocate memory", "std::bad_alloc")
)
def test_memory_exhaustion_messages_are_classified_as_safety(message):
    helper = load_helper_module()
    assert helper.is_local_qwen_memory_error(RuntimeError(message))


@pytest.mark.parametrize(
    "message", ("OSError: [Errno 28] No space left on device", "disk full")
)
def test_disk_exhaustion_exit_is_a_safety_error(message):
    helper = load_helper_module()
    with pytest.raises(helper.LocalQwenSafetyError, match="资源压力"):
        helper.raise_for_local_qwen_returncode(1, "gguf", message)


def test_guarded_command_keeps_only_bounded_log_tail(monkeypatch):
    helper = load_helper_module()
    monkeypatch.setattr(helper, "read_local_resource_snapshot", lambda: healthy_snapshot(helper))
    returncode, log_text = helper.run_guarded_local_qwen_command(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 400000); sys.stdout.write('TAIL')"],
        "gguf",
        timeout_seconds=5,
    )
    assert returncode == 0
    assert len(log_text.encode()) <= helper.LOCAL_QWEN_LOG_LIMIT_BYTES
    assert log_text.endswith("TAIL")


def test_guarded_timeout_reaps_spawned_descendant(monkeypatch, tmp_path):
    helper = load_helper_module()
    monkeypatch.setattr(helper, "read_local_resource_snapshot", lambda: healthy_snapshot(helper))
    child_pid_path = tmp_path / "child.pid"
    code = (
        "import pathlib,subprocess,sys,time; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(p.pid)); "
        "time.sleep(30)"
    )
    try:
        with pytest.raises(helper.LocalQwenSafetyError, match="超时"):
            helper.run_guarded_local_qwen_command(
                [sys.executable, "-c", code], "gguf", timeout_seconds=0.2
            )
        child_pid = int(child_pid_path.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        if child_pid_path.exists():
            child_pid = int(child_pid_path.read_text())
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_qwen_batch_env_is_hard_clamped_to_one(monkeypatch):
    helper = load_helper_module()
    monkeypatch.setenv("SUBFIX_QWEN_GENERATE_BATCH_SIZE", "8")
    assert helper.qwen_generate_batch_size() == 1


def test_safety_error_is_not_swallowed_by_auto_fallback(monkeypatch, tmp_path):
    helper = load_helper_module()
    calls = []
    monkeypatch.setattr(helper, "AUTO_TRANSCRIBE_BACKENDS", ("qwen3_asr", "mlx_whisper"))
    monkeypatch.setattr(
        helper, "transcribe_qwen3_asr",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(helper.LocalQwenSafetyError("low memory")),
    )
    monkeypatch.setattr(
        helper, "transcribe_mlx_whisper", lambda *_args: calls.append("fallback") or {"text": "bad"},
    )
    with pytest.raises(helper.LocalQwenSafetyError, match="low memory"):
        helper.transcribe_with_backend(tmp_path / "x.wav", "model", "auto")
    assert calls == []
