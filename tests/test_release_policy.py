from pathlib import Path
import subprocess


def test_340_incremental_release_is_rejected_before_publishing():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["bash", str(root / "scripts/release_update.sh"), "3.4.0",
         str(root / "docs/releases/v3.4.0.md")],
        cwd=root, capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 2
    assert "完整 PKG" in result.stderr
    assert "旧更新器" in result.stderr
