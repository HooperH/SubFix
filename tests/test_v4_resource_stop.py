"""Resource stops must survive the alignment/context-retry boundary."""

from pathlib import Path

import pytest

from test_asr_helper import load_helper_module


def test_resource_stop_does_not_retry_with_larger_context(monkeypatch, tmp_path):
    helper = load_helper_module()
    v4 = helper.generate_v4
    failure = helper.LocalQwenSafetyError("资源不足，已停止")
    calls = []

    def stop_alignment(*args):
        calls.append(args)
        raise failure

    def no_context_audio(*args):
        pytest.fail("Resource stop must not prepare a larger retry window")

    monkeypatch.setattr(v4._load(), "write_context_window_audio", no_context_audio)
    with pytest.raises(helper.LocalQwenSafetyError) as caught:
        v4.require_aligned_window_with_context(
            track_audio_path=tmp_path / "track.wav",
            track_start_frame=0,
            window={"audio_path": tmp_path / "one.wav", "start_frame": 0, "end_frame": 30},
            payload={"text": "你好"},
            adjacent_window={"start_frame": 30, "end_frame": 60},
            adjacent_payload={"text": "世界"},
            merged_audio_path=tmp_path / "merged.wav",
            align_fn=stop_alignment,
            language="Chinese",
            fps=30.0,
        )
    assert caught.value is failure
    assert len(calls) == 1


def test_ordinary_alignment_error_keeps_existing_error_contract(tmp_path):
    v4 = load_helper_module().generate_v4

    def fail_alignment(*args):
        raise RuntimeError("bad timestamps")

    with pytest.raises(v4.V4AlignmentError) as caught:
        v4.require_aligned_units(
            Path(tmp_path / "audio.wav"), {"text": "你好"}, fail_alignment, "Chinese", 30.0, 0
        )
    assert str(caught.value.__cause__) == "bad timestamps"
