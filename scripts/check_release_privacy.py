"""Reject private development data in a staged release payload."""
import json
import mmap
from pathlib import Path
import sys

PRIVATE_FIELDS = {"examples", "diagnostic", "project_name", "timeline_name", "timeline_id", "source_path", "source_text", "manual_text", "manual_chunks", "text_corrections"}


def check_payload(root):
    root = Path(root).resolve(strict=True)
    private_prefix = (str(Path.home()) + "/").encode()

    def check_profile(value):
        if isinstance(value, dict):
            if PRIVATE_FIELDS.intersection(value):
                raise ValueError("发行配置包含原文或私人诊断字段")
            for item in value.values():
                check_profile(item)
        elif isinstance(value, list):
            for item in value:
                check_profile(item)

    for path in root.rglob("*"):
        if path.is_symlink() and not path.resolve().is_relative_to(root):
            raise ValueError("发行载荷包含指向外部的软链接")
        if not path.is_file():
            continue
        if path.suffix == ".pyc" or path.name in {"doubao_credentials.json", "subfix_generate_prefs.json"}:
            raise ValueError("发行载荷包含缓存或用户配置")
        if path.name.startswith("segmentation_profile") and path.suffix == ".json":
            check_profile(json.loads(path.read_text(encoding="utf-8")))
        if path.stat().st_size:
            with path.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
                if data.find(private_prefix) >= 0:
                    raise ValueError("发行载荷包含本机用户目录路径，请重新构建相关运行时")


if __name__ == "__main__":
    check_payload(sys.argv[1])
    print("发行载荷隐私检查通过")
