from __future__ import annotations

import difflib
import importlib.util
import math
import re
import wave
from array import array
from bisect import bisect_left
from itertools import accumulate
from pathlib import Path
from typing import Any

def _sibling_module(name):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name != name:
            raise
    # Resolve can load this file by path without putting its directory on sys.path.
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"生成模块无法加载: {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


v4 = _sibling_module("subfix_generate_v4")
textnorm = _sibling_module("subfix_generate_textnorm")


PROFILE_SCHEMA = "subfix_segmentation_profile_v4"
DIAGNOSTIC_SCHEMA = "subfix_generate_diagnostic_v3"


def _normalized_similarity(left: str, right: str) -> float:
    left_text = v4.normalize_text(left)
    right_text = v4.normalize_text(right)
    if not left_text and not right_text:
        return 1.0
    if not left_text or not right_text:
        return 0.0
    return difflib.SequenceMatcher(None, left_text, right_text, autojunk=False).ratio()


def _merge_retry_regions(regions: list[dict[str, Any]], maximum_frames: int) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for region in sorted(regions, key=lambda row: (int(row["track_index"]), int(row["start_frame"]))):
        if (
            merged
            and int(merged[-1]["track_index"]) == int(region["track_index"])
            and int(region["start_frame"]) <= int(merged[-1]["end_frame"])
            and max(int(merged[-1]["end_frame"]), int(region["end_frame"])) - int(merged[-1]["start_frame"]) <= maximum_frames
        ):
            merged[-1]["end_frame"] = max(int(merged[-1]["end_frame"]), int(region["end_frame"]))
            merged[-1]["reasons"] = sorted(set(merged[-1].get("reasons") or []) | set(region.get("reasons") or []))
            merged[-1]["reason"] = "+".join(merged[-1]["reasons"])
        else:
            merged.append(dict(region))
    return merged


def _merge_adaptive_regions(regions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for region in sorted(regions, key=lambda row: (int(row["start_frame"]), int(row["end_frame"]))):
        start = int(region["start_frame"])
        end = max(start + 1, int(region["end_frame"]))
        reasons = set(region.get("reasons") or [])
        if merged and start <= int(merged[-1]["end_frame"]):
            merged[-1]["end_frame"] = max(int(merged[-1]["end_frame"]), end)
            merged[-1]["reasons"] = sorted(set(merged[-1]["reasons"]) | reasons)
        else:
            merged.append({"start_frame": start, "end_frame": end, "reasons": sorted(reasons)})
    return merged


def partition_adaptive_regions(
    units: list[dict[str, Any]],
    fps: float,
    padding_seconds: float = 2.5,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    valid_units = sorted(
        [
            dict(unit)
            for unit in units or []
            if int(unit.get("end_frame") or 0) > int(unit.get("start_frame") or 0)
        ],
        key=lambda unit: (int(unit["start_frame"]), int(unit["end_frame"]), int(unit.get("track_index") or 0)),
    )
    risky_regions: list[dict[str, Any]] = []
    active_units: list[dict[str, Any]] = []
    recent_units: list[dict[str, Any]] = []
    echo_lookback_frames = max(1, int(round(3.0 * float(fps))))
    for unit in valid_units:
        start = int(unit["start_frame"])
        end = int(unit["end_frame"])
        reasons: list[str] = []
        coverage = unit.get("raw_alignment_coverage")
        if coverage is not None and float(coverage) < 0.98:
            reasons.append("low_alignment_coverage")
        if bool(unit.get("alignment_repaired")):
            reasons.append("alignment_repaired")
        if str(unit.get("candidate_kind") or "primary") == "local_retry":
            reasons.append("local_retry")
        if reasons:
            risky_regions.append({"start_frame": start, "end_frame": end, "reasons": reasons})

        active_units = [candidate for candidate in active_units if int(candidate["end_frame"]) > start]
        for active in active_units:
            if int(active.get("track_index") or 0) == int(unit.get("track_index") or 0):
                continue
            overlap_start = max(start, int(active["start_frame"]))
            overlap_end = min(end, int(active["end_frame"]))
            if overlap_end > overlap_start:
                risky_regions.append(
                    {
                        "start_frame": overlap_start,
                        "end_frame": overlap_end,
                        "reasons": ["cross_track_overlap"],
                    }
                )
        active_units.append(unit)

        recent_units = [
            candidate
            for candidate in recent_units
            if int(candidate["end_frame"]) > start - echo_lookback_frames
        ]
        unit_text = v4.normalize_text(unit.get("text"))
        if unit_text:
            for recent in recent_units:
                if int(recent.get("track_index") or 0) == int(unit.get("track_index") or 0):
                    continue
                if unit_text != v4.normalize_text(recent.get("text")):
                    continue
                risky_regions.append(
                    {
                        "start_frame": min(start, int(recent["start_frame"])),
                        "end_frame": max(end, int(recent["end_frame"])),
                        "reasons": ["cross_track_echo"],
                    }
                )
        recent_units.append(unit)

    padding_frames = max(0, int(round(float(padding_seconds) * float(fps))))
    expanded_risky = _merge_adaptive_regions(
        [
            {
                "start_frame": int(region["start_frame"]) - padding_frames,
                "end_frame": int(region["end_frame"]) + padding_frames,
                "reasons": region["reasons"],
            }
            for region in _merge_adaptive_regions(risky_regions)
        ]
    )
    safe_regions = _merge_adaptive_regions(
        [
            {"start_frame": int(unit["start_frame"]), "end_frame": int(unit["end_frame"]), "reasons": []}
            for unit in valid_units
            if not any(
                int(region["end_frame"]) > int(unit["start_frame"])
                and int(unit["end_frame"]) > int(region["start_frame"])
                for region in expanded_risky
            )
        ]
    )
    return safe_regions, expanded_risky


def _payload_overlap_text(
    payload: dict[str, Any],
    window: dict[str, Any],
    overlap_start: int,
    overlap_end: int,
    fps: float,
    side: str,
) -> str:
    words: list[dict[str, Any]] = []
    for segment in payload.get("segments") or []:
        words.extend(segment.get("words") or [])
    words.extend(payload.get("words") or [])
    relative_start = (overlap_start - int(window.get("start_frame") or 0)) / fps
    relative_end = (overlap_end - int(window.get("start_frame") or 0)) / fps
    timed_text: list[str] = []
    for word in words:
        try:
            start = float(word.get("start"))
            end = float(word.get("end"))
        except (TypeError, ValueError):
            continue
        if min(end, relative_end) <= max(start, relative_start):
            continue
        timed_text.append(str(word.get("text") or word.get("word") or ""))
    normalized = v4.normalize_text("".join(timed_text))
    if normalized:
        return normalized

    full_text = v4.normalize_text(payload.get("text"))
    if not full_text:
        return ""
    overlap_ratio = min(1.0, max(0.1, (overlap_end - overlap_start) / max(1, int(window.get("end_frame") or 0) - int(window.get("start_frame") or 0))))
    sample_length = max(4, min(len(full_text), int(math.ceil(len(full_text) * overlap_ratio * 1.5))))
    return full_text[-sample_length:] if side == "left" else full_text[:sample_length]


def detect_ambiguous_regions(
    windows: list[dict[str, Any]],
    payloads: list[dict[str, Any]],
    fps: float,
    similarity_threshold: float = 0.92,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    pairs = sorted(
        zip(windows or [], payloads or []),
        key=lambda pair: (int(pair[0].get("track_index") or 0), int(pair[0].get("start_frame") or 0)),
    )
    padding = max(1, int(round(2.5 * fps)))
    maximum_frames = max(1, int(round(16.0 * fps)))
    regions: list[dict[str, Any]] = []
    disagreement_count = 0
    for (left_window, left_payload), (right_window, right_payload) in zip(pairs, pairs[1:]):
        track_index = int(left_window.get("track_index") or 0)
        if track_index != int(right_window.get("track_index") or 0):
            continue
        overlap_start = max(int(left_window.get("start_frame") or 0), int(right_window.get("start_frame") or 0))
        overlap_end = min(int(left_window.get("end_frame") or 0), int(right_window.get("end_frame") or 0))
        if overlap_end <= overlap_start:
            continue
        left_overlap_text = _payload_overlap_text(
            left_payload,
            left_window,
            overlap_start,
            overlap_end,
            fps,
            "left",
        )
        right_overlap_text = _payload_overlap_text(
            right_payload,
            right_window,
            overlap_start,
            overlap_end,
            fps,
            "right",
        )
        similarity = _normalized_similarity(left_overlap_text, right_overlap_text)
        if similarity >= similarity_threshold:
            continue
        disagreement_count += 1
        center = (overlap_start + overlap_end) // 2
        start = max(
            min(int(left_window.get("start_frame") or overlap_start), int(right_window.get("start_frame") or overlap_start)),
            overlap_start - padding,
        )
        end = min(
            max(int(left_window.get("end_frame") or overlap_end), int(right_window.get("end_frame") or overlap_end)),
            overlap_end + padding,
        )
        if end - start > maximum_frames:
            start = max(0, center - maximum_frames // 2)
            end = start + maximum_frames
        regions.append(
            {
                "track_index": track_index,
                "start_frame": start,
                "end_frame": end,
                "reason": "overlap_text_disagreement",
                "reasons": ["overlap_text_disagreement"],
                "overlap_similarity": round(similarity, 6),
            }
        )
    merged = _merge_retry_regions(regions, maximum_frames)
    return merged, {
        "overlap_disagreement_count": disagreement_count,
        "local_retry_region_count": len(merged),
    }


def _repetition_ratio(text: str) -> float:
    normalized = v4.normalize_text(text)
    if len(normalized) < 4:
        return 0.0
    repeated = 0
    for size in range(1, min(6, len(normalized) // 2) + 1):
        for index in range(0, len(normalized) - size * 2 + 1):
            if normalized[index:index + size] == normalized[index + size:index + size * 2]:
                repeated = max(repeated, size)
    return repeated / max(1, len(normalized))


def candidate_quality_score(candidate: dict[str, Any]) -> float:
    text = v4.normalize_text(candidate.get("text"))
    unit_count = max(1, int(candidate.get("unit_count") or len(text) or 1))
    repaired_ratio = float(candidate.get("alignment_repaired_unit_count") or 0) / unit_count
    coverage = max(0.0, min(1.0, float(candidate.get("raw_alignment_coverage") or 0.0)))
    speech_coverage = max(0.0, min(1.0, float(candidate.get("speech_coverage") or 0.0)))
    edge_distance = max(0.0, min(1.0, float(candidate.get("edge_distance_ratio") or 0.0)))
    repetition = _repetition_ratio(text)
    character_rate = float(candidate.get("character_rate") or 0.0)
    rate_penalty = 0.0
    if character_rate and not 1.0 <= character_rate <= 12.0:
        rate_penalty = min(2.0, abs(character_rate - 6.0) / 4.0)
    return round(
        coverage * 6.0
        + speech_coverage * 3.0
        + edge_distance
        - repaired_ratio * 8.0
        - repetition * 10.0
        - rate_penalty,
        6,
    )


def select_best_candidate(candidates: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    if not candidates:
        raise ValueError("v5 candidate list is empty")
    scored = [(candidate_quality_score(candidate), index, candidate) for index, candidate in enumerate(candidates)]
    score, _index, selected = max(scored, key=lambda item: (item[0], -item[1]))
    return dict(selected), {
        "candidate_count": len(candidates),
        "selected_candidate_score": score,
        "low_confidence_region": score < 6.0,
        "candidate_scores": [item[0] for item in scored],
    }


def alignment_needs_retry(diagnostic: dict[str, Any]) -> bool:
    coverage = float(diagnostic.get("raw_alignment_coverage") or 0.0)
    unit_count = max(1, int(diagnostic.get("unit_count") or 1))
    repaired_ratio = float(diagnostic.get("alignment_repaired_unit_count") or 0) / unit_count
    return coverage < 0.98 or repaired_ratio > 0.05


def alignment_failure_is_fatal(window: dict[str, Any]) -> bool:
    return str(window.get("candidate_kind") or "primary") != "local_retry"


def _units_candidate(units: list[dict[str, Any]]) -> dict[str, Any]:
    text = "".join(str(unit.get("text") or "") for unit in units)
    unit_count = max(1, len(units))
    coverages = [
        float(unit["raw_alignment_coverage"])
        if unit.get("raw_alignment_coverage") is not None
        else 1.0
        for unit in units
    ]
    return {
        "text": text,
        "unit_count": unit_count,
        "raw_alignment_coverage": min(coverages) if coverages else 0.0,
        "alignment_repaired_unit_count": sum(1 for unit in units if unit.get("alignment_repaired")),
        "speech_coverage": sum(1 for unit in units if unit.get("independent_vad")) / unit_count,
        "edge_distance_ratio": 0.5,
    }


def select_retry_region_units(
    original_units: list[dict[str, Any]],
    retry_units: list[dict[str, Any]],
    region: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    start = int(region.get("start_frame") or 0)
    end = max(start + 1, int(region.get("end_frame") or start + 1))

    def inside(unit: dict[str, Any]) -> bool:
        midpoint = (int(unit.get("start_frame") or 0) + int(unit.get("end_frame") or 0)) / 2.0
        return start <= midpoint < end

    original_region = [dict(unit) for unit in original_units if inside(unit)]
    retry_region = [dict(unit) for unit in retry_units if inside(unit)]
    if not retry_region:
        return [dict(unit) for unit in original_units], {
            "local_retry_selected": False,
            "low_confidence_region": True,
            "reason": "retry_has_no_aligned_units",
        }
    selected, diagnostic = select_best_candidate(
        [
            {**_units_candidate(original_region), "candidate_kind": "primary"},
            {**_units_candidate(retry_region), "candidate_kind": "local_retry"},
        ]
    )
    use_retry = (
        selected.get("candidate_kind") == "local_retry"
        and float(diagnostic.get("selected_candidate_score") or 0.0) >= 6.0
    )
    kept = [dict(unit) for unit in original_units if not inside(unit)]
    replacement = retry_region if use_retry else original_region
    for unit in replacement:
        unit["candidate_decision"] = "local_retry_selected" if use_retry else "primary_retained"
    output = sorted(
        [*kept, *replacement],
        key=lambda unit: (int(unit.get("start_frame") or 0), int(unit.get("end_frame") or 0)),
    )
    diagnostic["local_retry_selected"] = use_retry
    diagnostic["low_confidence_region"] = float(
        diagnostic.get("selected_candidate_score") or 0.0
    ) < 6.0
    diagnostic["reason"] = (
        "candidate_quality"
        if use_retry or not diagnostic["low_confidence_region"]
        else "retry_below_quality_gate"
    )
    return output, diagnostic


def arbitrate_retry_region_units(
    original_units: list[dict[str, Any]],
    retry_units: list[dict[str, Any]],
    region: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    _proposed_units, diagnostic = select_retry_region_units(
        original_units,
        retry_units,
        region,
    )
    diagnostic["local_retry_proposed"] = bool(diagnostic.get("local_retry_selected"))
    diagnostic["local_retry_selected"] = False
    diagnostic["local_retry_writeback_mode"] = "shadow"
    return [dict(unit) for unit in original_units], diagnostic


def build_segmentation_profile_v4(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    training_payloads: list[dict[str, Any]] = []
    ignored_test_count = 0
    ignored_counts = {"editorial": 0, "format_only": 0}
    for payload in payloads or []:
        if str(payload.get("dataset_role") or "training") == "test":
            ignored_test_count += 1
            continue
        cleaned = dict(payload)
        cleaned["subtitle_mode"] = str(cleaned.get("subtitle_mode") or "live")
        payload_class = str(cleaned.get("calibration_class") or "")
        for row_key in ("plugin_rows", "manual_rows"):
            speech_rows: list[dict[str, Any]] = []
            for raw_row in cleaned.get(row_key) or []:
                row = dict(raw_row)
                row_class = str(row.get("calibration_class") or payload_class)
                if row_class not in {"speech", "editorial", "format_only"}:
                    row_class = classify_calibration_row(
                        row,
                        bool(row.get("has_speech", True)),
                    )
                if row_class == "speech":
                    speech_rows.append(row)
                else:
                    ignored_counts[row_class] += 1
            cleaned[row_key] = speech_rows
        training_payloads.append(cleaned)
    profile = v4.build_segmentation_profile_v3(training_payloads)
    profile["schema_version"] = PROFILE_SCHEMA
    profile.setdefault("diagnostic", {})["test_payload_ignored_count"] = ignored_test_count
    profile["diagnostic"]["source_schema"] = v4.PROFILE_SCHEMA
    profile["diagnostic"]["editorial_row_ignored_count"] = ignored_counts["editorial"]
    profile["diagnostic"]["format_only_row_ignored_count"] = ignored_counts["format_only"]
    return profile


def classify_calibration_row(row: dict[str, Any], has_speech: bool = True) -> str:
    text = str(row.get("text") or "").strip()
    if not has_speech or re.match(r"^[（(【\[].*[）)】\]]$", text):
        return "editorial"
    normalized = v4.normalize_text(text)
    if normalized and all(char.isdigit() or char in "%+-零一二三四五六七八九十百千万" for char in normalized):
        return "format_only"
    return "speech"


def require_profile_mode(profile: dict[str, Any] | None, subtitle_mode: str) -> dict[str, Any]:
    mode = "live" if subtitle_mode == "live" else "narration"
    mode_profile = ((profile or {}).get("modes") or {}).get(mode)
    if not isinstance(mode_profile, dict):
        raise RuntimeError(f"v5 缺少 {mode} 分句配置，请重新安装最新版 SubFix")
    return mode_profile


def _read_pcm16(path: str | Path) -> tuple[int, array]:
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise ValueError("v5 boundary refinement requires mono PCM16 WAV")
        sample_rate = handle.getframerate()
        samples = array("h")
        samples.frombytes(handle.readframes(handle.getnframes()))
    return sample_rate, samples


def _window_rms(samples: array, size: int) -> list[float]:
    levels: list[float] = []
    for index in range(0, len(samples), size):
        chunk = samples[index:index + size]
        if not chunk:
            continue
        levels.append(math.sqrt(sum(float(value) * float(value) for value in chunk) / len(chunk)))
    return levels


def _speech_regions(levels: list[float], sample_rate: int, hop_samples: int, fps: float, track_start_frame: int) -> list[tuple[int, int]]:
    if not levels:
        return []
    sorted_levels = sorted(levels)
    noise = sorted_levels[min(len(sorted_levels) - 1, int(len(sorted_levels) * 0.20))]
    peak = max(sorted_levels)
    on_threshold = max(noise * 2.0, peak * 0.15, 1.0)
    off_threshold = max(noise * 1.35, peak * 0.08, 0.5)
    hangover_windows = max(1, int(math.ceil(0.08 * sample_rate / hop_samples)))
    voiced = False
    start_index = 0
    below_count = 0
    regions: list[tuple[int, int]] = []
    for index, level in enumerate(levels):
        if not voiced and level >= on_threshold:
            voiced = True
            start_index = index
            below_count = 0
        elif voiced:
            if level < off_threshold:
                below_count += 1
                if below_count >= hangover_windows:
                    end_index = max(start_index + 1, index - below_count + 1)
                    start_frame = track_start_frame + int(round(start_index * hop_samples / sample_rate * fps))
                    end_frame = track_start_frame + int(round(end_index * hop_samples / sample_rate * fps))
                    regions.append((start_frame, max(start_frame + 1, end_frame)))
                    voiced = False
                    below_count = 0
            else:
                below_count = 0
    if voiced:
        start_frame = track_start_frame + int(round(start_index * hop_samples / sample_rate * fps))
        end_frame = track_start_frame + int(round(len(levels) * hop_samples / sample_rate * fps))
        regions.append((start_frame, max(start_frame + 1, end_frame)))
    return regions


def _nearest_region(regions: list[tuple[int, int]], start: int, end: int, search_frames: int) -> tuple[int, int] | None:
    candidates = [
        region for region in regions
        if region[1] >= start - search_frames and region[0] <= end + search_frames
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda region: abs(region[0] - start) + abs(region[1] - end))


def _lowest_energy_frame(
    levels: list[float],
    left_frame: int,
    right_frame: int,
    track_start_frame: int,
    fps: float,
    hop_seconds: float,
) -> int:
    if right_frame <= left_frame:
        return left_frame
    best_frame = (left_frame + right_frame) // 2
    best_level = math.inf
    for frame in range(left_frame, right_frame + 1):
        seconds = (frame - track_start_frame) / fps
        index = max(0, min(len(levels) - 1, int(round(seconds / hop_seconds))))
        if levels[index] < best_level:
            best_level = levels[index]
            best_frame = frame
    return best_frame


def _original_overlap_error(left: dict[str, Any], right: dict[str, Any], stage: str) -> ValueError:
    left_start, left_end = int(left["original_start_frame"]), int(left["original_end_frame"])
    right_start, right_end = int(right["original_start_frame"]), int(right["original_end_frame"])
    overlap = min(left_end, right_end) - max(left_start, right_start)
    return ValueError(
        "字幕原始对齐范围重叠，无法安全修正边界"
        f"（阶段={stage}；左：轨道={left.get('speaker_track_index', '未知')}，帧={left_start}-{left_end}；"
        f"右：轨道={right.get('speaker_track_index', '未知')}，帧={right_start}-{right_end}；重叠={overlap} 帧）。"
        "请保留此错误中的帧范围和诊断日志以定位冲突。"
    )


def refine_subtitle_boundaries(
    rows: list[dict[str, Any]],
    audio_path: str | Path,
    track_start_frame: int,
    fps: float,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    sample_rate, samples = _read_pcm16(audio_path)
    hop_samples = max(1, int(round(sample_rate * 0.01)))
    levels = _window_rms(samples, hop_samples)
    regions = _speech_regions(levels, sample_rate, hop_samples, fps, track_start_frame)
    search_frames = max(1, int(round(12)))
    output: list[dict[str, Any]] = []
    for raw_row in sorted(rows or [], key=lambda row: (int(row.get("start_frame") or 0), int(row.get("end_frame") or 0))):
        row = dict(raw_row)
        original_start = int(row.get("start_frame") or 0)
        original_end = max(original_start + 1, int(row.get("end_frame") or original_start + 1))
        row["original_start_frame"] = original_start
        row["original_end_frame"] = original_end
        region = _nearest_region(regions, original_start, original_end, search_frames)
        if region is not None:
            # Energy islands often cover only a loud syllable. They may expand,
            # but never crop, the already aligned text span.
            refined_start = max(original_start - search_frames, min(original_start, region[0]))
            refined_end = max(original_end, min(original_end + search_frames, region[1]))
            row["start_frame"] = refined_start
            row["end_frame"] = refined_end
        row["timing_decision"] = "audio_refined" if region is not None else "forced_alignment_kept"
        output.append(row)

    valley_count = 0
    preserved_gaps = 0
    maximum_continuous_gap = max(1, int(round(0.20 * fps)))
    for left, right in zip(output, output[1:]):
        # Decide from pre-refinement bounds, not gaps created by amplitude guesses.
        left_end = int(left["original_end_frame"])
        right_start = int(right["original_start_frame"])
        gap = right_start - left_end
        if gap < 0:
            raise _original_overlap_error(left, right, "音轨边界修正")
        if gap == 0:
            boundary = right_start
            left["end_frame"] = boundary
            right["start_frame"] = boundary
            continue
        if gap <= maximum_continuous_gap:
            boundary = _lowest_energy_frame(
                levels,
                left_end,
                right_start,
                track_start_frame,
                fps,
                hop_samples / sample_rate,
            )
            left["end_frame"] = boundary
            right["start_frame"] = boundary
            left["timing_decision"] = "energy_valley_boundary"
            right["timing_decision"] = "energy_valley_boundary"
            valley_count += 1
        else:
            left["end_frame"] = left_end
            right["start_frame"] = right_start
            preserved_gaps += 1
    return output, {
        "audio_refined_row_count": sum(row["start_frame"] != row["original_start_frame"] or row["end_frame"] != row["original_end_frame"] for row in output),
        "audio_refinement_shrink_rejected_count": 0,
        "energy_valley_boundary_count": valley_count,
        "original_gap_preserved_count": preserved_gaps,
        "confirmed_silence_preserved_count": 0,
    }


def preserve_refined_row_order(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Keep independent audio-track refinements inside the original handoff order."""
    output = sorted(
        [dict(row) for row in rows],
        key=lambda row: (int(row["original_start_frame"]), int(row["original_end_frame"])),
    )
    for row in output:
        original_start = int(row["original_start_frame"])
        original_end = int(row["original_end_frame"])
        row["start_frame"] = min(int(row["start_frame"]), original_start)
        row["end_frame"] = max(int(row["end_frame"]), original_end)
        if row["start_frame"] >= row["end_frame"]:
            row["start_frame"], row["end_frame"] = original_start, original_end
    conflicts = 0
    for left, right in zip(output, output[1:]):
        if int(left["original_end_frame"]) > int(right["original_start_frame"]):
            raise _original_overlap_error(left, right, "跨轨顺序校验")
        different_track = left.get("speaker_track_index") != right.get("speaker_track_index")
        if different_track or left["end_frame"] > right["start_frame"]:
            had_overlap = left["end_frame"] > right["start_frame"]
            left["end_frame"] = min(left["end_frame"], int(left["original_end_frame"]))
            right["start_frame"] = max(right["start_frame"], int(right["original_start_frame"]))
            left["timing_decision"] = right["timing_decision"] = "original_handoff_preserved"
            conflicts += int(had_overlap)
    return output, conflicts


def _affirmation_leads_clause(following: str, previous: str) -> bool:
    if previous.endswith(("对吧", "是吧", "对不对", "是不是", "呢", "吗")):
        return False
    # A repeated statement, an explanation or an instruction can continue
    # the affirmation. A question or a fresh reaction needs its own turn.
    if len(following) < 3 or re.search(r"是不是|[吗呢]$", following):
        return False
    if following.startswith("把") and re.search(r"谁|哪(?:一|个|些|里|儿|位|种)", following):
        return False
    return following == previous or bool(re.match(r"我(?:觉得|认为)|[他她它](?:一定|确实)|把", following))


def _paired_descriptions(text: str, following: str) -> bool:
    return bool(re.fullmatch(r"(.{2})\1", text) and re.fullmatch(r"各.各.|形形色色", following))


def _brief_continuation(text: str, following: str, previous: str, punct: float) -> bool:
    if text == "对":
        # "还没有 / 对 / 还没出现" confirms the preceding answer.
        # A standalone response to another question should remain separate.
        negative = re.match(r"还没|并没|并不|不是|没有", following)
        return bool(negative and negative[0] in previous[-6:]) or (
            0 < punct < 1 and _affirmation_leads_clause(following, previous)
        )
    if punct >= 1 or len(following) < 3:
        return False
    if 0 < punct < 1:
        # A vocative opens the following remark. A repeated pronoun resumes
        # an interrupted subject, e.g. “就是他，他…” .
        if re.fullmatch(r"(?:就是|那|所以)?(?:各位|大家|朋友们|同学们)", text):
            return True
        if re.fullmatch(r"[啊呃嗯]?(?:就是|这个|那个)[我你他她它]", text) and following.startswith(text[-1]):
            return True
        if _paired_descriptions(text, following):
            return True
    if text in {"嗯", "啊", "呃"} or re.fullmatch(r"(?:而且|然后|不过|所以)呢", text):
        return True
    return bool(re.fullmatch(r"[那这]一(?:阵|次|天|年|段(?:时间|日子))", text)
                and following in {"其实是", "其实就是", "就是", "其实也就是"})


def _same_brief_clause(left, right, previous, ordered, cut, fps, corroborated) -> bool:
    before, after = ordered[cut - 1:cut + 1]
    text, following = left["text"], right["text"]
    right_track = right["speaker_track_index"]
    if (before.get("track_index") != left["speaker_track_index"] or after.get("track_index") != right_track
            or not 0 <= after["start_frame"] - before["end_frame"] <= round(.3 * fps)
            or not text.casefold().endswith(before["text"].casefold())
            or not following.casefold().startswith(after["text"].casefold())):
        return False
    if left["speaker_track_index"] == right_track:
        return _brief_continuation(text, following, previous, before.get("asr_punctuation_strength") or 0)
    # A cross-mic “对” needs an uninterrupted recording on the destination
    # mic through at least three following units; a shared boundary word
    # alone cannot distinguish an instruction from another person's reply.
    span = ordered[cut - 1:cut + 3]
    return (text == "对" and _affirmation_leads_clause(following, previous) and len(span) == 4
            and following.startswith("".join(u["text"] for u in span[1:]))
            and corroborated(span, 1, required_track=right_track,
                             witness_gap_limit=round(.2 * fps), seam_punctuation_limit=.55))


def _spoken_leadin_edit(left, right, ordered, cut, cuts, fps, max_chars):
    """Return left text, remaining right text (if any), and an optional aligned handoff."""
    text, following = left["text"], right["text"]
    track = left.get("speaker_track_index")
    if (not track or track != right.get("speaker_track_index") or not 0 < cut < len(ordered)
            or "\n" in text or "\n" in following):
        return None
    before, after = ordered[cut - 1:cut + 1]
    gap = after["start_frame"] - before["end_frame"]
    if (not 0 <= gap <= round(.55 * fps)
            or before.get("track_index") != track or after.get("track_index") != track
            or before["start_frame"] < left.get("original_start_frame", left["start_frame"])
            or after["start_frame"] != right.get("original_start_frame", right["start_frame"])
            or not text.endswith(before["text"]) or not following.startswith(after["text"])
            or bisect_left(cuts, before["end_frame"]) != bisect_left(cuts, after["start_frame"] + 1)):
        return None
    punct = before.get("asr_punctuation_strength") or 0
    # A stopped 把-construction followed by a negative predicate has no object.
    # With an explicit subject, the restarted predicate can follow that
    # subject directly. Drop only a confirmed abandoned 会把, not its subject.
    if (punct >= 1 and re.search(r"(?:会|想|要|打算|准备)把$", text)
            and re.match(r"(?:都|也|其实)(?:不要|不用|不想|不需要)[^的，。！？…]{1,8}(?:就|直接|了$)", following)
            and len(text) + 2 <= max_chars):
        subject = re.fullmatch(r"((?:然后|接着|后来)?[你我他她]们?)会把", text)
        if subject and cut >= 2:
            modal = ordered[cut - 2]
            if ((modal["text"], before["text"]) == ("会", "把") and modal.get("track_index") == track
                    and left["start_frame"] <= modal["start_frame"] < modal["end_frame"] <= before["start_frame"]
                    and all(not u.get("alignment_repaired") and u.get("candidate_kind") != "local_retry"
                            for u in (modal, before, after))):
                return subject[1], following, None
        return text + "……", following, None
    if (punct >= 1 or right["end_frame"] - left["start_frame"] > round(5 * fps)
            or left["end_frame"] - left["start_frame"] > round(1.5 * fps)):
        return None
    if (not punct and gap <= round(.12 * fps)
            and re.fullmatch(r"(?:就)?(?:那时候|那时|当时|原来|以前|现在)?[你我他她]们(?:俩|两个|两个人)", text)
            and re.match(r"(?:其实|当时|原来)?(?:都|也都)", following)):
        joined = text + following
    elif (0 < punct < 1 and round(.2 * fps) <= gap
            and re.fullmatch(r"(?:但是|不过|可是|但)就是", text)
            and re.fullmatch(r"[嗯呃][^，。！？…]+的话", following)
            and after["text"] in ("嗯", "呃") and 0 < (after.get("asr_punctuation_strength") or 0) < 1):
        # Keep the hesitation visible through its pause, then bring in the
        # condition with the spoken filler instead of revealing it ahead of time.
        onset = after["start_frame"]
        # A short hesitation belongs to the following topic. If it lasts
        # long enough to read on its own, leave a clause boundary for display.
        next_word = ordered[cut + 1] if cut + 1 < len(ordered) else None
        short_filler = (next_word and next_word.get("track_index") == track
                        and following[1:].startswith(next_word["text"])
                        and 0 < next_word["start_frame"] - onset < math.ceil(.5 * fps))
        remainder = following[0] + (" " if short_filler else "，") + following[1:]
        if (max(len(text) + 2, len(remainder)) <= max_chars
                and min(onset - left["start_frame"], right["end_frame"] - onset) >= round(.5 * fps)):
            return text + "……", remainder, onset
        return None
    else:
        return None
    return (joined, None, None) if len(joined) <= max_chars else None


def _brief_borrowed_stop(left, right, ordered, cut, fps, corroborated, shots):
    # A mic switch can insert a single other-mic particle, including its
    # full stop, inside a continuous utterance. Preserve the selected words
    # while keeping the two fragments readable as one caption.
    track = left["speaker_track_index"]
    onset = left.get("original_start_frame", left["start_frame"])
    end = right.get("original_end_frame", right["end_frame"])
    if (track != right["speaker_track_index"] or cut < 3 or cut + 2 > len(ordered)
            or right["end_frame"] - right["start_frame"] >= round(.5 * fps)
            or bisect_left(shots, onset + 1) != bisect_left(shots, end)):
        return False
    a, b, particle, c, d = ordered[cut - 3:cut + 2]
    if (particle.get("speaker_decision") != "smoothed_short_flip"
            or particle["text"] not in {"了", "啊", "呢", "吧", "哦"}
            or (particle.get("asr_punctuation_strength") or 0) < 1
            or particle["end_frame"] - particle["start_frame"] > max(1, round(.05 * fps))
            or a["start_frame"] < onset or d["end_frame"] > end
            or not all(u.get("track_index") == track for u in (a, b, particle, c, d))
            or not left["text"].endswith(a["text"] + b["text"] + particle["text"])
            or not right["text"].startswith(c["text"] + d["text"])):
        return False
    return corroborated([a, b, c, d], 2, required_track=track, witness_gap_limit=round(.15 * fps))


def _hold_short_command(reply, command, previous, next_row, last_word, shot, next_shot,
                        starts, spoken_ends, fps):
    # A command starts on the next shot. A few frames of the already
    # displayed final syllable can give the reply a half-second without
    # advancing it by more than a sixth of a second from its real onset.
    new_start = reply["start_frame"]
    if (previous and last_word and previous["end_frame"] == reply["start_frame"]
            and last_word.get("track_index") == previous.get("speaker_track_index")
            and previous["text"].endswith(last_word["text"])):
        onset = reply.get("original_start_frame", reply["start_frame"])
        min_start = max(onset - round(fps / 6), reply["start_frame"] - round(.1 * fps),
                        previous["start_frame"] + round(.8 * fps),
                        last_word["start_frame"] + max(1, round(.06 * fps)))
        if last_word["end_frame"] <= onset:
            new_start = min(new_start, max(min_start, shot - round(.5 * fps)))
    if shot - new_start < round(.5 * fps):
        return False
    if new_start < reply["start_frame"]:
        previous["end_frame"] = reply["start_frame"] = new_start
        previous["timing_decision"] = "picture_cut_handoff"
    if next_row:
        end = command["end_frame"]
        edge = bisect_left(starts, end)
        limit = min(next_row["start_frame"], shot + round(.8 * fps))
        if edge < len(starts):
            limit = min(limit, starts[edge])
        if next_shot is not None:
            limit = min(limit, next_shot)
        if edge and spoken_ends[edge - 1] <= end:
            command["end_frame"] = max(end, limit)
    return True


def _settle_flash_transitions(rows, units, starts, spoken_ends, fps, max_chars, picture_cuts):
    # At normal speed a subtitle change a few frames from a picture cut
    # reads as a second edit. Only reconsider replies still short after the
    # regular pause/reading-time pass.
    cuts = sorted(set(picture_cuts or ()))
    output = []
    floor = round(.5 * fps)
    tolerance = round(.15 * fps)
    for index, right in enumerate(rows):
        left = output[-1] if output else None
        output.append(right)
        if left is None:
            continue
        seam = right["start_frame"]
        gap = seam - left["end_frame"]
        duration = left["end_frame"] - left["start_frame"]
        if (not 0 <= gap <= tolerance or (gap and duration > floor)
                or (not gap and duration >= .45 * fps)):
            continue
        onset = left.get("original_start_frame", left["start_frame"])
        next_onset = right.get("original_start_frame", seam)
        boundary = bisect_left(starts, next_onset)
        first = bisect_left(starts, onset)
        spoken = units[first:boundary]
        if (not spoken or boundary >= len(units) or starts[first] != onset
                or starts[boundary] != next_onset or not left.get("speaker_track_index")
                or not right.get("speaker_track_index") or "".join(u["text"] for u in spoken) != left["text"]
                or not all(u.get("track_index") == left.get("speaker_track_index") for u in spoken)
                or (spoken[-1].get("asr_punctuation_strength") or 0) < 1
                or units[boundary].get("track_index") != right.get("speaker_track_index")
                or not right["text"].casefold().startswith(units[boundary]["text"].casefold())):
            continue

        reaction = re.fullmatch(r"[嗯啊哦噢呃对好是]{1,2}", left["text"])
        if gap:
            # After padding a short reply, a few empty frames before the next
            # onset can blink. Bridge only silent frames on the same shot.
            if (reaction and next_onset == seam and spoken_ends[boundary - 1] <= left["end_frame"]
                    and bisect_left(cuts, left["end_frame"]) == bisect_left(cuts, seam)):
                left["end_frame"] = seam
                left["timing_decision"] = "brief_display_handoff"
            continue

        # Quick confirmations can be read together without attributing the
        # second turn to the first. Keep both lines, and keep a new question
        # in its own cue. Never span a picture edit between them.
        cut_index = bisect_left(cuts, seam - tolerance)
        shot = cuts[cut_index] if cut_index < len(cuts) else None
        if shot is not None and (shot > seam + tolerance or abs(shot - next_onset) > tolerance
                                 or (cut_index + 1 < len(cuts) and cuts[cut_index + 1] <= seam + tolerance)):
            shot = None
        same_mic = left["speaker_track_index"] == right["speaker_track_index"]
        repeated = (left["text"] in {"就是", "是啊", "是的", "对啊", "对的"}
                    and re.fullmatch(r"([对是好嗯])\1+", right["text"])
                    and right["end_frame"] - seam <= round(.8 * fps)
                    and bisect_left(cuts, left["start_frame"] + 1) == bisect_left(cuts, right["end_frame"]))
        if not repeated and (not reaction or shot is None or shot <= left["start_frame"]):
            continue
        # A short reaction straddling an edit can share the new shot with
        # the following sentence, on separate lines. Both onsets must lie
        # within a few frames of the edit.
        previous = output[-3] if len(output) >= 3 else None
        straddles = (reaction and shot is not None and shot < seam - round(.07 * fps)
                     and abs(shot - onset) <= round(.07 * fps) and previous
                     and previous["end_frame"] == left["start_frame"]
                     and re.match(r"[啊哦噢嗯呃]", right["text"])
                     and not re.search(r"[吗呢？?]$|是不是|对不对|怎么|为什么", right["text"])
                     and right["end_frame"] - shot <= round(3 * fps))
        if repeated or straddles:
            if not same_mic or len(left["text"]) + len(right["text"]) > max_chars:
                continue
            display_start = shot if straddles else left["start_frame"]
            if next_onset - display_start > tolerance:
                # Showing both lines here would give away the answer. Allow a
                # small handoff delay only while the first reply syllable is
                # still audible and both captions have enough reading time.
                handoff = max(next_onset, left["start_frame"] + floor)
                if (handoff - next_onset <= round(.08 * fps)
                        and handoff < units[boundary]["end_frame"]
                        and right["end_frame"] - handoff >= floor):
                    left["end_frame"] = right["start_frame"] = handoff
                    left["timing_decision"] = right["timing_decision"] = "spoken_reply_handoff"
                continue
            if straddles:
                left["start_frame"] = previous["end_frame"] = shot
                previous["timing_decision"] = "picture_cut_handoff"
            left.update(text=left["text"] + "\n" + right["text"], end_frame=right["end_frame"],
                        timing_decision="picture_cut_handoff" if straddles else "grouped_turns",
                        segmentation_decision="brief_dialogue_lines")
            if "original_end_frame" in right:
                left["original_end_frame"] = right["original_end_frame"]
            output.pop()
        elif shot >= seam or seam - shot <= round(.07 * fps):
            # Keep a new sentence readable if its onset follows the edit;
            # an earlier edit is only useful for an immediately adjacent command.
            # For an initialism ASR may time each letter separately. Its
            # first letter is not a complete spoken Chinese word.
            first_gone = (shot > seam and shot >= units[boundary]["end_frame"]
                          and not re.match(r"[a-zA-Z]{2}", right["text"]))
            if (first_gone or right["end_frame"] - shot < floor
                    or (shot < seam and right["end_frame"] - seam > round(.7 * fps))):
                continue
            if shot < seam:
                next_cut = cut_index + 1
                held = _hold_short_command(
                    left, right, previous, rows[index + 1] if index + 1 < len(rows) else None,
                    units[first - 1] if first else None, shot,
                    cuts[next_cut] if next_cut < len(cuts) else None, starts, spoken_ends, fps)
                if not held:
                    continue
            left["end_frame"] = right["start_frame"] = shot
            left["timing_decision"] = right["timing_decision"] = "picture_cut_handoff"
    return output


def _audible_restart(prefix, suffix, gap, punct, fps):
    if not 0 <= gap <= round(.55 * fps):
        return False
    # An unfinished 已 followed by a new subject, or a hanging 都 whose
    # predicate restarts after a pause. Preserve the speaker's first attempt.
    if prefix.endswith("已") and punct >= .5 and v4._resumed_clause(prefix, suffix):
        return True
    if (prefix.endswith("甚至都") and gap >= round(.3 * fps)
            and re.match(r"一[\u4e00-\u9fff]{1,3}都.", suffix)):
        return True
    # A comparison with one's routine gives way to a fresh emphatic clause.
    subject = re.search(r"(?:像|比如)([我你他她]们?)(?:平时|平常|平日|以前)$", prefix)
    return bool(subject and re.match(subject[1] + r"(?:根本|压根|从来)(?:就)?[不没]", suffix))


def _split_paused_clauses(rows, units, starts, cuts, fps, max_chars):
    """Reveal a held answer on its first syllable and carry an unfinished subject through silence."""
    floor = max(1, math.ceil(fps / 2))
    output = []
    for i, row in enumerate(rows):
        output.append(row)
        start, end, text = row["start_frame"], row["end_frame"], row["text"]
        first, stop = bisect_left(starts, start), bisect_left(starts, end)
        spoken = units[first:stop]
        if ("\n" in text or len(spoken) < 2 or end - start < floor
                or any(u.get("alignment_repaired") or u.get("candidate_kind") == "local_retry"
                       for u in spoken)):
            continue

        prefix = ""
        for j, (before, after) in enumerate(zip(spoken, spoken[1:]), 1):
            prefix += before["text"]
            if not text.startswith(prefix):
                break
            suffix = text[len(prefix):]
            onset = after["start_frame"]
            gap = onset - before["end_frame"]
            punct = before.get("asr_punctuation_strength") or 0
            restart_suffix = suffix.lstrip(" …")
            restart = (before.get("track_index") == after.get("track_index")
                       and _audible_restart(prefix, restart_suffix, gap, punct, fps))
            if restart:
                suffix = restart_suffix
            if not suffix.startswith(after["text"]) or onset - start < floor:
                continue
            hold_to = end
            left, right = prefix, suffix
            long_stop = gap >= round(.6 * fps) and punct >= 1
            contrast = long_stop and prefix in {"但是", "可是", "不过", "然而"}
            duration = long_stop and re.match(v4._DURATION, suffix) and v4._hesitant_complement(prefix, suffix)
            valuation = (gap >= round(.33 * fps) and punct >= .5 and re.match(
                r"(?:就是|也就|只要|才|是)[零一二三四五六七八九十百千万两点\d.]+(?:块|元|角|毛|美元|欧元)", suffix))
            if contrast:
                left += "……"
            elif (re.search(r"(?:的时候|那一年|那段时间)$", prefix)
                    and re.fullmatch(r"(?:就)?是[我你他她](?:们)?", suffix) and i + 1 < len(rows)):
                # The predicate follows after a silence. Keep its subject on
                # screen during that silence, without exposing the predicate early.
                next_row = rows[i + 1]
                hold_to = next_row["start_frame"]
                track = after.get("track_index")
                if (not round(.6 * fps) <= hold_to - end <= round(1.8 * fps)
                        or not re.match(r"最.{1,8}的(?:时候|日子|时光|时期|阶段)", next_row["text"])
                        or gap < 0
                        or not track or next_row.get("speaker_track_index") != track
                        or stop >= len(units) or starts[stop] != hold_to
                        or units[stop].get("track_index") != track
                        or not next_row["text"].startswith(units[stop]["text"])
                        or bisect_left(cuts, end) != bisect_left(cuts, hold_to)):
                    continue
                right += "……"
            elif not (duration or valuation or restart):
                continue
            left_track, right_track = before.get("track_index"), after.get("track_index")
            spoken_suffix = "".join(u["text"] for u in spoken[j:])
            if (hold_to - onset < floor or suffix not in (spoken_suffix, textnorm.normalize_subtitle_text(spoken_suffix))
                    or not left_track or not right_track or right_track != row.get("speaker_track_index")
                    or max(len(left), len(right)) > max_chars
                    or any(u.get("track_index") != left_track for u in spoken[:j])
                    or any(u.get("track_index") != right_track for u in spoken[j:])):
                continue
            # A clipped word such as 已 cannot stand as the completed clause.
            # Keep the false start visible while its speaker supplies the restart.
            if restart and prefix.endswith("已"):
                right = prefix + " " + suffix
                if len(right) > max_chars:
                    continue
            output[-1:] = [
                {**row, "text": left, "end_frame": onset, "original_end_frame": before["end_frame"],
                 "speaker_track_index": left_track, "segmentation_decision": "paused_clause_handoff"},
                {**row, "text": right, "start_frame": onset, "original_start_frame": onset,
                 "end_frame": hold_to, "segmentation_decision": "paused_clause_handoff"},
            ]
            break
    return output


def stabilize_brief_cues(
    rows: list[dict[str, Any]], units: list[dict[str, Any]], fps: float, max_chars: int,
    candidates: list[dict[str, Any]] | None = None, *, timeline_end_frame: int | None = None,
    picture_cuts: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Keep brief replies readable and attach short lead-ins to the same voice."""
    output = []
    ordered = sorted(units, key=lambda u: (u["start_frame"], u["end_frame"]))
    starts = [u["start_frame"] for u in ordered]
    corroborated = v4._aligned_phrase_witnesses(candidates or [], fps)
    shots = sorted(set(picture_cuts or ()))
    for original in rows:
        right = dict(original)
        if not output:
            output.append(right)
            continue
        left = output[-1]
        text, following = left["text"], right["text"]
        duration = left["end_frame"] - left["start_frame"]
        gap = right["start_frame"] - left["end_frame"]
        track = left.get("speaker_track_index")
        cut = bisect_left(starts, right.get("original_start_frame", right["start_frame"]))
        right_track = right.get("speaker_track_index")
        edit = _spoken_leadin_edit(left, right, ordered, cut, shots, fps, max_chars)
        joined = None
        if edit:
            edited_text, remainder, handoff = edit
            if remainder is not None:
                left["text"], right["text"] = edited_text, remainder
                if handoff is not None:
                    left["end_frame"] = right["start_frame"] = handoff
                    left["timing_decision"] = right["timing_decision"] = "spoken_leadin_handoff"
                output.append(right)
                continue
            joined = edited_text
        paired_modifiers = (_paired_descriptions(text, following) and duration <= round(fps)
                            and right["end_frame"] - right["start_frame"] <= round(.5 * fps))
        if (joined is None and track and right_track and 0 <= gap <= round(.2 * fps)
                and (duration <= round(.7 * fps) or paired_modifiers) and len(text + following) <= max_chars
                and right["end_frame"] - left["start_frame"] <= round(6 * fps)
                and 0 < cut < len(ordered)):
            nearby = len(output) >= 2 and left["start_frame"] - output[-2]["end_frame"] <= round(.3 * fps)
            previous = output[-2]["text"] if nearby else ""
            separator = ""
            if (len(text + following) < max_chars
                    and _brief_borrowed_stop(left, right, ordered, cut, fps, corroborated, shots)):
                separator = "，"
            if separator or _same_brief_clause(left, right, previous, ordered, cut, fps, corroborated):
                joined = text + separator + following
        if joined is not None:
            left["speaker_track_index"] = right_track
            left["text"] = joined
            left["end_frame"] = right["end_frame"]
            if "original_end_frame" in right:
                left["original_end_frame"] = right["original_end_frame"]
            left["segmentation_decision"] = "brief_clause_join"
            continue
        output.append(right)

    # Fit brief captions after joins, when all neighboring display limits are known.
    targets = [round(fps * min(.8, max(.5, len(v4.normalize_text(r["text"])) / 10)))
               for r in output]
    spoken_ends = list(accumulate((u["end_frame"] for u in ordered), max))
    for i, row in enumerate(output):
        start, end = row["start_frame"], row["end_frame"]
        missing = targets[i] - (end - start)
        if missing <= 0:
            continue
        limit = output[i + 1]["start_frame"] if i + 1 < len(output) else timeline_end_frame
        if limit is not None:
            # Leave the next onset intact, even for words absent from the captions.
            cut = bisect_left(starts, end)
            if cut < len(starts):
                limit = min(limit, starts[cut])
            if cut and spoken_ends[cut - 1] <= end:
                extra = min(missing, max(0, limit - end))
                if extra:
                    row["end_frame"] += extra
                    row["timing_decision"] = "brief_display_extension"
                    missing -= extra
        if not missing or not i:
            continue

        # A preceding caption sometimes displays past its last syllable. Use
        # at most a sixth of a second of confirmed air before this onset.
        onset = row.get("original_start_frame", start)
        cut = bisect_left(starts, onset)
        if not 0 < cut < len(ordered):
            continue
        previous = output[i - 1]
        last, first = ordered[cut - 1], ordered[cut]
        if (first["start_frame"] != onset or not first.get("track_index")
                or first["track_index"] != row.get("speaker_track_index")
                or last.get("track_index") != previous.get("speaker_track_index")
                or last["start_frame"] < previous.get("original_start_frame", previous["start_frame"])
                or last["end_frame"] > previous["end_frame"]
                or not row["text"].casefold().startswith(first["text"].casefold())
                or not previous["text"].casefold().endswith(last["text"].casefold())):
            continue
        # Preserve both aligned speech and the preceding caption's own reading time.
        earliest = max(spoken_ends[cut - 1], onset - round(fps / 6),
                       previous["start_frame"] + targets[i - 1])
        if (previous.get("timing_decision") == "audio_refined"
                and previous["end_frame"] > previous.get("original_end_frame", previous["end_frame"])):
            earliest = max(earliest, previous["end_frame"])
        new_start = max(earliest, start - missing)
        if new_start < start:
            if previous["end_frame"] > new_start:
                previous["end_frame"] = new_start
                previous["timing_decision"] = "brief_display_handoff"
            row["start_frame"] = new_start
            row["timing_decision"] = "brief_display_extension"
    settled = _settle_flash_transitions(output, ordered, starts, spoken_ends, fps, max_chars, shots)
    return _split_paused_clauses(settled, ordered, starts, shots, fps, max_chars)


def shadow_refine_subtitle_boundaries(
    rows: list[dict[str, Any]],
    audio_path: str | Path,
    track_start_frame: int,
    fps: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    original_rows = [
        dict(row)
        for row in sorted(
            rows or [],
            key=lambda row: (int(row.get("start_frame") or 0), int(row.get("end_frame") or 0)),
        )
    ]
    suggested_rows, suggested = refine_subtitle_boundaries(
        original_rows,
        audio_path,
        track_start_frame,
        fps,
    )
    output: list[dict[str, Any]] = []
    for original, candidate in zip(original_rows, suggested_rows, strict=True):
        row = dict(original)
        row["suggested_start_frame"] = int(candidate.get("start_frame") or row.get("start_frame") or 0)
        row["suggested_end_frame"] = int(candidate.get("end_frame") or row.get("end_frame") or 0)
        row["timing_decision"] = "forced_alignment_kept_shadow"
        output.append(row)
    return output, {
        "audio_refinement_mode": "shadow",
        "audio_refined_row_count": 0,
        "audio_refinement_suggested_row_count": int(suggested.get("audio_refined_row_count") or 0),
        "energy_valley_boundary_count": 0,
        "audio_refinement_suggested_energy_valley_count": int(suggested.get("energy_valley_boundary_count") or 0),
        "confirmed_silence_preserved_count": 0,
        "audio_refinement_suggested_silence_count": int(suggested.get("confirmed_silence_preserved_count") or 0),
    }
