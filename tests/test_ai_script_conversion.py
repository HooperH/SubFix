"""Exercise AI task routing and conversion result application in Resolve LuaJIT."""

from pathlib import Path

import pytest
import ctypes
import json

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
    assert state
    lua.luaL_openlibs(state)

    def run(code):
        status = lua.luaL_loadstring(state, code.encode("utf-8"))
        if status == 0:
            status = lua.lua_pcall(state, 0, 0, 0)
        assert status == 0, (lua.lua_tolstring(state, -1, None) or b"Lua failure").decode("utf-8")

    try:
        yield run
    finally:
        lua.lua_close(state)




SOURCE = (Path(__file__).resolve().parents[1] / "SubFix.lua").read_text(encoding="utf-8")


def section(start, end):
    return SOURCE[SOURCE.index(start):SOURCE.index(end, SOURCE.index(start))]


def test_conversion_task_has_no_correction_context(run_lua):
    task_setup = section("    -- 获取用户选择的任务", "    local function start_ai_progress(")
    run_lua('''
        win = {GetItems = function() return {AITaskSelect = {CurrentIndex = 3}} end}
        shared_config = {script_content = "参考文稿不应参与转换", is_script_enabled = true}
        REFERENCE_SCRIPT_HARD_LIMIT = 10000
        sanitize_reference_script_text = function(text) return text end
        count_utf8_chars = function(text) return #text end
    ''' + task_setup + '''
        assert(task_name == "简体转繁体")
        assert(not is_correction_task, "conversion must not run correction or boundary repair")
        assert(not use_script_context, "reference script must not rewrite conversion text")
        assert(sys_prompt:find("只转换简繁字形", 1, true))
        assert(sys_prompt:find("序号|文本", 1, true))
        assert(sys_prompt:find("空格", 1, true))
        assert(sys_prompt:find("不替换地区用语", 1, true))
    ''')


@pytest.mark.parametrize("index,task_type", [
    (0, "full_fix"), (1, "particle_fix"),
    (2, "zh_to_en"), (3, "simplified_to_traditional"),
])
def test_grouped_task_routing(run_lua, index, task_type):
    routing = section("    -- 获取用户选择的任务", "    local script_context =")
    run_lua('''
        win = {GetItems = function() return {AITaskSelect = {CurrentIndex = ''' + str(index) + '''}} end}
    ''' + routing + '''
        assert(task_type == "''' + task_type + '''")
    ''')


def test_task_menu_groups_related_tasks(run_lua):
    menu = section("        if full_items and full_items.AITaskSelect then", "\n        end")
    run_lua('''
        local labels = {}
        full_items = {AITaskSelect = {AddItem = function(_, text) table.insert(labels, text) end}}
    ''' + menu + '''
        end
        assert(#labels == 4)
        assert(labels[3]:find("中英翻译", 1, true))
        assert(labels[4]:find("简繁转换", 1, true))
    ''')


def test_direction_controls_are_removed():
    for widget in ("AIDirectionRow", "AITranslationDirection", "AIScriptDirection"):
        assert widget not in SOURCE
    assert "sync_ai_task_direction" not in SOURCE


@pytest.mark.parametrize("task_type,detected", [
    ("zh_to_en", "zh_to_en"), ("zh_to_en", "en_to_zh"),
    ("simplified_to_traditional", "simplified_to_traditional"),
    ("simplified_to_traditional", "traditional_to_simplified"),
])
def test_detection_samples_whole_selection_and_accepts_only_task_family(run_lua, task_type, detected):
    helper = section("function detect_ai_task_direction(", "local function open_full_window()")
    run_lua('''
        trim_text = function(text) return tostring(text or ""):match("^%s*(.-)%s*$") end
    ''' + helper + '''
        local rows = {}
        for i = 1, 100 do rows[i] = {text = string.rep("测试", 300)} end
        rows[1].text = "FIRST"; rows[100].text = "LAST"
        local calls = 0
        local direction = detect_ai_task_direction("''' + task_type + '''", rows, function(content, label, options)
            calls = calls + 1
            assert(content:find("FIRST", 1, true) and content:find("LAST", 1, true))
            assert(#content < 21000, "detection sample must be bounded")
            assert(options.sys_prompt_override and not options.use_script_context)
            return " ''' + detected + ''' ", "stop"
        end)
        assert(calls == 1 and direction == "''' + detected + '''")
        assert(rows[50].text == string.rep("测试", 300))
    ''')


@pytest.mark.parametrize("response,finish,status", [
    ('"UNKNOWN"', '"stop"', 'nil'),
    ('"en_to_zh"', '"stop"', 'nil'),
    ('"traditional_to_simplified"', '"length"', 'nil'),
    ('"traditional_to_simplified"', '"MAX_TOKENS"', 'nil'),
    ('nil', 'nil', '"cancelled"'),
])
def test_detection_rejects_invalid_truncated_and_cancelled_results(run_lua, response, finish, status):
    helper = section("function detect_ai_task_direction(", "local function open_full_window()")
    run_lua('''
        trim_text = function(text) return tostring(text or ""):match("^%s*(.-)%s*$") end
    ''' + helper + '''
        local result, err, state = detect_ai_task_direction("simplified_to_traditional", {{text = "字幕"}},
            function() return ''' + response + ', ' + finish + ', "请求失败", ' + status + ''' end)
        assert(result == nil and err ~= nil)
        if ''' + status + ''' == "cancelled" then assert(state == "cancelled") end
    ''')


def test_detected_direction_configures_all_following_batches(run_lua):
    setup = section("    -- 获取用户选择的任务", "    local function start_ai_progress(")
    detection = section("    if not is_correction_task then\n        update_ai_progress", "    -- 收集对比报告")
    run_lua('''
        win = {GetItems = function() return {AITaskSelect = {CurrentIndex = 3}} end}
        shared_config = {script_content = "", is_script_enabled = false}
        sanitize_reference_script_text = function(text) return text end
        count_utf8_chars = function(text) return #text end
        detect_ai_task_direction = function() return "traditional_to_simplified" end
        update_ai_progress = function() end
    ''' + setup + '''
        local base_sys_prompt = sys_prompt
    ''' + detection + '''
        assert(task_type == "traditional_to_simplified" and task_name == "繁体转简体")
        assert(base_sys_prompt:find("统一转换为简体中文", 1, true))
        assert(base_sys_prompt == sys_prompt)
    ''')


@pytest.mark.parametrize("truncated", [False, True])
def test_conversion_updates_only_text_and_aborts_on_truncation(run_lua, truncated):
    batch_body = section("        local translation_batch_size = 20", '    print("[Hooper AI 2.0] 自动应用')
    run_lua('''
        local rows = {{text = "软件里的头发，FX3 4K", start_frame = 10, end_frame = 30},
            {text = "English 123", start_frame = 40, end_frame = 60}}
        local sorted_list = rows
        local task_name = "简体转繁体"
        local fix_count, pending_count = 0, 0
        local applied_any_change = false
        local report_entries = {}
        local ai_progress = {}
        local final_status = nil
        print = function() end
        trim_text = function(text) return text:match("^%s*(.-)%s*$") end
        build_subtitle_list = function(first, last) return "1|" .. rows[first].text .. "\\n2|" .. rows[last].text end
        update_ai_progress = function() end
        finish_ai_progress = function(_, kind) final_status = kind end
        execute_ai_request = function(_, label, options)
            assert(label == "简体转繁体_batch_1")
            assert(options.batch_line_count == 2 and not options.use_script_context)
            return "response", "''' + ("length" if truncated else "stop") + '''"
        end
        ai_helpers = {parse_ai_line_payload = function(_, count, first, allow_missing)
            assert(count == 2 and first == 1 and allow_missing)
            return {[1] = "軟件裡的頭髮，FX3 4K", [2] = "English 123"}, nil, {}
        end}
        report_helpers = {build_report_entry = function() return {} end}
        local function apply()
            if true then
    ''' + batch_body + '''
        end
        apply()
        assert(rows[1].text == "''' + ("软件里的头发，FX3 4K" if truncated else "軟件裡的頭髮，FX3 4K") + '''")
        assert(rows[2].text == "English 123")
        assert(rows[1].start_frame == 10 and rows[1].end_frame == 30)
        assert(rows[2].start_frame == 40 and rows[2].end_frame == 60)
        assert(fix_count == ''' + ("0" if truncated else "1") + ''')
        assert(applied_any_change == ''' + str(not truncated).lower() + ''')
    ''')


def test_complete_plugin_compiles_in_resolve_luajit(run_lua):
    run_lua("assert(loadfile(" + json.dumps(str(Path(__file__).resolve().parents[1] / "SubFix.lua"), ensure_ascii=False) + "))")
