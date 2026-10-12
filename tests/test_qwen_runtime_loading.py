"""Exercise relocation with a broken legacy RPATH and transitive dylibs."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from test_qwen_memory_safety import load_helper_module


@pytest.mark.parametrize("returncode", [-6, 1, 134])
def test_loader_failure_has_installation_guidance(returncode):
    helper = load_helper_module()
    log = "dyld[123]: Library not loaded: @rpath/libqwen3-asr.dylib\nReason: tried: missing build directory"
    with pytest.raises(helper.LocalQwenSafetyError) as error:
        helper.raise_for_local_qwen_returncode(returncode, "gguf", log)
    assert "运行库" in str(error.value)
    assert "完整安装包" in str(error.value)
    assert "请缩小选区" not in str(error.value)
    assert log in str(error.value)


def test_gguf_launch_uses_its_own_library_directory(monkeypatch, tmp_path):
    helper = load_helper_module()
    monkeypatch.setattr(helper.sys, "platform", "darwin")
    binary = tmp_path / "插件 bin" / "qwen3-asr-cli"
    monkeypatch.setenv("DYLD_LIBRARY_PATH", "/unrelated/libraries")
    monkeypatch.setattr(helper, "resolve_qwen3_cpp_paths", lambda: (binary, tmp_path / "model.gguf"))

    def launch(cmd, stage, **kwargs):
        assert stage == "gguf"
        assert kwargs["env"]["DYLD_LIBRARY_PATH"] == str(binary.parent.resolve())
        assert os.environ["DYLD_LIBRARY_PATH"] == "/unrelated/libraries"
        Path(cmd[cmd.index("-o") + 1]).write_text('{"words":[{"text":"好","start":0.1,"end":0.3}]}')
        return 0, ""

    monkeypatch.setattr(helper, "run_guarded_local_qwen_command", launch)
    words = helper.qwen3_cpp_force_align_items(tmp_path / "audio.wav", "好", "Chinese", tmp_path / "work")
    assert words[0]["word"] == "好"


def test_other_platform_keeps_existing_environment(monkeypatch, tmp_path):
    helper = load_helper_module()
    monkeypatch.setattr(helper.sys, "platform", "linux")
    monkeypatch.setenv("DYLD_LIBRARY_PATH", "/existing/libraries")
    assert helper.qwen3_cpp_runtime_env(tmp_path / "qwen3-asr-cli") == dict(os.environ)


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("clang"), reason="Mach-O compiler required")
def test_relocated_legacy_binary_loads_transitive_libraries_without_build_directory(tmp_path, monkeypatch):
    helper = load_helper_module()
    original = tmp_path / "build"
    original.mkdir()
    (original / "leaf.c").write_text("int leaf(void) { return 42; }\n")
    (original / "qwen.c").write_text("extern int leaf(void); int qwen(void) { return leaf(); }\n")
    (original / "main.c").write_text('#include <stdio.h>\nextern int qwen(void); int main(void) { printf("%d\\n", qwen()); return 0; }\n')
    commands = [
        ["clang", "-dynamiclib", "leaf.c", "-o", "libleaf.dylib", "-Wl,-install_name,@rpath/libleaf.dylib"],
        ["clang", "-dynamiclib", "qwen.c", "-L.", "-lleaf", "-o", "libqwen3-asr.dylib", "-Wl,-install_name,@rpath/libqwen3-asr.dylib"],
        ["clang", "main.c", "-L.", "-lqwen3-asr", "-o", "qwen3-asr-cli", "-Wl,-rpath," + str(original)],
    ]
    for command in commands:
        subprocess.run(command, cwd=original, check=True, capture_output=True)
    relocated = tmp_path / "another user 插件" / "bin"
    relocated.parent.mkdir()
    shutil.move(str(original), relocated)
    assert not original.exists()
    monkeypatch.delenv("DYLD_LIBRARY_PATH", raising=False)
    monkeypatch.delenv("DYLD_FALLBACK_LIBRARY_PATH", raising=False)
    binary = relocated / "qwen3-asr-cli"
    broken = subprocess.run([str(binary)], cwd=tmp_path, capture_output=True, text=True)
    assert broken.returncode != 0
    assert "Library not loaded: @rpath/libqwen3-asr.dylib" in broken.stderr
    fixed = subprocess.run([str(binary)], cwd=tmp_path, env=helper.qwen3_cpp_runtime_env(binary), capture_output=True, text=True)
    assert fixed.returncode == 0, fixed.stderr
    assert fixed.stdout.strip() == "42"
    assert "DYLD_LIBRARY_PATH" not in os.environ
