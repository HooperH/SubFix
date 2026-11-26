#!/usr/bin/env python3
"""SubFix v4 canonical aligned-unit generation primitives."""

from __future__ import annotations

import difflib
import hashlib
import itertools
import json
import math
import re
import statistics
import wave
from array import array
from bisect import bisect_left
from collections import Counter, defaultdict
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
# 词边界保护 (word-boundary protection, see protect_word_boundaries): a
# small, dependency-free word list used only to decide "does this position
# fall inside a multi-character word", so a row-to-row cut (or a hard
# max_chars split) never lands inside one.
#
# Environment check done before picking this approach: the ASR helper venv
# actually used at generation time (.subfix_asr_env, the one
# setup_asr_env.sh provisions) has no jieba installed, and sync_to_plugin.sh
# only copies these .py source files into the Resolve plugin folder -- it
# never syncs/rebuilds .subfix_asr_env. Shipping `import jieba` here would
# ImportError on every already-installed machine until the user re-runs
# setup_asr_env.sh, and setup_asr_env.sh itself would first need a new `pip
# install jieba` line -- not retroactive for existing installs either way.
# That is exactly the "分词依赖无法可靠分发" risk called out in the spec, so
# this intentionally does NOT add jieba (or any pip dependency): it is a
# plain in-memory dictionary + greedy maximum-forward-match tokenizer
# (_default_word_tokenizer below). Coverage is intentionally partial -- a
# word missing from this list can still be split across rows -- but it is
# zero-dependency and needs no environment/packaging change to ship.
WORD_DICTIONARY = frozenset({
    # common function words / pronouns / demonstratives
    "这个", "那个", "什么", "一个", "没有", "可以", "因为", "所以", "但是", "而且", "已经", "我们", "你们",
    "他们", "自己", "现在", "还是", "就是", "不是", "什么样", "怎么样", "为什么", "这些", "那些", "这样",
    "那样", "一样", "一直", "一起", "一下", "一遍", "一定", "一些", "真的", "真的是", "绝对", "不过",
    "然后", "另外", "同时", "因此", "其实", "如果", "虽然", "所有", "所有人", "大家", "只是", "只要",
    "想过", "想要", "看看", "有点", "别的", "地方", "还能", "还没", "算一下", "加一起",
    # nouns/verbs from typical spoken-video content (helps the shipped
    # fixtures/regression videos as well as general coverage)
    "效果", "视频", "平台", "硬件", "产品", "功能", "机器", "机器人", "价格", "块钱", "毛钱", "包邮",
    "体验", "广告", "意思", "存在", "合理", "尺寸", "性价比", "扫地机", "拼多多", "迫不及待", "发展",
    "推出", "获得", "开始", "原谅", "顺到", "摇一摇", "破两百", "能够", "做出", "告诉", "成功", "感觉",
    "科技", "评论", "转发", "帮助", "环绕", "运镜", "自动", "逻辑", "神奇", "非常", "力气", "姿态",
    "控制", "跳舞", "摄影棚", "录音棚", "演播室", "摄影机", "摄像机",
})
_WORD_DICTIONARY_MAX_LENGTH = max((len(word) for word in WORD_DICTIONARY), default=1)


def _default_word_tokenizer(text: str) -> list[str]:
    """Zero-dependency tokenizer: greedy maximum-forward-match against
    WORD_DICTIONARY, falling back to single characters for anything not in
    the dictionary. See WORD_DICTIONARY for why this exists instead of a
    real segmenter dependency (jieba, etc). Always partitions ``text``
    exactly (concatenating the returned tokens reproduces ``text``).
    """
    tokens: list[str] = []
    position = 0
    length = len(text)
    while position < length:
        matched: str | None = None
        max_span = min(_WORD_DICTIONARY_MAX_LENGTH, length - position)
        for span in range(max_span, 1, -1):
            candidate = text[position:position + span]
            if candidate in WORD_DICTIONARY:
                matched = candidate
                break
        if matched is None:
            matched = text[position]
        tokens.append(matched)
        position += len(matched)
    return tokens


def hotword_tokenizer(entries: list[dict[str, Any]]) -> Callable[[str], list[str]] | None:
    """Protect configured names, including a spoken Chinese brand and its model."""
    names = set()
    for entry in entries:
        for label in [entry["term"], *entry.get("aliases", [])]:
            names.add(normalize_text(label))
            names.update(re.findall(r"[\u4e00-\u9fff]{2,}", label))
    terms = sorted((name for name in names if len(name) >= 2), key=lambda name: (-len(name), name))
    if not terms:
        return None
    # Audio may contain just the Chinese part of a bilingual product name.
    # Keep a following Latin model with that name when it is spoken together.
    alternatives = [re.escape(term) + (r"(?:[a-z][a-z0-9]*)?" if re.search(r"[\u4e00-\u9fff]$", term) else "")
                    for term in terms]
    pattern = re.compile("|".join(alternatives), re.IGNORECASE)

    def tokenize(text: str) -> list[str]:
        tokens = []
        cursor = 0
        for match in pattern.finditer(text):
            tokens.extend(_default_word_tokenizer(text[cursor:match.start()]))
            tokens.append(match.group())
            cursor = match.end()
        tokens.extend(_default_word_tokenizer(text[cursor:]))
        return tokens

    return tokenize


def _word_boundary_positions(
    text: str,
    tokenizer: Callable[[str], list[str]] | None = None,
) -> frozenset[int]:
    """Character offsets (0..len(text)) that fall *between* words -- i.e.
    safe places to cut ``text`` without splitting a word in half.

    If ``tokenizer`` breaks its partition contract (returned tokens don't
    concatenate back to ``text``), this refuses to guess and reports only
    the two string ends as safe (so callers protect nothing rather than
    risk a wrong merge/cut).
    """
    tokenize = tokenizer or _default_word_tokenizer
    tokens = tokenize(text)
    if "".join(tokens) != text:
        return frozenset({0, len(text)})
    positions = {0}
    cursor = 0
    for token in tokens:
        cursor += len(token)
        positions.add(cursor)
    return frozenset(positions)


CLAUSE_STARTERS = (
    "也欢迎", "我们会", "但我", "但是", "而且", "其实", "所以", "不过", "然后", "另外", "同时", "因此",
    "你看", "开启", "自动", "除了",
)
INCOMPLETE_CLAUSE_SUFFIXES = ("我们", "你们", "他们", "我", "你", "他", "会", "要", "能", "的", "把", "让", "给", "跟")
NON_BREAK_RIGHT_PREFIXES = ("不了", "得了")
CROSS_MIC_ECHO_TEXT_SIMILARITY = 0.80
CROSS_MIC_ECHO_MINIMUM_CHARACTERS = 4
CROSS_MIC_ECHO_MAXIMUM_MEDIAN_OFFSET_FRAMES = 5
CROSS_MIC_ECHO_AMBIGUOUS_DB = 2.0
# "Late echo": a phrase already spoken on one track bleeds through a different
# mic a few seconds later (room reflection / neighbouring performer's mic
# picking up the tail of the line), instead of the near-simultaneous bleed the
# maximum-median-offset check above targets. This is the single knob for how
# far apart (in seconds) the two occurrences may be and still be treated as
# the same echo -- do not hardcode a frame count anywhere else.
CROSS_MIC_LATE_ECHO_MAX_GAP_SECONDS = 3.0
# A whole-window text match can coincidentally splice together two unrelated
# utterances that both happen to end/start with the same character (e.g. two
# different sentences that each contain a stray "是"). Forced-aligned
# characters within a single spoken phrase abut with ~0 gap; a real
# sentence/utterance boundary leaves a noticeably larger gap. Matched runs are
# trimmed to the longest stretch where both sides stay within this gap, so a
# late-echo match can never accidentally swallow a neighbouring, unrelated
# utterance on either track.
CROSS_MIC_LATE_ECHO_SAME_UTTERANCE_GAP_SECONDS = 0.1
# P3-问题6 (行尾显示延伸): trigger window G selected via parameter
# simulation over G in {round(0.5*fps), round(1.0*fps), round(1.5*fps)}
# against the 2026-07-13 bili_master fixture replay; per-row extension is
# additionally capped at round(0.27*fps) frames (capped variant simulated
# and selected by review) so a row nudges toward the next line's start
# without swallowing a skipped manual boundary. See commit message for the
# full table.
SUBTITLE_ROW_TAIL_EXTENSION_GAP_SECONDS = 1.0
SUBTITLE_ROW_TAIL_EXTENSION_MAX_SECONDS = 0.27
# P3-问题6续 (回收句尾字对齐时长溢出): forced alignment occasionally hands the
# last character (or last couple of characters) of a row an enormous span --
# swallowing the pause/silence that should have followed it and dragging the
# whole row's end_frame far past where the line actually finished. Real
# example (fps=29.97, "机器人合集" diagnostic replay): "一分我想爸爸了"
# 113065-113247 (182 frames) is built from per-character units 一(11) 分(5)
# 我(5) 想(7) 爸(8) 爸(63) 了(64) -- the trailing "爸""了" pair is a clear
# alignment overrun against the row's own 5-11 frame characters.
#
# Only the last OVERLONG_UNIT_TAIL_WINDOW characters of a row are ever
# eligible ("末字/近末字"): overlong characters earlier in the row (or at the
# very start of the row) don't push the row's displayed end_frame anywhere,
# so leaving them alone is free and keeps this pass from ever guessing at
# mid-row timing. A character only counts as overlong if its own duration
# exceeds *both* a multiple of the row's own median character duration *and*
# an absolute frame cap -- so a row where every character is uniformly a
# little slow (real unhurried delivery) never trips the multiplier test, and
# a genuinely drawn-out single-character row (e.g. "哇——") has nothing to
# compare itself against (rows need >= 2 characters to run at all) and is
# always left untouched. Reclaimed characters are given back the row's own
# median duration (capped at the same absolute frame cap) rather than being
# dropped to zero, so a real (if unusually long) character still gets a
# plausible amount of screen time.
OVERLONG_UNIT_TAIL_WINDOW = 2
OVERLONG_UNIT_MEDIAN_MULTIPLIER = 4.0
OVERLONG_UNIT_ABS_MAX_SECONDS = 0.6


class V4AlignmentError(RuntimeError):
    pass


def normalize_text(text: Any) -> str:
    return re.sub(r"[\s\-_—–，。！？、；：,.!?;:\"'“”‘’（）()《》【】\[\]{}<>…·/\\|]+", "", str(text or "").lower())


def _is_ascii_word_char(value: str) -> bool:
    return len(value) == 1 and value.isascii() and value.isalnum()


def annotate_original_word_boundaries(
    units: list[dict[str, Any]],
    raw_text: str,
) -> list[dict[str, Any]]:
    output = [dict(unit) for unit in units or []]
    for unit in output:
        unit.pop("space_before", None)
    expected = normalize_text(raw_text)
    actual = "".join(normalize_text(unit.get("text")) for unit in output)
    if not expected or actual != expected or len(output) != len(expected):
        return output

    boundaries: set[int] = set()
    normalized_index = 0
    previous_char = ""
    whitespace_pending = False
    for raw_char in str(raw_text or ""):
        normalized_piece = normalize_text(raw_char)
        if not normalized_piece:
            if raw_char.isspace():
                whitespace_pending = True
            continue
        for normalized_char in normalized_piece:
            if (
                whitespace_pending
                and _is_ascii_word_char(previous_char)
                and _is_ascii_word_char(normalized_char)
            ):
                boundaries.add(normalized_index)
            previous_char = normalized_char
            normalized_index += 1
            whitespace_pending = False

    for boundary in boundaries:
        output[boundary]["space_before"] = True
    return output


def propagate_original_word_boundaries(
    candidates: list[dict[str, Any]],
    selected: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    output = [dict(unit) for unit in selected or []]
    boundary_events: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for unit in candidates or []:
        if unit.get("space_before") is not True:
            continue
        boundary_events[int(unit.get("track_index") or 0)].append(
            (int(unit.get("start_frame") or 0), int(unit.get("end_frame") or 0))
        )
    for track_index, events in boundary_events.items():
        boundary_events[track_index] = sorted(set(events))

    previous_end_by_track: dict[int, int] = {}
    event_cursor_by_track: dict[int, int] = defaultdict(int)
    for unit in output:
        track_index = int(unit.get("track_index") or 0)
        current_start = int(unit.get("start_frame") or 0)
        lower_bound = previous_end_by_track.get(track_index, current_start)
        events = boundary_events.get(track_index, [])
        event_cursor = event_cursor_by_track[track_index]
        while event_cursor < len(events) and events[event_cursor][1] < lower_bound:
            event_cursor += 1
        crossed_boundary = False
        while event_cursor < len(events) and events[event_cursor][1] <= current_start:
            if events[event_cursor][0] >= lower_bound:
                crossed_boundary = True
            event_cursor += 1
        event_cursor_by_track[track_index] = event_cursor
        if crossed_boundary:
            unit["space_before"] = True
        previous_end_by_track[track_index] = int(unit.get("end_frame") or current_start)
    return output


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


def merge_audio_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Union of the half-open timeline intervals containing source samples."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def group_units_by_audio_span(units, spans):
    """Clip alignment tails at the cut and exclude timestamps in timeline gaps."""
    groups = [[] for _ in spans]
    ends = [end for _, end in spans]
    for unit in units:
        onset = int(unit["start_frame"])
        index = bisect_left(ends, onset + 1)
        if index < len(spans) and onset >= spans[index][0]:
            groups[index].append({**unit, "end_frame": min(int(unit["end_frame"]), ends[index])})
    return groups


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
    source_audio_tail_truncated_count = 0
    source_audio_tail_silence_seconds = 0.0
    for item, path, sample_count in valid:
        if item.get("timeline_end_frame") is None:
            continue
        expected_frames = int(item["timeline_end_frame"]) - int(item.get("timeline_start_frame") or 0)
        actual_frames = int(round(sample_count / sample_rate * fps))
        tolerance = max(2, int(math.ceil(max(1, expected_frames) * 0.01)))
        if expected_frames <= 0:
            raise ValueError(f"v4 时间线音频片段跨度无效: {path.name}")
        if actual_frames - expected_frames > tolerance:
            raise ValueError(f"v4 音频时长超过时间线跨度，暂不支持重定时片段: {path.name}")
        if expected_frames - actual_frames > tolerance:
            # Resolve can keep the timeline span after ffmpeg reaches source EOF; preserve that span with silence.
            source_audio_tail_truncated_count += 1
            source_audio_tail_silence_seconds += (expected_frames - actual_frames) / fps
    timeline_end = max(
        int(item["timeline_end_frame"])
        if item.get("timeline_end_frame") is not None
        else int(item.get("timeline_start_frame") or 0) + int(math.ceil(sample_count / sample_rate * fps))
        for item, _path, sample_count in valid
    )
    total_samples = max(1, int(math.ceil((timeline_end - timeline_start) / fps * sample_rate)))
    mixed = array("h", [0]) * total_samples
    overlap_sample_count = 0
    audio_spans = []
    for item, path, sample_count in valid:
        start = int(item.get("timeline_start_frame") or 0)
        end = start + math.ceil(sample_count / sample_rate * fps)
        if item.get("timeline_end_frame") is not None:
            end = min(end, int(item["timeline_end_frame"]))
        audio_spans.append((start, end))
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
        "audio_spans": merge_audio_spans(audio_spans),
        "source_audio_tail_truncated_count": source_audio_tail_truncated_count,
        "source_audio_tail_silence_seconds": source_audio_tail_silence_seconds,
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
        windows = [
            window for start, end in composite["audio_spans"]
            for window in build_context_windows(start, end, fps)
        ]
        prepared_windows: list[dict[str, Any]] = []
        for window_index, window in enumerate(windows, start=1):
            window["window_index"] = window_index
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
        snr_db = max(-20.0, min(60.0, 20.0 * math.log10(max(1.0, unit_level) / noise_floor)))
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
    is_doubao = str(payload.get("backend") or "") in {"doubao_asr", "doubao_asr_v2"}
    for segment in payload.get("segments") or []:
        segment_items: list[dict[str, Any]] = []
        for word in segment.get("words") or []:
            text = str(word.get("word") or word.get("text") or "")
            if text and word.get("start") is not None and word.get("end") is not None:
                segment_items.append(
                    {"text": text, "start": float(word["start"]), "end": float(word["end"])}
                )
        items.extend(segment_items)
        if is_doubao and not segment_items:
            text = str(segment.get("text") or "")
            if text and segment.get("start") is not None and segment.get("end") is not None:
                items.append(
                    {"text": text, "start": float(segment["start"]), "end": float(segment["end"])}
                )
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


def _timestamp_units_collapsed(units: list[dict[str, Any]]) -> bool:
    if len(units) < 8:
        return False
    start_frames = [int(unit.get("start_frame") or 0) for unit in units]
    end_frames = [int(unit.get("end_frame") or 0) for unit in units]
    aligned_span = max(end_frames) - min(start_frames)
    same_start_counts = Counter(start_frames)
    minimum_span = max(2, int(math.ceil(len(units) * 0.50)))
    maximum_same_start = max(4, int(math.ceil(len(units) * 0.25)))
    return aligned_span < minimum_span or max(same_start_counts.values()) > maximum_same_start


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
    text = str(payload.get("text") or "").strip()
    expected = normalize_text(text)
    retry_count = 0
    native_timestamp_fallback_count = 0
    if items:
        native_units = _expand_timestamp_items(items, fps, timeline_start_frame)
        native_text = "".join(unit["text"] for unit in native_units)
        native_coverage = difflib.SequenceMatcher(None, expected, native_text, autojunk=False).ratio() if expected else 1.0
        if not native_units or native_coverage < 0.90 or _timestamp_units_collapsed(native_units):
            items = []
            native_timestamp_fallback_count = 1
    if not items:
        if not expected:
            return [], {
                "forced_align_retry_count": 0,
                "aligned_unit_count": 0,
                "alignment_coverage": 1.0,
                "empty_silence_window": True,
            }
        retry_count = 1
        try:
            items = list(align_fn(Path(audio_path), text, language) or [])
        except Exception as exc:
            # The helper is loaded dynamically; preserve its resource stop marker
            # so context retries cannot turn a safety stop into a larger workload.
            if getattr(exc, "subfix_resource_stop", False):
                raise
            raise V4AlignmentError("v4 Forced Aligner 执行失败，已终止写回") from exc
    units = _expand_timestamp_items(items, fps, timeline_start_frame)
    actual = "".join(unit["text"] for unit in units)
    raw_coverage = difflib.SequenceMatcher(None, expected, actual, autojunk=False).ratio() if expected else 1.0
    if not units or raw_coverage < 0.90:
        raise V4AlignmentError("v4 无法获得可靠字词时间戳，已终止写回")
    units, repaired_count = repair_aligned_unit_text(expected, units)
    repaired_text = "".join(normalize_text(unit.get("text")) for unit in units)
    coverage = difflib.SequenceMatcher(None, expected, repaired_text, autojunk=False).ratio() if expected else 1.0
    if repaired_text != expected:
        raise V4AlignmentError("v4 Forced Aligner 文本守恒修复失败，已终止写回")
    if _timestamp_units_collapsed(units):
        start_frames = [int(unit.get("start_frame") or 0) for unit in units]
        end_frames = [int(unit.get("end_frame") or 0) for unit in units]
        aligned_span = max(end_frames) - min(start_frames)
        maximum_observed_same_start = max(Counter(start_frames).values())
        raise V4AlignmentError(
            "v4 Forced Aligner 时间戳坍缩: "
            f"字符={len(units)}, 跨度={aligned_span}帧, 同帧最多={maximum_observed_same_start}"
        )
    units = annotate_asr_punctuation(units, str(payload.get("text") or ""))
    units = annotate_original_word_boundaries(units, str(payload.get("text") or ""))
    return units, {
        "forced_align_retry_count": retry_count,
        "aligned_unit_count": len(units),
        "alignment_coverage": coverage,
        "raw_alignment_coverage": raw_coverage,
        "alignment_repaired_unit_count": repaired_count,
        "native_timestamp_fallback_count": native_timestamp_fallback_count,
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
    if local_index > 0 and windows[local_index - 1]["end_frame"] >= current["start_frame"]:
        previous = windows[local_index - 1]
        previous_text = str((payloads_by_window_index.get(int(previous.get("window_index") or 0)) or {}).get("text") or "")
        choices.append((_text_overlap_size(previous_text, current_text), 1, previous))
    if local_index + 1 < len(windows) and windows[local_index + 1]["start_frame"] <= current["end_frame"]:
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


def _cross_mic_echo_midpoint(unit: dict[str, Any]) -> float:
    start = int(unit.get("start_frame") or 0)
    end = max(start + 1, int(unit.get("end_frame") or start + 1))
    return (start + end) / 2.0


def restore_corroborated_short_flips(selected, candidates, fps):
    """Recover a displaced unit when two mics agree on it and both neighbors."""
    flips = [i for i in range(1, len(selected) - 1)
             if selected[i].get("speaker_decision") == "smoothed_short_flip"]
    if not flips:
        return selected
    runs = defaultdict(list)
    for u in candidates:
        runs[(u.get("track_index"), u.get("window_index"), u.get("candidate_kind"))].append(u)
    by_neighbors = defaultdict(list)
    tolerance = max(1, round(.15 * fps))
    for run in runs.values():
        run.sort(key=lambda u: (u["start_frame"], u["end_frame"]))
        for before, mid, after in zip(run, run[1:], run[2:]):
            if (0 <= mid["start_frame"] - before["end_frame"] <= tolerance
                    and 0 <= after["start_frame"] - mid["end_frame"] <= tolerance):
                by_neighbors[before["text"], after["text"]].append((before, mid, after))
    output = list(selected)
    for i in flips:
        left, displaced, right = selected[i-1:i+2]
        home = displaced["track_index"]
        if left["track_index"] != home or right["track_index"] != home:
            continue
        votes = defaultdict(set)
        local = {}
        for before, mid, after in by_neighbors[left["text"], right["text"]]:
            if (abs(before["start_frame"] - left["start_frame"]) > tolerance
                    or abs(after["start_frame"] - right["start_frame"]) > tolerance
                    or mid["end_frame"] <= displaced["start_frame"]
                    or mid["start_frame"] >= displaced["end_frame"]):
                continue
            votes[mid["text"]].add(mid["track_index"])
            if (mid["track_index"] == home and left["end_frame"] <= mid["start_frame"]
                    and mid["end_frame"] <= right["start_frame"]):
                local[mid["text"]] = mid
        if len(votes) == 1:
            word, tracks = next(iter(votes.items()))
            if len(tracks) >= 2 and word != displaced["text"] and word in local:
                output[i] = {**local[word], "speaker_decision": "cross_mic_phrase_agreement"}
    return output


def reconcile_short_speech_handoffs(
    selected: list[dict[str, Any]], candidates: list[dict[str, Any]], fps: float,
) -> list[dict[str, Any]]:
    """Compare short gaps and borrowed syllables with uninterrupted microphone runs."""
    runs = defaultdict(list)
    for u in candidates:
        runs[(u["track_index"], u.get("window_index"), u.get("candidate_kind"))].append(u)
    at_onset = defaultdict(list)
    for run in runs.values():
        run.sort(key=lambda u: (u["start_frame"], u["end_frame"]))
        for pos, u in enumerate(run):
            at_onset[u["start_frame"], u["text"]].append((run, pos))
    tolerance = max(1, round(.15 * fps))

    def anchored(u):
        for frame in range(u["start_frame"] - tolerance, u["start_frame"] + tolerance + 1):
            yield from at_onset.get((frame, u["text"]), ())

    def matches(u, other):
        return u["text"] == other["text"] and abs(u["start_frame"] - other["start_frame"]) <= tolerance

    def energy(u):
        return float(u.get("speaker_score_db") or 0)

    def split_vocalization(i, previous, first, second):
        # A single sustained hesitation can get a one-frame initial token
        # and a second sentence stop. Require a separate mic to transcribe
        # one sound between matching words before discarding a repetition.
        if (first["text"] != second["text"] or first["text"] not in {"嗯", "呃", "哦"}
                or first["track_index"] != second["track_index"]
                or not 0 < first["end_frame"] - first["start_frame"] <= max(1, round(.04 * fps))
                or not 0 <= second["start_frame"] - first["end_frame"] <= round(.04 * fps)
                or second["end_frame"] - first["start_frame"] > round(.22 * fps)
                or not all(u.get("asr_punctuation_strength") == 1 for u in (first, second))):
            return False
        following = selected[i + 1:i + 4]
        witnessed = False
        for run, pos in anchored(previous):
            if run[pos]["track_index"] == first["track_index"]:
                continue
            tail = run[pos + 1:pos + 5]
            if not tail or not matches(tail[0], first):
                continue
            if any(matches(u, second) for u in tail[1:]):
                return False
            if (energy(tail[0]) >= 6 and not tail[0].get("alignment_repaired")
                    and any(matches(u, v) for u in tail[1:] for v in following
                            if v["text"] not in SHORT_INTERJECTIONS
                            and v["start_frame"] - second["end_frame"] <= round(.5 * fps))):
                witnessed = True
        # A mic can start recording at the reaction and miss the preceding word.
        return witnessed and not any(
            run[pos]["track_index"] != first["track_index"] and pos + 1 < len(run)
            and matches(run[pos + 1], second) for run, pos in anchored(first)
        )

    def missing_tail(previous, left, current, following):
        gap = current["start_frame"] - left["end_frame"]
        count = (len(left["text"]) == 1 and left["text"] in "一二两三四五六七八九十0123456789"
                 and not left.get("asr_punctuation_strength"))
        reply = (current["text"] in ("对", "是", "嗯", "好") and left.get("asr_punctuation_strength") == 1
                 and bool(current.get("asr_punctuation_strength")))
        if not 0 <= gap <= round(.75 * fps) or not (count or reply):
            return []
        if reply and not any(run[pos]["track_index"] == current["track_index"]
                             and following is not None and pos + 1 < len(run)
                             and matches(run[pos + 1], following)
                             for run, pos in anchored(current)):
            return []
        alternatives = defaultdict(dict)
        for run, pos in anchored(left):
            if pos == 0 or not matches(run[pos - 1], previous) or run[pos].get("asr_punctuation_strength"):
                continue
            tail = run[pos + 1:pos + (2 if count else 3)]
            spelling = "".join(u["text"] for u in tail)
            if count and (spelling == current["text"] or spelling not in {
                "遍", "次", "个", "件", "条", "张", "回", "份", "天", "年"
            }):
                continue
            if reply:
                if spelling not in {"对吧", "是吧", "对吗", "是吗"} or run[pos]["track_index"] == current["track_index"]:
                    continue
                # A distant mic can hallucinate a tag in bleed. Require usable energy
                # on its own two syllables, comparable to its adjacent phrase ending.
                lead_db = min(energy(u) for u in run[pos - 1:pos + 1])
                if min(energy(u) for u in tail) < max(6, lead_db - 8):
                    continue
                distinct_onset = current["start_frame"] - tail[0]["end_frame"]
                if tail[0]["text"] == current["text"] and distinct_onset < max(1, round(.06 * fps)):
                    continue
            if (not tail or tail[-1].get("asr_punctuation_strength") != 1
                    or not left["start_frame"] < tail[0]["start_frame"] <= left["end_frame"] + tolerance
                    or tail[-1]["end_frame"] > current["start_frame"]
                    or any(not 0 <= b["start_frame"] - a["end_frame"] <= tolerance
                           for a, b in zip([run[pos], *tail], tail))):
                continue
            alternatives[spelling][run[pos]["track_index"]] = tail
        if len(alternatives) != 1:
            return []
        witnesses = next(iter(alternatives.values()))
        if count and (len(witnesses) < 2 or left["track_index"] not in witnesses):
            return []
        return witnesses.get(left["track_index"]) or max(witnesses.values(), key=lambda seq: min(map(energy, seq)))

    output = []
    for i, current in enumerate(selected):
        if output and i + 1 < len(selected) and current.get("speaker_decision") == "smoothed_short_flip":
            left, right = output[-1], selected[i + 1]
            # Did a quieter microphone splice one syllable into a continuous
            # phrase on the main mic? Keep genuine replies and interruptions.
            tracks = {run[pos]["track_index"] for run, pos in anchored(left)
                      if pos + 1 < len(run) and matches(run[pos + 1], right)}
            weak = min(energy(left), energy(right)) - energy(current)
            home = left["track_index"]
            repeated = (current["text"] in [u["text"] for u in output[-2:]]
                        and current.get("asr_punctuation_strength") == 1)
            gap = right["start_frame"] - left["end_frame"]
            # A weak microphone may transcribe a Latin word phonetically,
            # leaving its final Chinese syllable stranded after the Latin token.
            latin_bleed = (weak >= 20 and gap <= round(fps) and len(output) >= 2
                           and all(u["text"].isascii() and u["text"].isalpha() for u in output[-2:])
                           and any(pos > 0 and run[pos]["track_index"] != home
                                   and not run[pos - 1]["text"].isascii()
                                   and run[pos - 1]["start_frame"] < left["end_frame"]
                                   and run[pos - 1]["end_frame"] >= left["start_frame"]
                                   for run, pos in anchored(current)))
            if (home == right["track_index"] and home in tracks and weak >= 12
                    and current["text"] not in SHORT_INTERJECTIONS
                    and current["text"] not in {"不", "没", "有", "能", "会", "要", "行", "可"}
                    and (latin_bleed or (len(tracks) >= 2 and gap <= round(.4 * fps)))):
                if repeated:
                    # A speaker can restart the last word before the answer.
                    # Preserve their syllable and its final punctuation when
                    # the recording with that syllable also contains its lead-in.
                    if any(pos > 0 and matches(run[pos - 1], left)
                           and run[pos - 1].get("asr_punctuation_strength", 0) == 0
                           for run, pos in anchored(current)):
                        output[-1] = dict(left, asr_punctuation_strength=0)
                    output.append(current)
                continue

        if len(output) >= 2:
            left = output[-1]
            if split_vocalization(i, output[-2], left, current):
                output[-1] = dict(left, end_frame=current["end_frame"],
                                  speaker_decision="corroborated_single_vocalization")
                continue
            tail = missing_tail(output[-2], left, current, selected[i + 1] if i + 1 < len(selected) else None)
            if tail:
                output[-1] = dict(left, end_frame=min(left["end_frame"], tail[0]["start_frame"]),
                                  asr_punctuation_strength=0)
                output.extend(dict(u, track_index=left["track_index"], speaker_decision="aligned_phrase_tail")
                              for u in tail)
        output.append(current)
    return output


def _cross_mic_echo_offsets(
    left_units: list[dict[str, Any]],
    right_units: list[dict[str, Any]],
) -> list[int]:
    left_text = [normalize_text(unit.get("text")) for unit in left_units]
    right_text = [normalize_text(unit.get("text")) for unit in right_units]
    matcher = difflib.SequenceMatcher(None, left_text, right_text, autojunk=False)
    offsets: list[int] = []
    for left_start, right_start, size in matcher.get_matching_blocks():
        for offset in range(size):
            offsets.append(
                int(left_units[left_start + offset].get("start_frame") or 0)
                - int(right_units[right_start + offset].get("start_frame") or 0)
            )
    return offsets


def _cross_mic_echo_candidate(
    left_track: int,
    right_track: int,
    left_units: list[dict[str, Any]],
    right_units: list[dict[str, Any]],
    left_start: int,
    left_end: int,
    right_start: int,
    right_end: int,
    max_offset_frames: float = CROSS_MIC_ECHO_MAXIMUM_MEDIAN_OFFSET_FRAMES,
) -> dict[str, Any] | None:
    left_slice = left_units[left_start:left_end]
    right_slice = right_units[right_start:right_end]
    left_text = "".join(normalize_text(unit.get("text")) for unit in left_slice)
    right_text = "".join(normalize_text(unit.get("text")) for unit in right_slice)
    if min(len(left_text), len(right_text)) < CROSS_MIC_ECHO_MINIMUM_CHARACTERS:
        return None
    similarity = difflib.SequenceMatcher(None, left_text, right_text, autojunk=False).ratio()
    offsets = _cross_mic_echo_offsets(left_slice, right_slice)
    median_offset = statistics.median(abs(offset) for offset in offsets) if offsets else math.inf
    if (
        similarity < CROSS_MIC_ECHO_TEXT_SIMILARITY
        or median_offset > max_offset_frames
    ):
        return None
    return {
        "left_track": left_track,
        "right_track": right_track,
        "left_start_index": left_start,
        "left_end_index": left_end,
        "right_start_index": right_start,
        "right_end_index": right_end,
        "start_frame": min(
            int(left_slice[0].get("start_frame") or 0),
            int(right_slice[0].get("start_frame") or 0),
        ),
        "end_frame": max(
            int(left_slice[-1].get("end_frame") or 0),
            int(right_slice[-1].get("end_frame") or 0),
        ),
        "text_similarity": similarity,
        "median_offset_frames": median_offset,
        "left_candidate_indices": [int(unit["_echo_candidate_index"]) for unit in left_slice],
        "right_candidate_indices": [int(unit["_echo_candidate_index"]) for unit in right_slice],
    }


def _detect_cross_mic_echo_regions(
    candidates: list[dict[str, Any]],
    fps: float,
) -> list[dict[str, Any]]:
    tracks: dict[int, list[dict[str, Any]]] = {}
    ordered = sorted(
        [candidate for candidate in candidates if normalize_text(candidate.get("text"))],
        key=lambda row: (
            int(row.get("start_frame") or 0),
            int(row.get("end_frame") or 0),
            int(row.get("track_index") or 0),
        ),
    )
    for candidate in ordered:
        tracks.setdefault(int(candidate.get("track_index") or 0), []).append(candidate)
    track_indices = sorted(index for index in tracks if index > 0)
    window_frames = max(1, int(round(6.0 * fps)))
    step_frames = max(1, int(round(3.0 * fps)))
    join_gap_frames = max(1, int(round(0.4 * fps)))
    regions: list[dict[str, Any]] = []
    for left_position, left_track in enumerate(track_indices):
        for right_track in track_indices[left_position + 1:]:
            left_units = tracks[left_track]
            right_units = tracks[right_track]
            timeline_start = min(
                int(left_units[0].get("start_frame") or 0),
                int(right_units[0].get("start_frame") or 0),
            )
            timeline_end = max(
                int(left_units[-1].get("end_frame") or 0),
                int(right_units[-1].get("end_frame") or 0),
            )
            for window_start in range(timeline_start, timeline_end + 1, step_frames):
                window_end = window_start + window_frames
                left_indices = [
                    index for index, unit in enumerate(left_units)
                    if window_start <= _cross_mic_echo_midpoint(unit) < window_end
                ]
                right_indices = [
                    index for index, unit in enumerate(right_units)
                    if window_start <= _cross_mic_echo_midpoint(unit) < window_end
                ]
                if (
                    len(left_indices) < CROSS_MIC_ECHO_MINIMUM_CHARACTERS
                    or len(right_indices) < CROSS_MIC_ECHO_MINIMUM_CHARACTERS
                ):
                    continue
                left_start, left_end = left_indices[0], left_indices[-1] + 1
                right_start, right_end = right_indices[0], right_indices[-1] + 1
                left_text = [normalize_text(unit.get("text")) for unit in left_units[left_start:left_end]]
                right_text = [normalize_text(unit.get("text")) for unit in right_units[right_start:right_end]]
                matcher = difflib.SequenceMatcher(None, left_text, right_text, autojunk=False)
                for block in matcher.get_matching_blocks():
                    if block.size < CROSS_MIC_ECHO_MINIMUM_CHARACTERS:
                        continue
                    candidate = _cross_mic_echo_candidate(
                        left_track,
                        right_track,
                        left_units,
                        right_units,
                        left_start + block.a,
                        left_start + block.a + block.size,
                        right_start + block.b,
                        right_start + block.b + block.size,
                    )
                    if candidate:
                        regions.append(candidate)

    regions.sort(
        key=lambda region: (
            region["left_track"],
            region["right_track"],
            region["start_frame"],
            region["end_frame"],
        )
    )
    merged: list[dict[str, Any]] = []
    for region in regions:
        if not merged:
            merged.append(region)
            continue
        previous = merged[-1]
        joinable = (
            previous["left_track"] == region["left_track"]
            and previous["right_track"] == region["right_track"]
            and region["start_frame"] - previous["end_frame"] <= join_gap_frames
            and region["left_start_index"] <= previous["left_end_index"] + 2
            and region["right_start_index"] <= previous["right_end_index"] + 2
            and max(previous["end_frame"], region["end_frame"])
            - min(previous["start_frame"], region["start_frame"])
            <= window_frames
        )
        if not joinable:
            merged.append(region)
            continue
        combined = _cross_mic_echo_candidate(
            int(region["left_track"]),
            int(region["right_track"]),
            tracks[int(region["left_track"])],
            tracks[int(region["right_track"])],
            min(int(previous["left_start_index"]), int(region["left_start_index"])),
            max(int(previous["left_end_index"]), int(region["left_end_index"])),
            min(int(previous["right_start_index"]), int(region["right_start_index"])),
            max(int(previous["right_end_index"]), int(region["right_end_index"])),
        )
        if combined:
            merged[-1] = combined
        elif not (
            region["left_start_index"] >= previous["left_start_index"]
            and region["left_end_index"] <= previous["left_end_index"]
            and region["right_start_index"] >= previous["right_start_index"]
            and region["right_end_index"] <= previous["right_end_index"]
        ):
            merged.append(region)
    return merged


def _detect_late_cross_mic_echo_regions(
    candidates: list[dict[str, Any]],
    fps: float,
) -> list[dict[str, Any]]:
    """Delayed-echo counterpart to _detect_cross_mic_echo_regions.

    The simultaneous detector above only accepts matches whose matched
    characters land within CROSS_MIC_ECHO_MAXIMUM_MEDIAN_OFFSET_FRAMES of
    each other -- i.e. genuinely simultaneous mic bleed. A different failure
    mode: a phrase already spoken in full on one track gets picked up faintly
    on another mic (room reflection / a neighbouring performer's mic) a few
    seconds later, after the first track has already finished the line. That
    repeat can itself be immediately followed, on the *same* track and with no
    gap, by unrelated real speech -- so detection must operate at the
    matched-character-run granularity (like the simultaneous detector) rather
    than flagging a whole clause, or the trailing real content would be
    deleted along with the echo.

    Acceptance is gated on the wall-clock boundary gap between the two
    occurrences (later occurrence's first frame minus the earlier
    occurrence's last frame), which must be strictly positive (the earlier
    line has already finished -- this is not overlapping simultaneous bleed,
    that is the other detector's territory) and at most
    CROSS_MIC_LATE_ECHO_MAX_GAP_SECONDS worth of frames. Each returned region
    records which side occurred first as "earlier_side" ("left" or "right")
    so callers only ever consider deleting the later, redundant repeat.
    """
    tracks: dict[int, list[dict[str, Any]]] = {}
    ordered = sorted(
        [candidate for candidate in candidates if normalize_text(candidate.get("text"))],
        key=lambda row: (
            int(row.get("start_frame") or 0),
            int(row.get("end_frame") or 0),
            int(row.get("track_index") or 0),
        ),
    )
    for candidate in ordered:
        tracks.setdefault(int(candidate.get("track_index") or 0), []).append(candidate)
    track_indices = sorted(index for index in tracks if index > 0)
    late_gap_frames = max(1, int(round(CROSS_MIC_LATE_ECHO_MAX_GAP_SECONDS * fps)))
    window_frames = max(1, int(round(6.0 * fps)), late_gap_frames * 2)
    step_frames = max(1, int(round(3.0 * fps)))
    same_utterance_gap_frames = max(1, int(round(CROSS_MIC_LATE_ECHO_SAME_UTTERANCE_GAP_SECONDS * fps)))
    join_gap_frames = same_utterance_gap_frames

    def _boundary_gap(
        left_slice: list[dict[str, Any]],
        right_slice: list[dict[str, Any]],
    ) -> tuple[int, str] | None:
        left_first_start = int(left_slice[0].get("start_frame") or 0)
        left_last_end = int(left_slice[-1].get("end_frame") or 0)
        right_first_start = int(right_slice[0].get("start_frame") or 0)
        right_last_end = int(right_slice[-1].get("end_frame") or 0)
        if left_first_start <= right_first_start:
            return right_first_start - left_last_end, "left"
        return left_first_start - right_last_end, "right"

    def _longest_same_utterance_run(
        left_slice: list[dict[str, Any]],
        right_slice: list[dict[str, Any]],
    ) -> tuple[int, int]:
        """Trim a difflib matching block down to its longest internally
        contiguous (same-utterance) run on *both* sides.

        `left_slice`/`right_slice` are the two, index-aligned halves of a
        matching block (same length; left_slice[i] is matched to
        right_slice[i]). Characters that were force-aligned within one
        spoken phrase abut with ~0 gap; a real utterance/sentence boundary
        leaves a much larger gap. A coincidental match can splice an
        unrelated later utterance onto the tail of the true echo (e.g. an
        unrelated "是" a few frames after the echo ends, matched against the
        leading "是" of trailing real content) -- splitting wherever *either*
        side's consecutive gap exceeds the tight same-utterance threshold and
        keeping only the longest surviving run discards that contamination
        without needing the far side to also show a gap.
        """
        size = len(left_slice)
        if size <= 1:
            return 0, size
        best_start, best_end = 0, 1
        run_start = 0
        for index in range(1, size):
            left_gap = int(left_slice[index].get("start_frame") or 0) - int(
                left_slice[index - 1].get("end_frame") or 0
            )
            right_gap = int(right_slice[index].get("start_frame") or 0) - int(
                right_slice[index - 1].get("end_frame") or 0
            )
            if left_gap > same_utterance_gap_frames or right_gap > same_utterance_gap_frames:
                if (index - run_start) > (best_end - best_start):
                    best_start, best_end = run_start, index
                run_start = index
        if (size - run_start) > (best_end - best_start):
            best_start, best_end = run_start, size
        return best_start, best_end

    def _extend_matched_run(
        left_units: list[dict[str, Any]],
        right_units: list[dict[str, Any]],
        left_start: int,
        left_end: int,
        right_start: int,
        right_end: int,
    ) -> tuple[int, int, int, int]:
        """Grow a matched block outward, one character at a time, over the
        *full* per-track unit lists (not the window-restricted slice used to
        find it).

        The sliding window used to bound the difflib comparison can clip the
        true start/end of a genuine echo run purely because a character's
        midpoint lands a few frames on the wrong side of an arbitrary window
        boundary (e.g. the window ends one step short of the echo's tail, or
        starts one step after the echo's head). Since the run is already
        known-genuine at this point, extending it past the window edge as
        long as the next character still matches and still abuts with no
        real utterance gap recovers those clipped characters without
        widening what counts as a match in the first place.
        """
        while (
            left_start > 0
            and right_start > 0
            and normalize_text(left_units[left_start - 1].get("text"))
            == normalize_text(right_units[right_start - 1].get("text"))
            and int(left_units[left_start].get("start_frame") or 0)
            - int(left_units[left_start - 1].get("end_frame") or 0)
            <= same_utterance_gap_frames
            and int(right_units[right_start].get("start_frame") or 0)
            - int(right_units[right_start - 1].get("end_frame") or 0)
            <= same_utterance_gap_frames
        ):
            left_start -= 1
            right_start -= 1
        while (
            left_end < len(left_units)
            and right_end < len(right_units)
            and normalize_text(left_units[left_end].get("text"))
            == normalize_text(right_units[right_end].get("text"))
            and int(left_units[left_end].get("start_frame") or 0)
            - int(left_units[left_end - 1].get("end_frame") or 0)
            <= same_utterance_gap_frames
            and int(right_units[right_end].get("start_frame") or 0)
            - int(right_units[right_end - 1].get("end_frame") or 0)
            <= same_utterance_gap_frames
        ):
            left_end += 1
            right_end += 1
        return left_start, left_end, right_start, right_end

    regions: list[dict[str, Any]] = []
    for left_position, left_track in enumerate(track_indices):
        for right_track in track_indices[left_position + 1:]:
            left_units = tracks[left_track]
            right_units = tracks[right_track]
            timeline_start = min(
                int(left_units[0].get("start_frame") or 0),
                int(right_units[0].get("start_frame") or 0),
            )
            timeline_end = max(
                int(left_units[-1].get("end_frame") or 0),
                int(right_units[-1].get("end_frame") or 0),
            )
            for window_start in range(timeline_start, timeline_end + 1, step_frames):
                window_end = window_start + window_frames
                left_indices = [
                    index for index, unit in enumerate(left_units)
                    if window_start <= _cross_mic_echo_midpoint(unit) < window_end
                ]
                right_indices = [
                    index for index, unit in enumerate(right_units)
                    if window_start <= _cross_mic_echo_midpoint(unit) < window_end
                ]
                if (
                    len(left_indices) < CROSS_MIC_ECHO_MINIMUM_CHARACTERS
                    or len(right_indices) < CROSS_MIC_ECHO_MINIMUM_CHARACTERS
                ):
                    continue
                left_start, left_end = left_indices[0], left_indices[-1] + 1
                right_start, right_end = right_indices[0], right_indices[-1] + 1
                left_text = [normalize_text(unit.get("text")) for unit in left_units[left_start:left_end]]
                right_text = [normalize_text(unit.get("text")) for unit in right_units[right_start:right_end]]
                matcher = difflib.SequenceMatcher(None, left_text, right_text, autojunk=False)
                for block in matcher.get_matching_blocks():
                    if block.size < CROSS_MIC_ECHO_MINIMUM_CHARACTERS:
                        continue
                    block_left_start = left_start + block.a
                    block_left_end = block_left_start + block.size
                    block_right_start = right_start + block.b
                    block_right_end = block_right_start + block.size
                    trim_start, trim_end = _longest_same_utterance_run(
                        left_units[block_left_start:block_left_end],
                        right_units[block_right_start:block_right_end],
                    )
                    if trim_end - trim_start < CROSS_MIC_ECHO_MINIMUM_CHARACTERS:
                        continue
                    block_left_end = block_left_start + trim_end
                    block_left_start = block_left_start + trim_start
                    block_right_end = block_right_start + trim_end
                    block_right_start = block_right_start + trim_start
                    block_left_start, block_left_end, block_right_start, block_right_end = _extend_matched_run(
                        left_units,
                        right_units,
                        block_left_start,
                        block_left_end,
                        block_right_start,
                        block_right_end,
                    )
                    gap_result = _boundary_gap(
                        left_units[block_left_start:block_left_end],
                        right_units[block_right_start:block_right_end],
                    )
                    if gap_result is None:
                        continue
                    gap_frames, earlier_side = gap_result
                    if not (0 < gap_frames <= late_gap_frames):
                        continue
                    candidate = _cross_mic_echo_candidate(
                        left_track,
                        right_track,
                        left_units,
                        right_units,
                        block_left_start,
                        block_left_end,
                        block_right_start,
                        block_right_end,
                        max_offset_frames=math.inf,
                    )
                    if candidate:
                        candidate["earlier_side"] = earlier_side
                        regions.append(candidate)

    regions.sort(
        key=lambda region: (
            region["left_track"],
            region["right_track"],
            region["start_frame"],
            region["end_frame"],
        )
    )
    merged: list[dict[str, Any]] = []
    for region in regions:
        if not merged:
            merged.append(region)
            continue
        previous = merged[-1]
        joinable = (
            previous["left_track"] == region["left_track"]
            and previous["right_track"] == region["right_track"]
            and previous["earlier_side"] == region["earlier_side"]
            and region["start_frame"] - previous["end_frame"] <= join_gap_frames
            and region["left_start_index"] <= previous["left_end_index"] + 2
            and region["right_start_index"] <= previous["right_end_index"] + 2
            and max(previous["end_frame"], region["end_frame"])
            - min(previous["start_frame"], region["start_frame"])
            <= window_frames
        )
        if not joinable:
            merged.append(region)
            continue
        combined = _cross_mic_echo_candidate(
            int(region["left_track"]),
            int(region["right_track"]),
            tracks[int(region["left_track"])],
            tracks[int(region["right_track"])],
            min(int(previous["left_start_index"]), int(region["left_start_index"])),
            max(int(previous["left_end_index"]), int(region["left_end_index"])),
            min(int(previous["right_start_index"]), int(region["right_start_index"])),
            max(int(previous["right_end_index"]), int(region["right_end_index"])),
            max_offset_frames=math.inf,
        )
        if combined:
            combined["earlier_side"] = region["earlier_side"]
            merged[-1] = combined
        elif not (
            region["left_start_index"] >= previous["left_start_index"]
            and region["left_end_index"] <= previous["left_end_index"]
            and region["right_start_index"] >= previous["right_start_index"]
            and region["right_end_index"] <= previous["right_end_index"]
        ):
            merged.append(region)
    return merged


def _suppress_late_cross_mic_echo_regions(
    candidates: list[dict[str, Any]],
    fps: float,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    indexed = [dict(candidate, _echo_candidate_index=index) for index, candidate in enumerate(candidates or [])]
    regions = _detect_late_cross_mic_echo_regions(indexed, fps)
    suppressed_indices: set[int] = set()
    selected_indices: set[int] = set()
    suppressed_regions = 0
    ambiguous_regions = 0
    for region in regions:
        left_indices = [int(index) for index in region["left_candidate_indices"]]
        right_indices = [int(index) for index in region["right_candidate_indices"]]
        earlier_indices, later_indices = (
            (left_indices, right_indices) if region["earlier_side"] == "left" else (right_indices, left_indices)
        )
        earlier_units = [indexed[index] for index in earlier_indices]
        later_units = [indexed[index] for index in later_indices]
        earlier_score = statistics.median(float(unit.get("speaker_score_db") or 0.0) for unit in earlier_units)
        later_score = statistics.median(float(unit.get("speaker_score_db") or 0.0) for unit in later_units)
        if abs(earlier_score - later_score) < CROSS_MIC_ECHO_AMBIGUOUS_DB:
            # Never guess on a late echo: if the two occurrences are not
            # clearly separated in level, leave both untouched rather than
            # risk deleting real speech.
            ambiguous_regions += 1
            continue
        if later_score >= earlier_score:
            # The later, matched occurrence is not the fainter bleed-through
            # -- do not delete it on an unclear signal.
            continue
        newly_suppressed = [index for index in later_indices if index not in selected_indices]
        if not newly_suppressed:
            continue
        suppressed_indices.update(newly_suppressed)
        selected_indices.update(earlier_indices)
        suppressed_regions += 1
        for index in earlier_indices:
            indexed[index]["speaker_decision"] = "late_cross_mic_echo_selected"

    filtered: list[dict[str, Any]] = []
    for index, candidate in enumerate(indexed):
        if index in suppressed_indices:
            continue
        output = dict(candidate)
        output.pop("_echo_candidate_index", None)
        filtered.append(output)
    return filtered, {
        "late_cross_mic_echo_region_count": len(regions),
        "late_cross_mic_echo_suppressed_count": suppressed_regions,
        "late_cross_mic_echo_ambiguous_count": ambiguous_regions,
        "late_cross_mic_echo_suppressed_unit_count": len(suppressed_indices),
    }


def suppress_cross_mic_echo_regions(
    candidates: list[dict[str, Any]],
    fps: float,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    indexed = [dict(candidate, _echo_candidate_index=index) for index, candidate in enumerate(candidates or [])]
    regions = _detect_cross_mic_echo_regions(indexed, fps)
    suppressed_indices: set[int] = set()
    selected_indices: set[int] = set()
    suppressed_regions = 0
    ambiguous_regions = 0
    for region in regions:
        left_indices = [int(index) for index in region["left_candidate_indices"]]
        right_indices = [int(index) for index in region["right_candidate_indices"]]
        left_units = [indexed[index] for index in left_indices]
        right_units = [indexed[index] for index in right_indices]
        left_score = statistics.median(float(unit.get("speaker_score_db") or 0.0) for unit in left_units)
        right_score = statistics.median(float(unit.get("speaker_score_db") or 0.0) for unit in right_units)
        if abs(left_score - right_score) < CROSS_MIC_ECHO_AMBIGUOUS_DB:
            ambiguous_regions += 1
            left_start = min(int(unit.get("start_frame") or 0) for unit in left_units)
            right_start = min(int(unit.get("start_frame") or 0) for unit in right_units)
            left_wins = (left_start, int(region["left_track"])) <= (right_start, int(region["right_track"]))
        else:
            left_wins = left_score > right_score
        winner_indices, loser_indices = (
            (left_indices, right_indices) if left_wins else (right_indices, left_indices)
        )
        newly_suppressed = [index for index in loser_indices if index not in selected_indices]
        if not newly_suppressed:
            continue
        suppressed_indices.update(newly_suppressed)
        selected_indices.update(winner_indices)
        suppressed_regions += 1
        for index in winner_indices:
            indexed[index]["speaker_decision"] = "cross_mic_echo_selected"

    stage1_filtered: list[dict[str, Any]] = []
    for index, candidate in enumerate(indexed):
        if index in suppressed_indices:
            continue
        output = dict(candidate)
        output.pop("_echo_candidate_index", None)
        stage1_filtered.append(output)

    late_filtered, late_diagnostic = _suppress_late_cross_mic_echo_regions(stage1_filtered, fps)

    diagnostic = {
        "cross_mic_echo_region_count": len(regions),
        "cross_mic_echo_suppressed_count": suppressed_regions,
        "cross_mic_echo_ambiguous_count": ambiguous_regions,
        "cross_mic_echo_suppressed_unit_count": len(suppressed_indices),
    }
    diagnostic.update(late_diagnostic)
    return late_filtered, diagnostic


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


def suppress_near_duplicate_units(
    units: list[dict[str, Any]],
    fps: float,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    ordered = sorted(
        [dict(unit) for unit in units or []],
        key=lambda row: (int(row.get("start_frame") or 0), int(row.get("end_frame") or 0)),
    )
    maximum_gap_frames = max(1, int(round(1.2 * fps)))
    output: list[dict[str, Any]] = []
    suppressed = 0

    for unit in ordered:
        start = int(unit.get("start_frame") or 0)
        end = max(start + 1, int(unit.get("end_frame") or start + 1))
        text = normalize_text(unit.get("text"))
        previous = output[-1] if output else None
        duration = end - start
        decision = str(unit.get("speaker_decision") or "")
        short_candidate = duration <= 3 or (
            decision == "smoothed_short_flip"
            and duration <= max(3, int(round(0.3 * fps)))
        )
        if previous is not None and 1 <= len(text) <= 2 and short_candidate:
            previous_end = int(previous.get("end_frame") or 0)
            previous_text = normalize_text(previous.get("text"))
            gap = start - previous_end
            changed_track = int(previous.get("track_index") or 0) != int(unit.get("track_index") or 0)
            low_score_punctuated_tail = (
                duration <= 3
                and float(unit.get("asr_punctuation_strength") or 0.0) >= 1.0
                and float(unit.get("source_score") or 0.0) + 4.0
                <= float(previous.get("source_score") or 0.0)
            )
            # Short duration alone would erase valid word-internal repeats such as "拼多多".
            has_ghost_evidence = (
                changed_track
                or gap > 0
                or decision == "smoothed_short_flip"
                or low_score_punctuated_tail
            )
            # Two separately aligned syllables in the same recording can be a
            # complete laugh after a sentence. A short gap is normal here.
            recorded_laugh = (
                text == previous_text == "哈"
                and not changed_track and unit.get("track_index")
                and unit.get("window_index") == previous.get("window_index")
                and all(item.get("candidate_kind") == "primary" and not item.get("alignment_repaired")
                        for item in (previous, unit))
                and 0 <= gap <= round(.12 * fps)
                and len(output) >= 2 and output[-2].get("asr_punctuation_strength", 0) >= 1
                and not previous.get("asr_punctuation_strength")
                and unit.get("asr_punctuation_strength", 0) >= 1
                and not low_score_punctuated_tail and decision != "smoothed_short_flip"
            )
            if (
                0 <= gap <= maximum_gap_frames
                and previous_text.endswith(text)
                and has_ghost_evidence
                and not recorded_laugh
            ):
                suppressed += 1
                continue
        output.append(unit)

    return output, {"near_duplicate_suppressed_count": suppressed}


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


def _boundary_model_context(
    units: list[dict[str, Any]],
    position: int,
) -> tuple[str, int]:
    context = normalize_text("".join(str(unit.get("text") or "") for unit in units))
    character_position = len(
        normalize_text("".join(str(unit.get("text") or "") for unit in units[:position]))
    )
    return context, character_position


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
    right_context = normalize_text("".join(str(unit.get("text") or "") for unit in units[position:]))
    if any(right_context.startswith(starter) for starter in CLAUSE_STARTERS):
        score += 5.0
    if any(right_context.startswith(prefix) for prefix in NON_BREAK_RIGHT_PREFIXES):
        score -= 20.0
    if profile and profile.get("boundary_model"):
        model = profile["boundary_model"]
        context, character_position = _boundary_model_context(units, position)
        probability = boundary_probability(context, character_position, model)
        threshold = max(0.05, min(0.95, float(model.get("decision_threshold") or 0.5)))
        if probability >= threshold:
            model_margin = (probability - threshold) / max(1e-6, 1.0 - threshold)
        else:
            model_margin = (probability - threshold) / max(1e-6, threshold)
        score += model_margin * 4.0
    if normalize_text(str(left.get("text") or "") + str(right.get("text") or "")) in PROTECTED_BIGRAMS:
        score -= 20.0
    return score


def _has_non_model_boundary_evidence(
    units: list[dict[str, Any]],
    position: int,
    fps: float,
    profile: dict[str, Any] | None = None,
) -> bool:
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
    if any(right_context.startswith(starter) for starter in CLAUSE_STARTERS):
        return True
    if profile and profile.get("boundary_model"):
        model = profile["boundary_model"]
        context, character_position = _boundary_model_context(units, position)
        probability = boundary_probability(context, character_position, model)
        threshold = max(0.05, min(0.95, float(model.get("decision_threshold") or 0.5)))
        margin_value = profile.get("model_boundary_evidence_margin", 0.15)
        if margin_value is None:
            margin_value = 0.15
        margin = max(0.0, min(1.0, float(margin_value)))
        return probability >= threshold + margin
    return False


def _fill_short_segment_gaps(rows: list[dict[str, Any]], fps: float) -> int:
    maximum_gap = max(1, int(round(0.40 * fps)))
    filled = 0
    for left, right in zip(rows, rows[1:]):
        gap = int(right.get("start_frame") or 0) - int(left.get("end_frame") or 0)
        if 0 < gap <= maximum_gap:
            left["end_frame"] = int(right.get("start_frame") or left.get("end_frame") or 0)
            filled += 1
    return filled


def _merge_same_frame_segment_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A single subtitle track cannot display separate rows starting in one frame."""
    output: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        if (output
                and int(output[-1].get("start_frame") or 0) == int(row.get("start_frame") or 0)
                and output[-1].get("speaker_track_index") == row.get("speaker_track_index")):
            previous = output[-1]
            previous["text"] = str(previous.get("text") or "") + str(row.get("text") or "")
            previous["end_frame"] = max(int(previous["end_frame"]), int(row["end_frame"]))
            previous["independent_vad"] = bool(previous.get("independent_vad") or row.get("independent_vad"))
            previous["segmentation_decision"] = "merged_same_frame_boundary"
        else:
            output.append(row)
    return output


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
    accepted = _merge_same_frame_segment_rows(accepted)
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


def _segmentation_length_parameters(
    mode: str,
    profile: dict[str, Any] | None,
    max_chars: int | None = None,
) -> tuple[int, int, int, int]:
    fallback_minimum, fallback_preferred_maximum, fallback_maximum = (
        (6, 14, 18) if mode == "live" else (8, 16, 18)
    )
    length_model = profile.get("length_model") if isinstance(profile, dict) else None
    length_model = length_model if isinstance(length_model, dict) else {}
    minimum = max(1, int(length_model.get("minimum") or fallback_minimum))
    median = max(minimum, int(length_model.get("median") or 11))
    preferred_maximum = max(median, int(length_model.get("preferred_maximum") or fallback_preferred_maximum))
    maximum = max(preferred_maximum, int(length_model.get("maximum") or fallback_maximum))
    if max_chars is not None:
        # User-facing "subtitle length" override (standard <=25 / short-video
        # <=10): replaces the length_model-derived caps with values scaled
        # from the requested per-row character ceiling. Only takes effect
        # when a caller explicitly passes --max-chars; otherwise the
        # length_model/profile-derived defaults above are untouched.
        maximum = int(max_chars)
        preferred_maximum = max(1, round(maximum * 0.8))
        minimum = max(1, round(maximum * 0.4))
        median = max(minimum, min(median, preferred_maximum))
        preferred_maximum = max(median, preferred_maximum)
        maximum = max(preferred_maximum, maximum)
    return minimum, median, preferred_maximum, maximum


def _editorial_pause_boundary(units: list[dict[str, Any]], fps: float) -> int | None:
    """Give long clauses a readable break before text-model scoring."""
    lengths = [len(normalize_text(unit.get("text"))) for unit in units]
    total = sum(lengths)
    if total < 18 or len(units) < 2:
        return None
    gaps = [
        max(0, int(right.get("start_frame") or 0) - int(left.get("end_frame") or 0))
        for left, right in zip(units, units[1:])
    ]
    # Ignore uniform alignment spacing; a short pause needs local prominence.
    minimum_gap = max(math.ceil(0.08 * fps), statistics.median(gaps) + math.ceil(0.06 * fps))
    word_bounds = _unit_word_boundary_positions(units)
    text = "".join(normalize_text(unit.get("text")) for unit in units)
    reporting = re.match(r"^(?:那|其实|当时)?(?:我|你|他|她)们?(?:本来|原本|一直|还)?"
                         r"(?:以为|觉得|发现)(?=(?:我|你|他|她)们?.{7})", text)
    candidates: list[tuple[float, int, int, int]] = []
    offset = 0
    for position, gap in enumerate(gaps, start=1):
        offset += lengths[position - 1]
        if offset < 4 or total - offset < 4 or offset not in word_bounds:
            continue
        punctuation = float(units[position - 1].get("asr_punctuation_strength") or 0.0)
        # A reported thought has a natural break before its embedded subject.
        # Rank it below a real comma but above a sub-word alignment gap.
        if reporting and offset == reporting.end():
            candidates.append((0.25, gap, -abs(total - 2 * offset), position))
        if punctuation <= 0.0 and gap < minimum_gap:
            continue
        # Micro-pauses help long rows; do not recursively fragment a readable
        # remainder after its stronger clause boundary has already been used.
        if punctuation <= 0.0 and total <= 20 and gap < math.ceil(0.25 * fps):
            continue
        candidates.append((punctuation, gap, -abs(total - 2 * offset), position))
    return max(candidates)[-1] if candidates else None


def _leading_temporal_clause_boundary(units: list[dict[str, Any]]) -> int | None:
    """Recognize a bounded temporal introduction followed by a new subject."""
    text = "".join(normalize_text(unit.get("text")) for unit in units)
    if len(text) < 18:
        return None
    match = re.match(
        r"^(?:在|到了?|直到|从)[\u4e00-\u9fff0-9]{0,12}?"
        r"(?:第[一二三四五六七八九十百两0-9]+(?:天|周|个月|年)|[之以][前后]|的时候)",
        text,
    )
    if match is None or match.end() < 4:
        return None
    remainder = text[match.end():]
    # Do not detach a time expression from its modifiers or a date range.
    if len(remainder) < 6 or remainder.startswith((
        "的", "里", "内", "外", "前", "后", "到", "至", "早上", "上午",
        "中午", "下午", "晚上", "凌晨", "结束", "开始", "期间",
    )):
        return None
    if not re.match(r"^[\u4e00-\u9fff]{2,6}?(?:就|便|已经|开始|正在)", remainder):
        return None
    offset = 0
    for position, unit in enumerate(units, start=1):
        offset += len(normalize_text(unit.get("text")))
        if offset == match.end():
            return position
        if offset > match.end():
            return None
    return None


def _copular_clause_boundary(units: list[dict[str, Any]]) -> int | None:
    """Keep a long subject separate from its complete explanatory predicate."""
    text = "".join(normalize_text(unit.get("text")) for unit in units)
    if len(text) < 18 or text.startswith((
        "如果", "只要", "只有", "即使", "就算", "哪怕", "无论", "不管",
    )):
        return None
    offsets: dict[int, int] = {}
    offset = 0
    for position, unit in enumerate(units, start=1):
        offset += len(normalize_text(unit.get("text")))
        offsets[offset] = position
    for match in re.finditer(r"也?(?:就是|正是|才是|便是)", text):
        subject = text[:match.start()]
        predicate = text[match.start():]
        # Avoid short fragments, unfinished modifiers and negated/embedded
        # copulas. An indivisible aligned unit never gets a fabricated time.
        if (len(subject) < 6 or len(predicate) < 6
                or subject.endswith(("的", "地", "得", "不", "也", "把", "被", "让", "给", "跟", "和", "与", "或"))):
            continue
        position = offsets.get(match.start())
        if position is not None:
            return position
    return None


def segment_canonical_units(
    units: list[dict[str, Any]],
    mode: str,
    profile: dict[str, Any] | None,
    fps: float,
    max_chars: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    max_chars_applied: int | str = max_chars if max_chars is not None else "default"
    ordered = sorted([dict(unit) for unit in units or []], key=lambda row: (int(row.get("start_frame") or 0), int(row.get("end_frame") or 0)))
    if not ordered:
        return [], {
            "ordinary_single_character_count": 0,
            "text_conservation_failed_count": 0,
            "max_chars_applied": max_chars_applied,
        }
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
    # Editorial clause boundaries must survive the below-cap fast path and
    # short-row penalties, using the same real unit times as acoustic breaks.
    first_region = ordered[:hard_positions[0]] if hard_positions else ordered
    editorial_boundary = _leading_temporal_clause_boundary(first_region)
    if editorial_boundary is None:
        editorial_boundary = _copular_clause_boundary(first_region)
    if editorial_boundary is None:
        editorial_boundary = _editorial_pause_boundary(first_region, fps)
    if editorial_boundary is not None:
        hard_positions = sorted({*hard_positions, editorial_boundary})
    if hard_positions:
        rows: list[dict[str, Any]] = []
        start = 0
        ordinary_suppressed = 0
        short_reply_suppressed = 0
        incomplete_merged = 0
        short_gap_filled = 0
        for end in [*hard_positions, len(ordered)]:
            group_rows, group_diagnostic = segment_canonical_units(ordered[start:end], mode, profile, fps, max_chars=max_chars)
            rows.extend(group_rows)
            ordinary_suppressed += int(group_diagnostic.get("ordinary_single_character_suppressed_count") or 0)
            short_reply_suppressed += int(group_diagnostic.get("short_reply_suppressed_count") or 0)
            incomplete_merged += int(group_diagnostic.get("incomplete_clause_merged_count") or 0)
            short_gap_filled += int(group_diagnostic.get("short_gap_filled_count") or 0)
            start = end
        rows, orphan_merged = _merge_orphan_segment_rows(rows, fps)
        rows = _merge_same_frame_segment_rows(rows)
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
            "max_chars_applied": max_chars_applied,
        }
    mode_profile = ((profile or {}).get("modes") or {}).get(mode) if profile and isinstance(profile.get("modes"), dict) else profile
    preferred_min, preferred_center, preferred_max, hard_max = _segmentation_length_parameters(
        mode,
        mode_profile,
        max_chars=max_chars,
    )
    count = len(ordered)
    # After separating the subject, a compact explanation should not be
    # fragmented again by alignment gaps. Hard breaks were handled above;
    # punctuation and a new clause still permit further segmentation.
    compact_predicate = (
        len(expected_text) <= min(16, hard_max)
        and re.match(r"^也?(?:就是|正是|才是|便是)", expected_text) is not None
        and not any(float(unit.get("asr_punctuation_strength") or 0.0) > 0.0
                    or _is_break_punctuation(str(unit.get("text") or ""))
                    for unit in ordered[:-1])
        and not any(starter in expected_text[2:] for starter in CLAUSE_STARTERS)
    )
    if count <= hard_max and (compact_predicate or not any(
        _has_non_model_boundary_evidence(ordered, position, fps, mode_profile)
        for position in range(1, count)
    )):
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
        finalized_diagnostic["max_chars_applied"] = max_chars_applied
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
                # A model prediction alone cannot justify a short fragment;
                # require independent evidence at its internal boundaries.
                if start > 0 and not _has_non_model_boundary_evidence(ordered, start, fps):
                    continue
                if end < count and not _has_non_model_boundary_evidence(ordered, end, fps):
                    continue
                strong_short_clause = True
            length_score = -abs(length - preferred_center) * 0.25
            if preferred_min <= length <= preferred_max:
                length_score += 1.0
            if strong_short_clause:
                length_score += 2.5
            # Acoustic/punctuation evidence may add an editorial boundary.
            # A text-model-only boundary pays a higher cost so hash/model
            # rewards cannot create unnecessary fragments.
            split_penalty = 0.0
            if end < count:
                split_penalty = 2.5 if _has_non_model_boundary_evidence(ordered, end, fps, mode_profile) else 16.0
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
    finalized_diagnostic["max_chars_applied"] = max_chars_applied
    return finalized_rows, finalized_diagnostic


def reclaim_overlong_unit_tails(
    rows: list[dict[str, Any]],
    units: list[dict[str, Any]],
    fps: float,
) -> tuple[list[dict[str, Any]], int]:
    """Reclaim a row's end_frame when its last unit(s) hit an alignment overrun.

    ``rows`` is the output of :func:`segment_canonical_units` (each row a
    contiguous slice of ``units`` by character count); ``units`` is the same
    per-character canonical unit stream that was fed into segmentation (each
    with its own ``start_frame``/``end_frame``). Rows and units are both
    re-sorted here for safety, but the pairing between a row and its
    constituent units relies on the text-conservation invariant that
    segmentation already guarantees: walking the (sorted) units in order and
    consuming exactly ``len(row["text"])`` characters per row reproduces the
    same row/unit grouping segmentation itself used, with no need to trust
    frame ranges for the association.

    Only ``end_frame`` is ever mutated; text, row count, and start_frame are
    untouched, so this cannot violate text conservation or shift any other
    row's boundaries.

    Returns (rows, overlong_tail_reclaimed_count).
    """
    ordered_units = sorted(
        [dict(unit) for unit in units or []],
        key=lambda unit: (int(unit.get("start_frame") or 0), int(unit.get("end_frame") or 0)),
    )
    output = [dict(row) for row in rows or []]
    abs_cap_frames = max(1, int(round(OVERLONG_UNIT_ABS_MAX_SECONDS * fps)))
    cursor = 0
    reclaimed_count = 0
    for row in output:
        target_length = len(str(row.get("text") or ""))
        row_units: list[dict[str, Any]] = []
        consumed = 0
        while consumed < target_length and cursor < len(ordered_units):
            candidate_unit = ordered_units[cursor]
            row_units.append(candidate_unit)
            consumed += len(str(candidate_unit.get("text") or "")) or 1
            cursor += 1
        if len(row_units) < 2:
            continue
        durations = [
            max(0, int(candidate_unit.get("end_frame") or 0) - int(candidate_unit.get("start_frame") or 0))
            for candidate_unit in row_units
        ]
        median_duration = statistics.median(durations)
        threshold = max(median_duration * OVERLONG_UNIT_MEDIAN_MULTIPLIER, abs_cap_frames)
        window_start = max(0, len(row_units) - OVERLONG_UNIT_TAIL_WINDOW)
        flagged_start = len(row_units)
        index = len(row_units) - 1
        while index >= window_start and durations[index] > threshold:
            flagged_start = index
            index -= 1
        if flagged_start >= len(row_units):
            continue
        per_unit_cap_frames = max(1, min(int(round(median_duration)), abs_cap_frames))
        flagged_count = len(row_units) - flagged_start
        first_flagged_unit = row_units[flagged_start]
        new_end_frame = int(first_flagged_unit.get("start_frame") or 0) + per_unit_cap_frames * flagged_count
        original_start = int(row.get("start_frame") or 0)
        original_end = int(row.get("end_frame") or 0)
        new_end_frame = max(original_start + 1, min(new_end_frame, original_end))
        if new_end_frame < original_end:
            row["end_frame"] = new_end_frame
            reclaimed_count += 1
    return output, reclaimed_count


_LINKING_PREFIXES = ("但是", "而且", "所以", "不过", "因为", "然后")
_SUBJECT_PREFIXES = ("我们", "你们", "他们", "她们", "我", "你", "他", "她")


def _continuation_prefix(left: str, right: str, same_track: bool) -> str:
    if same_track and re.fullmatch(r"在[\u4e00-\u9fff]{1,4}(?:上|中|里|方面)", left):
        return left if right.startswith(("我", "你", "他", "她", "它")) else ""
    if left in ("这也", "那也") and right.startswith(("太", "很", "真")):
        return left
    for subject in _SUBJECT_PREFIXES + ("它们", "它"):
        if left.endswith("但" + subject):
            return "但" + subject
    for linker in _LINKING_PREFIXES:
        if left.endswith(linker[0]) and right.startswith(linker[1:]):
            return linker[0]
    for prefix in _LINKING_PREFIXES + _SUBJECT_PREFIXES:
        if left.endswith(prefix) and (left != prefix or prefix in _LINKING_PREFIXES):
            return prefix
    return ""


def _aligned_phrase_witnesses(candidates, fps):
    # Keep competing microphones and ASR windows separate. A witness must
    # actually contain the clause opening and its continuation in one take.
    runs = defaultdict(list)
    for u in candidates:
        runs[(u.get("track_index"), u.get("window_index"), u.get("candidate_kind"))].append(u)
    by_onset = defaultdict(list)
    for run in runs.values():
        run.sort(key=lambda u: (u["start_frame"], u["end_frame"]))
        for i, u in enumerate(run):
            by_onset[(u["start_frame"], u["text"])].append((run, i))

    tolerance = max(1, round(0.35 * fps))

    def corroborated(span, seam_index, *, other_than_track=None, required_track=None, witness_gap_limit=None,
                     seam_punctuation_limit=0):
        first = span[0]
        for frame in range(first["start_frame"] - tolerance, first["start_frame"] + tolerance + 1):
            for run, start in by_onset.get((frame, first["text"]), ()):
                sample = run[start:start + len(span)]
                if len(sample) != len(span) or sample[0].get("track_index") == other_than_track:
                    continue
                if required_track is not None and sample[0].get("track_index") != required_track:
                    continue
                if witness_gap_limit is not None:
                    pause = sample[seam_index]["start_frame"] - sample[seam_index - 1]["end_frame"]
                    if not 0 <= pause <= witness_gap_limit:
                        continue
                if (all(a["text"] == b["text"] and abs(a["start_frame"] - b["start_frame"]) <= tolerance
                        for a, b in zip(sample, span))
                        and all((u.get("asr_punctuation_strength") or 0)
                                <= (seam_punctuation_limit if n == seam_index - 1 else 0)
                                for n, u in enumerate(sample[:-1]))):
                    return True
        return False

    return corroborated


def repair_clause_continuations(
    rows: list[dict[str, Any]],
    units: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    fps: float,
    max_chars: int,
) -> tuple[list[dict[str, Any]], int]:
    """Reconsider short clause openings at row boundaries, using aligned speech."""
    ordered = sorted(units, key=lambda u: (u["start_frame"], u["end_frame"]))
    if len(rows) < 2 or "".join(u["text"] for u in ordered) != "".join(r["text"] for r in rows):
        return rows, 0

    corroborated = _aligned_phrase_witnesses(candidates, fps)

    # Map character offsets to real unit boundaries. Never interpolate the
    # start time of a prefix that is embedded inside one alignment unit.
    at_offset = {0: 0}
    offset = 0
    for i, u in enumerate(ordered, 1):
        offset += len(u["text"])
        at_offset[offset] = i

    output = []
    offset = 0
    repairs = 0
    for original in rows:
        left = output[-1] if output else None
        right = dict(original)
        output.append(right)
        seam_offset = offset
        offset += len(right["text"])
        if left is None:
            continue
        same_track = left["speaker_track_index"] == right["speaker_track_index"]
        prefix = _continuation_prefix(left["text"], right["text"], same_track)
        seam = at_offset.get(seam_offset)
        if not prefix and seam is not None and right["text"] in ("是一个", "是其中一个"):
            # A short subject after the last comma can be stranded on the
            # previous microphone. Require a real clause start and the same
            # timing/witness checks as for other cross-track continuations.
            for length in range(2, min(4, len(left["text"]) - 1) + 1):
                subject_start = at_offset.get(seam_offset - length)
                if subject_start and ordered[subject_start - 1].get("asr_punctuation_strength"):
                    suffix = left["text"][-length:]
                    if re.fullmatch(r"[\u4e00-\u9fff]+", suffix):
                        prefix = suffix
                    break
        if not prefix or len(prefix) + len(right["text"]) > max_chars:
            continue
        start = at_offset.get(seam_offset - len(prefix))
        end = at_offset.get(offset)
        if start is None or seam is None or end is None or not start < seam < end:
            continue
        whole_left = left["text"] == prefix
        if any(u.get("asr_punctuation_strength") for u in ordered[start:seam - 1]):
            continue
        if not whole_left and not ordered[start - 1].get("asr_punctuation_strength"):
            continue
        gap = ordered[seam]["start_frame"] - ordered[seam - 1]["end_frame"]
        if gap < 0:
            continue
        punctuation = float(ordered[seam - 1].get("asr_punctuation_strength") or 0)
        direct_join = (same_track and gap <= round(0.20 * fps)
                       and punctuation < 1 and (whole_left or not punctuation))
        if not direct_join:
            linker = prefix in _LINKING_PREFIXES
            if same_track and not linker:
                continue
            # Microphones can align the same hesitation a few frames apart.
            # Allow a small margin on the selected timestamps, but the witness
            # itself must hear a continuous conjunction within one second.
            if gap > round((1.20 if linker else 0.45) * fps) or end - seam < 2:
                continue
            if not corroborated(
                ordered[start:min(end, seam + 3)], seam - start,
                other_than_track=right["speaker_track_index"] if same_track else None,
                witness_gap_limit=round(fps) if linker else None,
            ):
                continue

        new_start = left["start_frame"] if whole_left else ordered[start]["start_frame"]
        if new_start >= right["end_frame"]:
            continue
        if whole_left:
            output.pop(-2)
        else:
            new_end = min(new_start, ordered[start - 1]["end_frame"], left["end_frame"])
            if new_end <= left["start_frame"]:
                continue
            left["text"] = left["text"][:-len(prefix)]
            left["end_frame"] = new_end
        right["start_frame"] = new_start
        right["text"] = prefix + right["text"]
        right["segmentation_decision"] = "clause_continuation"
        repairs += 1
    for i, row in enumerate(output, 1):
        row["index"] = i
    return output, repairs


# The literal separator marks the candidate cue break. Match the grammar
# on both sides without changing any ASR characters.
_DEPENDENT_PHRASE_SEAMS = tuple(re.compile(pattern) for pattern in (
    r"\|的(?:流程|方式|结果|东西)",
    r"(?:什么样|怎样|怎么样)\|的",
    r"(?:这样|那样|这么|那么)一个\|",
    r"(?:做|交|留|送|递)给\|[你我他她]",
    r"(?:把|将)[你我他她它]们?\|的[\u4e00-\u9fff]",
    r"(?:会|要|能)在\|",
    r"(?:彻底|完全|真正|认真)[地的]\|",
    r"(?:为|替|跟)[你我他她]们?\|",
    r"会\|(?:想|觉得|选择)",
    r"(?:太|特别|非常)\|.{1,5}$",
    r"哪.{1,6}是\|.+[呀呢吗]$",
    r"(?:问|提)(?:出)?一个\|(?:就|让|大家|所有人).+的问题",
    r"(?:不想|不愿|打算)\|去",
    r"(?:这|那)几\|[种件套名位项类]",
    r"^(?:我|你|他|她)(?:们)?(?:相信|觉得|认为)\|(?:我|你|他|她|它|这|那)",
    r"(?:因为|由于)(?:觉得|认为)\|(?:这|那)[样么]",
))

_DURATION = r"[一二三四五六七八九十两半\d]+(?:个(?:月|小时)|分钟|小时|天|周|年)"


def _hesitant_complement(left: str, right: str) -> bool:
    """A same-speaker hesitation inside a quantified answer or verb/object."""
    return bool(
        (re.search(r"(?:比较|更|最)[快慢长短久]的$", left)
         and re.fullmatch(_DURATION + r"(?:都|也)(?:有|要|行|可以)[呢啊了]?", right))
        or (re.search(r"(?:没有|缺少)任何(?:尊重|珍惜|欣赏|爱护|重视)$", left)
            and re.fullmatch(r"[这那](?:个|份|件|种).{1,6}的(?:感觉|意思|意识)", right))
    )


def _internal_clause_break(left: str, right: str, previous: str) -> bool:
    # The same name can be the object of one clause and the next subject.
    for size in range(2, min(4, len(left) - 1, len(right) - 1) + 1):
        subject = left[-size:]
        if (re.fullmatch(r"[\u4e00-\u9fff]+", subject)
                and right.startswith(subject) and right[size:].startswith(("就", "也", "会", "再", "负责"))):
            return True
    count = re.search(r"[一二三四五六七八九十两\d]+([遍次轮])$", left)
    if count and right.startswith(("就是", "比如", "也就是")) and count[1] in right:
        return True
    return bool(previous == left and re.fullmatch(_DURATION, left)
                and re.search(r"(?:比较|更|最)[快慢长短久]的$", right))


def _split_punctuated_clauses(rows, ordered, offsets, word_bounds):
    """Split an internal clause only at a punctuated, aligned word boundary."""
    output = []
    cursor = 0
    for row in rows:
        text = row["text"]
        start = cursor
        cursor += len(text)
        previous = output[-1]["text"] if output else ""
        for pos in range(2, len(text) - 1):
            edge = offsets.get(start + pos)
            if edge is None or start + pos not in word_bounds:
                continue
            before, after = ordered[edge - 1], ordered[edge]
            if (not before.get("asr_punctuation_strength")
                    or before["track_index"] != after["track_index"]
                    or not _internal_clause_break(text[:pos], text[pos:], previous)):
                continue
            onset = after["start_frame"]
            end = min(onset, before["end_frame"])
            if row["start_frame"] < end <= onset < row["end_frame"]:
                output.extend((
                    {**row, "text": text[:pos], "end_frame": end, "segmentation_decision": "phrase_reflow"},
                    {**row, "text": text[pos:], "start_frame": onset, "segmentation_decision": "phrase_reflow"},
                ))
                break
        else:
            output.append(row)
    return output


def _carried_hesitation_onset(left, right, ordered, starts, fps, max_chars) -> int | None:
    """Find the aligned onset of a hesitation introducing the next topic."""
    following = right["text"]
    if (not left["text"].endswith("嗯") or "\n" in left["text"]
            or not re.fullmatch(r"对[\u4e00-\u9fff]{1,12}(?:这|那)方面", following)
            or len(following) + 2 > max_chars or left["end_frame"] != right["start_frame"]):
        return None
    cut = bisect_left(starts, right["start_frame"])
    if cut < 2 or cut >= len(ordered):
        return None
    before, filler, topic = ordered[cut - 2:cut + 1]
    track = left.get("speaker_track_index")
    if (
        track and track == right.get("speaker_track_index")
        and all(u.get("track_index") == track and u.get("candidate_kind") == "primary"
                and u.get("speaker_decision") in (None, "viterbi_exclusive")
                and not u.get("alignment_repaired") for u in (before, filler, topic))
        and filler.get("window_index") is not None
        and before.get("window_index") == filler["window_index"] == topic.get("window_index")
        and before["start_frame"] >= left["start_frame"]
        and left["text"].endswith(before["text"] + "嗯") and filler["text"] == "嗯"
        and topic["text"] == "对" and not topic.get("asr_punctuation_strength")
        and all(0 < (u.get("asr_punctuation_strength") or 0) < 1 for u in (before, filler))
        and before["end_frame"] <= filler["start_frame"] < filler["end_frame"] <= topic["start_frame"]
        and topic["start_frame"] == right["start_frame"]
        and filler["start_frame"] - left["start_frame"] >= round(1.5 * fps)
        and topic["start_frame"] - filler["start_frame"] <= round(.25 * fps)
    ):
        return filler["start_frame"]
    return None


def _split_unfinished_connector(left, response, ordered, starts, cuts, fps):
    """Reveal a trailing 然后 at its onset when a silence and lone filler follow."""
    lead = re.fullmatch(r"([^\n]+)，然后(?:……)?", left["text"])
    if not lead or response["text"] not in ("嗯", "呃"):
        return None
    edge = bisect_left(starts, response["start_frame"])
    if not 3 <= edge < len(ordered):
        return None
    before, ran, hou, filler = ordered[edge - 3:edge + 1]
    onset = ran["start_frame"]
    end = max(left["end_frame"], onset + math.ceil(.8 * fps))
    track = left.get("speaker_track_index")
    if (not track or response.get("speaker_track_index") != track
            or any(u.get("track_index") != track or u.get("alignment_repaired")
                   or u.get("candidate_kind") == "local_retry" for u in (before, ran, hou, filler))
            or (ran["text"], hou["text"], filler["text"]) != ("然", "后", response["text"])
            or not lead[1].endswith(before["text"]) or before["start_frame"] < left["start_frame"]
            or not .5 <= (before.get("asr_punctuation_strength") or 0) < 1
            or (hou.get("asr_punctuation_strength") or 0) < 1
            or (filler.get("asr_punctuation_strength") or 0) < 1
            or not before["end_frame"] <= onset < ran["end_frame"] <= hou["start_frame"] < hou["end_frame"] <= left["end_frame"]
            or onset - left["start_frame"] < math.ceil(.5 * fps)
            or filler["start_frame"] != response["start_frame"]
            or filler["start_frame"] - hou["end_frame"] < round(.8 * fps)
            or end > filler["start_frame"] or any(f in cuts for f in range(onset + 1, end))):
        return None
    return [
        {**left, "text": lead[1], "end_frame": onset, "original_end_frame": before["end_frame"],
         "segmentation_decision": "delayed_ending"},
        {**left, "text": "然后", "start_frame": onset, "end_frame": end, "original_start_frame": onset,
         "original_end_frame": hou["end_frame"], "segmentation_decision": "delayed_ending"},
    ]


def split_delayed_endings(rows, units, fps, picture_cuts, max_chars=25):
    """Keep delayed answers, topics and independent reactions close to their onset."""
    ordered = sorted(units, key=lambda unit: unit["start_frame"])
    starts = [unit["start_frame"] for unit in ordered]
    cuts = set(picture_cuts)
    floor = max(1, math.ceil(fps / 2))
    output = []
    for row in rows:
        if output:
            previous = output[-1]
            connector = _split_unfinished_connector(previous, row, ordered, starts, cuts, fps)
            if connector:
                output[-1:] = connector
            onset = _carried_hesitation_onset(previous, row, ordered, starts, fps, max_chars)
            if onset is not None:
                output[-1] = {**previous, "text": previous["text"][:-1], "end_frame": onset,
                              "timing_decision": "spoken_leadin_handoff"}
                # This same-speaker filler leads its topic by at most a quarter
                # second. Preserve its onset without creating a standalone flash.
                row = {**row, "text": "嗯 " + row["text"], "start_frame": onset,
                       "timing_decision": "spoken_leadin_handoff", "segmentation_decision": "delayed_ending"}
        text = row["text"]
        laugh = re.search(r"(?<=[^哈])(哈{2,})$", text)
        reveal = re.search(r"的(?:部分|环节|主题|领域)([\u4e00-\u9fff]{2,4})$", text)
        answer = re.search(r"(?:还是|就是|答案是|结果是)(还没有|还没定|不知道|没想好)$", text)
        ending = laugh or reveal or answer
        if not ending or "\n" in text:
            output.append(row)
            continue
        suffix = ending[1]
        split_pos = ending.start(1)
        start, end = row["start_frame"], row["end_frame"]
        # Work backwards from the actual final words. Display normalization or
        # a line borrowed from another speaker must not create a false match.
        last = bisect_left(starts, end)
        first = last
        spoken = ""
        while first and starts[first - 1] >= start and len(spoken) < len(suffix):
            first -= 1
            spoken = normalize_text(ordered[first]["text"]) + spoken
        if not first or first == last or spoken != suffix:
            output.append(row)
            continue
        before, after = ordered[first - 1], ordered[first]
        onset = after["start_frame"]
        track = after.get("track_index")
        punct = before.get("asr_punctuation_strength", 0)
        timing_matches = (
            start <= before["start_frame"] and before["end_frame"] <= onset
            and text[:split_pos].endswith(normalize_text(before["text"]))
            and track and all(unit.get("track_index") == track for unit in ordered[first - 1:last])
            and min(onset - start, end - onset) >= floor
        )
        if reveal:
            near_cut = any(frame in cuts for frame in range(onset - round(.1 * fps), onset + 1))
            separate = punct >= .5 and onset - before["end_frame"] >= round(.2 * fps) and near_cut
        elif answer:
            # Wait for an elicited answer when delivery includes a marked hesitation.
            prompted = output and re.search(r"哪|什么|谁|多少|[吗呢？?]$", output[-1]["text"])
            held = any(v["start_frame"] - u["end_frame"] >= round(.3 * fps)
                       for u, v in zip(ordered[first - 1:last - 1], ordered[first:last]))
            separate = (prompted and 0 <= start - output[-1]["end_frame"] <= round(.5 * fps)
                        and held and onset - start >= round(fps)
                        and ordered[last - 1].get("asr_punctuation_strength", 0) >= 1)
        else:
            separate = punct >= 1 and ordered[last - 1].get("asr_punctuation_strength", 0) >= 1
        if timing_matches and separate:
            output.extend((
                {**row, "text": text[:split_pos], "end_frame": onset,
                 "original_end_frame": before["end_frame"], "segmentation_decision": "delayed_ending"},
                {**row, "text": suffix, "start_frame": onset, "original_start_frame": onset,
                 "speaker_track_index": track, "segmentation_decision": "delayed_ending"},
            ))
        else:
            output.append(row)
    return output


def display_comma_clauses(text: str) -> list[str]:
    # Drop grouping separators in 1,234,567; commas outside these groups
    # delimit clauses. Spaces inside each clause carry pauses and brand names.
    ungrouped = re.sub(r"(?<![0-9])[0-9]{1,3}(?:[,，][0-9]{3})+(?![0-9])",
                       lambda m: re.sub(r"[,，]", "", m[0]), text)
    return [part.strip() for part in re.split(r"[,，]", ungrouped) if part.strip()]


def _display_word_frames(
    text: str, spoken: list[dict[str, Any]],
) -> list[tuple[int, int, int | None] | None]:
    """Map displayed characters to aligned source frames, allowing spelling and number normalization."""
    raw, spans = [], []
    for word in spoken:
        chars = normalize_text(word["text"])
        start, end = word["start_frame"], word["end_frame"]
        for k, char in enumerate(chars):
            raw.append(char)
            spans.append((start + round(k * (end - start) / len(chars)),
                          start + round((k + 1) * (end - start) / len(chars)), word.get("track_index")))
    source = "".join(raw)
    if source == text:
        return spans
    mapped = [None] * len(text)
    matcher = difflib.SequenceMatcher(None, text, source, autojunk=False)
    for tag, i, j, k, l in matcher.get_opcodes():
        if tag == "equal":
            mapped[i:j] = spans[k:l]
        elif tag == "replace" and (i or j < len(text)) and l > k:
            # A replaced name or numeral between matching words begins at the
            # beginning of the corresponding spoken span.
            mapped[i] = spans[k]
            if j - i > 1:
                mapped[j - 1] = spans[l - 1]
    return mapped


def split_display_commas(rows: list[dict[str, Any]], units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turn each displayed clause into a cue, using its first word's actual onset."""
    ordered = sorted(units, key=lambda u: u["start_frame"])
    onsets = [u["start_frame"] for u in ordered]
    output = []
    for row in rows:
        text = row["text"]
        if "," not in text and "，" not in text:
            output.append(row)
            continue
        parts = display_comma_clauses(text)
        start, end = row["start_frame"], row["end_frame"]
        while len(parts) > max(1, end - start):
            # Each cue needs at least one frame. Keep brief repetitions together
            # first when the source row has fewer frames than phrases.
            k = min(range(len(parts) - 1), key=lambda n: (
                parts[n] != parts[n + 1] and parts[n] not in SHORT_INTERJECTIONS,
                len(parts[n]) + len(parts[n + 1])))
            parts[k:k + 2] = [" ".join(parts[k:k + 2])]
        if len(parts) < 2:
            if parts:
                output.append({**row, "text": parts[0]})
            continue
        first = bisect_left(onsets, min(start, row.get("original_start_frame", start)))
        stop = bisect_left(onsets, max(end, row.get("original_end_frame", end)))
        normalized = [normalize_text(part) or "\ufffd" for part in parts]
        mapped = _display_word_frames("".join(normalized), ordered[first:stop])
        offsets = [0, *itertools.accumulate(map(len, normalized))]
        frames = [start] + [None] * (len(parts) - 1) + [end]
        # Interpolate only unaligned boundaries, between the nearest known
        # onsets. Reserve one frame per remaining part even on poor alignments.
        previous = 0
        for n in range(1, len(frames)):
            if n < len(parts):
                span = mapped[offsets[n]]
                if not span:
                    continue
                frames[n] = max(frames[previous] + n - previous,
                                min(end - (len(parts) - n), span[0]))
            for k in range(previous + 1, n):
                fraction = (offsets[k] - offsets[previous]) / (offsets[n] - offsets[previous])
                value = frames[previous] + round((frames[n] - frames[previous]) * fraction)
                frames[k] = max(frames[k - 1] + 1, min(frames[n] - (n - k), value))
            previous = n
        for n, part in enumerate(parts):
            head, tail = mapped[offsets[n]], mapped[offsets[n + 1] - 1]
            child = {**row, "text": part, "start_frame": frames[n], "end_frame": frames[n + 1],
                     "original_start_frame": head[0] if head else frames[n],
                     "original_end_frame": tail[1] if tail else frames[n + 1],
                     "segmentation_decision": "display_comma_split"}
            if n:
                child["timing_decision"] = ("aligned_comma_handoff" if head and frames[n] == head[0]
                                            else "estimated_comma_handoff")
            if head and head[2]:
                child["speaker_track_index"] = head[2]
            output.append(child)
    return output


def _quantity_seam(left: str, right: str) -> bool:
    return bool(re.search(r"(?:有|要|剩|差)[个约近]$", left)
                and re.match(r"[一二三四五六七八九十百千万两0-9]+", right))


_SPOKEN_RESTARTS = (
    (re.compile(r"(?:以为|觉得|认为)[^，。！？…\n]+是\Z"), re.compile(r"就是"), "，"),
    (re.compile(r"(?:就会|已经|本来|原来)已\Z"), re.compile(r"[他她它我你](?:们)?已经"), "……"),
)


def punctuate_spoken_restarts(text: str) -> str:
    """Show a false start without removing spoken words or creating another cue."""
    for ending, opening, separator in _SPOKEN_RESTARTS:
        text = opening.sub(lambda m: (separator if ending.search(text, 0, m.start()) else "") + m[0], text)
    return text


def _resumed_clause(left: str, right: str) -> bool:
    # Preserve the speaker's stutter or false start within the clause it introduces.
    return bool(
        any(ending.search(left) and opening.match(right) for ending, opening, _ in _SPOKEN_RESTARTS)
        or (re.fullmatch(r"(?:就|那个)[嗯呃]+", left) and right.startswith(("然后", "接着", "其实")))
        or (re.fullmatch(r"(?:因为|由于)[这那](?:一)?[条件个部次款]", left)
            and re.match(r"(?:它|这|那)(?:其实|本来|已经|也|还|是)", right))
        or (re.fullmatch(r"(?:就是|然后|那就|我们就|咱们就)先", left)
            and re.match(r"(?:得到|拿到|找到|做好|准备|确定|尝试|记录|把|将|打开|完成|处理)", right))
        or (re.fullmatch(r"(?:但是|不过|可是|但)(?:当时|那时(?:候)?|当初|那会儿)", left)
            and re.match(r"(?:其实|确实|实际上|毕竟)(?:[我你他她它]|大家)", right))
    )


def _phrase_cut(left: str, right: str, commas: set[int], stop: bool, max_chars: int) -> int | None:
    """Select a better unit boundary in adjacent cues. Zero joins the pair."""
    seam = len(left)
    if re.match(r"^[吧吗呢](?:对|是的|没错)", right) and seam + 1 in commas:
        return seam + 1
    if _quantity_seam(left, right):
        return 0
    if left.endswith("工作") and re.fullmatch(r"(?:步骤|流程|方式)[呢吗呀]?", right):
        return 0
    if stop:
        # A false ASR period after a stranded subject needs another mic
        # hearing the same uninterrupted phrase (checked by the caller).
        if len(left) <= 4 and left.endswith(("我", "你", "他", "她")) and len(left) > 1:
            return 0
        return None
    # A movement or action is part of the temporal modifier that precedes it.
    if (re.search(r"(?:能够|可以|需要)在.{1,8}(?:之后|之前|以后|以前)$", left)
            and right.startswith(("重新", "再次", "继续", "直接"))):
        conditional = re.search(r"(?:如果|只要|假如)(?:把|将)", left)
        return conditional.start() if conditional else 0
    # Coordinate lists and comparisons have stronger outer boundaries than
    # the commas between their items.
    if seam in commas:
        obj = left.rfind("把")
        if obj >= 3 and seam - obj <= 6 and any(seam < p <= seam + 5 for p in commas):
            return obj
        change = re.match(r".{1,5}?(?=变成|变为|成为)", right)
        if change and re.search(r"从.{1,5}$", left):
            return seam + change.end()

    # Move a complete modifier, example, or attribution to the clause it opens.
    inside = sorted(p for p in commas if 2 <= p < seam)
    if inside:
        last_break = inside[-1]
        tail = left[last_break:]
        if tail in ("来", "走") and seam in commas and right.startswith(("然后", "接着", "随后")):
            return None
        if (tail in ("哇", "诶", "哎", "走", "来", "最多", "至少", "其实")
                or (len(tail) <= 7 and tail.endswith(("就说", "会说", "告诉我")))
                or (re.search(r"一旦|只要|如果", left[:last_break]) and right.startswith(("就", "也就")))):
            return last_break
    for p in inside:
        if left[p:].startswith(("比如说", "举个例子")) and len(right) <= 10:
            return p
    repeat = re.search(r"([\u4e00-\u9fff]{2,4})\1$", left)
    if repeat and right.startswith(("其实", "就是", "也", "才")):
        return repeat.start(1) + len(repeat[1])

    # Keep mixed-script names and equipment designations with their head.
    if re.match(r"[a-z]+", right, re.I):
        brand = re.search(r"(?:的|名为|叫作)([\u4e00-\u9fff]{2,4})$", left)
        if brand:
            return brand.start(1)
        model = re.search(r"(?:[几两一二三四五六七八九十0-9]+[款台种套])?[a-z]+$", left, re.I)
        if model and re.match(r"[a-z]+(?:相机|摄像机|设备|系统|镜头)", right, re.I):
            # Include the qualifier with the counted model. A completed verb
            # before a short brand provides a clean start for the whole object.
            stem = left[:model.start()]
            modifier = re.search(r"(?:最[新大小早]|全新|升级)的$", stem)
            if modifier:
                brand = re.search(r"了([\u4e00-\u9fff]{1,6})$", stem[:modifier.start()])
                if brand and not any(brand.start(1) < pos <= modifier.start() for pos in commas):
                    return brand.start(1)
                return modifier.start()
            return model.start()
    if left[-1:].isascii() and re.match(r"技术|芯片|系统|镜头|相机", right):
        return 0
    if right.startswith(("自动化", "智能化", "数字化")):
        obj = re.search(r"(?:发布|公布|采用|使用|推出)了?(.{1,5})$", left)
        if obj:
            return obj.start(1)

    # Determiners/complements, prepositions and short predicates cannot
    # finish the phrase alone. Prefer one readable cue when it fits.
    if left.endswith(("那个", "这个")) and "一样" in right:
        return seam + right.index("一样") + 2
    if re.search(r"离.{1,5}$", left) and right.startswith(("最近", "最远", "更近", "更远")):
        location = left.rfind("离")
        placement = re.search(r"(?:要|会|得)?(?:放|摆|留|坐|站|装|设)在$", left[:location])
        if placement:
            return 0 if len(left) + len(right) <= max_chars else placement.start()
        return location
    seam_context = left + "|" + right
    if any(pattern.search(seam_context) for pattern in _DEPENDENT_PHRASE_SEAMS):
        return 0
    if len(left) <= 5:
        if len(right) <= 4 and left[-1:] == right[:1]:
            return 0
        if re.fullmatch(r"(?:这些|那些|这类|那类|这种|那种).{1,3}", left):
            return 0
        if left.startswith(("我们", "你们", "他们")) and right.startswith(("就是", "也是", "主要")):
            return 0
        if left.startswith("而") and right.startswith(("自动", "智能", "实时")):
            return 0
    return None


def _attach_brief_echo(output, preceding_unit, first_unit, fps, max_chars):
    """After splitting a repeated noun, keep a brief first mention with its lead-in."""
    if len(output) < 3 or preceding_unit is None:
        return
    previous, brief, following = output[-3:]
    noun = re.fullmatch(r"(?:这个|那个|这种|那种)([\u4e00-\u9fff]{2,4})", brief["text"])
    if not noun or not following["text"].startswith(noun[1]):
        return
    if brief["end_frame"] - brief["start_frame"] > round(.5 * fps):
        return
    gap = first_unit["start_frame"] - preceding_unit["end_frame"]
    if (previous["speaker_track_index"] != brief["speaker_track_index"]
            or (preceding_unit.get("asr_punctuation_strength") or 0) >= 1
            or not 0 <= gap <= round(.25 * fps)
            or len(previous["text"]) + len(brief["text"]) > max_chars
            or brief["end_frame"] - previous["start_frame"] > round(6 * fps)):
        return
    previous["text"] += brief["text"]
    previous["end_frame"] = brief["end_frame"]
    previous["segmentation_decision"] = "phrase_reflow"
    del output[-2]


def reflow_phrase_boundaries(
    rows: list[dict[str, Any]], units: list[dict[str, Any]], candidates: list[dict[str, Any]],
    fps: float, max_chars: int, tokenizer: Callable[[str], list[str]] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Reconsider grammatical breaks using real onsets and corroborated microphone seams."""
    ordered = sorted(units, key=lambda u: (u["start_frame"], u["end_frame"]))
    text = "".join(u["text"] for u in ordered)
    if not rows or text != "".join(r["text"] for r in rows):
        return rows, 0
    offsets = {0: 0}
    cursor = 0
    for i, unit in enumerate(ordered, 1):
        cursor += len(unit["text"])
        offsets[cursor] = i
    word_bounds = _unit_word_boundary_positions(ordered, tokenizer)
    corroborated = _aligned_phrase_witnesses(candidates, fps)
    output = []
    end = 0
    split_rows = _split_punctuated_clauses(rows, ordered, offsets, word_bounds)
    changed = len(split_rows) - len(rows)
    for index, row in enumerate(split_rows):
        seam = end
        right = dict(row)
        end += len(right["text"])
        left = output[-1] if output else None
        output.append(right)
        if left is None or seam not in offsets or end not in offsets:
            continue
        start = seam - len(left["text"])
        if start not in offsets:
            continue
        i, j, k = offsets[start], offsets[seam], offsets[end]
        if not i < j < k:
            continue
        gap = ordered[j]["start_frame"] - ordered[j - 1]["end_frame"]
        same_track = left["speaker_track_index"] == right["speaker_track_index"]
        hesitation = same_track and _hesitant_complement(left["text"], right["text"])
        if not 0 <= gap <= round((.85 if hesitation else .5) * fps):
            continue
        punct = ordered[j - 1].get("asr_punctuation_strength") or 0
        commas = set()
        pos = 0
        for unit in ordered[i:k]:
            pos += len(unit["text"])
            if unit.get("asr_punctuation_strength"):
                commas.add(pos)
        resumed = same_track and punct < 1 and _resumed_clause(left["text"], right["text"])
        cut = 0 if hesitation or resumed else _phrase_cut(left["text"], right["text"], commas, punct >= 1, max_chars)
        # A mic can drop out after the first word of a hesitant restart.
        # The new mic must hear that word run into the hesitation itself.
        # A weak comma on the continuing mic is also plausible when the
        # hesitation immediately leads into that same mic's resumed clause.
        following = split_rows[index + 1] if index + 1 < len(split_rows) else None
        resumes_after_filler = (right["text"] in ("嗯", "呃") and following
                                and following["speaker_track_index"] == right["speaker_track_index"]
                                and _resumed_clause("就" + right["text"], following["text"])
                                and (ordered[k - 1].get("asr_punctuation_strength") or 0) < 1
                                and 0 <= following["start_frame"] - ordered[k - 1]["end_frame"] <= round(.3 * fps))
        restart = (j - i >= 2 and ordered[j - 1]["text"] == "就"
                   and right["text"] in ("嗯", "呃")
                   and (ordered[j - 2].get("asr_punctuation_strength") or 0) >= 1
                   and corroborated(ordered[j - 1:k], 1, other_than_track=left["speaker_track_index"],
                                    required_track=None if same_track else right["speaker_track_index"],
                                    witness_gap_limit=round(.3 * fps),
                                    seam_punctuation_limit=.55 if resumes_after_filler else 0))
        if restart:
            cut = len(left["text"]) - 1
        if cut is None or cut == seam - start or start + cut not in offsets:
            continue
        if cut and start + cut not in word_bounds:
            continue
        if max(cut, end - start - cut) > max_chars or (cut and end - start - cut < 2):
            continue
        if cut == 0 and right["end_frame"] - left["start_frame"] > round(6 * fps):
            continue
        stranded_subject = left["text"].endswith(_SUBJECT_PREFIXES)
        if not restart and (not same_track or (not hesitation and punct >= 1
                                               and (gap > round(.3 * fps) or stranded_subject))):
            a, b = max(i, j - 2), min(k, j + 2)
            # A brief hesitation before an estimate can get a spurious period
            # even on the mic that transcribed both sides of the count.
            quantity = _quantity_seam(left["text"], right["text"]) and gap <= round(.3 * fps)
            # In a conditional, both the condition and its consequence can
            # contain a comma. A weak comma before 就 does not end the turn.
            conditional = (cut in commas and 0 < cut < len(left["text"]) and 0 < punct < 1
                           and re.search(r"一旦|只要|如果", left["text"][:cut])
                           and right["text"].startswith(("就", "也就")))
            if not corroborated(ordered[a:b], j - a, witness_gap_limit=round((.3 if quantity else .5) * fps),
                                seam_punctuation_limit=1 if quantity else (.55 if conditional else 0),
                                other_than_track=left["speaker_track_index"] if same_track else None):
                continue
        if cut:
            edge = offsets[start + cut]
            onset = ordered[edge]["start_frame"]
            if not left["start_frame"] < onset < right["end_frame"]:
                continue
            new_end = min(onset, ordered[edge - 1]["end_frame"])
            if new_end <= left["start_frame"]:
                continue
            left["text"], right["text"] = text[start:start + cut], text[start + cut:end]
            left["end_frame"] = new_end
            right["start_frame"] = onset
            right["segmentation_decision"] = "phrase_reflow"
            _attach_brief_echo(output, ordered[i - 1] if i else None, ordered[i], fps, max_chars)
        else:
            left["text"] += right["text"]
            left["end_frame"] = right["end_frame"]
            output.pop()
        left["segmentation_decision"] = "phrase_reflow"
        changed += 1
    for i, row in enumerate(output, 1):
        row["index"] = i
    return output, changed


def protect_word_boundaries(
    rows: list[dict[str, Any]],
    units: list[dict[str, Any]],
    fps: float,
    tokenizer: Callable[[str], list[str]] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """禁止字幕在词内部切分 (word-boundary protection).

    Several unrelated root causes (cross-mic echo swapping the selected
    track mid-word, the DP boundary scorer landing a hash-boundary between
    two characters, a hard max_chars cut hitting exactly inside a word,
    ...) can all produce the same visible symptom: one row ends with the
    first half of a word and the very next row starts with its second half
    (e.g. row N ending "...效" immediately followed by row N+1 starting
    "果来"). Rather than chase every root cause individually, this is a
    single generic outlet-level fix applied once all row boundaries exist:
    tokenize the full concatenated text and, for every row-to-row boundary
    landing inside a tokenized word, move the leftover half of that word
    from the start of the right-hand row onto the end of the left-hand row
    (merging the word back together on the row where it started), then
    re-derive both rows' frame boundaries from the real per-character
    ``units`` so time and text stay exactly conserved.

    ``rows`` is the output of :func:`segment_canonical_units` (typically
    already passed through :func:`reclaim_overlong_unit_tails`) -- each a
    contiguous slice of ``units`` by character count, the same pairing
    convention :func:`reclaim_overlong_unit_tails` and
    :func:`enforce_hard_char_limit` rely on: walking the (start_frame-
    sorted) units in order and consuming exactly ``len(row["text"])``
    characters per row reproduces the same row/unit grouping, with no need
    to trust frame ranges.

    Word boundaries come from ``tokenizer`` (defaults to the built-in
    zero-dependency :func:`_default_word_tokenizer` / WORD_DICTIONARY --
    see there for why this isn't jieba). A boundary the tokenizer doesn't
    recognise as unsafe (including every single-character "word", e.g. a
    lone "啊") is left untouched, per the "拿不准的词边界不动" rule:
    coverage is intentionally partial rather than guessing. A word that is
    itself longer than a row can still only be partially absorbed by its
    immediate neighbour on a given pass; the loop below re-scans until no
    unsafe boundary remains (or nothing more can move), so a word spanning
    more than two rows is still fully reassembled onto the row where it
    started.

    Returns (rows, word_boundary_protected_count) -- the count is the
    number of *original* row-to-row boundaries that fell inside a word
    (computed once, up front, so it doesn't depend on how many internal
    merge passes were needed to fix them).
    """
    original_rows = [dict(row) for row in rows or []]
    if len(original_rows) < 2:
        return original_rows, 0

    ordered_units = sorted(
        [dict(unit) for unit in units or []],
        key=lambda unit: (int(unit.get("start_frame") or 0), int(unit.get("end_frame") or 0)),
    )

    entries: list[dict[str, Any]] = []
    cursor = 0
    for row in original_rows:
        text = str(row.get("text") or "")
        length = len(text)
        row_units = ordered_units[cursor:cursor + length]
        cursor += length
        entries.append(
            {
                "meta": row,
                "text": text,
                "units": row_units,
                "start_frame": int(row.get("start_frame") or 0),
                "end_frame": int(row.get("end_frame") or 0),
            }
        )

    full_text = "".join(entry["text"] for entry in entries)
    entry_units = [unit for entry in entries for unit in entry["units"]]
    word_bounds = _unit_word_boundary_positions(entry_units, tokenizer)

    protected_count = 0
    running = 0
    for entry in entries[:-1]:
        running += len(entry["text"])
        if running not in word_bounds:
            protected_count += 1

    if protected_count == 0:
        return original_rows, 0

    changed = True
    guard = 0
    guard_limit = max(1000, len(full_text) * 4)
    while changed and guard < guard_limit:
        changed = False
        guard += 1
        running = 0
        for index in range(len(entries) - 1):
            running += len(entries[index]["text"])
            if running in word_bounds:
                continue
            left = entries[index]
            right = entries[index + 1]
            word_end = min(bound for bound in word_bounds if bound > running)
            word_start = max(bound for bound in word_bounds if bound < running)
            word = full_text[word_start:word_end]
            # At a Chinese brand / Latin model boundary, start the new cue
            # at the brand rather than absorbing the model into the prior cue.
            take = running - word_start
            if (re.search(r"[\u4e00-\u9fff][a-zA-Z]", word) and take < len(left["units"])
                    and left["start_frame"] < left["units"][-take]["start_frame"] < right["end_frame"]):
                onset = left["units"][-take]["start_frame"]
                right["units"] = left["units"][-take:] + right["units"]
                right["text"] = left["text"][-take:] + right["text"]
                left["units"] = left["units"][:-take]
                left["text"] = left["text"][:-take]
                left["end_frame"] = right["start_frame"] = onset
                changed = True
                break
            move_count = min(word_end - running, len(right["text"]))
            if move_count <= 0:
                continue
            left["text"] += right["text"][:move_count]
            left["units"] += right["units"][:move_count]
            right["text"] = right["text"][move_count:]
            right["units"] = right["units"][move_count:]
            if right["text"]:
                boundary_frame = int(right["units"][0].get("start_frame") or 0)
                if left["start_frame"] < boundary_frame < right["end_frame"]:
                    left["end_frame"] = boundary_frame
                    right["start_frame"] = boundary_frame
                else:
                    # Preserve the complete text span when quantized word times
                    # cannot leave positive duration on both sides of a split.
                    left["text"] += right["text"]
                    left["units"] += right["units"]
                    left["end_frame"] = max(left["end_frame"], right["end_frame"])
                    del entries[index + 1]
            else:
                left["end_frame"] = right["end_frame"]
                del entries[index + 1]
            changed = True
            break

    new_rows: list[dict[str, Any]] = []
    for entry in entries:
        row = dict(entry["meta"])
        row["text"] = entry["text"]
        row["start_frame"] = entry["start_frame"]
        row["end_frame"] = entry["end_frame"]
        new_rows.append(row)
    for index, row in enumerate(new_rows, start=1):
        row["index"] = index

    actual_text = "".join(str(row.get("text") or "") for row in new_rows)
    if actual_text != full_text:
        raise RuntimeError("v4 word boundary protection text conservation failed")

    return new_rows, protected_count


def _hard_cut_boundaries(
    row_units: list[dict[str, Any]],
    max_chars: int,
    tokenizer: Callable[[str], list[str]] | None = None,
) -> list[int]:
    """Choose unit-index boundaries splitting ``row_units`` near ``max_chars``.

    A single word may exceed the limit because preserving a whole English
    word takes precedence over the cap.

    Returns a sorted list of indices into ``row_units`` (each in
    ``1..len(row_units) - 1``); ``row_units[:boundary]`` /
    ``row_units[boundary:]`` marks a cut. Within the character window that
    must be cut to respect ``max_chars``, a boundary right after an ASR
    punctuation mark or the widest inter-unit time gap is preferred (the
    least jarring place to break), but only when it is also a word boundary.
    Failing that, a word boundary (per
    ``tokenizer`` / WORD_DICTIONARY -- see protect_word_boundaries) closest
    to the limit is preferred over cutting through a word; when neither
    exists in that window the cut lands exactly at the ``max_chars``
    character limit (a forced/even split, which can land inside a word not
    covered by the dictionary -- the unavoidable floor when no evidence of
    any kind is available).
    """
    lengths = [len(str(unit.get("text") or "")) or 1 for unit in row_units]
    prefix = [0]
    for length in lengths:
        prefix.append(prefix[-1] + length)
    total = prefix[-1]
    word_bounds = _unit_word_boundary_positions(row_units, tokenizer)
    boundaries: list[int] = []
    start_prefix = 0
    unit_count = len(row_units)
    while total - start_prefix > max_chars:
        forced_limit = start_prefix + max_chars
        best_index: int | None = None
        best_score: tuple[int, int, int] | None = None
        for index in range(1, unit_count):
            if prefix[index] <= start_prefix:
                continue
            if prefix[index] > forced_limit:
                break
            if prefix[index] not in word_bounds:
                continue
            left_unit = row_units[index - 1]
            right_unit = row_units[index]
            punctuation = 1 if (
                _is_break_punctuation(str(left_unit.get("text") or ""))
                or float(left_unit.get("asr_punctuation_strength") or 0.0) >= 1.0
            ) else 0
            gap = max(0, int(right_unit.get("start_frame") or 0) - int(left_unit.get("end_frame") or 0))
            score = (punctuation, gap, prefix[index])
            if best_score is None or score > best_score:
                best_score = score
                best_index = index
        if best_index is not None and (best_score[0] > 0 or best_score[1] > 0):
            chosen = best_index
        else:
            word_safe_candidates = [
                index
                for index in range(1, unit_count)
                if start_prefix < prefix[index] <= forced_limit and prefix[index] in word_bounds
            ]
            if word_safe_candidates:
                chosen = max(word_safe_candidates)
            else:
                next_word_safe_candidates = [
                    index
                    for index in range(1, unit_count)
                    if prefix[index] > forced_limit
                    and prefix[index] > start_prefix
                    and prefix[index] in word_bounds
                ]
                chosen = min(next_word_safe_candidates) if next_word_safe_candidates else None
                if chosen is None:
                    # Only the end of the row remains word-safe.
                    break
        if chosen <= 0 or prefix[chosen] <= start_prefix:
            break
        boundaries.append(chosen)
        start_prefix = prefix[chosen]
    return boundaries


def enforce_hard_char_limit(
    rows: list[dict[str, Any]],
    units: list[dict[str, Any]],
    max_chars: int | None,
    tokenizer: Callable[[str], list[str]] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Cap rows near ``max_chars`` without cutting through English words.

    功能: 短视频档硬性字数上限强制切. ``segment_canonical_units``'s DP
    boundary scoring treats ``max_chars`` as a *soft* target: a long,
    pause-free clause can still score higher unsplit than split (the split
    penalty outweighs the length preference), so a handful of rows can land
    above the requested cap. This pass is a deterministic backstop that
    forces long rows toward ``max_chars`` characters. A single English word
    may exceed the limit because word integrity takes precedence over a
    strict character cap.

    Only takes effect when ``max_chars`` is not ``None`` — with no cap
    requested (the pre-existing default), this returns ``rows`` unchanged
    for full backward compatibility.

    ``rows`` is the output of :func:`segment_canonical_units` (or of
    :func:`reclaim_overlong_unit_tails`, which only touches ``end_frame``);
    ``units`` is the same per-character canonical unit stream fed into
    segmentation. As in :func:`reclaim_overlong_unit_tails`, rows and units
    are paired by walking the (start_frame-sorted) units in order and
    consuming exactly ``len(row["text"])`` characters per row — the same
    grouping segmentation itself used, no need to trust frame ranges.

    When a row needs splitting, each resulting sub-row's ``start_frame``/
    ``end_frame`` comes from real per-character unit boundaries: the first
    sub-row keeps the original row's ``start_frame``, the last sub-row keeps
    the original row's ``end_frame`` (preserving any padding/extension
    already applied upstream), and every internal cut uses the exact
    ``start_frame`` of the unit beginning the next sub-row — so adjacent
    sub-rows are frame-contiguous with no invented or overlapping time.
    Text is preserved exactly (concatenating the sub-rows' text reproduces
    the original row's text).

    Returns (rows, hard_char_split_count) where hard_char_split_count counts
    how many original rows were split (not the number of resulting rows).
    """
    if max_chars is None:
        return [dict(row) for row in rows or []], 0
    max_chars = max(1, int(max_chars))
    ordered_units = sorted(
        [dict(unit) for unit in units or []],
        key=lambda unit: (int(unit.get("start_frame") or 0), int(unit.get("end_frame") or 0)),
    )
    output: list[dict[str, Any]] = []
    cursor = 0
    split_count = 0
    for row in rows or []:
        row = dict(row)
        text = str(row.get("text") or "")
        target_length = len(text)
        row_units: list[dict[str, Any]] = []
        consumed = 0
        while consumed < target_length and cursor < len(ordered_units):
            candidate_unit = ordered_units[cursor]
            row_units.append(candidate_unit)
            consumed += len(str(candidate_unit.get("text") or "")) or 1
            cursor += 1
        if len(text) <= max_chars or len(row_units) < 2:
            output.append(row)
            continue
        original_start = int(row.get("start_frame") or 0)
        original_end = int(row.get("end_frame") or 0)
        boundaries = []
        previous_frame = original_start
        # Several aligned characters can share one frame, or lie beyond a
        # reclaimed tail. Keep them together instead of inventing a 1-frame overlap.
        for boundary in _hard_cut_boundaries(row_units, max_chars, tokenizer):
            frame = int(row_units[boundary].get("start_frame") or 0)
            if previous_frame < frame < original_end:
                boundaries.append(boundary)
                previous_frame = frame
        if not boundaries:
            output.append(row)
            continue
        edges = [0, *boundaries, len(row_units)]
        for position in range(len(edges) - 1):
            slice_start, slice_end = edges[position], edges[position + 1]
            sub_units = row_units[slice_start:slice_end]
            sub_row = dict(row)
            sub_row["text"] = "".join(str(unit.get("text") or "") for unit in sub_units)
            if position == 0:
                sub_row["start_frame"] = original_start
            else:
                sub_row["start_frame"] = int(sub_units[0].get("start_frame") or 0)
            if position == len(edges) - 2:
                sub_row["end_frame"] = original_end
            else:
                next_unit_start = int(row_units[slice_end].get("start_frame") or 0)
                sub_row["end_frame"] = next_unit_start
            sub_row["segmentation_decision"] = "hard_char_split"
            output.append(sub_row)
        split_count += 1
    for index, row in enumerate(output, start=1):
        row["index"] = index
    return output, split_count


def _unit_word_boundary_positions(
    units: list[dict[str, Any]],
    tokenizer: Callable[[str], list[str]] | None = None,
) -> frozenset[int]:
    ordered = [dict(unit) for unit in units or []]
    text = "".join(str(unit.get("text") or "") for unit in ordered)
    positions = set(_word_boundary_positions(text, tokenizer))
    cursor = 0
    for index in range(1, len(ordered)):
        left_text = str(ordered[index - 1].get("text") or "")
        right_text = str(ordered[index].get("text") or "")
        cursor += len(left_text)
        left_char = left_text[-1:] if left_text else ""
        right_char = right_text[:1] if right_text else ""
        # The dictionary sees unpunctuated text. A sentence end in the ASR
        # separates homographs such as 实现。在 and 现在.
        if not (_is_ascii_word_char(left_char) and _is_ascii_word_char(right_char)):
            if float(ordered[index - 1].get("asr_punctuation_strength") or 0) >= 1.0:
                positions.add(cursor)
            continue
        if ordered[index].get("space_before") is True:
            positions.add(cursor)
        else:
            positions.discard(cursor)
    return frozenset(positions)


def _punctuate_spoken_actions(text: str, units: list[dict[str, Any]], fps: float) -> str:
    """Separate a completed action or repeated short answer within one turn."""
    action = re.search(
        r"先(?:得到|拿到|找到|收到|做好|完成)(?:它|这件事|这个|那个)"
        r"(?=先(?:放|存|收|留|摆|记|拿))", text
    )
    answer = re.match(r"(([我你他她]们?)(?:有|会|能|懂|知道|记得))\1(?=\2[\u4e00-\u9fff]{2,})", text)
    if action:
        boundaries = {action.end()}
    elif answer:
        boundaries = {answer.end(1), answer.end()}
    else:
        return text

    # Add the separators together, only at measured pauses between units.
    # A repeated syllable without these gaps can be a stutter instead.
    if (len({u.get("track_index") for u in units}) != 1
            or not units[0].get("track_index")
            or "".join(u["text"] for u in units) != text):
        return text
    offset = 0
    pauses = {}
    for left, right in zip(units, units[1:]):
        offset += len(left["text"])
        gap = right["start_frame"] - left["end_frame"]
        if max(1, round(.05 * fps)) <= gap <= round(.4 * fps):
            pauses[offset] = right["start_frame"]
    if boundaries <= pauses.keys():
        # Quick repetitions form one answer. The next clause still gets a
        # real boundary, even if each individual repetition would flash.
        join_echo = (not action and answer
                     and min(pauses[answer.end(1)] - units[0]["start_frame"],
                             pauses[answer.end()] - pauses[answer.end(1)]) < math.ceil(.5 * fps))
        for offset in sorted(boundaries, reverse=True):
            separator = " " if join_echo and offset == answer.end(1) else "，"
            text = text[:offset] + separator + text[offset:]
    return text


def _punctuate_leadin_pause(text: str, units: list[dict[str, Any]], fps: float) -> str:
    # An experiential lead-in can introduce a second clause in the same cue.
    # A short direct object (e.g. 试过才知道答案) does not warrant a comma.
    lead = re.match(r"[\u4e00-\u9fff]{1,5}过才(?:知道|明白|发现|意识到)", text)
    if not lead or not re.match(r"[\u4e00-\u9fff]{1,7}?(?:远没|并不|并非|比|不如|没有|确实|真的)",
                                text[lead.end():]):
        return text
    pos = 0
    for left, right in zip(units, units[1:]):
        pos += len(left["text"])
        if pos >= lead.end():
            gap = right["start_frame"] - left["end_frame"]
            if (pos == lead.end() and left.get("track_index") == right.get("track_index")
                    and round(.17 * fps) <= gap <= round(.5 * fps)
                    and (left.get("asr_punctuation_strength") or 0) < 1):
                return text[:pos] + "，" + text[pos:]
            break
    return text


def _spoken_clause_separator(left: str, right: str) -> str:
    if left.endswith(("进行讨论", "一起讨论", "商量一下")) and re.match(
            r"看(?:看)?[我你他她]们?(?:还|能|要|该|到底)", right):
        return "，"
    # Indirect speech can open with a subordinate clause of its own.
    if (re.search(r"(?:告诉|提醒|叮嘱)[我你他她]们?$", left)
            and right.startswith(("不管", "无论", "即使", "如果", "只要"))):
        return "，"
    # Repeat the same description with an intensifier, or restate the same
    # negative predicate more emphatically. Match the repeated word itself.
    emphasis = re.match(r"(?:很|非常|特别|更)([\u4e00-\u9fff]{1,4}?的)(?=[\u4e00-\u9fff])", right)
    if emphasis and left.endswith(emphasis[1]):
        return "，"
    negative = re.search(r"(?:无法|不能)([\u4e00-\u9fff]{1,4})$", left)
    if negative and re.match(r"根本(?:就)?"+negative[1]+r"不(?:了|成|到)", right):
        return "，"
    if left.endswith("着来") and re.fullmatch(r"[\u4e00-\u9fff]{1,3}着来", right):
        return "，"

    # An example may contrast two roles, or two opposing movement directions.
    if (re.search(r"[他她我你](?:站|坐|靠|躺)在[\u4e00-\u9fff]{1,5}$", left)
            and re.match(r"[我你他她](?:是|负责|扮演)", right)):
        return "，"
    direction = re.search(r"(?:往|向)([上下左右前后])(?:拉|推|移|走|挪|拍)$", left)
    if direction:
        opposite = str.maketrans("上下左右前后", "下上右左后前")
        axis = direction[1]
        if re.match(r"从" + axis + r"(?:往|向|到)" + axis.translate(opposite), right):
            return "，"
    if left in ("你好", "您好", "大家好") and re.match(r"(?:又|还|刚)(?:来|到|见|遇|碰)", right):
        return "，"

    # A lone subject or a time phrase is abandoned for a new one.
    if (left in ("他", "她", "我", "你") and right[:1] != left
            and re.match(r"[我你他她](?:设想|想象|打算|觉得|以为)", right)):
        return "……"
    time_subject = re.fullmatch(r"([这那])(时(?:候)?)([我你他她]们?)", left)
    if time_subject:
        other = "这" if time_subject[1] == "那" else "那"
        if right.startswith(other + time_subject[2] + time_subject[3]):
            return "……"

    # An interrupted description restarts with a location and repeats its verb.
    restart = re.search(r"(?:已经|开始|快要)([冒发变泛][\u4e00-\u9fff])$", left)
    if restart and re.match(r"(?:眼睛|眼前|脑海|脑袋|脸上|心里|手上)", right) and restart[1] in right:
        return "……"

    # Parallel alternatives and a command followed by its negative consequence.
    parallel = r"([\u4e00-\u9fff]{2,5})就\1"
    if re.fullmatch(parallel, left) and re.fullmatch(parallel, right):
        return "，"
    command = re.search(r"(?:要|应该|可以|得)([\u4e00-\u9fff]{1,5})的$", left)
    if command and right.startswith("不" + command[1]) and len(right) > len(command[1]) + 1:
        return "，"

    if re.search(r"是[\u4e00-\u9fff]{1,8}的$", left) and right.startswith("就是"):
        return "，"
    partner = re.search(r"(?:跟|和|向|对)([他她你我]们?)[\u4e00-\u9fff]{2,4}$", left)
    if partner and re.match(partner[1] + r"(?:就|却|也|才)", right):
        return "，"

    # A completed predicate can become the topic of the next clause.
    echo = re.match(r"([\u4e00-\u9fff]{2,5})[他她你我](?:们)?(?:就|也|才|便)", right)
    if echo and left.endswith(echo[1]):
        return "，"
    return ""


def _restore_spoken_clause_punctuation(
    text: str, units: list[dict[str, Any]], fps: float, next_unit: dict[str, Any] | None = None,
) -> str:
    if text != "".join(unit["text"] for unit in units) or not re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9]+", text):
        return text
    # A sentence-final connector, a long silence, then only a filler: the
    # promised continuation never starts. Leave the filler at its own onset.
    last = units[-1]
    unfinished = (
        text.endswith("然后") and (last.get("asr_punctuation_strength") or 0) >= 1 and next_unit
        and last.get("track_index") == next_unit.get("track_index")
        and next_unit["start_frame"] - last["end_frame"] >= round(.8 * fps)
        and next_unit["text"] in ("嗯", "呃") and (next_unit.get("asr_punctuation_strength") or 0) >= 1
    )
    interrupted = (
        text.endswith(("就已经", "都已经")) and (last.get("asr_punctuation_strength") or 0) >= 1
        and next_unit and last.get("track_index") and next_unit.get("track_index")
        and last["track_index"] != next_unit["track_index"] and next_unit["text"] in ("嗯", "呃")
        and 0 <= next_unit["start_frame"] - last["end_frame"] <= round(.55 * fps)
    )
    parts = []
    offset = 0
    for prev, following in zip(units, units[1:]):
        parts.append(prev["text"])
        offset += len(prev["text"])
        gap = following["start_frame"] - prev["end_frame"]
        if not prev.get("track_index") or prev.get("track_index") != following.get("track_index") or not 0 <= gap <= round(.55 * fps):
            continue
        left, right = text[:offset], text[offset:]
        if (gap > 0 and re.search(r"(?:几|数)十(?:多)?秒(?:钟)?$", left)
                and re.fullmatch(r"(?:半|一两?|两三?)分钟(?:左右|上下)?", right)):
            # These are two estimates, distinct from a compound like 一分三十秒.
            parts.append(" ")
        elif (.5 <= float(prev.get("asr_punctuation_strength") or 0) < 1 and gap <= round(.5 * fps)):
            separator = "，" if unfinished and right == "然后" else _spoken_clause_separator(left, right)
            parts.append(separator)
        elif round(.3 * fps) <= gap:
            if ((re.fullmatch(r"[你我他她]们?的", left) and right.startswith(left)
                 and re.match(r"(?:意思|想法|意见|看法|问题)", right[len(left):]))
                    or (left.endswith("甚至都") and re.match(r"一[\u4e00-\u9fff]{1,3}都.", right))):
                parts.append("……")
    parts.append(last["text"])
    if unfinished or interrupted:
        parts.append("……")
    return "".join(parts)


def restore_display_spacing(
    rows: list[dict[str, Any]],
    units: list[dict[str, Any]],
    editorial_fps: float | None = None,
) -> list[dict[str, Any]]:
    ordered_units = sorted(
        [dict(unit) for unit in units or []],
        key=lambda unit: (int(unit.get("start_frame") or 0), int(unit.get("end_frame") or 0)),
    )
    output: list[dict[str, Any]] = []
    cursor = 0
    for raw_row in rows or []:
        row = dict(raw_row)
        target_length = len(normalize_text(row.get("text")))
        row_units: list[dict[str, Any]] = []
        consumed = 0
        while consumed < target_length and cursor < len(ordered_units):
            candidate = ordered_units[cursor]
            row_units.append(candidate)
            consumed += len(normalize_text(candidate.get("text"))) or 1
            cursor += 1
        pieces: list[str] = []
        previous_char = ""
        for unit in row_units:
            unit_text = str(unit.get("text") or "")
            current_char = unit_text[:1]
            if (
                pieces
                and unit.get("space_before") is True
                and _is_ascii_word_char(previous_char)
                and _is_ascii_word_char(current_char)
            ):
                pieces.append(" ")
            pieces.append(unit_text)
            previous_char = unit_text[-1:] if unit_text else previous_char
        if row_units:
            row["text"] = "".join(pieces)
            if editorial_fps:
                row["text"] = _punctuate_leadin_pause(row["text"], row_units, editorial_fps)
                row["text"] = _punctuate_spoken_actions(row["text"], row_units, editorial_fps)
                next_unit = ordered_units[cursor] if cursor < len(ordered_units) else None
                row["text"] = _restore_spoken_clause_punctuation(row["text"], row_units, editorial_fps, next_unit)
        output.append(row)
    return output


def extend_subtitle_row_tails(
    rows: list[dict[str, Any]],
    fps: float,
    max_gap_frames: int,
) -> tuple[list[dict[str, Any]], int]:
    """Extend each row's display end toward the next row's start.

    P3-问题6: manual editors habitually keep a subtitle row on screen until
    the next line's speech begins, while the DP segmenter cuts a row the
    moment its own speech ends. If the gap between the current row's
    ``end_frame`` and the next row's ``start_frame`` is small (<= max_gap_frames),
    treat it as the same "kept on screen" gap and extend the current row's
    ``end_frame`` toward it, by at most
    ``round(SUBTITLE_ROW_TAIL_EXTENSION_MAX_SECONDS * fps)`` frames per row
    (capped variant selected by review simulation: extending all the way to
    the next row's start swallowed skipped manual boundaries and collapsed
    one-to-one matches). A gap larger than ``max_gap_frames`` is treated as
    real silence and is left untouched. Rows that are already adjacent
    (gap <= 0) are also left untouched. Only ``end_frame`` is mutated; text
    and start_frame are never touched, so this cannot violate
    segment_canonical_units' text conservation invariant.

    Returns (rows, tail_extended_row_count).
    """
    ordered = sorted(
        [dict(row) for row in rows or []],
        key=lambda row: (int(row.get("start_frame") or 0), int(row.get("end_frame") or 0)),
    )
    max_extension_frames = max(1, int(round(SUBTITLE_ROW_TAIL_EXTENSION_MAX_SECONDS * fps)))
    extended_count = 0
    for index in range(len(ordered) - 1):
        current = ordered[index]
        following = ordered[index + 1]
        current_end = int(current.get("end_frame") or 0)
        following_start = int(following.get("start_frame") or 0)
        gap = following_start - current_end
        if 0 < gap <= max_gap_frames:
            current["end_frame"] = current_end + min(gap, max_extension_frames)
            extended_count += 1
    return ordered, extended_count


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
