import hashlib
import io
import json
from pathlib import Path
import select
import signal
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".subfix_support"))
import subfix_update as updater


def manifest(version="3.4.1"):
    return {
        "schemaVersion": 1, "channel": "stable", "version": version,
        "platform": "macos", "arch": "arm64", "publishedAt": "2026-11-09T12:00:00Z",
        "name": "SubFix " + version, "notes": "更新说明",
        "releaseUrl": "https://github.com/HooperH/SubFix/releases/tag/v" + version,
        "downloadUrl": "https://downloads.example.invalid/full.zip", "sizeBytes": 30000000,
        "update": {"url": "https://downloads.example.invalid/update.zip", "sha256": "a" * 64,
                   "sizeBytes": 1234, "fallbackUrls": ["https://github.com/HooperH/SubFix/update.zip"]},
    }


class Response(io.BytesIO):
    def __init__(self, body, status=200, headers=None, url="https://downloads.example.invalid/update.zip"):
        super().__init__(body)
        self.status, self.headers, self.url = status, headers or {}, url

    def geturl(self):
        return self.url


def archive_bytes(version="3.4.1"):
    body = io.BytesIO()
    with zipfile.ZipFile(body, "w") as archive:
        archive.writestr("subfix-update-manifest.json", json.dumps({
            "schema_version": 1, "version": version, "files": ["SubFix/SubFix.lua"]}))
        archive.writestr("SubFix/SubFix.lua", "-- verified update")
    return body.getvalue()


def download_args(body, **kwargs):
    return dict(zip_url="https://downloads.example.invalid/update.zip", version="3.4.1",
                sha256=hashlib.sha256(body).hexdigest(), size_bytes=len(body), **kwargs)


def test_website_manifest_drives_update_and_manual_download():
    result = updater.manifest_to_update_info(manifest(), "3.4.0")
    assert result["ok"] and result["sha256"] == "a" * 64
    assert result["size_bytes"] == 1234
    assert result["fallback_urls"] == manifest()["update"]["fallbackUrls"]
    assert result["manual_download_url"] == manifest()["downloadUrl"]


@pytest.mark.parametrize("key,value", [
    ("schemaVersion", True), ("channel", "beta"), ("version", "../3.4.1"),
    ("platform", "windows"), ("arch", "x86"), ("publishedAt", "tomorrow"),
    ("downloadUrl", "http://example.invalid/full.zip"), ("sizeBytes", True),
])
def test_website_manifest_rejects_bad_fields(key, value):
    data = manifest(); data[key] = value
    with pytest.raises(updater.UpdateError):
        updater.manifest_to_update_info(data, "3.4.0")


@pytest.mark.parametrize("key,value", [
    ("url", "https://user:password@example.invalid/u.zip"), ("sha256", "bad"),
    ("sizeBytes", 0), ("fallbackUrls", ["file:///tmp/update.zip"]),
])
def test_website_manifest_rejects_bad_update_fields(key, value):
    data = manifest(); data["update"][key] = value
    with pytest.raises(updater.UpdateError):
        updater.manifest_to_update_info(data, "3.4.0")


def test_valid_website_response_never_requires_github(monkeypatch):
    calls = []
    def fetch(request, **kwargs):
        calls.append(request.full_url)
        return Response(json.dumps(manifest()).encode(), url=request.full_url)
    monkeypatch.setattr(updater, "urlopen", fetch)
    assert updater.check_for_update("3.4.0")["ok"]
    assert calls == [updater.WEBSITE_API_URL]


def test_failed_website_falls_back_to_github_without_false_no_update(monkeypatch):
    calls = []
    def fetch(request, **kwargs):
        calls.append(request.full_url)
        raise URLError("offline")
    monkeypatch.setattr(updater, "urlopen", fetch)
    with pytest.raises(updater.UpdateError, match="官网"):
        updater.check_for_update("3.4.0")
    assert calls == [updater.WEBSITE_API_URL, updater.RELEASE_API_URL]


def test_malformed_website_uses_valid_github_fallback(monkeypatch):
    github = {"tag_name": "v3.4.1", "assets": [
        {"name": "SubFix-update-v3.4.1." + suffix,
         "browser_download_url": "https://github.com/HooperH/SubFix/update." + suffix}
        for suffix in ("zip", "sha256")]}
    def fetch(request, **kwargs):
        value = {"error": "unavailable"} if request.full_url == updater.WEBSITE_API_URL else github
        return Response(json.dumps(value).encode(), url=request.full_url)
    monkeypatch.setattr(updater, "urlopen", fetch)
    result = updater.check_for_update("3.4.0")
    assert result["ok"] and result["version"] == "3.4.1"
    assert result["zip_url"].startswith("https://github.com/")


@pytest.mark.parametrize("version", ["3.3.9", "3.4.0"])
def test_website_no_update_is_not_a_network_error(monkeypatch, version):
    monkeypatch.setattr(updater, "urlopen", lambda request, **_: Response(
        json.dumps(manifest(version)).encode(), url=request.full_url))
    result = updater.check_for_update("3.4.0")
    assert not result["ok"] and result["update_available"] is False
    assert result["version"] == version


def test_whole_check_timeout_covers_stalled_response_body(monkeypatch):
    class Slow(Response):
        def read(self, *_):
            time.sleep(5)
    monkeypatch.setattr(updater, "urlopen", lambda *_a, **_k: Slow(b""))
    previous = signal.getsignal(signal.SIGALRM)
    start = time.monotonic()
    with pytest.raises(updater.UpdateError):
        updater.check_for_update("3.4.0", timeout_seconds=0.1)
    assert time.monotonic() - start < 0.8
    assert signal.getsignal(signal.SIGALRM) == previous
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_download_only_then_local_install(tmp_path, monkeypatch):
    body = archive_bytes(); digest = hashlib.sha256(body).hexdigest()
    monkeypatch.setattr(updater, "UPDATE_CACHE_ROOT", tmp_path / "cache")
    monkeypatch.setattr(updater, "urlopen", lambda *_a, **_k: Response(body, headers={"Content-Length": str(len(body))}))
    output = tmp_path / "download.json"
    target = tmp_path / "Utility"
    assert updater.main(["download", "--zip-url", "https://example.invalid/update.zip", "--sha256", digest,
                         "--size-bytes", str(len(body)), "--version", "3.4.1", "--output", str(output)]) == 0
    result = json.loads(output.read_text())
    assert not target.exists()
    assert result["sha256"] == digest and Path(result["archive_path"]).read_bytes() == body
    assert updater.main(["install-local", "--archive", result["archive_path"], "--sha256", digest,
                         "--version", "3.4.1", "--target-root", str(target)]) == 0
    assert (target / "SubFix/SubFix.lua").read_text() == "-- verified update"


def test_interrupted_download_resumes_without_replacing_installed_files(tmp_path, monkeypatch):
    body = archive_bytes(); half = len(body) // 2
    class Interrupted(Response):
        def read(self, *_):
            if self.tell():
                raise OSError("connection lost")
            return super().read(half)
    monkeypatch.setattr(updater, "urlopen", lambda *_a, **_k: Interrupted(body, headers={"Content-Length": str(len(body))}))
    args = download_args(body, cache_root=tmp_path / "cache")
    with pytest.raises(updater.UpdateError):
        updater.download_update(**args)
    part = next((tmp_path / "cache").glob("*.part"))
    assert part.read_bytes() == body[:half]
    def resumed(request, **kwargs):
        assert request.get_header("Range") == f"bytes={half}-"
        return Response(body[half:], 206, {"Content-Range": f"bytes {half}-{len(body)-1}/{len(body)}",
                                          "Content-Length": str(len(body)-half)})
    monkeypatch.setattr(updater, "urlopen", resumed)
    result = updater.download_update(**args)
    assert Path(result["archive_path"]).read_bytes() == body and not part.exists()


def seed_partial(cache, body):
    cache.mkdir()
    digest = hashlib.sha256(body).hexdigest()
    path = cache / f"3.4.1-{digest}.zip.part"
    path.write_bytes(body[:50])
    return path


def test_server_ignoring_range_restarts_instead_of_appending(tmp_path, monkeypatch):
    body = archive_bytes(); cache = tmp_path / "cache"
    seed_partial(cache, body)
    monkeypatch.setattr(updater, "urlopen", lambda *_a, **_k: Response(body, headers={"Content-Length": str(len(body))}))
    result = updater.download_update(**download_args(body, cache_root=cache))
    assert Path(result["archive_path"]).read_bytes() == body


def test_range_not_satisfiable_restarts_once(tmp_path, monkeypatch):
    body = archive_bytes(); cache = tmp_path / "cache"; calls = []
    seed_partial(cache, body)
    def response(request, **kwargs):
        calls.append(request.get_header("Range"))
        if len(calls) == 1:
            raise HTTPError(request.full_url, 416, "Range not satisfiable", {}, None)
        return Response(body, headers={"Content-Length": str(len(body))})
    monkeypatch.setattr(updater, "urlopen", response)
    result = updater.download_update(**download_args(body, cache_root=cache))
    assert calls == ["bytes=50-", None]
    assert Path(result["archive_path"]).read_bytes() == body


def test_invalid_content_range_keeps_existing_part(tmp_path, monkeypatch):
    body = archive_bytes(); cache = tmp_path / "cache"
    part = seed_partial(cache, body)
    monkeypatch.setattr(updater, "urlopen", lambda *_a, **_k: Response(body[50:], 206,
        {"Content-Range": f"bytes 0-{len(body)-51}/{len(body)}"}))
    with pytest.raises(updater.UpdateError):
        updater.download_update(**download_args(body, cache_root=cache))
    assert part.read_bytes() == body[:50]


def test_bad_primary_checksum_retries_fallback_from_zero(tmp_path, monkeypatch):
    body = archive_bytes(); calls = []
    def response(request, **kwargs):
        calls.append(request.full_url)
        assert request.get_header("Range") is None
        data = b"x" * len(body) if len(calls) == 1 else body
        return Response(data, headers={"Content-Length": str(len(data))})
    monkeypatch.setattr(updater, "urlopen", response)
    result = updater.download_update(**download_args(body, cache_root=tmp_path / "cache",
        fallback_urls=["https://backup.example.invalid/update.zip"]))
    assert len(calls) == 2 and Path(result["archive_path"]).read_bytes() == body


def test_cached_verified_archive_needs_no_network(tmp_path, monkeypatch):
    body = archive_bytes(); cache = tmp_path / "cache"
    part = seed_partial(cache, body); part.write_bytes(body)
    def offline(*args, **kwargs):
        pytest.fail("complete verified cache should not access network")
    monkeypatch.setattr(updater, "urlopen", offline)
    result = updater.download_update(**download_args(body, cache_root=cache))
    assert Path(result["archive_path"]).read_bytes() == body


def test_cache_symlinks_are_not_followed(tmp_path, monkeypatch):
    body = archive_bytes(); cache = tmp_path / "cache"; cache.mkdir()
    outside = tmp_path / "keep"; outside.write_bytes(b"do not overwrite")
    digest = hashlib.sha256(body).hexdigest()
    (cache / f"3.4.1-{digest}.zip.part").symlink_to(outside)
    with pytest.raises(updater.UpdateError, match="符号链接"):
        updater.download_update(**download_args(body, cache_root=cache))
    assert outside.read_bytes() == b"do not overwrite"


def test_concurrent_download_is_rejected_without_waiting(tmp_path):
    body = archive_bytes(); cache = tmp_path / "cache"
    key = "3.4.1-" + hashlib.sha256(body).hexdigest()
    with updater._cache_lock(cache, key):
        with pytest.raises(updater.UpdateError, match="已有更新下载"):
            updater.download_update(**download_args(body, cache_root=cache))


def test_terminated_download_leaves_resumable_bytes_and_unlocks_cache(tmp_path, monkeypatch):
    body = archive_bytes(); cache = tmp_path / "cache"
    script = '''
import io, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import subfix_update as u
body = bytes.fromhex(sys.argv[3])
class Response(io.BytesIO):
    headers = {"Content-Length": str(len(body))}
    status = 200
    def geturl(self): return "https://example.invalid/update.zip"
    def read(self, _): return super().read(50)
u.urlopen = lambda *a, **k: Response(body)
def progress(count, total):
    if count:
        print("partial", flush=True)
        time.sleep(30)
u.download_update(zip_url="https://example.invalid/update.zip", version="3.4.1",
    sha256=u.hashlib.sha256(body).hexdigest(), size_bytes=len(body),
    cache_root=Path(sys.argv[2]), progress=progress)
'''
    process = subprocess.Popen([sys.executable, "-c", script, str(Path(updater.__file__).parent),
                                str(cache), body.hex()], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        readable, _, _ = select.select([process.stdout], [], [], 5)
        assert readable and process.stdout.readline() == b"partial\n"
    finally:
        process.terminate()
        process.communicate(timeout=5)
    assert next(cache.glob("*.part")).read_bytes() == body[:50]
    monkeypatch.setattr(updater, "urlopen", lambda *_a, **_k: Response(body[50:], 206,
        {"Content-Range": f"bytes 50-{len(body)-1}/{len(body)}", "Content-Length": str(len(body)-50)}))
    result = updater.download_update(**download_args(body, cache_root=cache))
    assert Path(result["archive_path"]).read_bytes() == body


def test_install_local_refuses_hash_mismatch_before_mutation(tmp_path):
    archive = tmp_path / "update.zip"; archive.write_bytes(archive_bytes())
    target = tmp_path / "Utility"
    assert updater.main(["install-local", "--archive", str(archive), "--sha256", "0"*64,
        "--version", "3.4.1", "--target-root", str(target)]) == 1
    assert not target.exists()
