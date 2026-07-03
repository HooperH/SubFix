#!/usr/bin/env python3
"""Local alignment helper for SubFix.

Outputs a small JSON payload with segment-level start/end/text. The default
path force-aligns existing SubFix subtitle rows with stable-ts, while fixture
mode avoids heavyweight imports for tests.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path
from typing import Any


DEFAULT_MODEL = "small"
DEFAULT_CTC_MODEL = "jonatasgrosman/wav2vec2-large-xlsr-53-chinese-zh-cn"
HELPER_VERSION = "ctc-align-2026-06-16-v2"
TRANSCRIBE_BACKENDS = ("mimo_asr", "qwen3_asr", "mlx_whisper", "openai_whisper")
GENERATED_SUBTITLE_MAX_CHARS = 24
GENERATED_SUBTITLE_PREFERRED_MIN_CHARS = 12
GENERATED_SUBTITLE_PREFERRED_MAX_CHARS = 18
EXTERNAL_TRANSCRIBE_COMMAND_ENV = {
    "mimo_asr": "SUBFIX_MIMO_ASR_CMD",
}
QWEN3_ASR_MODEL = "Qwen/Qwen3-ASR-1.7B"
QWEN3_FORCED_ALIGNER_MODEL = "Qwen/Qwen3-ForcedAligner-0.6B"
DEFAULT_FFMPEG_CANDIDATES = (
    os.path.expanduser("~/.local/bin/ffmpeg"),
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
)
_QWEN3_ASR_MODEL_CACHE: dict[tuple[str, str, str, str | None], tuple[Any, str, str]] = {}
_OPENAI_WHISPER_MODEL_CACHE: dict[str, Any] = {}


def write_payload(path: Path, payload: dict[str, Any]) -> None:
    payload.setdefault("helper_version", HELPER_VERSION)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_progress(path: Path | None, stage: str, message: str, **extra: Any) -> None:
    if not path:
        return
    payload = {
        "stage": stage,
        "message": message,
        "timestamp": time.time(),
        "helper_version": HELPER_VERSION,
    }
    payload.update(extra)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def normalize_segments(raw_payload: dict[str, Any]) -> list[dict[str, Any]]:
    segments = []
    for index, segment in enumerate(raw_payload.get("segments") or [], start=1):
        start = float(segment.get("start") or 0)
        end = float(segment.get("end") or start)
        text = str(segment.get("text") or "").strip()
        if end > start and text:
            normalized = {"index": index, "start": start, "end": end, "text": text}
            if "ctc_confidence" in segment:
                normalized["ctc_confidence"] = float(segment.get("ctc_confidence") or 0.0)
            if "ctc_char_count" in segment:
                normalized["ctc_char_count"] = int(segment.get("ctc_char_count") or 0)
            words = []
            for word in segment.get("words") or []:
                word_start = float(word.get("start") or start)
                word_end = float(word.get("end") or word_start)
                word_text = str(word.get("word") or word.get("text") or "").strip()
                if word_end > word_start and word_text:
                    words.append({"word": word_text, "start": word_start, "end": word_end})
            if words:
                normalized["words"] = words
            segments.append(normalized)
    return segments


def generated_subtitle_text_length(text: str) -> int:
    return len(re.sub(r"\s+", "", str(text or "")))


def normalize_generated_subtitle_text(text: str) -> str:
    value = re.sub(r"\s+", " ", str(text or "").strip())
    return value


def split_generated_subtitle_clause(text: str, max_chars: int = GENERATED_SUBTITLE_MAX_CHARS) -> list[str]:
    value = normalize_generated_subtitle_text(text)
    if not value:
        return []

    chunks: list[str] = []
    soft_breaks = "，、,：:"
    preferred_min = GENERATED_SUBTITLE_PREFERRED_MIN_CHARS
    preferred_max = min(GENERATED_SUBTITLE_PREFERRED_MAX_CHARS, max_chars)

    while generated_subtitle_text_length(value) > max_chars:
        break_at = 0
        upper = min(preferred_max, len(value))
        for index in range(upper, preferred_min - 1, -1):
            if value[index - 1] in soft_breaks:
                break_at = index
                break
        if break_at == 0:
            for index in range(min(max_chars, len(value)), preferred_min - 1, -1):
                if value[index - 1] in soft_breaks:
                    break_at = index
                    break
        if break_at == 0:
            break_at = min(max_chars, len(value))
        chunk = value[:break_at].strip()
        if chunk:
            chunks.append(chunk)
        value = value[break_at:].strip()

    if value:
        chunks.append(value)
    return chunks


def split_generated_subtitle_text(text: str, max_chars: int = GENERATED_SUBTITLE_MAX_CHARS) -> list[str]:
    value = normalize_generated_subtitle_text(text)
    if not value:
        return []

    hard_breaks = "。！？!?；;"
    clauses: list[str] = []
    buffer: list[str] = []
    for char in value:
        buffer.append(char)
        if char in hard_breaks:
            clause = "".join(buffer).strip()
            if clause:
                clauses.append(clause)
            buffer = []
    tail = "".join(buffer).strip()
    if tail:
        clauses.append(tail)

    chunks: list[str] = []
    for clause in clauses:
        chunks.extend(split_generated_subtitle_clause(clause, max_chars=max_chars))
    return chunks


def seconds_to_timeline_frame(seconds: float, fps: float, timeline_start_frame: int) -> int:
    return int(timeline_start_frame + math.floor((float(seconds) * float(fps)) + 0.5))


def distribute_generated_subtitle_frames(
    segment_start_frame: int,
    segment_end_frame: int,
    chunks: list[str],
) -> list[tuple[int, int]]:
    if not chunks:
        return []

    start_frame = int(segment_start_frame)
    end_frame = max(start_frame + len(chunks), int(segment_end_frame))
    total_frames = max(len(chunks), end_frame - start_frame)
    weights = [max(1, generated_subtitle_text_length(chunk)) for chunk in chunks]
    total_weight = max(1, sum(weights))
    frames: list[tuple[int, int]] = []
    cursor = start_frame
    elapsed_weight = 0

    for index, weight in enumerate(weights):
        remaining_chunks = len(chunks) - index - 1
        elapsed_weight += weight
        if index == len(chunks) - 1:
            chunk_end = end_frame
        else:
            proportional = start_frame + int(math.floor((total_frames * elapsed_weight / total_weight) + 0.5))
            chunk_end = max(cursor + 1, proportional)
            chunk_end = min(chunk_end, end_frame - remaining_chunks)
        frames.append((cursor, max(cursor + 1, chunk_end)))
        cursor = frames[-1][1]

    return frames


def generate_subtitle_rows_from_segments(
    segments: list[dict[str, Any]],
    fps: float,
    timeline_start_frame: int = 0,
    max_chars: int = GENERATED_SUBTITLE_MAX_CHARS,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    rate = float(fps or 30.0)
    for source_segment_index, segment in enumerate(segments or [], start=1):
        text = str(segment.get("text") or "").strip()
        start_seconds = float(segment.get("start") or 0.0)
        end_seconds = float(segment.get("end") or start_seconds)
        if not text or end_seconds <= start_seconds:
            continue
        chunks = split_generated_subtitle_text(text, max_chars=max_chars)
        if not chunks:
            continue
        segment_start_frame = seconds_to_timeline_frame(start_seconds, rate, int(timeline_start_frame))
        segment_end_frame = seconds_to_timeline_frame(end_seconds, rate, int(timeline_start_frame))
        frame_ranges = distribute_generated_subtitle_frames(segment_start_frame, segment_end_frame, chunks)
        for chunk, (start_frame, end_frame) in zip(chunks, frame_ranges, strict=True):
            rows.append(
                {
                    "index": len(rows) + 1,
                    "start_frame": start_frame,
                    "end_frame": end_frame,
                    "text": chunk,
                    "source_segment_index": source_segment_index,
                }
            )
    return rows


def milliseconds_to_srt_time(milliseconds: int) -> str:
    value = max(0, int(milliseconds))
    hours = value // 3_600_000
    value %= 3_600_000
    minutes = value // 60_000
    value %= 60_000
    seconds = value // 1000
    millis = value % 1000
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def write_subtitle_rows_to_srt(path: Path, rows: list[dict[str, Any]], fps: float, base_frame: int = 0) -> int:
    rate = float(fps or 30.0)
    sorted_rows = sorted(rows or [], key=lambda row: int(row.get("start_frame") or 0))
    lines: list[str] = []
    written = 0
    for row in sorted_rows:
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        start_frame = int(row.get("start_frame") or 0)
        end_frame = int(row.get("end_frame") or start_frame + 1)
        if end_frame <= start_frame:
            end_frame = start_frame + 1
        start_ms = int(math.floor(((start_frame - base_frame) / rate) * 1000 + 0.5))
        end_ms = int(math.floor(((end_frame - base_frame) / rate) * 1000 + 0.5))
        if end_ms <= start_ms:
            end_ms = start_ms + 1
        written += 1
        lines.extend(
            [
                str(written),
                f"{milliseconds_to_srt_time(start_ms)} --> {milliseconds_to_srt_time(end_ms)}",
                text,
                "",
            ]
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return written


def audio_duration_seconds(audio_path: Path) -> float:
    try:
        with wave.open(str(audio_path), "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate() or 1
            return frames / float(rate)
    except Exception:
        return 0.0


def qwen3_language_name(language: str | None) -> str | None:
    value = str(language or "").strip().lower().replace("_", "-")
    if not value or value == "auto":
        return None
    mapping = {
        "zh": "Chinese",
        "zh-cn": "Chinese",
        "chinese": "Chinese",
        "cmn": "Chinese",
        "yue": "Cantonese",
        "cantonese": "Cantonese",
        "en": "English",
        "en-us": "English",
        "en-gb": "English",
        "english": "English",
    }
    return mapping.get(value, language)


def qwen3_torch_dtype(torch_module: Any) -> Any:
    dtype_name = str(os.getenv("SUBFIX_QWEN3_ASR_DTYPE") or "bfloat16").strip()
    if dtype_name == "auto":
        return None
    return getattr(torch_module, dtype_name, None)


def qwen3_timestamp_value(item: Any, *names: str) -> Any:
    if isinstance(item, dict):
        for name in names:
            if name in item:
                return item[name]
    for name in names:
        if hasattr(item, name):
            return getattr(item, name)
    return None


def qwen3_timestamp_segments(value: Any) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        if node is None:
            return
        if isinstance(node, (list, tuple)):
            for child in node:
                visit(child)
            return
        text = str(qwen3_timestamp_value(node, "text", "word") or "").strip()
        start = qwen3_timestamp_value(node, "start_time", "start", "begin")
        end = qwen3_timestamp_value(node, "end_time", "end", "finish")
        if text and start is not None and end is not None:
            try:
                start_float = float(start)
                end_float = float(end)
            except (TypeError, ValueError):
                return
            if end_float > start_float:
                segments.append({"start": start_float, "end": end_float, "text": text})

    visit(value)
    return segments


def normalize_rows_payload(rows: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(rows or [], start=1):
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        start_frame = int(float(row.get("start_frame") or 0))
        end_frame = int(float(row.get("end_frame") or start_frame + 1))
        normalized.append(
            {
                "index": int(row.get("index") or index),
                "text": text,
                "start_frame": start_frame,
                "end_frame": max(start_frame + 1, end_frame),
            }
        )
    return normalized


def load_rows(rows_json: str | None) -> list[dict[str, Any]]:
    if not rows_json:
        return []
    payload = json.loads(Path(rows_json).read_text(encoding="utf-8"))
    rows = payload.get("rows") if isinstance(payload, dict) else payload
    return normalize_rows_payload(rows)


def load_batch_plan(batch_plan_json: str | None) -> list[dict[str, Any]]:
    if not batch_plan_json:
        return []
    payload = json.loads(Path(batch_plan_json).read_text(encoding="utf-8"))
    raw_batches = payload.get("batches") if isinstance(payload, dict) else payload
    batches: list[dict[str, Any]] = []
    for index, batch in enumerate(raw_batches or [], start=1):
        rows = normalize_rows_payload(batch.get("rows") or [])
        if not rows:
            continue
        batches.append(
            {
                "batch_id": str(batch.get("batch_id") or index),
                "audio": str(batch.get("audio") or batch.get("audio_path") or ""),
                "source_start": float(batch.get("source_start") or 0.0),
                "source_end": float(batch["source_end"]) if batch.get("source_end") is not None else None,
                "timeline_start_frame": int(float(batch.get("timeline_start_frame") or 0)),
                "fps": float(batch.get("fps") or 30.0),
                "rows": rows,
            }
        )
    return batches


def load_transcribe_windows(windows_json: str | None) -> list[dict[str, Any]]:
    if not windows_json:
        return []
    payload = json.loads(Path(windows_json).read_text(encoding="utf-8"))
    raw_windows = payload.get("windows") if isinstance(payload, dict) else payload
    windows: list[dict[str, Any]] = []
    for index, window in enumerate(raw_windows or [], start=1):
        if not isinstance(window, dict):
            continue
        source_start = float(window.get("source_start") or 0.0)
        source_end = window.get("source_end")
        source_end_float = float(source_end) if source_end is not None else None
        if source_end_float is not None and source_end_float <= source_start:
            continue
        windows.append(
            {
                "window_id": str(window.get("window_id") or index),
                "source_start": source_start,
                "source_end": source_end_float,
                "row_label": str(window.get("row_label") or ""),
                "review_type": str(window.get("review_type") or ""),
            }
        )
    return windows


def build_alignment_text(rows: list[dict[str, Any]]) -> str:
    return "\n".join(str(row.get("text") or "").strip() for row in rows if str(row.get("text") or "").strip())


def normalize_ctc_text(text: str) -> str:
    value = str(text or "").lower()
    value = re.sub(r"[\s\-_—–]+", "", value)
    value = re.sub(r"[，。！？、；：,.!?;:\"'“”‘’（）()《》【】\[\]{}<>…·/\\|]+", "", value)
    return value


def ctc_units_for_rows(rows: list[dict[str, Any]]) -> list[list[str]]:
    units: list[list[str]] = []
    for row in rows:
        row_units = list(normalize_ctc_text(str(row.get("text") or "")))
        if not row_units:
            raise RuntimeError(f"CTC 对齐文本为空: 字幕 #{row.get('index', len(units) + 1)}")
        units.append(row_units)
    return units


def rows_to_seed_segments(
    rows: list[dict[str, Any]],
    fps: float,
    timeline_start_frame: int,
    max_seconds: float | None = None,
) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    previous_start: float | None = None
    frame_tolerance = max(0.05, 2.0 / max(float(fps), 1.0))
    for row in rows:
        start = (float(row["start_frame"]) - timeline_start_frame) / fps
        end = (float(row["end_frame"]) - timeline_start_frame) / fps
        row_label = row.get("index", len(segments) + 1)
        if start < -frame_tolerance:
            raise RuntimeError(f"stable-ts seed 超出音频片段开头: 字幕 #{row_label}")
        if end <= start:
            raise RuntimeError(f"stable-ts seed 空时间段: 字幕 #{row_label}")
        if previous_start is not None and start <= previous_start:
            raise RuntimeError(f"stable-ts seed 非递增: 字幕 #{row_label}")
        if max_seconds is not None and start > max_seconds + frame_tolerance:
            raise RuntimeError(f"stable-ts seed 超出音频片段结尾: 字幕 #{row_label}")
        if max_seconds is not None and end > max_seconds + frame_tolerance:
            raise RuntimeError(f"stable-ts seed 超出音频片段结尾: 字幕 #{row_label}")
        previous_start = start
        segments.append({"start": round(start, 3), "end": round(end, 3), "text": row["text"]})
    return segments


def alignment_unit_count(text: str) -> int:
    return len(re.sub(r"\s+", "", str(text or "")))


def result_to_payload(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return result
    if hasattr(result, "to_dict"):
        return result.to_dict()
    if hasattr(result, "segments"):
        segments = []
        for segment in result.segments:
            if isinstance(segment, dict):
                segments.append(segment)
            elif hasattr(segment, "to_dict"):
                segments.append(segment.to_dict())
            else:
                segments.append(
                    {
                        "start": getattr(segment, "start", 0),
                        "end": getattr(segment, "end", 0),
                        "text": getattr(segment, "text", ""),
                        "words": getattr(segment, "words", []),
                    }
                )
        return {"segments": segments}
    raise RuntimeError("stable-ts 对齐结果格式不可识别")


def split_aligned_segments_by_rows(rows: list[dict[str, Any]], segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    timed_words: list[dict[str, Any]] = []
    for segment in segments:
        for word in segment.get("words") or []:
            word_text = str(word.get("word") or word.get("text") or "").strip()
            word_start = float(word.get("start") or 0)
            word_end = float(word.get("end") or word_start)
            units = alignment_unit_count(word_text)
            if units > 0 and word_end > word_start:
                timed_words.append({"word": word_text, "start": word_start, "end": word_end, "units": units})

    if not timed_words:
        raise RuntimeError(f"stable-ts 分段数量不匹配: 字幕 {len(rows)} 条，对齐结果 {len(segments)} 段，且无 word 时间戳可重切")

    row_units = [max(1, alignment_unit_count(str(row.get("text") or ""))) for row in rows]
    total_row_units = sum(row_units)
    total_word_units = sum(word["units"] for word in timed_words)
    if total_row_units <= 0 or total_word_units <= 0:
        raise RuntimeError("stable-ts word 时间戳不可用于重切字幕行")

    unit_ratio = total_word_units / total_row_units
    split_segments: list[dict[str, Any]] = []
    word_index = 0
    consumed_target_units = 0
    consumed_word_units = 0

    for row, units in zip(rows, row_units, strict=True):
        start_index = min(word_index, len(timed_words) - 1)
        consumed_target_units += units
        target_word_units = max(consumed_word_units + 1, int(round(consumed_target_units * unit_ratio)))

        end_index = start_index
        while end_index < len(timed_words) - 1 and consumed_word_units + timed_words[end_index]["units"] < target_word_units:
            consumed_word_units += timed_words[end_index]["units"]
            end_index += 1

        if end_index == len(timed_words) - 1:
            consumed_word_units = total_word_units
        else:
            consumed_word_units += timed_words[end_index]["units"]

        start_word = timed_words[start_index]
        end_word = timed_words[end_index]
        split_segments.append(
            {
                "index": row.get("index"),
                "start": start_word["start"],
                "end": end_word["end"],
                "text": row.get("text") or "",
            }
        )
        word_index = min(end_index + 1, len(timed_words) - 1)

    return split_segments


def apply_alignment_to_rows(
    rows: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    fps: float,
    timeline_start_frame: int,
    allow_non_monotonic: bool = False,
) -> list[dict[str, Any]]:
    if len(segments) != len(rows):
        segments = split_aligned_segments_by_rows(rows, segments)

    aligned: list[dict[str, Any]] = []
    previous_start: int | None = None
    for row, segment in zip(rows, segments, strict=True):
        segment_start = float(segment.get("start") or 0)
        segment_end = float(segment.get("end") or segment_start)
        if segment_end <= segment_start:
            raise RuntimeError("stable-ts 返回空时间段")
        start_frame = int(round(timeline_start_frame + segment_start * fps))
        original_duration = max(1, int(row["end_frame"]) - int(row["start_frame"]))
        non_monotonic_candidate = previous_start is not None and start_frame < previous_start
        if non_monotonic_candidate and not allow_non_monotonic:
            raise RuntimeError("stable-ts 返回非单调时间")
        if not non_monotonic_candidate:
            previous_start = start_frame
        aligned_row = {
            "index": row["index"],
            "text": row["text"],
            "start": round(segment_start, 3),
            "end": round(segment_end, 3),
            "start_frame": start_frame,
            "end_frame": start_frame + original_duration,
            "original_start_frame": row["start_frame"],
            "original_end_frame": row["end_frame"],
        }
        if non_monotonic_candidate:
            aligned_row["non_monotonic_candidate"] = True
        aligned.append(aligned_row)
    return aligned


def normalize_aligned_segments_for_rows(raw_payload: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    segments = normalize_segments(raw_payload)
    if len(segments) == len(rows):
        return segments
    return split_aligned_segments_by_rows(rows, segments)


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * ratio))))
    return ordered[index]


def decode_pcm_samples(raw: bytes, sample_width: int, channels: int) -> list[float]:
    if sample_width != 2:
        raise RuntimeError(f"unsupported wav sample width: {sample_width}")
    step = sample_width * channels
    samples = []
    for offset in range(0, len(raw) - step + 1, step):
        channel_values = [
            int.from_bytes(raw[offset + channel * sample_width : offset + (channel + 1) * sample_width], "little", signed=True)
            for channel in range(channels)
        ]
        samples.append(sum(channel_values) / (len(channel_values) * 32768.0))
    return samples


def read_wav_mono_samples(wav_path: Path) -> tuple[list[float], int]:
    with wave.open(str(wav_path), "rb") as handle:
        sample_rate = handle.getframerate()
        channels = handle.getnchannels()
        sample_width = handle.getsampwidth()
        raw = handle.readframes(handle.getnframes())
    return decode_pcm_samples(raw, sample_width, channels), sample_rate


def segments_from_ctc_char_segments(
    rows: list[dict[str, Any]],
    char_segments: list[dict[str, Any]],
    row_units: list[list[str]] | None = None,
    fps: float | None = None,
    timeline_start_frame: int = 0,
) -> list[dict[str, Any]]:
    row_units = row_units if row_units is not None else ctc_units_for_rows(rows)
    normalized_chars = []
    for item in char_segments or []:
        char_text = normalize_ctc_text(str(item.get("char") or item.get("text") or ""))
        if not char_text:
            continue
        start = float(item.get("start") or 0.0)
        end = float(item.get("end") or start)
        score = float(item.get("score") or item.get("confidence") or 0.0)
        for char in char_text:
            normalized_chars.append({"char": char, "start": start, "end": end, "score": score})

    required_units = sum(len(units) for units in row_units)
    if len(normalized_chars) < required_units:
        raise RuntimeError(f"CTC 字符数量不匹配: 字幕需要 {required_units} 字，结果 {len(normalized_chars)} 字")

    segments: list[dict[str, Any]] = []
    cursor = 0
    for row, units in zip(rows, row_units, strict=True):
        count = len(units)
        row_chars = normalized_chars[cursor : cursor + count]
        cursor += count
        if not row_chars:
            if count > 0 or fps is None:
                raise RuntimeError(f"CTC 字符时间为空: 字幕 #{row.get('index', len(segments) + 1)}")
            start = max(0.0, (float(row.get("start_frame") or 0) - float(timeline_start_frame or 0)) / max(float(fps), 1.0))
            end = max(start + 0.001, (float(row.get("end_frame") or 0) - float(timeline_start_frame or 0)) / max(float(fps), 1.0))
            confidence = 0.0
        else:
            start = float(row_chars[0]["start"])
            end = max(float(row_chars[-1]["end"]), start + 0.001)
            confidence = sum(float(char.get("score") or 0.0) for char in row_chars) / max(1, len(row_chars))
        segments.append(
            {
                "index": row.get("index"),
                "start": round(start, 3),
                "end": round(end, 3),
                "text": row.get("text") or "",
                "ctc_confidence": round(confidence, 4),
                "ctc_char_count": count,
            }
        )
    return segments


def ctc_token_id(tokenizer: Any, char: str) -> int:
    token_id = tokenizer.convert_tokens_to_ids(char)
    unk_id = getattr(tokenizer, "unk_token_id", None)
    if token_id is not None and token_id != unk_id:
        return int(token_id)
    encoded = tokenizer(char, add_special_tokens=False)
    input_ids = getattr(encoded, "input_ids", None)
    if input_ids is None and isinstance(encoded, dict):
        input_ids = encoded.get("input_ids")
    if input_ids and len(input_ids) == 1 and input_ids[0] != unk_id:
        return int(input_ids[0])
    raise RuntimeError(f"CTC 模型词表不支持字符: {char}")


def ctc_units_and_token_ids_for_tokenizer(
    rows: list[dict[str, Any]],
    tokenizer: Any,
) -> tuple[list[list[str]], list[int], dict[str, int]]:
    row_units: list[list[str]] = []
    token_ids: list[int] = []
    skipped_chars: dict[str, int] = {}
    for row in rows:
        units: list[str] = []
        for char in normalize_ctc_text(str(row.get("text") or "")):
            try:
                token_id = ctc_token_id(tokenizer, char)
            except RuntimeError:
                skipped_chars[char] = skipped_chars.get(char, 0) + 1
                continue
            units.append(char)
            token_ids.append(token_id)
        row_units.append(units)
    if not token_ids:
        raise RuntimeError("没有可用于 CTC 文本对齐的模型词表支持字符")
    return row_units, token_ids, skipped_chars


def ctc_forced_align_char_segments(
    emissions: Any,
    token_ids: list[int],
    transcript_chars: list[str],
    blank_id: int,
    seconds_per_frame: float,
) -> list[dict[str, Any]]:
    import torch  # type: ignore

    if not token_ids:
        raise RuntimeError("CTC 对齐文本为空")
    if emissions.numel() == 0:
        raise RuntimeError("CTC 模型未返回 emission")

    emissions = emissions.cpu()
    expanded_ids: list[int] = []
    expanded_char_indices: list[int | None] = []
    for index, token_id in enumerate(token_ids):
        if index > 0 and token_ids[index - 1] == token_id:
            expanded_ids.append(blank_id)
            expanded_char_indices.append(None)
        expanded_ids.append(token_id)
        expanded_char_indices.append(index)

    frame_count = int(emissions.size(0))
    token_count = len(expanded_ids)
    trellis = torch.full((frame_count + 1, token_count + 1), -float("inf"))
    trellis[0, 0] = 0.0
    for frame_index in range(frame_count):
        trellis[frame_index + 1, 0] = trellis[frame_index, 0] + emissions[frame_index, blank_id]
        for token_index, token_id in enumerate(expanded_ids, start=1):
            stay = trellis[frame_index, token_index] + emissions[frame_index, blank_id]
            change = trellis[frame_index, token_index - 1] + emissions[frame_index, token_id]
            trellis[frame_index + 1, token_index] = torch.maximum(stay, change)

    end_frame = int(torch.argmax(trellis[:, token_count]).item())
    if end_frame <= 0:
        end_frame = frame_count

    points: list[dict[str, Any]] = []
    token_index = token_count
    frame_index = end_frame
    while token_index > 0 and frame_index > 0:
        token_id = expanded_ids[token_index - 1]
        stay = trellis[frame_index - 1, token_index] + emissions[frame_index - 1, blank_id]
        change = trellis[frame_index - 1, token_index - 1] + emissions[frame_index - 1, token_id]
        if change > stay:
            score = float(torch.exp(emissions[frame_index - 1, token_id]).item())
            char_index = expanded_char_indices[token_index - 1]
            if char_index is not None:
                points.append({"token_index": char_index, "frame": frame_index - 1, "score": score})
            token_index -= 1
        frame_index -= 1

    if token_index > 0:
        raise RuntimeError("CTC 对齐失败: 无法回溯完整文本")

    points.reverse()
    char_segments = []
    for point_index, point in enumerate(points):
        frame = int(point["frame"])
        next_frame = int(points[point_index + 1]["frame"]) if point_index + 1 < len(points) else frame + 1
        start = frame * seconds_per_frame
        end = max(start + seconds_per_frame, next_frame * seconds_per_frame)
        char_segments.append(
            {
                "char": transcript_chars[point["token_index"]],
                "start": round(start, 3),
                "end": round(end, 3),
                "score": round(float(point["score"]), 4),
            }
        )
    return char_segments


def detect_speech_regions(
    wav_path: Path,
    window_seconds: float = 0.01,
    merge_gap_seconds: float = 0.08,
    min_region_seconds: float = 0.04,
) -> tuple[list[dict[str, float]], list[float], dict[str, Any]]:
    with wave.open(str(wav_path), "rb") as handle:
        sample_rate = handle.getframerate()
        channels = handle.getnchannels()
        sample_width = handle.getsampwidth()
        raw = handle.readframes(handle.getnframes())

    samples = decode_pcm_samples(raw, sample_width, channels)
    window_size = max(1, int(sample_rate * window_seconds))
    windows: list[dict[str, float]] = []
    for start in range(0, len(samples), window_size):
        chunk = samples[start : start + window_size]
        if not chunk:
            continue
        rms = math.sqrt(sum(sample * sample for sample in chunk) / len(chunk))
        peak = max(abs(sample) for sample in chunk)
        windows.append({"start": start / sample_rate, "end": min(len(samples), start + len(chunk)) / sample_rate, "rms": rms, "peak": peak})

    rms_values = [window["rms"] for window in windows]
    peak_values = [window["peak"] for window in windows]
    max_rms = max(rms_values or [0.0])
    noise_floor = percentile(rms_values, 0.20)
    rms_threshold = max(0.012, noise_floor * 3.0, max_rms * 0.18)
    peak_threshold = max(0.025, percentile(peak_values, 0.20) * 3.0, max(peak_values or [0.0]) * 0.18)

    regions: list[dict[str, float]] = []
    active_start: float | None = None
    active_end: float | None = None
    for window in windows:
        active = window["rms"] >= rms_threshold or window["peak"] >= peak_threshold
        if active:
            if active_start is None:
                active_start = window["start"]
            active_end = window["end"]
        elif active_start is not None and active_end is not None:
            regions.append({"start": active_start, "end": active_end})
            active_start = None
            active_end = None
    if active_start is not None and active_end is not None:
        regions.append({"start": active_start, "end": active_end})

    merged: list[dict[str, float]] = []
    for region in regions:
        if region["end"] - region["start"] < min_region_seconds:
            continue
        if merged and region["start"] - merged[-1]["end"] <= merge_gap_seconds:
            merged[-1]["end"] = max(merged[-1]["end"], region["end"])
        else:
            merged.append({"start": round(region["start"], 3), "end": round(region["end"], 3)})

    onsets = [region["start"] for region in merged]
    diagnostic = {
        "speech_region_count": len(merged),
        "speech_onset_count": len(onsets),
        "rms_threshold": round(rms_threshold, 6),
        "peak_threshold": round(peak_threshold, 6),
        "sample_rate": sample_rate,
        "window_seconds": window_seconds,
    }
    return merged, onsets, diagnostic


def is_executable_file(path: str) -> bool:
    return bool(path) and Path(path).is_file() and os.access(path, os.X_OK)


def ffmpeg_candidates() -> list[str]:
    override = os.environ.get("SUBFIX_FFMPEG_CANDIDATES")
    if override is not None:
        return [part for part in override.split(os.pathsep) if part]
    return list(DEFAULT_FFMPEG_CANDIDATES)


def ensure_ffmpeg_path_env() -> None:
    candidate_dirs = [str(Path(candidate).parent) for candidate in ffmpeg_candidates()]
    existing_parts = [part for part in os.environ.get("PATH", "").split(os.pathsep) if part]
    merged: list[str] = []
    for part in candidate_dirs + existing_parts:
        if part and part not in merged:
            merged.append(part)
    os.environ["PATH"] = os.pathsep.join(merged)


def resolve_ffmpeg(requested: str | None) -> str:
    ensure_ffmpeg_path_env()
    checked: list[str] = []
    if requested and requested != "ffmpeg":
        checked.append(requested)
        if is_executable_file(requested):
            return requested

    for candidate in ffmpeg_candidates():
        checked.append(candidate)
        if is_executable_file(candidate):
            return candidate

    path_match = shutil.which(requested or "ffmpeg") or shutil.which("ffmpeg")
    if path_match:
        return path_match

    checked_text = ", ".join(checked) if checked else "无固定候选路径"
    raise RuntimeError(
        "未找到 ffmpeg；已检查: "
        + checked_text
        + "；请运行 which ffmpeg 确认路径，或安装/链接到 ~/.local/bin/ffmpeg"
    )


def cut_audio(
    ffmpeg: str,
    audio: Path,
    output: Path,
    source_start: float,
    source_end: float | None,
    audio_channel_index: int | None = None,
) -> None:
    cmd = [ffmpeg, "-y", "-hide_banner", "-nostdin", "-ss", f"{source_start:.3f}"]
    if source_end is not None and source_end > source_start:
        cmd.extend(["-to", f"{source_end:.3f}"])
    cmd.extend(["-i", str(audio)])
    if audio_channel_index is not None and audio_channel_index > 0:
        cmd.extend(["-filter:a", f"pan=mono|c0=c{audio_channel_index - 1}"])
    cmd.extend(["-ac", "1", "-ar", "16000", "-vn", str(output)])
    subprocess.run(cmd, check=True, text=True, capture_output=True)


def transcribe_mlx_whisper(audio_path: Path, model: str, language: str | None) -> dict[str, Any]:
    try:
        import mlx_whisper  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on local setup
        raise RuntimeError("ASR 环境未安装，请先运行 setup_asr_env.sh 安装 mlx-whisper") from exc

    kwargs: dict[str, Any] = {"path_or_hf_repo": model, "word_timestamps": True}
    if language and language != "auto":
        kwargs["language"] = language
    try:
        return mlx_whisper.transcribe(str(audio_path), **kwargs)
    except TypeError:
        kwargs.pop("path_or_hf_repo", None)
        kwargs["model"] = model
        return mlx_whisper.transcribe(str(audio_path), **kwargs)


def transcribe_openai_whisper(audio_path: Path, model: str, language: str | None) -> dict[str, Any]:
    try:
        import whisper  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on local setup
        raise RuntimeError("OpenAI Whisper 环境未安装，请先运行 setup_asr_env.sh 安装 stable-ts/openai-whisper") from exc

    try:
        model_name = model or DEFAULT_MODEL
        whisper_model = _OPENAI_WHISPER_MODEL_CACHE.get(model_name)
        if whisper_model is None:
            whisper_model = whisper.load_model(model_name)
            _OPENAI_WHISPER_MODEL_CACHE[model_name] = whisper_model
        kwargs: dict[str, Any] = {
            "word_timestamps": True,
            "fp16": False,
            "temperature": 0,
            "condition_on_previous_text": False,
            "no_speech_threshold": 0.45,
            "logprob_threshold": -0.8,
            "compression_ratio_threshold": 2.4,
            "hallucination_silence_threshold": 1.0,
        }
        if language and language != "auto":
            kwargs["language"] = language
        try:
            return whisper_model.transcribe(str(audio_path), **kwargs)
        except TypeError as exc:
            if "hallucination_silence_threshold" not in str(exc):
                raise
            kwargs.pop("hallucination_silence_threshold", None)
            return whisper_model.transcribe(str(audio_path), **kwargs)
    except Exception as exc:  # pragma: no cover - depends on local setup/model cache
        raise RuntimeError(f"OpenAI Whisper 转写失败: {exc}") from exc


def transcribe_qwen3_asr(audio_path: Path, model: str, language: str | None) -> dict[str, Any]:
    try:
        import torch  # type: ignore
        from qwen_asr import Qwen3ASRModel  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on optional local setup
        raise RuntimeError("Qwen3-ASR 环境未安装，请安装 qwen-asr 或继续使用 Whisper fallback") from exc

    model_name = os.getenv("SUBFIX_QWEN3_ASR_MODEL") or QWEN3_ASR_MODEL
    aligner_name = os.getenv("SUBFIX_QWEN3_ALIGNER_MODEL") or QWEN3_FORCED_ALIGNER_MODEL
    device_map = os.getenv("SUBFIX_QWEN3_ASR_DEVICE_MAP") or "auto"
    dtype = qwen3_torch_dtype(torch)

    init_kwargs: dict[str, Any] = {
        "device_map": device_map,
        "max_inference_batch_size": int(os.getenv("SUBFIX_QWEN3_ASR_MAX_BATCH") or "8"),
        "max_new_tokens": int(os.getenv("SUBFIX_QWEN3_ASR_MAX_NEW_TOKENS") or "512"),
        "forced_aligner": aligner_name,
        "forced_aligner_kwargs": {"device_map": device_map},
    }
    if dtype is not None:
        init_kwargs["dtype"] = dtype
        init_kwargs["forced_aligner_kwargs"]["dtype"] = dtype

    cache_key = (model_name, aligner_name, device_map, str(dtype))
    try:
        cached_model = _QWEN3_ASR_MODEL_CACHE.get(cache_key)
        if cached_model is None:
            qwen_model = Qwen3ASRModel.from_pretrained(model_name, **init_kwargs)
            _QWEN3_ASR_MODEL_CACHE[cache_key] = (qwen_model, aligner_name, device_map)
        else:
            qwen_model, aligner_name, device_map = cached_model
        results = qwen_model.transcribe(
            audio=str(audio_path),
            language=qwen3_language_name(language),
            return_time_stamps=True,
        )
    except Exception as exc:  # pragma: no cover - depends on optional local setup/model cache
        raise RuntimeError(f"Qwen3-ASR 转写失败: {exc}") from exc

    first_result = results[0] if isinstance(results, list) and results else results
    text = str(qwen3_timestamp_value(first_result, "text") or "").strip()
    language_name = qwen3_timestamp_value(first_result, "language")
    segments = qwen3_timestamp_segments(qwen3_timestamp_value(first_result, "time_stamps", "timestamps", "segments"))
    if not segments and text:
        duration = audio_duration_seconds(audio_path)
        segments = [{"start": 0.0, "end": max(0.01, duration), "text": text}]

    return {
        "backend": "qwen3_asr",
        "model": model_name,
        "language": language_name,
        "segments": segments,
        "text": text,
        "diagnostic": {
            "forced_aligner": aligner_name,
            "device_map": device_map,
            "requested_model": model,
        },
    }


def transcribe_external_backend(audio_path: Path, model: str, language: str | None, backend: str) -> dict[str, Any]:
    env_name = EXTERNAL_TRANSCRIBE_COMMAND_ENV.get(backend)
    command_template = os.getenv(env_name or "")
    if not command_template:
        raise RuntimeError(f"{backend} 未配置本地命令环境变量 {env_name}")

    with tempfile.TemporaryDirectory(prefix=f"subfix_{backend}_") as tmp_dir:
        output_path = Path(tmp_dir) / "transcribe.json"
        # External ASR commands are opt-in via env vars; quote substituted paths
        # so a media filename cannot change the shell command structure.
        command = command_template.format(
            audio=shlex.quote(str(audio_path)),
            output=shlex.quote(str(output_path)),
            model=shlex.quote(str(model)),
            language=shlex.quote(str(language or "auto")),
        )
        result = subprocess.run(command, shell=True, text=True, capture_output=True)
        if result.returncode != 0:
            error_text = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"{backend} 执行失败: {error_text}")
        if output_path.exists():
            payload_text = output_path.read_text(encoding="utf-8")
        else:
            payload_text = result.stdout or ""
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{backend} 输出不是合法 JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"{backend} 输出 JSON 必须是对象")
        if payload.get("ok") is False:
            raise RuntimeError(str(payload.get("error") or f"{backend} 返回失败"))
        return payload


def transcribe_with_backend(audio_path: Path, model: str, language: str | None, backend: str = "auto") -> dict[str, Any]:
    requested_backend = backend or "auto"
    candidates = list(TRANSCRIBE_BACKENDS) if requested_backend == "auto" else [requested_backend]
    fallback_errors: list[str] = []

    for candidate in candidates:
        try:
            if candidate == "mlx_whisper":
                payload = transcribe_mlx_whisper(audio_path, model, language)
            elif candidate == "openai_whisper":
                payload = transcribe_openai_whisper(audio_path, model, language)
            elif candidate == "qwen3_asr":
                payload = transcribe_qwen3_asr(audio_path, model, language)
            elif candidate in EXTERNAL_TRANSCRIBE_COMMAND_ENV:
                payload = transcribe_external_backend(audio_path, model, language, candidate)
            else:
                raise RuntimeError(f"未知 ASR backend: {candidate}")
        except Exception as exc:
            fallback_errors.append(f"{candidate}: {exc}")
            continue

        payload = dict(payload)
        payload["backend"] = candidate
        payload["model"] = str(payload.get("model") or model)
        if fallback_errors:
            payload["fallback_errors"] = fallback_errors
        return payload

    raise RuntimeError("ASR backend 全部不可用: " + "；".join(fallback_errors))


def transcribe(audio_path: Path, model: str, language: str | None) -> dict[str, Any]:
    return transcribe_mlx_whisper(audio_path, model, language)


def run_transcribe_windows(
    audio_path: Path,
    windows: list[dict[str, Any]],
    args: argparse.Namespace,
    progress_path: Path | None,
) -> dict[str, Any]:
    if not windows:
        raise RuntimeError("没有可转写的局部窗口")
    ffmpeg_path = resolve_ffmpeg(args.ffmpeg)
    output_windows: list[dict[str, Any]] = []
    diagnostic: dict[str, Any] = {
        "mode": args.mode,
        "ffmpeg": ffmpeg_path,
        "window_count": len(windows),
        "requested_backend": args.backend,
    }
    backend = ""
    model = ""
    fallback_errors: list[str] = []

    with tempfile.TemporaryDirectory(prefix="subfix_asr_windows_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        for index, window in enumerate(windows, start=1):
            window_id = str(window.get("window_id") or index)
            source_start = float(window.get("source_start") or 0.0)
            source_end = window.get("source_end")
            source_end_float = float(source_end) if source_end is not None else None
            audio_channel_index = int(window.get("audio_channel_index")) if window.get("audio_channel_index") else None
            label = str(window.get("row_label") or window_id)
            write_progress(
                progress_path,
                "transcribe_window",
                f"局部复核 {index}/{len(windows)}｜字幕 {label}",
                batch_index=index,
                total_batches=len(windows),
            )
            cut_path = tmp_path / f"window_{index:04d}.wav"
            try:
                cut_audio(ffmpeg_path, audio_path, cut_path, source_start, source_end_float, audio_channel_index)
                speech_regions, speech_onsets, onset_diagnostic = detect_speech_regions(cut_path)
                raw_payload = transcribe_with_backend(cut_path, args.model, args.language, args.backend)
                raw_segments = normalize_segments(raw_payload)
                window_backend = str(raw_payload.get("backend") or "")
                window_model = str(raw_payload.get("model") or args.model)
                backend = backend or window_backend
                model = model or window_model
                if raw_payload.get("fallback_errors"):
                    fallback_errors = list(raw_payload.get("fallback_errors") or [])
                output_windows.append(
                    {
                        "ok": True,
                        "window_id": window_id,
                        "review_type": window.get("review_type") or "",
                        "row_label": label,
                        "source_start": source_start,
                        "source_end": source_end_float,
                        "backend": window_backend,
                        "model": window_model,
                        "segments": raw_segments,
                        "speech_onsets": speech_onsets,
                        "speech_regions": speech_regions,
                        "text": str(raw_payload.get("text") or "").strip(),
                        "diagnostic": {
                            "cut_audio_bytes": cut_path.stat().st_size if cut_path.exists() else 0,
                            "raw_segment_count": len(raw_payload.get("segments") or []),
                            "segment_count": len(raw_segments),
                            "text_length": len(str(raw_payload.get("text") or "").strip()),
                            **onset_diagnostic,
                        },
                    }
                )
            except Exception as exc:
                output_windows.append(
                    {
                        "ok": False,
                        "window_id": window_id,
                        "review_type": window.get("review_type") or "",
                        "row_label": label,
                        "source_start": source_start,
                        "source_end": source_end_float,
                        "error": str(exc),
                        "segments": [],
                    }
                )

    if fallback_errors:
        diagnostic["fallback_errors"] = fallback_errors
    return {
        "backend": backend,
        "model": model or args.model,
        "segments": [],
        "text": "",
        "windows": output_windows,
        "diagnostic": diagnostic,
    }


def align_text_rows(
    audio_path: Path,
    rows: list[dict[str, Any]],
    model_name: str,
    language: str,
) -> dict[str, Any]:
    try:
        import stable_whisper  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on local setup
        raise RuntimeError("stable-ts 环境未安装，请先运行 setup_asr_env.sh 安装 stable-ts[mlx]") from exc

    alignment_text = build_alignment_text(rows)
    if not alignment_text:
        raise RuntimeError("没有可用于 stable-ts 文本对齐的字幕文本")

    try:
        model = stable_whisper.load_mlx_whisper(model_name)
    except Exception as exc:  # pragma: no cover - depends on local setup/model cache
        raise RuntimeError(f"stable-ts MLX 模型加载失败: {exc}") from exc

    try:
        result = model.align(
            str(audio_path),
            alignment_text,
            language=language or "zh",
            original_split=True,
            regroup=False,
            verbose=False,
        )
    except Exception as exc:  # pragma: no cover - depends on local setup/model cache
        raise RuntimeError(f"stable-ts 文本对齐失败: {exc}") from exc

    return result_to_payload(result)


def wav_duration_seconds(audio_path: Path) -> float:
    with wave.open(str(audio_path), "rb") as handle:
        frames = handle.getnframes()
        rate = handle.getframerate()
    return frames / float(rate or 16000)


def whisperx_align_text_rows(
    audio_path: Path,
    rows: list[dict[str, Any]],
    language: str,
    device: str = "cpu",
) -> dict[str, Any]:
    try:
        import whisperx  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on local setup
        raise RuntimeError("WhisperX 环境未安装，请先运行 setup_asr_env.sh 安装 whisperx") from exc

    alignment_text = build_alignment_text(rows).replace("\n", " ").strip()
    if not alignment_text:
        raise RuntimeError("没有可用于 WhisperX 文本对齐的字幕文本")

    duration = wav_duration_seconds(audio_path)
    try:
        audio = whisperx.load_audio(str(audio_path))
        model_a, metadata = whisperx.load_align_model(language_code=language or "zh", device=device)
        result = whisperx.align(
            [{"start": 0.0, "end": duration, "text": alignment_text}],
            model_a,
            metadata,
            audio,
            device,
            return_char_alignments=True,
        )
    except Exception as exc:  # pragma: no cover - depends on local setup/model cache
        raise RuntimeError(f"WhisperX 文本对齐失败: {exc}") from exc

    payload = result_to_payload(result)
    if payload.get("word_segments") and not payload.get("segments"):
        payload["segments"] = [{"start": 0.0, "end": duration, "text": alignment_text, "words": payload["word_segments"]}]
    elif payload.get("word_segments") and payload.get("segments"):
        for segment in payload["segments"]:
            if isinstance(segment, dict) and not segment.get("words"):
                segment["words"] = payload["word_segments"]
                break
    return payload


def load_ctc_model_bundle(model_name: str, progress_path: Path | None = None, **progress_extra: Any) -> dict[str, Any]:
    try:
        import torch  # type: ignore
        from transformers import AutoModelForCTC, AutoProcessor  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on local setup
        raise RuntimeError("CTC 环境未安装，请先运行 setup_asr_env.sh 安装 torch/transformers") from exc

    selected_model = model_name if model_name and model_name != DEFAULT_MODEL else DEFAULT_CTC_MODEL
    try:
        write_progress(progress_path, "load_ctc_model", f"正在加载 CTC 模型: {selected_model}", **progress_extra)
        processor = AutoProcessor.from_pretrained(selected_model)
        model = AutoModelForCTC.from_pretrained(selected_model)
        model.eval()
    except Exception as exc:  # pragma: no cover - depends on local setup/model cache
        raise RuntimeError(f"CTC 模型加载失败: {exc}") from exc
    return {"torch": torch, "processor": processor, "model": model, "selected_model": selected_model}


def ctc_align_text_rows_with_bundle(
    audio_path: Path,
    rows: list[dict[str, Any]],
    bundle: dict[str, Any],
    language: str,
    fps: float | None = None,
    timeline_start_frame: int = 0,
    progress_path: Path | None = None,
    **progress_extra: Any,
) -> dict[str, Any]:
    processor = bundle["processor"]
    model = bundle["model"]
    torch = bundle["torch"]
    selected_model = str(bundle.get("selected_model") or DEFAULT_CTC_MODEL)
    try:
        samples, sample_rate = read_wav_mono_samples(audio_path)
        write_progress(progress_path, "run_ctc_model", "正在执行 CTC 推理", **progress_extra)
        inputs = processor(samples, sampling_rate=sample_rate, return_tensors="pt", padding=False)
        with torch.no_grad():
            logits = model(**inputs).logits[0]
        emissions = torch.log_softmax(logits, dim=-1)
    except Exception as exc:  # pragma: no cover - depends on local setup/model cache
        raise RuntimeError(f"CTC 推理失败: {exc}") from exc

    tokenizer = getattr(processor, "tokenizer", processor)
    blank_id = getattr(tokenizer, "pad_token_id", None)
    if blank_id is None:
        blank_id = 0
    row_units, token_ids, skipped_chars = ctc_units_and_token_ids_for_tokenizer(rows, tokenizer)
    transcript_chars = [char for units in row_units for char in units]
    duration = len(samples) / float(sample_rate or 16000)
    seconds_per_frame = duration / max(1, int(emissions.size(0)))
    write_progress(progress_path, "forced_align", "正在生成逐字强制对齐结果", **progress_extra)
    char_segments = ctc_forced_align_char_segments(emissions, token_ids, transcript_chars, int(blank_id), seconds_per_frame)
    row_segments = segments_from_ctc_char_segments(
        rows,
        char_segments,
        row_units=row_units,
        fps=fps,
        timeline_start_frame=timeline_start_frame,
    )
    return {
        "segments": row_segments,
        "char_segments": char_segments,
        "text": "".join(transcript_chars),
        "diagnostic": {
            "ctc_model": selected_model,
            "ctc_char_count": len(transcript_chars),
            "ctc_frame_count": int(emissions.size(0)),
            "ctc_language": language or "zh",
            "ctc_skipped_char_count": sum(skipped_chars.values()),
            "ctc_skipped_unique_chars": sorted(skipped_chars)[:20],
        },
    }


def ctc_align_text_rows(
    audio_path: Path,
    rows: list[dict[str, Any]],
    model_name: str,
    language: str,
    fps: float | None = None,
    timeline_start_frame: int = 0,
    progress_path: Path | None = None,
) -> dict[str, Any]:
    bundle = load_ctc_model_bundle(model_name, progress_path)
    return ctc_align_text_rows_with_bundle(
        audio_path,
        rows,
        bundle,
        language,
        fps=fps,
        timeline_start_frame=timeline_start_frame,
        progress_path=progress_path,
    )


def align_existing_subtitle_rows(
    audio_path: Path,
    rows: list[dict[str, Any]],
    model_name: str,
    language: str,
    fps: float,
    timeline_start_frame: int,
    max_seconds: float | None = None,
) -> dict[str, Any]:
    try:
        import stable_whisper  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on local setup
        raise RuntimeError("stable-ts 环境未安装，请先运行 setup_asr_env.sh 安装 stable-ts[mlx]") from exc

    seed_segments = rows_to_seed_segments(rows, fps, timeline_start_frame, max_seconds=max_seconds)
    if not seed_segments:
        raise RuntimeError("没有可用于 stable-ts 对齐的字幕文本")

    try:
        model = stable_whisper.load_mlx_whisper(model_name)
    except Exception as exc:  # pragma: no cover - depends on local setup/model cache
        raise RuntimeError(f"stable-ts MLX 模型加载失败: {exc}") from exc

    try:
        result = model.align_words(
            str(audio_path),
            seed_segments,
            language=language or "zh",
            regroup=False,
            normalize_text=True,
            verbose=False,
        )
    except Exception as exc:  # pragma: no cover - depends on local setup/model cache
        raise RuntimeError(f"stable-ts 对齐失败: {exc}") from exc

    return result_to_payload(result)


def aligned_payload_for_ctc_rows(
    raw_payload: dict[str, Any],
    rows: list[dict[str, Any]],
    fps: float,
    timeline_start_frame: int,
) -> dict[str, Any]:
    raw_segments = normalize_aligned_segments_for_rows(raw_payload, rows)
    aligned_rows = apply_alignment_to_rows(
        rows,
        raw_segments,
        fps,
        timeline_start_frame,
        allow_non_monotonic=True,
    )
    for aligned_row, segment in zip(aligned_rows, raw_segments, strict=True):
        if "ctc_confidence" in segment:
            aligned_row["ctc_confidence"] = segment["ctc_confidence"]
        if "ctc_char_count" in segment:
            aligned_row["ctc_char_count"] = segment["ctc_char_count"]
    return {
        "segments": raw_segments,
        "aligned_rows": aligned_rows,
        "text": str(raw_payload.get("text") or "").strip(),
    }


def run_ctc_batch_plan(
    batches: list[dict[str, Any]],
    args: argparse.Namespace,
    progress_path: Path | None,
) -> dict[str, Any]:
    if not batches:
        raise RuntimeError("缺少 CTC batch plan")

    total_batches = len(batches)
    output_batches: list[dict[str, Any]] = []
    diagnostic: dict[str, Any] = {
        "mode": "fixture" if args.fixture_json else "ctc_align_text_batches",
        "requested_mode": args.mode,
        "batch_count": total_batches,
        "requested_ffmpeg": args.ffmpeg,
    }

    fixture_payload = json.loads(Path(args.fixture_json).read_text(encoding="utf-8")) if args.fixture_json else None
    ffmpeg_path = resolve_ffmpeg(args.ffmpeg) if not fixture_payload else None
    bundle = None
    if not fixture_payload:
        bundle = load_ctc_model_bundle(
            args.model,
            progress_path,
            batch_index=0,
            total_batches=total_batches,
        )
        diagnostic["ffmpeg"] = ffmpeg_path

    with tempfile.TemporaryDirectory(prefix="subfix_asr_batches_") as tmp_dir:
        tmp_root = Path(tmp_dir)
        for batch_index, batch in enumerate(batches, start=1):
            batch_id = str(batch.get("batch_id") or batch_index)
            rows = batch["rows"]
            batch_fps = float(batch.get("fps") or args.fps or 30.0)
            timeline_start_frame = int(batch.get("timeline_start_frame") or 0)
            try:
                write_progress(
                    progress_path,
                    "cut_audio" if not fixture_payload else "write_output",
                    f"正在处理第 {batch_index}/{total_batches} 批",
                    batch_index=batch_index,
                    total_batches=total_batches,
                )
                if fixture_payload:
                    speech_regions = fixture_payload.get("speech_regions") or []
                    speech_onsets = fixture_payload.get("speech_onsets") or [
                        region.get("start")
                        for region in speech_regions
                        if isinstance(region, dict)
                    ]
                    raw_payload = dict(fixture_payload)
                else:
                    cut_path = tmp_root / f"batch_{batch_index}.wav"
                    audio_path = Path(str(batch.get("audio") or ""))
                    if not audio_path.exists():
                        raise RuntimeError("audio file not found")
                    cut_audio(
                        str(ffmpeg_path),
                        audio_path,
                        cut_path,
                        float(batch.get("source_start") or 0.0),
                        batch.get("source_end"),
                        int(batch.get("audio_channel_index")) if batch.get("audio_channel_index") else None,
                    )
                    write_progress(
                        progress_path,
                        "detect_speech",
                        "正在检测音频起点",
                        batch_index=batch_index,
                        total_batches=total_batches,
                    )
                    speech_regions, speech_onsets, onset_diagnostic = detect_speech_regions(cut_path)
                    raw_payload = ctc_align_text_rows_with_bundle(
                        cut_path,
                        rows,
                        bundle,
                        args.language or "zh",
                        batch_fps,
                        timeline_start_frame,
                        progress_path,
                        batch_index=batch_index,
                        total_batches=total_batches,
                    )
                    raw_payload = dict(raw_payload)
                    raw_payload.setdefault("diagnostic", {}).update(onset_diagnostic)

                aligned_payload = aligned_payload_for_ctc_rows(
                    raw_payload,
                    rows,
                    batch_fps,
                    timeline_start_frame,
                )
                batch_diagnostic = dict(raw_payload.get("diagnostic") or {})
                batch_diagnostic["row_count"] = len(rows)
                batch_diagnostic["aligned_count"] = len(aligned_payload["aligned_rows"])
                output_batches.append(
                    {
                        "batch_id": batch_id,
                        "ok": True,
                        "aligned_rows": aligned_payload["aligned_rows"],
                        "segments": aligned_payload["segments"],
                        "speech_onsets": speech_onsets,
                        "speech_regions": speech_regions,
                        "text": aligned_payload["text"],
                        "diagnostic": batch_diagnostic,
                    }
                )
            except Exception as exc:
                output_batches.append(
                    {
                        "batch_id": batch_id,
                        "ok": False,
                        "aligned_rows": [],
                        "segments": [],
                        "speech_onsets": [],
                        "speech_regions": [],
                        "error": str(exc),
                        "diagnostic": {"row_count": len(rows)},
                    }
                )

    diagnostic["successful_batch_count"] = sum(1 for batch in output_batches if batch.get("ok") is True)
    diagnostic["failed_batch_count"] = total_batches - diagnostic["successful_batch_count"]
    write_progress(
        progress_path,
        "done",
        "批量 CTC helper 已完成",
        batch_index=total_batches,
        total_batches=total_batches,
    )
    return {
        "ok": diagnostic["failed_batch_count"] == 0,
        "model": args.model,
        "batches": output_batches,
        "diagnostic": diagnostic,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio")
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--language", default="auto")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--source-start", type=float, default=0.0)
    parser.add_argument("--source-end", type=float)
    parser.add_argument("--audio-channel-index", type=int)
    parser.add_argument("--rows-json")
    parser.add_argument("--batch-plan-json")
    parser.add_argument("--windows-json")
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--timeline-start-frame", type=int, default=0)
    parser.add_argument("--srt-output")
    parser.add_argument("--srt-base-frame", type=int)
    parser.add_argument("--backend", choices=("auto", "mimo_asr", "qwen3_asr", "mlx_whisper", "openai_whisper"), default="auto")
    parser.add_argument(
        "--mode",
        choices=(
            "align",
            "align_text",
            "whisperx_align_text",
            "ctc_align_text",
            "ctc_align_text_batches",
            "transcribe",
            "generate_subtitles",
            "onsets",
        ),
        default="align",
    )
    parser.add_argument("--fixture-json")
    parser.add_argument("--progress-json")
    args = parser.parse_args(argv)

    output_path = Path(args.output)
    progress_path = Path(args.progress_json) if args.progress_json else None
    try:
        diagnostic: dict[str, Any] = {
            "requested_ffmpeg": args.ffmpeg,
            "requested_mode": args.mode,
            "requested_backend": args.backend,
            "source_start": args.source_start,
            "source_end": args.source_end,
        }
        transcribe_windows = load_transcribe_windows(args.windows_json)
        if args.mode == "ctc_align_text_batches":
            payload = run_ctc_batch_plan(load_batch_plan(args.batch_plan_json), args, progress_path)
            write_payload(output_path, payload)
            return 0 if payload.get("ok") else 1

        if args.fixture_json:
            write_progress(progress_path, "write_output", "正在写入 fixture 对齐结果")
            raw_payload = json.loads(Path(args.fixture_json).read_text(encoding="utf-8"))
            audio_used = str(args.fixture_json)
            diagnostic["mode"] = "fixture"
            if args.mode == "transcribe" and transcribe_windows:
                fixture_segments = normalize_segments(raw_payload)
                raw_payload = {
                    "backend": "fixture",
                    "model": args.model,
                    "segments": [],
                    "text": "",
                    "windows": [
                        {
                            "ok": True,
                            "window_id": str(window.get("window_id") or index),
                            "review_type": window.get("review_type") or "",
                            "row_label": window.get("row_label") or "",
                            "source_start": window.get("source_start"),
                            "source_end": window.get("source_end"),
                            "backend": "fixture",
                            "model": args.model,
                            "segments": fixture_segments,
                            "speech_onsets": raw_payload.get("speech_onsets") or [],
                            "speech_regions": raw_payload.get("speech_regions") or [],
                            "text": str(raw_payload.get("text") or "").strip(),
                            "diagnostic": {"mode": "fixture_window"},
                        }
                        for index, window in enumerate(transcribe_windows, start=1)
                    ],
                }
            rows = load_rows(args.rows_json)
            if args.mode in {"align", "align_text", "whisperx_align_text", "ctc_align_text"} and rows:
                if args.mode == "ctc_align_text" and raw_payload.get("char_segments"):
                    raw_segments = segments_from_ctc_char_segments(rows, raw_payload.get("char_segments") or [])
                else:
                    raw_segments = normalize_aligned_segments_for_rows(raw_payload, rows)
                raw_payload = dict(raw_payload)
                raw_payload["segments"] = raw_segments
                raw_payload["aligned_rows"] = apply_alignment_to_rows(
                    rows,
                    raw_segments,
                    args.fps,
                    args.timeline_start_frame,
                    allow_non_monotonic=args.mode in {"align_text", "whisperx_align_text", "ctc_align_text"},
                )
                if args.mode == "ctc_align_text":
                    for aligned_row, segment in zip(raw_payload["aligned_rows"], raw_segments, strict=True):
                        if "ctc_confidence" in segment:
                            aligned_row["ctc_confidence"] = segment["ctc_confidence"]
                        if "ctc_char_count" in segment:
                            aligned_row["ctc_char_count"] = segment["ctc_char_count"]
                diagnostic["row_count"] = len(rows)
                diagnostic["aligned_count"] = len(raw_payload["aligned_rows"])
            speech_regions = raw_payload.get("speech_regions") or []
            speech_onsets = raw_payload.get("speech_onsets") or [region.get("start") for region in speech_regions if isinstance(region, dict)]
            if args.mode == "generate_subtitles":
                segments = normalize_segments(raw_payload)
                raw_payload = dict(raw_payload)
                raw_payload["segments"] = segments
                raw_payload["subtitle_rows"] = generate_subtitle_rows_from_segments(
                    segments,
                    args.fps,
                    args.timeline_start_frame,
                )
                diagnostic["generated_subtitle_count"] = len(raw_payload["subtitle_rows"])
            diagnostic["backend"] = "fixture"
        else:
            if not args.audio or not Path(args.audio).exists():
                raise RuntimeError("audio file not found")
            rows = load_rows(args.rows_json)
            if args.mode == "transcribe" and transcribe_windows:
                raw_payload = run_transcribe_windows(Path(args.audio), transcribe_windows, args, progress_path)
                diagnostic["mode"] = args.mode
                diagnostic["ffmpeg"] = raw_payload.get("diagnostic", {}).get("ffmpeg")
                diagnostic["backend"] = raw_payload.get("backend")
                diagnostic["model"] = raw_payload.get("model") or args.model
                if raw_payload.get("diagnostic", {}).get("fallback_errors"):
                    diagnostic["fallback_errors"] = raw_payload.get("diagnostic", {}).get("fallback_errors")
                audio_used = str(args.audio)
                speech_regions = []
                speech_onsets = []
                segments = normalize_segments(raw_payload)
                diagnostic["raw_segment_count"] = len(raw_payload.get("segments") or [])
                diagnostic["segment_count"] = len(segments)
                diagnostic["text_length"] = len(str(raw_payload.get("text") or "").strip())
                write_progress(progress_path, "write_output", "正在写入批量局部转写结果")
                write_payload(
                    output_path,
                    {
                        "ok": True,
                        "model": str(raw_payload.get("model") or args.model),
                        "backend": raw_payload.get("backend") or diagnostic.get("backend"),
                        "audio": audio_used,
                        "segments": segments,
                        "aligned_rows": raw_payload.get("aligned_rows") or [],
                        "speech_onsets": speech_onsets,
                        "speech_regions": speech_regions,
                        "text": str(raw_payload.get("text") or "").strip(),
                        "windows": raw_payload.get("windows") or [],
                        "diagnostic": diagnostic,
                    },
                )
                write_progress(progress_path, "done", "批量局部转写 helper 已完成")
                return 0
            with tempfile.TemporaryDirectory(prefix="subfix_asr_") as tmp_dir:
                cut_path = Path(tmp_dir) / "audio.wav"
                ffmpeg_path = resolve_ffmpeg(args.ffmpeg)
                diagnostic["mode"] = args.mode
                diagnostic["ffmpeg"] = ffmpeg_path
                write_progress(progress_path, "cut_audio", "正在截取音频片段")
                cut_audio(ffmpeg_path, Path(args.audio), cut_path, args.source_start, args.source_end, args.audio_channel_index)
                diagnostic["cut_audio_bytes"] = cut_path.stat().st_size if cut_path.exists() else 0
                write_progress(progress_path, "detect_speech", "正在检测音频起点")
                speech_regions, speech_onsets, onset_diagnostic = detect_speech_regions(cut_path)
                diagnostic.update(onset_diagnostic)
                if args.mode == "align":
                    raw_payload = align_existing_subtitle_rows(
                        cut_path,
                        rows,
                        args.model,
                        args.language or "zh",
                        args.fps,
                        args.timeline_start_frame,
                        (args.source_end - args.source_start) if args.source_end is not None and args.source_end > args.source_start else None,
                    )
                    raw_segments = normalize_aligned_segments_for_rows(raw_payload, rows)
                    aligned_rows = apply_alignment_to_rows(rows, raw_segments, args.fps, args.timeline_start_frame)
                    raw_payload = dict(raw_payload)
                    raw_payload["segments"] = raw_segments
                    raw_payload["aligned_rows"] = aligned_rows
                    diagnostic["row_count"] = len(rows)
                    diagnostic["aligned_count"] = len(aligned_rows)
                elif args.mode == "align_text":
                    raw_payload = align_text_rows(
                        cut_path,
                        rows,
                        args.model,
                        args.language or "zh",
                    )
                    raw_segments = normalize_aligned_segments_for_rows(raw_payload, rows)
                    aligned_rows = apply_alignment_to_rows(
                        rows,
                        raw_segments,
                        args.fps,
                        args.timeline_start_frame,
                        allow_non_monotonic=True,
                    )
                    raw_payload = dict(raw_payload)
                    raw_payload["segments"] = raw_segments
                    raw_payload["aligned_rows"] = aligned_rows
                    diagnostic["row_count"] = len(rows)
                    diagnostic["aligned_count"] = len(aligned_rows)
                elif args.mode == "whisperx_align_text":
                    raw_payload = whisperx_align_text_rows(
                        cut_path,
                        rows,
                        args.language or "zh",
                    )
                    raw_segments = normalize_aligned_segments_for_rows(raw_payload, rows)
                    aligned_rows = apply_alignment_to_rows(
                        rows,
                        raw_segments,
                        args.fps,
                        args.timeline_start_frame,
                        allow_non_monotonic=True,
                    )
                    raw_payload = dict(raw_payload)
                    raw_payload["segments"] = raw_segments
                    raw_payload["aligned_rows"] = aligned_rows
                    diagnostic["row_count"] = len(rows)
                    diagnostic["aligned_count"] = len(aligned_rows)
                elif args.mode == "ctc_align_text":
                    raw_payload = ctc_align_text_rows(
                        cut_path,
                        rows,
                        args.model,
                        args.language or "zh",
                        args.fps,
                        args.timeline_start_frame,
                        progress_path,
                    )
                    raw_segments = normalize_aligned_segments_for_rows(raw_payload, rows)
                    aligned_rows = apply_alignment_to_rows(
                        rows,
                        raw_segments,
                        args.fps,
                        args.timeline_start_frame,
                        allow_non_monotonic=True,
                    )
                    for aligned_row, segment in zip(aligned_rows, raw_segments, strict=True):
                        if "ctc_confidence" in segment:
                            aligned_row["ctc_confidence"] = segment["ctc_confidence"]
                        if "ctc_char_count" in segment:
                            aligned_row["ctc_char_count"] = segment["ctc_char_count"]
                    raw_payload = dict(raw_payload)
                    raw_payload["segments"] = raw_segments
                    raw_payload["aligned_rows"] = aligned_rows
                    diagnostic["row_count"] = len(rows)
                    diagnostic["aligned_count"] = len(aligned_rows)
                elif args.mode == "onsets":
                    raw_payload = {"segments": [], "aligned_rows": [], "text": ""}
                else:
                    raw_payload = transcribe_with_backend(cut_path, args.model, args.language, args.backend)
                    diagnostic["backend"] = raw_payload.get("backend")
                    diagnostic["model"] = raw_payload.get("model") or args.model
                    if raw_payload.get("fallback_errors"):
                        diagnostic["fallback_errors"] = raw_payload.get("fallback_errors")
                    if args.mode == "generate_subtitles":
                        segments = normalize_segments(raw_payload)
                        raw_payload = dict(raw_payload)
                        raw_payload["segments"] = segments
                        raw_payload["subtitle_rows"] = generate_subtitle_rows_from_segments(
                            segments,
                            args.fps,
                            args.timeline_start_frame,
                        )
                        diagnostic["generated_subtitle_count"] = len(raw_payload["subtitle_rows"])
                audio_used = str(cut_path)

        segments = normalize_segments(raw_payload)
        diagnostic["raw_segment_count"] = len(raw_payload.get("segments") or [])
        diagnostic["segment_count"] = len(segments)
        diagnostic["text_length"] = len(str(raw_payload.get("text") or "").strip())
        if args.srt_output and args.mode == "generate_subtitles":
            srt_path = Path(args.srt_output)
            srt_base_frame = args.srt_base_frame if args.srt_base_frame is not None else args.timeline_start_frame
            diagnostic["srt_output"] = str(srt_path)
            diagnostic["srt_row_count"] = write_subtitle_rows_to_srt(
                srt_path,
                raw_payload.get("subtitle_rows") or [],
                args.fps,
                srt_base_frame,
            )
        write_progress(progress_path, "write_output", "正在写入对齐结果")
        write_payload(
            output_path,
            {
                "ok": True,
                "model": str(raw_payload.get("model") or args.model),
                "backend": raw_payload.get("backend") or diagnostic.get("backend"),
                "audio": audio_used,
                "segments": segments,
                "aligned_rows": raw_payload.get("aligned_rows") or [],
                "subtitle_rows": raw_payload.get("subtitle_rows") or [],
                "speech_onsets": speech_onsets,
                "speech_regions": speech_regions,
                "text": str(raw_payload.get("text") or "").strip(),
                "windows": raw_payload.get("windows") or [],
                "diagnostic": diagnostic,
            },
        )
        write_progress(progress_path, "done", "对齐 helper 已完成")
        return 0
    except Exception as exc:
        write_progress(progress_path, "failed", str(exc))
        write_payload(output_path, {"ok": False, "error": str(exc), "segments": []})
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
