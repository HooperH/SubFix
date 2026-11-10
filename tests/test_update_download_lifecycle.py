"""Exercise update phases in Resolve LuaJIT without network or installed-file writes."""

from pathlib import Path

import pytest

from test_normalize_length_options import run_lua  # noqa: F401


SOURCE = (Path(__file__).resolve().parents[1] / "SubFix.lua").read_text(encoding="utf-8")
UPDATE_FUNCTIONS = SOURCE[SOURCE.index("function open_subfix_update_website()"):SOURCE.index("function win.On.ModelCenterBtn.Clicked")]
CHECK_HANDLER = SOURCE[SOURCE.index("function win.On.CheckUpdateBtn.Clicked(ev)"):SOURCE.index("-- 更新时间线按钮（自动备份后执行）")]
QUOTE = SOURCE[SOURCE.index("local function shell_quote(value)"):SOURCE.index("\nend", SOURCE.index("local function shell_quote(value)")) + 4] + "\n"

HARNESS = r'''
subfix_update_helper_path = function() return "/safe folder/updater.py" end
subfix_update_python_path = function() return "/safe folder/python3" end
trim_text = function(value) return value end
os.remove = function() end
os.execute = function() return 0 end
read_text_file = function(path) return path end
calls, windows = {}, {}
payload = {version = "3.4.1", zip_url = "https://website.invalid/update.zip",
    sha256 = string.rep("a", 64), size_bytes = 337,
    sha256_url = "https://backup.invalid/update.sha256",
    fallback_urls = {"https://backup.invalid/update.zip", "https://backup2.invalid/update.zip"}}
function show_long_task_progress_window(options)
    local state = {started_at = 1, cancel_requested = false, cancellable = options.cancellable,
        window = {visible = true, Hide = function(self) self.visible = false end}}
    windows[#windows + 1] = state.window
    return state
end
update_long_task_progress_window = function() end
function finish_long_task_progress_window(state, status)
    if status == "done" then state.window:Hide() end
end
function run_subfix_background_command(cmd, options)
    calls[#calls + 1] = {cmd = cmd, cancellable = options.progress_state.cancellable}
    if #calls == 1 then
        if scenario == "cancelled" then return false, "", "cancelled" end
        if scenario == "cancel_race" then options.progress_state.cancel_requested = true end
        if scenario == "failed" then return false, "connection failed" end
    end
    if #calls == 2 and scenario == "install_failed" then return false, "write failed" end
    return true, ""
end
function decode_json_text(path)
    if path:find(".download.json", 1, true) then
        if scenario == "failed" then return {ok = false, error = "connection failed"} end
        if scenario == "invalid" then return {ok = true, version = "3.4.1"} end
        if scenario == "wrong_version" then return {ok = true, version = "3.4.2", archive_path = "/safe folder/cache.zip", sha256 = string.rep("a", 64)} end
        return {ok = true, version = "3.4.1", archive_path = "/safe folder/cache.zip", sha256 = string.rep("a", 64)}
    end
    return {ok = scenario ~= "install_failed", version = "3.4.1", error = "write failed"}
end
'''


@pytest.mark.parametrize("inline_hash", [True, False])
def test_download_verifies_before_non_cancellable_install(run_lua, inline_hash):
    run_lua(QUOTE + UPDATE_FUNCTIONS + HARNESS + ("payload.sha256 = nil\n" if not inline_hash else "") + r'''
scenario = "success"
local ok, message = run_subfix_update(payload)
assert(ok and message:find("3.4.1", 1, true))
assert(#calls == 2 and calls[1].cancellable == true and calls[2].cancellable == false)
assert(calls[1].cmd:find("download --zip-url 'https://website.invalid/update.zip'", 1, true))
assert(calls[1].cmd:find("--size-bytes '337'", 1, true))
assert(calls[1].cmd:find("--fallback-url 'https://backup.invalid/update.zip'", 1, true))
assert(calls[1].cmd:find("--fallback-url 'https://backup2.invalid/update.zip'", 1, true))
if payload.sha256 then
    assert(calls[1].cmd:find("--sha256 '" .. payload.sha256 .. "'", 1, true))
    assert(not calls[1].cmd:find("--sha256-url", 1, true))
else
    assert(calls[1].cmd:find("--sha256-url 'https://backup.invalid/update.sha256'", 1, true))
end
assert(calls[2].cmd:find("install-local --archive '/safe folder/cache.zip'", 1, true))
assert(not calls[2].cmd:find("--zip-url", 1, true))
assert(not windows[1].visible and not windows[2].visible)
''')


@pytest.mark.parametrize("scenario", ["cancelled", "cancel_race", "failed", "invalid", "wrong_version"])
def test_unsuccessful_download_never_starts_install(run_lua, scenario):
    run_lua(QUOTE + UPDATE_FUNCTIONS + HARNESS + f'scenario = "{scenario}"\n' + r'''
local ok, message, status = run_subfix_update(payload)
assert(not ok and message ~= "")
assert(#calls == 1, "installation must never start after cancellation or invalid download")
assert(not windows[1].visible, "finished download window must not remain below the error dialog")
if scenario == "cancelled" or scenario == "cancel_race" then
    assert(status == "cancelled" and message:find("继续下载", 1, true))
end
''')


def test_failed_install_closes_progress_for_actionable_error(run_lua):
    run_lua(QUOTE + UPDATE_FUNCTIONS + HARNESS + r'''
scenario = "install_failed"
local ok, message, status = run_subfix_update(payload)
assert(not ok and status == "failed" and message == "write failed")
assert(#calls == 2 and calls[2].cancellable == false)
assert(not windows[2].visible)
''')


@pytest.mark.parametrize("result", ["no_update", "failed", "cancelled"])
def test_check_failures_offer_manual_download_but_normal_status_does_not(run_lua, result):
    run_lua(QUOTE + UPDATE_FUNCTIONS + r'''
win = {On = {CheckUpdateBtn = {}}, GetItems = function() return {CheckUpdateBtn = {}} end}
subfix_update_helper_path = function() return "/helper.py" end
subfix_update_python_path = function() return "/python3" end
update_shared_status = function(_, message) last_status = message end
SUBFIX_VERSION = "3.4.0"
local dialogs = 0
show_subfix_update_confirm = function(_, failure)
    if failure then dialogs = dialogs + 1; return "website" end
    return "install"
end
''' + f'scenario = "{result}"\n' + r'''
run_subfix_update_check = function()
    if scenario == "cancelled" then return true, {version = "3.4.1"} end
    return false, "result message", scenario
end
run_subfix_update = function() return false, "已取消下载", "cancelled" end
''' + CHECK_HANDLER + r'''
win.On.CheckUpdateBtn.Clicked({})
assert(dialogs == (scenario == "failed" and 1 or 0))
assert(SUBFIX_UPDATE_RUNNING == false)
''')


@pytest.mark.parametrize("browser_success", [True, False])
def test_manual_download_button_opens_official_site_and_handles_failure(run_lua, browser_success):
    run_lua(QUOTE + UPDATE_FUNCTIONS + r'''
SUBFIX_WINDOW_GEOMETRY = {centered_geometry = function(value) return value end}
ui = setmetatable({}, {__index = function() return function(_, definition) return definition end end})
local items = {}
local function collect(node)
    if type(node) ~= "table" then return end
    if node.ID then items[node.ID] = node end
    for _, child in ipairs(node) do collect(child) end
end
dispatcher = {}
function dispatcher:AddWindow(_, layout)
    collect(layout)
    dialog = {On = setmetatable({}, {__index = function(t, key) local events = {}; rawset(t, key, events); return events end}),
        Show = function() end, Hide = function() end, GetItems = function() return items end}
    return dialog
end
function dispatcher:ExitLoop() end
''' + f'browser_success = {str(browser_success).lower()}\n' + r'''
os.execute = function(cmd) opened_command = cmd; return browser_success and 0 or 1 end
function dispatcher:RunLoop()
    assert(items.SubFixUpdateInstall.Hidden == true)
    assert(items.SubFixUpdateNotes.PlainText == "connection failed")
    dialog.On.SubFixUpdateWebsite.Clicked({})
    if not browser_success then
        assert(items.SubFixUpdateNotes.PlainText:find("https://subfix-by-hooper.ysjf.chatgpt.site/", 1, true))
        dialog.On.SubFixUpdateCancel.Clicked({})
    end
end
local action = show_subfix_update_confirm(nil, "connection failed")
assert(action == (browser_success and "website" or "cancel"))
assert(opened_command == "/usr/bin/open 'https://subfix-by-hooper.ysjf.chatgpt.site/' >/dev/null 2>&1")
''')
