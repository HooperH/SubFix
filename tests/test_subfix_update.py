import hashlib
import io
import ctypes
import json
import sys
import zipfile
from pathlib import Path
from urllib.error import HTTPError

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".subfix_support"))

import subfix_update


def release(version: str, *, prerelease: bool = False, draft: bool = False) -> dict:
    base = f"https://example.invalid/SubFix-update-v{version}"
    return {
        "tag_name": f"v{version}",
        "name": f"SubFix {version}",
        "body": "修复字幕生成",
        "draft": draft,
        "prerelease": prerelease,
        "assets": [
            {"name": f"SubFix-update-v{version}.zip", "browser_download_url": base + ".zip"},
            {"name": f"SubFix-update-v{version}.sha256", "browser_download_url": base + ".sha256"},
        ],
    }


def test_release_check_accepts_only_newer_stable_release():
    result = subfix_update.release_to_update_info(release("3.2.0"), "3.1.0")

    assert result["ok"] is True
    assert result["update_available"] is True
    assert result["version"] == "3.2.0"
    assert result["zip_url"].endswith(".zip")


def test_release_check_prefers_complete_full_asset_pair():
    payload = release("3.4.0")
    full_assets = [
        {"name": f"SubFix-update-v3.4.0-full.{suffix}",
         "browser_download_url": f"https://example.invalid/SubFix-update-v3.4.0-full.{suffix}"}
        for suffix in ("zip", "sha256")
    ]
    payload["assets"].append(full_assets[0])
    assert subfix_update.release_to_update_info(payload, "3.3.1")["zip_url"].endswith("v3.4.0.zip")
    payload["assets"].append(full_assets[1])
    result = subfix_update.release_to_update_info(payload, "3.3.1")
    assert result["zip_url"].endswith("v3.4.0-full.zip")
    assert result["sha256_url"].endswith("v3.4.0-full.sha256")


@pytest.mark.parametrize("remote", [release("3.1.0"), release("3.0.9"), release("3.2.0", prerelease=True), release("3.2.0", draft=True)])
def test_release_check_rejects_non_newer_or_nonstable_release(remote):
    result = subfix_update.release_to_update_info(remote, "3.1.0")

    assert result["ok"] is False


def test_local_next_version_is_not_downgraded_or_called_latest_stable():
    older = subfix_update.release_to_update_info(release("3.3.9"), "3.4.0")
    assert older["ok"] is False
    assert older["error"] == "当前版本高于最新正式版（3.3.9）"
    equal = subfix_update.release_to_update_info(release("3.4.0"), "3.4.0")
    assert equal["error"] == "当前已是最新稳定版"
    newer = subfix_update.release_to_update_info(release("3.4.1"), "3.4.0")
    assert newer["ok"] is True
    assert newer["version"] == "3.4.1"


@pytest.mark.parametrize("declared_size", [3, 6, None])
def test_download_reports_bytes_and_rejects_incomplete_content(tmp_path, monkeypatch, declared_size):
    response = io.BytesIO(b"abc")
    response.headers = {} if declared_size is None else {"Content-Length": str(declared_size)}
    response.geturl = lambda: "https://example.invalid/update.zip"
    monkeypatch.setattr(subfix_update, "urlopen", lambda *_args, **_kwargs: response)
    updates = []
    target = tmp_path / "update.zip"
    if declared_size == 6:
        with pytest.raises(subfix_update.UpdateError, match="下载不完整"):
            subfix_update.download_to(response.geturl(), target, progress=lambda count, total: updates.append((count, total)))
    else:
        subfix_update.download_to(response.geturl(), target, progress=lambda count, total: updates.append((count, total)))
        assert target.read_bytes() == b"abc"
    assert updates == [(0, declared_size), (3, declared_size)]


def test_release_check_rejects_invalid_version_and_missing_assets():
    invalid = release("3.2.0")
    invalid["tag_name"] = "next"
    assert subfix_update.release_to_update_info(invalid, "3.1.0")["ok"] is False

    missing = release("3.2.0")
    missing["assets"] = missing["assets"][:1]
    assert subfix_update.release_to_update_info(missing, "3.1.0")["ok"] is False


def test_fetch_latest_release_explains_missing_first_release(monkeypatch):
    def missing_release(*_args, **_kwargs):
        raise HTTPError(subfix_update.RELEASE_API_URL, 404, "Not Found", None, None)

    monkeypatch.setattr(subfix_update, "urlopen", missing_release)

    with pytest.raises(subfix_update.UpdateError, match="尚未发布首个稳定版本"):
        subfix_update.fetch_latest_release()


def make_update_archive(path: Path, version: str = "3.2.0", extra_files: list[str] | None = None) -> None:
    files = ["SubFix/SubFix.lua", ".subfix_support/subfix_update.py", *(extra_files or [])]
    manifest = {"schema_version": 1, "version": version, "files": files}
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("subfix-update-manifest.json", json.dumps(manifest))
        archive.writestr("SubFix/SubFix.lua", "-- new version\n")
        archive.writestr(".subfix_support/subfix_update.py", "# new updater\n")
        for extra_file in extra_files or []:
            archive.writestr(extra_file, b"bundled ffmpeg")


def test_install_archive_replaces_only_whitelisted_code_files(tmp_path: Path):
    archive = tmp_path / "update.zip"
    make_update_archive(archive)
    target = tmp_path / "Utility"
    old_main = target / "SubFix" / "SubFix.lua"
    credential = target / ".subfix_support" / "doubao_credentials.json"
    model = target / ".subfix_support" / "models" / "model.gguf"
    old_main.parent.mkdir(parents=True)
    credential.parent.mkdir(parents=True)
    model.parent.mkdir(parents=True)
    old_main.write_text("-- old version\n", encoding="utf-8")
    credential.write_text('{"api_key":"secret"}', encoding="utf-8")
    model.write_text("model", encoding="utf-8")

    subfix_update.install_archive(archive, hashlib.sha256(archive.read_bytes()).hexdigest(), "3.2.0", target)

    assert old_main.read_text(encoding="utf-8") == "-- new version\n"
    assert credential.read_text(encoding="utf-8") == '{"api_key":"secret"}'
    assert model.read_text(encoding="utf-8") == "model"


def test_install_archive_allows_bundled_ffmpeg_only_at_its_fixed_path(tmp_path: Path):
    archive = tmp_path / "update.zip"
    bundled_path = ".subfix_support/bin/ffmpeg"
    make_update_archive(archive, extra_files=[bundled_path])
    target = tmp_path / "Utility"

    subfix_update.install_archive(archive, hashlib.sha256(archive.read_bytes()).hexdigest(), "3.2.0", target)

    assert (target / bundled_path).read_bytes() == b"bundled ffmpeg"


def test_install_archive_rejects_checksum_before_writing(tmp_path: Path):
    archive = tmp_path / "update.zip"
    make_update_archive(archive)
    target_file = tmp_path / "Utility" / "SubFix" / "SubFix.lua"
    target_file.parent.mkdir(parents=True)
    target_file.write_text("-- old version\n", encoding="utf-8")

    with pytest.raises(subfix_update.UpdateError, match="SHA-256"):
        subfix_update.install_archive(archive, "0" * 64, "3.2.0", target_file.parents[1])

    assert target_file.read_text(encoding="utf-8") == "-- old version\n"


def test_install_archive_refuses_a_symlinked_destination_parent(tmp_path: Path):
    archive = tmp_path / "update.zip"
    make_update_archive(archive)
    target = tmp_path / "Utility"
    outside = tmp_path / "outside"
    outside.mkdir()
    target.mkdir()
    (target / "SubFix").symlink_to(outside, target_is_directory=True)

    with pytest.raises(subfix_update.UpdateError, match="符号链接"):
        subfix_update.install_archive(archive, hashlib.sha256(archive.read_bytes()).hexdigest(), "3.2.0", target)

    assert not (outside / "SubFix.lua").exists()


def test_release_script_requires_clean_verified_versioned_release():
    script = (ROOT / "scripts" / "release_update.sh").read_text(encoding="utf-8")

    assert "git status --porcelain" in script
    assert "SUBFIX_VERSION" in script
    assert "python3 -m pytest -q" in script
    assert 'PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"' in script
    assert "gh release create" in script
    assert 'python3 "$ROOT/scripts/build_update_archives.py" "$PAYLOAD" "$VERSION" "$WORK"' in script
    assert '"$WORK/$ASSET_PREFIX-full.zip" "$WORK/$ASSET_PREFIX-full.sha256"' in script
    assert "subfix_qwen_local_manager.py" in script
    assert "subfix_process_group.py" in script
    assert '".subfix_support/bin/ffmpeg:$WORK/subfix-ffmpeg"' in script
    assert 'shasum -a 256 "$WORK/$ASSET" >' not in script
    assert "doubao_credentials.json" not in script
    assert "qwen3-asr.cpp" not in script


def test_update_allowlist_accepts_the_process_group_runner():
    assert ".subfix_support/subfix_process_group.py" in subfix_update.UPDATE_FILE_PATHS


def test_release_script_creates_metadata_free_update_zip():
    script = (ROOT / "scripts" / "build_update_archives.py").read_text(encoding="utf-8")

    assert "zipfile.ZipFile" in script
    assert "ZIP_DEFLATED" in script
    assert "ditto -c -k --sequesterRsrc" not in script


def test_update_check_runs_in_background_and_install_tracks_download_progress():
    source = (ROOT / "SubFix.lua").read_text(encoding="utf-8")
    wrapper = source.split("function run_subfix_update_with_progress(", 1)[1].split("function run_subfix_update(payload)", 1)[0]
    check = source.split("function win.On.CheckUpdateBtn.Clicked(ev)", 1)[1].split("-- 更新时间线按钮", 1)[0]
    install = source.split("function run_subfix_update(payload)", 1)[1].split("function win.On.CheckUpdateBtn.Clicked", 1)[0]
    assert "show_long_task_progress_window" in wrapper
    assert "pcall(run_subfix_background_command" in wrapper
    assert 'status == "cancelled"' in wrapper
    assert 'finish_long_task_progress_window(progress_state, "failed"' in wrapper
    assert 'finish_long_task_progress_window(progress_state, "done"' in wrapper
    assert 'run_subfix_update_check(cmd, output_path)' in check
    assert 'if SUBFIX_UPDATE_RUNNING then return end' in check
    assert 'run_subfix_update_with_progress(cmd, output_path, "下载更新包", true, progress_path)' in install
    assert '"安装更新", false' in install
    assert '"--progress", shell_quote(progress_path)' in install
    assert check.index("if not ok then") < check.index("show_subfix_update_confirm(payload)")


def test_update_progress_ticks_without_a_progress_file_and_isolates_cancellation():
    source = (ROOT / "SubFix.lua").read_text(encoding="utf-8")
    runner = source.split("function run_subfix_background_command(cmd, options)", 1)[1].split("local function parse_fps", 1)[0]
    poll = runner.split("local function update_progress_from_file()", 1)[1]
    assert poll.index("update_long_task_progress_window(options.progress_state") < poll.index('if not progress_file or progress_file == "" then return end')
    assert "return options.progress_state.cancel_requested == true" in runner
    assert runner.count("if background_cancel_requested() then") >= 3


def test_update_progress_supports_indeterminate_display_and_protects_install():
    source = (ROOT / "SubFix.lua").read_text(encoding="utf-8")
    bar = source.split("function long_task_progress_bar_text(progress_state, payload)", 1)[1].split("function show_long_task_progress_window", 1)[0]
    window = source.split("function show_long_task_progress_window(options)", 1)[1].split("function update_long_task_progress_window", 1)[0]
    render = source.split("function update_long_task_progress_window(progress_state", 1)[1].split("function finish_long_task_progress_window", 1)[0]
    assert "payload.indeterminate == true" in bar
    assert "progress_state.started_at" in bar
    assert "percent == nil" in render
    assert "if options.cancellable == false then return end" in window
    assert window.index("if progress_state.finished then") < window.index("if options.cancellable == false then return end")


def test_update_progress_lua_runtime_lifecycle():
    library = Path("/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/libluajit-5.1.2.dylib")
    if not library.exists():
        pytest.skip("Resolve LuaJIT runtime is not installed")
    lua = ctypes.CDLL(str(library))
    lua.luaL_newstate.restype = ctypes.c_void_p
    lua.luaL_openlibs.argtypes = [ctypes.c_void_p]
    lua.luaL_loadstring.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lua.lua_pcall.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    lua.lua_tolstring.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
    lua.lua_tolstring.restype = ctypes.c_char_p
    lua.lua_close.argtypes = [ctypes.c_void_p]
    state = lua.luaL_newstate()
    assert state

    def run(code, *, compile_only=False):
        status = lua.luaL_loadstring(state, code.encode("utf-8"))
        if status == 0 and not compile_only:
            status = lua.lua_pcall(state, 0, 0, 0)
        assert status == 0, (lua.lua_tolstring(state, -1, None) or b"Lua failure").decode("utf-8")

    source = (ROOT / "SubFix.lua").read_text(encoding="utf-8")
    shared = source[source.index("local LONG_TASK_PROGRESS_BAR_WIDTH"):source.index("function normalize_progress_elapsed_text")]
    wrapper = "function run_subfix_update_with_progress(" + source.split("function run_subfix_update_with_progress(", 1)[1].split("function run_subfix_update(payload)", 1)[0]
    try:
        lua.luaL_openlibs(state)
        run(source, compile_only=True)
        run('''
            ui = setmetatable({}, {__index = function() return function(_, value) return value end end})
            SUBFIX_WINDOW_GEOMETRY = {centered_geometry = function(value) return value end}
            dispatcher = {AddWindow = function()
                local items = {LongTaskProgressStatusLabel = {}, LongTaskProgressBarLabel = {},
                    LongTaskProgressMetaLabel = {}, LongTaskProgressCancelBtn = {Enabled = false}}
                last_window = {On = {LongTaskProgressCancelBtn = {}, LongTaskProgressWindow = {}},
                    Show = function(self) self.visible = true end,
                    Hide = function(self) self.visible = false end,
                    GetItems = function() return items end}
                return last_window
            end}
        ''' + shared + wrapper + '''
            local state = show_long_task_progress_window({cancellable = false})
            state.window.On.LongTaskProgressWindow.Close()
            assert(not state.cancel_requested)
            update_long_task_progress_window(state, {indeterminate = true})
            assert(not state.window:GetItems().LongTaskProgressMetaLabel.Text:find("%%"))
            local first = long_task_progress_bar_text(state, {indeterminate = true})
            state.started_at = state.started_at - 1
            assert(first ~= long_task_progress_bar_text(state, {indeterminate = true}))
            finish_long_task_progress_window(state, "failed", "网络错误")
            assert(state.window:GetItems().LongTaskProgressCancelBtn.Enabled)
            state.window.On.LongTaskProgressCancelBtn.Clicked()
            assert(not state.window.visible)

            trim_text = function(value) return value end
            read_text_file = function() return "{}" end
            decode_json_text = function() return result end
            shell_quote = function(value) return value end
            os.execute = function() end
            run_subfix_background_command = function(_, options)
                assert(options.progress_state and options.status_started_at)
                if scenario == "exception" then error("runner failed") end
                if scenario == "cancelled" then return false, "已取消", "cancelled" end
                return scenario == "success", ""
            end
            result = {ok = true, version = "3.2.0"}
            scenario = "success"
            local ok, payload = run_subfix_update_with_progress("", "", "检查更新", true)
            assert(ok and payload.version == "3.2.0" and not last_window.visible)
            for _, failure in ipairs({"cancelled", "exception", "invalid"}) do
                scenario = failure
                result = nil
                local ok, message = run_subfix_update_with_progress("", "", "检查更新", true)
                assert(not ok and message ~= "" and last_window.visible)
                assert(last_window:GetItems().LongTaskProgressCancelBtn.Text == "关闭")
                last_window.On.LongTaskProgressCancelBtn.Clicked()
                assert(not last_window.visible)
            end
        ''')
    finally:
        lua.lua_close(state)
