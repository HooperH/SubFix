#!/usr/bin/env python3
"""Website-first, resumable updater for the macOS SubFix Resolve scripts."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import shutil
import stat
import tempfile
import threading
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import zipfile


REPOSITORY = "HooperH/SubFix"
RELEASE_API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
WEBSITE_URL = "https://subfix-by-hooper.ysjf.chatgpt.site/"
WEBSITE_API_URL = WEBSITE_URL + "api/latest-release"
UPDATE_CACHE_ROOT = Path.home() / "Library/Caches/SubFix/updates"
MAX_UPDATE_BYTES = 256 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024
USER_UTILITY_ROOT = Path.home() / "Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
MANIFEST_NAME = "subfix-update-manifest.json"
UPDATE_FILE_PATHS = frozenset(
    {
        "SubFix/SubFix.lua",
        "SubFix/生成选区字幕.lua",
        ".subfix_support/subfix_generate_selection_core.lua",
        ".subfix_support/subfix_process_group.py",
        ".subfix_support/subfix_update.py",
        ".subfix_support/subfix_asr_transcribe.py",
        ".subfix_support/subfix_generate_v4.py",
        ".subfix_support/subfix_generate_v5.py",
        ".subfix_support/subfix_generate_textnorm.py",
        ".subfix_support/subfix_qwen_local_manager.py",
        ".subfix_support/subfix_model_manager.py",
        ".subfix_support/subfix_model_center.lua",
        ".subfix_support/model-downloads.json",
        ".subfix_support/bin/ffmpeg",
        ".subfix_support/licenses/FFmpeg-LGPL-2.1.txt",
        ".subfix_support/setup_asr_env.sh",
        ".subfix_support/segmentation_profile.json",
        ".subfix_support/segmentation_profile_v3.json",
        ".subfix_support/segmentation_profile_v4.json",
    }
)


class UpdateError(RuntimeError):
    pass


def _https_url(value: Any) -> str:
    if not isinstance(value, str) or re.search(r"[\x00-\x20\x7f]", value):
        raise UpdateError("更新资源地址无效")
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise ValueError()
        _ = parsed.port
    except ValueError as exc:
        raise UpdateError("更新资源地址必须使用有效的 HTTPS 地址") from exc
    return value


def _sha256(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise UpdateError("SHA-256 校验值格式错误")
    return value.lower()


@contextmanager
def _time_limit(seconds: float):
    # urllib's socket timeout does not bound DNS or a slowly streaming response.
    if seconds <= 0 or threading.current_thread() is not threading.main_thread():
        raise UpdateError("更新网络请求超时，请重试或前往官网下载")
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()
    def expired(_signum, _frame):
        raise UpdateError("更新网络请求超时，请重试或前往官网下载")
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, min(seconds, previous_timer[0]) if previous_timer[0] else seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0]:
            signal.setitimer(signal.ITIMER_REAL, max(0.001, previous_timer[0] - (time.monotonic() - started)), previous_timer[1])


def _read_json_url(url: str, timeout_seconds: float) -> dict[str, Any]:
    request = Request(_https_url(url), headers={"Accept": "application/json", "User-Agent": "SubFix-Updater"})
    with urlopen(request, timeout=timeout_seconds) as response:
        _https_url(response.geturl())
        raw = response.read(MAX_METADATA_BYTES + 1)
    if len(raw) > MAX_METADATA_BYTES:
        raise UpdateError("更新清单过大")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise UpdateError("更新清单格式错误")
    return payload


def parse_version(value: str) -> tuple[int, int, int]:
    text = str(value or "").strip()
    if text.startswith("v"):
        text = text[1:]
    parts = text.split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise UpdateError(f"版本号无效: {value}")
    return tuple(int(part) for part in parts)  # type: ignore[return-value]


def normalized_version(value: str) -> str:
    return ".".join(str(part) for part in parse_version(value))


def _no_update(remote_version: str, current_version: str) -> dict[str, Any] | None:
    remote, current = parse_version(remote_version), parse_version(current_version)
    if remote <= current:
        message = "当前已是最新稳定版" if remote == current else f"当前版本高于最新正式版（{remote_version}）"
        return {"ok": False, "update_available": False, "error": message, "version": remote_version}
    return None


def release_to_update_info(release: dict[str, Any], current_version: str) -> dict[str, Any]:
    if release.get("draft") is True or release.get("prerelease") is True:
        return {"ok": False, "error": "最新发布不是稳定版"}
    try:
        remote_version = normalized_version(str(release.get("tag_name") or ""))
        no_update = _no_update(remote_version, current_version)
    except UpdateError as exc:
        return {"ok": False, "error": str(exc)}
    if no_update is not None:
        return no_update

    prefix = f"SubFix-update-v{remote_version}"
    assets = release.get("assets")
    if not isinstance(assets, list):
        return {"ok": False, "error": "Release 未提供更新资源"}
    asset_urls = {
        str(asset.get("name")): str(asset.get("browser_download_url"))
        for asset in assets
        if isinstance(asset, dict) and str(asset.get("browser_download_url") or "").startswith("https://")
    }
    # 老更新器始终读取默认兼容包；支持完整清单的新更新器优先更新内置 FFmpeg。
    if all(asset_urls.get(prefix + "-full" + suffix) for suffix in (".zip", ".sha256")):
        prefix += "-full"
    zip_url = asset_urls.get(prefix + ".zip")
    sha256_url = asset_urls.get(prefix + ".sha256")
    if not zip_url or not sha256_url:
        return {"ok": False, "error": "Release 缺少更新 ZIP 或 SHA-256 校验文件"}
    return {
        "ok": True,
        "update_available": True,
        "version": remote_version,
        "name": str(release.get("name") or f"SubFix v{remote_version}"),
        "notes": str(release.get("body") or ""),
        "zip_url": zip_url,
        "sha256_url": sha256_url,
        "manual_download_url": WEBSITE_URL,
    }


def manifest_to_update_info(manifest: dict[str, Any], current_version: str) -> dict[str, Any]:
    if type(manifest.get("schemaVersion")) is not int or manifest["schemaVersion"] != 1 or manifest.get("channel") != "stable":
        raise UpdateError("官网更新清单版本不支持")
    if manifest.get("platform") != "macos" or manifest.get("arch") != "arm64":
        raise UpdateError("官网更新清单不适用于此平台")
    version = normalized_version(manifest.get("version", ""))
    try:
        date = datetime.fromisoformat(manifest["publishedAt"].replace("Z", "+00:00"))
        if date.tzinfo is None:
            raise ValueError()
    except (KeyError, AttributeError, TypeError, ValueError) as exc:
        raise UpdateError("官网更新清单发布时间无效") from exc
    manual_url = _https_url(manifest.get("downloadUrl"))
    _https_url(manifest.get("releaseUrl"))
    if type(manifest.get("sizeBytes")) is not int or manifest["sizeBytes"] <= 0:
        raise UpdateError("官网安装包大小无效")
    update = manifest.get("update")
    if not isinstance(update, dict):
        raise UpdateError("官网缺少更新包清单")
    url = _https_url(update.get("url"))
    checksum = _sha256(update.get("sha256"))
    size = update.get("sizeBytes")
    if type(size) is not int or not 0 < size <= MAX_UPDATE_BYTES:
        raise UpdateError("官网更新包大小无效")
    fallbacks = update.get("fallbackUrls", [])
    if not isinstance(fallbacks, list) or len(fallbacks) > 3:
        raise UpdateError("官网备用下载地址无效")
    fallbacks = [_https_url(item) for item in fallbacks]
    for field in ("name", "notes"):
        if field in manifest and not isinstance(manifest[field], str):
            raise UpdateError("官网更新说明格式错误")
    no_update = _no_update(version, current_version)
    if no_update is not None:
        return {**no_update, "manual_download_url": manual_url}
    return {
        "ok": True, "update_available": True, "version": version,
        "name": manifest.get("name") or f"SubFix v{version}", "notes": manifest.get("notes", ""),
        "zip_url": url, "sha256_url": "", "sha256": checksum, "size_bytes": size,
        "fallback_urls": fallbacks, "manual_download_url": manual_url,
    }


def fetch_latest_release(timeout_seconds: float = 5) -> dict[str, Any]:
    try:
        return _read_json_url(RELEASE_API_URL, timeout_seconds)
    except HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("GitHub 尚未发布首个稳定版本，请联系发布者") from exc
        raise UpdateError(f"无法读取 GitHub Release: HTTP {exc.code}") from exc
    except (URLError, OSError, HTTPException, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError(f"无法连接 GitHub Release: {exc}") from exc


def check_for_update(current_version: str, timeout_seconds: float = 10) -> dict[str, Any]:
    parse_version(current_version)
    deadline = time.monotonic() + timeout_seconds
    for source in ("website", "github"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        budget = min(4.0 if source == "website" else 5.0, remaining)
        try:
            with _time_limit(budget):
                if source == "website":
                    return manifest_to_update_info(_read_json_url(WEBSITE_API_URL, budget), current_version)
                result = release_to_update_info(fetch_latest_release(budget), current_version)
                if result.get("ok") or result.get("update_available") is False:
                    return result
        except (UpdateError, URLError, OSError, HTTPException, ValueError, UnicodeDecodeError):
            continue
    raise UpdateError("官网和备用更新源暂时无法连接，请稍后重试或前往官网下载")


def download_to(url: str, destination: Path, timeout_seconds: int = 60, *,
                progress: Callable[[int, int | None], None] | None = None,
                max_bytes: int = MAX_UPDATE_BYTES) -> None:
    request = Request(_https_url(url), headers={"User-Agent": "SubFix-Updater"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response, destination.open("wb") as output:
            _https_url(response.geturl())
            try:
                total = int(response.headers.get("Content-Length", ""))
            except (TypeError, ValueError):
                total = 0
            total = total if total > 0 else None
            if total is not None and total > max_bytes:
                raise UpdateError("更新资源大小超过限制")
            downloaded = 0
            if progress:
                progress(downloaded, total)
            while chunk := response.read(256 * 1024):
                if downloaded + len(chunk) > max_bytes:
                    raise UpdateError("更新资源大小超过限制")
                output.write(chunk)
                downloaded += len(chunk)
                if progress:
                    progress(downloaded, total)
            if total is not None and downloaded != total:
                raise UpdateError("更新包下载不完整，请重试")
    except (URLError, OSError, HTTPException) as exc:
        raise UpdateError(f"下载更新失败: {exc}") from exc


def read_sha256_file(path: Path) -> str:
    tokens = path.read_text(encoding="utf-8").split(maxsplit=1) if path.exists() else []
    return _sha256(tokens[0] if tokens else "")


def _cache_file(path: Path, mode: str):
    flags = os.O_NOFOLLOW | os.O_NONBLOCK
    if mode == "rb":
        flags |= os.O_RDONLY
    else:
        flags |= os.O_WRONLY | os.O_CREAT | (os.O_APPEND if mode == "ab" else os.O_TRUNC)
    if path.is_symlink():
        raise UpdateError("拒绝使用符号链接更新缓存")
    fd = os.open(path, flags, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise UpdateError("更新缓存必须是普通文件")
    return os.fdopen(fd, mode, buffering=0)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with _cache_file(path, "rb") as source:
        while chunk := source.read(256 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def _cache_lock(root: Path, key: str):
    root = root.absolute()
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise UpdateError("拒绝使用符号链接更新缓存目录")
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / (key + ".lock")
    with _cache_file(lock_path, "ab") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise UpdateError("已有更新下载正在进行，请稍后重试") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _verified_archive(path: Path, checksum: str, version: str, size: int | None) -> bool:
    if path.is_symlink():
        raise UpdateError("拒绝使用符号链接更新缓存")
    if not path.exists():
        return False
    if size is not None and path.stat().st_size != size:
        return False
    if path.stat().st_size > MAX_UPDATE_BYTES or _file_sha256(path) != checksum:
        return False
    with zipfile.ZipFile(path) as archive:
        _safe_update_files(archive, version)
    return True


def _download_part(url: str, path: Path, size: int | None,
                   progress: Callable[[int, int | None], None] | None) -> None:
    for attempt in range(2):
        if path.is_symlink():
            raise UpdateError("拒绝使用符号链接更新缓存")
        offset = path.stat().st_size if path.exists() else 0
        if offset > (size or MAX_UPDATE_BYTES):
            path.unlink()
            offset = 0
        headers = {"User-Agent": "SubFix-Updater", "Accept-Encoding": "identity"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        try:
            response = urlopen(Request(_https_url(url), headers=headers), timeout=15)
        except HTTPError as exc:
            if exc.code == 416 and offset and attempt == 0:
                path.unlink()
                continue
            raise
        with response:
            _https_url(response.geturl())
            status = getattr(response, "status", 200)
            length_text = response.headers.get("Content-Length")
            length = None
            if length_text is not None:
                try:
                    length = int(length_text)
                except (TypeError, ValueError) as exc:
                    raise UpdateError("下载服务器返回了无效大小") from exc
                if not 0 < length <= MAX_UPDATE_BYTES:
                    raise UpdateError("更新包大小超过限制或无效")
            if status == 206:
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                if not match:
                    raise UpdateError("下载服务器返回了无效续传范围")
                start, end, total = map(int, match.groups())
                if start != offset or not start <= end < total <= MAX_UPDATE_BYTES or (size is not None and total != size) or (length is not None and length != end - start + 1):
                    raise UpdateError("下载服务器返回的续传范围不匹配")
            elif status == 200:
                offset = 0  # A server without Range support must replace, never append.
                if size is not None and length is not None and size != length:
                    raise UpdateError("下载服务器返回的更新包大小不匹配")
                total = size or length
            else:
                raise UpdateError(f"下载服务器返回 HTTP {status}")
            received = offset
            if progress:
                progress(received, total)
            with _cache_file(path, "ab" if offset else "wb") as output:
                while chunk := response.read(256 * 1024):
                    if received + len(chunk) > (total or MAX_UPDATE_BYTES):
                        raise UpdateError("更新包下载内容超过声明大小")
                    output.write(chunk)
                    received += len(chunk)
                    if progress:
                        progress(received, total)
            if total is not None and received != total:
                raise UpdateError("更新包下载不完整，已保留进度，请重试")
            return


def download_update(*, zip_url: str, version: str, sha256: str | None = None,
                    sha256_url: str | None = None, size_bytes: int | None = None,
                    fallback_urls: list[str] | None = None, cache_root: Path | None = None,
                    progress: Callable[[int, int | None], None] | None = None) -> dict[str, Any]:
    version = normalized_version(version)
    urls = list(dict.fromkeys([_https_url(zip_url), *[_https_url(url) for url in (fallback_urls or [])]]))
    if len(urls) > 4:
        raise UpdateError("备用下载地址过多")
    if size_bytes is not None and (type(size_bytes) is not int or not 0 < size_bytes <= MAX_UPDATE_BYTES):
        raise UpdateError("更新包大小无效")
    if sha256 is None:
        if not sha256_url:
            raise UpdateError("缺少更新包 SHA-256 校验信息")
        with tempfile.TemporaryDirectory(prefix="subfix_update_checksum_") as temp_dir:
            path = Path(temp_dir) / "update.sha256"
            with _time_limit(10):
                download_to(sha256_url, path, timeout_seconds=5, max_bytes=4096)
            sha256 = read_sha256_file(path)
    checksum = _sha256(sha256)
    root = cache_root if cache_root is not None else UPDATE_CACHE_ROOT
    key = f"{version}-{checksum}"
    ready, part = root / (key + ".zip"), root / (key + ".zip.part")
    result = {"ok": True, "version": version, "archive_path": str(ready.absolute()), "sha256": checksum}
    with _cache_lock(root, key):
        if _verified_archive(ready, checksum, version, size_bytes):
            return result
        if ready.exists():
            ready.unlink()
        if _verified_archive(part, checksum, version, size_bytes):
            part.replace(ready)
            return result
        deadline = time.monotonic() + 300
        failure = "下载失败"
        for url in urls:
            try:
                with _time_limit(deadline - time.monotonic()):
                    _download_part(url, part, size_bytes, progress)
                if not _verified_archive(part, checksum, version, size_bytes):
                    part.unlink(missing_ok=True)
                    raise UpdateError("SHA-256 校验失败，未安装更新")
                part.replace(ready)
                return result
            except (UpdateError, URLError, OSError, HTTPException, zipfile.BadZipFile) as exc:
                failure = str(exc)
        raise UpdateError(f"{failure}；可重试续传或前往官网下载")


def _safe_update_files(archive: zipfile.ZipFile, version: str) -> list[str]:
    try:
        manifest = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("更新包缺少有效 manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise UpdateError("更新包 manifest 版本不支持")
    if normalized_version(str(manifest.get("version") or "")) != normalized_version(version):
        raise UpdateError("更新包版本与 Release 不一致")
    files = manifest.get("files")
    if not isinstance(files, list) or not files or any(not isinstance(item, str) for item in files):
        raise UpdateError("更新包 manifest 文件列表错误")
    normalized_files = [str(PurePosixPath(item)) for item in files]
    if len(normalized_files) != len(set(normalized_files)) or any(
        item.startswith("/") or ".." in PurePosixPath(item).parts or item not in UPDATE_FILE_PATHS
        for item in normalized_files
    ):
        raise UpdateError("更新包包含未授权文件")
    archive_files = {info.filename for info in archive.infolist() if not info.is_dir()}
    if archive_files != {MANIFEST_NAME, *normalized_files}:
        raise UpdateError("更新包内容与 manifest 不一致")
    return normalized_files


def _safe_destination(target_root: Path, relative_path: str) -> Path:
    target_root.mkdir(parents=True, exist_ok=True)
    parent = target_root
    if parent.is_symlink():
        raise UpdateError("拒绝写入符号链接目录")
    # 仅允许在用户 Resolve 脚本目录的真实子目录中替换白名单文件。
    for part in PurePosixPath(relative_path).parts[:-1]:
        parent = parent / part
        if parent.is_symlink():
            raise UpdateError(f"拒绝写入符号链接目录: {relative_path}")
        parent.mkdir(exist_ok=True)
    return parent / PurePosixPath(relative_path).name


def install_archive(archive_path: Path, expected_sha256: str, version: str, target_root: Path = USER_UTILITY_ROOT) -> None:
    actual_sha256 = _file_sha256(archive_path)
    if actual_sha256.lower() != str(expected_sha256).lower():
        raise UpdateError("SHA-256 校验失败，未安装更新")

    with zipfile.ZipFile(archive_path) as archive:
        files = _safe_update_files(archive, version)
        with tempfile.TemporaryDirectory(prefix="subfix_update_stage_") as temporary_dir:
            stage_root = Path(temporary_dir)
            for relative_path in files:
                staged_path = stage_root / relative_path
                staged_path.parent.mkdir(parents=True, exist_ok=True)
                staged_path.write_bytes(archive.read(relative_path))

            backups: dict[Path, bytes | None] = {}
            replaced: list[Path] = []
            try:
                for relative_path in files:
                    destination = _safe_destination(target_root, relative_path)
                    if destination.is_symlink():
                        raise UpdateError(f"拒绝覆盖符号链接: {relative_path}")
                    backups[destination] = destination.read_bytes() if destination.exists() else None
                    replacement = destination.with_name(destination.name + ".subfix-new")
                    shutil.copy2(stage_root / relative_path, replacement)
                    os.replace(replacement, destination)
                    if relative_path == ".subfix_support/bin/ffmpeg":
                        destination.chmod(0o755)
                    replaced.append(destination)
            except Exception as exc:
                for destination in reversed(replaced):
                    original = backups[destination]
                    if original is None:
                        destination.unlink(missing_ok=True)
                    else:
                        destination.write_bytes(original)
                raise UpdateError(f"安装更新失败，已回滚: {exc}") from exc


def write_output(path: Path | None, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if path is None:
        print(text)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")


def write_progress(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    temporary = path.with_name(path.name + ".tmp")
    try:
        write_output(temporary, payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SubFix 官网更新器")
    subparsers = parser.add_subparsers(dest="action", required=True)
    check = subparsers.add_parser("check")
    check.add_argument("--current-version", required=True)
    check.add_argument("--output", type=Path)
    for action in ("download", "install"):
        command = subparsers.add_parser(action)
        command.add_argument("--zip-url", required=True)
        command.add_argument("--sha256-url")
        command.add_argument("--sha256")
        command.add_argument("--size-bytes", type=int)
        command.add_argument("--fallback-url", action="append", default=[])
        command.add_argument("--version", required=True)
        command.add_argument("--target-root", type=Path, default=USER_UTILITY_ROOT)
        command.add_argument("--output", type=Path)
        command.add_argument("--progress", type=Path)
    local = subparsers.add_parser("install-local")
    local.add_argument("--archive", required=True, type=Path)
    local.add_argument("--sha256", required=True)
    local.add_argument("--version", required=True)
    local.add_argument("--target-root", type=Path, default=USER_UTILITY_ROOT)
    local.add_argument("--output", type=Path)
    local.add_argument("--progress", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "check":
            payload = check_for_update(args.current_version)
            write_output(args.output, payload)
            return 0 if payload.get("ok") else 1
        if args.action in ("download", "install"):
            def report_download(downloaded: int, total: int | None) -> None:
                amount = f"{downloaded / (1024 * 1024):.2f} MB"
                if total:
                    amount += f" / {total / (1024 * 1024):.2f} MB"
                write_progress(args.progress, {
                    "stage": "下载更新包", "message": f"正在下载更新包：{amount}",
                    "download_progress": True, "progress_index": downloaded,
                    "progress_total": total, "indeterminate": total is None,
                })

            write_progress(args.progress, {"stage": "连接下载服务器", "indeterminate": True})
            payload = download_update(zip_url=args.zip_url, version=args.version,
                sha256=args.sha256, sha256_url=args.sha256_url, size_bytes=args.size_bytes,
                fallback_urls=args.fallback_url, progress=report_download)
            if args.action == "download":
                write_progress(args.progress, {"stage": "下载并校验完成", "progress_index": 100, "progress_total": 100})
                write_output(args.output, payload)
                return 0
            archive_path, checksum = Path(payload["archive_path"]), payload["sha256"]
        else:
            archive_path, checksum = args.archive, _sha256(args.sha256)
        write_progress(args.progress, {"stage": "校验并安装更新", "indeterminate": True})
        install_archive(archive_path, checksum, args.version, args.target_root)
        write_progress(args.progress, {"stage": "完成", "progress_index": 100, "progress_total": 100})
        write_output(args.output, {"ok": True, "version": normalized_version(args.version), "restart_required": True})
        return 0
    except (UpdateError, OSError, HTTPException, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        if getattr(args, "progress", None):
            try:
                write_progress(args.progress, {"stage": "失败", "message": str(exc)})
            except OSError:
                pass
        write_output(getattr(args, "output", None), {"ok": False, "error": str(exc), "manual_download_url": WEBSITE_URL})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
