"""Execute the actual Lua polling loop without touching Resolve project data."""
import ctypes
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / ".subfix_support/subfix_generate_selection_core.lua").read_text()


@pytest.fixture
def run_lua():
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
    lua.luaL_openlibs(state)

    def run(code):
        result = lua.luaL_loadstring(state, code.encode())
        if result == 0:
            result = lua.lua_pcall(state, 0, 0, 0)
        assert result == 0, (lua.lua_tolstring(state, -1, None) or b"Lua failed").decode()

    try:
        yield run
    finally:
        lua.lua_close(state)


@pytest.mark.parametrize("mode", ["success", "failed_exit", "exit_only", "failed_no_exit", "dead", "missing_pid", "cancel", "long_running"])
def test_progress_loop_finishes_and_preserves_only_failure_logs(run_lua, mode):
    body = SOURCE[SOURCE.index("local function progress_payload_signature("):SOURCE.index("local function run_asr_helper_with_progress(")]
    run_lua('mode = "' + mode + '"\n' + r'''
        local now, ticks, stopped, kills = 0, 0, false, 0
        local removed_log, callback, last_display = false, nil, ""
        local complete_tick = mode == "long_running" and 350 or 3
        print = function() end
        os.time = function() return now end
        os.execute = function(cmd)
            if cmd:find("kill %-0") then return mode == "dead" and 256 or 0 end
            if cmd:find("^rm %-f") and cmd:find("asr_stdout_") then removed_log = true end
            return 0
        end
        trim_text = function(s) return s:match("^%s*(.-)%s*$") end
        shell_quote = function(s) return "'" .. s .. "'" end
        temp_dir = function() return "/tmp" end
        resolve_asr_paths = function() return {runtime_python = "python", process_group = "group"} end
        file_exists = function(p)
            if p == "python" or p == "group" then return true end
            return p:find("asr_done_") and ticks >= complete_tick and
                (mode == "success" or mode == "failed_exit" or mode == "long_running")
        end
        read_text_file = function(p)
            if p:find("asr_pid_") then return mode == "missing_pid" and "" or "1234" end
            if p:find("asr_exit_") and ticks >= complete_tick then
                if mode == "success" or mode == "long_running" then return "0" end
                if mode == "failed_exit" or mode == "exit_only" then return "134" end
            end
            if p:find("asr_stdout_") then return "native diagnostic" end
            return ""
        end
        parse_progress_payload = function()
            return {stage = mode == "failed_no_exit" and "failed" or "qwen_align",
                message = "underlying error", elapsed_seconds = now, progress_index = 25, progress_total = 100}
        end
        kill_background_process = function() kills = kills + 1 end
        ui_timer_handlers = {}
        GENERATE_PROGRESS_STALL_WARNING_SECONDS = 300
        ui = {Timer = function() return {Start = function() end, Stop = function() end} end}
        register_ui_timer = function(_, fn) callback = fn; return true end
        update_generate_progress_window = function(_, _, message) last_display = message end
        dispatcher = {ExitLoop = function() stopped = true end,
            RunLoop = function()
                for i = 1, 360 do
                    ticks = i; now = i; callback()
                    if stopped then return end
                end
                error("poller stayed open after worker termination")
            end}
    ''' + body + r'''
        local ok, output, status = run_background_command_with_progress("cmd", "progress", {cancel_requested = mode == "cancel"})
        local success = mode == "success" or mode == "long_running"
        assert(ok == success, "wrong completion state")
        assert(removed_log == (success or mode == "cancel"), "wrong log retention")
        if mode == "cancel" then
            assert(status == "cancelled" and kills == 1)
        elseif not success then
            assert(output:find("诊断日志:"), "failure log path missing")
            assert(output:find("native diagnostic"), "native error lost")
        end
        if mode == "failed_no_exit" then assert(kills == 1 and ticks <= 4) end
        if mode == "dead" then assert(ticks <= 5) end
        if mode == "missing_pid" then assert(ticks <= 11) end
        if mode == "long_running" then assert(kills == 0 and ticks == 350) end
        assert(not last_display:find("本步骤已用"), "internal elapsed time leaked into UI")
        assert(last_display:find("underlying error"), "phase message missing")
    ''')


def test_progress_json_exposes_elapsed_without_changing_stall_signature(run_lua):
    parser = SOURCE[SOURCE.index("local function parse_progress_payload("):SOURCE.index("local function progress_bar_text(")]
    signature = SOURCE[SOURCE.index("local function progress_payload_signature("):SOURCE.index("local function run_background_command_with_progress(")]
    run_lua(parser + signature + '''
        local a = parse_progress_payload('{"stage":"qwen_align","message":"align","elapsed_seconds":12}')
        local b = parse_progress_payload('{"stage":"qwen_align","message":"align","elapsed_seconds":24}')
        assert(a.elapsed_seconds == 12)
        assert(progress_payload_signature(a) == progress_payload_signature(b))
    ''')
