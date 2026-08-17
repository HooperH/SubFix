from __future__ import annotations

import difflib
import importlib.util
import math
import re
import wave
from array import array
from pathlib import Path
from typing import Any

try:
    import subfix_generate_v4 as v4
except ModuleNotFoundError as exc:
    if exc.name != "subfix_generate_v4":
        raise
    _v4_path = Path(__file__).resolve().with_name("subfix_generate_v4.py")
    _v4_spec = importlib.util.spec_from_file_location("subfix_generate_v4", _v4_path)
    if _v4_spec is None or _v4_spec.loader is None:
        raise RuntimeError("v4 生成模块无法加载")
    v4 = importlib.util.module_from_spec(_v4_spec)
    _v4_spec.loader.exec_module(v4)


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
        raise RuntimeError(f"v5 缺少 {mode} 断句模型，请切回 v4 或选择已有训练数据的模式")
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
    refined_count = 0
    shrink_rejected_count = 0
    for raw_row in sorted(rows or [], key=lambda row: (int(row.get("start_frame") or 0), int(row.get("end_frame") or 0))):
        row = dict(raw_row)
        original_start = int(row.get("start_frame") or 0)
        original_end = max(original_start + 1, int(row.get("end_frame") or original_start + 1))
        row["original_start_frame"] = original_start
        row["original_end_frame"] = original_end
        region = _nearest_region(regions, original_start, original_end, search_frames)
        if region is not None:
            refined_start = max(original_start - search_frames, min(original_start + search_frames, region[0]))
            refined_end = max(refined_start + 1, max(original_end - search_frames, min(original_end + search_frames, region[1])))
            original_duration = original_end - original_start
            refined_duration = refined_end - refined_start
            minimum_duration = min(original_duration, max(2, int(round(0.35 * fps))))
            if refined_duration < minimum_duration or refined_duration * 2 < original_duration:
                row["timing_decision"] = "refinement_rejected_too_short"
                shrink_rejected_count += 1
                output.append(row)
                continue
            row["start_frame"] = refined_start
            row["end_frame"] = refined_end
            if refined_start != original_start or refined_end != original_end:
                refined_count += 1
        row["timing_decision"] = "audio_refined" if region is not None else "forced_alignment_kept"
        output.append(row)

    valley_count = 0
    preserved_silence = 0
    maximum_continuous_gap = max(1, int(round(0.20 * fps)))
    for left, right in zip(output, output[1:]):
        left_end = int(left.get("end_frame") or 0)
        right_start = int(right.get("start_frame") or 0)
        gap = right_start - left_end
        if gap <= 0:
            boundary = min(right_start, max(int(left.get("start_frame") or 0) + 1, (left_end + right_start) // 2))
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
            preserved_silence += 1
    return output, {
        "audio_refined_row_count": refined_count,
        "audio_refinement_shrink_rejected_count": shrink_rejected_count,
        "energy_valley_boundary_count": valley_count,
        "confirmed_silence_preserved_count": preserved_silence,
    }


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
