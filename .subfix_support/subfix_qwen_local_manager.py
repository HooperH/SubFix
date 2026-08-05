#!/usr/bin/env python3
"""Manage the optional local Qwen ASR extension shipped beside SubFix."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Callable


QWEN_ASR_MODEL_ID = "Qwen/Qwen3-ASR-1.7B"
QWEN_ASR_REQUIRED_MODEL_FILES = (
    "config.json",
    "model.safetensors.index.json",
    "model-00001-of-00002.safetensors",
    "model-00002-of-00002.safetensors",
)
MODEL_DOWNLOAD_ETA_MIN_ELAPSED_SECONDS = 10
MODEL_DOWNLOAD_ETA_MIN_DOWNLOADED_BYTES = 8 * 1024 * 1024
ProgressReporter = Callable[..., None]


@dataclass(frozen=True)
class SubFixQwenPaths:
    root: Path

    @property
    def base_python(self) -> Path:
        return self.root / "runtime" / "python" / "bin" / "python3"

    @property
    def env_dir(self) -> Path:
        return self.root / "envs" / "qwen-local"

    @property
    def env_python(self) -> Path:
        return self.env_dir / "bin" / "python"

    @property
    def model_dir(self) -> Path:
        return self.root / "models" / "qwen3-asr-1.7b"

    @property
    def ready_marker(self) -> Path:
        return self.root / ".subfix-qwen-local-ready.json"

    @property
    def legacy_ready_marker(self) -> Path:
        return self.model_dir / ".subfix-ready.json"


def python_can_import_qwen_asr(python: Path) -> bool:
    try:
        result = subprocess.run(
            [str(python), "-c", "import qwen_asr, torch"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def model_directory_is_complete(model_dir: Path) -> bool:
    return model_dir.is_dir() and all(model_dir.joinpath(name).is_file() for name in QWEN_ASR_REQUIRED_MODEL_FILES)


def huggingface_cache_roots() -> list[Path]:
    roots: list[Path] = []
    explicit_cache = os.getenv("HF_HUB_CACHE") or os.getenv("HUGGINGFACE_HUB_CACHE")
    if explicit_cache:
        roots.append(Path(explicit_cache).expanduser())
    hf_home = os.getenv("HF_HOME")
    if hf_home:
        roots.append(Path(hf_home).expanduser() / "hub")
    roots.append(Path.home() / ".cache" / "huggingface" / "hub")
    return list(dict.fromkeys(roots))


def existing_model_dir(paths: SubFixQwenPaths) -> Path | None:
    if model_directory_is_complete(paths.model_dir):
        return paths.model_dir
    for cache_root in huggingface_cache_roots():
        snapshots_dir = cache_root / "models--Qwen--Qwen3-ASR-1.7B" / "snapshots"
        if not snapshots_dir.is_dir():
            continue
        candidates = sorted(
            (path for path in snapshots_dir.iterdir() if model_directory_is_complete(path)),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0]
    return None


def has_model_artifacts(paths: SubFixQwenPaths) -> bool:
    if paths.model_dir.is_dir():
        return True
    return any((root / "models--Qwen--Qwen3-ASR-1.7B" / "snapshots").is_dir() for root in huggingface_cache_roots())


def has_ready_marker(paths: SubFixQwenPaths) -> bool:
    return paths.ready_marker.is_file() or paths.legacy_ready_marker.is_file()


def inspect_install(paths: SubFixQwenPaths) -> dict[str, object]:
    model_dir = existing_model_dir(paths)
    if not paths.env_python.is_file() or not has_model_artifacts(paths):
        return {"state": "missing", "ready": False}
    # The marker is written only after the installer verifies the dependency;
    # do not re-import qwen_asr/torch on Resolve's synchronous status path.
    if model_dir is None or not has_ready_marker(paths):
        return {"state": "repair_required", "ready": False}
    return {
        "state": "installed",
        "ready": True,
        "python": str(paths.env_python),
        "model": str(model_dir),
    }


def ensure_base_python(python: Path) -> None:
    if not python.is_file():
        raise RuntimeError(f"未找到 SubFix 内置 Python：{python}")


def run_checked(command: list[str], *, error_prefix: str) -> None:
    try:
        subprocess.run(command, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"{error_prefix}：{exc}") from exc


def create_or_reuse_venv(base_python: Path, env_dir: Path) -> None:
    if not (env_dir / "bin" / "python").is_file():
        run_checked([str(base_python), "-m", "venv", str(env_dir)], error_prefix="创建本地 Qwen 环境失败")


def install_qwen_dependencies(env_python: Path) -> None:
    run_checked(
        [
            str(env_python),
            "-m",
            "pip",
            "install",
            "--upgrade",
            "pip",
            "qwen-asr",
            "torch",
            "huggingface_hub",
        ],
        error_prefix="安装本地 Qwen 依赖失败",
    )


def directory_size_bytes(directory: Path) -> int:
    if not directory.is_dir():
        return 0
    total = 0
    for path in directory.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            # The downloader may replace a temporary file between iteration and stat.
            continue
    return total


def fetch_model_total_bytes(env_python: Path) -> int | None:
    script = (
        "from huggingface_hub import HfApi\n"
        "import json, sys\n"
        "info = HfApi().model_info(sys.argv[1], files_metadata=True)\n"
        "print(json.dumps(sum(int(getattr(item, 'size', 0) or 0) for item in info.siblings)))\n"
    )
    try:
        result = subprocess.run(
            [str(env_python), "-c", script, QWEN_ASR_MODEL_ID],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        total = int(result.stdout.strip())
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
        return None
    return total if total > 0 else None


def model_download_eta_seconds(
    *,
    total_bytes: int,
    initial_bytes: int,
    current_bytes: int,
    started_at: float,
    current_at: float,
) -> int | None:
    elapsed = max(0.0, current_at - started_at)
    downloaded = max(0, current_bytes - initial_bytes)
    if (
        elapsed < MODEL_DOWNLOAD_ETA_MIN_ELAPSED_SECONDS
        or downloaded < MODEL_DOWNLOAD_ETA_MIN_DOWNLOADED_BYTES
        or current_bytes >= total_bytes
    ):
        return None
    average_speed = downloaded / elapsed
    if average_speed <= 0:
        return None
    return max(1, round((total_bytes - current_bytes) / average_speed))


def report_model_download_progress(
    model_dir: Path,
    total_bytes: int | None,
    report: ProgressReporter,
    stop_event: threading.Event,
) -> None:
    initial_bytes = directory_size_bytes(model_dir)
    started_at = time.monotonic()
    while not stop_event.is_set():
        current_at = time.monotonic()
        current_bytes = directory_size_bytes(model_dir)
        details: dict[str, object] = {}
        if total_bytes is not None:
            details["progress_index"] = min(current_bytes, total_bytes)
            details["progress_total"] = total_bytes
            eta_seconds = model_download_eta_seconds(
                total_bytes=total_bytes,
                initial_bytes=initial_bytes,
                current_bytes=current_bytes,
                started_at=started_at,
                current_at=current_at,
            )
            if eta_seconds is not None:
                details["eta_seconds"] = eta_seconds
        report("下载模型", "正在下载 Qwen3-ASR-1.7B", **details)
        stop_event.wait(1)


def download_model(env_python: Path, model_dir: Path, report: ProgressReporter) -> None:
    script = (
        "from huggingface_hub import snapshot_download\n"
        "import sys\n"
        "snapshot_download(repo_id=sys.argv[1], local_dir=sys.argv[2])\n"
    )
    stop_event = threading.Event()
    monitor = threading.Thread(
        target=report_model_download_progress,
        args=(model_dir, fetch_model_total_bytes(env_python), report, stop_event),
        daemon=True,
    )
    monitor.start()
    try:
        run_checked(
            [str(env_python), "-c", script, QWEN_ASR_MODEL_ID, str(model_dir)],
            error_prefix="下载模型失败",
        )
    finally:
        stop_event.set()
        monitor.join(timeout=2)


def write_ready_marker(paths: SubFixQwenPaths, model_dir: Path) -> None:
    paths.ready_marker.write_text(
        json.dumps(
            {"model_id": QWEN_ASR_MODEL_ID, "python": str(paths.env_python), "model": str(model_dir)},
            ensure_ascii=False,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def install(paths: SubFixQwenPaths, report: ProgressReporter) -> dict[str, object]:
    existing_status = inspect_install(paths)
    if existing_status.get("ready"):
        report("完成", "本地 Qwen 已安装，正在启用")
        return existing_status
    ensure_base_python(paths.base_python)
    paths.ready_marker.unlink(missing_ok=True)
    report("创建环境", "正在准备本地 Qwen 运行环境")
    create_or_reuse_venv(paths.base_python, paths.env_dir)
    if not python_can_import_qwen_asr(paths.env_python):
        report("安装依赖", "正在安装 qwen-asr 与 PyTorch")
        install_qwen_dependencies(paths.env_python)
    else:
        report("检查依赖", "本地 Qwen 运行环境已就绪")
    model_dir = existing_model_dir(paths)
    if model_dir is not None:
        report("复用模型", "正在复用已下载的 Qwen3-ASR-1.7B")
    else:
        try:
            report("下载模型", "正在下载 Qwen3-ASR-1.7B")
            download_model(paths.env_python, paths.model_dir, report)
            model_dir = paths.model_dir
        except Exception as exc:
            paths.ready_marker.unlink(missing_ok=True)
            raise RuntimeError(f"下载模型失败：{exc}") from exc
    report("校验模型", "正在验证模型文件与运行环境")
    write_ready_marker(paths, model_dir)
    status = inspect_install(paths)
    if not status.get("ready"):
        paths.ready_marker.unlink(missing_ok=True)
        raise RuntimeError("本地 Qwen 安装校验未通过")
    report("完成", "本地 Qwen 已安装，可用于生成字幕")
    return status


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    temp_path.replace(path)


def make_progress_reporter(progress_path: Path | None) -> ProgressReporter:
    def report(stage: str, message: str, **details: object) -> None:
        if progress_path is not None:
            payload: dict[str, object] = {"stage": stage, "message": message, **details}
            if "progress_index" not in payload or "progress_total" not in payload:
                payload["indeterminate"] = True
            write_json(progress_path, payload)

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SubFix 本地 Qwen 管理器")
    parser.add_argument("--action", choices=("status", "install"), required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--progress-json", type=Path)
    args = parser.parse_args(argv)
    paths = SubFixQwenPaths(args.root.resolve())
    try:
        if args.action == "install":
            payload = install(paths, make_progress_reporter(args.progress_json))
        else:
            payload = inspect_install(paths)
    except Exception as exc:
        payload = {"state": "error", "ready": False, "error": str(exc)}
        if args.progress_json is not None:
            write_json(args.progress_json, {"stage": "失败", "message": str(exc), "indeterminate": True})
        if args.output is not None:
            write_json(args.output, payload)
        else:
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 1
    if args.output is not None:
        write_json(args.output, payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
