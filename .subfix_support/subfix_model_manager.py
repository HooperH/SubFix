#!/usr/bin/env python3
"""Optional, offline-inspectable SubFix model catalog and installer."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import threading
import time
from urllib.parse import urlparse

import subfix_qwen_local_manager as runtime


# Official repository file manifests captured 2026-11-09. The pinned hashes also
# verify the ModelScope mirror without assuming its revision IDs match Git.
ASR_MANIFESTS = [
    {
        "repo": "Qwen/Qwen3-ASR-0.6B",
        "revision": "5eb144179a02acc5e5ba31e748d22b0cf3e303b0",
        "files": {
            "chat_template.json": {
                "size": 1161,
                "hash": "c44736493efd71ec96218cc626904698cdb13235",
                "algorithm": "git-sha1"
            },
            "config.json": {
                "size": 6193,
                "hash": "8113d596dec2d1a2ef4552edfe005ee84d16d9ec",
                "algorithm": "git-sha1"
            },
            "generation_config.json": {
                "size": 142,
                "hash": "7382a4d347c0a865b76bb1b8277f66a5ac312854",
                "algorithm": "git-sha1"
            },
            "merges.txt": {
                "size": 1671853,
                "hash": "31349551d90c7606f325fe0f11bbb8bd5fa0d7c7",
                "algorithm": "git-sha1"
            },
            "model.safetensors": {
                "size": 1876091704,
                "hash": "79d6cbd4c98c7bbffe9db2edac07f56cd6637d0d5944b27f6c2b8353840323ea",
                "algorithm": "sha256"
            },
            "preprocessor_config.json": {
                "size": 330,
                "hash": "8f7f07346466d5d494ec0d4969d1c3d0190eed72",
                "algorithm": "git-sha1"
            },
            "tokenizer_config.json": {
                "size": 12487,
                "hash": "b93109843922a40c6654c5449d3bf95372267c66",
                "algorithm": "git-sha1"
            },
            "vocab.json": {
                "size": 2776833,
                "hash": "4783fe10ac3adce15ac8f358ef5462739852c569",
                "algorithm": "git-sha1"
            }
        }
    },
    {
        "repo": "Qwen/Qwen3-ASR-1.7B",
        "revision": "7278e1e70fe206f11671096ffdd38061171dd6e5",
        "files": {
            "chat_template.json": {
                "size": 1161,
                "hash": "c44736493efd71ec96218cc626904698cdb13235",
                "algorithm": "git-sha1"
            },
            "config.json": {
                "size": 6194,
                "hash": "2bc16c9d4ca08963715cfb94d879799b9adbd0e9",
                "algorithm": "git-sha1"
            },
            "generation_config.json": {
                "size": 142,
                "hash": "7382a4d347c0a865b76bb1b8277f66a5ac312854",
                "algorithm": "git-sha1"
            },
            "merges.txt": {
                "size": 1671853,
                "hash": "31349551d90c7606f325fe0f11bbb8bd5fa0d7c7",
                "algorithm": "git-sha1"
            },
            "model-00001-of-00002.safetensors": {
                "size": 4220320824,
                "hash": "a4cd1f1a04d90b757dc7f7dd26254e69a013b19e80efe590a83c6a3bde8608d6",
                "algorithm": "sha256"
            },
            "model-00002-of-00002.safetensors": {
                "size": 478200688,
                "hash": "6e0b9d9e09e2e0238e7ef3cc8a484ab387e91b90f1900bedf88bc92d7929ccfc",
                "algorithm": "sha256"
            },
            "model.safetensors.index.json": {
                "size": 64821,
                "hash": "1048a4eb4f21fef9aea06d8568a784b2b5595689",
                "algorithm": "git-sha1"
            },
            "preprocessor_config.json": {
                "size": 330,
                "hash": "8f7f07346466d5d494ec0d4969d1c3d0190eed72",
                "algorithm": "git-sha1"
            },
            "tokenizer_config.json": {
                "size": 12487,
                "hash": "b93109843922a40c6654c5449d3bf95372267c66",
                "algorithm": "git-sha1"
            },
            "vocab.json": {
                "size": 2776833,
                "hash": "4783fe10ac3adce15ac8f358ef5462739852c569",
                "algorithm": "git-sha1"
            }
        }
    }
]
ASR_IDS = ("qwen3-asr-0.6b", "qwen3-asr-1.7b")
ALIGNER_ID = "qwen3-forced-aligner-0.6b"
ALIGNER_FILENAME = "qwen3-forced-aligner-0.6b-f16.gguf"
ALIGNER_SIZE = 1842214304
ALIGNER_SHA256 = "9ae1928e6dfbcb32511a1666e187c15bf96bc886dee6a02f2441dc5b5791994c"
SOURCE_LABELS = {"modelscope": "魔搭国内源", "huggingface": "Hugging Face 备用源", "publisher": "模型发布源"}
ProgressReporter = runtime.ProgressReporter


def default_data_root() -> Path:
    return Path(os.getenv("SUBFIX_DATA_ROOT") or Path.home() / "Library" / "Application Support" / "SubFix").expanduser()


@dataclass(frozen=True)
class ModelPaths:
    root: Path
    data_root: Path

    @property
    def runtime_paths(self) -> runtime.SubFixQwenPaths:
        return runtime.SubFixQwenPaths(self.root, self.data_root)

    @property
    def selection(self) -> Path:
        return self.data_root / "model-selection.json"

    @property
    def models(self) -> Path:
        return self.data_root / "models"

    @property
    def staging(self) -> Path:
        return self.data_root / "downloads"

    @property
    def lock(self) -> Path:
        return self.runtime_paths.install_lock


def require_model_id(model_id: str) -> None:
    if model_id not in (*ASR_IDS, ALIGNER_ID):
        raise ValueError(f"未知模型：{model_id}")


def spec_for(model_id: str) -> dict:
    require_model_id(model_id)
    if model_id == ALIGNER_ID:
        return {"files": {ALIGNER_FILENAME: {"size": ALIGNER_SIZE, "hash": ALIGNER_SHA256, "algorithm": "sha256"}}}
    return ASR_MANIFESTS[ASR_IDS.index(model_id)]


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def file_matches(path: Path, entry: dict, *, full: bool = False) -> bool:
    try:
        if not path.is_file() or path.stat().st_size != entry["size"]:
            return False
        if not full:
            return True
        digest = hashlib.sha256() if entry["algorithm"] == "sha256" else hashlib.sha1()
        if entry["algorithm"] == "git-sha1":
            digest.update(f"blob {entry['size']}\0".encode())
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest() == entry["hash"]
    except OSError:
        return False


def compatible_aligner_header(path: Path) -> bool:
    # The current runtime requires predict-woo's qwen3-asr metadata namespace.
    # CrispASR/llama.cpp GGUF files have the same extension but are incompatible.
    try:
        with path.open("rb") as stream:
            data = stream.read(16 * 1024)
        return (data[:4] == b"GGUF" and len(data) >= 24
                and struct.unpack_from("<I", data, 4)[0] in (2, 3)
                and b"qwen3-asr.audio.encoder.layer_count" in data
                and b"qwen3-asr.classify_num" in data)
    except OSError:
        return False


def validate_model(path: Path, model_id: str, *, full: bool = False) -> bool:
    spec = spec_for(model_id)
    if model_id == ALIGNER_ID:
        return compatible_aligner_header(path) and file_matches(path, spec["files"][ALIGNER_FILENAME], full=full)
    if not path.is_dir():
        return False
    if not all(file_matches(path / name, entry, full=full) for name, entry in spec["files"].items()):
        return False
    config = read_json(path / "config.json")
    expected_hidden = 1024 if model_id == ASR_IDS[0] else 2048
    thinker = config.get("thinker_config")
    text_config = thinker.get("text_config") if isinstance(thinker, dict) else None
    return (config.get("model_type") == "qwen3_asr" and isinstance(text_config, dict)
            and text_config.get("hidden_size") == expected_hidden)


def model_candidates(paths: ModelPaths, model_id: str):
    if model_id == ALIGNER_ID:
        yield paths.models / ALIGNER_FILENAME
        yield paths.root / "models" / ALIGNER_FILENAME
        return
    yield paths.models / model_id
    yield paths.root / "models" / model_id
    repo = spec_for(model_id)["repo"].replace("/", "--")
    for cache in runtime.huggingface_cache_roots():
        snapshots = cache / ("models--" + repo) / "snapshots"
        if snapshots.is_dir():
            yield from sorted(snapshots.iterdir(), reverse=True)


def find_model(paths: ModelPaths, model_id: str, *, full: bool = False) -> Path | None:
    for candidate in model_candidates(paths, model_id):
        if validate_model(candidate, model_id, full=full):
            return candidate
    return None


def environment_python(paths: ModelPaths) -> Path | None:
    existing = runtime.ready_environment_python(paths.runtime_paths)
    if existing is not None:
        return existing
    marker = read_json(paths.data_root / "model-runtime.json")
    recorded = marker.get("python")
    if isinstance(recorded, str):
        for candidate in legacy_environment_candidates(paths):
            if str(candidate) == recorded and candidate.is_file():
                return candidate
    return None


def legacy_environment_candidates(paths: ModelPaths):
    yield paths.root / ".subfix_asr_env" / "bin" / "python"
    yield paths.runtime_paths.legacy_env_dir / "bin" / "python"


def selected_model_id(paths: ModelPaths) -> str:
    value = read_json(paths.selection).get("selected_model_id")
    if value in ASR_IDS:
        return value
    if find_model(paths, ASR_IDS[1]) is not None:
        return ASR_IDS[1]
    return ASR_IDS[0]


def aligner_sources(paths: ModelPaths) -> dict[str, str]:
    config = read_json(paths.root / "model-downloads.json").get(ALIGNER_ID, {})
    if not isinstance(config, dict):
        return {}
    # A distributor may host the already-verified compatible artifact; arbitrary
    # GGUF formats are never made valid merely by adding them to this config.
    if config.get("sha256") != ALIGNER_SHA256 or config.get("size") != ALIGNER_SIZE:
        return {}
    sources = config.get("sources", {})
    if not isinstance(sources, dict):
        return {}
    valid = {}
    for name, url in sources.items():
        if name not in SOURCE_LABELS or not isinstance(url, str):
            continue
        parsed = urlparse(url)
        if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
            valid[name] = url
    return valid


def model_status(paths: ModelPaths, model_id: str) -> dict:
    require_model_id(model_id)
    location = find_model(paths, model_id)
    is_asr = model_id in ASR_IDS
    environment_ready = environment_python(paths) is not None if is_asr else True
    ready = location is not None and environment_ready
    sources = ["modelscope", "huggingface"] if is_asr else list(aligner_sources(paths))
    total = sum(item["size"] for item in spec_for(model_id)["files"].values())
    artifacts = any(candidate.exists() for candidate in model_candidates(paths, model_id))
    state = "installed" if ready else ("environment_missing" if location else ("repair_required" if artifacts else "missing"))
    label = spec_for(model_id).get("repo", "Qwen3-ForcedAligner-0.6B").replace("Qwen/", "")
    return {
        "id": model_id, "label": label, "kind": "asr" if is_asr else "aligner",
        "state": state, "ready": ready, "model_installed": location is not None,
        "environment_ready": environment_ready, "model_path": str(location) if location else "",
        "download_bytes": total, "installed_bytes": total if location else 0,
        "dependencies": [ALIGNER_ID] if is_asr else [], "sources": sources,
        "download_available": bool(sources),
        "download_unavailable_reason": "" if sources else "尚未配置兼容对齐模型的下载地址，请导入 SubFix 配套 GGUF 文件。",
        "download_url": ("https://modelscope.cn/models/" + spec_for(model_id)["repo"]) if is_asr else "",
        "description": ("轻量本地语音识别" if model_id == ASR_IDS[0] else "高容量本地语音识别") if is_asr else "共享字幕时间对齐模型",
    }


def runtime_status(paths: ModelPaths, model_id: str | None = None) -> dict:
    model_id = model_id or selected_model_id(paths)
    require_model_id(model_id)
    if model_id == ALIGNER_ID:
        item = model_status(paths, model_id)
        return {**item, "model_id": model_id, "aligner": item["model_path"], "missing": [] if item["ready"] else [ALIGNER_ID]}
    asr = model_status(paths, model_id)
    aligner = model_status(paths, ALIGNER_ID)
    missing = []
    if not asr["model_installed"]:
        missing.append(model_id)
    if not asr["environment_ready"]:
        missing.append("qwen-runtime")
    if not aligner["ready"]:
        missing.append(ALIGNER_ID)
    python = environment_python(paths)
    return {
        "state": "installed" if not missing else "missing", "ready": not missing,
        "python": str(python) if python else "", "model": asr["model_path"],
        "aligner": aligner["model_path"], "model_id": model_id, "missing": missing,
    }


def list_models(paths: ModelPaths) -> dict:
    return {"models": [model_status(paths, value) for value in (*ASR_IDS, ALIGNER_ID)],
            "selected_model_id": selected_model_id(paths), "data_root": str(paths.data_root)}


def select_model(paths: ModelPaths, model_id: str) -> dict:
    if model_id not in ASR_IDS:
        raise ValueError("只能将语音识别模型设为当前模型")
    runtime.write_json(paths.selection, {"selected_model_id": model_id})
    return {**runtime_status(paths, model_id), "selected_model_id": model_id}


def ensure_environment(paths: ModelPaths, report: ProgressReporter) -> Path:
    existing = environment_python(paths)
    if existing:
        return existing
    rp = paths.runtime_paths
    for candidate in legacy_environment_candidates(paths):
        if candidate.is_file():
            report("检查已有环境", "正在检查已有本地识别环境，无需重新下载模型")
            if runtime.python_can_import_qwen_asr(candidate):
                runtime.write_json(paths.data_root / "model-runtime.json", {"python": str(candidate)})
                return candidate
    runtime.ensure_base_python(rp.base_python)
    report("准备运行环境", "正在准备本地语音识别环境；对齐模型无需此环境")
    runtime.create_or_reuse_venv(rp.base_python, rp.env_dir)
    if not runtime.python_can_import_qwen_asr(rp.env_python):
        runtime.install_qwen_dependencies(rp.env_python, report, rp.install_log)
    if not runtime.python_can_import_qwen_asr(rp.env_python):
        raise RuntimeError("本地语音识别环境校验失败，请重试准备环境。")
    runtime.write_json(rp.ready_marker, {"python": str(rp.env_python), "runtime_ready": True})
    return rp.env_python


def ensure_space(directory: Path, required: int) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(directory).free < required + 256 * 1024 * 1024:
        raise RuntimeError("磁盘空间不足，请释放空间后重试；下载缓存会保留。")


@contextmanager
def validation_progress(report: ProgressReporter, message: str):
    stop, monitor = runtime.start_command_heartbeat(report, ("校验模型", message))
    try:
        yield
    finally:
        if stop is not None:
            stop.set()
        if monitor is not None:
            monitor.join(timeout=2)


def reusable_download_bytes(directory: Path, files: dict, *, full: bool = False) -> int:
    total = 0
    for name, entry in files.items():
        destination = directory / name
        if not destination.is_symlink() and file_matches(destination, entry, full=full):
            total += entry["size"]
            continue
        partial = destination.with_name(destination.name + ".part")
        try:
            if partial.is_symlink() or not partial.is_file():
                continue
            size = partial.stat().st_size
            if 0 < size < entry["size"] or file_matches(partial, entry, full=full):
                total += size
        except OSError:
            continue
    return total


@contextmanager
def progress_monitor(directory: Path, files: dict, report: ProgressReporter, label: str):
    total = sum(entry["size"] for entry in files.values())
    stop = threading.Event()
    def poll():
        while not stop.is_set():
            current = reusable_download_bytes(directory, files)
            report("下载模型", label, progress_index=min(current, total), progress_total=total)
            stop.wait(1)
    thread = threading.Thread(target=poll, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=2)


def download_file(url: str, destination: Path, entry: dict, report: ProgressReporter, log: Path) -> None:
    if file_matches(destination, entry, full=True):
        return
    partial = destination.with_name(destination.name + ".part")
    if partial.is_symlink() or destination.is_symlink():
        raise RuntimeError("下载暂存文件不能是软链接")
    if file_matches(partial, entry, full=True):
        partial.replace(destination)
        return
    if partial.exists() and partial.stat().st_size >= entry["size"]:
        partial.rename(partial.with_name(partial.name + f".invalid-{time.time_ns()}"))
    ensure_space(destination.parent, entry["size"] - (partial.stat().st_size if partial.exists() else 0))
    curl = shutil.which("curl")
    if not curl:
        raise RuntimeError("未找到系统下载工具 curl，请使用离线导入。")
    command = [curl, "--fail", "--location", "--silent", "--show-error",
               "--proto", "=https", "--proto-redir", "=https",
               "--connect-timeout", "20", "--speed-time", "60", "--speed-limit", "1024",
               "--retry", "2", "--retry-delay", "2", "--continue-at", "-",
               "--max-filesize", str(entry["size"]),
               "--output", str(partial), url]
    try:
        runtime.run_checked(command, error_prefix="模型文件下载失败", log_path=log)
    except RuntimeError as exc:
        # Some mirrors refuse Range requests. Preserve that partial and retry
        # just this file from zero instead of discarding the whole model cache.
        message = str(exc).lower()
        if partial.exists() and any(value in message for value in ("(33)", "(416)", "range", "resume")):
            partial.rename(partial.with_name(partial.name + f".unresumable-{time.time_ns()}"))
            report("重新连接", "当前来源不支持此文件续传，保留缓存并重新下载该文件。")
            ensure_space(destination.parent, entry["size"])
            runtime.run_checked(command, error_prefix="模型文件下载失败", log_path=log)
        else:
            raise
    report("校验模型", f"正在校验 {destination.name}")
    if not file_matches(partial, entry, full=True):
        partial.rename(partial.with_name(partial.name + f".invalid-{time.time_ns()}"))
        raise RuntimeError(f"{destination.name} 校验失败，错误文件已隔离，请重试或使用离线导入。")
    partial.replace(destination)


def publish_staging(staging: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.parent.is_symlink():
        raise RuntimeError("模型安装目录不能是软链接")
    backup = None
    if target.exists():
        backup = target.with_name(target.name + f".backup-{time.time_ns()}")
        target.rename(backup)
    try:
        staging.replace(target)
    except OSError:
        if backup is not None:
            backup.replace(target)
        raise


def install_model(paths: ModelPaths, model_id: str, source: str, report: ProgressReporter) -> dict:
    require_model_id(model_id)
    if source not in ("auto", *SOURCE_LABELS):
        raise ValueError("未知下载源")
    current = model_status(paths, model_id)
    with validation_progress(report, "正在检查已有模型的完整性"):
        existing = find_model(paths, model_id, full=True)
    if model_id in ASR_IDS:
        ensure_environment(paths, report)
    if existing is not None:
        # If the preferred copy is corrupt, repair it from a valid cached copy.
        if current["model_path"] != str(existing):
            return import_model(paths, model_id, existing, report)
        report("完成", "模型校验通过，无需重复下载")
        return model_status(paths, model_id)
    source_urls = aligner_sources(paths) if model_id == ALIGNER_ID else {key: "" for key in ("modelscope", "huggingface")}
    available = list(source_urls) if source == "auto" else [source]
    if not source_urls or any(key not in source_urls for key in available):
        raise RuntimeError(current["download_unavailable_reason"] or "该模型没有所选下载源，请选择自动或其他来源。")
    spec = spec_for(model_id)
    staging = paths.staging / model_id
    if staging.is_symlink() or paths.staging.is_symlink():
        raise RuntimeError("模型下载目录不能是软链接")
    total = sum(entry["size"] for entry in spec["files"].values())
    ensure_space(staging, max(0, total - reusable_download_bytes(staging, spec["files"], full=True)))
    errors = []
    for source_name in available:
        try:
            with progress_monitor(staging, spec["files"], report, f"正在通过{SOURCE_LABELS[source_name]}下载 {current['label']}"):
                for name, entry in spec["files"].items():
                    if model_id == ALIGNER_ID:
                        url = source_urls[source_name]
                    elif source_name == "modelscope":
                        url = f"https://modelscope.cn/models/{spec['repo']}/resolve/master/{name}"
                    else:
                        url = f"https://huggingface.co/{spec['repo']}/resolve/{spec['revision']}/{name}"
                    download_file(url, staging / name, entry, report, paths.runtime_paths.install_log)
            break
        except RuntimeError as exc:
            errors.append(f"{SOURCE_LABELS[source_name]}：{exc}")
            report("切换下载源", "下载未完成，保留已下载文件，正在尝试其他可用来源。")
    else:
        raise RuntimeError("下载未完成，请重试或离线导入。\n" + "\n".join(errors))
    candidate = staging / ALIGNER_FILENAME if model_id == ALIGNER_ID else staging
    if not validate_model(candidate, model_id):
        raise RuntimeError("下载内容与模型不匹配，未启用；请重新下载或导入正确的模型。")
    target = paths.models / (ALIGNER_FILENAME if model_id == ALIGNER_ID else model_id)
    publish_staging(candidate, target)
    report("完成", "模型已下载并校验；配套模型请在模型中心单独安装")
    return model_status(paths, model_id)


def import_model(paths: ModelPaths, model_id: str, input_path: Path, report: ProgressReporter) -> dict:
    require_model_id(model_id)
    source = input_path.expanduser().resolve()
    staging = paths.staging / (model_id + "-import")
    resolved_staging = staging.resolve()
    if (source == resolved_staging or source.is_relative_to(resolved_staging)
            or resolved_staging.is_relative_to(source)):
        raise RuntimeError("导入来源与暂存目录重叠，请将模型移到独立目录后重新选择；源文件未修改。")
    with validation_progress(report, "正在检查模型格式与文件完整性；不会联网"):
        if not validate_model(source, model_id, full=True):
            raise RuntimeError("模型不匹配或文件不完整。ASR 请选择完整官方模型目录；对齐请选择 SubFix 配套 F16 GGUF。源文件未修改。")
        existing = find_model(paths, model_id)
        reuse_existing = existing is not None and (
            existing.resolve() == source or validate_model(existing, model_id, full=True))
    if reuse_existing:
        report("完成", "已存在完整模型，无需重复导入")
        return model_status(paths, model_id)
    spec = spec_for(model_id)
    total = sum(entry["size"] for entry in spec["files"].values())
    if staging.is_symlink() or paths.staging.is_symlink():
        raise RuntimeError("模型导入暂存目录不能是软链接")
    for name in spec["files"]:
        origin = source if model_id == ALIGNER_ID else source / name
        destination = staging / name
        if destination.is_symlink() or (destination.exists() and origin.samefile(destination)):
            raise RuntimeError("导入文件与暂存文件重叠或暂存文件为软链接；源文件未修改。")
    ensure_space(staging, total)
    done = 0
    for name, entry in spec["files"].items():
        origin = source if model_id == ALIGNER_ID else source / name
        destination = staging / name
        if destination.is_symlink():
            raise RuntimeError("模型导入暂存文件不能是软链接")
        with origin.open("rb") as reader, destination.open("wb") as writer:
            for chunk in iter(lambda: reader.read(4 * 1024 * 1024), b""):
                writer.write(chunk)
                done += len(chunk)
                report("导入模型", f"正在导入 {name}", progress_index=done, progress_total=total)
    candidate = staging / ALIGNER_FILENAME if model_id == ALIGNER_ID else staging
    with validation_progress(report, "正在校验导入后的模型完整性"):
        if not validate_model(candidate, model_id, full=True):
            raise RuntimeError("复制后的模型校验失败，未启用，请检查磁盘后重试。")
    target = paths.models / (ALIGNER_FILENAME if model_id == ALIGNER_ID else model_id)
    publish_staging(candidate, target)
    result = model_status(paths, model_id)
    report("完成", "模型已离线导入" if result["ready"] else "模型已离线导入；请点击下载安装准备运行环境")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SubFix 模型中心")
    parser.add_argument("--action", choices=("list", "status", "install", "import", "select"), required=True)
    parser.add_argument("--model-id", choices=(*ASR_IDS, ALIGNER_ID))
    parser.add_argument("--source", choices=("auto", *SOURCE_LABELS), default="auto")
    parser.add_argument("--input-path", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--data-root", type=Path, default=default_data_root())
    parser.add_argument("--output", type=Path)
    parser.add_argument("--progress-json", type=Path)
    args = parser.parse_args(argv)
    paths = ModelPaths(args.root.expanduser().resolve(), args.data_root.expanduser().resolve())
    writer = runtime.make_progress_reporter(args.progress_json)
    report_lock = threading.Lock()
    def report(stage: str, message: str, **details):
        with report_lock:
            writer(stage, message, **details)
    code = 0
    try:
        if args.action == "list":
            payload = list_models(paths)
        elif args.action == "status":
            payload = runtime_status(paths, args.model_id)
        else:
            if args.model_id is None:
                raise ValueError("请先选择模型")
            with runtime.exclusive_install_lock(paths.lock):
                if args.action == "select":
                    payload = select_model(paths, args.model_id)
                elif args.action == "install":
                    payload = install_model(paths, args.model_id, args.source, report)
                else:
                    if args.input_path is None:
                        raise ValueError("请选择模型目录或 GGUF 文件")
                    payload = import_model(paths, args.model_id, args.input_path, report)
    except Exception as exc:
        payload = {"state": "error", "ready": False, "error": str(exc)}
        report("失败", str(exc))
        code = 1
    if args.output:
        runtime.write_json(args.output, payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
