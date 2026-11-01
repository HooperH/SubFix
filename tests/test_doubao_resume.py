"""Offline regression tests for standard-tier recovery and durable progress."""

import importlib.util
import io
import json
import os
import stat
import sys
import types
import urllib.error
from pathlib import Path

import pytest


def load_helper():
    path = Path(__file__).resolve().parents[1] / "subfix_asr_transcribe.py"
    spec = importlib.util.spec_from_file_location("doubao_resume_helper", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def helper(monkeypatch, tmp_path):
    monkeypatch.setenv("SUBFIX_DOUBAO_API_KEY", "secret-for-test-only")
    monkeypatch.setenv("SUBFIX_DOUBAO_CACHE_DIR", str(tmp_path / "cache"))
    module = load_helper()
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    monkeypatch.setattr(module, "DOUBAO_ASR_V2_MAX_QUERY_ATTEMPTS", 4)
    return module


def http_error(code):
    return urllib.error.HTTPError(
        "https://example.invalid", code, "Gateway Time-out", {},
        io.BytesIO(b"<html><body>Powered by volc-dcdn</body></html>"),
    )


def success(text="完成"):
    return "20000000", json.dumps({"result": {"text": text, "utterances": []}}), "query-log"


def test_submit_504_queries_original_id_and_recovers_query_503(helper, monkeypatch, tmp_path):
    audio = tmp_path / "window.wav"
    audio.write_bytes(b"audio-one")
    calls = []

    def request(endpoint, headers, *_):
        calls.append((endpoint, headers["X-Api-Request-Id"]))
        if len(calls) == 1:
            raise http_error(504)
        if len(calls) == 2:
            raise http_error(503)
        return success()

    monkeypatch.setattr(helper, "_doubao_asr_request_once", request)
    assert helper.transcribe_doubao_asr_v2(audio, "bigmodel", "zh")["text"] == "完成"
    assert [call[0] for call in calls] == [
        helper.DOUBAO_ASR_V2_SUBMIT_ENDPOINT,
        helper.DOUBAO_ASR_V2_QUERY_ENDPOINT,
        helper.DOUBAO_ASR_V2_QUERY_ENDPOINT,
    ]
    assert len({call[1] for call in calls}) == 1


def test_restart_reuses_successful_window_and_resumes_pending_task(helper, monkeypatch, tmp_path):
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    first.write_bytes(b"first-window")
    second.write_bytes(b"second-window")
    calls = []
    second_id = None

    def request(endpoint, headers, *_):
        nonlocal second_id
        calls.append(endpoint)
        if endpoint == helper.DOUBAO_ASR_V2_SUBMIT_ENDPOINT:
            if calls.count(endpoint) == 2:
                second_id = headers["X-Api-Request-Id"]
            return "20000000", "{}", "submit-log"
        if second_id:
            raise http_error(504)
        return success("第一段")

    monkeypatch.setattr(helper, "_doubao_asr_request_once", request)
    args = types.SimpleNamespace(backend="doubao_asr_v2", model="bigmodel", language="zh")
    with pytest.raises(RuntimeError):
        helper.transcribe_v4_window_batch([first, second], args)

    # A fresh module and different temp filenames emulate restarting Resolve's helper.
    restarted = load_helper()
    renamed = tmp_path / "new-render-name.wav"
    renamed.write_bytes(first.read_bytes())
    resumed_calls = []

    def resume(endpoint, headers, *_):
        resumed_calls.append((endpoint, headers["X-Api-Request-Id"]))
        return success("第二段")

    monkeypatch.setattr(restarted, "_doubao_asr_request_once", resume)
    payloads, _ = restarted.transcribe_v4_window_batch([renamed, second], args)
    assert [payload["text"] for payload in payloads] == ["第一段", "第二段"]
    assert resumed_calls == [(helper.DOUBAO_ASR_V2_QUERY_ENDPOINT, second_id)]
    checkpoint_text = "".join(path.read_text() for path in (tmp_path / "cache").rglob("*.json"))
    assert "secret-for-test-only" not in checkpoint_text
    assert "first-window" not in checkpoint_text


@pytest.mark.parametrize("failure", [504, "timeout"])
def test_unknown_submission_survives_restart_without_resubmission(helper, monkeypatch, tmp_path, failure):
    audio = tmp_path / "window.wav"
    audio.write_bytes(b"audio")
    original_ids = []

    def fail(endpoint, headers, *_):
        original_ids.append(headers["X-Api-Request-Id"])
        raise http_error(failure) if isinstance(failure, int) else TimeoutError("timed out")

    monkeypatch.setattr(helper, "_doubao_asr_request_once", fail)
    with pytest.raises(RuntimeError) as exc:
        helper.transcribe_doubao_asr_v2(audio, "bigmodel", "zh")
    assert "<html>" not in str(exc.value)
    assert "重试" in str(exc.value)
    assert len(original_ids) <= 5

    calls = []
    def resume(endpoint, headers, *_):
        calls.append((endpoint, headers["X-Api-Request-Id"]))
        return success()

    restarted = load_helper()
    monkeypatch.setattr(restarted, "_doubao_asr_request_once", resume)
    assert restarted.transcribe_doubao_asr_v2(audio, "bigmodel", "zh")["text"] == "完成"
    assert calls == [(helper.DOUBAO_ASR_V2_QUERY_ENDPOINT, original_ids[0])]


def test_auth_failure_is_not_retried_or_saved_as_pending(helper, monkeypatch, tmp_path):
    audio = tmp_path / "window.wav"
    audio.write_bytes(b"audio")
    calls = []
    def fail(endpoint, *_):
        calls.append(endpoint)
        raise http_error(401)
    monkeypatch.setattr(helper, "_doubao_asr_request_once", fail)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="401"):
            helper.transcribe_doubao_asr_v2(audio, "bigmodel", "zh")
    assert calls == [helper.DOUBAO_ASR_V2_SUBMIT_ENDPOINT] * 2


@pytest.mark.parametrize("change", ["audio", "language", "resource", "credentials"])
def test_cache_key_includes_audio_and_recognition_configuration(helper, monkeypatch, tmp_path, change):
    audio = tmp_path / "window.wav"
    audio.write_bytes(b"audio")
    calls = []
    def request(endpoint, *_):
        calls.append(endpoint)
        return ("20000000", "{}", "") if endpoint == helper.DOUBAO_ASR_V2_SUBMIT_ENDPOINT else success()
    monkeypatch.setattr(helper, "_doubao_asr_request_once", request)
    helper.transcribe_doubao_asr_v2(audio, "bigmodel", "zh")
    helper.transcribe_doubao_asr_v2(audio, "bigmodel", "zh")
    assert calls.count(helper.DOUBAO_ASR_V2_SUBMIT_ENDPOINT) == 1
    language = "zh"
    if change == "audio":
        audio.write_bytes(b"different-audio")
    elif change == "language":
        language = "en"
    elif change == "resource":
        monkeypatch.setattr(helper, "DOUBAO_ASR_V2_RESOURCE_ID", "different-resource")
    else:
        monkeypatch.setenv("SUBFIX_DOUBAO_API_KEY", "different-secret")
    helper.transcribe_doubao_asr_v2(audio, "bigmodel", language)
    assert calls.count(helper.DOUBAO_ASR_V2_SUBMIT_ENDPOINT) == 2


def test_cache_write_failure_stops_before_paid_submission(helper, monkeypatch, tmp_path):
    audio = tmp_path / "window.wav"
    audio.write_bytes(b"audio")
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("file")
    monkeypatch.setenv("SUBFIX_DOUBAO_CACHE_DIR", str(blocked))
    calls = []
    def request(endpoint, *_):
        calls.append(endpoint)
        return ("20000000", "{}", "") if endpoint == helper.DOUBAO_ASR_V2_SUBMIT_ENDPOINT else success()
    monkeypatch.setattr(helper, "_doubao_asr_request_once", request)
    with pytest.raises(RuntimeError, match="断点缓存"):
        helper.transcribe_doubao_asr_v2(audio, "bigmodel", "zh")
    assert calls == []


@pytest.mark.parametrize("status", ["20000001", ""])
def test_processing_response_with_partial_result_is_not_cached_as_complete(helper, monkeypatch, tmp_path, status):
    audio = tmp_path / "window.wav"
    audio.write_bytes(b"audio")
    responses = iter([
        ("20000000", "{}", ""),
        (status, json.dumps({"result": {"text": "partial"}}), ""),
        success("完整结果"),
    ])
    monkeypatch.setattr(helper, "_doubao_asr_request_once", lambda *_: next(responses))
    assert helper.transcribe_doubao_asr_v2(audio, "bigmodel", "zh")["text"] == "完整结果"


def test_recovery_hint_uses_server_task_id(helper, monkeypatch, tmp_path):
    audio = tmp_path / "window.wav"
    audio.write_bytes(b"audio")
    def request(endpoint, *_):
        if endpoint == helper.DOUBAO_ASR_V2_SUBMIT_ENDPOINT:
            return "20000000", '{"task_id":"server-task"}', ""
        raise http_error(504)
    monkeypatch.setattr(helper, "_doubao_asr_request_once", request)
    with pytest.raises(RuntimeError, match="请求 ID：server-task"):
        helper.transcribe_doubao_asr_v2(audio, "bigmodel", "zh")


def test_consecutive_gateway_errors_have_a_shorter_bound_than_processing_polls(helper, monkeypatch, tmp_path):
    audio = tmp_path / "window.wav"
    audio.write_bytes(b"audio")
    monkeypatch.setattr(helper, "DOUBAO_ASR_V2_MAX_QUERY_ATTEMPTS", 60)
    calls = []
    def request(endpoint, *_):
        calls.append(endpoint)
        raise http_error(504)
    monkeypatch.setattr(helper, "_doubao_asr_request_once", request)
    with pytest.raises(RuntimeError, match="重试已达上限"):
        helper.transcribe_doubao_asr_v2(audio, "bigmodel", "zh")
    assert calls.count(helper.DOUBAO_ASR_V2_SUBMIT_ENDPOINT) == 1
    assert calls.count(helper.DOUBAO_ASR_V2_QUERY_ENDPOINT) <= 5


@pytest.mark.parametrize("contents", ['{"state": []}', '{"state":"complete","payload":{}}', 'broken-json'])
def test_corrupt_cache_is_reported_without_resubmitting(helper, monkeypatch, tmp_path, contents):
    audio = tmp_path / "window.wav"
    audio.write_bytes(b"audio")
    def request(endpoint, *_):
        return ("20000000", "{}", "") if endpoint == helper.DOUBAO_ASR_V2_SUBMIT_ENDPOINT else success()
    monkeypatch.setattr(helper, "_doubao_asr_request_once", request)
    helper.transcribe_doubao_asr_v2(audio, "bigmodel", "zh")
    checkpoint = next((tmp_path / "cache").glob("*.json"))
    checkpoint.write_text(contents)
    def forbidden(*_):
        pytest.fail("corrupt checkpoint must not cause an untracked paid submission")
    monkeypatch.setattr(helper, "_doubao_asr_request_once", forbidden)
    with pytest.raises(RuntimeError, match="断点缓存.*损坏"):
        helper.transcribe_doubao_asr_v2(audio, "bigmodel", "zh")


def test_failed_result_save_keeps_pending_task_for_next_run(helper, monkeypatch, tmp_path):
    audio = tmp_path / "window.wav"
    audio.write_bytes(b"audio")
    calls = []
    def request(endpoint, *_):
        calls.append(endpoint)
        return ("20000000", "{}", "") if endpoint == helper.DOUBAO_ASR_V2_SUBMIT_ENDPOINT else success()
    monkeypatch.setattr(helper, "_doubao_asr_request_once", request)
    original_replace = helper.os.replace
    def disk_full(source, destination):
        if json.loads(Path(source).read_text()).get("state") == "complete":
            raise OSError("disk full")
        return original_replace(source, destination)
    monkeypatch.setattr(helper.os, "replace", disk_full)
    with pytest.raises(RuntimeError, match="断点缓存"):
        helper.transcribe_doubao_asr_v2(audio, "bigmodel", "zh")
    monkeypatch.setattr(helper.os, "replace", original_replace)
    assert helper.transcribe_doubao_asr_v2(audio, "bigmodel", "zh")["text"] == "完成"
    assert calls.count(helper.DOUBAO_ASR_V2_SUBMIT_ENDPOINT) == 1
    assert list((tmp_path / "cache").glob("*.tmp")) == []
    if os.name != "nt":
        checkpoint = next((tmp_path / "cache").glob("*.json"))
        assert stat.S_IMODE(checkpoint.stat().st_mode) == 0o600


def test_ambiguous_query_rejection_does_not_create_another_task(helper, monkeypatch, tmp_path):
    audio = tmp_path / "window.wav"
    audio.write_bytes(b"audio")
    calls = []
    def request(endpoint, *_):
        calls.append(endpoint)
        if endpoint == helper.DOUBAO_ASR_V2_SUBMIT_ENDPOINT:
            raise http_error(504)
        return "45000001", "{}", ""
    monkeypatch.setattr(helper, "_doubao_asr_request_once", request)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="45000001"):
            helper.transcribe_doubao_asr_v2(audio, "bigmodel", "zh")
    assert calls.count(helper.DOUBAO_ASR_V2_SUBMIT_ENDPOINT) == 1
