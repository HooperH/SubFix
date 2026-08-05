#!/usr/bin/env python3
"""Local alignment helper for SubFix.

Outputs a small JSON payload with segment-level start/end/text. The default
path force-aligns existing SubFix subtitle rows with stable-ts, while fixture
mode avoids heavyweight imports for tests.
"""

from __future__ import annotations

import argparse
import base64
import difflib
import importlib.util
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
import urllib.error
import urllib.request
import uuid
import wave
from collections import Counter
from pathlib import Path
from typing import Any


class _LazyGenerateV4:
    _module: Any = None

    def _load(self) -> Any:
        if self._module is not None:
            return self._module
        module_path = Path(__file__).resolve().with_name("subfix_generate_v4.py")
        if not module_path.is_file():
            raise RuntimeError(f"v4 生成模块缺失: {module_path.name}")
        spec = importlib.util.spec_from_file_location("subfix_generate_v4", module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("v4 生成模块无法加载")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._module = module
        return module

    def __getattr__(self, name: str) -> Any:
        return getattr(self._load(), name)


generate_v4 = _LazyGenerateV4()


class _LazyGenerateV5:
    _module: Any = None

    def _load(self) -> Any:
        if self._module is not None:
            return self._module
        module_path = Path(__file__).resolve().with_name("subfix_generate_v5.py")
        if not module_path.is_file():
            raise RuntimeError(f"v5 生成模块缺失: {module_path.name}")
        spec = importlib.util.spec_from_file_location("subfix_generate_v5", module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("v5 生成模块无法加载")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._module = module
        return module

    def __getattr__(self, name: str) -> Any:
        return getattr(self._load(), name)


generate_v5 = _LazyGenerateV5()


class _LazyGenerateTextnorm:
    _module: Any = None

    def _load(self) -> Any:
        if self._module is not None:
            return self._module
        module_path = Path(__file__).resolve().with_name("subfix_generate_textnorm.py")
        if not module_path.is_file():
            raise RuntimeError(f"数字与格式规范化模块缺失: {module_path.name}")
        spec = importlib.util.spec_from_file_location("subfix_generate_textnorm", module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError("数字与格式规范化模块无法加载")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self._module = module
        return module

    def __getattr__(self, name: str) -> Any:
        return getattr(self._load(), name)


generate_textnorm = _LazyGenerateTextnorm()


DEFAULT_MODEL = "small"
DEFAULT_CTC_MODEL = "jonatasgrosman/wav2vec2-large-xlsr-53-chinese-zh-cn"
HELPER_VERSION = "subfix-2026-07-13-candidate-arbitration-v5"
TRANSCRIBE_BACKENDS = ("mimo_asr", "qwen3_asr", "mlx_whisper", "openai_whisper", "doubao_asr")
# doubao_asr is a paid, credential-gated backend (Volcano Engine). It must
# never be silently invoked by the "auto" fallback chain -- default behavior
# stays exactly as before (auto -> qwen3 first). Only an explicit
# `--backend doubao_asr` CLI flag enables it (Lua UI still always passes
# "auto" this round; see transcribe_with_backend below).
AUTO_TRANSCRIBE_BACKENDS = tuple(name for name in TRANSCRIBE_BACKENDS if name != "doubao_asr")
# NOTE: GENERATED_SUBTITLE_MAX_CHARS and friends below drive the legacy v3
# rule-based splitter (split_generated_subtitle_text/_clause,
# generate_subtitle_rows_from_segments). The user-facing "字幕长度"
# (--max-chars) option only overrides the v4/v5 main segmentation path
# (subfix_generate_v4.segment_canonical_units / _segmentation_length_parameters);
# the v3 path intentionally keeps its own fixed constants untouched.
GENERATED_SUBTITLE_MAX_CHARS = 18
GENERATED_SUBTITLE_PREFERRED_MIN_CHARS = 8
GENERATED_SUBTITLE_PREFERRED_MAX_CHARS = 14
GENERATED_SUBTITLE_MIN_TAIL_CHARS = 7
GENERATED_SUBTITLE_SEMANTIC_COMMA_MIN_CHARS = 4
GENERATED_SUBTITLE_COMMON_WORD_BOUNDARY_BIGRAMS = {
    "一个",
    "一些",
    "什么",
    "价格",
    "厉害",
    "可以",
    "嘲笑",
    "平台",
    "怎么",
    "意思",
    "硬件",
    "绝对",
    "视频",
    "这个",
    "这些",
    "那个",
    "那些",
    "产品",
    "把它",
    "块钱",
}
GENERATED_SUBTITLE_WORD_GAP_BREAK_SECONDS = 0.65
GENERATED_SUBTITLE_HARD_SILENCE_SECONDS = 0.45
GENERATED_SUBTITLE_VAD_ONLY_HARD_SILENCE_SECONDS = 0.80
GENERATED_SUBTITLE_WORD_GAP_CONSENSUS_SECONDS = 0.18
GENERATED_SUBTITLE_WORD_GAP_MIN_LEFT_CHARS = 2
GENERATED_SUBTITLE_WORD_GAP_MIN_RIGHT_CHARS = 2
GENERATED_SUBTITLE_LEARNED_MERGE_GAP_SECONDS = 0.45
SEGMENTATION_PROFILE_SCHEMA = "subfix_segmentation_profile_v2"
LEGACY_SEGMENTATION_PROFILE_SCHEMA = "subfix_segmentation_profile_v1"
SEGMENTATION_PROFILE_ENV = "SUBFIX_SEGMENTATION_PROFILE"
V5_WRITEBACK_ENV = "SUBFIX_V5_WRITEBACK"
GENERATED_SUBTITLE_MIN_DURATION_SECONDS = 0.55
GENERATED_SUBTITLE_SECONDS_PER_CHAR = 0.22
GENERATED_SUBTITLE_MAX_DURATION_SECONDS = 2.6
GENERATE_SUBTITLES_BATCH_MAX_SECONDS = 45.0
LIVE_SPEAKER_ACTIVE_MARGIN_DB = 6.0
LIVE_SPEAKER_SWITCH_MARGIN_DB = 3.0
LIVE_SPEAKER_SWITCH_HOLD_SECONDS = 0.12
EXTERNAL_TRANSCRIBE_COMMAND_ENV = {
    "mimo_asr": "SUBFIX_MIMO_ASR_CMD",
}
# Volcano Engine (豆包/Doubao) 大模型录音文件极速版识别 backend. Its response
# carries word timestamps in milliseconds; normalize them to seconds so v4/v5
# can use them directly and reserve Qwen forced alignment for bad/missing data.
#
# Field names / endpoint below follow
# https://docs.volcengine.com/docs/6561/1631584 (fetched 2026-07-16). Marked
# "待校准" (to calibrate) where the public docs sample didn't fully pin the
# field down; verify against a real account response before relying on it.
DOUBAO_ASR_API_KEY_ENV = "SUBFIX_DOUBAO_API_KEY"
DOUBAO_ASR_RESOURCE_ID_ENV = "SUBFIX_DOUBAO_RESOURCE_ID"
DOUBAO_ASR_ENDPOINT_ENV = "SUBFIX_DOUBAO_ENDPOINT"
# Optional credentials file read as a fallback when the env vars are unset.
# It sits next to this helper (in the deployed plugin that is
# .subfix_support/doubao_credentials.json). Configuration accepts only
# "api_key". The real file is gitignored; a doubao_credentials.json.example
# template is tracked.
DOUBAO_ASR_CREDENTIALS_FILENAME = "doubao_credentials.json"
# "录音文件极速版识别" (flash/turbo tier) HTTP endpoint -- synchronous, no
# submit/query polling needed per the docs.
DOUBAO_ASR_DEFAULT_ENDPOINT = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
# Resource id required for the flash/turbo tier (different from the
# standard-tier "volc.bigasr.auc"); must be enabled for the account.
DOUBAO_ASR_DEFAULT_RESOURCE_ID = "volc.bigasr.auc_turbo"
DOUBAO_ASR_TIMEOUT_SECONDS = 30.0
DOUBAO_ASR_MAX_ATTEMPTS = 2
DOUBAO_ASR_RETRY_BACKOFF_SECONDS = 0.5
# 待校准: 文档 demo 中响应头 X-Api-Status-Code 的成功值；响应体 JSON 内未见到顶层
# code/message 字段（区别于旧版录音文件标准版接口）。
DOUBAO_ASR_SUCCESS_STATUS_CODE = "20000000"
QWEN3_ASR_MODEL = "Qwen/Qwen3-ASR-1.7B"
QWEN3_FORCED_ALIGNER_MODEL = "Qwen/Qwen3-ForcedAligner-0.6B"
QWEN3_CPP_BIN_ENV = "SUBFIX_QWEN3_ASR_CPP_BIN"
QWEN3_CPP_ALIGNER_GGUF_ENV = "SUBFIX_QWEN3_ALIGNER_GGUF"
QWEN3_FORCED_ALIGNER_MODEL_NAMES = (
    "qwen3-forced-aligner-0.6b-f16.gguf",
    "qwen3-forced-aligner-0.6b-q8_0.gguf",
    "qwen3-forced-aligner-0.6b-q5_0.gguf",
    "qwen3-forced-aligner-0.6b-q4_k.gguf",
)
QWEN_ROW_REMAP_MIN_SCORE = 0.86
QWEN_ROW_REMAP_MAX_START_LOOKBACK = 2
QWEN_ROW_REMAP_MAX_START_LOOKAHEAD = 12
DEFAULT_FFMPEG_CANDIDATES = (
    os.path.expanduser("~/.local/bin/ffmpeg"),
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
)
_QWEN3_ASR_MODEL_CACHE: dict[tuple[str, str, str, str | None], tuple[Any, str, str]] = {}
_QWEN3_FORCED_ALIGNER_CACHE: dict[tuple[str, str, str], Any] = {}
_OPENAI_WHISPER_MODEL_CACHE: dict[str, Any] = {}


def generate_subtitles_batch_max_seconds() -> float:
    raw_value = os.getenv("SUBFIX_GENERATE_BATCH_MAX_SECONDS")
    if raw_value:
        try:
            return max(1.0, float(raw_value))
        except ValueError:
            pass
    return GENERATE_SUBTITLES_BATCH_MAX_SECONDS


def v5_writeback_mode() -> str:
    mode = str(os.getenv(V5_WRITEBACK_ENV) or "shadow").strip().lower()
    return mode if mode in {"live", "shadow"} else "shadow"


def write_payload(path: Path, payload: dict[str, Any]) -> None:
    payload.setdefault("helper_version", HELPER_VERSION)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with temporary_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def sanitize_generate_diagnostic_payload(payload: dict[str, Any]) -> dict[str, Any]:
    diagnostic_keys = {
        "subtitle_mode",
        "live_engine",
        "source_batch_count",
        "batch_count",
        "successful_batch_count",
        "failed_batch_count",
        "generated_subtitle_count",
        "activity_track_count",
        "activity_item_count",
        "activity_skipped_item_count",
        "speech_island_count",
        "confirmed_silence_count",
        "vad_only_gap_count",
        "speaker_turn_count",
        "speaker_switch_count",
        "bleed_rejected_count",
        "ambiguous_window_count",
        "ambiguous_island_count",
        "overlap_suppressed_count",
        "duplicate_suppressed_count",
        "short_fragment_merged_count",
        "short_utterance_kept_count",
        "short_utterance_dropped_count",
        "speech_island_timing_anchor_count",
        "forced_silence_break_row_count",
        "learned_segmentation_row_count",
        "learned_text_correction_count",
        "legacy_passthrough_row_count",
        "live_fallback_no_word_timing",
        "live_fallback_no_activity",
        "qwen_batch_used",
        "segmentation_profile_used",
        "segmentation_profile_schema",
        "generate_engine",
        "track_composite_count",
        "context_window_count",
        "asr_empty_speech_window_count",
        "asr_single_retry_count",
        "asr_subwindow_retry_count",
        "asr_recovered_window_count",
        "asr_unrecovered_window_count",
        "forced_align_retry_count",
        "context_align_retry_count",
        "alignment_repaired_unit_count",
        "minimum_raw_alignment_coverage",
        "unaligned_rejected_count",
        "alignment_window_alternative_count",
        "alignment_window_uncovered_count",
        "canonical_unit_count",
        "forced_aligned_unit_coverage",
        "cross_mic_echo_region_count",
        "cross_mic_echo_suppressed_count",
        "cross_mic_echo_ambiguous_count",
        "cross_mic_echo_suppressed_unit_count",
        "cross_mic_duplicate_count",
        "short_speaker_flip_suppressed_count",
        "viterbi_source_switch_count",
        "ordinary_single_character_count",
        "ordinary_single_character_suppressed_count",
        "short_reply_suppressed_count",
        "single_unit_merged_count",
        "incomplete_clause_merged_count",
        "overlap_candidate_suppressed_count",
        "window_seam_suppressed_count",
        "cross_mic_boundary_serialized_count",
        "structural_duplicate_suppressed_count",
        "near_duplicate_suppressed_count",
        "orphan_segment_merged_count",
        "short_gap_filled_count",
        "text_conservation_failed_count",
        "hard_boundary_count",
        "overlap_disagreement_count",
        "local_retry_region_count",
        "local_retry_elapsed_seconds",
        "local_retry_selected_count",
        "local_retry_proposed_count",
        "local_retry_writeback_mode",
        "low_confidence_region_count",
        "local_retry_alignment_failed_count",
        "audio_refined_row_count",
        "audio_refinement_mode",
        "audio_refinement_suggested_row_count",
        "audio_refinement_suggested_energy_valley_count",
        "audio_refinement_suggested_silence_count",
        "energy_valley_boundary_count",
        "confirmed_silence_preserved_count",
        "overlong_tail_reclaimed_count",
        "tail_extended_row_count",
        "textnorm_changed_row_count",
        "profile_load_status",
        "failure_code",
        "asr_backend_used",
        "doubao_fallback_count",
        "doubao_native_timestamp_window_count",
        "doubao_native_timestamp_fallback_count",
        "doubao_fallback_errors",
        "doubao_log_ids",
        "doubao_resource_ids",
        "doubao_status_codes",
    }
    row_keys = {
        "index",
        "start_frame",
        "end_frame",
        "text",
        "track_index",
        "speaker_track_index",
        "speaker_score_db",
        "speaker_dominance_db",
        "speaker_decision",
        "speech_island_id",
        "short_utterance_confident",
        "timing_anchor_source",
        "segmentation_decision",
        "forced_silence_break",
        "text_correction_count",
        "original_start_frame",
        "original_end_frame",
        "suggested_start_frame",
        "suggested_end_frame",
        "timing_decision",
    }
    raw_diagnostic = payload.get("diagnostic") if isinstance(payload.get("diagnostic"), dict) else {}
    diagnostic = {key: raw_diagnostic[key] for key in diagnostic_keys if key in raw_diagnostic}
    raw_stages = raw_diagnostic.get("stages") if isinstance(raw_diagnostic.get("stages"), dict) else {}
    stage_keys = {
        "track_index",
        "window_index",
        "start_frame",
        "end_frame",
        "text",
        "source_score",
        "speaker_score_db",
        "speaker_decision",
        "boundary_probability",
        "alignment_coverage",
        "raw_alignment_coverage",
        "alignment_repaired",
        "asr_recovery",
        "speech_seconds",
        "asr_punctuation_strength",
        "independent_vad",
        "candidate_kind",
        "candidate_decision",
        "candidate_score",
        "retry_reason",
    }
    stages: dict[str, list[dict[str, Any]]] = {}
    for stage_name in ("window_asr", "aligned_units", "source_selection", "segmentation"):
        rows = raw_stages.get(stage_name) if isinstance(raw_stages.get(stage_name), list) else []
        stages[stage_name] = [
            {key: row[key] for key in stage_keys if key in row}
            for row in rows
            if isinstance(row, dict)
        ]
    if any(stages.values()):
        diagnostic["stages"] = stages
    subtitle_rows = [
        {key: row[key] for key in row_keys if key in row}
        for row in payload.get("subtitle_rows") or []
        if isinstance(row, dict)
    ]
    batch_summaries = []
    for batch in payload.get("batches") or []:
        if not isinstance(batch, dict):
            continue
        batch_diagnostic = batch.get("diagnostic") if isinstance(batch.get("diagnostic"), dict) else {}
        batch_summaries.append(
            {
                "ok": batch.get("ok") is True,
                "track_index": int(batch.get("track_index") or 0),
                "diagnostic": {
                    key: batch_diagnostic[key]
                    for key in diagnostic_keys | {"segment_count", "generated_subtitle_count", "item_count", "window_count"}
                    if key in batch_diagnostic
                },
            }
        )
    diagnostic_schema = (
        generate_v5.DIAGNOSTIC_SCHEMA
        if str(raw_diagnostic.get("generate_engine") or "").lower() == "v5"
        else "subfix_generate_diagnostic_v2"
    )
    return {
        "schema_version": diagnostic_schema,
        "helper_version": HELPER_VERSION,
        "generated_at": time.time(),
        "ok": payload.get("ok") is True,
        "backend": str(payload.get("backend") or ""),
        "model": Path(str(payload.get("model") or "")).name if "/" in str(payload.get("model") or "") else str(payload.get("model") or ""),
        "subtitle_mode": str(raw_diagnostic.get("subtitle_mode") or "narration"),
        "diagnostic": diagnostic,
        "batches": batch_summaries,
        "subtitle_rows": subtitle_rows,
    }


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


def generate_batch_progress_total(total_batches: int) -> int:
    return max(1, int(total_batches)) * 2 + 2


def v4_progress_point(start: int, end: int, completed: int, total: int) -> int:
    if total <= 0:
        return int(start)
    fraction = max(0.0, min(1.0, float(completed) / float(total)))
    return int(round(float(start) + (float(end) - float(start)) * fraction))


def generate_helper_completion_progress(diagnostic: dict[str, Any]) -> tuple[int, int]:
    if str(diagnostic.get("generate_engine") or "").lower() in {"v4", "v5"}:
        return 95, 100
    total = generate_batch_progress_total(int(diagnostic.get("batch_count") or 0))
    return total, total


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
                if word_end <= word_start:
                    word_end = word_start + 0.001
                if word_text:
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


def strip_generated_subtitle_punctuation(text: str) -> str:
    value = str(text or "")
    value = re.sub(r"[，。！？、；：,.!?;:\"'“”‘’（）()《》【】\[\]{}<>…·/\\|]+", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def generated_subtitle_effective_length(text: str) -> int:
    return len(normalize_ctc_text(text))


def generated_subtitle_right_side_until_hard_break(text: str) -> str:
    hard_breaks = "。！？!?；;"
    chars: list[str] = []
    for char in str(text or ""):
        chars.append(char)
        if char in hard_breaks:
            break
    return "".join(chars)


def generated_subtitle_starts_with_orphan_particle(text: str) -> bool:
    value = re.sub(r"^[\s，、,：:。！？!?；;]+", "", str(text or ""))
    return bool(value) and value[0] in "了的呢吧吗啊呀嘛啦"


def should_split_generated_subtitle_semantic_comma(left_text: str, right_text: str) -> bool:
    right_clause = generated_subtitle_right_side_until_hard_break(right_text)
    if generated_subtitle_starts_with_orphan_particle(right_clause):
        return False
    return (
        generated_subtitle_effective_length(left_text) >= GENERATED_SUBTITLE_SEMANTIC_COMMA_MIN_CHARS
        and generated_subtitle_effective_length(right_clause) >= GENERATED_SUBTITLE_SEMANTIC_COMMA_MIN_CHARS
    )


def rebalance_generated_subtitle_short_tail(
    chunks: list[str],
    min_tail_chars: int = GENERATED_SUBTITLE_MIN_TAIL_CHARS,
    max_chars: int = GENERATED_SUBTITLE_MAX_CHARS,
) -> list[str]:
    if len(chunks) < 2:
        return chunks

    output = chunks[:]
    if not strip_generated_subtitle_punctuation(output[-1]):
        return output

    while (
        generated_subtitle_text_length(output[-1]) < min_tail_chars
        and generated_subtitle_text_length(output[-2]) > min_tail_chars
        and generated_subtitle_text_length(output[-1]) < max_chars
    ):
        previous = output[-2].rstrip()
        if not previous:
            break
        output[-2] = previous[:-1].rstrip()
        output[-1] = (previous[-1] + output[-1]).strip()

    return [chunk for chunk in output if chunk]


def should_rebalance_generated_subtitle_row_tail(
    previous_text: str,
    current_text: str,
    min_tail_chars: int = GENERATED_SUBTITLE_MIN_TAIL_CHARS,
    max_chars: int = GENERATED_SUBTITLE_MAX_CHARS,
) -> bool:
    previous_length = generated_subtitle_text_length(previous_text)
    current_length = generated_subtitle_text_length(current_text)
    if not previous_text or not current_text or current_length >= min_tail_chars:
        return False
    return (
        previous_length > min_tail_chars
        and previous_text[-1] + current_text[0] in GENERATED_SUBTITLE_COMMON_WORD_BOUNDARY_BIGRAMS
    )


def rebalance_generated_subtitle_row_short_tails(
    rows: list[dict[str, Any]],
    min_tail_chars: int = GENERATED_SUBTITLE_MIN_TAIL_CHARS,
    max_chars: int = GENERATED_SUBTITLE_MAX_CHARS,
    max_gap_frames: int = 12,
) -> list[dict[str, Any]]:
    if len(rows) < 2:
        return rows

    output = [dict(row) for row in rows]
    for index in range(1, len(output)):
        previous = output[index - 1]
        current = output[index]
        previous_text = str(previous.get("text") or "").rstrip()
        current_text = str(current.get("text") or "").strip()
        previous_length = generated_subtitle_text_length(previous_text)
        current_length = generated_subtitle_text_length(current_text)
        frame_gap = int(current.get("start_frame") or 0) - int(previous.get("end_frame") or 0)
        previous_speaker = previous.get("speaker_track_index")
        current_speaker = current.get("speaker_track_index")
        previous_island = str(previous.get("speech_island_id") or "")
        current_island = str(current.get("speech_island_id") or "")
        if (
            previous_speaker is not None
            and current_speaker is not None
            and int(previous_speaker) != int(current_speaker)
        ):
            continue
        if (previous_island or current_island) and previous_island != current_island:
            continue
        if (
            not should_rebalance_generated_subtitle_row_tail(
                previous_text,
                current_text,
                min_tail_chars=min_tail_chars,
                max_chars=max_chars,
            )
            or frame_gap > max_gap_frames
        ):
            continue

        moved_chars = 0
        original_previous_length = previous_length
        while current_length < min_tail_chars and previous_length > min_tail_chars:
            char = previous_text[-1]
            previous_text = previous_text[:-1].rstrip()
            current_text = (char + current_text).strip()
            previous_length = generated_subtitle_text_length(previous_text)
            current_length = generated_subtitle_text_length(current_text)
            moved_chars += 1

        if moved_chars <= 0 or not previous_text:
            continue

        previous["text"] = previous_text
        current["text"] = current_text

        previous_start = int(previous.get("start_frame") or 0)
        previous_end = int(previous.get("end_frame") or previous_start + 1)
        current_end = int(current.get("end_frame") or previous_end + 1)
        frames_per_char = max(1, int(round((previous_end - previous_start) / max(1, original_previous_length))))
        boundary = max(previous_start + 1, previous_end - frames_per_char * moved_chars)
        previous["end_frame"] = boundary
        current["start_frame"] = boundary
        if current_end <= boundary:
            current["end_frame"] = boundary + 1

    return output


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
            for index in range(preferred_min - 1, 1, -1):
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
    return rebalance_generated_subtitle_short_tail(chunks, max_chars=max_chars)


def split_generated_subtitle_text(text: str, max_chars: int = GENERATED_SUBTITLE_MAX_CHARS) -> list[str]:
    value = normalize_generated_subtitle_text(text)
    if not value:
        return []

    hard_breaks = "。！？!?；;"
    semantic_comma_breaks = "，、,"
    clauses: list[str] = []
    buffer: list[str] = []
    for index, char in enumerate(value):
        buffer.append(char)
        should_break = char in hard_breaks
        if not should_break and char in semantic_comma_breaks:
            should_break = should_split_generated_subtitle_semantic_comma("".join(buffer), value[index + 1 :])
        if should_break:
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


def generated_subtitle_timed_units(words: list[dict[str, Any]]) -> list[dict[str, float]]:
    units: list[dict[str, float]] = []
    for word in words or []:
        word_text = str(word.get("word") or word.get("text") or "")
        normalized = normalize_ctc_text(word_text)
        if not normalized:
            continue
        start = float(word.get("start") or 0.0)
        end = float(word.get("end") or start)
        if end <= start:
            end = start + 0.001
        duration = (end - start) / max(1, len(normalized))
        for index, _char in enumerate(normalized):
            unit_start = start + duration * index
            unit_end = start + duration * (index + 1)
            units.append({"start": unit_start, "end": max(unit_end, unit_start + 0.001)})
    return units


def generated_subtitle_forced_silence_boundaries(
    words: list[dict[str, Any]],
    speech_regions: list[dict[str, Any]] | None = None,
    min_silence_seconds: float = GENERATED_SUBTITLE_HARD_SILENCE_SECONDS,
) -> list[dict[str, Any]]:
    timed_units = generated_subtitle_timed_units(words)
    if len(timed_units) < 2:
        return []

    candidates: dict[int, dict[str, Any]] = {}
    for left_region, right_region in zip(speech_regions or [], (speech_regions or [])[1:]):
        silence_start = float(left_region.get("end") or 0.0)
        silence_end = float(right_region.get("start") or silence_start)
        silence_seconds = silence_end - silence_start
        if silence_seconds < min_silence_seconds:
            continue
        silence_midpoint = (silence_start + silence_end) * 0.5
        first_midpoint = (timed_units[0]["start"] + timed_units[0]["end"]) * 0.5
        last_midpoint = (timed_units[-1]["start"] + timed_units[-1]["end"]) * 0.5
        if silence_midpoint <= first_midpoint or silence_midpoint >= last_midpoint:
            continue
        offset = min(
            range(1, len(timed_units)),
            key=lambda index: abs(
                ((timed_units[index - 1]["end"] + timed_units[index]["start"]) * 0.5)
                - silence_midpoint
            ),
        )
        left_count = offset
        right_count = len(timed_units) - offset
        if left_count < 2 or right_count < 2:
            continue
        word_gap = float(timed_units[offset].get("start") or 0.0) - float(
            timed_units[offset - 1].get("end") or 0.0
        )
        if word_gap >= GENERATED_SUBTITLE_WORD_GAP_CONSENSUS_SECONDS:
            source = "vad_word_consensus"
        elif silence_seconds >= GENERATED_SUBTITLE_VAD_ONLY_HARD_SILENCE_SECONDS:
            source = "vad_long"
        else:
            continue
        candidates[offset] = {
            "offset": offset,
            "silence_start": silence_start,
            "silence_end": silence_end,
            "silence_seconds": silence_seconds,
            "word_gap_seconds": max(0.0, word_gap),
            "source": source,
        }

    for offset in range(1, len(timed_units)):
        silence_start = float(timed_units[offset - 1].get("end") or 0.0)
        silence_end = float(timed_units[offset].get("start") or silence_start)
        silence_seconds = silence_end - silence_start
        if (
            silence_seconds < GENERATED_SUBTITLE_WORD_GAP_BREAK_SECONDS
            or offset in candidates
            or offset < 2
            or len(timed_units) - offset < 2
        ):
            continue
        candidates[offset] = {
            "offset": offset,
            "silence_start": silence_start,
            "silence_end": silence_end,
            "silence_seconds": silence_seconds,
            "source": "word_timing",
        }
    return [candidates[offset] for offset in sorted(candidates)]


def split_generated_subtitle_text_at_normalized_offset(text: str, offset: int) -> tuple[str, str] | None:
    if offset <= 0:
        return None
    consumed = 0
    for index, char in enumerate(str(text or "")):
        consumed += len(normalize_ctc_text(char))
        if consumed >= offset:
            left = str(text or "")[: index + 1].strip()
            right = str(text or "")[index + 1 :].strip()
            if left and right:
                return left, right
            return None
    return None


def generated_subtitle_has_break_punctuation_at_normalized_offset(text: str, offset: int) -> bool:
    if offset <= 0:
        return False
    break_punctuation = "，、,：:。！？!?；;"
    value = str(text or "")
    consumed = 0
    for index, char in enumerate(value):
        consumed += len(normalize_ctc_text(char))
        if consumed >= offset:
            return index + 1 < len(value) and value[index + 1] in break_punctuation
    return False


def split_generated_subtitle_chunk_by_timed_gap(
    chunk: str,
    timed_units: list[dict[str, float]],
    min_gap_seconds: float = GENERATED_SUBTITLE_WORD_GAP_BREAK_SECONDS,
    min_left_chars: int = GENERATED_SUBTITLE_WORD_GAP_MIN_LEFT_CHARS,
    min_right_chars: int = GENERATED_SUBTITLE_WORD_GAP_MIN_RIGHT_CHARS,
) -> list[str]:
    unit_count = len(normalize_ctc_text(chunk))
    if unit_count < min_left_chars + min_right_chars or len(timed_units) < unit_count:
        return [chunk]

    best_offset = 0
    best_gap = 0.0
    for offset in range(min_left_chars, unit_count - min_right_chars + 1):
        right_count = unit_count - offset
        if right_count < GENERATED_SUBTITLE_WORD_GAP_MIN_RIGHT_CHARS + 2 and not generated_subtitle_has_break_punctuation_at_normalized_offset(chunk, offset):
            continue
        gap = float(timed_units[offset].get("start") or 0.0) - float(timed_units[offset - 1].get("end") or 0.0)
        if gap >= min_gap_seconds and gap > best_gap:
            best_gap = gap
            best_offset = offset
    if best_offset <= 0:
        return [chunk]

    split = split_generated_subtitle_text_at_normalized_offset(chunk, best_offset)
    if not split:
        return [chunk]
    left, right = split
    return [left, right]


def split_generated_subtitle_chunks_by_word_gaps(chunks: list[str], words: list[dict[str, Any]]) -> list[str]:
    timed_units = generated_subtitle_timed_units(words)
    if not timed_units:
        return chunks

    output: list[str] = []
    cursor = 0
    for chunk in chunks:
        unit_count = len(normalize_ctc_text(chunk))
        unit_slice = timed_units[cursor : cursor + unit_count]
        output.extend(split_generated_subtitle_chunk_by_timed_gap(chunk, unit_slice))
        cursor += unit_count
    return output


def _subtitle_rows_interval(rows: list[dict[str, Any]]) -> tuple[int, int] | None:
    valid_rows = [row for row in rows or [] if row.get("start_frame") is not None and row.get("end_frame") is not None]
    if not valid_rows:
        return None
    return (
        min(int(row["start_frame"]) for row in valid_rows),
        max(int(row["end_frame"]) for row in valid_rows),
    )


def _subtract_frame_intervals(
    interval: tuple[int, int],
    covered: list[tuple[int, int]],
) -> tuple[list[tuple[int, int]], bool]:
    regions = [interval]
    overlapped = False
    for covered_start, covered_end in sorted(covered):
        next_regions: list[tuple[int, int]] = []
        for start, end in regions:
            if covered_end <= start or covered_start >= end:
                next_regions.append((start, end))
                continue
            overlapped = True
            if start < covered_start:
                next_regions.append((start, covered_start))
            if covered_end < end:
                next_regions.append((covered_end, end))
        regions = next_regions
    return [(start, end) for start, end in regions if end > start], overlapped


def _merge_frame_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def _rows_inside_interval(rows: list[dict[str, Any]], interval: tuple[int, int]) -> list[dict[str, Any]]:
    start, end = interval
    return [
        row
        for row in rows or []
        if int(row.get("start_frame") or 0) >= start and int(row.get("end_frame") or 0) <= end
    ]


def _map_sequence_boundary(matcher: difflib.SequenceMatcher, source_position: int, reverse: bool = False) -> int | None:
    for source_start, target_start, size in matcher.get_matching_blocks():
        if reverse:
            source_start, target_start = target_start, source_start
        if source_start <= source_position <= source_start + size:
            return target_start + (source_position - source_start)
    return None


def _segmentation_boundary_features(text: str, position: int) -> list[str]:
    value = str(text or "")
    if position <= 0 or position >= len(value):
        return []
    features = [
        f"left1={value[position - 1:position]}",
        f"right1={value[position:position + 1]}",
        f"cross2={value[position - 1:position + 1]}",
    ]
    if position >= 2:
        features.append(f"left2={value[position - 2:position]}")
    if position + 2 <= len(value):
        features.append(f"right2={value[position:position + 2]}")
    if position >= 2 and position + 2 <= len(value):
        features.append(f"cross4={value[position - 2:position + 2]}")
    return features


def _percentile(values: list[int], fraction: float, default: int) -> int:
    if not values:
        return default
    ordered = sorted(int(value) for value in values)
    index = int(round((len(ordered) - 1) * max(0.0, min(1.0, fraction))))
    return ordered[index]


def _build_segmentation_training_example(
    plugin_rows: list[dict[str, Any]],
    manual_rows: list[dict[str, Any]],
    source_label: str,
) -> dict[str, Any] | None:
    plugin_chunks = [normalize_ctc_text(row.get("text") or "") for row in plugin_rows]
    manual_chunks = [normalize_ctc_text(row.get("text") or "") for row in manual_rows]
    plugin_chunks = [chunk for chunk in plugin_chunks if chunk]
    manual_chunks = [chunk for chunk in manual_chunks if chunk]
    source_text = "".join(plugin_chunks)
    manual_text = "".join(manual_chunks)
    if len(source_text) < 2 or len(manual_text) < 2:
        return None

    matcher = difflib.SequenceMatcher(None, source_text, manual_text, autojunk=False)
    manual_boundaries: list[int] = []
    cursor = 0
    for chunk in manual_chunks[:-1]:
        cursor += len(chunk)
        manual_boundaries.append(cursor)

    mapped_boundaries: list[int] = []
    for boundary in manual_boundaries:
        mapped = _map_sequence_boundary(matcher, boundary, reverse=True)
        if mapped is not None and 0 < mapped < len(source_text):
            mapped_boundaries.append(mapped)

    if not mapped_boundaries and len(manual_chunks) > 1:
        return None
    return {
        "source": source_label,
        "source_text": source_text,
        "manual_text": manual_text,
        "manual_chunks": manual_chunks,
        "mapped_boundaries": sorted(set(mapped_boundaries)),
        "text_match_score": round(float(matcher.ratio()), 6),
        "text_corrections": _extract_segmentation_text_corrections(source_text, manual_text),
        "start_frame": min(int(row.get("start_frame") or 0) for row in plugin_rows),
        "end_frame": max(int(row.get("end_frame") or 0) for row in plugin_rows),
    }


def _extract_segmentation_text_corrections(source_text: str, manual_text: str) -> list[dict[str, Any]]:
    matcher = difflib.SequenceMatcher(None, source_text, manual_text, autojunk=False)
    corrections: list[dict[str, Any]] = []
    for tag, source_start, source_end, manual_start, manual_end in matcher.get_opcodes():
        if tag != "replace":
            continue
        source = source_text[source_start:source_end]
        replacement = manual_text[manual_start:manual_end]
        if not source or not replacement or len(source) > 8 or len(replacement) > 12:
            continue
        left_context = source_text[max(0, source_start - 4):source_start]
        right_context = source_text[source_end:source_end + 4]
        if len(left_context) + len(right_context) < 4:
            continue
        corrections.append(
            {
                "source": source,
                "replacement": replacement,
                "left_context": left_context,
                "right_context": right_context,
            }
        )
    return corrections


def _build_segmentation_mode_profile(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    ordered_payloads = sorted(
        [payload for payload in payloads or [] if isinstance(payload, dict)],
        key=lambda payload: str(payload.get("exported_at") or ""),
        reverse=True,
    )
    covered_by_timeline: dict[str, list[tuple[int, int]]] = {}
    examples: list[dict[str, Any]] = []
    overridden_region_count = 0

    for payload_index, payload in enumerate(ordered_payloads, start=1):
        plugin_rows = list(payload.get("plugin_rows") or [])
        manual_rows = list(payload.get("manual_rows") or [])
        plugin_interval = _subtitle_rows_interval(plugin_rows)
        manual_interval = _subtitle_rows_interval(manual_rows)
        if not plugin_interval or not manual_interval:
            continue
        sample_interval = (
            min(plugin_interval[0], manual_interval[0]),
            max(plugin_interval[1], manual_interval[1]),
        )
        if sample_interval[1] <= sample_interval[0]:
            continue
        timeline_name = str(payload.get("timeline_name") or "")
        regions, overlapped = _subtract_frame_intervals(
            sample_interval,
            covered_by_timeline.get(timeline_name, []),
        )
        if overlapped:
            overridden_region_count += 1
        source_path_value = str(payload.get("source_path") or "")
        source_label = (
            Path(source_path_value).name
            if source_path_value
            else str(payload.get("exported_at") or f"sample_{payload_index}")
        )
        for region_index, region in enumerate(regions, start=1):
            region_plugin_rows = _rows_inside_interval(plugin_rows, region)
            region_manual_rows = _rows_inside_interval(manual_rows, region)
            if not region_plugin_rows or not region_manual_rows:
                continue
            example = _build_segmentation_training_example(
                region_plugin_rows,
                region_manual_rows,
                f"{source_label}#{region_index}",
            )
            if example:
                examples.append(example)
        covered_by_timeline[timeline_name] = _merge_frame_intervals(
            [*covered_by_timeline.get(timeline_name, []), sample_interval]
        )

    positive_counts: Counter[str] = Counter()
    negative_counts: Counter[str] = Counter()
    manual_lengths: list[int] = []
    for example in examples:
        source_text = str(example["source_text"])
        boundaries = set(int(value) for value in example.get("mapped_boundaries") or [])
        manual_lengths.extend(len(str(chunk)) for chunk in example.get("manual_chunks") or [] if chunk)
        for position in range(1, len(source_text)):
            target = positive_counts if position in boundaries else negative_counts
            target.update(_segmentation_boundary_features(source_text, position))

    total_positive = max(1, sum(positive_counts.values()))
    total_negative = max(1, sum(negative_counts.values()))
    feature_weights: dict[str, float] = {}
    for feature in sorted(set(positive_counts) | set(negative_counts)):
        positive_rate = (positive_counts[feature] + 0.5) / (total_positive + 1.0)
        negative_rate = (negative_counts[feature] + 0.5) / (total_negative + 1.0)
        feature_weights[feature] = round(max(-3.0, min(3.0, math.log(positive_rate / negative_rate))), 6)

    correction_counts: Counter[tuple[str, str, str, str]] = Counter()
    for example in examples:
        for correction in example.get("text_corrections") or []:
            correction_counts[
                (
                    str(correction.get("source") or ""),
                    str(correction.get("replacement") or ""),
                    str(correction.get("left_context") or ""),
                    str(correction.get("right_context") or ""),
                )
            ] += 1
    text_corrections = [
        {
            "source": source,
            "replacement": replacement,
            "left_context": left_context,
            "right_context": right_context,
            "support": support,
        }
        for (source, replacement, left_context, right_context), support in correction_counts.most_common()
        if source and replacement
    ]

    return {
        "length_model": {
            "minimum": max(2, _percentile(manual_lengths, 0.05, 4)),
            "p25": _percentile(manual_lengths, 0.25, 7),
            "median": _percentile(manual_lengths, 0.5, 10),
            "p75": _percentile(manual_lengths, 0.75, 14),
            "p95": _percentile(manual_lengths, 0.95, 20),
            "maximum": max(manual_lengths, default=GENERATED_SUBTITLE_MAX_CHARS),
        },
        "feature_weights": feature_weights,
        "text_corrections": text_corrections,
        "examples": examples,
        "diagnostic": {
            "input_payload_count": len(ordered_payloads),
            "training_region_count": len(examples),
            "overridden_region_count": overridden_region_count,
            "positive_boundary_count": sum(len(example.get("mapped_boundaries") or []) for example in examples),
            "manual_row_count": len(manual_lengths),
            "text_correction_rule_count": len(text_corrections),
        },
    }


def build_segmentation_profile_from_payloads(payloads: list[dict[str, Any]]) -> dict[str, Any]:
    valid_payloads = [payload for payload in payloads or [] if isinstance(payload, dict)]
    mode_payloads: dict[str, list[dict[str, Any]]] = {"narration": [], "live": []}
    for payload in valid_payloads:
        mode = str(payload.get("subtitle_mode") or "narration")
        mode_payloads["live" if mode == "live" else "narration"].append(payload)
    modes = {
        mode: _build_segmentation_mode_profile(group)
        for mode, group in mode_payloads.items()
        if group
    }
    training_region_count = sum(
        int(mode_profile.get("diagnostic", {}).get("training_region_count") or 0)
        for mode_profile in modes.values()
    )
    manual_row_count = sum(
        int(mode_profile.get("diagnostic", {}).get("manual_row_count") or 0)
        for mode_profile in modes.values()
    )
    return {
        "schema_version": SEGMENTATION_PROFILE_SCHEMA,
        "modes": modes,
        "diagnostic": {
            "input_payload_count": len(valid_payloads),
            "narration_payload_count": len(mode_payloads["narration"]),
            "live_payload_count": len(mode_payloads["live"]),
            "training_region_count": training_region_count,
            "manual_row_count": manual_row_count,
        },
    }


def segmentation_profile_for_mode(
    profile: dict[str, Any] | None,
    subtitle_mode: str,
) -> dict[str, Any] | None:
    if not isinstance(profile, dict):
        return None
    mode = "live" if subtitle_mode == "live" else "narration"
    schema = str(profile.get("schema_version") or "")
    if schema == SEGMENTATION_PROFILE_SCHEMA:
        mode_profile = (profile.get("modes") or {}).get(mode)
        return mode_profile if isinstance(mode_profile, dict) else None
    if schema == LEGACY_SEGMENTATION_PROFILE_SCHEMA:
        return profile if mode == "narration" else None
    if "length_model" in profile and "feature_weights" in profile:
        return profile
    return None


def load_segmentation_profile(path: str | Path | None = None) -> dict[str, Any] | None:
    raw_path = path or os.getenv(SEGMENTATION_PROFILE_ENV)
    profile_path = Path(raw_path).expanduser() if raw_path else Path(__file__).resolve().with_name("segmentation_profile.json")
    if not profile_path.is_file():
        return None
    try:
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") not in {SEGMENTATION_PROFILE_SCHEMA, LEGACY_SEGMENTATION_PROFILE_SCHEMA}:
        return None
    payload["_path"] = str(profile_path)
    return payload


def load_segmentation_profile_v4(path: str | Path | None = None) -> dict[str, Any] | None:
    raw_path = path or os.getenv(SEGMENTATION_PROFILE_ENV)
    profile_path = Path(raw_path).expanduser() if raw_path else Path(__file__).resolve().with_name("segmentation_profile_v3.json")
    if not profile_path.is_file():
        return None
    try:
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != generate_v4.PROFILE_SCHEMA:
        return None
    payload["_path"] = str(profile_path)
    return payload


def load_segmentation_profile_v5(path: str | Path | None = None) -> tuple[dict[str, Any] | None, str]:
    raw_path = path or os.getenv(SEGMENTATION_PROFILE_ENV)
    profile_path = Path(raw_path).expanduser() if raw_path else Path(__file__).resolve().with_name("segmentation_profile_v4.json")
    if not profile_path.is_file():
        return None, "missing"
    try:
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None, "invalid_json"
    except OSError:
        return None, "unreadable"
    if not isinstance(payload, dict):
        return None, "invalid_payload"
    if payload.get("schema_version") != generate_v5.PROFILE_SCHEMA:
        return None, "schema_mismatch"
    payload["_path"] = str(profile_path)
    return payload, "loaded"


def _mapped_example_boundaries(text: str, profile: dict[str, Any]) -> tuple[set[int], float]:
    best_boundaries: set[int] = set()
    best_score = 0.0
    for example in profile.get("examples") or []:
        source_text = str(example.get("source_text") or "")
        if not source_text:
            continue
        if text in source_text:
            offset = source_text.index(text)
            boundaries = {
                int(position) - offset
                for position in example.get("mapped_boundaries") or []
                if offset < int(position) < offset + len(text)
            }
            score = 1.0
        else:
            matcher = difflib.SequenceMatcher(None, source_text, text, autojunk=False)
            matched_chars = sum(size for _source, _target, size in matcher.get_matching_blocks())
            score = matched_chars / max(1, len(text))
            boundaries = set()
            for position in example.get("mapped_boundaries") or []:
                mapped = _map_sequence_boundary(matcher, int(position))
                if mapped is not None and 0 < mapped < len(text):
                    boundaries.add(mapped)
        if score > best_score:
            best_score = score
            best_boundaries = boundaries
    return best_boundaries, min(1.0, best_score)


def _normalized_break_punctuation_offsets(text: str) -> set[int]:
    offsets: set[int] = set()
    consumed = 0
    for char in str(text or ""):
        if char in "，、,：:。！？!?；;":
            if consumed > 0:
                offsets.add(consumed)
            continue
        consumed += len(normalize_ctc_text(char))
    return offsets


def _learned_boundary_score(
    text: str,
    position: int,
    profile: dict[str, Any],
    exact_boundaries: set[int],
    punctuation_offsets: set[int],
    timed_units: list[dict[str, float]],
) -> float:
    if position <= 0 or position >= len(text):
        return -1000.0
    if text[position - 1].isascii() and text[position - 1].isalnum() and text[position].isascii() and text[position].isalnum():
        return -1000.0

    score = -5.0
    feature_weights = profile.get("feature_weights") or {}
    feature_values = [float(feature_weights.get(feature) or 0.0) for feature in _segmentation_boundary_features(text, position)]
    if feature_values:
        score += sum(feature_values) / len(feature_values) * 0.8
    if position in exact_boundaries:
        score += 14.0
    if position in punctuation_offsets:
        score += 5.0
    if position < len(timed_units):
        gap = float(timed_units[position].get("start") or 0.0) - float(timed_units[position - 1].get("end") or 0.0)
        if gap >= 0.65:
            score += 8.0
        elif gap >= 0.25:
            score += 4.0
        elif gap >= 0.12:
            score += 1.5
    if text[position - 1:position + 1] in GENERATED_SUBTITLE_COMMON_WORD_BOUNDARY_BIGRAMS:
        score -= 7.0
    if generated_subtitle_starts_with_orphan_particle(text[position:]):
        score -= 5.0
    return score


def _learned_segment_length_score(length: int, length_model: dict[str, Any]) -> float:
    minimum = max(2, int(length_model.get("minimum") or 4))
    p25 = max(minimum, int(length_model.get("p25") or 7))
    median = max(p25, int(length_model.get("median") or 10))
    p75 = max(median, int(length_model.get("p75") or 14))
    p95 = max(p75, int(length_model.get("p95") or 20))
    if length < 2:
        return -1000.0
    score = -abs(length - median) * 0.22
    if p25 <= length <= p75:
        score += 0.25
    if length < minimum:
        score -= (minimum - length) * 3.0
    if length > p95:
        score -= (length - p95) * 1.2
    return score


def split_generated_subtitle_text_learned(
    text: str,
    words: list[dict[str, Any]],
    profile: dict[str, Any],
    speech_regions: list[dict[str, Any]] | None = None,
    subtitle_mode: str = "narration",
) -> tuple[list[str], dict[str, Any]]:
    normalized_text = normalize_ctc_text(text)
    mode_profile = segmentation_profile_for_mode(profile, subtitle_mode)
    if not normalized_text or not mode_profile:
        return split_generated_subtitle_text(text), {"used": False, "reason": "invalid_profile"}
    timed_units = generated_subtitle_timed_units(words)
    if len(timed_units) < len(normalized_text):
        return split_generated_subtitle_text(text), {"used": False, "reason": "missing_word_timing"}

    mapped_boundaries, matched_example_score = _mapped_example_boundaries(normalized_text, mode_profile)
    exact_boundaries = mapped_boundaries if matched_example_score >= 0.75 else set()
    forced_silence_boundaries = generated_subtitle_forced_silence_boundaries(words, speech_regions)
    forced_offsets = {int(boundary["offset"]) for boundary in forced_silence_boundaries}
    punctuation_offsets = _normalized_break_punctuation_offsets(text)
    length_model = mode_profile.get("length_model") or {}
    soft_limit = 14 if subtitle_mode == "live" else GENERATED_SUBTITLE_MAX_CHARS
    if (
        len(normalized_text) <= soft_limit
        and not exact_boundaries
        and not forced_offsets
        and not punctuation_offsets
    ):
        return [text], {
            "used": True,
            "reason": "below_soft_limit_without_boundary_evidence",
            "candidate_count": max(0, len(normalized_text) - 1),
            "matched_example_score": round(matched_example_score, 6),
            "matched_boundary_count": 0,
            "forced_silence_boundaries": [],
            "forced_silence_break_count": 0,
            "selected_boundaries": [],
            "score": 0.0,
        }
    hard_max = max(24, int(length_model.get("p95") or 20) + 8, int(length_model.get("maximum") or 18) + 3)
    positions = list(range(0, len(normalized_text) + 1))
    scores = [-math.inf] * len(positions)
    previous = [-1] * len(positions)
    scores[0] = 0.0

    for end in positions[1:]:
        for start in range(max(0, end - hard_max), end):
            if not math.isfinite(scores[start]):
                continue
            if any(start < forced_offset < end for forced_offset in forced_offsets):
                continue
            length = end - start
            segment_score = _learned_segment_length_score(length, length_model)
            if segment_score <= -999.0 and length == 1 and (
                start == 0 or end == len(normalized_text) or start in forced_offsets or end in forced_offsets
            ):
                segment_score = -4.0
            if segment_score <= -999.0:
                continue
            boundary_score = 0.0
            if end < len(normalized_text):
                boundary_score = _learned_boundary_score(
                    normalized_text,
                    end,
                    mode_profile,
                    exact_boundaries,
                    punctuation_offsets,
                    timed_units,
                )
                if boundary_score <= -999.0:
                    continue
            candidate_score = scores[start] + segment_score + boundary_score
            if candidate_score > scores[end]:
                scores[end] = candidate_score
                previous[end] = start

    if previous[-1] < 0:
        return split_generated_subtitle_text(text), {"used": False, "reason": "no_dp_path"}

    boundaries: list[int] = []
    cursor = len(normalized_text)
    while cursor > 0:
        start = previous[cursor]
        if start < 0:
            return split_generated_subtitle_text(text), {"used": False, "reason": "broken_dp_path"}
        if start > 0:
            boundaries.append(start)
        cursor = start
    boundaries.reverse()

    chunks: list[str] = []
    raw_tail = str(text or "")
    consumed = 0
    for boundary in [*boundaries, len(normalized_text)]:
        split_offset = boundary - consumed
        if boundary == len(normalized_text):
            chunk = raw_tail.strip()
            raw_tail = ""
        else:
            split = split_generated_subtitle_text_at_normalized_offset(raw_tail, split_offset)
            if not split:
                return split_generated_subtitle_text(text), {"used": False, "reason": "raw_text_mapping_failed"}
            chunk, raw_tail = split
        if chunk:
            chunks.append(chunk)
        consumed = boundary

    return chunks, {
        "used": True,
        "reason": "learned_dp",
        "candidate_count": max(0, len(normalized_text) - 1),
        "matched_example_score": round(matched_example_score, 6),
        "matched_boundary_count": len(exact_boundaries),
        "forced_silence_boundaries": forced_silence_boundaries,
        "forced_silence_break_count": len(forced_silence_boundaries),
        "selected_boundaries": boundaries,
        "score": round(scores[-1], 6),
    }


def apply_forced_silence_to_frame_ranges(
    frame_ranges: list[tuple[int, int]],
    chunks: list[str],
    forced_silence_boundaries: list[dict[str, Any]],
    fps: float,
    timeline_start_frame: int,
) -> tuple[list[tuple[int, int]], set[int]]:
    if len(frame_ranges) < 2 or not forced_silence_boundaries:
        return frame_ranges, set()
    forced_by_offset = {
        int(boundary.get("offset") or 0): boundary
        for boundary in forced_silence_boundaries
        if int(boundary.get("offset") or 0) > 0
    }
    adjusted = [[int(start), int(end)] for start, end in frame_ranges]
    adjusted_boundaries: set[int] = set()
    offset = 0
    for index, chunk in enumerate(chunks[:-1]):
        offset += len(normalize_ctc_text(chunk))
        boundary = forced_by_offset.get(offset)
        if not boundary:
            continue
        silence_end_frame = seconds_to_timeline_frame(
            float(boundary.get("silence_start") or 0.0),
            fps,
            timeline_start_frame,
        )
        next_speech_frame = seconds_to_timeline_frame(
            float(boundary.get("silence_end") or 0.0),
            fps,
            timeline_start_frame,
        )
        if next_speech_frame <= silence_end_frame:
            continue
        adjusted[index][1] = max(adjusted[index][0] + 1, silence_end_frame)
        adjusted[index + 1][0] = min(adjusted[index + 1][1] - 1, next_speech_frame)
        if adjusted[index + 1][0] > adjusted[index][1]:
            adjusted_boundaries.add(index)
    return [(start, end) for start, end in adjusted], adjusted_boundaries


def _same_generated_subtitle_speaker(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_speaker = left.get("speaker_track_index")
    right_speaker = right.get("speaker_track_index")
    return left_speaker == right_speaker or left_speaker is None or right_speaker is None


def _same_speech_island(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_island = str(left.get("speech_island_id") or "")
    right_island = str(right.get("speech_island_id") or "")
    return bool(left_island) and left_island == right_island


def attach_speech_islands_to_segments(
    segments: list[dict[str, Any]],
    speech_regions: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    regions: list[tuple[int, float, float]] = []
    for index, region in enumerate(speech_regions or [], start=1):
        start = float(region.get("start") or 0.0)
        end = float(region.get("end") or start)
        if end > start:
            regions.append((index, start, end))
    if not regions:
        return [dict(segment) for segment in segments or []]

    attached: list[dict[str, Any]] = []
    for raw_segment in segments or []:
        segment = dict(raw_segment)
        if segment.get("speech_island_id"):
            attached.append(segment)
            continue
        start = float(segment.get("start") or 0.0)
        end = float(segment.get("end") or start)
        overlapping_regions = [
            region
            for region in regions
            if min(end, region[2]) - max(start, region[1]) > 0.0
        ]
        if len(overlapping_regions) == 1:
            region_index, region_start, region_end = overlapping_regions[0]
            segment["speech_island_id"] = f"shared:{region_index}:{region_start:.3f}-{region_end:.3f}"
            segment["speech_island_start"] = region_start
            segment["speech_island_end"] = region_end
        attached.append(segment)
    return attached


def attach_speech_islands_to_rows(
    rows: list[dict[str, Any]],
    speech_regions: list[dict[str, Any]] | None,
    fps: float,
    timeline_start_frame: int,
) -> list[dict[str, Any]]:
    rate = max(1.0, float(fps or 30.0))
    regions: list[tuple[int, int, int]] = []
    for index, region in enumerate(speech_regions or [], start=1):
        start_frame = seconds_to_timeline_frame(float(region.get("start") or 0.0), rate, timeline_start_frame)
        end_frame = seconds_to_timeline_frame(float(region.get("end") or 0.0), rate, timeline_start_frame)
        if end_frame > start_frame:
            regions.append((index, start_frame, end_frame))

    attached: list[dict[str, Any]] = []
    for raw_row in rows or []:
        row = dict(raw_row)
        if row.get("speech_island_id") and not str(row.get("speech_island_id")).startswith("shared:"):
            attached.append(row)
            continue
        row_start = int(row.get("start_frame") or 0)
        row_end = int(row.get("end_frame") or row_start + 1)
        best_region: tuple[int, int, int] | None = None
        best_overlap = 0
        for region in regions:
            overlap = max(0, min(row_end, region[2]) - max(row_start, region[1]))
            if overlap > best_overlap:
                best_overlap = overlap
                best_region = region
        if best_region is not None and best_overlap > 0:
            region_index, region_start, region_end = best_region
            row["speech_island_id"] = f"shared:{region_index}:{region_start}-{region_end}"
            row["speech_island_start_frame"] = region_start
            row["speech_island_end_frame"] = region_end
        attached.append(row)
    return attached


def reconcile_live_subtitle_rows(
    rows: list[dict[str, Any]],
    fps: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rate = max(1.0, float(fps or 30.0))
    nearby_frames = max(1, int(math.ceil(0.35 * rate)))
    accepted: list[dict[str, Any]] = []
    duplicate_suppressed_count = 0
    ambiguous_island_count = 0

    def quality(row: dict[str, Any]) -> tuple[float, float, int, int]:
        start = int(row.get("start_frame") or 0)
        end = int(row.get("end_frame") or start + 1)
        return (
            float(row.get("speaker_dominance_db") or 0.0),
            float(row.get("speaker_score_db") or 0.0),
            len(normalize_ctc_text(row.get("text") or "")),
            end - start,
        )

    for raw_row in sorted(
        rows or [],
        key=lambda row: (int(row.get("start_frame") or 0), int(row.get("end_frame") or 0)),
    ):
        row = dict(raw_row)
        row_start = int(row.get("start_frame") or 0)
        row_end = int(row.get("end_frame") or row_start + 1)
        row_text = normalize_ctc_text(row.get("text") or "")
        duplicate_index: int | None = None
        overlapping_different_text = False
        for index, existing in enumerate(accepted):
            existing_start = int(existing.get("start_frame") or 0)
            existing_end = int(existing.get("end_frame") or existing_start + 1)
            intersection = max(0, min(row_end, existing_end) - max(row_start, existing_start))
            minimum_duration = max(1, min(row_end - row_start, existing_end - existing_start))
            overlap_ratio = intersection / minimum_duration
            starts_nearby = abs(row_start - existing_start) <= nearby_frames
            if intersection <= 0 or (overlap_ratio < 0.5 and not starts_nearby):
                continue
            existing_text = normalize_ctc_text(existing.get("text") or "")
            similarity = difflib.SequenceMatcher(None, row_text, existing_text, autojunk=False).ratio()
            if similarity >= 0.80:
                duplicate_index = index
                break
            if intersection > 0 and row.get("speaker_track_index") != existing.get("speaker_track_index"):
                overlapping_different_text = True
        if duplicate_index is not None:
            duplicate_suppressed_count += 1
            if quality(row) > quality(accepted[duplicate_index]):
                accepted[duplicate_index] = row
            continue
        if overlapping_different_text:
            ambiguous_island_count += 1
        accepted.append(row)

    accepted.sort(key=lambda row: (int(row.get("start_frame") or 0), int(row.get("end_frame") or 0)))
    for index, row in enumerate(accepted, start=1):
        row["index"] = index
    return accepted, {
        "duplicate_suppressed_count": duplicate_suppressed_count,
        "ambiguous_island_count": ambiguous_island_count,
    }


def stabilize_generated_subtitle_rows(
    rows: list[dict[str, Any]],
    fps: float,
    subtitle_mode: str = "narration",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rate = max(1.0, float(fps or 30.0))
    minimum_frames = max(1, int(math.ceil(0.18 * rate)))
    maximum_anchor_frames = max(1, int(math.ceil(12.0 * rate / 30.0)))
    maximum_neighbor_gap = max(1, int(math.ceil(0.50 * rate)))
    soft_max_chars = 14 if subtitle_mode == "live" else GENERATED_SUBTITLE_MAX_CHARS
    output = sorted(
        [dict(row) for row in rows or []],
        key=lambda row: (int(row.get("start_frame") or 0), int(row.get("end_frame") or 0)),
    )
    diagnostic = {
        "short_fragment_merged_count": 0,
        "short_utterance_kept_count": 0,
        "short_utterance_dropped_count": 0,
        "duplicate_suppressed_count": 0,
        "speech_island_timing_anchor_count": 0,
    }
    for row in output:
        row["_original_duration_frames"] = int(row.get("end_frame") or 0) - int(row.get("start_frame") or 0)

    island_rows: dict[str, list[dict[str, Any]]] = {}
    for row in output:
        island_id = str(row.get("speech_island_id") or "")
        if island_id:
            island_rows.setdefault(island_id, []).append(row)
    for grouped_rows in island_rows.values():
        first = grouped_rows[0]
        last = grouped_rows[-1]
        island_start = int(first.get("speech_island_start_frame") or first.get("start_frame") or 0)
        island_end = int(last.get("speech_island_end_frame") or last.get("end_frame") or 0)
        current_start = int(first.get("start_frame") or 0)
        current_end = int(last.get("end_frame") or current_start + 1)
        if 0 <= current_start - island_start <= maximum_anchor_frames:
            first["start_frame"] = island_start
            first["timing_anchor_source"] = "speech_island"
            diagnostic["speech_island_timing_anchor_count"] += 1
        if 0 <= island_end - current_end <= maximum_anchor_frames:
            last["end_frame"] = island_end
            last["timing_anchor_source"] = "speech_island"
            diagnostic["speech_island_timing_anchor_count"] += 1

    deduped: list[dict[str, Any]] = []
    for index, row in enumerate(output):
        text = normalize_ctc_text(row.get("text") or "")
        previous = deduped[-1] if deduped else None
        next_row = output[index + 1] if index + 1 < len(output) else None
        is_short = 0 < len(text) <= 2
        duplicate = False
        if is_short and previous and _same_generated_subtitle_speaker(previous, row):
            gap = int(row.get("start_frame") or 0) - int(previous.get("end_frame") or 0)
            previous_text = normalize_ctc_text(previous.get("text") or "")
            previous_island = str(previous.get("speech_island_id") or "")
            row_island = str(row.get("speech_island_id") or "")
            same_island = not previous_island and not row_island or previous_island == row_island
            duplicate = same_island and gap <= maximum_neighbor_gap and bool(previous_text) and previous_text.endswith(text)
        if is_short and not duplicate and next_row and _same_generated_subtitle_speaker(row, next_row):
            gap = int(next_row.get("start_frame") or 0) - int(row.get("end_frame") or 0)
            next_text = normalize_ctc_text(next_row.get("text") or "")
            row_island = str(row.get("speech_island_id") or "")
            next_island = str(next_row.get("speech_island_id") or "")
            same_island = not row_island and not next_island or row_island == next_island
            duplicate = same_island and gap <= maximum_neighbor_gap and bool(next_text) and next_text.startswith(text)
        if duplicate:
            diagnostic["duplicate_suppressed_count"] += 1
            continue
        deduped.append(row)

    stabilized: list[dict[str, Any]] = []
    index = 0
    while index < len(deduped):
        row = deduped[index]
        start_frame = int(row.get("start_frame") or 0)
        end_frame = int(row.get("end_frame") or start_frame + 1)
        duration = end_frame - start_frame
        text = normalize_ctc_text(row.get("text") or "")
        island_start = int(row.get("speech_island_start_frame") or start_frame)
        island_end = int(row.get("speech_island_end_frame") or end_frame)
        has_short_evidence = bool(row.get("short_utterance_confident")) or (
            float(row.get("speaker_dominance_db") or 0.0) >= LIVE_SPEAKER_SWITCH_MARGIN_DB
            and int(row.get("speaker_hold_frames") or 0) >= max(1, int(math.ceil(LIVE_SPEAKER_SWITCH_HOLD_SECONDS * rate)))
        )
        requires_short_evidence = subtitle_mode == "live" and 0 < len(text) <= 2
        if duration >= minimum_frames and (not requires_short_evidence or has_short_evidence):
            if (
                int(row.get("_original_duration_frames") or duration) < minimum_frames
                and len(text) <= 2
                and has_short_evidence
            ):
                diagnostic["short_utterance_kept_count"] += 1
            stabilized.append(row)
            index += 1
            continue

        confident_short = has_short_evidence and island_end - island_start >= minimum_frames
        if confident_short:
            previous_end = int(stabilized[-1].get("end_frame") or island_start) if stabilized else island_start
            next_start = int(deduped[index + 1].get("start_frame") or island_end) if index + 1 < len(deduped) else island_end
            lower_bound = max(island_start, previous_end)
            upper_bound = min(island_end, next_start)
            needed = max(0, minimum_frames - duration)
            expand_before = min((needed + 1) // 2, maximum_anchor_frames, max(0, start_frame - lower_bound))
            expand_after = min(needed - expand_before, maximum_anchor_frames, max(0, upper_bound - end_frame))
            remaining = needed - expand_before - expand_after
            if remaining > 0:
                expand_before += min(
                    remaining,
                    maximum_anchor_frames - expand_before,
                    max(0, start_frame - lower_bound - expand_before),
                )
            new_start = start_frame - expand_before
            new_end = end_frame + expand_after
            if new_end - new_start >= minimum_frames:
                row["start_frame"] = new_start
                row["end_frame"] = new_end
                row["timing_anchor_source"] = "speech_island"
                diagnostic["short_utterance_kept_count"] += 1
                stabilized.append(row)
                index += 1
                continue

        next_row = deduped[index + 1] if index + 1 < len(deduped) else None
        if (
            next_row
            and _same_generated_subtitle_speaker(row, next_row)
            and _same_speech_island(row, next_row)
            and generated_subtitle_effective_length(str(row.get("text") or "") + str(next_row.get("text") or "")) <= soft_max_chars
        ):
            merged = dict(next_row)
            merged["text"] = str(row.get("text") or "") + str(next_row.get("text") or "")
            merged["start_frame"] = start_frame
            merged["end_frame"] = max(end_frame, int(next_row.get("end_frame") or end_frame))
            diagnostic["short_fragment_merged_count"] += 1
            deduped[index + 1] = merged
            index += 1
            continue

        previous = stabilized[-1] if stabilized else None
        if (
            previous
            and _same_generated_subtitle_speaker(previous, row)
            and _same_speech_island(previous, row)
            and generated_subtitle_effective_length(str(previous.get("text") or "") + str(row.get("text") or "")) <= soft_max_chars
        ):
            previous["text"] = str(previous.get("text") or "") + str(row.get("text") or "")
            previous["end_frame"] = max(int(previous.get("end_frame") or 0), end_frame)
            diagnostic["short_fragment_merged_count"] += 1
        else:
            diagnostic["short_utterance_dropped_count"] += 1
        index += 1

    for row_index, row in enumerate(stabilized, start=1):
        row["index"] = row_index
        row.pop("_original_duration_frames", None)
    return stabilized, diagnostic


def merge_generated_subtitle_segments_for_learning(
    segments: list[dict[str, Any]],
    max_gap_seconds: float = GENERATED_SUBTITLE_LEARNED_MERGE_GAP_SECONDS,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for segment in segments or []:
        current = dict(segment)
        if not merged:
            merged.append(current)
            continue
        previous = merged[-1]
        gap = float(current.get("start") or 0.0) - float(previous.get("end") or 0.0)
        previous_speaker = previous.get("speaker_track_index")
        current_speaker = current.get("speaker_track_index")
        same_speaker = previous_speaker == current_speaker or previous_speaker is None or current_speaker is None
        previous_island = str(previous.get("speech_island_id") or "")
        current_island = str(current.get("speech_island_id") or "")
        same_island = not previous_island and not current_island or previous_island == current_island
        if (
            gap <= max_gap_seconds
            and gap >= -0.05
            and same_speaker
            and same_island
            and previous.get("words")
            and current.get("words")
        ):
            previous_text = str(previous.get("text") or "")
            current_text = str(current.get("text") or "")
            separator = " " if previous_text[-1:].isascii() and current_text[:1].isascii() else ""
            previous["text"] = previous_text + separator + current_text
            previous["end"] = max(float(previous.get("end") or 0.0), float(current.get("end") or 0.0))
            previous["words"] = [*(previous.get("words") or []), *(current.get("words") or [])]
            continue
        merged.append(current)
    return merged


def _replace_normalized_text_span(text: str, start: int, end: int, replacement: str) -> str | None:
    if start < 0 or end <= start:
        return None
    consumed = 0
    raw_start: int | None = None
    raw_end: int | None = None
    for index, char in enumerate(str(text or "")):
        normalized_length = len(normalize_ctc_text(char))
        if raw_start is None and consumed == start:
            raw_start = index
        consumed += normalized_length
        if consumed == end:
            raw_end = index + 1
            break
        if consumed > end:
            return None
    if raw_start is None or raw_end is None:
        return None
    return text[:raw_start] + replacement + text[raw_end:]


def apply_segmentation_text_corrections_to_chunks(
    chunks: list[str],
    profile: dict[str, Any],
) -> tuple[list[str], list[int]]:
    output = list(chunks)
    correction_counts = [0] * len(output)
    for correction in profile.get("text_corrections") or []:
        source = normalize_ctc_text(correction.get("source") or "")
        replacement = str(correction.get("replacement") or "")
        left_context = normalize_ctc_text(correction.get("left_context") or "")
        right_context = normalize_ctc_text(correction.get("right_context") or "")
        if not source or not replacement:
            continue

        normalized_chunks = [normalize_ctc_text(chunk) for chunk in output]
        full_text = "".join(normalized_chunks)
        search_start = 0
        while True:
            position = full_text.find(source, search_start)
            if position < 0:
                break
            source_end = position + len(source)
            visible_left = full_text[max(0, position - len(left_context)):position]
            visible_right = full_text[source_end:source_end + len(right_context)]
            left_matches = not left_context or (visible_left and left_context.endswith(visible_left))
            right_matches = not right_context or (visible_right and right_context.startswith(visible_right))
            full_context_on_one_side = visible_left == left_context or visible_right == right_context
            if (
                not left_matches
                or not right_matches
                or not full_context_on_one_side
                or len(visible_left) + len(visible_right) < 4
            ):
                search_start = position + 1
                continue

            chunk_start = 0
            applied = False
            for chunk_index, normalized_chunk in enumerate(normalized_chunks):
                chunk_end = chunk_start + len(normalized_chunk)
                if chunk_start <= position and source_end <= chunk_end:
                    corrected = _replace_normalized_text_span(
                        output[chunk_index],
                        position - chunk_start,
                        source_end - chunk_start,
                        replacement,
                    )
                    if corrected is not None:
                        output[chunk_index] = corrected
                        correction_counts[chunk_index] += 1
                        applied = True
                    break
                chunk_start = chunk_end
            if not applied:
                search_start = position + 1
                continue
            break
    return output, correction_counts


def distribute_generated_subtitle_frames_from_words(
    words: list[dict[str, Any]],
    chunks: list[str],
    fps: float,
    timeline_start_frame: int,
) -> list[tuple[int, int]] | None:
    timed_units = generated_subtitle_timed_units(words)
    chunk_unit_counts = [len(normalize_ctc_text(chunk)) for chunk in chunks]
    if not timed_units or any(count <= 0 for count in chunk_unit_counts):
        return None
    required_units = sum(chunk_unit_counts)
    if required_units <= 0 or len(timed_units) < required_units:
        return None

    rate = float(fps or 30.0)
    frames: list[tuple[int, int]] = []
    cursor = 0
    for index, count in enumerate(chunk_unit_counts):
        remaining_units = sum(chunk_unit_counts[index + 1 :])
        take = min(count, len(timed_units) - cursor - remaining_units)
        if take <= 0:
            return None
        unit_slice = timed_units[cursor : cursor + take]
        cursor += take
        start_frame = seconds_to_timeline_frame(unit_slice[0]["start"], rate, int(timeline_start_frame))
        end_frame = seconds_to_timeline_frame(unit_slice[-1]["end"], rate, int(timeline_start_frame))
        if frames and start_frame < frames[-1][1]:
            start_frame = frames[-1][1]
        frames.append((start_frame, max(start_frame + 1, end_frame)))

    return frames


def clamp_generated_subtitle_frame_range(
    start_frame: int,
    end_frame: int,
    text: str,
    fps: float,
) -> tuple[int, int]:
    rate = max(1.0, float(fps or 30.0))
    char_count = max(1, generated_subtitle_text_length(text))
    max_duration_seconds = min(
        GENERATED_SUBTITLE_MAX_DURATION_SECONDS,
        max(GENERATED_SUBTITLE_MIN_DURATION_SECONDS, char_count * GENERATED_SUBTITLE_SECONDS_PER_CHAR),
    )
    max_frames = max(1, int(math.ceil(max_duration_seconds * rate)))
    return start_frame, min(max(start_frame + 1, end_frame), start_frame + max_frames)


def generate_subtitle_rows_from_segments(
    segments: list[dict[str, Any]],
    fps: float,
    timeline_start_frame: int = 0,
    max_chars: int = GENERATED_SUBTITLE_MAX_CHARS,
    segmentation_profile: dict[str, Any] | None = None,
    speech_regions: list[dict[str, Any]] | None = None,
    subtitle_mode: str = "narration",
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    rate = float(fps or 30.0)
    mode_profile = segmentation_profile_for_mode(segmentation_profile, subtitle_mode)
    learned_mode = bool(mode_profile)
    island_segments = (
        attach_speech_islands_to_segments(segments, speech_regions)
        if subtitle_mode == "narration"
        else [dict(segment) for segment in segments or []]
    )
    source_segments = (
        merge_generated_subtitle_segments_for_learning(island_segments)
        if learned_mode
        else island_segments
    )
    for source_segment_index, segment in enumerate(source_segments, start=1):
        text = str(segment.get("text") or "").strip()
        start_seconds = float(segment.get("start") or 0.0)
        end_seconds = float(segment.get("end") or start_seconds)
        if not text or end_seconds <= start_seconds:
            continue
        segmentation_diagnostic: dict[str, Any] = {"used": False, "reason": "legacy_rules"}
        if learned_mode:
            chunks, segmentation_diagnostic = split_generated_subtitle_text_learned(
                text,
                segment.get("words") or [],
                mode_profile or {},
                speech_regions=speech_regions,
                subtitle_mode=subtitle_mode,
            )
        else:
            chunks = split_generated_subtitle_text(text, max_chars=max_chars)
            chunks = split_generated_subtitle_chunks_by_word_gaps(chunks, segment.get("words") or [])
        chunks = [
            chunk
            for chunk in chunks
            if strip_generated_subtitle_punctuation(chunk) and len(normalize_ctc_text(chunk)) > 0
        ]
        if not chunks:
            continue
        segment_start_frame = seconds_to_timeline_frame(start_seconds, rate, int(timeline_start_frame))
        segment_end_frame = seconds_to_timeline_frame(end_seconds, rate, int(timeline_start_frame))
        frame_ranges = distribute_generated_subtitle_frames_from_words(
            segment.get("words") or [],
            chunks,
            rate,
            int(timeline_start_frame),
        )
        used_word_timing = frame_ranges is not None
        if frame_ranges is None:
            frame_ranges = distribute_generated_subtitle_frames(segment_start_frame, segment_end_frame, chunks)
        frame_ranges, forced_silence_row_boundaries = apply_forced_silence_to_frame_ranges(
            frame_ranges,
            chunks,
            segmentation_diagnostic.get("forced_silence_boundaries") or [],
            rate,
            int(timeline_start_frame),
        )
        if learned_mode:
            output_chunks, text_correction_counts = apply_segmentation_text_corrections_to_chunks(
                chunks,
                mode_profile or {},
            )
        else:
            output_chunks = chunks
            text_correction_counts = [0] * len(chunks)
        for chunk_index, (chunk, output_chunk, correction_count, (start_frame, end_frame)) in enumerate(zip(
            chunks,
            output_chunks,
            text_correction_counts,
            frame_ranges,
            strict=True,
        )):
            output_text = strip_generated_subtitle_punctuation(output_chunk)
            if not output_text:
                continue
            if used_word_timing:
                timing_text = strip_generated_subtitle_punctuation(chunk)
                start_frame, end_frame = clamp_generated_subtitle_frame_range(start_frame, end_frame, timing_text, rate)
            row = {
                "index": len(rows) + 1,
                "start_frame": start_frame,
                "end_frame": end_frame,
                "text": output_text,
                "source_segment_index": source_segment_index,
            }
            if segmentation_diagnostic.get("used"):
                row["segmentation_decision"] = "learned_dp"
                row["segmentation_score"] = segmentation_diagnostic.get("score")
                row["segmentation_example_score"] = segmentation_diagnostic.get("matched_example_score")
                row["text_correction_count"] = correction_count
                row["forced_silence_break"] = (
                    chunk_index in forced_silence_row_boundaries
                    or (chunk_index - 1) in forced_silence_row_boundaries
                )
            for field in (
                "speaker_track_index",
                "speaker_score_db",
                "speaker_dominance_db",
                "speaker_hold_frames",
                "speaker_decision",
                "speech_island_id",
                "short_utterance_confident",
            ):
                if field in segment:
                    row[field] = segment[field]
            if segment.get("speech_island_start") is not None:
                row["speech_island_start_frame"] = seconds_to_timeline_frame(
                    float(segment.get("speech_island_start") or 0.0),
                    rate,
                    int(timeline_start_frame),
                )
            if segment.get("speech_island_end") is not None:
                row["speech_island_end_frame"] = seconds_to_timeline_frame(
                    float(segment.get("speech_island_end") or 0.0),
                    rate,
                    int(timeline_start_frame),
                )
            rows.append(row)
    if subtitle_mode == "narration":
        rows = attach_speech_islands_to_rows(rows, speech_regions, rate, int(timeline_start_frame))
    if not learned_mode:
        rows = rebalance_generated_subtitle_row_short_tails(rows, max_chars=max_chars)
    if subtitle_mode == "narration" and any(row.get("speech_island_id") for row in rows):
        rows, _diagnostic = stabilize_generated_subtitle_rows(rows, rate, subtitle_mode="narration")
    return rows


def build_live_speaker_turns(
    activity_windows_by_track: dict[int, list[dict[str, Any]]],
    fps: float,
    active_margin_db: float = LIVE_SPEAKER_ACTIVE_MARGIN_DB,
    switch_margin_db: float = LIVE_SPEAKER_SWITCH_MARGIN_DB,
    switch_hold_seconds: float = LIVE_SPEAKER_SWITCH_HOLD_SECONDS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rate = max(1.0, float(fps or 30.0))
    score_by_track: dict[int, dict[int, float]] = {}
    first_frame: int | None = None
    last_frame: int | None = None
    for raw_track_index, windows in (activity_windows_by_track or {}).items():
        track_index = int(raw_track_index)
        frame_scores: dict[int, list[float]] = {}
        for window in windows or []:
            start_frame = int(math.floor(float(window.get("start_frame") or 0)))
            end_frame = max(start_frame + 1, int(math.ceil(float(window.get("end_frame") or start_frame + 1))))
            score = float(window.get("activity_db") or 0.0)
            first_frame = start_frame if first_frame is None else min(first_frame, start_frame)
            last_frame = end_frame if last_frame is None else max(last_frame, end_frame)
            for frame in range(start_frame, end_frame):
                frame_scores.setdefault(frame, []).append(score)
        score_by_track[track_index] = {
            frame: sum(values) / max(1, len(values))
            for frame, values in frame_scores.items()
        }

    if first_frame is None or last_frame is None or not score_by_track:
        return [], {
            "speaker_turn_count": 0,
            "speaker_switch_count": 0,
            "ambiguous_window_count": 0,
            "overlap_suppressed_count": 0,
        }

    hold_frames = max(1, int(math.ceil(float(switch_hold_seconds) * rate)))
    current_track: int | None = None
    current_start = first_frame
    pending_track: int | None = None
    pending_start: int | None = None
    pending_frames = 0
    ambiguous_count = 0
    overlap_count = 0
    turns: list[dict[str, Any]] = []

    def finish_turn(end_frame: int) -> None:
        if current_track is None or end_frame <= current_start:
            return
        values = [
            score_by_track.get(current_track, {}).get(frame)
            for frame in range(current_start, end_frame)
        ]
        valid_values = [float(value) for value in values if value is not None]
        dominance_values: list[float] = []
        for frame in range(current_start, end_frame):
            current_value = score_by_track.get(current_track, {}).get(frame)
            if current_value is None:
                continue
            other_values = [
                scores.get(frame)
                for track_index, scores in score_by_track.items()
                if track_index != current_track and scores.get(frame) is not None
            ]
            if other_values:
                dominance_values.append(float(current_value) - max(float(value) for value in other_values))
        turns.append(
            {
                "start_frame": current_start,
                "end_frame": end_frame,
                "speaker_track_index": current_track,
                "speaker_score_db": round(sum(valid_values) / max(1, len(valid_values)), 3),
                "speaker_dominance_db": round(sum(dominance_values) / max(1, len(dominance_values)), 3),
                "speaker_hold_frames": end_frame - current_start,
                "speaker_decision": "initial" if not turns else "switch",
            }
        )

    for frame in range(first_frame, last_frame):
        frame_scores = sorted(
            (
                (track_index, scores.get(frame, float("-inf")))
                for track_index, scores in score_by_track.items()
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        active_scores = [item for item in frame_scores if item[1] >= active_margin_db]
        candidate_track = active_scores[0][0] if active_scores else None
        candidate_score = active_scores[0][1] if active_scores else float("-inf")
        if len(active_scores) > 1:
            overlap_count += 1
            if candidate_score - active_scores[1][1] < switch_margin_db:
                ambiguous_count += 1

        if current_track is None:
            if candidate_track is not None:
                current_track = candidate_track
                current_start = frame
            continue

        current_score = score_by_track.get(current_track, {}).get(frame, float("-inf"))
        should_challenge = (
            candidate_track is not None
            and candidate_track != current_track
            and (current_score < active_margin_db or candidate_score >= current_score + switch_margin_db)
        )
        if not should_challenge:
            pending_track = None
            pending_start = None
            pending_frames = 0
            continue

        if pending_track != candidate_track:
            pending_track = candidate_track
            pending_start = frame
            pending_frames = 1
        else:
            pending_frames += 1
        if pending_frames < hold_frames:
            continue

        switch_frame = int(pending_start if pending_start is not None else frame)
        finish_turn(switch_frame)
        current_track = pending_track
        current_start = switch_frame
        pending_track = None
        pending_start = None
        pending_frames = 0

    finish_turn(last_frame)
    return turns, {
        "speaker_turn_count": len(turns),
        "speaker_switch_count": max(0, len(turns) - 1),
        "ambiguous_window_count": ambiguous_count,
        "overlap_suppressed_count": overlap_count,
    }


def live_speaker_turn_at_frame(
    speaker_turns: list[dict[str, Any]],
    frame: float,
) -> dict[str, Any] | None:
    for turn in speaker_turns or []:
        if float(turn.get("start_frame") or 0) <= frame < float(turn.get("end_frame") or 0):
            return turn
    return None


def join_live_asr_words(words: list[dict[str, Any]]) -> str:
    output = ""
    for word in words or []:
        value = str(word.get("word") or word.get("text") or "")
        if not value:
            continue
        if output and output[-1:].isascii() and output[-1:].isalnum() and value[:1].isascii() and value[:1].isalnum():
            output += " "
        output += value
    return output.strip()


def live_fallback_segments_for_track(
    segments: list[dict[str, Any]],
    track_index: int,
    reason: str,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for raw_segment in segments or []:
        segment = dict(raw_segment)
        segment["speaker_track_index"] = int(track_index)
        segment["speaker_decision"] = reason
        segment.pop("speech_island_id", None)
        segment.pop("speech_island_start", None)
        segment.pop("speech_island_end", None)
        output.append(segment)
    return output


def merge_live_vad_regions(
    speech_regions: list[dict[str, Any]] | None,
    hard_gap_seconds: float = GENERATED_SUBTITLE_VAD_ONLY_HARD_SILENCE_SECONDS,
) -> list[dict[str, float]]:
    merged: list[dict[str, float]] = []
    for raw_region in sorted(speech_regions or [], key=lambda region: float(region.get("start") or 0.0)):
        start = float(raw_region.get("start") or 0.0)
        end = float(raw_region.get("end") or start)
        if end <= start:
            continue
        if merged and start - merged[-1]["end"] < float(hard_gap_seconds):
            merged[-1]["end"] = round(max(merged[-1]["end"], end), 3)
        else:
            merged.append({"start": round(start, 3), "end": round(end, 3)})
    return merged


def _filter_live_segments_for_track_legacy(
    segments: list[dict[str, Any]],
    track_index: int,
    speaker_turns: list[dict[str, Any]],
    fps: float,
    timeline_start_frame: int,
) -> tuple[list[dict[str, Any]], int, bool]:
    if not segments:
        return [], 0, False
    if not speaker_turns or any(not segment.get("words") for segment in segments):
        reason = "live_fallback_no_word_timing" if any(not segment.get("words") for segment in segments) else "live_fallback_no_activity"
        return live_fallback_segments_for_track(segments, track_index, reason), 0, True

    rate = max(1.0, float(fps or 30.0))
    selected: list[dict[str, Any]] = []
    rejected_count = 0
    for segment in segments:
        group: list[dict[str, Any]] = []
        group_turn: dict[str, Any] | None = None

        def flush_group() -> None:
            nonlocal group, group_turn
            if not group or group_turn is None:
                group = []
                group_turn = None
                return
            selected.append(
                {
                    "start": float(group[0].get("start") or 0.0),
                    "end": float(group[-1].get("end") or group[0].get("start") or 0.0),
                    "text": join_live_asr_words(group),
                    "words": [dict(word) for word in group],
                    "speaker_track_index": int(track_index),
                    "speaker_score_db": float(group_turn.get("speaker_score_db") or 0.0),
                    "speaker_decision": str(group_turn.get("speaker_decision") or "live_energy"),
                }
            )
            group = []
            group_turn = None

        for word in segment.get("words") or []:
            word_start = float(word.get("start") or 0.0)
            word_end = float(word.get("end") or word_start)
            midpoint = int(timeline_start_frame) + ((word_start + word_end) * 0.5 * rate)
            turn = live_speaker_turn_at_frame(speaker_turns, midpoint)
            if turn and int(turn.get("speaker_track_index") or 0) == int(track_index):
                if group_turn is not None and turn is not group_turn:
                    flush_group()
                group_turn = turn
                group.append(dict(word))
            else:
                rejected_count += 1
                flush_group()
        flush_group()

    return [segment for segment in selected if segment.get("text")], rejected_count, False


def filter_live_segments_for_track(
    segments: list[dict[str, Any]],
    track_index: int,
    speaker_turns: list[dict[str, Any]],
    fps: float,
    timeline_start_frame: int,
    speech_regions: list[dict[str, Any]] | None = None,
    engine: str | None = None,
) -> tuple[list[dict[str, Any]], int, bool]:
    selected_engine = str(engine or os.getenv("SUBFIX_LIVE_ENGINE") or "islands_v2")
    if selected_engine == "legacy" or not speech_regions:
        return _filter_live_segments_for_track_legacy(
            segments,
            track_index,
            speaker_turns,
            fps,
            timeline_start_frame,
        )
    if not segments:
        return [], 0, False
    if not speaker_turns or any(not segment.get("words") for segment in segments):
        reason = "live_fallback_no_word_timing" if any(not segment.get("words") for segment in segments) else "live_fallback_no_activity"
        return live_fallback_segments_for_track(segments, track_index, reason), 0, True

    rate = max(1.0, float(fps or 30.0))
    all_words = [dict(word) for segment in segments for word in segment.get("words") or []]
    assigned_word_ids: set[int] = set()
    selected: list[dict[str, Any]] = []
    rejected_count = 0
    hold_frames_required = max(1, int(math.ceil(LIVE_SPEAKER_SWITCH_HOLD_SECONDS * rate)))

    speech_islands: list[dict[str, float]] = []
    for region in merge_live_vad_regions(speech_regions):
        region_start = float(region.get("start") or 0.0)
        region_end = float(region.get("end") or region_start)
        if region_end <= region_start:
            continue
        boundaries = [region_start, region_end]
        for turn in speaker_turns:
            if str(turn.get("speaker_decision") or "") != "switch":
                continue
            if int(turn.get("speaker_hold_frames") or 0) < hold_frames_required:
                continue
            if float(turn.get("speaker_dominance_db") or 0.0) < LIVE_SPEAKER_SWITCH_MARGIN_DB:
                continue
            boundary = (float(turn.get("start_frame") or timeline_start_frame) - int(timeline_start_frame)) / rate
            if region_start < boundary < region_end:
                boundaries.append(boundary)
        boundaries = sorted(set(boundaries))
        speech_islands.extend(
            {"start": left, "end": right}
            for left, right in zip(boundaries, boundaries[1:])
            if right > left
        )

    for region in speech_islands:
        region_start = float(region.get("start") or 0.0)
        region_end = float(region.get("end") or region_start)
        if region_end <= region_start:
            continue
        island_words: list[dict[str, Any]] = []
        for word in all_words:
            word_id = id(word)
            if word_id in assigned_word_ids:
                continue
            word_start = float(word.get("start") or 0.0)
            word_end = float(word.get("end") or word_start)
            midpoint = (word_start + word_end) * 0.5
            if region_start - 0.08 <= midpoint <= region_end + 0.08:
                assigned_word_ids.add(word_id)
                island_words.append(word)
        if not island_words:
            continue

        votes: dict[int, float] = {}
        score_totals: dict[int, float] = {}
        dominance_totals: dict[int, float] = {}
        hold_by_track: dict[int, int] = {}
        for word in island_words:
            word_start = float(word.get("start") or 0.0)
            word_end = float(word.get("end") or word_start)
            midpoint_frame = int(timeline_start_frame) + ((word_start + word_end) * 0.5 * rate)
            turn = live_speaker_turn_at_frame(speaker_turns, midpoint_frame)
            if not turn:
                continue
            owner = int(turn.get("speaker_track_index") or 0)
            weight = max(0.02, word_end - word_start)
            votes[owner] = votes.get(owner, 0.0) + weight
            score_totals[owner] = score_totals.get(owner, 0.0) + float(turn.get("speaker_score_db") or 0.0) * weight
            dominance_totals[owner] = dominance_totals.get(owner, 0.0) + float(turn.get("speaker_dominance_db") or 0.0) * weight
            hold_by_track[owner] = max(
                hold_by_track.get(owner, 0),
                int(float(turn.get("end_frame") or midpoint_frame) - float(turn.get("start_frame") or midpoint_frame)),
            )
        if not votes:
            rejected_count += len(island_words)
            continue
        owner = max(votes, key=lambda value: (votes[value], score_totals.get(value, 0.0)))
        if owner != int(track_index):
            rejected_count += len(island_words)
            continue

        owner_weight = max(1e-6, votes[owner])
        speaker_score = score_totals.get(owner, 0.0) / owner_weight
        speaker_dominance = dominance_totals.get(owner, 0.0) / owner_weight
        island_duration = region_end - region_start
        selected.append(
            {
                "start": float(island_words[0].get("start") or region_start),
                "end": float(island_words[-1].get("end") or region_end),
                "text": join_live_asr_words(island_words),
                "words": island_words,
                "speaker_track_index": int(track_index),
                "speaker_score_db": round(speaker_score, 3),
                "speaker_dominance_db": round(speaker_dominance, 3),
                "speaker_hold_frames": int(hold_by_track.get(owner, 0)),
                "speaker_decision": "speech_island_v2",
                "speech_island_id": f"{int(track_index)}:{region_start:.3f}-{region_end:.3f}",
                "speech_island_start": region_start,
                "speech_island_end": region_end,
                "short_utterance_confident": bool(
                    island_duration >= 0.18
                    and speaker_dominance >= LIVE_SPEAKER_SWITCH_MARGIN_DB
                    and hold_by_track.get(owner, 0) >= hold_frames_required
                ),
            }
        )

    unassigned_count = len(all_words) - len(assigned_word_ids)
    rejected_count += max(0, unassigned_count)
    if not selected and all_words:
        legacy_segments, legacy_rejected, _legacy_fallback = _filter_live_segments_for_track_legacy(
            segments,
            track_index,
            speaker_turns,
            fps,
            timeline_start_frame,
        )
        return legacy_segments, legacy_rejected, True
    return [segment for segment in selected if segment.get("text")], rejected_count, False


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
        return None
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


def qwen3_timestamp_words(value: Any) -> list[dict[str, Any]]:
    words: list[dict[str, Any]] = []

    def visit(node: Any) -> None:
        if node is None:
            return
        if isinstance(node, (list, tuple)):
            for child in node:
                visit(child)
            return
        for container_key in ("items", "words", "tokens", "segments"):
            items = qwen3_timestamp_value(node, container_key)
            if items is not None and items is not node:
                visit(items)
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
            if end_float <= start_float:
                end_float = start_float + 0.001
            words.append({"word": text, "start": start_float, "end": end_float})

    visit(value)
    return words


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
                "timeline_end_frame": int(float(batch["timeline_end_frame"])) if batch.get("timeline_end_frame") is not None else None,
                "audio_channel_index": int(batch["audio_channel_index"]) if int(batch.get("audio_channel_index") or 0) > 0 else None,
                "fps": float(batch.get("fps") or 30.0),
                "rows": rows,
            }
        )
    return batches


def load_generate_subtitles_batch_plan(batch_plan_json: str | None) -> list[dict[str, Any]]:
    if not batch_plan_json:
        return []
    payload = json.loads(Path(batch_plan_json).read_text(encoding="utf-8"))
    raw_batches = payload.get("batches") if isinstance(payload, dict) else payload
    batches: list[dict[str, Any]] = []
    for index, batch in enumerate(raw_batches or [], start=1):
        if not isinstance(batch, dict):
            continue
        source_start = float(batch.get("source_start") or 0.0)
        source_end = batch.get("source_end")
        source_end_float = float(source_end) if source_end is not None else None
        if source_end_float is not None and source_end_float <= source_start:
            continue
        batches.append(
            {
                "batch_id": str(batch.get("batch_id") or index),
                "audio": str(batch.get("audio") or batch.get("audio_path") or ""),
                "source_start": source_start,
                "source_end": source_end_float,
                "timeline_start_frame": int(float(batch.get("timeline_start_frame") or 0)),
                "timeline_end_frame": int(float(batch["timeline_end_frame"])) if batch.get("timeline_end_frame") is not None else None,
                "fps": float(batch.get("fps") or 30.0),
                "audio_channel_index": int(batch["audio_channel_index"]) if batch.get("audio_channel_index") else None,
                "track_order": int(float(batch.get("track_order") or batch.get("track_index") or 0)),
                "track_index": int(float(batch.get("track_index") or 0)),
                "track_name": str(batch.get("track_name") or ""),
                "item_index": int(float(batch.get("item_index") or 0)),
            }
        )
    return batches


def expand_generate_subtitles_batches(
    batches: list[dict[str, Any]],
    max_seconds: float | None = None,
) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    window_seconds = max(1.0, float(max_seconds if max_seconds is not None else generate_subtitles_batch_max_seconds()))
    for batch in batches or []:
        source_start = float(batch.get("source_start") or 0.0)
        source_end = batch.get("source_end")
        source_end_float = float(source_end) if source_end is not None else None
        if source_end_float is None or source_end_float - source_start <= window_seconds:
            next_batch = dict(batch)
            next_batch.setdefault("parent_batch_id", str(batch.get("batch_id") or len(expanded) + 1))
            next_batch.setdefault("batch_part_index", 1)
            next_batch.setdefault("batch_part_count", 1)
            expanded.append(next_batch)
            continue

        batch_fps = float(batch.get("fps") or 30.0)
        timeline_start_frame = int(batch.get("timeline_start_frame") or 0)
        part_count = int(math.ceil((source_end_float - source_start) / window_seconds))
        cursor = source_start
        parent_batch_id = str(batch.get("batch_id") or len(expanded) + 1)
        for part_index in range(1, part_count + 1):
            part_start = cursor
            part_end = min(source_end_float, source_start + (window_seconds * part_index))
            if part_end <= part_start:
                break
            next_batch = dict(batch)
            next_batch["parent_batch_id"] = parent_batch_id
            next_batch["batch_id"] = f"{parent_batch_id}_part{part_index:02d}"
            next_batch["batch_part_index"] = part_index
            next_batch["batch_part_count"] = part_count
            next_batch["source_start"] = part_start
            next_batch["source_end"] = part_end
            next_batch["timeline_start_frame"] = timeline_start_frame + int(round((part_start - source_start) * batch_fps))
            expanded.append(next_batch)
            cursor = part_end
    return expanded


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
        if "ctc_start_frame" in segment:
            aligned_row["ctc_start_frame"] = int(segment["ctc_start_frame"])
        if "ctc_end_frame" in segment:
            aligned_row["ctc_end_frame"] = int(segment["ctc_end_frame"])
        for key in (
            "ctc_confidence",
            "ctc_char_count",
            "alignment_mode",
            "row_remap_score",
            "row_remap_decision",
            "remap_text_candidate",
            "row_remap_target",
        ):
            if key in segment:
                aligned_row[key] = segment[key]
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
        ctc_start_frame = None
        ctc_end_frame = None
        if not row_chars:
            if count > 0 or fps is None:
                raise RuntimeError(f"CTC 字符时间为空: 字幕 #{row.get('index', len(segments) + 1)}")
            start = max(0.0, (float(row.get("start_frame") or 0) - float(timeline_start_frame or 0)) / max(float(fps), 1.0))
            end = max(start + 0.001, (float(row.get("end_frame") or 0) - float(timeline_start_frame or 0)) / max(float(fps), 1.0))
            confidence = 0.0
            ctc_start_frame = int(round(float(row.get("start_frame") or 0)))
            ctc_end_frame = int(round(float(row.get("end_frame") or ctc_start_frame + 1)))
        else:
            start = float(row_chars[0]["start"])
            end = max(float(row_chars[-1]["end"]), start + 0.001)
            confidence = sum(float(char.get("score") or 0.0) for char in row_chars) / max(1, len(row_chars))
            if fps is not None:
                ctc_start_frame = int(round(float(timeline_start_frame or 0) + start * float(fps)))
                ctc_end_frame = int(round(float(timeline_start_frame or 0) + end * float(fps)))
        segment = {
            "index": row.get("index"),
            "start": round(start, 3),
            "end": round(end, 3),
            "text": row.get("text") or "",
            "ctc_confidence": round(confidence, 4),
            "ctc_char_count": count,
        }
        if ctc_start_frame is not None:
            segment["ctc_start_frame"] = ctc_start_frame
        if ctc_end_frame is not None:
            segment["ctc_end_frame"] = max(int(ctc_end_frame), int(ctc_start_frame or 0) + 1)
        segments.append(segment)
    return segments


def qwen_timestamp_items_to_char_units(timestamp_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []
    for item in timestamp_items or []:
        char_text = normalize_ctc_text(str(item.get("word") or item.get("text") or ""))
        if not char_text:
            continue
        start = float(item.get("start") or 0.0)
        end = float(item.get("end") or start)
        raw_score = item.get("score")
        if raw_score is None:
            raw_score = item.get("confidence")
        score = float(raw_score) if raw_score is not None else 1.0
        for char in char_text:
            units.append({"char": char, "start": start, "end": end, "score": score})
    return units


def row_remap_length_tolerance(target_length: int) -> int:
    return max(3, int(math.ceil(max(1, target_length) * 0.25)))


def best_local_row_span(
    target_text: str,
    units: list[dict[str, Any]],
    cursor: int,
) -> dict[str, Any] | None:
    target_length = len(target_text)
    if target_length <= 0 or not units:
        return None
    length_tolerance = row_remap_length_tolerance(target_length)
    start_min = max(0, cursor - QWEN_ROW_REMAP_MAX_START_LOOKBACK)
    start_max = min(len(units) - 1, cursor + QWEN_ROW_REMAP_MAX_START_LOOKAHEAD)
    min_span_length = max(1, target_length - length_tolerance)
    max_span_length = max(min_span_length, target_length + length_tolerance)
    best: dict[str, Any] | None = None

    for start_index in range(start_min, start_max + 1):
        for span_length in range(min_span_length, max_span_length + 1):
            end_index = start_index + span_length
            if end_index > len(units):
                break
            candidate_text = "".join(str(unit.get("char") or "") for unit in units[start_index:end_index])
            if not candidate_text:
                continue
            score = difflib.SequenceMatcher(None, target_text, candidate_text).ratio()
            cursor_distance = abs(start_index - cursor)
            adjusted_score = score - (cursor_distance * 0.001)
            if (
                best is None
                or adjusted_score > best["adjusted_score"]
                or (
                    adjusted_score == best["adjusted_score"]
                    and abs(len(candidate_text) - target_length) < best["length_diff"]
                )
                or (
                    adjusted_score == best["adjusted_score"]
                    and abs(len(candidate_text) - target_length) == best["length_diff"]
                    and cursor_distance < best["cursor_distance"]
                )
            ):
                best = {
                    "start_index": start_index,
                    "end_index": end_index,
                    "text": candidate_text,
                    "score": score,
                    "adjusted_score": adjusted_score,
                    "cursor_distance": cursor_distance,
                    "length_diff": abs(len(candidate_text) - target_length),
                }
    return best


def preserved_row_segment(
    row: dict[str, Any],
    fps: float,
    timeline_start_frame: int,
    decision: str,
    score: float,
    candidate_text: str,
    target_text: str,
) -> dict[str, Any]:
    start_frame = int(round(float(row.get("start_frame") or 0)))
    end_frame = int(round(float(row.get("end_frame") or start_frame + 1)))
    start = max(0.0, (float(start_frame) - float(timeline_start_frame or 0)) / max(float(fps), 1.0))
    end = max(start + 0.001, (float(end_frame) - float(timeline_start_frame or 0)) / max(float(fps), 1.0))
    return {
        "index": row.get("index"),
        "start": round(start, 3),
        "end": round(end, 3),
        "text": row.get("text") or "",
        "ctc_start_frame": start_frame,
        "ctc_end_frame": max(end_frame, start_frame + 1),
        "ctc_confidence": round(max(0.0, min(1.0, float(score or 0.0))), 4),
        "ctc_char_count": len(target_text),
        "row_remap_score": round(max(0.0, min(1.0, float(score or 0.0))), 4),
        "row_remap_decision": decision,
        "remap_text_candidate": candidate_text,
        "row_remap_target": target_text,
        "alignment_mode": "qwen3_forced_aligner",
    }


def qwen3_remap_timestamp_items_to_rows(
    rows: list[dict[str, Any]],
    timestamp_items: list[dict[str, Any]],
    fps: float,
    timeline_start_frame: int,
    min_score: float = QWEN_ROW_REMAP_MIN_SCORE,
) -> list[dict[str, Any]]:
    units = qwen_timestamp_items_to_char_units(timestamp_items)
    row_units = ctc_units_for_rows(rows)
    segments: list[dict[str, Any]] = []
    cursor = 0

    for row, target_units in zip(rows, row_units, strict=True):
        target_text = "".join(target_units)
        if not target_text:
            segments.append(
                preserved_row_segment(row, fps, timeline_start_frame, "rejected_empty_text", 0.0, "", target_text)
            )
            continue
        if not units:
            segments.append(
                preserved_row_segment(row, fps, timeline_start_frame, "rejected_no_timestamp", 0.0, "", target_text)
            )
            continue

        best = best_local_row_span(target_text, units, cursor)
        candidate_text = str(best.get("text") or "") if best else ""
        score = float(best.get("score") or 0.0) if best else 0.0
        length_diff = int(best["length_diff"]) if best and "length_diff" in best else len(target_text)
        accepted = bool(best) and score >= min_score and length_diff <= row_remap_length_tolerance(len(target_text))
        if not accepted:
            segments.append(
                preserved_row_segment(row, fps, timeline_start_frame, "rejected_low_score", score, candidate_text, target_text)
            )
            # Advance by the target length so one bad row does not poison later rows.
            cursor = min(len(units), cursor + len(target_text))
            continue

        start_index = int(best["start_index"])
        end_index = int(best["end_index"])
        row_chars = units[start_index:end_index]
        start = float(row_chars[0]["start"])
        end = max(float(row_chars[-1]["end"]), start + 0.001)
        ctc_start_frame = int(round(float(timeline_start_frame or 0) + start * float(fps)))
        ctc_end_frame = max(
            int(round(float(timeline_start_frame or 0) + end * float(fps))),
            ctc_start_frame + 1,
        )
        segments.append(
            {
                "index": row.get("index"),
                "start": round(start, 3),
                "end": round(end, 3),
                "text": row.get("text") or "",
                "ctc_start_frame": ctc_start_frame,
                "ctc_end_frame": ctc_end_frame,
                "ctc_confidence": round(score, 4),
                "ctc_char_count": len(target_text),
                "row_remap_score": round(score, 4),
                "row_remap_decision": "accepted_local_match",
                "remap_text_candidate": candidate_text,
                "row_remap_target": target_text,
                "alignment_mode": "qwen3_forced_aligner",
            }
        )
        cursor = end_index
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
    min_region_seconds: float = 0.08,
    hangover_seconds: float = 0.12,
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
    noise_floor = percentile(rms_values, 0.20)
    peak_floor = percentile(peak_values, 0.20)
    rms_on_threshold = max(0.0005, noise_floor * 3.0)
    rms_off_threshold = max(0.0003, noise_floor * 2.0)
    peak_on_threshold = max(0.0015, peak_floor * 3.0)
    peak_off_threshold = max(0.0008, peak_floor * 2.0)

    regions: list[dict[str, float]] = []
    active_start: float | None = None
    last_active_end: float | None = None
    inactive_seconds = 0.0
    for window in windows:
        starts_speech = window["rms"] >= rms_on_threshold or window["peak"] >= peak_on_threshold
        continues_speech = window["rms"] >= rms_off_threshold or window["peak"] >= peak_off_threshold
        if active_start is None and starts_speech:
            active_start = window["start"]
            last_active_end = window["end"]
            inactive_seconds = 0.0
        elif active_start is not None and continues_speech:
            last_active_end = window["end"]
            inactive_seconds = 0.0
        elif active_start is not None:
            inactive_seconds += max(0.0, float(window["end"]) - float(window["start"]))
            if inactive_seconds < hangover_seconds:
                continue
            if last_active_end is not None:
                regions.append({"start": active_start, "end": last_active_end})
            active_start = None
            last_active_end = None
            inactive_seconds = 0.0
    if active_start is not None and last_active_end is not None:
        regions.append({"start": active_start, "end": last_active_end})

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
        "vad_mode": "hysteresis",
        "speech_region_count": len(merged),
        "speech_onset_count": len(onsets),
        "rms_threshold": round(rms_on_threshold, 6),
        "peak_threshold": round(peak_on_threshold, 6),
        "rms_on_threshold": round(rms_on_threshold, 6),
        "rms_off_threshold": round(rms_off_threshold, 6),
        "peak_on_threshold": round(peak_on_threshold, 6),
        "peak_off_threshold": round(peak_off_threshold, 6),
        "hangover_seconds": hangover_seconds,
        "sample_rate": sample_rate,
        "window_seconds": window_seconds,
    }
    return merged, onsets, diagnostic


def read_live_rms_windows(
    wav_path: Path,
    fps: float,
    timeline_start_frame: int,
    window_seconds: float = 0.01,
    smoothing_seconds: float = 0.10,
) -> list[dict[str, float]]:
    with wave.open(str(wav_path), "rb") as handle:
        sample_rate = handle.getframerate()
        channels = handle.getnchannels()
        sample_width = handle.getsampwidth()
        raw = handle.readframes(handle.getnframes())

    samples = decode_pcm_samples(raw, sample_width, channels)
    window_size = max(1, int(sample_rate * window_seconds))
    raw_windows: list[dict[str, float]] = []
    for start in range(0, len(samples), window_size):
        chunk = samples[start : start + window_size]
        if not chunk:
            continue
        rms = math.sqrt(sum(sample * sample for sample in chunk) / len(chunk))
        raw_windows.append(
            {
                "start_seconds": start / sample_rate,
                "end_seconds": min(len(samples), start + len(chunk)) / sample_rate,
                "rms": rms,
            }
        )

    radius = max(0, int(round(smoothing_seconds / max(window_seconds, 0.001) / 2.0)))
    prefix = [0.0]
    for window in raw_windows:
        prefix.append(prefix[-1] + float(window["rms"]))
    rate = max(1.0, float(fps or 30.0))
    output: list[dict[str, float]] = []
    for index, window in enumerate(raw_windows):
        left = max(0, index - radius)
        right = min(len(raw_windows), index + radius + 1)
        smoothed_rms = (prefix[right] - prefix[left]) / max(1, right - left)
        output.append(
            {
                "start_frame": int(timeline_start_frame) + float(window["start_seconds"]) * rate,
                "end_frame": int(timeline_start_frame) + float(window["end_seconds"]) * rate,
                "rms": smoothed_rms,
            }
        )
    return output


def build_live_activity_windows(
    work_items: list[dict[str, Any]],
) -> tuple[dict[int, list[dict[str, Any]]], dict[str, Any]]:
    raw_by_track: dict[int, list[dict[str, float]]] = {}
    skipped_item_count = 0
    for item in work_items or []:
        cut_path = item.get("cut_path")
        batch = item.get("batch") if isinstance(item.get("batch"), dict) else {}
        track_index = int(batch.get("track_index") or 0)
        if not cut_path or track_index <= 0 or not Path(cut_path).is_file():
            skipped_item_count += 1
            continue
        windows = read_live_rms_windows(
            Path(cut_path),
            float(item.get("batch_fps") or batch.get("fps") or 30.0),
            int(item.get("timeline_start_frame") or batch.get("timeline_start_frame") or 0),
        )
        raw_by_track.setdefault(track_index, []).extend(windows)

    activity_by_track: dict[int, list[dict[str, Any]]] = {}
    noise_floor_by_track: dict[str, float] = {}
    for track_index, windows in raw_by_track.items():
        rms_values = [float(window.get("rms") or 0.0) for window in windows]
        noise_floor = max(1e-6, percentile(rms_values, 0.20))
        noise_floor_by_track[str(track_index)] = round(noise_floor, 6)
        normalized = []
        for window in windows:
            rms = max(1e-9, float(window.get("rms") or 0.0))
            normalized.append(
                {
                    "start_frame": float(window.get("start_frame") or 0.0),
                    "end_frame": float(window.get("end_frame") or 0.0),
                    "activity_db": round(20.0 * math.log10(rms / noise_floor), 3),
                }
            )
        activity_by_track[track_index] = normalized

    return activity_by_track, {
        "activity_track_count": len(activity_by_track),
        "activity_item_count": sum(1 for item in work_items or [] if item.get("cut_path")),
        "activity_skipped_item_count": skipped_item_count,
        "activity_noise_floor_by_track": noise_floor_by_track,
    }


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


def load_qwen3_asr_model(model: str) -> tuple[Any, str, str, str | None]:
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
    except Exception as exc:  # pragma: no cover - depends on optional local setup/model cache
        raise RuntimeError(f"Qwen3-ASR 模型加载失败: {exc}") from exc
    return qwen_model, model_name, aligner_name, device_map


def load_qwen3_forced_aligner() -> Any:
    try:
        import torch  # type: ignore
        from qwen_asr import Qwen3ForcedAligner  # type: ignore
    except Exception as exc:  # pragma: no cover - optional local runtime
        raise RuntimeError("Qwen3-ForcedAligner 环境未安装，请运行 setup_asr_env.sh") from exc

    model_name = os.getenv("SUBFIX_QWEN3_ALIGNER_MODEL") or QWEN3_FORCED_ALIGNER_MODEL
    device_map = os.getenv("SUBFIX_QWEN3_ASR_DEVICE_MAP") or "auto"
    dtype = qwen3_torch_dtype(torch)
    cache_key = (model_name, device_map, str(dtype))
    cached = _QWEN3_FORCED_ALIGNER_CACHE.get(cache_key)
    if cached is not None:
        return cached
    kwargs: dict[str, Any] = {"device_map": device_map}
    if dtype is not None:
        kwargs["dtype"] = dtype
    try:
        aligner = Qwen3ForcedAligner.from_pretrained(model_name, **kwargs)
    except Exception as exc:  # pragma: no cover - optional local runtime/model cache
        raise RuntimeError(f"Qwen3-ForcedAligner 模型加载失败: {exc}") from exc
    _QWEN3_FORCED_ALIGNER_CACHE[cache_key] = aligner
    return aligner


def qwen3_force_align_items(audio_path: Path, text: str, language: str) -> list[dict[str, Any]]:
    aligner = load_qwen3_forced_aligner()
    try:
        results = aligner.align(audio=str(audio_path), text=str(text), language=str(language))
    except Exception as exc:  # pragma: no cover - optional local runtime/model execution
        raise RuntimeError(f"Qwen3-ForcedAligner 对齐失败: {exc}") from exc
    first = results[0] if isinstance(results, list) and results else results
    items = qwen3_timestamp_value(first, "items") or first or []
    normalized: list[dict[str, Any]] = []
    for item in items:
        unit_text = str(qwen3_timestamp_value(item, "text", "word") or "")
        start = qwen3_timestamp_value(item, "start_time", "start")
        end = qwen3_timestamp_value(item, "end_time", "end")
        if unit_text and start is not None and end is not None and float(end) > float(start):
            normalized.append({"text": unit_text, "start": float(start), "end": float(end)})
    return normalized


def qwen3_result_to_payload(
    result: Any,
    audio_path: Path,
    requested_model: str,
    model_name: str,
    aligner_name: str,
    device_map: str,
) -> dict[str, Any]:
    text = str(qwen3_timestamp_value(result, "text") or "").strip()
    language_name = qwen3_timestamp_value(result, "language")
    timestamp_payload = qwen3_timestamp_value(result, "time_stamps", "timestamps", "segments")
    words = qwen3_timestamp_words(timestamp_payload)
    segments = qwen3_timestamp_segments(timestamp_payload)
    if text and words:
        segments = [{"start": words[0]["start"], "end": words[-1]["end"], "text": text, "words": words}]
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
            "requested_model": requested_model,
            "forced_align_item_count": len(words),
            "uses_word_timing": bool(words),
        },
    }


def transcribe_qwen3_asr(audio_path: Path, model: str, language: str | None) -> dict[str, Any]:
    qwen_model, model_name, aligner_name, device_map = load_qwen3_asr_model(model)
    try:
        results = qwen_model.transcribe(
            audio=str(audio_path),
            language=qwen3_language_name(language),
            return_time_stamps=True,
        )
    except Exception as exc:  # pragma: no cover - depends on optional local setup/model cache
        raise RuntimeError(f"Qwen3-ASR 转写失败: {exc}") from exc
    first_result = results[0] if isinstance(results, list) and results else results
    return qwen3_result_to_payload(first_result, audio_path, model, model_name, aligner_name, device_map)


def transcribe_qwen3_asr_batch(audio_paths: list[Path], model: str, language: str | None) -> list[dict[str, Any]]:
    if not audio_paths:
        return []
    qwen_model, model_name, aligner_name, device_map = load_qwen3_asr_model(model)
    try:
        results = qwen_model.transcribe(
            audio=[str(path) for path in audio_paths],
            language=qwen3_language_name(language),
            return_time_stamps=True,
        )
    except Exception as exc:  # pragma: no cover - depends on optional local setup/model cache
        raise RuntimeError(f"Qwen3-ASR 批量转写失败: {exc}") from exc
    if not isinstance(results, list):
        results = [results]
    if len(results) != len(audio_paths):
        raise RuntimeError(f"Qwen3-ASR 批量结果数量不匹配: audio={len(audio_paths)} result={len(results)}")
    return [
        qwen3_result_to_payload(result, audio_path, model, model_name, aligner_name, device_map)
        for result, audio_path in zip(results, audio_paths)
    ]


def _doubao_credentials_path() -> Path:
    """Path to the optional doubao_credentials.json next to this helper."""
    return Path(__file__).with_name(DOUBAO_ASR_CREDENTIALS_FILENAME)


def _load_doubao_credentials_file() -> dict[str, Any]:
    """Read the single API Key from doubao_credentials.json if present.

    Returns an empty dict when the file is missing, unreadable, or not a JSON
    object -- callers fall back to raising a clear "no credentials" error.
    Never raises on a malformed/absent file so a stray file can't crash ASR.
    """
    try:
        raw = _doubao_credentials_path().read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    api_key = str(data.get("api_key") or "").strip()
    return {"api_key": api_key} if api_key else {}


def _doubao_asr_request_once(
    endpoint: str,
    headers: dict[str, str],
    body_bytes: bytes,
    timeout_seconds: float,
) -> tuple[str, str, str]:
    """Issue a single HTTP POST and return (status_code, response_text, log_id).

    Uses the stdlib urllib (no new dependency) rather than the `requests`
    package, since `requests` is not otherwise imported by this module.

    log_id is Volcano's per-request trace id (X-Tt-Logid), returned so it can
    be recorded in the generate diagnostic -- with it the user can look up in
    the console / a support ticket whether a given call was actually billed
    and against which quota/tier.
    """
    request = urllib.request.Request(endpoint, data=body_bytes, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        # 待校准: 状态码文档记录在响应头 X-Api-Status-Code 中；如实际账号返回改为响应体
        # 顶层 code 字段，需要在此处同步兼容。
        status_code = response.headers.get("X-Api-Status-Code") or ""
        response_text = response.read().decode("utf-8", errors="replace")
        # 火山每个请求的唯一追踪 ID，用于账务/工单精确核对本次调用是否计费。
        log_id = response.headers.get("X-Tt-Logid") or response.headers.get("X-Api-Request-Id") or ""
    return status_code, response_text, log_id


def _doubao_asr_timestamp_seconds(value: Any) -> float | None:
    try:
        return float(value) / 1000.0
    except (TypeError, ValueError):
        return None


def _doubao_asr_timestamp_payload(result: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(result, dict):
        return [], []
    segments: list[dict[str, Any]] = []
    words: list[dict[str, Any]] = []
    for utterance in result.get("utterances") or []:
        if not isinstance(utterance, dict):
            continue
        start = _doubao_asr_timestamp_seconds(utterance.get("start_time"))
        end = _doubao_asr_timestamp_seconds(utterance.get("end_time"))
        if start is None or end is None or end <= start:
            continue
        segment_words: list[dict[str, Any]] = []
        for item in utterance.get("words") or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or item.get("word") or "").strip()
            word_start = _doubao_asr_timestamp_seconds(item.get("start_time"))
            word_end = _doubao_asr_timestamp_seconds(item.get("end_time"))
            if text and word_start is not None and word_end is not None and word_end > word_start:
                word = {"word": text, "start": word_start, "end": word_end}
                segment_words.append(word)
                words.append(word)
        if segment_words:
            segments.append(
                {
                    "start": start,
                    "end": end,
                    "text": str(utterance.get("text") or "").strip(),
                    "words": segment_words,
                }
            )
    return segments, words


def transcribe_doubao_asr(audio_path: Path, model: str, language: str | None) -> dict[str, Any]:
    """Transcribe via Volcano Engine (豆包) 录音文件极速版识别 HTTP API.

    Word timestamps are normalized from the API's milliseconds to seconds.
    The v4/v5 pipeline validates them before use and falls back to Qwen forced
    alignment only when they are missing or unreliable.

    Configuration uses one API Key only. Legacy App ID / Access Token files
    are intentionally ignored so an empty API Key can never spend cloud quota.
    Optional (env only): SUBFIX_DOUBAO_RESOURCE_ID, SUBFIX_DOUBAO_ENDPOINT.

    Raises RuntimeError with a clear message on missing credentials, HTTP
    errors, timeouts, or malformed responses. Callers surface these failures
    to Resolve so the user can retry or explicitly switch to local Qwen.
    """
    api_key = str(os.getenv(DOUBAO_ASR_API_KEY_ENV) or "").strip()
    if not api_key:
        creds = _load_doubao_credentials_file()
        api_key = str(creds.get("api_key") or "").strip()
    if not api_key:
        raise RuntimeError(
            "豆包 ASR 未配置密钥：请设置环境变量 "
            f"{DOUBAO_ASR_API_KEY_ENV}，"
            f"或在 {_doubao_credentials_path()} 写入 "
            '{"api_key": "..."} 后重试'
        )
    resource_id = os.getenv(DOUBAO_ASR_RESOURCE_ID_ENV) or DOUBAO_ASR_DEFAULT_RESOURCE_ID
    endpoint = os.getenv(DOUBAO_ASR_ENDPOINT_ENV) or DOUBAO_ASR_DEFAULT_ENDPOINT

    audio_bytes = Path(audio_path).read_bytes()
    if not audio_bytes:
        raise RuntimeError(f"豆包 ASR 音频文件为空: {audio_path}")

    # 待校准: 请求体字段仅确认 user.uid / audio.data(或 audio.url) / request.model_name；
    # 文档 demo 未展示 audio.format/audio.codec 字段，暂不填写，若真实响应要求
    # 音频格式声明，需要在这里补充（cut_audio 输出固定为 16k/mono/wav）。
    body = {
        "user": {"uid": api_key},
        "audio": {"data": base64.b64encode(audio_bytes).decode("ascii")},
        "request": {"model_name": "bigmodel"},
    }
    headers = {
        "Content-Type": "application/json",
        "X-Api-Resource-Id": resource_id,
        "X-Api-Request-Id": str(uuid.uuid4()),
        "X-Api-Sequence": "-1",
    }
    headers["X-Api-Key"] = api_key
    body_bytes = json.dumps(body).encode("utf-8")

    last_error: Exception | None = None
    for attempt in range(1, DOUBAO_ASR_MAX_ATTEMPTS + 1):
        try:
            status_code, response_text, log_id = _doubao_asr_request_once(
                endpoint, headers, body_bytes, DOUBAO_ASR_TIMEOUT_SECONDS
            )
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            last_error = RuntimeError(f"豆包 ASR HTTP {exc.code}: {exc.reason}; {error_body[:500]}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = RuntimeError(f"豆包 ASR 请求失败（网络/超时）: {exc}")
        else:
            try:
                response_payload = json.loads(response_text) if response_text else {}
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"豆包 ASR 响应不是合法 JSON: {exc}") from exc
            if not isinstance(response_payload, dict):
                raise RuntimeError("豆包 ASR 响应 JSON 必须是对象")
            if status_code and status_code != DOUBAO_ASR_SUCCESS_STATUS_CODE:
                message = str(response_payload.get("message") or response_text[:300])
                last_error = RuntimeError(f"豆包 ASR 返回错误状态 {status_code}: {message}")
            else:
                result = response_payload.get("result")
                text = str((result or {}).get("text") or "").strip()
                segments, words = _doubao_asr_timestamp_payload(result)
                return {
                    "backend": "doubao_asr",
                    "model": model,
                    "language": language,
                    "segments": segments,
                    "words": words,
                    "text": text,
                    "diagnostic": {
                        "resource_id": resource_id,
                        "endpoint": endpoint,
                        "status_code": status_code,
                        "log_id": log_id,
                        "attempt": attempt,
                    },
                }
        if attempt < DOUBAO_ASR_MAX_ATTEMPTS:
            time.sleep(DOUBAO_ASR_RETRY_BACKOFF_SECONDS * attempt)

    raise RuntimeError(str(last_error) if last_error else "豆包 ASR 请求失败: 未知错误")


def transcribe_v4_window_batch(
    window_audio_paths: list[Path],
    args: argparse.Namespace,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Resolve per-window ASR text payloads for the v4/v5 main pipeline.

    This is the single dispatch point that keeps
    run_generate_subtitles_batch_plan_v4's first-step transcription
    switchable by backend while leaving segmentation, profiles, and
    SUBFIX_V5_WRITEBACK untouched:
      - default / any backend other than "doubao_asr": calls
        transcribe_qwen3_asr_batch exactly as before (zero behavior change).
      - backend == "doubao_asr": transcribes each window individually via
        Volcano's flash-recognize HTTP API (one HTTP request per window --
        there is no native batch endpoint documented, so mind QPS limits and
        the ~0.8 CNY/hour billing when testing on long batches). A per-window
        failure is surfaced to Resolve with the affected filename and original
        cause; valid word timestamps are consumed by the existing alignment
        validator, otherwise it falls back to Qwen. Resolve then lets the
        user explicitly retry or switch to Qwen for request failures.

    Returns (payloads, diagnostic) where diagnostic carries
    asr_backend_used / doubao_fallback_count (+ error detail) for the
    generate-batch diagnostic dict (see sanitize_generate_diagnostic_payload
    for the whitelist these keys must be added to).
    """
    backend = str(getattr(args, "backend", "auto") or "auto")
    if backend != "doubao_asr":
        payloads = transcribe_qwen3_asr_batch(window_audio_paths, args.model, args.language)
        return payloads, {"asr_backend_used": "qwen3_asr", "doubao_fallback_count": 0}

    payloads = []
    doubao_log_ids: list[str] = []
    doubao_resource_ids: list[str] = []
    doubao_status_codes: list[str] = []
    for window_audio_path in window_audio_paths:
        try:
            payload = transcribe_doubao_asr(window_audio_path, args.model, args.language)
            payloads.append(payload)
            # 收集火山返回的追踪/计费线索，便于在诊断里核对每次调用是否真计费。
            call_diag = payload.get("diagnostic") or {}
            if call_diag.get("log_id"):
                doubao_log_ids.append(str(call_diag["log_id"]))
            if call_diag.get("resource_id"):
                doubao_resource_ids.append(str(call_diag["resource_id"]))
            if call_diag.get("status_code"):
                doubao_status_codes.append(str(call_diag["status_code"]))
        except Exception as exc:
            raise RuntimeError(
                f"豆包 ASR 失败（{Path(window_audio_path).name}）: {exc}"
            ) from exc
    diagnostic: dict[str, Any] = {
        "asr_backend_used": "doubao_asr",
        "doubao_fallback_count": 0,
    }
    if doubao_log_ids:
        diagnostic["doubao_log_ids"] = doubao_log_ids
    if doubao_resource_ids:
        # 去重保序：同一批次通常同一个 resource_id。
        diagnostic["doubao_resource_ids"] = list(dict.fromkeys(doubao_resource_ids))
    if doubao_status_codes:
        diagnostic["doubao_status_codes"] = list(dict.fromkeys(doubao_status_codes))
    return payloads, diagnostic


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
    # "auto" never silently tries doubao_asr (paid/credential-gated); it must
    # be requested explicitly. See AUTO_TRANSCRIBE_BACKENDS definition.
    candidates = list(AUTO_TRANSCRIBE_BACKENDS) if requested_backend == "auto" else [requested_backend]
    fallback_errors: list[str] = []

    for candidate in candidates:
        try:
            if candidate == "mlx_whisper":
                payload = transcribe_mlx_whisper(audio_path, model, language)
            elif candidate == "openai_whisper":
                payload = transcribe_openai_whisper(audio_path, model, language)
            elif candidate == "qwen3_asr":
                payload = transcribe_qwen3_asr(audio_path, model, language)
            elif candidate == "doubao_asr":
                payload = transcribe_doubao_asr(audio_path, model, language)
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


def should_try_qwen_batch_backend(backend: str) -> bool:
    enabled = str(os.getenv("SUBFIX_ENABLE_QWEN_BATCH") or "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return False
    if os.getenv("SUBFIX_DISABLE_QWEN_BATCH"):
        return False
    if backend == "qwen3_asr":
        return True
    if backend != "auto":
        return False
    mimo_env = EXTERNAL_TRANSCRIBE_COMMAND_ENV.get("mimo_asr")
    return not bool(mimo_env and os.getenv(mimo_env))


def qwen_generate_batch_size() -> int:
    raw_value = os.getenv("SUBFIX_QWEN_GENERATE_BATCH_SIZE")
    if raw_value:
        try:
            return max(1, int(raw_value))
        except ValueError:
            pass
    return 8


def recover_v4_asr_window(
    *,
    window: dict[str, Any],
    payload: dict[str, Any],
    track_audio_path: Path,
    track_start_frame: int,
    retry_dir: Path,
    model: str,
    language: str | None,
    transcribe_fn: Any = None,
    detect_speech_fn: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Recover a speech-bearing v4 window that ASR returned as punctuation only."""
    diagnostic = {
        "asr_recovery": "not_needed",
        "asr_single_retry_count": 0,
        "asr_subwindow_retry_count": 0,
        "speech_seconds": 0.0,
    }
    if generate_v4.normalize_text(payload.get("text")):
        return dict(payload), diagnostic

    transcribe_fn = transcribe_fn or transcribe_qwen3_asr
    detect_speech_fn = detect_speech_fn or detect_speech_regions
    window_audio_path = Path(str(window.get("audio_path") or ""))
    speech_regions, _onsets, _speech_diagnostic = detect_speech_fn(window_audio_path)
    speech_seconds = sum(
        max(0.0, float(region.get("end") or 0.0) - float(region.get("start") or 0.0))
        for region in speech_regions or []
    )
    diagnostic["speech_seconds"] = round(speech_seconds, 3)
    if speech_seconds < 0.30:
        diagnostic["asr_recovery"] = "accepted_silence"
        return dict(payload), diagnostic

    diagnostic["asr_single_retry_count"] = 1
    serial_payload = dict(transcribe_fn(window_audio_path, model, language) or {})
    if generate_v4.normalize_text(serial_payload.get("text")):
        diagnostic["asr_recovery"] = "single_window"
        return serial_payload, diagnostic

    fps = float(window.get("fps") or 30.0)
    retry_windows = generate_v4.build_context_windows(
        int(window.get("start_frame") or 0),
        int(window.get("end_frame") or 0),
        fps,
        window_seconds=24.0,
        stride_seconds=20.0,
    )
    if len(retry_windows) < 2:
        raise generate_v4.V4AlignmentError("v4 有人声窗口转写为空，已终止写回")

    retry_dir = Path(retry_dir)
    retry_dir.mkdir(parents=True, exist_ok=True)
    texts: list[str] = []
    for retry_window in retry_windows:
        retry_path = retry_dir / (
            f"track_{int(window.get('track_index') or 0):03d}_"
            f"window_{int(window.get('window_index') or 0):04d}_"
            f"retry_{int(retry_window.get('window_index') or 0):04d}.wav"
        )
        generate_v4.write_context_window_audio(
            Path(track_audio_path),
            retry_path,
            int(track_start_frame),
            int(retry_window["start_frame"]),
            int(retry_window["end_frame"]),
            fps,
        )
        diagnostic["asr_subwindow_retry_count"] += 1
        retry_payload = dict(transcribe_fn(retry_path, model, language) or {})
        retry_text = str(retry_payload.get("text") or "")
        if not generate_v4.normalize_text(retry_text):
            retry_regions, _retry_onsets, _retry_diagnostic = detect_speech_fn(retry_path)
            retry_speech_seconds = sum(
                max(0.0, float(region.get("end") or 0.0) - float(region.get("start") or 0.0))
                for region in retry_regions or []
            )
            if retry_speech_seconds >= 0.30:
                raise generate_v4.V4AlignmentError("v4 有人声子窗口转写为空，已终止写回")
            continue
        texts.append(retry_text)

    merged_text = ""
    for text in texts:
        merged_text = generate_v4.merge_asr_transcripts(merged_text, text)
    if not generate_v4.normalize_text(merged_text):
        raise generate_v4.V4AlignmentError("v4 有人声窗口转写为空，已终止写回")

    recovered = dict(serial_payload or payload)
    recovered["text"] = merged_text
    recovered["segments"] = []
    diagnostic["asr_recovery"] = "short_subwindows"
    return recovered, diagnostic


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


def run_generate_subtitles_batch_plan_v3(
    batches: list[dict[str, Any]],
    args: argparse.Namespace,
    progress_path: Path | None,
    fixture_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # NOTE: legacy v3 path; intentionally does not read args.max_chars (see
    # generate_subtitle_rows_from_segments / GENERATED_SUBTITLE_MAX_CHARS note
    # above). Only the v4/v5 path honours --max-chars.
    if not batches:
        raise RuntimeError("缺少 generate_subtitles batch plan")
    source_batch_count = len(batches)
    batches = expand_generate_subtitles_batches(batches)
    subtitle_mode = str(getattr(args, "subtitle_mode", "narration") or "narration")
    if subtitle_mode not in {"narration", "live"}:
        subtitle_mode = "narration"
    ffmpeg_path = resolve_ffmpeg(args.ffmpeg)
    segmentation_profile = load_segmentation_profile(getattr(args, "segmentation_profile", None))
    active_segmentation_profile = segmentation_profile_for_mode(segmentation_profile, subtitle_mode)
    total_batches = len(batches)
    progress_total = generate_batch_progress_total(total_batches)
    output_batches: list[dict[str, Any]] = []
    subtitle_rows: list[dict[str, Any]] = []
    backend = ""
    model = ""
    fallback_errors: list[str] = []
    diagnostic: dict[str, Any] = {
        "mode": "fixture" if fixture_payload is not None else args.mode,
        "requested_mode": args.mode,
        "ffmpeg": ffmpeg_path,
        "source_batch_count": source_batch_count,
        "batch_count": total_batches,
        "batch_max_seconds": generate_subtitles_batch_max_seconds(),
        "requested_backend": args.backend,
        "subtitle_mode": subtitle_mode,
        "live_engine": str(os.getenv("SUBFIX_LIVE_ENGINE") or "islands_v2"),
        "qwen_batch_used": False,
        "qwen_batch_size": 0,
        "speaker_turn_count": 0,
        "speaker_switch_count": 0,
        "bleed_rejected_count": 0,
        "ambiguous_window_count": 0,
        "overlap_suppressed_count": 0,
        "live_fallback_no_word_timing": False,
        "speech_island_count": 0,
        "confirmed_silence_count": 0,
        "vad_only_gap_count": 0,
        "segmentation_profile_used": bool(active_segmentation_profile),
        "segmentation_profile_path": str((segmentation_profile or {}).get("_path") or ""),
        "segmentation_profile_schema": str((segmentation_profile or {}).get("schema_version") or ""),
    }

    with tempfile.TemporaryDirectory(prefix="subfix_generate_batches_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        work_items: list[dict[str, Any]] = []
        for batch_index, batch in enumerate(batches, start=1):
            batch_id = str(batch.get("batch_id") or batch_index)
            batch_fps = float(batch.get("fps") or args.fps or 30.0)
            timeline_start_frame = int(batch.get("timeline_start_frame") or 0)
            write_progress(
                progress_path,
                "prepare_subtitle_batch",
                f"准备 {batch_index}/{total_batches}",
                batch_index=batch_index,
                total_batches=total_batches,
                progress_index=batch_index,
                progress_total=progress_total,
                batch_id=batch_id,
            )
            try:
                if fixture_payload is not None:
                    cut_path = None
                    raw_payload: dict[str, Any] | None = dict(fixture_payload)
                    cut_audio_bytes = 0
                    onset_diagnostic: dict[str, Any] = {}
                    speech_regions = list(raw_payload.get("speech_regions") or [])
                else:
                    audio_path = Path(str(batch.get("audio") or ""))
                    if not audio_path.exists():
                        raise RuntimeError(f"audio file not found: {audio_path}")
                    cut_path = tmp_path / f"generate_batch_{batch_index:04d}.wav"
                    cut_audio(
                        ffmpeg_path,
                        audio_path,
                        cut_path,
                        float(batch.get("source_start") or 0.0),
                        batch.get("source_end"),
                        int(batch["audio_channel_index"]) if batch.get("audio_channel_index") else None,
                    )
                    cut_audio_bytes = cut_path.stat().st_size if cut_path.exists() else 0
                    speech_regions, _speech_onsets, onset_diagnostic = detect_speech_regions(cut_path)
                    diagnostic["vad_only_gap_count"] = int(diagnostic.get("vad_only_gap_count") or 0) + sum(
                        1
                        for left_region, right_region in zip(speech_regions, speech_regions[1:])
                        if float(right_region.get("start") or 0.0) - float(left_region.get("end") or 0.0)
                        >= GENERATED_SUBTITLE_HARD_SILENCE_SECONDS
                    )
                    raw_payload = None
                work_items.append(
                    {
                        "batch_index": batch_index,
                        "batch": batch,
                        "batch_id": batch_id,
                        "batch_fps": batch_fps,
                        "timeline_start_frame": timeline_start_frame,
                        "cut_path": cut_path,
                        "cut_audio_bytes": cut_audio_bytes,
                        "onset_diagnostic": onset_diagnostic,
                        "speech_regions": speech_regions,
                        "raw_payload": raw_payload,
                    }
                )
            except Exception as exc:
                output_batches.append(
                    {
                        "ok": False,
                        "batch_id": batch_id,
                        "parent_batch_id": str(batch.get("parent_batch_id") or batch_id),
                        "batch_part_index": int(batch.get("batch_part_index") or 1),
                        "batch_part_count": int(batch.get("batch_part_count") or 1),
                        "track_order": int(batch.get("track_order") or 0),
                        "track_index": int(batch.get("track_index") or 0),
                        "track_name": str(batch.get("track_name") or ""),
                        "item_index": int(batch.get("item_index") or 0),
                        "audio": str(batch.get("audio") or ""),
                        "source_start": float(batch.get("source_start") or 0.0),
                        "source_end": batch.get("source_end"),
                        "error": str(exc),
                        "segments": [],
                        "subtitle_rows": [],
                    }
                )

        speaker_turns: list[dict[str, Any]] = []
        if subtitle_mode == "live":
            activity_by_track, activity_diagnostic = build_live_activity_windows(work_items)
            speaker_turns, speaker_diagnostic = build_live_speaker_turns(
                activity_by_track,
                float(args.fps or (work_items[0].get("batch_fps") if work_items else 30.0) or 30.0),
            )
            diagnostic.update(activity_diagnostic)
            diagnostic.update(speaker_diagnostic)
            if len(activity_by_track) < 2 or not speaker_turns:
                diagnostic["live_fallback_no_activity"] = True

        # Qwen accepts a list of audio inputs; batch after cutting so the model and forced aligner schedule windows together.
        if fixture_payload is None and work_items and should_try_qwen_batch_backend(str(args.backend or "auto")):
            qwen_batch_size = qwen_generate_batch_size()
            qwen_chunks = [work_items[index : index + qwen_batch_size] for index in range(0, len(work_items), qwen_batch_size)]
            diagnostic["qwen_batch_size"] = qwen_batch_size
            diagnostic["qwen_batch_call_count"] = len(qwen_chunks)
            diagnostic["qwen_batch_failed_call_count"] = 0
            for chunk_index, chunk_items in enumerate(qwen_chunks, start=1):
                first_window = int(chunk_items[0]["batch_index"])
                last_window = int(chunk_items[-1]["batch_index"])
                try:
                    write_progress(
                        progress_path,
                        "generate_subtitle_batch",
                        f"批量识别 {chunk_index}/{len(qwen_chunks)}｜窗口 {first_window}-{last_window}/{total_batches}",
                        batch_index=last_window,
                        total_batches=total_batches,
                        progress_index=total_batches + last_window,
                        progress_total=progress_total,
                        batch_id="qwen_batch",
                        qwen_batch_index=chunk_index,
                        qwen_batch_count=len(qwen_chunks),
                    )
                    qwen_payloads = transcribe_qwen3_asr_batch(
                        [item["cut_path"] for item in chunk_items if item.get("cut_path")],
                        args.model,
                        args.language,
                    )
                    if len(qwen_payloads) != len(chunk_items):
                        raise RuntimeError(f"Qwen batch payload mismatch: {len(qwen_payloads)} != {len(chunk_items)}")
                    for item, raw_payload in zip(chunk_items, qwen_payloads):
                        item["raw_payload"] = raw_payload
                    diagnostic["qwen_batch_used"] = True
                except Exception as exc:
                    diagnostic["qwen_batch_failed_call_count"] = int(diagnostic.get("qwen_batch_failed_call_count") or 0) + 1
                    failed_errors = diagnostic.setdefault("qwen_batch_errors", [])
                    if isinstance(failed_errors, list):
                        failed_errors.append(str(exc))
                    for item in chunk_items:
                        item["raw_payload"] = None

        for item in work_items:
            batch_index = int(item["batch_index"])
            batch = item["batch"]
            batch_id = str(item["batch_id"])
            batch_fps = float(item["batch_fps"])
            timeline_start_frame = int(item["timeline_start_frame"])
            try:
                raw_payload = item.get("raw_payload")
                if raw_payload is None:
                    write_progress(
                        progress_path,
                        "generate_subtitle_batch",
                        f"识别 {batch_index}/{total_batches}",
                        batch_index=batch_index,
                        total_batches=total_batches,
                        progress_index=total_batches + batch_index,
                        progress_total=progress_total,
                        batch_id=batch_id,
                    )
                    raw_payload = transcribe_with_backend(item["cut_path"], args.model, args.language, args.backend)

                segments = normalize_segments(raw_payload)
                live_fallback = False
                rejected_word_count = 0
                if subtitle_mode == "live":
                    segments, rejected_word_count, live_fallback = filter_live_segments_for_track(
                        segments,
                        int(batch.get("track_index") or 0),
                        speaker_turns,
                        batch_fps,
                        timeline_start_frame,
                        speech_regions=item.get("speech_regions") or [],
                        engine=str(os.getenv("SUBFIX_LIVE_ENGINE") or "islands_v2"),
                    )
                    diagnostic["bleed_rejected_count"] = int(diagnostic.get("bleed_rejected_count") or 0) + rejected_word_count
                    if live_fallback:
                        diagnostic["live_fallback_no_word_timing"] = True
                    diagnostic["speech_island_count"] = int(diagnostic.get("speech_island_count") or 0) + sum(
                        1 for segment in segments if segment.get("speech_island_id")
                    )
                rows = generate_subtitle_rows_from_segments(
                    segments,
                    batch_fps,
                    timeline_start_frame,
                    segmentation_profile=segmentation_profile,
                    speech_regions=item.get("speech_regions") or [],
                    subtitle_mode=subtitle_mode,
                )
                for row in rows:
                    row["batch_id"] = batch_id
                    row["track_order"] = int(batch.get("track_order") or 0)
                    row["track_index"] = int(batch.get("track_index") or 0)
                    row["item_index"] = int(batch.get("item_index") or 0)
                batch_backend = str(raw_payload.get("backend") or ("fixture" if fixture_payload is not None else ""))
                batch_model = str(raw_payload.get("model") or args.model)
                backend = backend or batch_backend
                model = model or batch_model
                if raw_payload.get("fallback_errors"):
                    fallback_errors = list(raw_payload.get("fallback_errors") or [])
                batch_output = {
                    "ok": True,
                    "batch_id": batch_id,
                    "parent_batch_id": str(batch.get("parent_batch_id") or batch_id),
                    "batch_part_index": int(batch.get("batch_part_index") or 1),
                    "batch_part_count": int(batch.get("batch_part_count") or 1),
                    "track_order": int(batch.get("track_order") or 0),
                    "track_index": int(batch.get("track_index") or 0),
                    "track_name": str(batch.get("track_name") or ""),
                    "item_index": int(batch.get("item_index") or 0),
                    "audio": str(batch.get("audio") or ""),
                    "source_start": float(batch.get("source_start") or 0.0),
                    "source_end": batch.get("source_end"),
                    "timeline_start_frame": timeline_start_frame,
                    "fps": batch_fps,
                    "backend": batch_backend,
                    "model": batch_model,
                    "segments": segments,
                    "subtitle_rows": rows,
                    "text": str(raw_payload.get("text") or "").strip(),
                    "diagnostic": {
                        "cut_audio_bytes": int(item.get("cut_audio_bytes") or 0),
                        "raw_segment_count": len(raw_payload.get("segments") or []),
                        "segment_count": len(segments),
                        "generated_subtitle_count": len(rows),
                        "generated_subtitles_used_word_timing": any(segment.get("words") for segment in segments),
                        "subtitle_mode": subtitle_mode,
                        "bleed_rejected_count": rejected_word_count,
                        "live_fallback_no_word_timing": live_fallback,
                        "segmentation_profile_used": bool(active_segmentation_profile),
                        "learned_segmentation_row_count": sum(
                            1 for row in rows if row.get("segmentation_decision") == "learned_dp"
                        ),
                        "learned_text_correction_count": sum(
                            int(row.get("text_correction_count") or 0) for row in rows
                        ),
                        "forced_silence_break_row_count": sum(
                            1 for row in rows if row.get("forced_silence_break") is True
                        ),
                        **(item.get("onset_diagnostic") or {}),
                    },
                }
                output_batches.append(batch_output)
                subtitle_rows.extend(rows)
            except Exception as exc:
                output_batches.append(
                    {
                        "ok": False,
                        "batch_id": batch_id,
                        "parent_batch_id": str(batch.get("parent_batch_id") or batch_id),
                        "batch_part_index": int(batch.get("batch_part_index") or 1),
                        "batch_part_count": int(batch.get("batch_part_count") or 1),
                        "track_order": int(batch.get("track_order") or 0),
                        "track_index": int(batch.get("track_index") or 0),
                        "track_name": str(batch.get("track_name") or ""),
                        "item_index": int(batch.get("item_index") or 0),
                        "audio": str(batch.get("audio") or ""),
                        "source_start": float(batch.get("source_start") or 0.0),
                        "source_end": batch.get("source_end"),
                        "error": str(exc),
                        "segments": [],
                        "subtitle_rows": [],
                    }
                )

    failed_batches = [batch for batch in output_batches if batch.get("ok") is not True]
    if failed_batches:
        failed_ids = ", ".join(str(batch.get("batch_id") or "?") for batch in failed_batches)
        raise RuntimeError(f"generate_subtitles_batch 失败: {failed_ids}: {failed_batches[0].get('error')}")

    if subtitle_mode == "live" and str(os.getenv("SUBFIX_LIVE_ENGINE") or "islands_v2") != "legacy":
        island_rows = [row for row in subtitle_rows if row.get("speech_island_id")]
        legacy_rows = [row for row in subtitle_rows if not row.get("speech_island_id")]
        island_rows, reconciliation_diagnostic = reconcile_live_subtitle_rows(
            island_rows,
            float(args.fps or 30.0),
        )
        island_rows, stabilization_diagnostic = stabilize_generated_subtitle_rows(
            island_rows,
            float(args.fps or 30.0),
            subtitle_mode="live",
        )
        subtitle_rows = [*island_rows, *legacy_rows]
        stabilization_diagnostic["legacy_passthrough_row_count"] = len(legacy_rows)
        stabilization_diagnostic["duplicate_suppressed_count"] = (
            int(stabilization_diagnostic.get("duplicate_suppressed_count") or 0)
            + int(reconciliation_diagnostic.get("duplicate_suppressed_count") or 0)
        )
        stabilization_diagnostic["ambiguous_island_count"] = int(
            reconciliation_diagnostic.get("ambiguous_island_count") or 0
        )
        diagnostic.update(stabilization_diagnostic)
    subtitle_rows.sort(key=lambda row: (int(row.get("start_frame") or 0), int(row.get("end_frame") or 0)))
    for index, row in enumerate(subtitle_rows, start=1):
        row["index"] = index
    if fallback_errors:
        diagnostic["fallback_errors"] = fallback_errors
    diagnostic["successful_batch_count"] = len(output_batches)
    diagnostic["failed_batch_count"] = 0
    diagnostic["generated_subtitle_count"] = len(subtitle_rows)
    diagnostic["learned_segmentation_row_count"] = sum(
        1 for row in subtitle_rows if row.get("segmentation_decision") == "learned_dp"
    )
    diagnostic["learned_text_correction_count"] = sum(
        int(row.get("text_correction_count") or 0) for row in subtitle_rows
    )
    diagnostic["forced_silence_break_row_count"] = sum(
        1 for row in subtitle_rows if row.get("forced_silence_break") is True
    )
    diagnostic["confirmed_silence_count"] = int(
        math.ceil(int(diagnostic.get("forced_silence_break_row_count") or 0) / 2.0)
    )
    diagnostic["vad_only_gap_count"] = max(
        0,
        int(diagnostic.get("vad_only_gap_count") or 0) - int(diagnostic.get("confirmed_silence_count") or 0),
    )
    write_progress(
        progress_path,
        "write_output",
        "正在写入批量生成字幕结果",
        batch_index=total_batches,
        total_batches=total_batches,
        progress_index=total_batches * 2 + 1,
        progress_total=progress_total,
    )
    return {
        "ok": True,
        "backend": backend or ("fixture" if fixture_payload is not None else ""),
        "model": model or args.model,
        "segments": [],
        "subtitle_rows": subtitle_rows,
        "batches": output_batches,
        "text": "\n".join(str(batch.get("text") or "") for batch in output_batches if str(batch.get("text") or "")),
        "diagnostic": diagnostic,
    }


def run_generate_subtitles_batch_plan_v4(
    batches: list[dict[str, Any]],
    args: argparse.Namespace,
    progress_path: Path | None,
    fixture_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    requested_engine = str(getattr(args, "generate_engine", "v4") or "v4").lower()
    v5_mode = requested_engine == "v5"
    engine_label = "v5" if v5_mode else "v4"
    # Progress label for the per-window transcription step. It reflects the
    # requested ASR backend (识别模型) so the progress window doesn't always
    # read "Qwen" even when the user picked 豆包. Actual dispatch happens in
    # transcribe_v4_window_batch; forced alignment further downstream is still
    # Qwen is used only when the selected ASR has no reliable timestamps.
    asr_label = "豆包" if str(getattr(args, "backend", "auto") or "auto") == "doubao_asr" else "Qwen"
    if not batches:
        raise RuntimeError(f"缺少 generate_subtitles {engine_label} batch plan")
    if fixture_payload is not None:
        raise RuntimeError(f"{engine_label} fixture 必须提供独立 aligned-unit 测试入口")
    subtitle_mode = str(getattr(args, "subtitle_mode", "narration") or "narration")
    if subtitle_mode not in {"narration", "live"}:
        subtitle_mode = "narration"
    ffmpeg_path = resolve_ffmpeg(args.ffmpeg)
    diagnostic: dict[str, Any] = {
        "mode": "generate_subtitles_batch",
        "requested_mode": getattr(args, "mode", "generate_subtitles_batch"),
        "generate_engine": engine_label,
        "subtitle_mode": subtitle_mode,
        "source_batch_count": len(batches),
        "batch_count": len(batches),
        "successful_batch_count": len(batches),
        "failed_batch_count": 0,
        "forced_align_retry_count": 0,
        "context_align_retry_count": 0,
        "alignment_repaired_unit_count": 0,
        "minimum_raw_alignment_coverage": 1.0,
        "unaligned_rejected_count": 0,
        "alignment_window_alternative_count": 0,
        "alignment_window_uncovered_count": 0,
        "overlap_unit_suppressed_count": 0,
        "window_seam_suppressed_count": 0,
        "cross_mic_boundary_serialized_count": 0,
        "cross_mic_echo_region_count": 0,
        "cross_mic_echo_suppressed_count": 0,
        "cross_mic_echo_ambiguous_count": 0,
        "cross_mic_echo_suppressed_unit_count": 0,
        "cross_mic_duplicate_count": 0,
        "near_duplicate_suppressed_count": 0,
        "short_speaker_flip_suppressed_count": 0,
        "asr_empty_speech_window_count": 0,
        "asr_single_retry_count": 0,
        "asr_subwindow_retry_count": 0,
        "asr_recovered_window_count": 0,
        "asr_unrecovered_window_count": 0,
        "local_retry_elapsed_seconds": 0.0,
        "overlong_tail_reclaimed_count": 0,
        "tail_extended_row_count": 0,
        "textnorm_changed_row_count": 0,
        "asr_backend_used": "qwen3_asr",
        "doubao_fallback_count": 0,
        "doubao_native_timestamp_window_count": 0,
        "doubao_native_timestamp_fallback_count": 0,
    }
    profile: dict[str, Any] | None = None
    active_mode_profile: dict[str, Any] | None = None
    v5_writeback = v5_writeback_mode() if v5_mode else "shadow"
    if v5_mode:
        profile, profile_status = load_segmentation_profile_v5(
            getattr(args, "segmentation_profile", None)
        )
        diagnostic["profile_load_status"] = profile_status
        fallback_reason: str | None = None
        if profile_status != "loaded":
            fallback_reason = f"v5_profile_{profile_status}"
        else:
            try:
                active_mode_profile = generate_v5.require_profile_mode(profile, subtitle_mode)
            except RuntimeError as exc:
                fallback_reason = f"v5_profile_mode_missing: {exc}"
        if fallback_reason is not None:
            v5_mode = False
            engine_label = "v4"
            profile = None
            active_mode_profile = None
            v5_writeback = "shadow"
            diagnostic["generate_engine"] = "v4"
            diagnostic["engine_fallback"] = fallback_reason
    if v5_mode:
        diagnostic["v5_writeback_mode"] = v5_writeback
    output_batches: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="subfix_generate_v4_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        work_items: list[dict[str, Any]] = []
        for batch_index, batch in enumerate(batches, start=1):
            audio_path = Path(str(batch.get("audio") or ""))
            if not audio_path.is_file():
                raise RuntimeError(f"audio file not found: {audio_path}")
            batch_fps = float(batch.get("fps") or args.fps or 30.0)
            cut_path = tmp_path / f"item_{batch_index:04d}.wav"
            write_progress(
                progress_path,
                "prepare_subtitle_batch",
                f"{engine_label} 准备连续音轨 {batch_index}/{len(batches)}",
                batch_index=batch_index,
                total_batches=len(batches),
                progress_index=v4_progress_point(0, 25, batch_index - 1, len(batches)),
                progress_total=100,
            )
            cut_audio(
                ffmpeg_path,
                audio_path,
                cut_path,
                float(batch.get("source_start") or 0.0),
                float(batch["source_end"]) if batch.get("source_end") is not None else None,
                int(batch["audio_channel_index"]) if batch.get("audio_channel_index") else None,
            )
            work_items.append(
                {
                    "cut_path": cut_path,
                    "timeline_start_frame": int(batch.get("timeline_start_frame") or 0),
                    "timeline_end_frame": int(batch["timeline_end_frame"]) if batch.get("timeline_end_frame") is not None else None,
                    "fps": batch_fps,
                    "track_index": int(batch.get("track_index") or 0),
                    "batch_id": str(batch.get("batch_id") or batch_index),
                }
            )

        prepared_tracks, prepare_diagnostic = generate_v4.prepare_track_context_windows(
            work_items,
            tmp_path / "tracks",
        )
        diagnostic.update(prepare_diagnostic)
        windows = [
            {**window, "track_index": int(track["track_index"]), "fps": float(track["fps"])}
            for track in prepared_tracks
            for window in track.get("windows") or []
        ]
        if not windows:
            raise RuntimeError(f"{engine_label} 未生成连续音轨窗口")
        write_progress(
            progress_path,
            "generate_subtitle_batch",
            f"{engine_label} {asr_label} 转写 1/{len(windows)}",
            batch_index=0,
            total_batches=len(windows),
            progress_index=25,
            progress_total=100,
        )
        raw_payloads: list[dict[str, Any]] = []
        batch_size = qwen_generate_batch_size()
        for offset in range(0, len(windows), batch_size):
            chunk = windows[offset:offset + batch_size]
            write_progress(
                progress_path,
                "generate_subtitle_batch",
                f"{engine_label} {asr_label} 转写 {offset + 1}/{len(windows)}",
                batch_index=offset,
                total_batches=len(windows),
                progress_index=v4_progress_point(25, 55, offset, len(windows)),
                progress_total=100,
            )
            chunk_payloads, chunk_asr_diagnostic = transcribe_v4_window_batch(
                [Path(window["audio_path"]) for window in chunk],
                args,
            )
            raw_payloads.extend(chunk_payloads)
            diagnostic["asr_backend_used"] = chunk_asr_diagnostic.get(
                "asr_backend_used", diagnostic["asr_backend_used"]
            )
            diagnostic["doubao_fallback_count"] += int(
                chunk_asr_diagnostic.get("doubao_fallback_count") or 0
            )
            if chunk_asr_diagnostic.get("doubao_fallback_errors"):
                diagnostic.setdefault("doubao_fallback_errors", []).extend(
                    chunk_asr_diagnostic["doubao_fallback_errors"]
                )
            # 汇总豆包每次调用的追踪/计费线索，供账务核对（log_id 可去火山精确定位）。
            for _key in ("doubao_log_ids", "doubao_resource_ids", "doubao_status_codes"):
                if chunk_asr_diagnostic.get(_key):
                    diagnostic.setdefault(_key, []).extend(chunk_asr_diagnostic[_key])
            completed_windows = min(len(windows), offset + len(chunk))
            write_progress(
                progress_path,
                "generate_subtitle_batch",
                f"{engine_label} {asr_label} 转写 {completed_windows}/{len(windows)}",
                batch_index=completed_windows,
                total_batches=len(windows),
                progress_index=v4_progress_point(25, 55, completed_windows, len(windows)),
                progress_total=100,
            )
        if len(raw_payloads) != len(windows):
            raise RuntimeError(f"{engine_label} {asr_label} 窗口数量不匹配: {len(raw_payloads)} != {len(windows)}")
        track_by_index = {int(track["track_index"]): track for track in prepared_tracks}
        recovered_payloads: list[dict[str, Any]] = []
        recovery_diagnostics: list[dict[str, Any]] = []
        for recovery_index, (window, raw_payload) in enumerate(zip(windows, raw_payloads), start=1):
            write_progress(
                progress_path,
                "validate_subtitle_batch",
                f"{engine_label} 转写校验 {recovery_index}/{len(windows)}",
                batch_index=recovery_index,
                total_batches=len(windows),
                progress_index=v4_progress_point(55, 60, recovery_index - 1, len(windows)),
                progress_total=100,
            )
            track = track_by_index[int(window["track_index"])]
            try:
                recovered_payload, recovery_diagnostic = recover_v4_asr_window(
                    window=window,
                    payload=raw_payload,
                    track_audio_path=Path(track["audio_path"]),
                    track_start_frame=int(track["timeline_start_frame"]),
                    retry_dir=tmp_path / "asr_recovery",
                    model=args.model,
                    language=args.language,
                )
            except generate_v4.V4AlignmentError:
                diagnostic["asr_unrecovered_window_count"] += 1
                raise
            recovery = str(recovery_diagnostic.get("asr_recovery") or "not_needed")
            if recovery not in {"not_needed", "accepted_silence"}:
                diagnostic["asr_empty_speech_window_count"] += 1
                diagnostic["asr_recovered_window_count"] += 1
            diagnostic["asr_single_retry_count"] += int(
                recovery_diagnostic.get("asr_single_retry_count") or 0
            )
            diagnostic["asr_subwindow_retry_count"] += int(
                recovery_diagnostic.get("asr_subwindow_retry_count") or 0
            )
            recovered_payloads.append(recovered_payload)
            recovery_diagnostics.append(recovery_diagnostic)
        raw_payloads = recovered_payloads
        retry_regions: list[dict[str, Any]] = []
        if v5_mode and v5_writeback == "live":
            retry_regions, retry_detection = generate_v5.detect_ambiguous_regions(
                windows,
                raw_payloads,
                float(args.fps or 30.0),
            )
            diagnostic.update(retry_detection)
            for retry_index, region in enumerate(retry_regions, start=1):
                region["retry_region_index"] = retry_index
                track_index = int(region.get("track_index") or 0)
                track = track_by_index.get(track_index)
                if track is None:
                    continue
                write_progress(
                    progress_path,
                    "repair_overlap_batch",
                    f"{engine_label} 正在修复相邻窗口差异 {retry_index}/{len(retry_regions)}",
                    batch_index=retry_index - 1,
                    total_batches=len(retry_regions),
                    progress_index=v4_progress_point(60, 65, retry_index - 1, len(retry_regions)),
                    progress_total=100,
                )
                local_retry_started_at = time.monotonic()
                retry_path = tmp_path / "asr_local_retry" / f"track_{track_index:03d}_retry_{retry_index:04d}.wav"
                generate_v4.write_context_window_audio(
                    Path(track["audio_path"]),
                    retry_path,
                    int(track["timeline_start_frame"]),
                    int(region["start_frame"]),
                    int(region["end_frame"]),
                    float(track["fps"]),
                )
                retry_payload = transcribe_qwen3_asr(retry_path, args.model, args.language)
                diagnostic["local_retry_elapsed_seconds"] += time.monotonic() - local_retry_started_at
                write_progress(
                    progress_path,
                    "repair_overlap_batch",
                    f"{engine_label} 正在修复相邻窗口差异 {retry_index}/{len(retry_regions)}",
                    batch_index=retry_index,
                    total_batches=len(retry_regions),
                    progress_index=v4_progress_point(60, 65, retry_index, len(retry_regions)),
                    progress_total=100,
                )
                if not generate_v4.normalize_text(retry_payload.get("text")):
                    diagnostic["low_confidence_region_count"] = int(diagnostic.get("low_confidence_region_count") or 0) + 1
                    continue
                retry_window = {
                    "track_index": track_index,
                    "window_index": 100000 + retry_index,
                    "start_frame": int(region["start_frame"]),
                    "end_frame": int(region["end_frame"]),
                    "left_context_frames": 0,
                    "right_context_frames": 0,
                    "fps": float(track["fps"]),
                    "audio_path": str(retry_path),
                    "candidate_kind": "local_retry",
                    "retry_region_index": retry_index,
                    "retry_reason": str(region.get("reason") or "ambiguous_region"),
                }
                windows.append(retry_window)
                raw_payloads.append(retry_payload)
                recovery_diagnostics.append(
                    {
                        "asr_recovery": "local_retry",
                        "speech_seconds": audio_duration_seconds(retry_path),
                    }
                )
        diagnostic["stages"] = {
            "window_asr": [
                {
                    "track_index": int(window["track_index"]),
                    "window_index": int(window.get("window_index") or 0),
                    "start_frame": int(window["start_frame"]),
                    "end_frame": int(window["end_frame"]),
                    "text": str(raw_payload.get("text") or ""),
                    "asr_recovery": str(recovery_diagnostic.get("asr_recovery") or "not_needed"),
                    "speech_seconds": float(recovery_diagnostic.get("speech_seconds") or 0.0),
                    "candidate_kind": str(window.get("candidate_kind") or "primary"),
                    "retry_reason": str(window.get("retry_reason") or ""),
                }
                for window, raw_payload, recovery_diagnostic in zip(
                    windows,
                    raw_payloads,
                    recovery_diagnostics,
                )
            ],
            "aligned_units": [],
            "source_selection": [],
            "segmentation": [],
        }

        units_by_track: dict[int, list[dict[str, Any]]] = {}
        failed_alignment_windows: list[dict[str, Any]] = []
        successful_alignment_windows: list[dict[str, Any]] = []
        payload_by_window = {
            (int(window["track_index"]), int(window.get("window_index") or 0)): payload
            for window, payload in zip(windows, raw_payloads)
        }
        align_language = qwen3_language_name(args.language) or "Chinese"
        backend = "qwen3_asr"
        model = args.model
        alignment_retry_regions: list[dict[str, Any]] = []
        for align_index, (window, raw_payload) in enumerate(zip(windows, raw_payloads), start=1):
            has_doubao_words = (
                str(raw_payload.get("backend") or "") == "doubao_asr"
                and any(bool(segment.get("words")) for segment in raw_payload.get("segments") or [] if isinstance(segment, dict))
            )
            write_progress(
                progress_path,
                "align_subtitle_batch",
                f"{engine_label} {'豆包时间戳校验' if has_doubao_words else 'Forced Alignment'} {align_index}/{len(windows)}",
                batch_index=align_index,
                total_batches=len(windows),
                progress_index=v4_progress_point(65, 85, align_index - 1, len(windows)),
                progress_total=100,
            )
            track_index = int(window["track_index"])
            track = track_by_index[track_index]
            track_windows = [
                candidate
                for candidate in windows
                if int(candidate.get("track_index") or 0) == track_index
            ]
            local_index = next(
                (index for index, candidate in enumerate(track_windows) if int(candidate.get("window_index") or 0) == int(window.get("window_index") or 0)),
                -1,
            )
            track_payloads = {
                int(candidate.get("window_index") or 0): payload_by_window.get(
                    (track_index, int(candidate.get("window_index") or 0)),
                    {},
                )
                for candidate in track_windows
            }
            adjacent_window = None if window.get("candidate_kind") == "local_retry" else generate_v4.choose_alignment_context_window(
                track_windows,
                local_index,
                track_payloads,
            )
            adjacent_payload = (
                payload_by_window.get((track_index, int(adjacent_window.get("window_index") or 0)))
                if adjacent_window is not None
                else None
            )
            merged_audio_path = tmp_path / (
                f"align_context_track_{track_index:03d}_window_{int(window.get('window_index') or 0):04d}.wav"
            )
            try:
                aligned_units, align_diagnostic = generate_v4.require_aligned_window_with_context(
                    track_audio_path=Path(track["audio_path"]),
                    track_start_frame=int(track["timeline_start_frame"]),
                    window=window,
                    payload=raw_payload,
                    adjacent_window=adjacent_window,
                    adjacent_payload=adjacent_payload,
                    merged_audio_path=merged_audio_path,
                    align_fn=qwen3_force_align_items,
                    language=align_language,
                    fps=float(window["fps"]),
                )
            except generate_v4.V4AlignmentError as exc:
                diagnostic["unaligned_rejected_count"] += 1
                if v5_mode and not generate_v5.alignment_failure_is_fatal(window):
                    diagnostic["local_retry_alignment_failed_count"] = int(
                        diagnostic.get("local_retry_alignment_failed_count") or 0
                    ) + 1
                    diagnostic["low_confidence_region_count"] = int(
                        diagnostic.get("low_confidence_region_count") or 0
                    ) + 1
                    continue
                failed_alignment_windows.append(
                    {
                        "track_index": track_index,
                        "start_frame": int(window["start_frame"]),
                        "end_frame": int(window["end_frame"]),
                        "error": str(exc),
                    }
                )
                continue
            diagnostic["forced_align_retry_count"] += int(align_diagnostic.get("forced_align_retry_count") or 0)
            native_timestamp_fallback_count = int(align_diagnostic.get("native_timestamp_fallback_count") or 0)
            diagnostic["doubao_native_timestamp_fallback_count"] += native_timestamp_fallback_count
            if has_doubao_words and native_timestamp_fallback_count == 0:
                diagnostic["doubao_native_timestamp_window_count"] += 1
            diagnostic["alignment_repaired_unit_count"] += int(
                align_diagnostic.get("alignment_repaired_unit_count") or 0
            )
            diagnostic["minimum_raw_alignment_coverage"] = min(
                float(diagnostic.get("minimum_raw_alignment_coverage") or 1.0),
                float(align_diagnostic.get("raw_alignment_coverage") or 1.0),
            )
            context_retry = int(align_diagnostic.get("context_align_retry_count") or 0)
            diagnostic["context_align_retry_count"] += context_retry
            score_audio_path = merged_audio_path if context_retry else Path(window["audio_path"])
            score_start_frame = (
                min(int(window["start_frame"]), int(adjacent_window["start_frame"]))
                if context_retry and adjacent_window is not None
                else int(window["start_frame"])
            )
            aligned_units = generate_v4.score_aligned_units_from_audio(
                score_audio_path,
                aligned_units,
                score_start_frame,
                float(window["fps"]),
                float(align_diagnostic.get("alignment_coverage") or 0.0),
            )
            speech_regions, _speech_onsets, _vad_diagnostic = detect_speech_regions(score_audio_path)
            aligned_units = generate_v4.annotate_independent_vad(
                aligned_units,
                speech_regions,
                score_start_frame,
                float(window["fps"]),
            )
            raw_coverage = float(align_diagnostic.get("raw_alignment_coverage") or 0.0)
            repaired_count = int(align_diagnostic.get("alignment_repaired_unit_count") or 0)
            for unit in aligned_units:
                unit["track_index"] = track_index
                unit["window_index"] = int(window.get("window_index") or 0)
                unit["candidate_kind"] = str(window.get("candidate_kind") or "primary")
                unit["retry_region_index"] = int(window.get("retry_region_index") or 0)
                unit["retry_reason"] = str(window.get("retry_reason") or "")
                unit["raw_alignment_coverage"] = raw_coverage
                unit["alignment_repaired_unit_count"] = repaired_count
                unit["unit_count"] = len(aligned_units)
            if (
                v5_mode
                and window.get("candidate_kind") != "local_retry"
                and generate_v5.alignment_needs_retry(
                    {
                        "raw_alignment_coverage": raw_coverage,
                        "alignment_repaired_unit_count": repaired_count,
                        "unit_count": len(aligned_units),
                    }
                )
            ):
                repaired_units = [unit for unit in aligned_units if unit.get("alignment_repaired")]
                if repaired_units:
                    retry_start = min(int(unit.get("start_frame") or window["start_frame"]) for unit in repaired_units)
                    retry_end = max(int(unit.get("end_frame") or retry_start + 1) for unit in repaired_units)
                else:
                    center = (int(window["start_frame"]) + int(window["end_frame"])) // 2
                    retry_start = center - int(round(8.0 * float(window["fps"])))
                    retry_end = center + int(round(8.0 * float(window["fps"])))
                padding = int(round(2.5 * float(window["fps"])))
                retry_start = max(int(window["start_frame"]), retry_start - padding)
                retry_end = min(int(window["end_frame"]), retry_end + padding)
                maximum = int(round(16.0 * float(window["fps"])))
                if retry_end - retry_start > maximum:
                    center = (retry_start + retry_end) // 2
                    retry_start = max(int(window["start_frame"]), center - maximum // 2)
                    retry_end = retry_start + maximum
                alignment_retry_regions.append(
                    {
                        "track_index": track_index,
                        "start_frame": retry_start,
                        "end_frame": retry_end,
                        "reason": "alignment_quality",
                    }
                )
            units_by_track.setdefault(track_index, []).extend(aligned_units)
            successful_alignment_windows.append(
                {
                    "track_index": track_index,
                    "start_frame": int(window["start_frame"]),
                    "end_frame": int(window["end_frame"]),
                }
            )
            backend = str(raw_payload.get("backend") or backend)
            model = str(raw_payload.get("model") or model)

        if v5_mode and v5_writeback == "live" and alignment_retry_regions:
            merged_alignment_regions: list[dict[str, Any]] = []
            for region in sorted(
                alignment_retry_regions,
                key=lambda item: (int(item["track_index"]), int(item["start_frame"])),
            ):
                overlaps_existing = any(
                    int(existing.get("track_index") or 0) == int(region["track_index"])
                    and min(int(existing["end_frame"]), int(region["end_frame"]))
                    > max(int(existing["start_frame"]), int(region["start_frame"]))
                    for existing in retry_regions
                )
                if overlaps_existing:
                    continue
                if (
                    merged_alignment_regions
                    and int(merged_alignment_regions[-1]["track_index"]) == int(region["track_index"])
                    and int(region["start_frame"]) <= int(merged_alignment_regions[-1]["end_frame"])
                ):
                    merged_alignment_regions[-1]["end_frame"] = max(
                        int(merged_alignment_regions[-1]["end_frame"]),
                        int(region["end_frame"]),
                    )
                else:
                    merged_alignment_regions.append(dict(region))

            retry_offset = len(retry_regions)
            for local_index, region in enumerate(merged_alignment_regions, start=1):
                retry_index = retry_offset + local_index
                track_index = int(region["track_index"])
                track = track_by_index[track_index]
                retry_path = tmp_path / "alignment_local_retry" / f"track_{track_index:03d}_retry_{retry_index:04d}.wav"
                generate_v4.write_context_window_audio(
                    Path(track["audio_path"]),
                    retry_path,
                    int(track["timeline_start_frame"]),
                    int(region["start_frame"]),
                    int(region["end_frame"]),
                    float(track["fps"]),
                )
                try:
                    retry_payload = transcribe_qwen3_asr(retry_path, args.model, args.language)
                    retry_units, retry_align_diagnostic = generate_v4.require_aligned_units(
                        retry_path,
                        retry_payload,
                        qwen3_force_align_items,
                        align_language,
                        float(track["fps"]),
                        int(region["start_frame"]),
                    )
                except (RuntimeError, generate_v4.V4AlignmentError):
                    diagnostic["low_confidence_region_count"] = int(diagnostic.get("low_confidence_region_count") or 0) + 1
                    continue
                retry_units = generate_v4.score_aligned_units_from_audio(
                    retry_path,
                    retry_units,
                    int(region["start_frame"]),
                    float(track["fps"]),
                    float(retry_align_diagnostic.get("alignment_coverage") or 0.0),
                )
                speech_regions, _speech_onsets, _vad_diagnostic = detect_speech_regions(retry_path)
                retry_units = generate_v4.annotate_independent_vad(
                    retry_units,
                    speech_regions,
                    int(region["start_frame"]),
                    float(track["fps"]),
                )
                for unit in retry_units:
                    unit["track_index"] = track_index
                    unit["window_index"] = 200000 + retry_index
                    unit["candidate_kind"] = "local_retry"
                    unit["retry_region_index"] = retry_index
                    unit["retry_reason"] = "alignment_quality"
                    unit["raw_alignment_coverage"] = float(retry_align_diagnostic.get("raw_alignment_coverage") or 0.0)
                    unit["alignment_repaired_unit_count"] = int(retry_align_diagnostic.get("alignment_repaired_unit_count") or 0)
                    unit["unit_count"] = len(retry_units)
                units_by_track.setdefault(track_index, []).extend(retry_units)
                retry_regions.append(
                    {
                        **region,
                        "retry_region_index": retry_index,
                        "reason": "alignment_quality",
                    }
                )
            diagnostic["local_retry_region_count"] = len(retry_regions)

        uncovered_alignment_windows = generate_v4.alignment_failures_without_alternative(
            failed_alignment_windows,
            successful_alignment_windows,
        )
        diagnostic["alignment_window_alternative_count"] = (
            len(failed_alignment_windows) - len(uncovered_alignment_windows)
        )
        diagnostic["alignment_window_uncovered_count"] = len(uncovered_alignment_windows)
        if uncovered_alignment_windows:
            first_failure = uncovered_alignment_windows[0]
            raise generate_v4.V4AlignmentError(
                f"{engine_label} Forced Alignment 无替代麦覆盖: "
                f"轨道={first_failure['track_index']}, "
                f"范围={first_failure['start_frame']}-{first_failure['end_frame']}; "
                f"{first_failure.get('error') or '未知错误'}"
            )
        if not units_by_track:
            raise generate_v4.V4AlignmentError(f"{engine_label} 没有可用的 Forced Alignment 结果")

        write_progress(
            progress_path,
            "select_subtitle_sources",
            f"{engine_label} 正在进行文本候选仲裁",
            progress_index=85,
            progress_total=100,
        )

        candidates: list[dict[str, Any]] = []
        for track_index in sorted(units_by_track):
            track_windows = list((track_by_index.get(track_index) or {}).get("windows") or [])
            primary_units = [
                unit
                for unit in units_by_track[track_index]
                if str(unit.get("candidate_kind") or "primary") != "local_retry"
            ]
            stitched, seam_diagnostic = generate_v4.stitch_track_window_units(
                primary_units,
                track_windows,
            )
            diagnostic["window_seam_suppressed_count"] += int(
                seam_diagnostic.get("window_seam_suppressed_count") or 0
            )
            deduped, overlap_diagnostic = generate_v4.dedupe_overlap_units(stitched)
            diagnostic["overlap_unit_suppressed_count"] += int(
                overlap_diagnostic.get("overlap_unit_suppressed_count") or 0
            )
            if v5_mode:
                for region in (
                    item
                    for item in retry_regions
                    if int(item.get("track_index") or 0) == track_index
                ):
                    retry_region_index = int(region.get("retry_region_index") or 0)
                    retry_units = [
                        unit
                        for unit in units_by_track[track_index]
                        if str(unit.get("candidate_kind") or "") == "local_retry"
                        and int(unit.get("retry_region_index") or 0) == retry_region_index
                    ]
                    if not retry_units:
                        continue
                    if v5_writeback == "live":
                        deduped, selection_diagnostic = generate_v5.select_retry_region_units(
                            deduped,
                            retry_units,
                            region,
                        )
                        selection_diagnostic["local_retry_writeback_mode"] = "live"
                        selection_diagnostic.setdefault(
                            "local_retry_proposed",
                            bool(selection_diagnostic.get("local_retry_selected")),
                        )
                    else:
                        deduped, selection_diagnostic = generate_v5.arbitrate_retry_region_units(
                            deduped,
                            retry_units,
                            region,
                        )
                    diagnostic["local_retry_writeback_mode"] = str(
                        selection_diagnostic.get("local_retry_writeback_mode") or "shadow"
                    )
                    if selection_diagnostic.get("local_retry_proposed"):
                        diagnostic["local_retry_proposed_count"] = int(
                            diagnostic.get("local_retry_proposed_count") or 0
                        ) + 1
                    if selection_diagnostic.get("local_retry_selected"):
                        diagnostic["local_retry_selected_count"] = int(
                            diagnostic.get("local_retry_selected_count") or 0
                        ) + 1
                    if selection_diagnostic.get("low_confidence_region"):
                        diagnostic["low_confidence_region_count"] = int(
                            diagnostic.get("low_confidence_region_count") or 0
                        ) + 1
            candidates.extend(deduped)
        diagnostic["stages"]["aligned_units"] = [
            {
                "track_index": int(unit.get("track_index") or 0),
                "window_index": int(unit.get("window_index") or 0),
                "start_frame": int(unit.get("start_frame") or 0),
                "end_frame": int(unit.get("end_frame") or 0),
                "text": str(unit.get("text") or ""),
                "source_score": float(unit.get("source_score") or 0.0),
                "speaker_score_db": float(unit.get("speaker_score_db") or 0.0),
                "asr_punctuation_strength": float(unit.get("asr_punctuation_strength") or 0.0),
                "independent_vad": bool(unit.get("independent_vad")),
                "alignment_repaired": bool(unit.get("alignment_repaired")),
                "candidate_kind": str(unit.get("candidate_kind") or "primary"),
                "retry_region_index": int(unit.get("retry_region_index") or 0),
                "raw_alignment_coverage": float(unit.get("raw_alignment_coverage") or 0.0),
            }
            for unit in candidates
        ]
        echo_filtered_candidates, echo_diagnostic = generate_v4.suppress_cross_mic_echo_regions(
            candidates,
            float(args.fps or 30.0),
        )
        diagnostic.update(echo_diagnostic)
        canonical_units, exclusive_diagnostic = generate_v4.build_exclusive_unit_stream(
            echo_filtered_candidates,
            float(args.fps or 30.0),
        )
        diagnostic.update(exclusive_diagnostic)
        canonical_units, near_duplicate_diagnostic = generate_v4.suppress_near_duplicate_units(
            canonical_units,
            float(args.fps or 30.0),
        )
        diagnostic.update(near_duplicate_diagnostic)
        write_progress(
            progress_path,
            "segment_subtitles",
            f"{engine_label} 正在进行字幕断句",
            progress_index=90,
            progress_total=100,
        )
        if not v5_mode:
            profile = load_segmentation_profile_v4(getattr(args, "segmentation_profile", None))
        diagnostic["stages"]["source_selection"] = [
            {
                "track_index": int(unit.get("track_index") or 0),
                "start_frame": int(unit.get("start_frame") or 0),
                "end_frame": int(unit.get("end_frame") or 0),
                "text": str(unit.get("text") or ""),
                "source_score": float(unit.get("source_score") or 0.0),
                "speaker_score_db": float(unit.get("speaker_score_db") or 0.0),
                "speaker_decision": str(unit.get("speaker_decision") or "viterbi_exclusive"),
                "asr_punctuation_strength": float(unit.get("asr_punctuation_strength") or 0.0),
                "independent_vad": bool(unit.get("independent_vad")),
            }
            for unit in canonical_units
        ]
        subtitle_rows, segmentation_diagnostic = generate_v4.segment_canonical_units(
            canonical_units,
            subtitle_mode,
            profile,
            float(args.fps or 30.0),
            max_chars=getattr(args, "max_chars", None),
        )
        subtitle_rows, overlong_tail_reclaimed_count = generate_v4.reclaim_overlong_unit_tails(
            subtitle_rows,
            canonical_units,
            float(args.fps or 30.0),
        )
        diagnostic["overlong_tail_reclaimed_count"] = overlong_tail_reclaimed_count
        subtitle_rows, word_boundary_protected_count = generate_v4.protect_word_boundaries(
            subtitle_rows,
            canonical_units,
            float(args.fps or 30.0),
        )
        diagnostic["word_boundary_protected_count"] = word_boundary_protected_count
        subtitle_rows, hard_char_split_count = generate_v4.enforce_hard_char_limit(
            subtitle_rows,
            canonical_units,
            getattr(args, "max_chars", None),
        )
        diagnostic["hard_char_split_count"] = hard_char_split_count
        tail_extension_max_gap_frames = int(
            round(generate_v4.SUBTITLE_ROW_TAIL_EXTENSION_GAP_SECONDS * float(args.fps or 30.0))
        )
        subtitle_rows, tail_extended_row_count = generate_v4.extend_subtitle_row_tails(
            subtitle_rows,
            float(args.fps or 30.0),
            tail_extension_max_gap_frames,
        )
        diagnostic["tail_extended_row_count"] = tail_extended_row_count
        subtitle_rows, textnorm_diagnostic = generate_textnorm.normalize_subtitle_rows(subtitle_rows)
        diagnostic["textnorm_changed_row_count"] = textnorm_diagnostic["textnorm_changed_row_count"]
        if v5_mode and v5_writeback == "live" and subtitle_rows:
            refined_rows: list[dict[str, Any]] = []
            refinement_diagnostic = {
                "audio_refinement_mode": v5_writeback,
                "audio_refined_row_count": 0,
                "energy_valley_boundary_count": 0,
                "confirmed_silence_preserved_count": 0,
            }
            for track in prepared_tracks:
                track_index = int(track["track_index"])
                track_rows = [
                    row
                    for row in subtitle_rows
                    if int(row.get("speaker_track_index") or 0) == track_index
                ]
                if not track_rows:
                    continue
                refine_boundaries = (
                    generate_v5.refine_subtitle_boundaries
                    if v5_writeback == "live"
                    else generate_v5.shadow_refine_subtitle_boundaries
                )
                refined_track_rows, track_refinement = refine_boundaries(
                    track_rows,
                    Path(track["audio_path"]),
                    int(track["timeline_start_frame"]),
                    float(track["fps"]),
                )
                refined_rows.extend(refined_track_rows)
                for key, value in track_refinement.items():
                    if isinstance(value, (int, float)):
                        refinement_diagnostic[key] = int(refinement_diagnostic.get(key) or 0) + int(value)
                    else:
                        refinement_diagnostic[key] = value
            subtitle_rows = sorted(
                refined_rows,
                key=lambda row: (int(row.get("start_frame") or 0), int(row.get("end_frame") or 0)),
            )
            diagnostic.update(refinement_diagnostic)
        for index, row in enumerate(subtitle_rows, start=1):
            row["index"] = index
        diagnostic.update(segmentation_diagnostic)
        mode_profile = active_mode_profile if v5_mode else (((profile or {}).get("modes") or {}).get(subtitle_mode) if profile else None)
        boundary_model = (mode_profile or {}).get("boundary_model") if isinstance(mode_profile, dict) else None
        canonical_text = "".join(generate_v4.normalize_text(unit.get("text")) for unit in canonical_units)
        text_cursor = 0
        segmentation_stage: list[dict[str, Any]] = []
        for row in subtitle_rows:
            text_cursor += len(generate_v4.normalize_text(row.get("text")))
            probability = (
                generate_v4.boundary_probability(canonical_text, text_cursor, boundary_model)
                if boundary_model and 0 < text_cursor < len(canonical_text)
                else 1.0
            )
            segmentation_stage.append(
                {
                    "start_frame": int(row.get("start_frame") or 0),
                    "end_frame": int(row.get("end_frame") or 0),
                    "text": str(row.get("text") or ""),
                    "track_index": int(row.get("speaker_track_index") or 0),
                    "boundary_probability": round(float(probability), 6),
                }
            )
        diagnostic["stages"]["segmentation"] = segmentation_stage
        diagnostic["canonical_unit_count"] = len(canonical_units)
        diagnostic["generated_subtitle_count"] = len(subtitle_rows)
        diagnostic["forced_aligned_unit_coverage"] = 1.0 if canonical_units else 0.0
        diagnostic["segmentation_profile_schema"] = str((profile or {}).get("schema_version") or "")
        for track in prepared_tracks:
            track_index = int(track["track_index"])
            output_batches.append(
                {
                    "ok": True,
                    "batch_id": f"{engine_label}_track_{track_index}",
                    "track_index": track_index,
                    "timeline_start_frame": int(track["timeline_start_frame"]),
                    "fps": float(track["fps"]),
                    "backend": backend,
                    "model": model,
                    "segments": [],
                    "subtitle_rows": [
                        row for row in subtitle_rows if int(row.get("speaker_track_index") or 0) == track_index
                    ],
                    "diagnostic": {
                        "item_count": int(track["item_count"]),
                        "window_count": len(track.get("windows") or []),
                    },
                }
            )
    write_progress(
        progress_path,
        "write_output",
        f"{engine_label} 连续词时间线生成完成",
        batch_index=len(batches),
        total_batches=len(batches),
        progress_index=95,
        progress_total=100,
    )
    return {
        "ok": True,
        "backend": backend,
        "model": model,
        "segments": [],
        "subtitle_rows": subtitle_rows,
        "batches": output_batches,
        "text": "".join(str(row.get("text") or "") for row in subtitle_rows),
        "diagnostic": diagnostic,
    }


def run_generate_subtitles_batch_plan_v5(
    batches: list[dict[str, Any]],
    args: argparse.Namespace,
    progress_path: Path | None,
    fixture_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run v5 through the shared continuous-timeline pipeline with v5 gates enabled."""
    args.generate_engine = "v5"
    return run_generate_subtitles_batch_plan_v4(
        batches,
        args,
        progress_path,
        fixture_payload=fixture_payload,
    )


def run_generate_subtitles_batch_plan(
    batches: list[dict[str, Any]],
    args: argparse.Namespace,
    progress_path: Path | None,
    fixture_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    engine = str(getattr(args, "generate_engine", "v3") or "v3").lower()
    runners = {
        "v3": run_generate_subtitles_batch_plan_v3,
        "v4": run_generate_subtitles_batch_plan_v4,
        "v5": run_generate_subtitles_batch_plan_v5,
    }
    runner = runners.get(engine)
    if runner is None:
        raise RuntimeError(f"未知生成引擎: {engine}")
    return runner(batches, args, progress_path, fixture_payload=fixture_payload)


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


def qwen3_support_roots() -> list[Path]:
    module_dir = Path(__file__).resolve().parent
    roots = [
        module_dir / ".subfix_support",
        module_dir,
        Path.cwd() / ".subfix_support",
    ]
    unique_roots: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        root_key = str(root)
        if root_key not in seen:
            seen.add(root_key)
            unique_roots.append(root)
    return unique_roots


def discover_qwen3_cpp_bin() -> Path | None:
    for root in qwen3_support_roots():
        for candidate in (
            root / "qwen3-asr.cpp" / "build" / "qwen3-asr-cli",
            root / "bin" / "qwen3-asr-cli",
        ):
            if candidate.exists() and os.access(candidate, os.X_OK):
                return candidate
    resolved_bin = shutil.which("qwen3-asr-cli")
    return Path(resolved_bin) if resolved_bin else None


def discover_qwen3_aligner_model() -> Path | None:
    for root in qwen3_support_roots():
        model_dir = root / "models"
        for model_name in QWEN3_FORCED_ALIGNER_MODEL_NAMES:
            candidate = model_dir / model_name
            if candidate.exists():
                return candidate
        for candidate in sorted(model_dir.glob("qwen3-forced-aligner-0.6b-*.gguf")):
            if candidate.exists():
                return candidate
    return None


def resolve_qwen3_cpp_paths() -> tuple[Path, Path]:
    bin_value = str(os.getenv(QWEN3_CPP_BIN_ENV) or "").strip()
    if bin_value:
        bin_path = Path(bin_value).expanduser()
        if not bin_path.exists():
            resolved_bin = shutil.which(bin_value)
            if resolved_bin:
                bin_path = Path(resolved_bin)
            else:
                raise RuntimeError(f"{QWEN3_CPP_BIN_ENV} 指向的 qwen3-asr-cli 不存在: {bin_value}")
    else:
        discovered_bin = discover_qwen3_cpp_bin()
        if not discovered_bin:
            raise RuntimeError(f"{QWEN3_CPP_BIN_ENV} 未配置，且未在 .subfix_support 中找到 qwen3-asr-cli")
        bin_path = discovered_bin
    if not os.access(bin_path, os.X_OK):
        raise RuntimeError(f"{QWEN3_CPP_BIN_ENV} 指向的 qwen3-asr-cli 不可执行: {bin_path}")

    model_value = str(os.getenv(QWEN3_CPP_ALIGNER_GGUF_ENV) or "").strip()
    if model_value:
        model_path = Path(model_value).expanduser()
        if not model_path.exists():
            raise RuntimeError(f"{QWEN3_CPP_ALIGNER_GGUF_ENV} 指向的模型不存在: {model_value}")
    else:
        discovered_model = discover_qwen3_aligner_model()
        if not discovered_model:
            raise RuntimeError(f"{QWEN3_CPP_ALIGNER_GGUF_ENV} 未配置，且未在 .subfix_support/models 中找到 Qwen3 forced aligner GGUF")
        model_path = discovered_model
    return bin_path, model_path


def qwen3_cpp_forced_align_text_rows(
    audio_path: Path,
    rows: list[dict[str, Any]],
    language: str | None,
    fps: float,
    timeline_start_frame: int,
    work_dir: Path,
) -> dict[str, Any]:
    alignment_text = build_alignment_text(rows)
    if not alignment_text:
        raise RuntimeError("Qwen3 forced align 文本为空")

    bin_path, model_path = resolve_qwen3_cpp_paths()
    work_dir.mkdir(parents=True, exist_ok=True)
    output_path = work_dir / f"qwen_forced_align_{int(time.time() * 1000)}_{os.getpid()}.json"
    cmd = [
        str(bin_path),
        "-m",
        str(model_path),
        "-f",
        str(audio_path),
        "--align",
        "--text",
        alignment_text,
        "--language",
        qwen3_language_name(language) or "Chinese",
        "-o",
        str(output_path),
    ]
    result = subprocess.run(cmd, text=True, capture_output=True)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"Qwen3 forced align 执行失败: {detail or result.returncode}")
    if not output_path.exists():
        raise RuntimeError(f"Qwen3 forced align 未生成输出 JSON: {output_path}")

    try:
        qwen_payload = json.loads(output_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Qwen3 forced align 输出 JSON 非法: {exc}") from exc

    timestamp_items = qwen3_timestamp_words(qwen_payload)
    if not timestamp_items:
        raise RuntimeError("Qwen3 forced align 输出没有词/字时间戳")
    latest_timestamp = max(
        max(float(item.get("start") or 0.0), float(item.get("end") or 0.0))
        for item in timestamp_items
    )
    if latest_timestamp <= 0.001:
        raise RuntimeError(
            "Qwen3 forced align 时间戳无效：所有词/字均停留在 0 秒"
        )
    row_segments = qwen3_remap_timestamp_items_to_rows(
        rows,
        timestamp_items,
        fps=fps,
        timeline_start_frame=timeline_start_frame,
    )
    aligned_rows = apply_alignment_to_rows(
        rows,
        row_segments,
        fps,
        timeline_start_frame,
        allow_non_monotonic=True,
    )
    for aligned_row, segment in zip(aligned_rows, row_segments, strict=True):
        aligned_row["stable_ts_start_frame"] = aligned_row["start_frame"]
        aligned_row["ctc_start_frame"] = int(segment["ctc_start_frame"])
        aligned_row["ctc_end_frame"] = int(segment["ctc_end_frame"])
        aligned_row["ctc_confidence"] = segment.get("ctc_confidence")
        aligned_row["ctc_char_count"] = segment.get("ctc_char_count")
        aligned_row["row_remap_score"] = segment.get("row_remap_score")
        aligned_row["row_remap_decision"] = segment.get("row_remap_decision")
        aligned_row["remap_text_candidate"] = segment.get("remap_text_candidate")
        aligned_row["row_remap_target"] = segment.get("row_remap_target")
        aligned_row["alignment_mode"] = "qwen3_forced_aligner"

    return {
        "backend": "qwen3_cpp_forced_aligner",
        "model": str(model_path),
        "segments": row_segments,
        "aligned_rows": aligned_rows,
        "text": alignment_text,
        "diagnostic": {
            "align_engine": "qwen3_cpp",
            "qwen_item_count": len(timestamp_items),
            "qwen_output_path": str(output_path),
            "qwen_cli": str(bin_path),
            "qwen_model": str(model_path),
        },
    }


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


def run_qwen_forced_align_batch_plan(
    batches: list[dict[str, Any]],
    args: argparse.Namespace,
    progress_path: Path | None,
) -> dict[str, Any]:
    if not batches:
        raise RuntimeError("缺少 Qwen3 forced align batch plan")

    total_batches = len(batches)
    output_batches: list[dict[str, Any]] = []
    diagnostic: dict[str, Any] = {
        "mode": "fixture" if args.fixture_json else "qwen_forced_align_text_batches",
        "requested_mode": args.mode,
        "align_engine": "qwen3_cpp",
        "batch_count": total_batches,
        "requested_ffmpeg": args.ffmpeg,
        "qwen_item_count": 0,
        "qwen_output_path": "",
    }

    fixture_payload = json.loads(Path(args.fixture_json).read_text(encoding="utf-8")) if args.fixture_json else None
    ffmpeg_path = resolve_ffmpeg(args.ffmpeg) if not fixture_payload else None
    if not fixture_payload:
        diagnostic["ffmpeg"] = ffmpeg_path

    qwen_output_paths: list[str] = []
    qwen_output_dir = Path(args.output).parent / "subfix_qwen_align_outputs"
    with tempfile.TemporaryDirectory(prefix="subfix_qwen_align_batches_") as tmp_dir:
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
                    align_engine="qwen3_cpp",
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
                        "qwen_forced_align",
                        "正在执行 Qwen3 forced alignment",
                        batch_index=batch_index,
                        total_batches=total_batches,
                        align_engine="qwen3_cpp",
                    )
                    speech_regions, speech_onsets, onset_diagnostic = detect_speech_regions(cut_path)
                    raw_payload = qwen3_cpp_forced_align_text_rows(
                        cut_path,
                        rows,
                        args.language or "zh",
                        batch_fps,
                        timeline_start_frame,
                        qwen_output_dir,
                    )
                    raw_payload = dict(raw_payload)
                    raw_payload.setdefault("diagnostic", {}).update(onset_diagnostic)

                aligned_payload = {
                    "aligned_rows": raw_payload.get("aligned_rows") or [],
                    "segments": raw_payload.get("segments") or [],
                    "text": str(raw_payload.get("text") or "").strip(),
                }
                batch_diagnostic = dict(raw_payload.get("diagnostic") or {})
                batch_diagnostic["row_count"] = len(rows)
                batch_diagnostic["aligned_count"] = len(aligned_payload["aligned_rows"])
                diagnostic["qwen_item_count"] += int(batch_diagnostic.get("qwen_item_count") or 0)
                if batch_diagnostic.get("qwen_output_path"):
                    qwen_output_paths.append(str(batch_diagnostic["qwen_output_path"]))
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
                        "diagnostic": {"row_count": len(rows), "align_engine": "qwen3_cpp"},
                    }
                )

    diagnostic["qwen_output_path"] = ",".join(qwen_output_paths)
    diagnostic["successful_batch_count"] = sum(1 for batch in output_batches if batch.get("ok") is True)
    diagnostic["failed_batch_count"] = total_batches - diagnostic["successful_batch_count"]
    failed_batch_errors = [
        f"第 {batch.get('batch_id')} 批：{batch.get('error')}"
        for batch in output_batches
        if batch.get("ok") is False and str(batch.get("error") or "").strip()
    ]
    if failed_batch_errors:
        diagnostic["failed_batch_errors"] = failed_batch_errors[:3]
    write_progress(
        progress_path,
        "done",
        "批量 Qwen3 forced alignment helper 已完成",
        batch_index=total_batches,
        total_batches=total_batches,
        align_engine="qwen3_cpp",
    )
    payload = {
        "ok": diagnostic["failed_batch_count"] == 0,
        "model": os.getenv(QWEN3_CPP_ALIGNER_GGUF_ENV) or args.model,
        "batches": output_batches,
        "diagnostic": diagnostic,
    }
    if failed_batch_errors:
        payload["error"] = "；".join(failed_batch_errors[:3])
    return payload


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
    parser.add_argument("--subtitle-mode", choices=("narration", "live"), default="narration")
    parser.add_argument(
        "--generate-engine",
        choices=("v3", "v4", "v5"),
        default=str(os.getenv("SUBFIX_GENERATE_ENGINE") or "v5").lower(),
    )
    parser.add_argument("--segmentation-profile")
    parser.add_argument("--calibration-json", action="append", default=[])
    parser.add_argument(
        "--backend",
        choices=("auto", "mimo_asr", "qwen3_asr", "mlx_whisper", "openai_whisper", "doubao_asr"),
        default="auto",
    )
    parser.add_argument(
        "--mode",
        choices=(
            "qwen_forced_align_text_batches",
            "transcribe",
            "generate_subtitles",
            "generate_subtitles_batch",
            "learn_segmentation_profile",
            "onsets",
        ),
        default="transcribe",
    )
    parser.add_argument("--fixture-json")
    parser.add_argument("--progress-json")
    parser.add_argument("--diagnostic-output")
    parser.add_argument(
        "--max-chars",
        type=int,
        default=None,
        help=(
            "字幕长度（每条字幕最大字数）注入点，仅作用于 v4/v5 主断句路径"
            " (segment_canonical_units)。不传时保持现有 length_model/profile 行为不变。"
        ),
    )
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
        if args.mode == "learn_segmentation_profile":
            if not args.calibration_json:
                raise RuntimeError("缺少 --calibration-json")
            calibration_payloads: list[dict[str, Any]] = []
            for calibration_path_value in args.calibration_json:
                calibration_path = Path(calibration_path_value).expanduser()
                calibration_payload = json.loads(calibration_path.read_text(encoding="utf-8"))
                calibration_payload["source_path"] = str(calibration_path)
                calibration_payloads.append(calibration_payload)
            profile_builders = {
                "v3": build_segmentation_profile_from_payloads,
                "v4": generate_v4.build_segmentation_profile_v3,
                "v5": generate_v5.build_segmentation_profile_v4,
            }
            profile = profile_builders[args.generate_engine](calibration_payloads)
            write_payload(output_path, profile)
            return 0 if profile.get("diagnostic", {}).get("training_region_count") else 1
        if args.mode == "qwen_forced_align_text_batches":
            payload = run_qwen_forced_align_batch_plan(load_batch_plan(args.batch_plan_json), args, progress_path)
            write_payload(output_path, payload)
            return 0 if payload.get("ok") else 1
        if args.mode == "generate_subtitles_batch":
            fixture_payload = json.loads(Path(args.fixture_json).read_text(encoding="utf-8")) if args.fixture_json else None
            payload = run_generate_subtitles_batch_plan(
                load_generate_subtitles_batch_plan(args.batch_plan_json),
                args,
                progress_path,
                fixture_payload=fixture_payload,
            )
            if args.srt_output:
                srt_path = Path(args.srt_output)
                srt_base_frame = args.srt_base_frame if args.srt_base_frame is not None else args.timeline_start_frame
                payload.setdefault("diagnostic", {})["srt_output"] = str(srt_path)
                payload["diagnostic"]["srt_row_count"] = write_subtitle_rows_to_srt(
                    srt_path,
                    payload.get("subtitle_rows") or [],
                    args.fps,
                    srt_base_frame,
                )
            write_payload(output_path, payload)
            if args.diagnostic_output:
                write_payload(
                    Path(args.diagnostic_output).expanduser(),
                    sanitize_generate_diagnostic_payload(payload),
                )
            completion_index, completion_total = generate_helper_completion_progress(
                payload.get("diagnostic", {})
            )
            write_progress(
                progress_path,
                "done",
                "批量生成字幕 helper 已完成",
                batch_index=payload.get("diagnostic", {}).get("batch_count", 0),
                total_batches=payload.get("diagnostic", {}).get("batch_count", 0),
                progress_index=completion_index,
                progress_total=completion_total,
            )
            return 0 if payload.get("ok") else 1
        if args.mode == "generate_subtitles" and args.generate_engine == "v5":
            print(
                "[subfix] v5 需要连续音轨 batch plan，generate_subtitles 单段模式已回退 v3",
                file=sys.stderr,
            )
            args.generate_engine = "v3"

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
                    raw_segments = segments_from_ctc_char_segments(
                        rows,
                        raw_payload.get("char_segments") or [],
                        fps=args.fps,
                        timeline_start_frame=args.timeline_start_frame,
                    )
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
                        if "ctc_end_frame" in segment:
                            aligned_row["ctc_end_frame"] = segment["ctc_end_frame"]
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
                    segmentation_profile=load_segmentation_profile(args.segmentation_profile),
                    speech_regions=speech_regions,
                    subtitle_mode=args.subtitle_mode,
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
                    backend_diagnostic = raw_payload.get("diagnostic") if isinstance(raw_payload.get("diagnostic"), dict) else {}
                    for key in (
                        "forced_aligner",
                        "device_map",
                        "requested_model",
                        "forced_align_item_count",
                        "uses_word_timing",
                    ):
                        if key in backend_diagnostic:
                            diagnostic[key] = backend_diagnostic[key]
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
                            segmentation_profile=load_segmentation_profile(args.segmentation_profile),
                            speech_regions=speech_regions,
                            subtitle_mode=args.subtitle_mode,
                        )
                        diagnostic["generated_subtitle_count"] = len(raw_payload["subtitle_rows"])
                        diagnostic["generated_subtitles_used_word_timing"] = any(segment.get("words") for segment in segments)
                audio_used = str(cut_path)

        segments = normalize_segments(raw_payload)
        diagnostic["raw_segment_count"] = len(raw_payload.get("segments") or [])
        diagnostic["segment_count"] = len(segments)
        diagnostic["text_length"] = len(str(raw_payload.get("text") or "").strip())
        if args.srt_output and args.mode in {"generate_subtitles", "generate_subtitles_batch"}:
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
        if args.mode == "generate_subtitles_batch" and args.diagnostic_output:
            failure_code = "generate_failed"
            if args.generate_engine in {"v4", "v5"} and isinstance(exc, generate_v4.V4AlignmentError):
                failure_code = f"{args.generate_engine}_alignment_failed"
            write_payload(
                Path(args.diagnostic_output).expanduser(),
                sanitize_generate_diagnostic_payload(
                    {
                        "ok": False,
                        "diagnostic": {
                            "subtitle_mode": args.subtitle_mode,
                            "generate_engine": args.generate_engine,
                            "failure_code": failure_code,
                        },
                        "subtitle_rows": [],
                    }
                ),
            )
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
