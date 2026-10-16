import argparse
import importlib.util
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "subfix_generate_v4.py"


def load_v4():
    spec = importlib.util.spec_from_file_location("subfix_generate_v4", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_constant_wav(path: Path, amplitude: int, seconds: float, sample_rate: int = 16000) -> None:
    samples = [amplitude] * int(seconds * sample_rate)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"".join(sample.to_bytes(2, "little", signed=True) for sample in samples))


def unit(text, start, end, track=1, score=10.0, window=1):
    return {
        "text": text,
        "start_frame": start,
        "end_frame": end,
        "track_index": track,
        "source_score": score,
        "window_index": window,
    }


def _run_v4_pipeline_with_max_chars(tmp_path, monkeypatch, text, max_chars_kwargs):
    helper_spec = importlib.util.spec_from_file_location(
        f"subfix_asr_transcribe_v4_max_chars_{len(max_chars_kwargs)}_{max_chars_kwargs.get('max_chars')}",
        ROOT / "subfix_asr_transcribe.py",
    )
    helper = importlib.util.module_from_spec(helper_spec)
    sys.modules[helper_spec.name] = helper
    helper_spec.loader.exec_module(helper)
    source = tmp_path / "source.wav"
    write_constant_wav(source, 1000, 2.0)

    monkeypatch.setattr(helper, "resolve_ffmpeg", lambda _value: "ffmpeg")
    monkeypatch.setattr(helper, "write_progress", lambda *_args, **_kwargs: None)

    def fake_cut(_ffmpeg, _audio, output, source_start, source_end, _channel):
        write_constant_wav(output, 1000 + int(source_start), float(source_end - source_start))

    monkeypatch.setattr(helper, "cut_audio", fake_cut)
    monkeypatch.setattr(
        helper,
        "transcribe_qwen3_asr_batch",
        lambda paths, _model, _language: [
            {
                "backend": "qwen3_asr",
                "model": "qwen-test",
                "text": text,
                "segments": [
                    {
                        "start": 0.0,
                        "end": len(text) * 0.2,
                        "text": text,
                        "words": [
                            {"word": char, "start": index * 0.2, "end": (index + 1) * 0.2}
                            for index, char in enumerate(text)
                        ],
                    }
                ],
            }
            for _path in paths
        ],
    )
    args = argparse.Namespace(
        ffmpeg="ffmpeg",
        fps=30.0,
        model="qwen-test",
        language="zh",
        backend="qwen3_asr",
        subtitle_mode="live",
        mode="generate_subtitles_batch",
        segmentation_profile=None,
        generate_engine="v4",
        **max_chars_kwargs,
    )
    batches = [
        {
            "batch_id": "a",
            "audio": str(source),
            "source_start": 0.0,
            "source_end": len(text) * 0.2 + 0.5,
            "timeline_start_frame": 100,
            "fps": 30.0,
            "track_index": 1,
        },
    ]
    return helper.run_generate_subtitles_batch_plan_v4(batches, args, None)
