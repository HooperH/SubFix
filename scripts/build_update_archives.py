#!/usr/bin/env python3
"""Build a v3.1.0-compatible update ZIP and a full ZIP for newer updaters."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".subfix_support"))
import subfix_update


# Frozen v3.1.0 contract: expanding this list cannot update already-installed validators.
LEGACY_FILE_PATHS = frozenset({
    "SubFix/SubFix.lua", "SubFix/生成选区字幕.lua",
    ".subfix_support/subfix_generate_selection_core.lua",
    ".subfix_support/subfix_update.py", ".subfix_support/subfix_asr_transcribe.py",
    ".subfix_support/subfix_generate_v4.py", ".subfix_support/subfix_generate_v5.py",
    ".subfix_support/subfix_generate_textnorm.py", ".subfix_support/subfix_qwen_local_manager.py",
    ".subfix_support/setup_asr_env.sh", ".subfix_support/segmentation_profile.json",
    ".subfix_support/segmentation_profile_v3.json", ".subfix_support/segmentation_profile_v4.json",
})
FULL_ONLY_FILE_PATHS = frozenset({
    ".subfix_support/subfix_process_group.py",
    ".subfix_support/subfix_model_manager.py", ".subfix_support/subfix_model_center.lua",
    ".subfix_support/model-downloads.json",
    ".subfix_support/bin/ffmpeg", ".subfix_support/licenses/FFmpeg-LGPL-2.1.txt",
})


def build_archives(payload: Path, version: str, output: Path) -> list[Path]:
    version = subfix_update.normalized_version(version)
    if not payload.is_dir() or payload.is_symlink():
        raise ValueError("payload must be a real directory, not a symlink")
    entries = sorted(payload.rglob("*"))
    for path in entries:
        if path.is_symlink():
            raise ValueError(f"symlink payload entry: {path.relative_to(payload)}")
    files = {path.relative_to(payload).as_posix() for path in entries if path.is_file()}
    unexpected = files - (LEGACY_FILE_PATHS | FULL_ONLY_FILE_PATHS)
    if unexpected:
        raise ValueError(f"files outside update compatibility contract: {sorted(unexpected)}")
    missing = LEGACY_FILE_PATHS - files
    if missing:
        raise ValueError(f"missing legacy update files: {sorted(missing)}")
    source = (payload / "SubFix/SubFix.lua").read_text(encoding="utf-8")
    match = re.search(r'^SUBFIX_VERSION\s*=\s*"([^"]+)"', source, re.M)
    if not match or match.group(1) != version:
        raise ValueError("SubFix.lua version does not match archive version")

    assets = [output / f"SubFix-update-v{version}{suffix}{extension}"
              for suffix in ("", "-full") for extension in (".zip", ".sha256")]
    if any(path.exists() for path in assets):
        raise ValueError("refusing to overwrite existing update assets")
    output.mkdir(parents=True, exist_ok=True)
    for archive_path, selected in ((assets[0], LEGACY_FILE_PATHS), (assets[2], files)):
        manifest = {"schema_version": 1, "version": version, "files": sorted(selected)}
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(subfix_update.MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False))
            for name in sorted(selected):
                archive.write(payload / name, name)
        with zipfile.ZipFile(archive_path) as archive:
            subfix_update._safe_update_files(archive, version)
        digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        archive_path.with_suffix(".sha256").write_text(f"{digest}  {archive_path.name}\n", encoding="utf-8")
    return assets


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("payload", type=Path)
    parser.add_argument("version")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        assets = build_archives(args.payload, args.version, args.output)
    except (OSError, ValueError, subfix_update.UpdateError) as exc:
        parser.exit(1, f"update archive build failed: {exc}\n")
    for asset in assets:
        print(asset)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
