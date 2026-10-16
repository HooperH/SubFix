import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from test_generate_progress_lifecycle import run_lua


ROOT = Path(__file__).resolve().parents[1]


def test_test_package_builder_has_valid_shell_syntax():
    subprocess.run(["bash", "-n", str(ROOT / "build_subfix_test_package.sh")], check=True)


def test_test_package_builder_deploys_complete_bundle_to_logged_in_user():
    source = (ROOT / "build_subfix_test_package.sh").read_text(encoding="utf-8")

    assert "install_subfix_for_user.sh" in source
    assert '"$PKG_SCRIPTS_DIR/postinstall"' in source


def test_postinstall_script_preserves_user_data_while_deploying_runtime():
    postinstall = ROOT / "scripts" / "install_subfix_for_user.sh"

    assert postinstall.is_file(), "the package needs a postinstall deployment script"
    source = postinstall.read_text(encoding="utf-8")
    for marker in (
        "/dev/console",
        "runtime/python/bin/python3",
        '"$SYSTEM_UTILITY/SubFix"',
        '"$SYSTEM_SUPPORT"',
        '"$USER_UTILITY/SubFix"',
        '"$USER_SUPPORT"',
    ):
        assert marker in source
    assert 'rm -rf -- "$USER_UTILITY' not in source


def test_postinstall_refuses_to_follow_legacy_symlinks_as_root():
    source = (ROOT / "scripts" / "install_subfix_for_user.sh").read_text(encoding="utf-8")

    assert '"$USER_UTILITY/SubFix"' in source
    assert '"$USER_SUPPORT/runtime"' in source
    assert '"$USER_SUPPORT/bin"' in source
    assert '"$USER_SUPPORT/models"' in source
    assert "拒绝跟随旧版 SubFix 符号链接" in source


def test_postinstall_removes_system_staging_bundle_after_user_deployment():
    source = (ROOT / "scripts" / "install_subfix_for_user.sh").read_text(encoding="utf-8")

    assert 'rm -rf -- "$SYSTEM_SUPPORT"' in source
    assert 'rmdir "$SYSTEM_UTILITY/SubFix"' in source
    assert 'rm -rf -- "$SYSTEM_UTILITY/SubFix"' not in source
    assert source.index('ditto "$SYSTEM_SUPPORT" "$USER_SUPPORT"') < source.rindex("\ncleanup_system_subfix_menu\n")


def test_uninstaller_removes_the_matching_user_level_installation():
    source = (ROOT / "build_uninstaller.sh").read_text(encoding="utf-8")

    assert 'SOURCE="$SCRIPT_DIR/卸载_SubFix.command"' in source
    assert 'cp "$SOURCE" "$APP_PATH/Contents/Resources/uninstall.sh"' in source
    assert '--dry-run' in source
    assert '--needs-admin' in source


def test_test_package_builder_is_self_contained_and_arm64_only():
    source = (ROOT / "build_subfix_test_package.sh").read_text(encoding="utf-8")
    for marker in (
        "SubFix3.0测试版",
        "uname -m",
        "arm64",
        "fetch_python_runtime.sh",
        "stage_qwen3_cpp_runtime.sh",
        "qwen3-forced-aligner-0.6b-f16.gguf",
        "subfix_generate_textnorm.py",
        '"$SUPPORT_PAYLOAD/runtime/python"',
        "build_uninstaller.sh",
        "卸载_SubFix.app",
        "pkgbuild",
    ):
        assert marker in source
    assert "/Users/" not in source


def test_test_package_uses_an_ascii_pkg_filename_for_pkgbuild_compatibility():
    source = (ROOT / "build_subfix_test_package.sh").read_text(encoding="utf-8")

    assert 'PKG_PATH="$RELEASE_DIR/SubFix-v${VERSION}-macOS.pkg"' in source
    assert 'PKG_PATH="$RELEASE_DIR/$RELEASE_NAME.pkg"' not in source


def test_setup_uses_only_bundled_python_runtime():
    source = (ROOT / "setup_asr_env.sh").read_text(encoding="utf-8")
    assert 'runtime/python/bin/python3' in source
    assert 'command -v python3' not in source


def test_qwen_runtime_stage_checks_dependencies_without_matching_its_own_path():
    source = (ROOT / "scripts" / "stage_qwen3_cpp_runtime.sh").read_text(encoding="utf-8")
    assert 'otool -L "$binary" | tail -n +2' in source


def test_package_distributes_the_process_group_runner():
    build_source = (ROOT / "build_subfix_test_package.sh").read_text(encoding="utf-8")
    install_source = (ROOT / "scripts" / "install_subfix_for_user.sh").read_text(encoding="utf-8")
    sync_source = (ROOT / "sync_to_plugin.sh").read_text(encoding="utf-8")
    release_source = (ROOT / "scripts" / "release_update.sh").read_text(encoding="utf-8")

    assert "subfix_process_group.py" in build_source
    assert "subfix_process_group.py" in install_source
    assert "subfix_process_group.py" in sync_source
    assert "subfix_process_group.py" in release_source


def test_generate_core_uses_a_dedicated_process_group_for_cancellation():
    source = (ROOT / ".subfix_support" / "subfix_generate_selection_core.lua").read_text(encoding="utf-8")
    start = source.index("local function kill_background_process")
    end = source.index("local function run_asr_helper_with_progress", start)
    body = source[start:end]

    assert "paths.process_group" in source
    assert '"--stop"' in body
    assert 'kill -TERM -- -" .. tostring(pid)' in body
    assert 'kill -KILL -- -" .. tostring(pid)' in body


def test_legacy_update_inline_runner_creates_a_dedicated_process_group():
    source = (ROOT / ".subfix_support/subfix_generate_selection_core.lua").read_text(encoding="utf-8")
    inline = re.search(r"local inline_group_code = '([^'\n]+)'", source)
    assert inline, "legacy update payload needs a runner without adding a new file"
    probe = "import json,os; print(json.dumps([os.getpid(),os.getpgrp(),os.getsid(0)]))"
    command = shlex.join([sys.executable, "-c", probe])
    worker = subprocess.Popen([sys.executable, "-c", inline.group(1), command], stdout=subprocess.PIPE, text=True)
    try:
        output, _ = worker.communicate(timeout=5)
        assert worker.returncode == 0
        child_pid, group_id, session_id = json.loads(output)
        assert group_id == session_id == worker.pid
        assert child_pid > 0
        assert group_id != os.getpgrp()
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)


def macho_rpaths(binary: Path) -> list[str]:
    output = subprocess.check_output(["otool", "-l", str(binary)], text=True)
    lines = output.splitlines()
    rpaths = []
    for index, line in enumerate(lines):
        if line.strip() != "cmd LC_RPATH":
            continue
        for candidate in lines[index + 1 : index + 5]:
            fields = candidate.strip().split()
            if fields and fields[0] == "path":
                rpaths.append(fields[1])
                break
    return rpaths


def test_qwen_runtime_stage_repairs_absolute_rpaths_and_launches(tmp_path):
    source_build = ROOT / ".subfix_support" / "qwen3-asr.cpp" / "build"
    destination = tmp_path / "qwen-bin"
    if sys.platform != "darwin" or not (source_build / "qwen3-asr-cli").is_file():
        pytest.skip("requires a local macOS Qwen native build; release archive checks run separately")

    subprocess.run(
        [str(ROOT / "scripts" / "stage_qwen3_cpp_runtime.sh"), str(source_build), str(destination)],
        check=True,
    )

    cli = destination / "qwen3-asr-cli"
    binaries = [cli, *destination.glob("*.dylib")]
    assert "@executable_path" in macho_rpaths(cli)
    assert not [
        (binary.name, rpath)
        for binary in binaries
        for rpath in macho_rpaths(binary)
        if rpath.startswith("/")
    ]
    subprocess.run([str(cli), "--help"], check=True, capture_output=True, text=True)


@pytest.mark.parametrize("backend", ["doubao_asr", "doubao_asr_v2", "auto"])
@pytest.mark.parametrize("qwen_ready", [False, True])
@pytest.mark.parametrize("runtime_present", [False, True])
def test_backend_selects_a_usable_runtime(run_lua, backend, qwen_ready, runtime_present):
    source = (ROOT / ".subfix_support/subfix_generate_selection_core.lua").read_text(encoding="utf-8")
    start = source.index("local function build_asr_helper_batch_command(")
    end = source.index("local function kill_background_process(", start)
    run_lua("backend=" + json.dumps(backend) + "; qwen_ready=" + str(qwen_ready).lower()
            + "; runtime_present=" + str(runtime_present).lower())
    run_lua(r'''
        local paths = {helper="/helper.py", python="/obsolete/python", runtime_python="/bundled/python", diagnostic="/diagnostic.json"}
        local function resolve_asr_paths() return paths end
        local function file_exists(path)
            if path == "/bundled/python" then return runtime_present end
            return path == "/helper.py" or (path == "/qwen/python" and qwen_ready)
        end
        local function inspect_local_qwen() return {ready=qwen_ready, python="/qwen/python", model="/model"} end
        local function resolve_generate_engine() return "v5" end
        local function resolve_segmentation_profile_path() return nil end
        local function shell_quote(value) return "'" .. tostring(value) .. "'" end
        local DEFAULT_ASR_BACKEND="auto"
        local DEFAULT_ASR_MODEL="model"
        local DEFAULT_ASR_LANGUAGE="zh"
    ''' + source[start:end] + r'''
        local command, err = build_asr_helper_batch_command("/batch", "/srt", "/json", 108000, 30, "/progress", "narration", 25, backend, nil)
        if qwen_ready then
            assert(command and not err)
            assert(command:find("'/qwen/python'", 1, true))
            assert(command:find("env -u PYTHONHOME -u PYTHONPATH", 1, true))
        elseif backend == "auto" then
            assert(not command and err:find("本地 Qwen 尚未安装", 1, true))
        elseif runtime_present then
            assert(command and not err)
            assert(command:find("'/bundled/python'", 1, true))
        else
            assert(not command and err:find("内置 Python 缺失", 1, true))
        end
        if command then
            assert(command:find("--backend '" .. backend .. "'", 1, true))
            assert(not command:find("/obsolete/python", 1, true))
        end
    ''')


def test_qwen_install_failure_dialog_has_room_for_the_real_error_and_log_path():
    manager_source = (ROOT / ".subfix_support/subfix_qwen_local_manager.py").read_text(encoding="utf-8")
    core_source = (ROOT / ".subfix_support/subfix_generate_selection_core.lua").read_text(encoding="utf-8")

    assert 'qwen-local-install.log' in manager_source
    assert '完整日志：' in manager_source
    assert 'Geometry = SUBFIX_WINDOW_GEOMETRY.centered_geometry({560, 340, 560, 240})' in core_source
