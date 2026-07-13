#!/usr/bin/env python3
"""SubFix v4 canonical aligned-unit generation primitives."""

from __future__ import annotations

import difflib
import hashlib
import itertools
import json
import math
import re
import wave
from array import array
from collections import Counter
from pathlib import Path
from typing import Any, Callable


PROFILE_SCHEMA = "subfix_segmentation_profile_v3"
MODEL_BUCKETS = 512
SHORT_INTERJECTIONS = {"嗯", "啊", "哇", "哦", "噢", "好", "对", "哎", "呃", "诶", "行", "是", "哎呦", "哎哟", "好的", "ok"}
PROTECTED_BIGRAMS = {
    "非常", "力气", "姿态", "控制", "跳舞", "这个", "那个", "什么", "一个", "没有", "可以", "因为", "所以",
    "但是", "而且", "已经", "我们", "你们", "他们", "东西", "功能", "机器", "价格", "块钱", "产品", "告诉",
    "成功", "感觉", "科技", "评论", "转发", "帮助", "环绕", "运镜", "自动", "逻辑", "神奇",
}
CLAUSE_STARTERS = (
    "也欢迎", "我们会", "但我", "但是", "而且", "其实", "所以", "不过", "然后", "另外", "同时", "因此",
    "你看", "开启", "自动", "除了",
)
INCOMPLETE_CLAUSE_SUFFIXES = ("我们", "你们", "他们", "我", "你", "他", "会", "要", "能", "的", "把", "让", "给", "跟")
NON_BREAK_RIGHT_PREFIXES = ("不了", "得了")


class V4AlignmentError(RuntimeError):
    pass


def normalize_text(text: Any) -> str:
    return re.sub(r"[\s\-_—–，。！？、；：,.!?;:\"'“”‘’（）()《》【】\[\]{}<>…·/\\|]+", "", str(text or "").lower())


def _read_mono_pcm16(path: Path) -> tuple[int, array]:
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise ValueError(f"v4 轨道片段必须是 mono PCM16 WAV: {path.name}")
        rate = handle.getframerate()
        samples = array("h")
        samples.frombytes(handle.readframes(handle.getnframes()))
    if samples.itemsize != 2:
        raise RuntimeError("当前平台不支持 PCM16 array")
    return rate, samples


def _write_mono_pcm16(path: Path, sample_rate: int, samples: array) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(samples.tobytes())


def compose_track_audio(
    items: list[dict[str, Any]],
    output_path: Path,
    sample_rate: int = 16000,
) -> dict[str, Any]:
    valid: list[tuple[dict[str, Any], Path, int]] = []
    for raw_item in items or []:
        path = Path(raw_item.get("cut_path") or "")
        if not path.is_file():
            continue
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
                raise ValueError(f"v4 轨道片段必须是 mono PCM16 WAV: {path.name}")
            if rate != sample_rate:
                raise ValueError(f"v4 轨道片段采样率必须为 {sample_rate}: {path.name}={rate}")
            sample_count = handle.getnframes()
        valid.append((dict(raw_item), path, sample_count))
    if not valid:
        raise ValueError("v4 没有可拼接的轨道音频")

    timeline_start = min(int(item.get("timeline_start_frame") or 0) for item, _path, _count in valid)
    fps_values = [float(item.get("fps") or item.get("batch_fps") or 30.0) for item, _path, _count in valid]
    fps = fps_values[0]
    if any(abs(value - fps) > 1e-4 for value in fps_values[1:]):
        raise ValueError("v4 同一轨道的时间线 FPS 不一致")
    for item, path, sample_count in valid:
        if item.get("timeline_end_frame") is None:
            continue
        expected_frames = int(item["timeline_end_frame"]) - int(item.get("timeline_start_frame") or 0)
        actual_frames = int(round(sample_count / sample_rate * fps))
        tolerance = max(2, int(math.ceil(max(1, expected_frames) * 0.01)))
        if expected_frames <= 0 or abs(actual_frames - expected_frames) > tolerance:
            raise ValueError(f"v4 音频时长与时间线跨度不一致，暂不支持重定时片段: {path.name}")
    timeline_end = max(
        int(item["timeline_end_frame"])
        if item.get("timeline_end_frame") is not None
        else int(item.get("timeline_start_frame") or 0) + int(math.ceil(sample_count / sample_rate * fps))
        for item, _path, sample_count in valid
    )
    total_samples = max(1, int(math.ceil((timeline_end - timeline_start) / fps * sample_rate)))
    mixed = array("h", [0]) * total_samples
    overlap_sample_count = 0
    for item, path, _sample_count in valid:
        _rate, samples = _read_mono_pcm16(path)
        offset = int(round((int(item.get("timeline_start_frame") or 0) - timeline_start) / fps * sample_rate))
        for index, value in enumerate(samples):
            target = offset + index
            if target < 0 or target >= total_samples:
                continue
            if mixed[target] != 0:
                overlap_sample_count += 1
            mixed[target] = max(-32768, min(32767, mixed[target] + int(value)))
    _write_mono_pcm16(Path(output_path), sample_rate, mixed)
    return {
        "timeline_start_frame": timeline_start,
        "timeline_end_frame": timeline_end,
        "sample_rate": sample_rate,
        "item_count": len(valid),
        "overlap_sample_count": overlap_sample_count,
        "output_sample_count": len(mixed),
    }


def build_context_windows(
    start_frame: int,
    end_frame: int,
    fps: float,
    window_seconds: float = 45.0,
    stride_seconds: float = 42.0,
) -> list[dict[str, int]]:
    if end_frame <= start_frame:
        return []
    window_frames = max(1, int(round(window_seconds * fps)))
    stride_frames = max(1, int(round(stride_seconds * fps)))
    overlap_frames = max(0, window_frames - stride_frames)
    windows: list[dict[str, int]] = []
    cursor = int(start_frame)
    index = 1
    while cursor < end_frame:
        window_end = min(int(end_frame), cursor + window_frames)
        windows.append(
            {
                "window_index": index,
                "start_frame": cursor,
                "end_frame": window_end,
                "left_context_frames": 0 if index == 1 else overlap_frames,
                "right_context_frames": 0 if window_end >= end_frame else overlap_frames,
            }
        )
        if window_end >= end_frame:
            break
        cursor += stride_frames
        index += 1
    return windows


def write_context_window_audio(
    track_audio_path: Path,
    output_path: Path,
    track_start_frame: int,
    window_start_frame: int,
    window_end_frame: int,
    fps: float,
) -> dict[str, Any]:
    with wave.open(str(track_audio_path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise ValueError(f"v4 连续轨道必须是 mono PCM16 WAV: {Path(track_audio_path).name}")
        sample_rate = handle.getframerate()
        total_samples = handle.getnframes()
        start_sample = max(0, int(round((window_start_frame - track_start_frame) / fps * sample_rate)))
        end_sample = max(start_sample + 1, int(round((window_end_frame - track_start_frame) / fps * sample_rate)))
        handle.setpos(min(start_sample, total_samples))
        window_samples = array("h")
        window_samples.frombytes(handle.readframes(max(0, min(end_sample, total_samples) - start_sample)))
    expected_count = end_sample - start_sample
    if len(window_samples) < expected_count:
        window_samples.extend([0] * (expected_count - len(window_samples)))
    _write_mono_pcm16(Path(output_path), sample_rate, window_samples)
    return {
        "timeline_start_frame": int(window_start_frame),
        "timeline_end_frame": int(window_end_frame),
        "sample_rate": sample_rate,
        "output_sample_count": len(window_samples),
    }


def prepare_track_context_windows(
    work_items: list[dict[str, Any]],
    output_dir: Path,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for item in work_items or []:
        grouped.setdefault(int(item.get("track_index") or 0), []).append(dict(item))
    prepared: list[dict[str, Any]] = []
    context_window_count = 0
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for track_index in sorted(grouped):
        items = sorted(grouped[track_index], key=lambda row: int(row.get("timeline_start_frame") or 0))
        track_path = output_dir / f"track_{track_index:03d}.wav"
        composite = compose_track_audio(items, track_path)
        fps = float(items[0].get("fps") or items[0].get("batch_fps") or 30.0)
        windows = build_context_windows(
            int(composite["timeline_start_frame"]),
            int(composite["timeline_end_frame"]),
            fps,
        )
        prepared_windows: list[dict[str, Any]] = []
        for window in windows:
            window_path = output_dir / f"track_{track_index:03d}_window_{int(window['window_index']):04d}.wav"
            write_context_window_audio(
                track_path,
                window_path,
                int(composite["timeline_start_frame"]),
                int(window["start_frame"]),
                int(window["end_frame"]),
                fps,
            )
            prepared_windows.append({**window, "audio_path": str(window_path)})
        context_window_count += len(prepared_windows)
        prepared.append(
            {
                "track_index": track_index,
                "fps": fps,
                "timeline_start_frame": int(composite["timeline_start_frame"]),
                "timeline_end_frame": int(composite["timeline_end_frame"]),
                "item_count": int(composite["item_count"]),
                "audio_path": str(track_path),
                "windows": prepared_windows,
            }
        )
    return prepared, {
        "track_composite_count": len(prepared),
        "context_window_count": context_window_count,
    }


def _rms(samples: array) -> float:
    if not samples:
        return 0.0
    return math.sqrt(sum(float(value) * float(value) for value in samples) / len(samples))


def score_aligned_units_from_audio(
    audio_path: Path,
    units: list[dict[str, Any]],
    window_start_frame: int,
    fps: float,
    alignment_coverage: float,
) -> list[dict[str, Any]]:
    sample_rate, samples = _read_mono_pcm16(Path(audio_path))
    rms_window = max(1, int(round(sample_rate * 0.01)))
    window_levels = sorted(
        _rms(array("h", samples[index:index + rms_window]))
        for index in range(0, len(samples), rms_window)
        if samples[index:index + rms_window]
    )
    noise_floor = window_levels[min(len(window_levels) - 1, int(len(window_levels) * 0.20))] if window_levels else 0.0
    noise_floor = max(1.0, noise_floor)
    scored: list[dict[str, Any]] = []
    for raw_unit in units or []:
        unit = dict(raw_unit)
        start_frame = int(unit.get("start_frame") or window_start_frame)
        end_frame = max(start_frame + 1, int(unit.get("end_frame") or start_frame + 1))
        start_sample = max(0, int(round((start_frame - window_start_frame) / fps * sample_rate)))
        end_sample = min(len(samples), max(start_sample + 1, int(round((end_frame - window_start_frame) / fps * sample_rate))))
        unit_level = _rms(array("h", samples[start_sample:end_sample]))
        snr_db = max(-20.0, min(40.0, 20.0 * math.log10(max(1.0, unit_level) / noise_floor)))
        unit["speaker_score_db"] = round(snr_db, 3)
        unit["source_score"] = round(float(alignment_coverage) * 6.0 + max(0.0, snr_db) * 0.5, 3)
        scored.append(unit)
    return scored


def annotate_independent_vad(
    units: list[dict[str, Any]],
    speech_regions: list[dict[str, Any]],
    timeline_start_frame: int,
    fps: float,
) -> list[dict[str, Any]]:
    output = [dict(unit) for unit in units or []]
    minimum_frames = max(1, int(math.ceil(0.18 * fps)))
    for region in speech_regions or []:
        region_start = int(round(timeline_start_frame + float(region.get("start") or 0.0) * fps))
        region_end = int(round(timeline_start_frame + float(region.get("end") or 0.0) * fps))
        region_indices = [
            index
            for index, unit in enumerate(output)
            if int(unit.get("end_frame") or 0) > region_start and int(unit.get("start_frame") or 0) < region_end
        ]
        region_text = "".join(normalize_text(output[index].get("text")) for index in region_indices)
        if 0 < len(region_text) <= 2 and region_end - region_start >= minimum_frames:
            for index in region_indices:
                output[index]["independent_vad"] = True
    return output


def _interval_overlap_ratio(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_start = int(left.get("start_frame") or 0)
    left_end = max(left_start + 1, int(left.get("end_frame") or left_start + 1))
    right_start = int(right.get("start_frame") or 0)
    right_end = max(right_start + 1, int(right.get("end_frame") or right_start + 1))
    overlap = max(0, min(left_end, right_end) - max(left_start, right_start))
    return overlap / max(1, min(left_end - left_start, right_end - right_start))


def dedupe_overlap_units(units: list[dict[str, Any]], nearby_frames: int = 2) -> tuple[list[dict[str, Any]], dict[str, int]]:
    accepted: list[dict[str, Any]] = []
    active_indices: list[int] = []
    suppressed = 0
    for raw_unit in sorted(units or [], key=lambda row: (int(row.get("start_frame") or 0), int(row.get("end_frame") or 0))):
        unit = dict(raw_unit)
        text = normalize_text(unit.get("text"))
        duplicate_index = None
        unit_start = int(unit.get("start_frame") or 0)
        active_indices = [
            index
            for index in active_indices
            if int(accepted[index].get("end_frame") or 0) >= unit_start - nearby_frames
        ]
        for index in active_indices:
            existing = accepted[index]
            if normalize_text(existing.get("text")) != text or not text:
                continue
            starts_near = abs(int(existing.get("start_frame") or 0) - int(unit.get("start_frame") or 0)) <= nearby_frames
            existing_window = int(existing.get("window_index") or 0)
            unit_window = int(unit.get("window_index") or 0)
            cross_window_repeat = bool(existing_window and unit_window and existing_window != unit_window and starts_near)
            if _interval_overlap_ratio(existing, unit) >= 0.35 or cross_window_repeat:
                duplicate_index = index
                break
        if duplicate_index is None:
            accepted.append(unit)
            active_indices.append(len(accepted) - 1)
            continue
        suppressed += 1
        existing = accepted[duplicate_index]
        existing_quality = (float(existing.get("source_score") or 0.0), -int(existing.get("window_index") or 0))
        unit_quality = (float(unit.get("source_score") or 0.0), -int(unit.get("window_index") or 0))
        if unit_quality > existing_quality:
            accepted[duplicate_index] = unit
    accepted.sort(key=lambda row: (int(row.get("start_frame") or 0), int(row.get("end_frame") or 0)))
    return accepted, {"overlap_unit_suppressed_count": suppressed}


def stitch_track_window_units(
    units: list[dict[str, Any]],
    windows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    ordered_windows = sorted(
        [dict(window) for window in windows or []],
        key=lambda row: (int(row.get("start_frame") or 0), int(row.get("end_frame") or 0)),
    )
    units_by_window: dict[int, list[dict[str, Any]]] = {}
    for unit in units or []:
        units_by_window.setdefault(int(unit.get("window_index") or 0), []).append(dict(unit))
    for rows in units_by_window.values():
        rows.sort(key=lambda row: (int(row.get("start_frame") or 0), int(row.get("end_frame") or 0)))

    limits: dict[int, list[int]] = {
        window_index: [0, len(rows)]
        for window_index, rows in units_by_window.items()
    }
    for previous, current in zip(ordered_windows, ordered_windows[1:]):
        previous_end = int(previous.get("end_frame") or 0)
        current_start = int(current.get("start_frame") or 0)
        if previous_end <= current_start:
            continue
        previous_index = int(previous.get("window_index") or 0)
        current_index = int(current.get("window_index") or 0)
        margin = max(6, int(round((previous_end - current_start) * 0.15)))
        region_start = current_start - margin
        region_end = previous_end + margin

        def overlap_rows(window_index: int) -> list[tuple[int, dict[str, Any]]]:
            return [
                (row_index, row)
                for row_index, row in enumerate(units_by_window.get(window_index, []))
                if region_start <= (int(row.get("start_frame") or 0) + int(row.get("end_frame") or 0)) / 2.0 <= region_end
                and normalize_text(row.get("text"))
            ]

        left_pairs = overlap_rows(previous_index)
        right_pairs = overlap_rows(current_index)
        left_char_rows = [
            row_index
            for row_index, row in left_pairs
            for _char in normalize_text(row.get("text"))
        ]
        right_char_rows = [
            row_index
            for row_index, row in right_pairs
            for _char in normalize_text(row.get("text"))
        ]
        left_text = "".join(
            normalize_text(row.get("text"))
            for _row_index, row in left_pairs
        )
        right_text = "".join(
            normalize_text(row.get("text"))
            for _row_index, row in right_pairs
        )
        if not left_text or not right_text:
            continue
        matcher = difflib.SequenceMatcher(None, left_text, right_text, autojunk=False)
        blocks = [block for block in matcher.get_matching_blocks() if block.size >= 2]
        if not blocks:
            continue
        nominal_midpoint = (previous_end + current_start) / 2.0

        def block_key(block: difflib.Match) -> tuple[int, float]:
            left_row = units_by_window[previous_index][left_char_rows[block.a + block.size // 2]]
            right_row = units_by_window[current_index][right_char_rows[block.b + block.size // 2]]
            center = (
                int(left_row.get("start_frame") or 0)
                + int(left_row.get("end_frame") or 0)
                + int(right_row.get("start_frame") or 0)
                + int(right_row.get("end_frame") or 0)
            ) / 4.0
            return block.size, -abs(center - nominal_midpoint)

        block = max(blocks, key=block_key)
        split_offset = max(1, block.size // 2)
        previous_cut = left_char_rows[block.a + split_offset - 1] + 1
        current_cut = right_char_rows[block.b + split_offset]
        limits.setdefault(previous_index, [0, len(units_by_window.get(previous_index, []))])[1] = min(
            limits[previous_index][1],
            previous_cut,
        )
        limits.setdefault(current_index, [0, len(units_by_window.get(current_index, []))])[0] = max(
            limits[current_index][0],
            current_cut,
        )

    accepted: list[dict[str, Any]] = []
    suppressed = 0
    known_window_indices = set(units_by_window)
    for window_index, rows in units_by_window.items():
        lower, upper = limits.get(window_index, [0, len(rows)])
        accepted.extend(dict(row) for row in rows[lower:upper])
        suppressed += lower + max(0, len(rows) - upper)
    accepted.extend(
        dict(unit)
        for unit in units or []
        if int(unit.get("window_index") or 0) not in known_window_indices
    )
    accepted.sort(key=lambda row: (int(row.get("start_frame") or 0), int(row.get("end_frame") or 0)))
    return accepted, {"window_seam_suppressed_count": suppressed}


def _timestamp_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for segment in payload.get("segments") or []:
        for word in segment.get("words") or []:
            text = str(word.get("word") or word.get("text") or "")
            if text and word.get("start") is not None and word.get("end") is not None:
                items.append({"text": text, "start": float(word["start"]), "end": float(word["end"])})
    return items


def _expand_timestamp_items(
    items: list[dict[str, Any]],
    fps: float,
    timeline_start_frame: int,
) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for item in items:
        normalized = normalize_text(item.get("text"))
        if not normalized:
            continue
        start = float(item.get("start") or item.get("start_time") or 0.0)
        end = float(item.get("end") or item.get("end_time") or start)
        if end <= start:
            end = start + 0.001
        duration = (end - start) / len(normalized)
        for index, char in enumerate(normalized):
            unit_start = start + duration * index
            unit_end = start + duration * (index + 1)
            start_frame = timeline_start_frame + int(math.floor(unit_start * fps + 0.5))
            end_frame = timeline_start_frame + int(math.floor(unit_end * fps + 0.5))
            units.append({"text": char, "start_frame": start_frame, "end_frame": max(start_frame + 1, end_frame)})
    return units


def annotate_asr_punctuation(units: list[dict[str, Any]], raw_text: str) -> list[dict[str, Any]]:
    output = [dict(unit) for unit in units or []]
    expected_text = normalize_text(raw_text)
    actual_text = "".join(normalize_text(unit.get("text")) for unit in output)
    if not expected_text or not actual_text:
        return output
    punctuation_positions: dict[int, float] = {}
    normalized_count = 0
    for char in str(raw_text or ""):
        normalized_char = normalize_text(char)
        if normalized_char:
            normalized_count += len(normalized_char)
        elif normalized_count > 0:
            if char in "。！？!?；;":
                punctuation_positions[normalized_count - 1] = 1.0
            elif char in "，、,：:":
                punctuation_positions[normalized_count - 1] = max(
                    punctuation_positions.get(normalized_count - 1, 0.0),
                    0.55,
                )
    matcher = difflib.SequenceMatcher(None, expected_text, actual_text, autojunk=False)
    mapped: dict[int, int] = {}
    for expected_start, actual_start, size in matcher.get_matching_blocks():
        for offset in range(size):
            mapped[expected_start + offset] = actual_start + offset
    for expected_index, strength in punctuation_positions.items():
        actual_index = mapped.get(expected_index)
        if actual_index is not None and 0 <= actual_index < len(output):
            output[actual_index]["asr_punctuation_strength"] = strength
    return output


def _allocate_repaired_units(
    text: str,
    start_frame: int,
    end_frame: int,
    template: dict[str, Any],
) -> list[dict[str, Any]]:
    if not text:
        return []
    span = max(len(text), int(end_frame) - int(start_frame))
    output: list[dict[str, Any]] = []
    for index, char in enumerate(text):
        unit = dict(template)
        unit_start = int(start_frame) + int(math.floor(span * index / len(text)))
        unit_end = int(start_frame) + int(math.floor(span * (index + 1) / len(text)))
        unit["text"] = char
        unit["start_frame"] = unit_start
        unit["end_frame"] = max(unit_start + 1, unit_end)
        unit["alignment_repaired"] = True
        output.append(unit)
    return output


def repair_aligned_unit_text(
    expected_text: str,
    units: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    expected = normalize_text(expected_text)
    working = [dict(unit) for unit in units or []]
    actual = "".join(normalize_text(unit.get("text")) for unit in working)
    if not expected or expected == actual:
        return working, 0

    matcher = difflib.SequenceMatcher(None, expected, actual, autojunk=False)
    output: list[dict[str, Any]] = []
    repaired = 0
    for tag, expected_start, expected_end, actual_start, actual_end in matcher.get_opcodes():
        expected_span = expected[expected_start:expected_end]
        actual_span = working[actual_start:actual_end]
        if tag == "equal":
            output.extend(dict(unit) for unit in actual_span)
            continue
        if tag == "insert":
            repaired += len(actual_span)
            continue
        if tag == "replace" and len(expected_span) == len(actual_span):
            for char, raw_unit in zip(expected_span, actual_span):
                unit = dict(raw_unit)
                if normalize_text(unit.get("text")) != char:
                    repaired += 1
                    unit["alignment_repaired"] = True
                unit["text"] = char
                output.append(unit)
            continue

        repaired += max(len(expected_span), len(actual_span))
        if actual_span:
            template = actual_span[0]
            span_start = int(actual_span[0].get("start_frame") or 0)
            span_end = int(actual_span[-1].get("end_frame") or span_start + len(expected_span))
            output.extend(_allocate_repaired_units(expected_span, span_start, span_end, template))
            continue

        previous = output[-1] if output else None
        following = working[actual_start] if actual_start < len(working) else None
        template = following or previous or {}
        if previous and following:
            local_text = normalize_text(previous.get("text")) + expected_span + normalize_text(following.get("text"))
            local_start = int(previous.get("start_frame") or 0)
            local_end = int(following.get("end_frame") or local_start + len(local_text))
            allocated = _allocate_repaired_units(local_text, local_start, local_end, template)
            output[-1]["start_frame"] = allocated[0]["start_frame"]
            output[-1]["end_frame"] = allocated[0]["end_frame"]
            working[actual_start]["start_frame"] = allocated[-1]["start_frame"]
            working[actual_start]["end_frame"] = allocated[-1]["end_frame"]
            output.extend(allocated[1:-1])
        elif following:
            following_start = int(following.get("start_frame") or 0)
            output.extend(
                _allocate_repaired_units(
                    expected_span,
                    following_start - len(expected_span),
                    following_start,
                    template,
                )
            )
        elif previous:
            previous_end = int(previous.get("end_frame") or 0)
            output.extend(
                _allocate_repaired_units(
                    expected_span,
                    previous_end,
                    previous_end + len(expected_span),
                    template,
                )
            )
    return output, repaired


def require_aligned_units(
    audio_path: Path,
    payload: dict[str, Any],
    align_fn: Callable[[Path, str, str], list[dict[str, Any]]],
    language: str,
    fps: float,
    timeline_start_frame: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    items = _timestamp_items(payload)
    retry_count = 0
    if not items:
        text = str(payload.get("text") or "").strip()
        if not normalize_text(text):
            return [], {
                "forced_align_retry_count": 0,
                "aligned_unit_count": 0,
                "alignment_coverage": 1.0,
                "empty_silence_window": True,
            }
        if text:
            retry_count = 1
            try:
                items = list(align_fn(Path(audio_path), text, language) or [])
            except Exception as exc:
                raise V4AlignmentError("v4 Forced Aligner 执行失败，已终止写回") from exc
    units = _expand_timestamp_items(items, fps, timeline_start_frame)
    expected = normalize_text(payload.get("text"))
    actual = "".join(unit["text"] for unit in units)
    raw_coverage = difflib.SequenceMatcher(None, expected, actual, autojunk=False).ratio() if expected else 1.0
    if not units or raw_coverage < 0.90:
        raise V4AlignmentError("v4 无法获得可靠字词时间戳，已终止写回")
    units, repaired_count = repair_aligned_unit_text(expected, units)
    repaired_text = "".join(normalize_text(unit.get("text")) for unit in units)
    coverage = difflib.SequenceMatcher(None, expected, repaired_text, autojunk=False).ratio() if expected else 1.0
    if repaired_text != expected:
        raise V4AlignmentError("v4 Forced Aligner 文本守恒修复失败，已终止写回")
    if len(units) >= 8:
        start_frames = [int(unit.get("start_frame") or 0) for unit in units]
        end_frames = [int(unit.get("end_frame") or 0) for unit in units]
        aligned_span = max(end_frames) - min(start_frames)
        same_start_counts = Counter(start_frames)
        minimum_span = max(2, int(math.ceil(len(units) * 0.50)))
        maximum_same_start = max(4, int(math.ceil(len(units) * 0.25)))
        maximum_observed_same_start = max(same_start_counts.values())
        if aligned_span < minimum_span or maximum_observed_same_start > maximum_same_start:
            raise V4AlignmentError(
                "v4 Forced Aligner 时间戳坍缩: "
                f"字符={len(units)}, 跨度={aligned_span}帧, 同帧最多={maximum_observed_same_start}"
            )
    units = annotate_asr_punctuation(units, str(payload.get("text") or ""))
    return units, {
        "forced_align_retry_count": retry_count,
        "aligned_unit_count": len(units),
        "alignment_coverage": coverage,
        "raw_alignment_coverage": raw_coverage,
        "alignment_repaired_unit_count": repaired_count,
    }


def alignment_failures_without_alternative(
    failed_windows: list[dict[str, Any]],
    successful_windows: list[dict[str, Any]],
    minimum_coverage: float = 0.80,
) -> list[dict[str, Any]]:
    uncovered: list[dict[str, Any]] = []
    for failed in failed_windows or []:
        start = int(failed.get("start_frame") or 0)
        end = max(start + 1, int(failed.get("end_frame") or start + 1))
        failed_track = int(failed.get("track_index") or 0)
        covered_intervals: list[tuple[int, int]] = []
        for successful in successful_windows or []:
            if int(successful.get("track_index") or 0) == failed_track:
                continue
            overlap_start = max(start, int(successful.get("start_frame") or 0))
            overlap_end = min(end, int(successful.get("end_frame") or 0))
            if overlap_end > overlap_start:
                covered_intervals.append((overlap_start, overlap_end))
        merged: list[tuple[int, int]] = []
        for interval_start, interval_end in sorted(covered_intervals):
            if not merged or interval_start > merged[-1][1]:
                merged.append((interval_start, interval_end))
            else:
                merged[-1] = (merged[-1][0], max(merged[-1][1], interval_end))
        covered_frames = sum(interval_end - interval_start for interval_start, interval_end in merged)
        if covered_frames / max(1, end - start) < minimum_coverage:
            uncovered.append(failed)
    return uncovered


def _normalized_text_with_raw_ends(text: str) -> tuple[str, list[int]]:
    chars: list[str] = []
    raw_ends: list[int] = []
    for raw_index, raw_char in enumerate(str(text or "")):
        for normalized_char in normalize_text(raw_char):
            chars.append(normalized_char)
            raw_ends.append(raw_index + 1)
    return "".join(chars), raw_ends


def _boundary_text_overlap(left: str, right: str, maximum_prefix_noise: int = 4) -> tuple[int, int, int]:
    left_normalized, _left_raw_ends = _normalized_text_with_raw_ends(left)
    right_normalized, right_raw_ends = _normalized_text_with_raw_ends(right)
    best = (0, 0, 0)
    maximum_offset = min(maximum_prefix_noise, max(0, len(right_normalized) - 1))
    for offset in range(maximum_offset + 1):
        maximum_size = min(len(left_normalized), len(right_normalized) - offset)
        minimum_size = 2 if offset == 0 else 4
        for size in range(maximum_size, minimum_size - 1, -1):
            if left_normalized[-size:] != right_normalized[offset:offset + size]:
                continue
            candidate = (size, -offset, right_raw_ends[offset + size - 1])
            if candidate > (best[0], -best[1], best[2]):
                best = (size, offset, right_raw_ends[offset + size - 1])
            break
    return best


def merge_overlapping_text(left: str, right: str) -> str:
    left = str(left or "")
    right = str(right or "")
    overlap_size, _prefix_noise, right_raw_end = _boundary_text_overlap(left, right)
    return left + right[right_raw_end:] if overlap_size else left + right


def merge_asr_transcripts(left: str, right: str) -> str:
    """Merge overlapping ASR text while ignoring punctuation at the join."""
    return merge_overlapping_text(left, right)


def _text_overlap_size(left: str, right: str) -> int:
    return _boundary_text_overlap(left, right)[0]


def choose_alignment_context_window(
    windows: list[dict[str, Any]],
    local_index: int,
    payloads_by_window_index: dict[int, dict[str, Any]],
) -> dict[str, Any] | None:
    if local_index < 0 or local_index >= len(windows) or len(windows) < 2:
        return None
    current = windows[local_index]
    current_text = str((payloads_by_window_index.get(int(current.get("window_index") or 0)) or {}).get("text") or "")
    choices: list[tuple[int, int, dict[str, Any]]] = []
    if local_index > 0:
        previous = windows[local_index - 1]
        previous_text = str((payloads_by_window_index.get(int(previous.get("window_index") or 0)) or {}).get("text") or "")
        choices.append((_text_overlap_size(previous_text, current_text), 1, previous))
    if local_index + 1 < len(windows):
        following = windows[local_index + 1]
        following_text = str((payloads_by_window_index.get(int(following.get("window_index") or 0)) or {}).get("text") or "")
        choices.append((_text_overlap_size(current_text, following_text), 0, following))
    return max(choices, key=lambda choice: (choice[0], choice[1]))[2] if choices else None


def require_aligned_window_with_context(
    *,
    track_audio_path: Path,
    track_start_frame: int,
    window: dict[str, Any],
    payload: dict[str, Any],
    adjacent_window: dict[str, Any] | None,
    adjacent_payload: dict[str, Any] | None,
    merged_audio_path: Path,
    align_fn: Callable[[Path, str, str], list[dict[str, Any]]],
    language: str,
    fps: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    try:
        units, diagnostic = require_aligned_units(
            Path(window["audio_path"]) if window.get("audio_path") else Path(merged_audio_path),
            payload,
            align_fn,
            language,
            fps,
            int(window["start_frame"]),
        )
        diagnostic["context_align_retry_count"] = 0
        return units, diagnostic
    except V4AlignmentError:
        if adjacent_window is None or adjacent_payload is None:
            raise

    pairs = [(window, payload), (adjacent_window, adjacent_payload)]
    pairs.sort(key=lambda pair: int(pair[0].get("start_frame") or 0))
    merged_text = merge_overlapping_text(
        str(pairs[0][1].get("text") or ""),
        str(pairs[1][1].get("text") or ""),
    )
    merged_start = min(int(pair[0].get("start_frame") or 0) for pair in pairs)
    merged_end = max(int(pair[0].get("end_frame") or merged_start + 1) for pair in pairs)
    write_context_window_audio(
        Path(track_audio_path),
        Path(merged_audio_path),
        int(track_start_frame),
        merged_start,
        merged_end,
        fps,
    )
    units, diagnostic = require_aligned_units(
        Path(merged_audio_path),
        {"text": merged_text, "segments": []},
        align_fn,
        language,
        fps,
        merged_start,
    )
    diagnostic["forced_align_retry_count"] = int(diagnostic.get("forced_align_retry_count") or 0) + 1
    diagnostic["context_align_retry_count"] = 1
    return units, diagnostic


def smooth_speaker_assignments(
    units: list[dict[str, Any]],
    fps: float,
    minimum_hold_seconds: float = 0.3,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    output = [dict(unit) for unit in sorted(units or [], key=lambda row: (int(row.get("start_frame") or 0), int(row.get("end_frame") or 0)))]
    minimum_frames = max(1, int(math.ceil(minimum_hold_seconds * fps)))
    suppressed = 0
    index = 0
    while index < len(output):
        track = int(output[index].get("track_index") or 0)
        end_index = index + 1
        while end_index < len(output) and int(output[end_index].get("track_index") or 0) == track:
            end_index += 1
        run_start = int(output[index].get("start_frame") or 0)
        run_end = int(output[end_index - 1].get("end_frame") or run_start + 1)
        previous_track = int(output[index - 1].get("track_index") or 0) if index > 0 else 0
        next_track = int(output[end_index].get("track_index") or 0) if end_index < len(output) else 0
        if run_end - run_start < minimum_frames and previous_track and previous_track == next_track and track != previous_track:
            for row_index in range(index, end_index):
                output[row_index]["track_index"] = previous_track
                output[row_index]["speaker_decision"] = "smoothed_short_flip"
            suppressed += 1
        index = end_index
    return output, {"short_speaker_flip_suppressed_count": suppressed}


def build_exclusive_unit_stream(
    candidates: list[dict[str, Any]],
    fps: float,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    ordered = sorted(
        [dict(candidate) for candidate in candidates or []],
        key=lambda row: (int(row.get("start_frame") or 0), int(row.get("end_frame") or 0)),
    )
    if not ordered:
        return [], {
            "cross_mic_duplicate_count": 0,
            "viterbi_source_switch_count": 0,
            "short_speaker_flip_suppressed_count": 0,
            "overlap_candidate_suppressed_count": 0,
            "structural_duplicate_suppressed_count": 0,
            "cross_mic_boundary_serialized_count": 0,
        }

    clusters: list[dict[str, Any]] = []
    for candidate in ordered:
        start = int(candidate.get("start_frame") or 0)
        end = max(start + 1, int(candidate.get("end_frame") or start + 1))
        candidate_text = normalize_text(candidate.get("text"))
        target: dict[str, Any] | None = None
        for cluster in reversed(clusters[-3:]):
            anchor = cluster["anchor"]
            anchor_start = int(anchor.get("start_frame") or 0)
            anchor_end = max(anchor_start + 1, int(anchor.get("end_frame") or anchor_start + 1))
            overlap = max(0, min(end, anchor_end) - max(start, anchor_start))
            if overlap <= 0:
                continue
            same_text = candidate_text and candidate_text == normalize_text(anchor.get("text"))
            # Forced-aligned adjacent characters can overlap by a few frames.
            # Timing overlap alone is not duplicate evidence: clustering two
            # different characters here silently deletes transcript content.
            if same_text:
                target = cluster
                break
        if target is None:
            clusters.append({"anchor": candidate, "candidates": [candidate]})
        else:
            target["candidates"].append(candidate)

    choices_by_cluster: list[list[dict[str, Any]]] = []
    suppressed = 0
    for cluster in clusters:
        best_by_track: dict[int, dict[str, Any]] = {}
        for candidate in cluster["candidates"]:
            track = int(candidate.get("track_index") or 0)
            current = best_by_track.get(track)
            quality = (
                float(candidate.get("source_score") or 0.0),
                int(candidate.get("end_frame") or 0) - int(candidate.get("start_frame") or 0),
            )
            current_quality = (
                float(current.get("source_score") or 0.0),
                int(current.get("end_frame") or 0) - int(current.get("start_frame") or 0),
            ) if current else (-math.inf, -1)
            if current is None or quality > current_quality:
                best_by_track[track] = candidate
        choices = list(best_by_track.values())
        choices_by_cluster.append(choices)
        suppressed += max(0, len(cluster["candidates"]) - 1)

    switch_penalty = 10.0
    hold_frames = max(1, int(round(0.30 * fps)))
    scores: list[list[float]] = []
    previous_choice: list[list[int]] = []
    for cluster_index, choices in enumerate(choices_by_cluster):
        cluster_scores = [-math.inf] * len(choices)
        cluster_previous = [-1] * len(choices)
        for choice_index, choice in enumerate(choices):
            emission = float(choice.get("source_score") or 0.0)
            if cluster_index == 0:
                cluster_scores[choice_index] = emission
                continue
            previous_choices = choices_by_cluster[cluster_index - 1]
            for prior_index, prior in enumerate(previous_choices):
                transition = 0.0
                if int(prior.get("track_index") or 0) != int(choice.get("track_index") or 0):
                    gap = int(choice.get("start_frame") or 0) - int(prior.get("end_frame") or 0)
                    transition -= switch_penalty if gap < hold_frames else switch_penalty * 0.35
                score = scores[-1][prior_index] + emission + transition
                if score > cluster_scores[choice_index]:
                    cluster_scores[choice_index] = score
                    cluster_previous[choice_index] = prior_index
        scores.append(cluster_scores)
        previous_choice.append(cluster_previous)

    selected_indices = [0] * len(choices_by_cluster)
    selected_indices[-1] = max(range(len(scores[-1])), key=lambda index: scores[-1][index])
    for cluster_index in range(len(choices_by_cluster) - 1, 0, -1):
        selected_indices[cluster_index - 1] = previous_choice[cluster_index][selected_indices[cluster_index]]
    selected = [
        dict(choices_by_cluster[index][selected_indices[index]])
        for index in range(len(choices_by_cluster))
    ]
    selected, boundary_serialized_count = _serialize_punctuated_cross_mic_boundaries(selected, fps)
    selected.sort(key=lambda row: (int(row.get("start_frame") or 0), int(row.get("end_frame") or 0)))
    exclusive: list[dict[str, Any]] = []
    overlap_suppressed = 0
    for candidate in selected:
        conflicts = [
            index
            for index, existing in enumerate(exclusive)
            if int(existing.get("track_index") or 0) != int(candidate.get("track_index") or 0)
            and int(existing.get("end_frame") or 0) > int(candidate.get("start_frame") or 0)
            and int(candidate.get("end_frame") or 0) > int(existing.get("start_frame") or 0)
        ]
        if not conflicts:
            exclusive.append(candidate)
            continue
        best_existing = max(conflicts, key=lambda index: float(exclusive[index].get("source_score") or 0.0))
        if float(candidate.get("source_score") or 0.0) > float(exclusive[best_existing].get("source_score") or 0.0):
            for index in reversed(conflicts):
                exclusive.pop(index)
                overlap_suppressed += 1
            exclusive.append(candidate)
        else:
            overlap_suppressed += 1
    exclusive.sort(key=lambda row: (int(row.get("start_frame") or 0), int(row.get("end_frame") or 0)))
    exclusive, structural_duplicate_count = _suppress_structural_duplicates(exclusive, fps)
    smoothed, smooth_diagnostic = smooth_speaker_assignments(exclusive, fps)
    source_switches = sum(
        1
        for left, right in zip(smoothed, smoothed[1:])
        if int(left.get("track_index") or 0) != int(right.get("track_index") or 0)
    )
    return smoothed, {
        "cross_mic_duplicate_count": suppressed,
        "viterbi_source_switch_count": source_switches,
        "overlap_candidate_suppressed_count": overlap_suppressed,
        "structural_duplicate_suppressed_count": structural_duplicate_count,
        "cross_mic_boundary_serialized_count": boundary_serialized_count,
        **smooth_diagnostic,
    }


def _serialize_punctuated_cross_mic_boundaries(
    units: list[dict[str, Any]],
    fps: float,
) -> tuple[list[dict[str, Any]], int]:
    output = [dict(unit) for unit in units or []]
    output.sort(key=lambda row: (int(row.get("start_frame") or 0), int(row.get("end_frame") or 0)))
    maximum_overlap = max(2, int(math.ceil(0.18 * fps)))
    serialized = 0

    for terminal_index, terminal in enumerate(output):
        if float(terminal.get("asr_punctuation_strength") or 0.0) < 1.0:
            continue
        terminal_track = int(terminal.get("track_index") or 0)
        terminal_start = int(terminal.get("start_frame") or 0)
        terminal_end = int(terminal.get("end_frame") or terminal_start + 1)
        if terminal_end - terminal_start < 2:
            continue

        previous = next(
            (
                row
                for row in reversed(output[:terminal_index])
                if int(row.get("track_index") or 0) == terminal_track
                and 0 <= terminal_start - int(row.get("end_frame") or 0) <= 1
            ),
            None,
        )
        if previous is None:
            continue
        previous_start = int(previous.get("start_frame") or 0)

        onset_candidates: list[dict[str, Any]] = []
        for onset_index, onset in enumerate(output):
            if int(onset.get("track_index") or 0) == terminal_track:
                continue
            onset_start = int(onset.get("start_frame") or 0)
            onset_end = int(onset.get("end_frame") or onset_start + 1)
            overlap_span = min(terminal_end, onset_end) - max(previous_start, onset_start)
            if overlap_span <= 0 or overlap_span > maximum_overlap:
                continue
            onset_track = int(onset.get("track_index") or 0)
            has_continuation = any(
                int(following.get("track_index") or 0) == onset_track
                and 0 <= int(following.get("start_frame") or 0) - onset_end <= 1
                for following in output[onset_index + 1:onset_index + 4]
            )
            if has_continuation:
                onset_candidates.append(onset)
        if not onset_candidates:
            continue

        onset = max(onset_candidates, key=lambda row: float(row.get("source_score") or 0.0))
        onset_start = int(onset.get("start_frame") or 0)
        onset_end = int(onset.get("end_frame") or onset_start + 1)
        lower = max(terminal_start + 1, onset_start)
        upper = min(terminal_end - 1, onset_end - 1)
        if lower > upper:
            continue
        boundary = (lower + upper) // 2
        terminal["end_frame"] = boundary
        onset["start_frame"] = boundary
        terminal["speaker_decision"] = "serialized_punctuated_overlap"
        onset["speaker_decision"] = "serialized_punctuated_overlap"
        serialized += 1

    output.sort(key=lambda row: (int(row.get("start_frame") or 0), int(row.get("end_frame") or 0)))
    return output, serialized


def _suppress_structural_duplicates(
    units: list[dict[str, Any]],
    fps: float,
) -> tuple[list[dict[str, Any]], int]:
    output = [dict(unit) for unit in units]
    suppressed = 0

    index = 1
    while index < len(output):
        left = output[index - 1]
        right = output[index]
        same_text = normalize_text(left.get("text")) == normalize_text(right.get("text"))
        different_tracks = int(left.get("track_index") or 0) != int(right.get("track_index") or 0)
        gap = int(right.get("start_frame") or 0) - int(left.get("end_frame") or 0)
        punctuated_boundary = (
            gap <= 1
            and float(left.get("asr_punctuation_strength") or 0.0) >= 1.0
            and float(right.get("asr_punctuation_strength") or 0.0) >= 1.0
        )
        touching_duplicate = gap <= 1
        if same_text and different_tracks and (punctuated_boundary or touching_duplicate):
            keep_right = float(right.get("source_score") or 0.0) > float(left.get("source_score") or 0.0)
            output.pop(index - 1 if keep_right else index)
            suppressed += 1
            index = max(1, index - 1)
            continue
        index += 1

    if len(output) >= 3:
        stale, current, continuation = output[:3]
        stale_gap = int(current.get("start_frame") or 0) - int(stale.get("end_frame") or 0)
        continuation_gap = int(continuation.get("start_frame") or 0) - int(current.get("end_frame") or 0)
        stale_duration = int(stale.get("end_frame") or 0) - int(stale.get("start_frame") or 0)
        if (
            normalize_text(stale.get("text")) == normalize_text(current.get("text"))
            and int(stale.get("track_index") or 0) != int(current.get("track_index") or 0)
            and int(current.get("track_index") or 0) == int(continuation.get("track_index") or 0)
            and stale_gap >= int(round(0.80 * fps))
            and continuation_gap <= int(round(0.30 * fps))
            and stale_duration <= max(2, int(round(0.08 * fps)))
            and not stale.get("independent_vad")
            and float(stale.get("source_score") or 0.0) < float(current.get("source_score") or 0.0)
        ):
            output.pop(0)
            suppressed += 1

    return output, suppressed


def _is_break_punctuation(text: str) -> bool:
    return any(char in str(text or "") for char in "。！？!?；;")


def _boundary_strength(units: list[dict[str, Any]], position: int, fps: float, profile: dict[str, Any] | None) -> float:
    left = units[position - 1]
    right = units[position]
    score = 0.0
    gap = int(right.get("start_frame") or 0) - int(left.get("end_frame") or 0)
    if gap >= int(round(0.65 * fps)):
        score += 8.0
    elif gap >= int(round(0.25 * fps)):
        score += 3.0
    if int(left.get("track_index") or 0) != int(right.get("track_index") or 0):
        score += 4.0
    if _is_break_punctuation(str(left.get("text") or "")):
        score += 8.0
    score += float(left.get("asr_punctuation_strength") or 0.0) * 8.0
    context = "".join(str(unit.get("text") or "") for unit in units)
    right_context = normalize_text("".join(str(unit.get("text") or "") for unit in units[position:]))
    if any(right_context.startswith(starter) for starter in CLAUSE_STARTERS):
        score += 5.0
    if any(right_context.startswith(prefix) for prefix in NON_BREAK_RIGHT_PREFIXES):
        score -= 20.0
    if profile and profile.get("boundary_model"):
        model = profile["boundary_model"]
        probability = boundary_probability(context, position, model)
        threshold = max(0.05, min(0.95, float(model.get("decision_threshold") or 0.5)))
        if probability >= threshold:
            model_margin = (probability - threshold) / max(1e-6, 1.0 - threshold)
        else:
            model_margin = (probability - threshold) / max(1e-6, threshold)
        score += model_margin * 4.0
    if normalize_text(str(left.get("text") or "") + str(right.get("text") or "")) in PROTECTED_BIGRAMS:
        score -= 20.0
    return score


def _has_non_model_boundary_evidence(units: list[dict[str, Any]], position: int, fps: float) -> bool:
    if position <= 0 or position >= len(units):
        return False
    left = units[position - 1]
    right = units[position]
    gap = int(right.get("start_frame") or 0) - int(left.get("end_frame") or 0)
    if gap >= int(round(0.25 * fps)):
        return True
    if int(left.get("track_index") or 0) != int(right.get("track_index") or 0):
        return True
    if float(left.get("asr_punctuation_strength") or 0.0) > 0.0:
        return True
    right_context = normalize_text("".join(str(unit.get("text") or "") for unit in units[position:]))
    return any(right_context.startswith(starter) for starter in CLAUSE_STARTERS)


def _fill_short_segment_gaps(rows: list[dict[str, Any]], fps: float) -> int:
    maximum_gap = max(1, int(round(0.40 * fps)))
    filled = 0
    for left, right in zip(rows, rows[1:]):
        gap = int(right.get("start_frame") or 0) - int(left.get("end_frame") or 0)
        if 0 < gap <= maximum_gap:
            left["end_frame"] = int(right.get("start_frame") or left.get("end_frame") or 0)
            filled += 1
    return filled


def _finalize_segment_rows(rows: list[dict[str, Any]], fps: float, mode: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    minimum_frames = max(1, int(math.ceil(0.18 * fps)))
    prepared = [dict(row) for row in rows]
    incomplete_merged = 0
    if mode == "live":
        merged_rows: list[dict[str, Any]] = []
        index = 0
        while index < len(prepared):
            row = dict(prepared[index])
            while index + 1 < len(prepared):
                next_row = prepared[index + 1]
                row_text = normalize_text(row.get("text"))
                next_text = normalize_text(next_row.get("text"))
                same_speaker = int(row.get("speaker_track_index") or 0) == int(next_row.get("speaker_track_index") or 0)
                if (
                    not row_text.endswith(INCOMPLETE_CLAUSE_SUFFIXES)
                    or not same_speaker
                    or any(next_text.startswith(starter) for starter in CLAUSE_STARTERS)
                    or len(row_text) + len(next_text) > 18
                ):
                    break
                row["text"] = str(row.get("text") or "") + str(next_row.get("text") or "")
                row["end_frame"] = int(next_row.get("end_frame") or row.get("end_frame") or 0)
                row["independent_vad"] = bool(row.get("independent_vad") or next_row.get("independent_vad"))
                index += 1
                incomplete_merged += 1
            merged_rows.append(row)
            index += 1
        prepared = merged_rows
    accepted, orphan_merged = _merge_orphan_segment_rows(prepared, fps)
    for index, row in enumerate(accepted):
        start = int(row.get("start_frame") or 0)
        desired_end = max(int(row.get("end_frame") or start + 1), start + minimum_frames)
        if index + 1 < len(accepted):
            desired_end = min(desired_end, int(accepted[index + 1].get("start_frame") or desired_end))
        row["end_frame"] = max(start + 1, desired_end)
        row["index"] = index + 1
        row.pop("independent_vad", None)
    short_gap_filled = _fill_short_segment_gaps(accepted, fps)
    return accepted, {
        "ordinary_single_character_count": sum(
            1
            for row in accepted
            if len(normalize_text(row.get("text"))) == 1
            and normalize_text(row.get("text")) not in SHORT_INTERJECTIONS
        ),
        "ordinary_single_character_suppressed_count": 0,
        "short_reply_suppressed_count": 0,
        "orphan_segment_merged_count": orphan_merged,
        "incomplete_clause_merged_count": incomplete_merged,
        "short_gap_filled_count": short_gap_filled,
    }


def _merge_orphan_segment_rows(rows: list[dict[str, Any]], fps: float) -> tuple[list[dict[str, Any]], int]:
    output = [dict(row) for row in rows]
    maximum_gap = max(1, int(round(0.80 * fps)))
    merged = 0
    index = 0
    while index < len(output):
        row = output[index]
        normalized = normalize_text(row.get("text"))
        duration = int(row.get("end_frame") or 0) - int(row.get("start_frame") or 0)
        numeric_response = bool(normalized) and all(char.isdigit() or char in "零一二三四五六七八九十" for char in normalized)
        independent_reply = (
            len(normalized) <= 2
            and (normalized in SHORT_INTERJECTIONS or numeric_response)
            and duration >= max(1, int(math.ceil(0.18 * fps)))
        )
        if len(normalized) > 1 or independent_reply or len(output) == 1:
            index += 1
            continue

        candidates: list[tuple[int, int, int]] = []
        if index > 0:
            previous = output[index - 1]
            gap = int(row.get("start_frame") or 0) - int(previous.get("end_frame") or 0)
            combined_length = len(normalize_text(previous.get("text"))) + len(normalized)
            if gap <= maximum_gap and combined_length <= 18:
                same_speaker = int(previous.get("speaker_track_index") or 0) == int(row.get("speaker_track_index") or 0)
                candidates.append((0 if same_speaker else 1, max(0, gap), -1))
        if index + 1 < len(output):
            following = output[index + 1]
            gap = int(following.get("start_frame") or 0) - int(row.get("end_frame") or 0)
            combined_length = len(normalized) + len(normalize_text(following.get("text")))
            if gap <= maximum_gap and combined_length <= 18:
                same_speaker = int(following.get("speaker_track_index") or 0) == int(row.get("speaker_track_index") or 0)
                candidates.append((0 if same_speaker else 1, max(0, gap), 1))
        if not candidates:
            index += 1
            continue

        direction = min(candidates)[2]
        if direction < 0:
            previous = output[index - 1]
            previous["text"] = str(previous.get("text") or "") + str(row.get("text") or "")
            previous["end_frame"] = max(int(previous.get("end_frame") or 0), int(row.get("end_frame") or 0))
            previous["independent_vad"] = bool(previous.get("independent_vad") or row.get("independent_vad"))
            output.pop(index)
            index = max(0, index - 1)
        else:
            following = output[index + 1]
            following["text"] = str(row.get("text") or "") + str(following.get("text") or "")
            following["start_frame"] = min(int(row.get("start_frame") or 0), int(following.get("start_frame") or 0))
            following["independent_vad"] = bool(row.get("independent_vad") or following.get("independent_vad"))
            output.pop(index)
        merged += 1
    return output, merged


def _merge_single_unit_speaker_islands(units: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    output = [dict(unit) for unit in units]
    merged = 0
    index = 0
    while index < len(output):
        track = int(output[index].get("track_index") or 0)
        end = index + 1
        while end < len(output) and int(output[end].get("track_index") or 0) == track:
            end += 1
        run_text = "".join(normalize_text(unit.get("text")) for unit in output[index:end])
        independent_reply = bool(run_text in SHORT_INTERJECTIONS and any(unit.get("independent_vad") for unit in output[index:end]))
        if len(run_text) == 1 and not independent_reply and len(output) > end - index:
            previous_track = int(output[index - 1].get("track_index") or 0) if index > 0 else 0
            next_track = int(output[end].get("track_index") or 0) if end < len(output) else 0
            if previous_track and previous_track == next_track:
                target_track = previous_track
            elif previous_track and next_track:
                previous_gap = int(output[index].get("start_frame") or 0) - int(output[index - 1].get("end_frame") or 0)
                next_gap = int(output[end].get("start_frame") or 0) - int(output[end - 1].get("end_frame") or 0)
                target_track = previous_track if previous_gap <= next_gap else next_track
            else:
                target_track = previous_track or next_track
            if target_track:
                for unit in output[index:end]:
                    unit["track_index"] = target_track
                    unit["speaker_decision"] = "merged_single_unit_island"
                merged += 1
        index = end
    return output, merged


def segment_canonical_units(
    units: list[dict[str, Any]],
    mode: str,
    profile: dict[str, Any] | None,
    fps: float,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    ordered = sorted([dict(unit) for unit in units or []], key=lambda row: (int(row.get("start_frame") or 0), int(row.get("end_frame") or 0)))
    if not ordered:
        return [], {"ordinary_single_character_count": 0, "text_conservation_failed_count": 0}
    expected_text = "".join(normalize_text(unit.get("text")) for unit in ordered)
    ordered, single_unit_merged_count = _merge_single_unit_speaker_islands(ordered)
    hard_positions = [
        position
        for position in range(1, len(ordered))
        if (
            int(ordered[position].get("start_frame") or 0) - int(ordered[position - 1].get("end_frame") or 0)
            >= int(round(0.80 * fps))
            or int(ordered[position - 1].get("track_index") or 0) != int(ordered[position].get("track_index") or 0)
            or float(ordered[position - 1].get("asr_punctuation_strength") or 0.0) >= 1.0
        )
    ]
    if hard_positions:
        rows: list[dict[str, Any]] = []
        start = 0
        ordinary_suppressed = 0
        short_reply_suppressed = 0
        incomplete_merged = 0
        short_gap_filled = 0
        for end in [*hard_positions, len(ordered)]:
            group_rows, group_diagnostic = segment_canonical_units(ordered[start:end], mode, profile, fps)
            rows.extend(group_rows)
            ordinary_suppressed += int(group_diagnostic.get("ordinary_single_character_suppressed_count") or 0)
            short_reply_suppressed += int(group_diagnostic.get("short_reply_suppressed_count") or 0)
            incomplete_merged += int(group_diagnostic.get("incomplete_clause_merged_count") or 0)
            short_gap_filled += int(group_diagnostic.get("short_gap_filled_count") or 0)
            start = end
        rows, orphan_merged = _merge_orphan_segment_rows(rows, fps)
        minimum_frames = max(1, int(math.ceil(0.18 * fps)))
        for index in range(len(rows) - 1):
            left = rows[index]
            right = rows[index + 1]
            boundary = int(right.get("start_frame") or 0)
            left["end_frame"] = min(int(left.get("end_frame") or boundary), boundary)
            if int(left["end_frame"]) - int(left.get("start_frame") or 0) < minimum_frames:
                previous_end = int(rows[index - 1].get("end_frame") or 0) if index > 0 else -10**18
                left["start_frame"] = max(previous_end, int(left["end_frame"]) - minimum_frames)
        short_gap_filled += _fill_short_segment_gaps(rows, fps)
        for index, row in enumerate(rows, start=1):
            row["index"] = index
        actual_text = "".join(normalize_text(row.get("text")) for row in rows)
        if actual_text != expected_text:
            raise RuntimeError("v4 segmentation text conservation failed")
        return rows, {
            "ordinary_single_character_count": sum(
                1
                for row in rows
                if len(normalize_text(row.get("text"))) == 1
                and normalize_text(row.get("text")) not in SHORT_INTERJECTIONS
            ),
            "ordinary_single_character_suppressed_count": ordinary_suppressed,
            "short_reply_suppressed_count": short_reply_suppressed,
            "hard_boundary_count": len(hard_positions),
            "single_unit_merged_count": single_unit_merged_count,
            "incomplete_clause_merged_count": incomplete_merged,
            "orphan_segment_merged_count": orphan_merged,
            "short_gap_filled_count": short_gap_filled,
            "text_conservation_failed_count": 0,
        }
    mode_profile = ((profile or {}).get("modes") or {}).get(mode) if profile and isinstance(profile.get("modes"), dict) else profile
    preferred_min, preferred_max, hard_max = (6, 14, 18) if mode == "live" else (8, 16, 18)
    count = len(ordered)
    if count <= hard_max and not any(
        _has_non_model_boundary_evidence(ordered, position, fps)
        for position in range(1, count)
    ):
        row = {
            "index": 1,
            "text": "".join(str(unit.get("text") or "") for unit in ordered),
            "start_frame": int(ordered[0].get("start_frame") or 0),
            "end_frame": max(
                int(ordered[0].get("start_frame") or 0) + 1,
                int(ordered[-1].get("end_frame") or 0),
            ),
            "speaker_track_index": int(ordered[0].get("track_index") or 0),
            "segmentation_decision": "v4_unsplit_complete_region",
            "independent_vad": any(bool(unit.get("independent_vad")) for unit in ordered),
        }
        finalized_rows, finalized_diagnostic = _finalize_segment_rows([row], fps, mode)
        if "".join(normalize_text(item.get("text")) for item in finalized_rows) != expected_text:
            raise RuntimeError("v4 segmentation text conservation failed")
        finalized_diagnostic["single_unit_merged_count"] = single_unit_merged_count
        finalized_diagnostic["text_conservation_failed_count"] = 0
        return finalized_rows, finalized_diagnostic
    scores = [-math.inf] * (count + 1)
    previous = [-1] * (count + 1)
    scores[0] = 0.0
    for end in range(1, count + 1):
        for start in range(max(0, end - hard_max), end):
            if not math.isfinite(scores[start]):
                continue
            length = end - start
            text = "".join(str(unit.get("text") or "") for unit in ordered[start:end])
            normalized = normalize_text(text)
            duration = int(ordered[end - 1].get("end_frame") or 0) - int(ordered[start].get("start_frame") or 0)
            short_reply = len(normalized) <= 2 and normalized in SHORT_INTERJECTIONS and duration >= int(math.ceil(0.18 * fps))
            boundary_score = _boundary_strength(ordered, end, fps, mode_profile) if end < count else 0.0
            strong_short_clause = False
            if length < preferred_min and not short_reply and not (start == 0 and end == count):
                start_strength = _boundary_strength(ordered, start, fps, mode_profile) if start > 0 else 3.0
                end_strength = boundary_score if end < count else 3.0
                if len(normalized) < 3 or min(start_strength, end_strength) < 3.0:
                    continue
                if start > 0 and not _has_non_model_boundary_evidence(ordered, start, fps):
                    continue
                if end < count and not _has_non_model_boundary_evidence(ordered, end, fps):
                    continue
                strong_short_clause = True
            length_score = -abs(length - 11) * 0.25
            if preferred_min <= length <= preferred_max:
                length_score += 1.0
            if strong_short_clause:
                length_score += 2.5
            # Acoustic/punctuation evidence may add an editorial boundary.
            # A text-model-only boundary pays a higher cost so hash/model
            # rewards cannot create unnecessary fragments.
            split_penalty = 0.0
            if end < count:
                split_penalty = 2.5 if _has_non_model_boundary_evidence(ordered, end, fps) else 16.0
            candidate = scores[start] + length_score + boundary_score - split_penalty
            if candidate > scores[end]:
                scores[end] = candidate
                previous[end] = start
    if previous[-1] < 0:
        previous[-1] = 0

    spans: list[tuple[int, int]] = []
    cursor = count
    while cursor > 0:
        start = previous[cursor]
        if start < 0:
            start = 0
        spans.append((start, cursor))
        cursor = start
    spans.reverse()
    rows: list[dict[str, Any]] = []
    for start, end in spans:
        text = "".join(str(unit.get("text") or "") for unit in ordered[start:end])
        start_frame = int(ordered[start].get("start_frame") or 0)
        end_frame = int(ordered[end - 1].get("end_frame") or start_frame + 1)
        rows.append(
            {
                "index": len(rows) + 1,
                "text": text,
                "start_frame": start_frame,
                "end_frame": max(start_frame + 1, end_frame),
                "speaker_track_index": int(ordered[start].get("track_index") or 0),
                "segmentation_decision": "v4_boundary_dp",
                "independent_vad": any(bool(unit.get("independent_vad")) for unit in ordered[start:end]),
            }
        )
    finalized_rows, finalized_diagnostic = _finalize_segment_rows(rows, fps, mode)
    actual_text = "".join(normalize_text(row.get("text")) for row in finalized_rows)
    if actual_text != expected_text:
        raise RuntimeError("v4 segmentation text conservation failed")
    finalized_diagnostic["single_unit_merged_count"] = single_unit_merged_count
    finalized_diagnostic["text_conservation_failed_count"] = 0
    return finalized_rows, finalized_diagnostic


def _feature_names(text: str, position: int) -> list[str]:
    left1 = text[position - 1:position]
    right1 = text[position:position + 1]
    cross = left1 + right1
    right_context = text[position:]
    starter = next((value for value in CLAUSE_STARTERS if right_context.startswith(value)), "")
    return [
        "bias",
        f"l1={left1}",
        f"r1={right1}",
        f"l2={text[max(0, position - 2):position]}",
        f"r2={text[position:position + 2]}",
        f"cross={cross}",
        f"ascii={int(left1.isascii())}{int(right1.isascii())}",
        f"particle={int(right1 in '了的呢吧吗啊呀嘛啦')}",
        f"protected={int(cross in PROTECTED_BIGRAMS)}",
        f"starter={starter or 'none'}",
    ]


def _bucket(name: str, bucket_count: int = MODEL_BUCKETS) -> int:
    return int.from_bytes(hashlib.sha1(name.encode("utf-8")).digest()[:4], "big") % bucket_count


def _sigmoid(value: float) -> float:
    if value >= 0:
        exp_value = math.exp(-value)
        return 1.0 / (1.0 + exp_value)
    exp_value = math.exp(value)
    return exp_value / (1.0 + exp_value)


def boundary_probability(text: str, position: int, model: dict[str, Any]) -> float:
    feature_weights = model.get("feature_weights")
    if isinstance(feature_weights, dict):
        score = float(model.get("bias") or 0.0)
        for feature in _feature_names(text, position):
            score += float(feature_weights.get(feature) or 0.0)
        return _sigmoid(score)
    weights = [float(value) for value in model.get("weights") or []]
    if not weights:
        return 0.5
    score = float(model.get("bias") or 0.0)
    observed_raw = model.get("observed_features")
    observed = set(str(feature) for feature in observed_raw) if isinstance(observed_raw, list) else None
    for feature in _feature_names(text, position):
        if observed is not None and feature not in observed:
            continue
        score += weights[_bucket(feature, len(weights))]
    return _sigmoid(score)


def _map_manual_boundary_positions(source_text: str, manual_chunks: list[str]) -> list[int | None]:
    manual_text = "".join(manual_chunks)
    matcher = difflib.SequenceMatcher(None, source_text, manual_text, autojunk=False)
    boundaries: list[int | None] = []
    manual_cursor = 0
    for chunk in manual_chunks[:-1]:
        manual_cursor += len(chunk)
        mapped_position: int | None = None
        for source_start, target_start, size in matcher.get_matching_blocks():
            if target_start <= manual_cursor <= target_start + size:
                mapped = source_start + (manual_cursor - target_start)
                if 0 < mapped < len(source_text):
                    mapped_position = mapped
                break
        boundaries.append(mapped_position)
    return boundaries


def _map_manual_boundaries(source_text: str, manual_chunks: list[str]) -> set[int]:
    return {position for position in _map_manual_boundary_positions(source_text, manual_chunks) if position is not None}


def _source_position_for_frame(plugin_rows: list[dict[str, Any]], frame: int) -> int | None:
    offset = 0
    candidates: list[tuple[int, int]] = []
    for row in plugin_rows:
        text = normalize_text(row.get("text"))
        length = len(text)
        start = int(row.get("start_frame") or 0)
        end = max(start + 1, int(row.get("end_frame") or start + 1))
        candidates.append((abs(frame - start), offset))
        candidates.append((abs(frame - end), offset + length))
        if start <= frame <= end and length > 0:
            ratio = (frame - start) / max(1, end - start)
            return max(offset, min(offset + length, offset + int(round(ratio * length))))
        offset += length
    if not candidates:
        return None
    return min(candidates, key=lambda item: (item[0], item[1]))[1]


def _map_manual_boundaries_with_timing(
    plugin_rows: list[dict[str, Any]],
    manual_rows: list[dict[str, Any]],
) -> tuple[set[int], dict[str, int]]:
    source_text = "".join(normalize_text(row.get("text")) for row in plugin_rows)
    manual_chunks = [normalize_text(row.get("text")) for row in manual_rows if normalize_text(row.get("text"))]
    text_positions = _map_manual_boundary_positions(source_text, manual_chunks)
    boundary_rows = [row for row in manual_rows if normalize_text(row.get("text"))][1:]
    mapped: set[int] = set()
    text_count = 0
    timing_count = 0
    skipped = 0
    for index, manual_row in enumerate(boundary_rows):
        text_position = text_positions[index] if index < len(text_positions) else None
        if text_position is not None and text_position not in mapped:
            mapped.add(text_position)
            text_count += 1
            continue
        timing_position = _source_position_for_frame(plugin_rows, int(manual_row.get("start_frame") or 0))
        if timing_position is not None and 0 < timing_position < len(source_text) and timing_position not in mapped:
            mapped.add(timing_position)
            timing_count += 1
        else:
            skipped += 1
    return mapped, {
        "manual_boundary_count": len(boundary_rows),
        "mapped_boundary_count": len(mapped),
        "text_mapped_count": text_count,
        "timing_fallback_count": timing_count,
        "skipped_boundary_count": skipped,
    }


def _subtract_interval(interval: tuple[int, int], covered: list[tuple[int, int]]) -> list[tuple[int, int]]:
    regions = [interval]
    for covered_start, covered_end in covered:
        next_regions: list[tuple[int, int]] = []
        for start, end in regions:
            if covered_end <= start or covered_start >= end:
                next_regions.append((start, end))
            else:
                if start < covered_start:
                    next_regions.append((start, covered_start))
                if covered_end < end:
                    next_regions.append((covered_end, end))
        regions = next_regions
    return [region for region in regions if region[1] > region[0]]


def _train_boundary_model(
    samples: list[tuple[str, set[int]]],
    validation_samples: list[tuple[str, set[int]]] | None = None,
) -> dict[str, Any]:
    examples: list[tuple[list[str], float]] = []
    positive_count = 0
    for text, boundaries in samples:
        for position in range(1, len(text)):
            label = 1.0 if position in boundaries else 0.0
            positive_count += int(label)
            feature_names = _feature_names(text, position)
            examples.append((feature_names, label))
    negative_count = max(0, len(examples) - positive_count)
    positive_weight = max(1.0, negative_count / max(1, positive_count))
    feature_weights: dict[str, float] = {}
    bias = 0.0
    learning_rate = 0.08
    best_feature_weights: dict[str, float] | None = None
    best_bias = 0.0
    best_threshold = 0.5
    best_epoch = 0
    best_validation_f1 = -1.0
    minimum_epochs = 10
    patience = 15
    final_epoch = 0
    for epoch in range(1, 101):
        for feature_names, label in examples:
            score = bias + sum(feature_weights.get(name, 0.0) for name in feature_names)
            prediction = _sigmoid(score)
            sample_weight = positive_weight if label else 1.0
            error = (prediction - label) * sample_weight
            bias -= learning_rate * error * 0.2
            for name in feature_names:
                current = feature_weights.get(name, 0.0)
                feature_weights[name] = current - learning_rate * (error + 0.0005 * current)
        learning_rate *= 0.985
        final_epoch = epoch
        if validation_samples and epoch >= minimum_epochs:
            candidate_model = {
                "feature_weights": feature_weights,
                "bias": bias,
            }
            threshold, validation_metrics = _select_boundary_threshold(candidate_model, validation_samples)
            validation_f1 = float(validation_metrics["f1"])
            if validation_f1 > best_validation_f1 + 1e-9:
                best_validation_f1 = validation_f1
                best_feature_weights = dict(feature_weights)
                best_bias = bias
                best_threshold = threshold
                best_epoch = epoch
            if best_epoch and epoch - best_epoch >= patience:
                break
    if best_feature_weights is not None:
        feature_weights = best_feature_weights
        bias = best_bias
        final_epoch = best_epoch
    return {
        "type": "sparse_logistic_boundary_v3",
        "feature_weights": {
            name: round(value, 6)
            for name, value in sorted(feature_weights.items())
            if abs(value) >= 0.000001
        },
        "bias": round(bias, 6),
        "decision_threshold": round(best_threshold, 6),
        "positive_count": positive_count,
        "negative_count": negative_count,
        "training_epochs": final_epoch,
        "validation_f1": round(max(0.0, best_validation_f1), 6) if validation_samples else None,
    }


def _select_boundary_threshold(
    model: dict[str, Any],
    samples: list[tuple[str, set[int]]],
) -> tuple[float, dict[str, float | int]]:
    scored: list[tuple[float, bool]] = []
    for text, boundaries in samples:
        for position in range(1, len(text)):
            scored.append((boundary_probability(text, position, model), position in boundaries))
    if not scored:
        return 0.5, _evaluate_boundary_model({**model, "decision_threshold": 0.5}, samples)

    total_positive = sum(1 for _probability, label in scored if label)
    true_positive = false_positive = 0
    best_key = (-1.0, -1.0, -1.0)
    best_threshold = 0.5
    for probability, group in itertools.groupby(sorted(scored, key=lambda row: row[0], reverse=True), key=lambda row: row[0]):
        for _score, label in group:
            true_positive += int(label)
            false_positive += int(not label)
        false_negative = total_positive - true_positive
        precision = true_positive / max(1, true_positive + false_positive)
        recall = true_positive / max(1, true_positive + false_negative)
        f1 = 2.0 * precision * recall / max(1e-12, precision + recall)
        threshold = max(0.05, min(0.95, float(probability)))
        key = (f1, precision, threshold)
        if key > best_key:
            best_key = key
            best_threshold = threshold
    candidate = {**model, "decision_threshold": best_threshold}
    return best_threshold, _evaluate_boundary_model(candidate, samples)


def _evaluate_boundary_model(model: dict[str, Any], samples: list[tuple[str, set[int]]]) -> dict[str, float | int]:
    true_positive = false_positive = false_negative = 0
    threshold = float(model.get("decision_threshold") or 0.5)
    for text, boundaries in samples:
        predicted = {
            position
            for position in range(1, len(text))
            if boundary_probability(text, position, model) >= threshold
        }
        true_positive += len(predicted & boundaries)
        false_positive += len(predicted - boundaries)
        false_negative += len(boundaries - predicted)
    precision = true_positive / max(1, true_positive + false_positive)
    recall = true_positive / max(1, true_positive + false_negative)
    f1 = 2.0 * precision * recall / max(1e-12, precision + recall)
    return {
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(f1, 6),
    }


def build_segmentation_profile_v3(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    alignment_ignored = 0
    mode_payloads: dict[str, list[dict[str, Any]]] = {}
    for payload in payloads or []:
        if not isinstance(payload, dict):
            continue
        if not payload.get("plugin_rows") or not payload.get("manual_rows"):
            if str(payload.get("schema") or "").startswith("subfix_calibration"):
                alignment_ignored += 1
            continue
        mode = "live" if str(payload.get("subtitle_mode") or "live") == "live" else "narration"
        mode_payloads.setdefault(mode, []).append(payload)

    modes: dict[str, Any] = {}
    total_regions = 0
    for mode, raw_payloads in mode_payloads.items():
        ordered = sorted(raw_payloads, key=lambda row: str(row.get("exported_at") or ""), reverse=True)
        covered: dict[str, list[tuple[int, int]]] = {}
        region_records: list[dict[str, Any]] = []
        overridden = 0
        for payload in ordered:
            plugin_rows = [dict(row) for row in payload.get("plugin_rows") or []]
            manual_rows = [dict(row) for row in payload.get("manual_rows") or []]
            starts = [int(row.get("start_frame") or 0) for row in plugin_rows + manual_rows]
            ends = [int(row.get("end_frame") or 0) for row in plugin_rows + manual_rows]
            if not starts or not ends:
                continue
            interval = (min(starts), max(ends))
            project = str(payload.get("project_name") or "")
            timeline = str(payload.get("timeline_name") or "")
            scope_key = f"{project}\0{timeline}"
            regions = _subtract_interval(interval, covered.get(scope_key, []))
            if regions != [interval]:
                overridden += 1
            for region_start, region_end in regions:
                region_plugin = [row for row in plugin_rows if int(row.get("start_frame") or 0) >= region_start and int(row.get("end_frame") or 0) <= region_end]
                region_manual = [row for row in manual_rows if int(row.get("start_frame") or 0) >= region_start and int(row.get("end_frame") or 0) <= region_end]
                source_text = "".join(normalize_text(row.get("text")) for row in region_plugin)
                manual_chunks = [normalize_text(row.get("text")) for row in region_manual if normalize_text(row.get("text"))]
                if len(source_text) < 2 or not manual_chunks:
                    continue
                mapped_boundaries, mapping_diagnostic = _map_manual_boundaries_with_timing(
                    region_plugin,
                    region_manual,
                )
                region_records.append(
                    {
                        "timeline_name": timeline,
                        "project_name": project,
                        "dataset_role": str(payload.get("dataset_role") or "training"),
                        "start_frame": region_start,
                        "end_frame": region_end,
                        "sample": (source_text, mapped_boundaries),
                        "manual_lengths": [len(chunk) for chunk in manual_chunks],
                        "mapping_diagnostic": mapping_diagnostic,
                    }
                )
            covered.setdefault(scope_key, []).append(interval)
        region_records.sort(key=lambda row: (str(row["project_name"]), str(row["timeline_name"]), int(row["start_frame"]), int(row["end_frame"])))
        validation_records = [record for record in region_records if record["dataset_role"] == "validation"]
        training_records = [record for record in region_records if record["dataset_role"] != "validation"]
        if not validation_records and len(training_records) >= 3:
            validation_count = max(1, len(training_records) // 5)
            validation_records = training_records[-validation_count:]
            training_records = training_records[:-validation_count]
        training_samples = [record["sample"] for record in training_records]
        validation_samples = [record["sample"] for record in validation_records]
        manual_lengths = [length for record in training_records for length in record["manual_lengths"]]
        model = _train_boundary_model(training_samples, validation_samples)
        sorted_lengths = sorted(manual_lengths)
        median = sorted_lengths[len(sorted_lengths) // 2] if sorted_lengths else (11 if mode == "live" else 12)
        interval_fields = lambda record: {
            "project_name": str(record["project_name"]),
            "timeline_name": str(record["timeline_name"]),
            "start_frame": int(record["start_frame"]),
            "end_frame": int(record["end_frame"]),
        }
        modes[mode] = {
            "boundary_model": model,
            "length_model": {"minimum": 6 if mode == "live" else 8, "median": median, "preferred_maximum": 14 if mode == "live" else 16, "maximum": 18},
            "diagnostic": {
                "training_region_count": len(training_records),
                "validation_region_count": len(validation_records),
                "overridden_region_count": overridden,
                "manual_row_count": len(manual_lengths),
                "text_mapped_boundary_count": sum(
                    int(record["mapping_diagnostic"].get("text_mapped_count") or 0)
                    for record in region_records
                ),
                "timing_fallback_boundary_count": sum(
                    int(record["mapping_diagnostic"].get("timing_fallback_count") or 0)
                    for record in region_records
                ),
                "skipped_boundary_count": sum(
                    int(record["mapping_diagnostic"].get("skipped_boundary_count") or 0)
                    for record in region_records
                ),
                "training_intervals": [interval_fields(record) for record in training_records],
                "validation_intervals": [interval_fields(record) for record in validation_records],
                "validation_metrics": _evaluate_boundary_model(model, validation_samples),
            },
        }
        total_regions += len(training_records)
    return {
        "schema_version": PROFILE_SCHEMA,
        "modes": modes,
        "diagnostic": {
            "input_payload_count": len(payloads or []),
            "alignment_payload_ignored_count": alignment_ignored,
            "training_region_count": total_regions,
        },
    }
