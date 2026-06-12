#!/usr/bin/env lua
--[[
Hooper AI 2.0 - 达芬奇字幕管理插件终极版
基于 DaVinci Resolve 20 Fusion API 开发

【安装方式】
将本文件复制到以下任一位置：
1. ~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/
2. ~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Comp/

【运行方式】
在达芬奇中：Workspace → Script → Hooper AI 2.0

【版本】
v2.0.0 - 2026-03-18
支持 DaVinci Resolve 20.x
四大核心引擎架构：
  1. 全新 UI 架构：原生 TabBar + Stack
  2. 坚如磐石的时间码与更新引擎
  3. 时光机备份与恢复引擎
  4. 大模型 AI 纠错引擎
--]]

-- 顶部加载 utf8 库（达芬奇内置，安全容错）
pcall(require, "utf8")

-- 全程启动计时基准（用全局，避免主 chunk local 数量再次逼近 200 上限）
_subfix_script_started_at = os.clock()
function startup_elapsed_ms()
    return math.floor(((os.clock() - (_subfix_script_started_at or os.clock())) * 1000) + 0.5)
end

-- ========== UI 初始化 ==========
local ui = fusion.UIManager
local dispatcher = bmd.UIDispatcher(ui)
disp = dispatcher  -- 全局别名，确保弹窗函数内 disp 不为 nil
print(string.format("[Hooper AI 2.0] [STARTUP] Lua 主 chunk 起步: +%d ms", startup_elapsed_ms()))

-- ========== 全局状态 ==========
local subtitle_data_map = {}      -- {node_ptr = {target_abs_frame, fps, row_index, text}}
local subtitle_row_id_node_map = {} -- {row_id = node_ptr}
local current_rows = {}           -- { {index, target_abs_frame, fps, start_frame, end_frame, text, display_text} ... }
local current_track = 1
local current_subtitle_target_track = 1
local current_fps = 24.0
local current_tl_start_frame = 0
local timeline_offset = 0
local workflow_log_buffer = ""
local workflow_log_window = nil
local AIConfigPopWin = nil
local mini_win = nil
local active_window = nil
local is_subtitle_loaded = false
local current_search_query = ""
local current_selected_row_id = nil
local shared_status_text = "准备就绪，请先刷新字幕"
local suppress_track_change_events = false
local suppress_search_change_events = false
suppress_provider_change_events = false
provider_sync_in_progress = false
provider_combo_bootstrap_in_progress = false
full_window_ai_controls_initialized = false
full_window_tree_dirty = false  -- 标记完整版字幕树需要在切换时重新渲染
current_ai_provider_id = "siliconflow"
ai_config_popup_visible = false
ui_timer_handlers = {}
startup_refresh_timer = nil
full_window_deferred_sync_timer = nil
full_window_warmup_timer = nil  -- 空闲时段把字幕树渲染到完整版窗口，避免切换时同步渲染卡顿

-- AI 强制中止机制（B 方案：execute_ai_request 用后台 curl + 嵌套 RunLoop，
-- ⏻ 在 AI 跑批时也能派发 click，set 标志位 + kill curl 即时退出当前请求）
AI_CANCEL_REQUESTED = false        -- 用户请求取消正在跑的 AI 流程
AI_RUNNING = false                 -- 当前是否处于 execute_ai_request 嵌套循环里
AI_CURL_PID_FILE = nil             -- 当前后台 curl 子进程 PID 写在这个文件，force_quit 用它来 kill

local handle_main_window_close
local rebuild_tree_from_rows
local render_rows_to_window
local refresh_preview_windows
local get_row_timecodes
local LogMsg
local PendingChanges = {}
local pending_change_by_key = {}
local pending_change_item_map = {}
local pending_report_window = nil
local pending_report_tree = nil
pending_report_detail_view = nil
pending_detail_window = nil
applied_report_detail_view = nil
applied_report_detail_window = nil
local pending_report_summary_text = ""
local is_releasing_pending_report_ui = false
applied_toggle_tree = nil
applied_toggle_entries = {}       -- report_entries with row_id (for revert)
applied_toggle_item_map = {}      -- tree item -> index in applied_toggle_entries
local PENDING_UNCHECKED_MARK = "☐"
local PENDING_CHECKED_MARK = "☑"
local SEARCH_VIEW = {
    large_dataset_threshold = 800,
    max_tree_render_rows = 300,
    search_early_stop_limit = 300,
    modes = {
        full = "full",
        preview = "preview",
        search = "search"
    },
    dataset_revision = 0,
    cache = {
        last_query = "",
        last_match_rows = nil,
        dataset_revision = 0,
        last_dataset_revision = -1,
        last_was_truncated = false
    },
    -- 每个窗口最近一次渲染的"指纹"，用于跳过完全相同的重建
    rendered_signatures = {},
    -- 每个窗口当前的"全量基线"：tree 中已铺好所有当前 current_rows，
    -- 后续过滤只切换 Hidden 而无需重建。
    -- 结构：{dataset_revision, total_rendered, hide_supported}
    tree_baselines = {}
}

local GATED_ACTION_IDS = {
    "BatchReplaceBtn",
    "BtnStep1", "BtnStep2", "BtnStep3", "BtnStep4",
    "BtnStep5", "BtnStep6", "BtnStep7", "BtnStep8",
    "AIFixBtn"
}

local function find_ui_item(id)
    if not id then return nil end
    if win and win.Find then
        local ok, item = pcall(function() return win:Find(id) end)
        if ok and item then return item end
    end
    if AIConfigPopWin and AIConfigPopWin.Find then
        local ok, item = pcall(function() return AIConfigPopWin:Find(id) end)
        if ok and item then return item end
    end
    return nil
end

local function resolve_window(target_window)
    return target_window or active_window or mini_win or win
end

local function is_mini_window(target_window)
    return mini_win ~= nil and target_window == mini_win
end

local function get_window_control_id(target_window, full_id, mini_id)
    if is_mini_window(target_window) then
        return mini_id or full_id
    end
    return full_id
end

local function find_window_item(target_window, full_id, mini_id)
    local window = resolve_window(target_window)
    if not window or not window.Find then return nil end

    local id = get_window_control_id(window, full_id, mini_id)
    if not id then return nil end

    local ok, item = pcall(function() return window:Find(id) end)
    if ok and item then return item end
    return nil
end

local switch_stack_page
local switch_stack_page_index_only

local function set_window_status_text(target_window, text)
    local label = find_window_item(target_window, "StatusLabel", "MiniStatusLabel")
    if label and text ~= nil then
        pcall(function() label.Text = text end)
    end
end

local function update_shared_status(target_window, text)
    if text and text ~= "" then
        shared_status_text = text
    end
    set_window_status_text(target_window, shared_status_text)
end

local function set_gated_actions_enabled(enabled)
    for _, id in ipairs(GATED_ACTION_IDS) do
        local item = find_ui_item(id)
        if item then
            pcall(function() item.Enabled = enabled end)
        end
    end
end

local function set_load_status_label(is_loaded, text, target_window)
    -- 完整版的 LoadStatusLabel 已删除（底部「已加载 N 条」更准确）。
    -- 极简版仍保留 MiniLoadStatusLabel 提醒用户当前状态。
    if not is_mini_window(target_window) then
        return
    end

    local label = find_window_item(target_window, "MiniLoadStatusLabel")
    if not label then return end

    local html_text = text
    if html_text and html_text ~= "" then
        if html_text:find("正在刷新", 1, true) then
            html_text = "<font color='#FA8C16'>⏳ 刷新中</font>"
        elseif html_text:find("请先刷新字幕", 1, true) then
            html_text = "<font color='#FF4D4F'>⚠️ 未刷新</font>"
        elseif html_text:find("字幕已加载", 1, true) then
            html_text = "<font color='#00AA55'>✅ 已加载</font>"
        end
    end
    if not html_text or html_text == "" then
        if is_loaded then
            html_text = "<font color='#00AA55'>✅ 已加载</font>"
        else
            html_text = "<font color='#FF4D4F'>⚠️ 未刷新</font>"
        end
    end

    pcall(function() label.Text = html_text end)
end

local function set_subtitle_loaded_state(is_loaded, status_text, target_window)
    is_subtitle_loaded = is_loaded
    set_gated_actions_enabled(is_loaded)
    if is_loaded then
        set_load_status_label(true, status_text or "<font color='#00AA55'>✅ 字幕已加载</font>", target_window)
    else
        set_load_status_label(false, status_text or "<font color='#FF4D4F'>⚠️ 请先刷新字幕</font>", target_window)
    end
end

local function set_mini_subtitle_area_state(target_window, show_tree, message)
    local window = resolve_window(target_window)
    if not window or not is_mini_window(window) then
        return
    end

    local stack = find_window_item(window, "MiniSubtitleAreaStack")
    local placeholder_label = find_window_item(window, "MiniSubtitlePlaceholderLabel")
    if placeholder_label and message and message ~= "" then
        pcall(function() placeholder_label.Text = tostring(message) end)
    end
    if stack then
        switch_stack_page_index_only(window, "MiniSubtitleAreaStack", show_tree and MINI_SUBTITLE_TREE_INDEX or MINI_SUBTITLE_PLACEHOLDER_INDEX)
    end
end

local function update_target_track_hint()
    return
end

local function sync_target_track_control()
    local itms = win and win:GetItems()
    local input = itms and itms.TargetTrackSpin
    if input then
        pcall(function()
            input.Text = tostring(current_subtitle_target_track or 1)
        end)
    end
end

local function set_target_track_value(new_value, should_log)
    local parsed = tonumber(new_value) or current_subtitle_target_track or 1
    parsed = math.max(1, math.min(10, math.floor(parsed)))
    current_subtitle_target_track = parsed
    sync_target_track_control()
    update_target_track_hint()

    if should_log then
        local msg = "更新时间线目标轨已设置为 " .. tostring(current_subtitle_target_track)
        print("[Hooper AI 2.0] " .. msg)
        LogMsg(msg)

        local status = win and win:Find("StatusLabel")
        if status then status:Set("Text", msg) end
    end
end

-- ========== API 配置持久化 ==========
local config_path = (os.getenv("HOME") or os.getenv("USERPROFILE") or "") .. "/.hooperai_config.txt"
local recommended_config_path = (os.getenv("HOME") or os.getenv("USERPROFILE") or "") .. "/.hooperai_config_recommended.txt"
local custom_config_path = (os.getenv("HOME") or os.getenv("USERPROFILE") or "") .. "/.hooperai_config_custom.txt"
local json_config_path = (os.getenv("HOME") or os.getenv("USERPROFILE") or "") .. "/.hooperai_config.json"

local function json_escape_string(str)
    local value = tostring(str or "")
    value = value:gsub("\\", "\\\\")
    value = value:gsub('"', '\\"')
    value = value:gsub("\b", "\\b")
    value = value:gsub("\f", "\\f")
    value = value:gsub("\n", "\\n")
    value = value:gsub("\r", "\\r")
    value = value:gsub("\t", "\\t")
    return '"' .. value .. '"'
end

local function json_is_array(tbl)
    if type(tbl) ~= "table" then
        return false
    end

    local max_index = 0
    local count = 0
    for key, _ in pairs(tbl) do
        if type(key) ~= "number" or key < 1 or key ~= math.floor(key) then
            return false
        end
        if key > max_index then
            max_index = key
        end
        count = count + 1
    end

    return count == max_index
end

local function json_encode_value(value)
    local value_type = type(value)
    if value_type == "nil" then
        return "null"
    elseif value_type == "string" then
        return json_escape_string(value)
    elseif value_type == "number" then
        if value ~= value or value == math.huge or value == -math.huge then
            return "null"
        end
        return tostring(value)
    elseif value_type == "boolean" then
        return value and "true" or "false"
    elseif value_type == "table" then
        if json_is_array(value) then
            local parts = {}
            for i = 1, #value do
                parts[#parts + 1] = json_encode_value(value[i])
            end
            return "[" .. table.concat(parts, ",") .. "]"
        end

        local keys = {}
        for key, _ in pairs(value) do
            keys[#keys + 1] = tostring(key)
        end
        table.sort(keys)

        local parts = {}
        for _, key in ipairs(keys) do
            parts[#parts + 1] = json_escape_string(key) .. ":" .. json_encode_value(value[key])
        end
        return "{" .. table.concat(parts, ",") .. "}"
    end

    return "null"
end

local function decode_json_text(json_text)
    if type(json_text) ~= "string" or json_text == "" then
        return nil, "JSON 为空或不是字符串"
    end

    local pos = 1
    local json_len = #json_text
    local parse_value

    local function fail(msg)
        error(msg .. "（位置 " .. tostring(pos) .. "）", 0)
    end

    local function skip_whitespace()
        while pos <= json_len do
            local ch = json_text:sub(pos, pos)
            if ch == " " or ch == "\n" or ch == "\r" or ch == "\t" then
                pos = pos + 1
            else
                break
            end
        end
    end

    local function codepoint_to_utf8(code)
        if code <= 127 then
            return string.char(code)
        elseif code <= 2047 then
            local byte1 = 192 + math.floor(code / 64)
            local byte2 = 128 + (code % 64)
            return string.char(byte1, byte2)
        elseif code <= 65535 then
            local byte1 = 224 + math.floor(code / 4096)
            local byte2 = 128 + (math.floor(code / 64) % 64)
            local byte3 = 128 + (code % 64)
            return string.char(byte1, byte2, byte3)
        elseif code <= 1114111 then
            local byte1 = 240 + math.floor(code / 262144)
            local byte2 = 128 + (math.floor(code / 4096) % 64)
            local byte3 = 128 + (math.floor(code / 64) % 64)
            local byte4 = 128 + (code % 64)
            return string.char(byte1, byte2, byte3, byte4)
        end

        return ""
    end

    local function parse_string()
        if json_text:sub(pos, pos) ~= '"' then
            fail("JSON 字符串必须以双引号开始")
        end

        pos = pos + 1
        local parts = {}
        local chunk_start = pos

        while pos <= json_len do
            local ch = json_text:sub(pos, pos)
            if ch == '"' then
                if pos > chunk_start then
                    table.insert(parts, json_text:sub(chunk_start, pos - 1))
                end
                pos = pos + 1
                return table.concat(parts)
            elseif ch == "\\" then
                if pos > chunk_start then
                    table.insert(parts, json_text:sub(chunk_start, pos - 1))
                end

                local esc = json_text:sub(pos + 1, pos + 1)
                if esc == "" then
                    fail("JSON 字符串转义不完整")
                elseif esc == '"' or esc == "\\" or esc == "/" then
                    table.insert(parts, esc)
                    pos = pos + 2
                elseif esc == "b" then
                    table.insert(parts, "\b")
                    pos = pos + 2
                elseif esc == "f" then
                    table.insert(parts, "\f")
                    pos = pos + 2
                elseif esc == "n" then
                    table.insert(parts, "\n")
                    pos = pos + 2
                elseif esc == "r" then
                    table.insert(parts, "\r")
                    pos = pos + 2
                elseif esc == "t" then
                    table.insert(parts, "\t")
                    pos = pos + 2
                elseif esc == "u" then
                    local hex = json_text:sub(pos + 2, pos + 5)
                    if #hex < 4 or not hex:match("^[0-9a-fA-F]+$") then
                        fail("JSON Unicode 转义无效")
                    end

                    local code = tonumber(hex, 16)
                    pos = pos + 6

                    if code >= 55296 and code <= 56319 and json_text:sub(pos, pos + 1) == "\\u" then
                        local low_hex = json_text:sub(pos + 2, pos + 5)
                        local low_code = low_hex:match("^[0-9a-fA-F]+$") and tonumber(low_hex, 16) or nil
                        if low_code and low_code >= 56320 and low_code <= 57343 then
                            code = 65536 + (code - 55296) * 1024 + (low_code - 56320)
                            pos = pos + 6
                        end
                    end

                    table.insert(parts, codepoint_to_utf8(code))
                else
                    fail("遇到不支持的 JSON 转义字符")
                end

                chunk_start = pos
            else
                local byte = string.byte(json_text, pos)
                if byte and byte < 32 then
                    fail("JSON 字符串包含非法控制字符")
                end
                pos = pos + 1
            end
        end

        fail("JSON 字符串未正确闭合")
    end

    local function parse_number()
        local tail = json_text:sub(pos)
        local number_text = tail:match("^%-?%d+%.%d+[eE][%+%-]?%d+")
            or tail:match("^%-?%d+%.%d+")
            or tail:match("^%-?%d+[eE][%+%-]?%d+")
            or tail:match("^%-?%d+")

        if not number_text then
            fail("JSON 数字格式无效")
        end

        local value = tonumber(number_text)
        if not value then
            fail("JSON 数字无法转换")
        end

        pos = pos + #number_text
        return value
    end

    local function parse_array()
        pos = pos + 1
        skip_whitespace()

        local result = {}
        if json_text:sub(pos, pos) == "]" then
            pos = pos + 1
            return result
        end

        while true do
            result[#result + 1] = parse_value()
            skip_whitespace()

            local ch = json_text:sub(pos, pos)
            if ch == "," then
                pos = pos + 1
                skip_whitespace()
            elseif ch == "]" then
                pos = pos + 1
                break
            else
                fail("JSON 数组缺少逗号或右中括号")
            end
        end

        return result
    end

    local function parse_object()
        pos = pos + 1
        skip_whitespace()

        local result = {}
        if json_text:sub(pos, pos) == "}" then
            pos = pos + 1
            return result
        end

        while true do
            skip_whitespace()
            if json_text:sub(pos, pos) ~= '"' then
                fail("JSON 对象键必须是字符串")
            end

            local key = parse_string()
            skip_whitespace()
            if json_text:sub(pos, pos) ~= ":" then
                fail("JSON 对象键值之间缺少冒号")
            end

            pos = pos + 1
            result[key] = parse_value()
            skip_whitespace()

            local ch = json_text:sub(pos, pos)
            if ch == "," then
                pos = pos + 1
                skip_whitespace()
            elseif ch == "}" then
                pos = pos + 1
                break
            else
                fail("JSON 对象缺少逗号或右大括号")
            end
        end

        return result
    end

    parse_value = function()
        skip_whitespace()
        local ch = json_text:sub(pos, pos)

        if ch == "" then
            fail("JSON 提前结束")
        elseif ch == '"' then
            return parse_string()
        elseif ch == "{" then
            return parse_object()
        elseif ch == "[" then
            return parse_array()
        elseif ch == "t" and json_text:sub(pos, pos + 3) == "true" then
            pos = pos + 4
            return true
        elseif ch == "f" and json_text:sub(pos, pos + 4) == "false" then
            pos = pos + 5
            return false
        elseif ch == "n" and json_text:sub(pos, pos + 3) == "null" then
            pos = pos + 4
            return nil
        elseif ch == "-" or ch:match("%d") then
            return parse_number()
        end

        fail("无法识别的 JSON 值")
    end

    local ok, result = pcall(function()
        skip_whitespace()
        local value = parse_value()
        skip_whitespace()
        if pos <= json_len then
            fail("JSON 尾部存在多余内容")
        end
        return value
    end)

    if ok then
        return result
    end

    return nil, tostring(result)
end

AI_PROVIDER_DEFS = {
    {
        id = "mediastorm_gateway",
        label = "MediaStorm Gateway",
        api_url = "https://ai-gateway.mediastorm.studio",
        default_model = "Gemini 3.1 Pro Preview",
        is_custom = false,
        allow_base_url = true,
        protocol = "openai_compatible"
    },
    {
        id = "siliconflow",
        label = "SiliconFlow",
        api_url = "https://api.siliconflow.cn/v1/chat/completions",
        default_model = "deepseek-ai/DeepSeek-V3.2",
        is_custom = false,
        protocol = "openai_compatible",
        api_key_url = "https://cloud.siliconflow.cn/me/account/ak"
    },
    {
        id = "deepseek",
        label = "DeepSeek",
        api_url = "https://api.deepseek.com/chat/completions",
        default_model = "deepseek-chat",
        is_custom = false,
        protocol = "openai_compatible",
        api_key_url = "https://platform.deepseek.com/api-keys"
    },
    {
        id = "openai",
        label = "OpenAI",
        api_url = "https://api.openai.com/v1/chat/completions",
        default_model = "gpt-4o-mini",
        is_custom = false,
        protocol = "openai_compatible",
        api_key_url = "https://platform.openai.com/api-keys"
    },
    {
        id = "gemini",
        label = "Google Gemini",
        api_url = "https://generativelanguage.googleapis.com/v1beta",
        default_model = "gemini-2.5-flash",
        is_custom = false,
        protocol = "gemini_native",
        api_key_url = "https://aistudio.google.com/app/apikey"
    },
    {
        id = "custom_openai",
        label = "自定义（OpenAI兼容）",
        api_url = "",
        default_model = "",
        is_custom = true,
        protocol = "openai_compatible"
    }
}

AI_PROVIDER_BY_ID = {}
for _, provider in ipairs(AI_PROVIDER_DEFS) do
    AI_PROVIDER_BY_ID[provider.id] = provider
end

function get_provider_def(provider_id)
    local normalized_id = tostring(provider_id or "")
    if normalized_id == "dashscope" then
        normalized_id = "gemini"
    end
    return AI_PROVIDER_BY_ID[normalized_id] or AI_PROVIDER_BY_ID[AI_PROVIDER_DEFS[1].id]
end

function normalize_provider_id(provider_id)
    local normalized_id = tostring(provider_id or "")
    if normalized_id == "dashscope" then
        return "gemini"
    end
    if AI_PROVIDER_BY_ID[normalized_id] then
        return normalized_id
    end
    return AI_PROVIDER_DEFS[1].id
end

function get_provider_id_by_index(idx)
    local numeric_idx = tonumber(idx) or 0
    local provider = AI_PROVIDER_DEFS[numeric_idx + 1]
    if provider and provider.id then
        return provider.id
    end
    return AI_PROVIDER_DEFS[1].id
end

function get_provider_index_by_id(provider_id)
    local target_id = normalize_provider_id(provider_id)
    for idx, provider in ipairs(AI_PROVIDER_DEFS) do
        if provider.id == target_id then
            return idx - 1
        end
    end
    return 0
end

local function get_config_path_for_preset(preset_key)
    if preset_key == "custom" then
        return custom_config_path
    end
    return recommended_config_path
end

function provider_allows_api_url_edit(provider_def)
    return provider_def and (provider_def.is_custom == true or provider_def.allow_base_url == true)
end

function build_default_provider_config(provider_id)
    local provider_def = get_provider_def(provider_id)
    return {
        api_url = tostring(provider_def.api_url or ""),
        api_key = "",
        model = tostring(provider_def.default_model or "")
    }
end

function normalize_provider_config(config, provider_id)
    local provider_def = get_provider_def(provider_id)
    local source = type(config) == "table" and config or {}
    local normalized = build_default_provider_config(provider_def.id)
    normalized.api_key = tostring(source.api_key or "")

    local saved_model = tostring(source.model or "")
    if saved_model ~= "" then
        normalized.model = saved_model
    end

    if provider_allows_api_url_edit(provider_def) then
        local saved_api_url = tostring(source.api_url or "")
        if saved_api_url ~= "" then
            normalized.api_url = saved_api_url
        end
    else
        normalized.api_url = tostring(provider_def.api_url or "")
    end

    return normalized
end

local function build_default_shared_config()
    return {
        script_content = "",
        is_script_enabled = false
    }
end

local function normalize_shared_config(script_content, is_script_enabled)
    return {
        script_content = tostring(script_content or ""),
        is_script_enabled = is_script_enabled == true
    }
end

local function build_default_config_store()
    local providers = {}
    for _, provider_def in ipairs(AI_PROVIDER_DEFS) do
        providers[provider_def.id] = build_default_provider_config(provider_def.id)
    end

    return {
        version = 4,
        active_provider = AI_PROVIDER_DEFS[1].id,
        providers = providers,
        shared = build_default_shared_config()
    }
end

local function normalize_config_store(store)
    local normalized = build_default_config_store()
    if type(store) ~= "table" then
        return normalized
    end

    local provider_map = type(store.providers) == "table" and store.providers or {}
    local legacy_dashscope_config = type(provider_map.dashscope) == "table" and provider_map.dashscope or nil
    for _, provider_def in ipairs(AI_PROVIDER_DEFS) do
        local source_config = provider_map[provider_def.id]
        if source_config == nil and provider_def.id == "gemini" and legacy_dashscope_config then
            -- dashscope 与 Gemini 协议不兼容：迁移槽位但不继承旧 Key，避免错误复用。
            source_config = {
                api_url = provider_def.api_url,
                api_key = "",
                model = tostring(provider_def.default_model or "")
            }
        end
        normalized.providers[provider_def.id] = normalize_provider_config(source_config, provider_def.id)
    end

    local active_provider = normalize_provider_id(store.active_provider)
    if AI_PROVIDER_BY_ID[active_provider] then
        normalized.active_provider = active_provider
    end

    local shared = type(store.shared) == "table" and store.shared or {}
    normalized.shared = normalize_shared_config(
        shared.script_content,
        shared.is_script_enabled
    )

    return normalized
end

local function read_text_file(path)
    if type(path) ~= "string" or path == "" then
        return nil
    end

    local file = io.open(path, "r")
    if not file then
        return nil
    end

    local content = file:read("*a")
    file:close()
    return content
end

function file_exists(path)
    if type(path) ~= "string" or path == "" then
        return false
    end

    local file = io.open(path, "r")
    if not file then
        return false
    end

    file:close()
    return true
end

function has_legacy_config_files()
    return file_exists(config_path)
        or file_exists(recommended_config_path)
        or file_exists(custom_config_path)
end

local write_config_store

local function load_legacy_preset_config(preset_key)
    local active_preset = preset_key or "recommended"
    local active_config_path = get_config_path_for_preset(active_preset)
    local f = io.open(active_config_path, "r")
    if f then
        local lines = {}
        for line in f:lines() do
            table.insert(lines, line)
        end
        f:close()
        print("[Hooper AI 2.0] 已加载旧配置文件: " .. active_config_path)
        if active_preset == "custom" then
            return {
                api_url = tostring(lines[1] or ""),
                api_key = tostring(lines[2] or ""),
                model = tostring(lines[3] or "")
            }
        end

        return {
            api_url = "https://api.siliconflow.cn/v1/chat/completions",
            api_key = tostring(lines[2] or ""),
            model = "deepseek-ai/DeepSeek-V3.2"
        }
    end

    local legacy = active_preset == "recommended" and io.open(config_path, "r") or nil
    if legacy then
        local lines = {}
        for line in legacy:lines() do
            table.insert(lines, line)
        end
        legacy:close()

        print("[Hooper AI 2.0] 使用旧配置初始化 " .. active_preset .. " 预设: " .. config_path)
        return {
            api_url = "https://api.siliconflow.cn/v1/chat/completions",
            api_key = tostring(lines[2] or ""),
            model = "deepseek-ai/DeepSeek-V3.2"
        }
    end

    if active_preset == "custom" then
        return build_default_provider_config("custom_openai")
    end
    return build_default_provider_config("siliconflow")
end

local function migrate_legacy_config_store()
    local store = build_default_config_store()
    store.providers.siliconflow = normalize_provider_config(load_legacy_preset_config("recommended"), "siliconflow")
    store.providers.custom_openai = normalize_provider_config(load_legacy_preset_config("custom"), "custom_openai")
    if has_legacy_config_files() then
        store.active_provider = "siliconflow"
    end
    return normalize_config_store(store)
end

local function load_config_store()
    local json_content = read_text_file(json_config_path)
    if json_content and tostring(json_content):match("%S") then
        local decoded, decode_err = decode_json_text(json_content)
        if decoded and type(decoded) == "table" then
            print("[Hooper AI 2.0] 已加载 JSON 配置文件: " .. json_config_path)
            if type(decoded.providers) == "table" then
                return normalize_config_store(decoded)
            end

            local migrated = build_default_config_store()
            local recommended = type(decoded.recommended) == "table" and decoded.recommended or {}
            local custom = type(decoded.custom) == "table" and decoded.custom or {}
            migrated.providers.siliconflow = normalize_provider_config(recommended, "siliconflow")
            migrated.providers.custom_openai = normalize_provider_config(custom, "custom_openai")
            migrated.shared = normalize_shared_config(
                decoded.shared and decoded.shared.script_content,
                decoded.shared and decoded.shared.is_script_enabled
            )
            migrated.active_provider = "siliconflow"
            write_config_store(migrated)
            return normalize_config_store(migrated)
        end

        print("[Hooper AI 2.0] JSON 配置解析失败，回退旧配置: " .. tostring(decode_err))
    end

    local migrated = migrate_legacy_config_store()
    write_config_store(migrated)
    return migrated
end

write_config_store = function(store)
    local normalized_store = normalize_config_store(store)
    local file = io.open(json_config_path, "w")
    if not file then
        print("[Hooper AI 2.0] 警告：无法保存 JSON 配置文件")
        return false
    end

    file:write(json_encode_value(normalized_store))
    file:close()
    print("[Hooper AI 2.0] 配置已保存到: " .. json_config_path)
    return true
end

-- 加载 API 配置
local function LoadConfig(provider_id)
    local store = load_config_store()
    local active_provider = get_provider_def(provider_id).id
    return normalize_provider_config(store.providers[active_provider], active_provider)
end

local function LoadSharedConfig()
    local store = load_config_store()
    return normalize_shared_config(
        store.shared.script_content,
        store.shared.is_script_enabled
    )
end

-- 保存某个 provider 的配置，不改变当前 active_provider
local function SaveProviderConfig(provider_id, config)
    local active_provider = get_provider_def(provider_id).id
    local store = load_config_store()
    store.providers[active_provider] = normalize_provider_config(config, active_provider)
    write_config_store(store)
end

local function SaveSharedConfig(script_content, is_script_enabled)
    local store = load_config_store()
    store.shared = normalize_shared_config(script_content, is_script_enabled)
    write_config_store(store)
end

function LoadActiveProviderId()
    local store = load_config_store()
    return normalize_provider_id(store.active_provider)
end

function SaveActiveProviderId(provider_id)
    local store = load_config_store()
    store.active_provider = normalize_provider_id(provider_id)
    write_config_store(store)
end

-- ========== 备份路径初始化 ==========
current_backup_path = ""
BackupFileMap = {}
BackupHistoryEntries = {}
undo_stack = {}
redo_stack = {}
history_window = nil
history_window_items = nil
suppress_backup_restore_events = false
backup_selector_dirty = false
BACKUP_SELECTOR_PLACEHOLDER_TEXT = "选择历史备份..."
PREVIEW_SOURCE_TIMELINE = "timeline"
PREVIEW_SOURCE_HISTORY = "history"
current_preview_source = PREVIEW_SOURCE_TIMELINE
current_history_entry_filename = ""
local MINI_SUBTITLE_PLACEHOLDER_INDEX = 0
local MINI_SUBTITLE_TREE_INDEX = 1
BACKUP_HISTORY_LIMIT = 20
BACKUP_HISTORY_STORE_LIMIT = 200
BACKUP_HISTORY_MANIFEST = "_backup_history_manifest.tsv"

function clone_table(value)
    if type(value) ~= "table" then
        return value
    end

    local copied = {}
    for key, item in pairs(value) do
        copied[key] = clone_table(item)
    end
    return copied
end

function join_path(dir_path, leaf_name)
    local dir_value = tostring(dir_path or "")
    local name_value = tostring(leaf_name or "")
    if dir_value == "" then
        return name_value
    end
    if name_value == "" then
        return dir_value
    end
    local tail = dir_value:sub(-1)
    if tail == "/" or tail == "\\" then
        return dir_value .. name_value
    end
    return dir_value .. "/" .. name_value
end

function ensure_backup_directory()
    if current_backup_path == "" then return end
    os.execute('mkdir -p "' .. current_backup_path .. '" 2>/dev/null')
    os.execute('mkdir "' .. current_backup_path .. '" 2>nul')
end

function get_backup_manifest_path()
    return join_path(current_backup_path, BACKUP_HISTORY_MANIFEST)
end

function escape_manifest_field(value)
    local text = tostring(value or "")
    text = text:gsub("\\", "\\\\")
    text = text:gsub("\t", "\\t")
    text = text:gsub("\n", "\\n")
    text = text:gsub("\r", "\\r")
    return text
end

function unescape_manifest_field(value)
    local text = tostring(value or "")
    text = text:gsub("\\t", "\t")
    text = text:gsub("\\n", "\n")
    text = text:gsub("\\r", "\r")
    text = text:gsub("\\\\", "\\")
    return text
end

function split_tab_fields(line)
    local fields = {}
    local payload = tostring(line or "") .. "\t"
    for field in payload:gmatch("(.-)\t") do
        fields[#fields + 1] = field
    end
    return fields
end

function is_history_backup_filename(path)
    local filename = get_backup_display_name(path)
    return filename:match("^Backup_.*%.srt$") ~= nil
end

function list_backup_files(limit)
    if current_backup_path == "" then
        return {}
    end

    local file_limit = tonumber(limit) or BACKUP_HISTORY_LIMIT
    local files = {}
    local handle = nil
    if package.config:sub(1,1) == "\\" then
        handle = io.popen('dir /b /o-d "' .. current_backup_path .. '\\*.srt" 2>nul')
    else
        handle = io.popen('ls -t "' .. current_backup_path .. '"/*.srt 2>/dev/null | head -' .. tostring(file_limit))
    end
    if not handle then
        return files
    end

    for line in handle:lines() do
        local file_path = tostring(line or ""):gsub("\r", "")
        if file_path ~= "" then
            if package.config:sub(1,1) == "\\" and not file_path:match("^[A-Za-z]:[\\/]")
                and not file_path:match("^[/\\]")
            then
                file_path = join_path(current_backup_path, file_path)
            end
            if is_history_backup_filename(file_path) then
                files[#files + 1] = file_path
                if #files >= file_limit then
                    break
                end
            end
        end
    end
    handle:close()
    return files
end

function get_backup_display_name(path)
    local value = tostring(path or "")
    local filename = value:match("([^/\\]+)$")
    if filename and filename ~= "" then
        return filename
    end
    return value
end

function format_history_display_name(entry)
    local created_at = tostring(entry and entry.created_at or "")
    local action_label = tostring(entry and entry.action_label or "")
    if created_at ~= "" and action_label ~= "" then
        return created_at .. " · " .. action_label
    end
    if created_at ~= "" then
        return created_at
    end
    if action_label ~= "" then
        return action_label
    end
    return get_backup_display_name(entry and entry.full_path)
end

function load_backup_manifest_records()
    local records = {}
    if current_backup_path == "" then
        return records
    end

    local manifest_file = io.open(get_backup_manifest_path(), "r")
    if not manifest_file then
        return records
    end

    for line in manifest_file:lines() do
        local fields = split_tab_fields(line)
        if #fields >= 5 then
            local filename = unescape_manifest_field(fields[1])
            if filename ~= "" then
                records[filename] = {
                    filename = filename,
                    created_at = unescape_manifest_field(fields[2]),
                    action_label = unescape_manifest_field(fields[3]),
                    track = tonumber(unescape_manifest_field(fields[4])) or current_track,
                    row_count = tonumber(unescape_manifest_field(fields[5])) or 0
                }
            end
        end
    end

    manifest_file:close()
    return records
end

function write_backup_manifest_entries(entries)
    if current_backup_path == "" then
        return false
    end

    ensure_backup_directory()
    local manifest_file = io.open(get_backup_manifest_path(), "w")
    if not manifest_file then
        return false
    end

    local written = 0
    for _, entry in ipairs(entries or {}) do
        local filename = tostring(entry and entry.filename or "")
        if filename ~= "" then
            manifest_file:write(table.concat({
                escape_manifest_field(filename),
                escape_manifest_field(entry.created_at),
                escape_manifest_field(entry.action_label),
                escape_manifest_field(entry.track),
                escape_manifest_field(entry.row_count)
            }, "\t"))
            manifest_file:write("\n")
            written = written + 1
            if written >= BACKUP_HISTORY_STORE_LIMIT then
                break
            end
        end
    end

    manifest_file:close()
    return true
end

function refresh_backup_history_cache(limit)
    local file_limit = tonumber(limit) or BACKUP_HISTORY_LIMIT
    local files = list_backup_files(file_limit)
    local manifest_records = load_backup_manifest_records()
    local seen_display_names = {}

    BackupFileMap = {}
    BackupHistoryEntries = {}

    for _, full_path in ipairs(files) do
        local filename = get_backup_display_name(full_path)
        local manifest_entry = manifest_records[filename] or {}
        local created_at = tostring(manifest_entry.created_at or "")
        local action_label = tostring(manifest_entry.action_label or "")

        if created_at == "" then
            local date_part, time_part = filename:match("Backup_(%d%d%d%d%d%d%d%d)_(%d%d%d%d%d%d)")
            if date_part and time_part then
                created_at = string.format(
                    "%s-%s-%s %s:%s:%s",
                    date_part:sub(1, 4),
                    date_part:sub(5, 6),
                    date_part:sub(7, 8),
                    time_part:sub(1, 2),
                    time_part:sub(3, 4),
                    time_part:sub(5, 6)
                )
            end
        end
        if action_label == "" then
            action_label = filename
        end

        local entry = {
            filename = filename,
            full_path = full_path,
            created_at = created_at,
            action_label = action_label,
            track = tonumber(manifest_entry.track) or current_track,
            row_count = tonumber(manifest_entry.row_count) or 0
        }

        local display_name = format_history_display_name(entry)
        local display_count = (seen_display_names[display_name] or 0) + 1
        seen_display_names[display_name] = display_count
        if display_count > 1 then
            display_name = string.format("%s (%d)", display_name, display_count)
        end
        entry.display_name = display_name

        BackupHistoryEntries[#BackupHistoryEntries + 1] = entry
        BackupFileMap[display_name] = full_path
    end
end

function sync_backup_path_display()
    return
end

function set_current_preview_source(source, history_entry)
    if source == PREVIEW_SOURCE_HISTORY then
        current_preview_source = PREVIEW_SOURCE_HISTORY
        if type(history_entry) == "table" then
            current_history_entry_filename = tostring(history_entry.filename or "")
        else
            current_history_entry_filename = tostring(history_entry or "")
        end
        return
    end

    current_preview_source = PREVIEW_SOURCE_TIMELINE
    current_history_entry_filename = ""
end

function reset_backup_selector_to_placeholder()
    local combo = win and win:Find("BackupPathInput")
    if not combo then
        return false
    end

    local ok_count, combo_count = pcall(function() return combo:Count() end)
    if not ok_count or tonumber(combo_count) == nil or tonumber(combo_count) <= 0 then
        return false
    end

    suppress_backup_restore_events = true
    pcall(function() combo.CurrentIndex = 0 end)
    suppress_backup_restore_events = false
    return true
end

function repopulate_backup_combo(combo, preferred_display_name, options)
    if not combo then return end
    options = options or {}
    local preserve_current_selection = options.preserve_current_selection ~= false
    local selected_display_name = ""
    local selected_filename = ""

    if current_preview_source == PREVIEW_SOURCE_HISTORY then
        selected_display_name = tostring(preferred_display_name or "")
        selected_filename = tostring(options.preferred_filename or current_history_entry_filename or "")
        if preserve_current_selection and selected_display_name == "" then
            local current_text = tostring(combo.CurrentText or "")
            if current_text ~= "" and current_text ~= BACKUP_SELECTOR_PLACEHOLDER_TEXT then
                selected_display_name = current_text
            end
        end
    end

    suppress_backup_restore_events = true
    combo:Clear()
    combo:AddItem(BACKUP_SELECTOR_PLACEHOLDER_TEXT)
    local target_index = 0
    for idx, entry in ipairs(BackupHistoryEntries or {}) do
        combo:AddItem(entry.display_name or get_backup_display_name(entry.full_path))
        if current_preview_source == PREVIEW_SOURCE_HISTORY then
            if selected_filename ~= "" and tostring(entry.filename or "") == selected_filename then
                target_index = idx
            elseif selected_display_name ~= "" and entry.display_name == selected_display_name then
                target_index = idx
            end
        end
    end
    if combo:Count() > 0 then
        combo.CurrentIndex = target_index
    end
    suppress_backup_restore_events = false
end

local function mark_backup_selector_dirty()
    backup_selector_dirty = true
end

local function refresh_backup_selector_now(preferred_display_name, options)
    refresh_backup_history_cache(BACKUP_HISTORY_LIMIT)
    local refreshed = sync_backup_selector(preferred_display_name, options)
    if not refreshed then
        mark_backup_selector_dirty()
        return false
    end
    return true
end

local function ensure_backup_selector_fresh(preferred_display_name, options)
    if not backup_selector_dirty then
        return false
    end
    return refresh_backup_selector_now(preferred_display_name, options)
end

function append_backup_manifest_entry(entry)
    if type(entry) ~= "table" or tostring(entry.filename or "") == "" then
        return false
    end

    local files = list_backup_files(BACKUP_HISTORY_STORE_LIMIT)
    local manifest_records = load_backup_manifest_records()
    local next_entries = {
        {
            filename = entry.filename,
            created_at = entry.created_at,
            action_label = entry.action_label,
            track = entry.track,
            row_count = entry.row_count
        }
    }
    local seen = {
        [tostring(entry.filename)] = true
    }

    for _, full_path in ipairs(files) do
        local filename = get_backup_display_name(full_path)
        if not seen[filename] then
            local manifest_entry = manifest_records[filename]
            next_entries[#next_entries + 1] = {
                filename = filename,
                created_at = manifest_entry and manifest_entry.created_at or "",
                action_label = manifest_entry and manifest_entry.action_label or filename,
                track = manifest_entry and manifest_entry.track or current_track,
                row_count = manifest_entry and manifest_entry.row_count or 0
            }
            seen[filename] = true
        end
        if #next_entries >= BACKUP_HISTORY_STORE_LIMIT then
            break
        end
    end

    return write_backup_manifest_entries(next_entries)
end

local home_dir = os.getenv("HOME") or os.getenv("USERPROFILE")
if home_dir then
    current_backup_path = home_dir .. "/Desktop/HooperAI_Backups"
else
    current_backup_path = "C:/HooperAI_Backups" -- Windows 最后的兜底
end
print("[Hooper AI 2.0] 默认备份路径: " .. current_backup_path)
print(string.format("[Hooper AI 2.0] [STARTUP] 全部函数定义完成: +%d ms", startup_elapsed_ms()))

-- ========== 帧 -> SRT 时间码（HH:MM:SS,mmm）==========
local function frames_to_srt_time(frames, fps)
    if not fps or fps == 0 then fps = 24.0 end
    local total = (tonumber(frames) or 0) / fps
    local h = math.floor(total / 3600)
    local m = math.floor((total % 3600) / 60)
    local s = math.floor(total % 60)
    local ms = math.floor((total - math.floor(total)) * 1000 + 0.5)
    if ms >= 1000 then s = s + 1; ms = ms - 1000 end
    return string.format("%02d:%02d:%02d,%03d", h, m, s, ms)
end

local function milliseconds_to_srt_time(total_ms)
    local ms_value = math.max(0, math.floor((tonumber(total_ms) or 0) + 0.5))
    local h = math.floor(ms_value / 3600000)
    local m = math.floor((ms_value % 3600000) / 60000)
    local s = math.floor((ms_value % 60000) / 1000)
    local ms = ms_value % 1000
    return string.format("%02d:%02d:%02d,%03d", h, m, s, ms)
end

-- ========== SRT 时间码 -> 帧 ==========
local function srt_time_to_frames(srt_time, fps)
    if not fps or fps == 0 then fps = 24.0 end
    local h, m, s, ms = srt_time:match("(%d+):(%d+):(%d+),(%d+)")
    if not h then return 0 end
    h, m, s, ms = tonumber(h), tonumber(m), tonumber(s), tonumber(ms)
    local total_seconds = h * 3600 + m * 60 + s + ms / 1000
    return math.floor(total_seconds * fps + 0.5)
end

-- ========== 备份函数 (全局) ==========
function DoBackup(action_desc, rows_override, options)
    options = options or {}
    if current_backup_path == "" then return end

    ensure_backup_directory()

    local filename = string.format(
        "Backup_%s_%03d.srt",
        os.date("%Y%m%d_%H%M%S"),
        math.floor((os.clock() % 1) * 1000)
    )
    local full_path = join_path(current_backup_path, filename)
    
    local source_rows = rows_override or current_rows

    -- 修复：优先使用 source_rows/current_rows，避免搜索过滤导致备份数据丢失
    if not source_rows or #source_rows == 0 then
        print("[Hooper AI 2.0] 没有字幕数据，跳过备份")
        return
    end
    
    local export_list = {}
    for _, row in ipairs(source_rows) do
        if type(row) == "table" and row.text then
            table.insert(export_list, row)
        end
    end
    
    -- 修复：按 start_frame 排序，确保备份时序正确
    table.sort(export_list, function(a, b)
        return (tonumber(a.start_frame) or 0) < (tonumber(b.start_frame) or 0)
    end)
    
    local srt_content = ""
    local index = 1
    for _, data in ipairs(export_list) do
        local start_tc = frames_to_srt_time(data.start_frame, data.fps or current_fps)
        local end_tc = frames_to_srt_time(data.end_frame, data.fps or current_fps)
        srt_content = srt_content .. index .. "\n"
        srt_content = srt_content .. start_tc .. " --> " .. end_tc .. "\n"
        srt_content = srt_content .. tostring(data.text) .. "\n\n"
        index = index + 1
    end
    
    local file = io.open(full_path, "w")
    if file then
        file:write(srt_content)
        file:close()
        
        local manifest_entry = {
            filename = filename,
            full_path = full_path,
            created_at = os.date("%Y-%m-%d %H:%M:%S"),
            action_label = tostring(action_desc or "自动备份"),
            track = tonumber(current_track) or 1,
            row_count = #export_list
        }

        append_backup_manifest_entry(manifest_entry)
        refresh_backup_history_cache(BACKUP_HISTORY_LIMIT)
        if options.defer_selector_refresh == true then
            mark_backup_selector_dirty()
        elseif not sync_backup_selector(options.preferred_display_name, options.selector_sync_options) then
            mark_backup_selector_dirty()
        end

        print("[Hooper AI 2.0] 成功备份: " .. manifest_entry.created_at .. " · " .. manifest_entry.action_label .. " -> " .. full_path)
    else
        print("[Hooper AI 2.0] 写入备份失败: " .. full_path)
    end
end

local function persist_timeline_update_backup(rows_override)
    return DoBackup("更新时间线前-内存字幕备份", rows_override)
end

-- ========== 帧率表 ==========
local EXACT_FPS = {
    ["23.976"] = 24000 / 1001,
    ["23.98"] = 24000 / 1001,
    ["29.97"] = 30000 / 1001,
    ["59.94"] = 60000 / 1001,
    ["30"] = 30.0,
    ["24"] = 24.0,
    ["25"] = 25.0,
    ["50"] = 50.0,
    ["60"] = 60.0,
}

local function trim(s)
    if not s then return "" end
    return (tostring(s):gsub("^%s*(.-)%s*$", "%1"))
end

local function trim_text(str)
    return trim(str)
end

local function get_utf8_fallback_char_len(byte_value)
    local byte = tonumber(byte_value)
    if not byte then
        return 1
    end
    if byte <= 0x7F then
        return 1
    end
    if byte >= 0xC2 and byte <= 0xDF then
        return 2
    end
    if byte >= 0xE0 and byte <= 0xEF then
        return 3
    end
    if byte >= 0xF0 and byte <= 0xF4 then
        return 4
    end
    return 1
end

local REFERENCE_SCRIPT_SOFT_LIMIT = 3000
local REFERENCE_SCRIPT_HIGH_LIMIT = 8000
local REFERENCE_SCRIPT_HARD_LIMIT = 10000

local function count_utf8_chars(str)
    local value = tostring(str or "")
    if value == "" then
        return 0
    end
    if utf8 and utf8.len then
        local ok, length = pcall(utf8.len, value)
        if ok and length then
            return length
        end
    end
    local count = 0
    local index = 1
    while index <= #value do
        count = count + 1
        index = index + get_utf8_fallback_char_len(string.byte(value, index))
    end
    if count > 0 then
        return count
    end
    return #value
end

local function sanitize_reference_script_text(text)
    local normalized = tostring(text or "")
    normalized = normalized:gsub("\r\n", "\n")
    normalized = normalized:gsub("\r", "\n")

    local lines = {}
    local previous_blank = false
    for line in (normalized .. "\n"):gmatch("(.-)\n") do
        local cleaned_line = tostring(line or ""):gsub("[ \t]+$", "")
        if cleaned_line:match("^%s*$") then
            if #lines > 0 and not previous_blank then
                lines[#lines + 1] = ""
            end
            previous_blank = true
        else
            lines[#lines + 1] = cleaned_line
            previous_blank = false
        end
    end

    while #lines > 0 and lines[#lines] == "" do
        table.remove(lines)
    end

    return trim_text(table.concat(lines, "\n"))
end

local function get_textedit_content(item)
    if not item then
        return ""
    end

    local readers = {
        function() return item.PlainText end,
        function() return item.Text end,
    }
    for _, reader in ipairs(readers) do
        local ok, value = pcall(reader)
        if ok and type(value) == "string" then
            return value
        end
    end
    return ""
end

local function set_textedit_content(item, value)
    if not item then
        return
    end

    local text = tostring(value or "")
    pcall(function() item.PlainText = text end)
    pcall(function() item.Text = text end)
end

local report_helpers = (function()
    local function set_textedit_rich_content(item, plain_value, html_value)
        if not item then
            return false
        end

        local plain_text = tostring(plain_value or "")
        local rich_html = tostring(html_value or "")
        if trim_text(rich_html) ~= "" then
            local ok = pcall(function() item.HTML = rich_html end)
            if ok then
                return true
            end
        end

        set_textedit_content(item, plain_text)
        return false
    end

    local function split_text_chars_for_diff(str)
        local chars = {}
        local value = tostring(str or "")
        if value == "" then
            return chars
        end

        if utf8 and utf8.codes and utf8.char then
            local ok = pcall(function()
                for _, codepoint in utf8.codes(value) do
                    chars[#chars + 1] = utf8.char(codepoint)
                end
            end)
            if ok then
                return chars
            end
        end

        local index = 1
        while index <= #value do
            local char_len = get_utf8_fallback_char_len(string.byte(value, index))
            chars[#chars + 1] = value:sub(index, index + char_len - 1)
            index = index + char_len
        end
        if #chars > 0 then
            return chars
        end

        for i = 1, #value do
            chars[#chars + 1] = value:sub(i, i)
        end
        return chars
    end

    local function truncate_text_chars(value, max_chars)
        local limit = math.max(0, tonumber(max_chars) or 0)
        local text = tostring(value or "")
        if limit <= 0 then
            return text
        end

        local chars = split_text_chars_for_diff(text)
        if #chars <= limit then
            return text
        end

        local out = {}
        for i = 1, limit do
            out[#out + 1] = chars[i]
        end
        return table.concat(out) .. "…"
    end

    local function escape_html_text(text)
        local value = tostring(text or "")
        value = value:gsub("&", "&amp;")
        value = value:gsub("<", "&lt;")
        value = value:gsub(">", "&gt;")
        value = value:gsub('"', "&quot;")
        value = value:gsub("'", "&#39;")
        value = value:gsub("\r\n", "\n")
        value = value:gsub("\r", "\n")
        value = value:gsub("\n", "<br/>")
        return value
    end

    local function append_diff_segment(segments, text, changed, placeholder)
        local safe_text = tostring(text or "")
        if safe_text == "" then
            return
        end

        local last = segments[#segments]
        if last and last.changed == (changed == true) and last.placeholder == (placeholder == true) then
            last.text = last.text .. safe_text
            return
        end

        segments[#segments + 1] = {
            text = safe_text,
            changed = changed == true,
            placeholder = placeholder == true
        }
    end

    local function build_char_diff_segments(original, updated)
        local original_text = tostring(original or "")
        local updated_text = tostring(updated or "")
        if original_text == updated_text then
            return {
                { type = "equal", original = original_text, updated = updated_text }
            }
        end

        local original_chars = split_text_chars_for_diff(original_text)
        local updated_chars = split_text_chars_for_diff(updated_text)
        local dp = {}

        for i = 0, #original_chars + 1 do
            dp[i] = {}
        end

        for i = #original_chars, 1, -1 do
            local row = dp[i]
            for j = #updated_chars, 1, -1 do
                if original_chars[i] == updated_chars[j] then
                    row[j] = 1 + ((dp[i + 1] and dp[i + 1][j + 1]) or 0)
                else
                    local skip_original = (dp[i + 1] and dp[i + 1][j]) or 0
                    local skip_updated = row[j + 1] or 0
                    row[j] = math.max(skip_original, skip_updated)
                end
            end
        end

        local ops = {}
        local function append_op(op_type, text)
            if text == "" then
                return
            end
            local last = ops[#ops]
            if last and last.op == op_type then
                last.text = last.text .. text
            else
                ops[#ops + 1] = { op = op_type, text = text }
            end
        end

        local i = 1
        local j = 1
        while i <= #original_chars and j <= #updated_chars do
            if original_chars[i] == updated_chars[j] then
                append_op("equal", original_chars[i])
                i = i + 1
                j = j + 1
            else
                local skip_original = (dp[i + 1] and dp[i + 1][j]) or 0
                local skip_updated = (dp[i] and dp[i][j + 1]) or 0
                if skip_original >= skip_updated then
                    append_op("delete", original_chars[i])
                    i = i + 1
                else
                    append_op("insert", updated_chars[j])
                    j = j + 1
                end
            end
        end

        while i <= #original_chars do
            append_op("delete", original_chars[i])
            i = i + 1
        end

        while j <= #updated_chars do
            append_op("insert", updated_chars[j])
            j = j + 1
        end

        local chunks = {}
        local index = 1
        while index <= #ops do
            if ops[index].op == "equal" then
                chunks[#chunks + 1] = {
                    type = "equal",
                    original = ops[index].text,
                    updated = ops[index].text
                }
                index = index + 1
            else
                local old_text = {}
                local new_text = {}
                while index <= #ops and ops[index].op ~= "equal" do
                    if ops[index].op == "delete" then
                        old_text[#old_text + 1] = ops[index].text
                    elseif ops[index].op == "insert" then
                        new_text[#new_text + 1] = ops[index].text
                    end
                    index = index + 1
                end

                local old_joined = table.concat(old_text)
                local new_joined = table.concat(new_text)
                local chunk_type = "insert"
                if old_joined ~= "" and new_joined ~= "" then
                    chunk_type = "replace"
                elseif old_joined ~= "" then
                    chunk_type = "delete"
                end

                chunks[#chunks + 1] = {
                    type = chunk_type,
                    original = old_joined,
                    updated = new_joined
                }
            end
        end

        return chunks
    end

    local function build_diff_side_segments(chunks, side, options)
        local segments = {}
        local is_original = side == "original"
        local opts = type(options) == "table" and options or {}
        local show_placeholders = opts.show_placeholders == true

        for _, chunk in ipairs(chunks or {}) do
            if chunk.type == "equal" then
                append_diff_segment(segments, chunk.original, false, false)
            elseif is_original then
                if chunk.type == "replace" or chunk.type == "delete" then
                    append_diff_segment(segments, chunk.original, true, false)
                elseif show_placeholders then
                    append_diff_segment(segments, "[无]", true, true)
                end
            else
                if chunk.type == "replace" or chunk.type == "insert" then
                    append_diff_segment(segments, chunk.updated, true, false)
                elseif show_placeholders then
                    append_diff_segment(segments, "[无]", true, true)
                end
            end
        end

        if #segments == 0 then
            append_diff_segment(segments, "[空]", false, true)
        end

        return segments
    end

    local function render_diff_segments_html(segments, palette)
        local colors = palette or {}
        local normal_color = colors.normal_color or "#D6DDE7"
        local changed_color = colors.changed_color or "#FFB4A8"
        local changed_bg = colors.changed_bg or "#4A2325"
        local placeholder_color = colors.placeholder_color or changed_color

        local html_parts = {}
        for _, segment in ipairs(segments or {}) do
            local safe_text = escape_html_text(segment.text or "")
            if segment.changed then
                local text_color = segment.placeholder and placeholder_color or changed_color
                local extra_style = segment.placeholder and "font-style:italic;" or ""
                html_parts[#html_parts + 1] = string.format(
                    "<span style='color:%s; background-color:%s; font-weight:700; %s'>%s</span>",
                    text_color,
                    changed_bg,
                    extra_style,
                    safe_text
                )
            else
                html_parts[#html_parts + 1] = string.format(
                    "<span style='color:%s;'>%s</span>",
                    normal_color,
                    safe_text
                )
            end
        end

        return table.concat(html_parts)
    end

    local function render_diff_html(original, updated, options)
        local opts = type(options) == "table" and options or {}
        local chunks = build_char_diff_segments(original, updated)
        local original_segments = build_diff_side_segments(chunks, "original", {
            show_placeholders = opts.show_placeholders == true
        })
        local updated_segments = build_diff_side_segments(chunks, "updated", {
            show_placeholders = opts.show_placeholders == true
        })
        local original_label = tostring(opts.original_label or "原句")
        local updated_label = tostring(opts.updated_label or "结果")

        local original_html = render_diff_segments_html(original_segments, {
            normal_color = "#D6DDE7",
            changed_color = "#FFB4A8",
            changed_bg = "#4A2325",
            placeholder_color = "#F4C4BC"
        })
        local updated_html = render_diff_segments_html(updated_segments, {
            normal_color = "#D6DDE7",
            changed_color = "#E8FFAF",
            changed_bg = "#33411D",
            placeholder_color = "#DBF3A3"
        })

        return table.concat({
            "<div style='margin-top:6px; line-height:1.6;'>",
            string.format(
                "<div style='margin:0 0 4px 0;'><span style='color:#8C98A6; font-weight:600;'>%s：</span>%s</div>",
                escape_html_text(original_label),
                original_html
            ),
            string.format(
                "<div style='margin:0;'><span style='color:#8C98A6; font-weight:600;'>%s：</span>%s</div>",
                escape_html_text(updated_label),
                updated_html
            ),
            "</div>"
        })
    end

    local function sanitize_tree_inline_text_local(value)
        local text = tostring(value or "")
        text = text:gsub("\r\n", "\n")
        text = text:gsub("\r", "\n")
        text = text:gsub("\n+", " ")
        text = text:gsub("%s+", " ")
        return trim(text)
    end

    local function format_compact_diff_text(original, updated, max_chars)
        local chunks = build_char_diff_segments(original, updated)
        local parts = {}

        for _, chunk in ipairs(chunks or {}) do
            if chunk.type == "equal" then
                parts[#parts + 1] = chunk.original
            elseif chunk.type == "replace" then
                parts[#parts + 1] = string.format("[%s→%s]", chunk.original, chunk.updated)
            elseif chunk.type == "insert" then
                parts[#parts + 1] = string.format("[+%s]", chunk.updated)
            elseif chunk.type == "delete" then
                parts[#parts + 1] = string.format("[-%s]", chunk.original)
            end
        end

        local summary = sanitize_tree_inline_text_local(table.concat(parts))
        return truncate_text_chars(summary, tonumber(max_chars) or 64)
    end

    local function normalize_tree_segment_text(value)
        local text = tostring(value or "")
        text = text:gsub("\r\n", "\n")
        text = text:gsub("\r", "\n")
        text = text:gsub("\n+", " ")
        text = text:gsub("%s+", " ")
        return text
    end

    local function append_tree_diff_segment(parts, segment, open_marker, close_marker, remaining_chars)
        local safe_segment = type(segment) == "table" and segment or {}
        local text = safe_segment.placeholder and "无" or tostring(safe_segment.text or "")
        text = normalize_tree_segment_text(text)
        if text == "" or remaining_chars <= 0 then
            return remaining_chars, false
        end

        local chars = split_text_chars_for_diff(text)
        local visible_count = #chars
        local truncated = false
        if visible_count > remaining_chars then
            local clipped = {}
            for index = 1, remaining_chars do
                clipped[#clipped + 1] = chars[index]
            end
            text = table.concat(clipped) .. "…"
            visible_count = remaining_chars
            truncated = true
        end

        if safe_segment.changed then
            parts[#parts + 1] = open_marker .. text .. close_marker
        else
            parts[#parts + 1] = text
        end

        return remaining_chars - visible_count, truncated
    end

    local function format_tree_diff_side_text(original, updated, side, max_chars)
        local chunks = build_char_diff_segments(original, updated)
        local segments = build_diff_side_segments(chunks, side, { show_placeholders = false })
        local limit = math.max(1, tonumber(max_chars) or 32)
        local markers = side == "original" and {"〔", "〕"} or {"【", "】"}
        local parts = {}
        local remaining = limit
        local truncated = false

        for _, segment in ipairs(segments or {}) do
            remaining, truncated = append_tree_diff_segment(parts, segment, markers[1], markers[2], remaining)
            if truncated or remaining <= 0 then
                break
            end
        end

        return trim_text(table.concat(parts))
    end

    local function format_tree_multiline_overview_text(original, updated, options)
        local opts = type(options) == "table" and options or {}
        local original_label = tostring(opts.original_label or "原")
        local updated_label = tostring(opts.updated_label or "建议")
        local line_limit = math.max(1, tonumber(opts.max_chars_per_line) or 20)
        local original_text = format_tree_diff_side_text(original, updated, "original", line_limit):gsub("〔", "["):gsub("〕", "]")
        local updated_text = format_tree_diff_side_text(original, updated, "updated", line_limit):gsub("【", "["):gsub("】", "]")
        return string.format("%s: %s\n%s: %s", original_label, original_text, updated_label, updated_text)
    end

    local function build_report_entry(kind, row_label, original, updated, options)
        local opts = type(options) == "table" and options or {}
        return {
            kind = tostring(kind or "change"),
            row_label = tostring(row_label or ""),
            original = tostring(original or ""),
            updated = tostring(updated or ""),
            updated_label = tostring(opts.updated_label or "结果"),
            reason = tostring(opts.reason or ""),
            status = tostring(opts.status or ""),
            confidence = opts.confidence,
            error_type = tostring(opts.error_type or ""),
            row_id = opts.row_id or nil
        }
    end

    local function format_report_confidence(value)
        if value == nil or value == "" then
            return ""
        end
        return string.format("%.2f", tonumber(value) or 0)
    end

    local function render_report_entry_plain_text(entry)
        if type(entry) ~= "table" then
            return ""
        end

        local lines = {}
        local row_label = trim_text(entry.row_label)
        if row_label ~= "" then
            lines[#lines + 1] = string.format("【行 %s】", row_label)
        else
            lines[#lines + 1] = "【修改项】"
        end

        lines[#lines + 1] = "原句：" .. tostring(entry.original or "")
        lines[#lines + 1] = tostring(entry.updated_label or "结果") .. "：" .. tostring(entry.updated or "")

        if trim_text(entry.error_type) ~= "" then
            lines[#lines + 1] = "错误类型：" .. tostring(entry.error_type)
        end
        if trim_text(entry.reason) ~= "" then
            lines[#lines + 1] = "理由：" .. tostring(entry.reason)
        end
        local confidence_text = format_report_confidence(entry.confidence)
        if confidence_text ~= "" then
            lines[#lines + 1] = "置信度：" .. confidence_text
        end
        if trim_text(entry.status) ~= "" then
            lines[#lines + 1] = "状态：" .. tostring(entry.status)
        end

        return table.concat(lines, "\n")
    end

    local function append_report_meta_html(parts, label, value)
        local safe_value = trim_text(tostring(value or ""))
        if safe_value == "" then
            return
        end

        parts[#parts + 1] = string.format(
            "<div style='margin-top:4px; color:#CDD6E1;'><span style='color:#8C98A6; font-weight:600;'>%s：</span><span>%s</span></div>",
            escape_html_text(label),
            escape_html_text(safe_value)
        )
    end

    local function render_report_entry_html(entry)
        if type(entry) ~= "table" then
            return ""
        end

        local row_label = trim_text(entry.row_label)
        local title = row_label ~= "" and ("行 " .. row_label) or "修改项"
        local html_parts = {
            "<div style='margin:0 0 12px 0; padding:10px 12px; border:1px solid #2C3640; background-color:#12181F;'>",
            string.format(
                "<div style='color:#E8EEF8; font-size:14px; font-weight:700; margin-bottom:4px;'>%s</div>",
                escape_html_text(title)
            ),
            render_diff_html(entry.original, entry.updated, {
                original_label = "原句",
                updated_label = entry.updated_label or "结果"
            })
        }

        append_report_meta_html(html_parts, "错误类型", entry.error_type)
        append_report_meta_html(html_parts, "理由", entry.reason)
        append_report_meta_html(html_parts, "置信度", format_report_confidence(entry.confidence))
        append_report_meta_html(html_parts, "状态", entry.status)

        html_parts[#html_parts + 1] = "</div>"
        return table.concat(html_parts)
    end

    local function build_report_payload(entries, empty_text)
        local safe_entries = type(entries) == "table" and entries or {}
        local fallback = tostring(empty_text or "")
        if #safe_entries == 0 then
            local fallback_html = string.format(
                "<html><body style='background-color:#0F141A; color:#D6DDE7; font-family:Helvetica; font-size:13px;'><div>%s</div></body></html>",
                escape_html_text(fallback)
            )
            return fallback, fallback_html
        end

        local plain_parts = {}
        local html_parts = {
            "<html><body style='background-color:#0F141A; color:#D6DDE7; font-family:Helvetica; font-size:13px;'>"
        }

        for idx, entry in ipairs(safe_entries) do
            plain_parts[#plain_parts + 1] = render_report_entry_plain_text(entry)
            html_parts[#html_parts + 1] = render_report_entry_html(entry)
            if idx < #safe_entries then
                html_parts[#html_parts + 1] = "<div style='height:4px;'></div>"
            end
        end

        html_parts[#html_parts + 1] = "</body></html>"
        return table.concat(plain_parts, "\n- - - - - - - - - -\n"), table.concat(html_parts)
    end

    local function append_basic_report_entry(report_entries, row_index, old_text, new_text, options)
        local opts = type(options) == "table" and options or {}
        local normalize_fn = opts.normalize_fn or trim_text
        local normalized_old = normalize_fn(tostring(old_text or ""))
        local normalized_new = normalize_fn(tostring(new_text or ""))
        if normalized_old == normalized_new then
            return false
        end

        table.insert(report_entries, build_report_entry(
            tostring(opts.kind or "applied"),
            tonumber(row_index) or 0,
            tostring(old_text or ""),
            tostring(new_text or ""),
            {
                updated_label = tostring(opts.updated_label or "修正"),
                status = tostring(opts.status or "已自动应用"),
                reason = opts.reason,
                confidence = opts.confidence,
                error_type = opts.error_type,
                row_id = opts.row_id
            }
        ))
        return true
    end

    local function show_standard_ai_result_report(task_name, fix_count, report_entries)
        local report_str = ""
        local report_html = ""
        if tonumber(fix_count) == 0 then
            report_str = "🎉 本轮未产生任何改动。"
        else
            report_str, report_html = build_report_payload(report_entries, "🎉 本轮未产生任何改动。")
        end

        local report_win = dispatcher:AddWindow({
            ID = "ReportWindow",
            WindowTitle = task_name .. "报告",
            Geometry = {400, 200, 600, 500},
        },
        ui:VGroup{
            Spacing = 10,
            ui:TextEdit{ ID = "ReportContent", Text = report_str, ReadOnly = true, Weight = 1 },
            ui:HGroup{
                Weight = 0,
                ui:HGap(0, 1),
                ui:Button{ ID = "CloseReportBtn", Text = "确认", Weight = 0, MinimumSize = {120, 30} }
            }
        })

        function report_win.On.ReportWindow.Close(ev)
            report_win:Hide()
        end

        function report_win.On.CloseReportBtn.Clicked(ev)
            report_win:Hide()
        end

        report_win:Show()
        local report_items = report_win:GetItems()
        if report_items and report_items.ReportContent then
            set_textedit_rich_content(report_items.ReportContent, report_str, report_html)
        end
    end

    local function format_batch_change_report_line(index, old_text, new_text, opts)
        opts = type(opts) == "table" and opts or {}
        local entry = build_report_entry(
            "batch",
            tonumber(index) or 0,
            tostring(old_text or ""),
            tostring(new_text or ""),
            {
                updated_label = "结果",
                status = "已更新",
                row_id = opts.row_id
            }
        )
        -- 附带还原所需的信息，供 show_batch_review_dialog 取消单条修改
        entry.revert_kind = opts.revert_kind or "text"
        if opts.original_end_frame ~= nil then
            entry.original_end_frame = opts.original_end_frame
        end
        if opts.updated_end_frame ~= nil then
            entry.updated_end_frame = opts.updated_end_frame
        end
        return entry
    end

    local function show_batch_result_report(task_name, report_entries, fix_count)
        local ui_dispatcher = dispatcher or disp
        if not ui_dispatcher or not ui then
            return
        end

        local report_str = ""
        local report_html = ""
        local report_geometry = {420, 220, 200, 150}
        if tonumber(fix_count) and fix_count > 0 then
            report_str, report_html = build_report_payload(report_entries, "🎉 本轮未产生任何改动。")
            report_geometry = {380, 160, 560, 360}
        else
            report_str = "🎉 本轮未产生任何改动。"
        end

        -- 是否有可逐条还原的条目
        local has_revertable = false
        if type(report_entries) == "table" then
            for _, entry in ipairs(report_entries) do
                if entry and entry.row_id then
                    has_revertable = true
                    break
                end
            end
        end

        local uid = tostring(os.time()) .. tostring(math.random(1000, 9999))
        local revert_btn_id = "BatchRevertBtn_" .. uid
        local close_btn_id = "CloseReportBtn_" .. uid

        -- 根据是否可逐条还原决定底部按钮组
        local action_row
        if has_revertable then
            action_row = ui:HGroup{
                Weight = 0,
                Spacing = 6,
                ui:HGap(0, 1),
                ui:Button{ ID = revert_btn_id, Text = "取消部分修改", Weight = 0, MinimumSize = {110, 30} },
                ui:Button{ ID = close_btn_id, Text = "确认", Weight = 0, MinimumSize = {88, 30} }
            }
        else
            action_row = ui:HGroup{
                Weight = 0,
                ui:HGap(0, 1),
                ui:Button{ ID = close_btn_id, Text = "确认", Weight = 0, MinimumSize = {120, 30} }
            }
        end

        local report_win = ui_dispatcher:AddWindow({
            ID = "BatchReportWindow_" .. uid,
            WindowTitle = tostring(task_name or "修改结果") .. "报告",
            Geometry = report_geometry,
        },
        ui:VGroup{
            Spacing = 10,
            ContentsMargins = 10,
            ui:TextEdit{ ID = "ReportContent_" .. uid, Text = report_str, ReadOnly = true, Weight = 1 },
            action_row
        })

        report_win.On["BatchReportWindow_" .. uid].Close = function(ev)
            report_win:Hide()
        end

        report_win.On[close_btn_id].Clicked = function(ev)
            report_win:Hide()
        end

        if has_revertable then
            report_win.On[revert_btn_id].Clicked = function(ev)
                -- show_batch_review_dialog 是文件全局函数
                if type(show_batch_review_dialog) == "function" then
                    show_batch_review_dialog(task_name, report_entries)
                end
            end
        end

        report_win:Show()
        local report_items = report_win:GetItems()
        if report_items and report_items["ReportContent_" .. uid] then
            set_textedit_rich_content(report_items["ReportContent_" .. uid], report_str, report_html)
        end
    end

    return {
        set_textedit_rich_content = set_textedit_rich_content,
        format_compact_diff_text = format_compact_diff_text,
        format_tree_diff_original_text = function(original, updated, max_chars)
            return format_tree_diff_side_text(original, updated, "original", max_chars)
        end,
        format_tree_diff_updated_text = function(original, updated, max_chars)
            return format_tree_diff_side_text(original, updated, "updated", max_chars)
        end,
        format_tree_multiline_overview_text = format_tree_multiline_overview_text,
        build_report_entry = build_report_entry,
        build_report_payload = build_report_payload,
        append_basic_report_entry = append_basic_report_entry,
        show_standard_ai_result_report = show_standard_ai_result_report,
        format_batch_change_report_line = format_batch_change_report_line,
        show_batch_result_report = show_batch_result_report,
    }
end)()

local function get_checkbox_checked(item)
    if not item then
        return false
    end

    local readers = {
        function() return item.Checked end,
        function() return item.CheckState end,
    }
    for _, reader in ipairs(readers) do
        local ok, value = pcall(reader)
        if ok then
            if type(value) == "boolean" then
                return value
            elseif type(value) == "number" then
                return value ~= 0
            elseif type(value) == "string" then
                local lowered = value:lower()
                if lowered == "true" or lowered == "checked" or lowered == "1" then
                    return true
                elseif lowered == "false" or lowered == "unchecked" or lowered == "0" then
                    return false
                end
            end
        end
    end

    return false
end

local function set_checkbox_checked(item, checked)
    if not item then
        return
    end

    local next_value = checked == true
    pcall(function() item.Checked = next_value end)
    pcall(function() item.CheckState = next_value and 2 or 0 end)
end

local function get_reference_script_risk_meta(char_count)
    local count = tonumber(char_count) or 0
    if count > REFERENCE_SCRIPT_HARD_LIMIT then
        return "#FF4D4F", "超过上限，无法启用文稿模式"
    elseif count > REFERENCE_SCRIPT_HIGH_LIMIT then
        return "#FF4D4F", "明显变慢，接近上限"
    elseif count > REFERENCE_SCRIPT_SOFT_LIMIT then
        return "#FAAD14", "可能变慢"
    end
    return "#00AA55", "影响较小"
end

local function update_reference_script_risk_label(raw_text)
    local label = find_ui_item("ReferenceScriptRiskLabel")
    if not label then
        return
    end

    local script_text = raw_text
    if script_text == nil then
        local input = find_ui_item("ReferenceScriptInput")
        script_text = get_textedit_content(input)
    end

    local cleaned = sanitize_reference_script_text(script_text)
    local char_count = count_utf8_chars(cleaned)
    local color, message = get_reference_script_risk_meta(char_count)
    local html = string.format("<font color='%s'>当前字数：%d · %s</font>", color, char_count, message)
    pcall(function() label.Text = html end)
end

local function read_shared_config_from_ui()
    if not find_ui_item("ReferenceScriptInput") and not find_ui_item("EnableScriptAssistCheckbox") then
        return LoadSharedConfig()
    end

    return normalize_shared_config(
        get_textedit_content(find_ui_item("ReferenceScriptInput")),
        get_checkbox_checked(find_ui_item("EnableScriptAssistCheckbox"))
    )
end

local function apply_shared_config_to_ui(shared_config)
    local normalized = normalize_shared_config(
        shared_config and shared_config.script_content,
        shared_config and shared_config.is_script_enabled
    )

    set_textedit_content(find_ui_item("ReferenceScriptInput"), normalized.script_content)
    set_checkbox_checked(find_ui_item("EnableScriptAssistCheckbox"), normalized.is_script_enabled)
    update_reference_script_risk_label(normalized.script_content)
end

local function save_shared_config_from_ui()
    local shared_config = read_shared_config_from_ui()
    SaveSharedConfig(shared_config.script_content, shared_config.is_script_enabled)
    update_reference_script_risk_label(shared_config.script_content)
    return shared_config
end

function normalize_api_url_for_request(url)
    local cleaned = trim_text(url or "")
    cleaned = cleaned:gsub("/+$", "")
    return cleaned
end

function build_openai_compatible_request_url(url)
    local cleaned = normalize_api_url_for_request(url)
    if cleaned == "" then
        return ""
    end

    local lower_url = cleaned:lower()
    if lower_url:find("/chat/completions$", 1) then
        return cleaned
    end
    if lower_url:find("/v1$", 1) then
        return cleaned .. "/chat/completions"
    end
    return cleaned .. "/v1/chat/completions"
end

function set_item_hidden(item, hidden)
    if not item then
        return
    end

    if hidden then
        pcall(function() item:Hide() end)
    else
        pcall(function() item:Show() end)
    end
    if item.SetAttrs then
        pcall(function() item:SetAttrs({Hidden = hidden}) end)
    end
    pcall(function() item.Hidden = hidden end)
    pcall(function() item.Enabled = not hidden end)
end

local function set_layout_row_collapsed(item, collapsed, expanded_height)
    if not item then
        return
    end

    local height = collapsed and 0 or math.max(0, tonumber(expanded_height) or 24)
    local min_size = {0, height}
    local max_size = {16777215, height}

    if item.SetAttrs then
        pcall(function() item:SetAttrs({MinimumSize = min_size, MaximumSize = max_size}) end)
    end
    pcall(function() item.MinimumSize = min_size end)
    pcall(function() item.MaximumSize = max_size end)
    pcall(function() item.Enabled = not collapsed end)
end

local function set_stack_page_active(page, active)
    if not page then
        return
    end

    if active then
        pcall(function() page:Show() end)
    else
        pcall(function() page:Hide() end)
    end
    if page.SetAttrs then
        pcall(function() page:SetAttrs({Hidden = not active}) end)
    end
    pcall(function() page.Hidden = not active end)
    pcall(function() page.Enabled = active end)
end

switch_stack_page_index_only = function(target_window, stack_id, active_index)
    local stack = find_window_item(target_window, stack_id, stack_id)
    if not stack then
        return
    end

    local page_index = math.max(0, tonumber(active_index) or 0)
    pcall(function() stack.CurrentIndex = page_index end)
end

switch_stack_page = function(target_window, stack_id, page_ids, active_index)
    local stack = find_window_item(target_window, stack_id, stack_id)
    if not stack or type(page_ids) ~= "table" or #page_ids == 0 then
        return
    end

    local page_count = #page_ids
    local page_index = math.max(0, math.min(page_count - 1, tonumber(active_index) or 0))
    pcall(function() stack.CurrentIndex = page_index end)

    for idx, page_id in ipairs(page_ids) do
        local page = find_window_item(target_window, page_id, page_id)
        set_stack_page_active(page, (idx - 1) == page_index)
    end
end

function sync_ai_provider_ui_state(provider_id)
    if not AIConfigPopWin then
        return
    end

    local provider_def = get_provider_def(provider_id)
    local can_edit_api_url = provider_allows_api_url_edit(provider_def)

    local api_url_input = find_ui_item("ApiUrlInput")
    if api_url_input then
        pcall(function() api_url_input.Enabled = can_edit_api_url end)
        if not can_edit_api_url then
            pcall(function() api_url_input.Text = provider_def.api_url or "" end)
        end
    end
end

function read_provider_config_from_ui(provider_id)
    local provider_def = get_provider_def(provider_id)
    local stored_config = LoadConfig(provider_def.id)
    local can_edit_api_url = provider_allows_api_url_edit(provider_def)

    local api_url_input = find_ui_item("ApiUrlInput")
    local api_key_input = find_ui_item("ApiKeyInput")
    local model_input = find_ui_item("ModelInput")

    local api_url = can_edit_api_url
        and normalize_api_url_for_request(api_url_input and api_url_input.Text or stored_config.api_url or provider_def.api_url or "")
        or provider_def.api_url
    local api_key = trim_text(api_key_input and api_key_input.Text or stored_config.api_key or "")
    local model = trim_text(model_input and model_input.Text or stored_config.model or "")
    return normalize_provider_config({
        api_url = api_url,
        api_key = api_key,
        model = model
    }, provider_def.id)
end

function apply_provider_config_to_ui(provider_id, config)
    local provider_def = get_provider_def(provider_id)
    local normalized = normalize_provider_config(config, provider_def.id)

    if find_ui_item("ApiUrlInput") then
        find_ui_item("ApiUrlInput").Text = normalized.api_url
    end
    if find_ui_item("ApiKeyInput") then
        find_ui_item("ApiKeyInput").Text = normalized.api_key
    end
    if find_ui_item("ModelInput") then
        find_ui_item("ModelInput").Text = normalized.model
    end

    sync_ai_provider_ui_state(provider_def.id)
end

function sync_provider_combo_selection(provider_id)
    if not full_window_ai_controls_initialized then
        return
    end

    local provider_index = get_provider_index_by_id(provider_id)
    local main_combo = win and win:Find("PresetCombo")
    local previous_suppress_state = suppress_provider_change_events
    local previous_bootstrap_state = provider_combo_bootstrap_in_progress

    suppress_provider_change_events = true
    provider_combo_bootstrap_in_progress = true
    if main_combo then
        local current_index = tonumber(main_combo.CurrentIndex) or -1
        if current_index ~= provider_index then
            pcall(function() main_combo.CurrentIndex = provider_index end)
        end
    end
    provider_combo_bootstrap_in_progress = previous_bootstrap_state
    suppress_provider_change_events = previous_suppress_state
end

function switch_ai_provider(provider_id, options)
    if provider_sync_in_progress then
        return
    end

    local target_provider_id = get_provider_def(provider_id).id
    if target_provider_id == current_ai_provider_id then
        return
    end

    local switch_options = type(options) == "table" and options or {}
    local should_save_current = switch_options.save_current ~= false
    provider_sync_in_progress = true

    if should_save_current and ai_config_popup_visible and current_ai_provider_id then
        SaveProviderConfig(current_ai_provider_id, read_provider_config_from_ui(current_ai_provider_id))
    end

    current_ai_provider_id = target_provider_id
    SaveActiveProviderId(target_provider_id)
    sync_provider_combo_selection(target_provider_id)
    if ai_config_popup_visible then
        apply_provider_config_to_ui(target_provider_id, LoadConfig(target_provider_id))
    end
    provider_sync_in_progress = false
end

local function build_row_id(track_index, row_index, start_frame, end_frame)
    return table.concat({
        tostring(track_index or 0),
        tostring(row_index or 0),
        tostring(start_frame or 0),
        tostring(end_frame or 0)
    }, ":")
end

local function sanitize_tree_inline_text(value)
    local text = tostring(value or "")
    text = text:gsub("\r\n", "\n")
    text = text:gsub("\r", "\n")
    text = text:gsub("\n+", " ")
    text = text:gsub("%s+", " ")
    return trim(text)
end

local function build_tree_display_text(index, primary_timecode, secondary_timecode, text)
    local safe_index = tonumber(index) or 0
    local safe_text = sanitize_tree_inline_text(text)
    local left = tostring(primary_timecode or "")
    local right = tostring(secondary_timecode or "")

    if right ~= "" then
        return string.format("[%d] %s → %s │ %s", safe_index, left, right, safe_text)
    end

    return string.format("[%d] %s │ %s", safe_index, left, safe_text)
end

-- 探测一次 Fusion TreeItem 的赋值通道并缓存（首行成功后，后续 ~480 行全程
-- 跳过 pcall 包装，节省 ~1ms 主要、并把潜在异常提前在第一行就暴露）
-- 缓存挂在 SEARCH_VIEW 上避免增加主 chunk 的 local 数量。
local function set_tree_node_display_text(node, display_text)
    local safe_display_text = tostring(display_text or "")
    local method = SEARCH_VIEW and SEARCH_VIEW._tree_text_method
    if method == "text0" then
        node.Text[0] = safe_display_text
        return
    elseif method == "setproperty" then
        node:setProperty("Text", safe_display_text)
        return
    end
    -- 首次调用：探测可用通道
    if pcall(function() node.Text[0] = safe_display_text end) then
        if SEARCH_VIEW then SEARCH_VIEW._tree_text_method = "text0" end
        return
    end
    if pcall(function() node:setProperty("Text", safe_display_text) end) then
        if SEARCH_VIEW then SEARCH_VIEW._tree_text_method = "setproperty" end
    end
end

local function sync_track_control(target_window)
    local control = find_window_item(target_window, "TrackSpin", "MiniTrackSpin")
    if not control then return end

    suppress_track_change_events = true
    -- 主窗口与极简窗口现在都用 LineEdit，统一以 Text 同步
    pcall(function() control.Text = tostring(current_track or 1) end)
    suppress_track_change_events = false
end

local function sync_search_control(target_window)
    local box = find_window_item(target_window, "SearchBox", "MiniSearchBox")
    if not box then return end

    suppress_search_change_events = true
    pcall(function() box.Text = current_search_query or "" end)
    suppress_search_change_events = false
end

local function update_search_query_from_window(target_window)
    local box = find_window_item(target_window, "SearchBox", "MiniSearchBox")
    if box then
        current_search_query = trim(box.Text or "")
    else
        current_search_query = trim(current_search_query or "")
    end
    return current_search_query
end

local function clear_search_cache_state()
    SEARCH_VIEW.cache.last_query = ""
    SEARCH_VIEW.cache.last_match_rows = nil
    SEARCH_VIEW.cache.last_dataset_revision = -1
    SEARCH_VIEW.cache.last_was_truncated = false
end

local function invalidate_search_cache(reason, options)
    options = options or {}
    if options.skip_revision ~= true then
        SEARCH_VIEW.dataset_revision = SEARCH_VIEW.dataset_revision + 1
    end
    SEARCH_VIEW.cache.dataset_revision = SEARCH_VIEW.dataset_revision
    clear_search_cache_state()
end

local function get_row_search_text_lower(row)
    if type(row) ~= "table" then
        return ""
    end

    local raw_text = tostring(row.text or "")
    if row.search_text_source ~= raw_text or type(row.search_text_lower) ~= "string" then
        row.search_text_source = raw_text
        row.search_text_lower = raw_text:lower()
    end

    return row.search_text_lower
end

local function slice_rows(rows, limit)
    local row_list = type(rows) == "table" and rows or {}
    local max_count = math.max(0, math.min(tonumber(limit) or 0, #row_list))
    local result = {}
    for i = 1, max_count do
        result[i] = row_list[i]
    end
    return result
end

SEARCH_VIEW.build_current_view_context = function(query_override)
    local rows = current_rows or {}
    local row_count = #rows
    local query = trim(query_override ~= nil and query_override or current_search_query or "")
    local query_lower = query:lower()

    if query_lower == "" then
        clear_search_cache_state()
        SEARCH_VIEW.cache.dataset_revision = SEARCH_VIEW.dataset_revision

        if row_count > SEARCH_VIEW.large_dataset_threshold then
            local visible_rows = slice_rows(rows, SEARCH_VIEW.max_tree_render_rows)
            return {
                mode = SEARCH_VIEW.modes.preview,
                query = "",
                visible_rows = visible_rows,
                visible_count = #visible_rows,
                total_count = row_count,
                truncated = row_count > SEARCH_VIEW.max_tree_render_rows,
                status_text = string.format(
                    "已加载 %d 条，当前显示前 %d 条",
                    row_count,
                    SEARCH_VIEW.max_tree_render_rows
                )
            }
        end

        return {
            mode = SEARCH_VIEW.modes.full,
            query = "",
            visible_rows = rows,
            visible_count = row_count,
            total_count = row_count,
            truncated = false,
            status_text = string.format("已加载 %d 条", row_count)
        }
    end

    local can_reuse_same_query = (
        SEARCH_VIEW.cache.last_dataset_revision == SEARCH_VIEW.dataset_revision and
        not SEARCH_VIEW.cache.last_was_truncated and
        SEARCH_VIEW.cache.last_query == query_lower and
        type(SEARCH_VIEW.cache.last_match_rows) == "table"
    )
    if can_reuse_same_query then
        local matched_rows = SEARCH_VIEW.cache.last_match_rows or {}
        return {
            mode = SEARCH_VIEW.modes.search,
            query = query,
            visible_rows = slice_rows(matched_rows, SEARCH_VIEW.max_tree_render_rows),
            visible_count = math.min(#matched_rows, SEARCH_VIEW.max_tree_render_rows),
            total_count = #matched_rows,
            truncated = false,
            status_text = string.format("找到 %d 条匹配", #matched_rows)
        }
    end

    local base_rows = rows
    local last_query = SEARCH_VIEW.cache.last_query or ""
    local can_reuse_prefix_cache = (
        SEARCH_VIEW.cache.last_dataset_revision == SEARCH_VIEW.dataset_revision and
        not SEARCH_VIEW.cache.last_was_truncated and
        last_query ~= "" and
        type(SEARCH_VIEW.cache.last_match_rows) == "table" and
        #query_lower > #last_query and
        query_lower:sub(1, #last_query) == last_query
    )
    if can_reuse_prefix_cache then
        base_rows = SEARCH_VIEW.cache.last_match_rows
    end

    local matched_rows = {}
    local visible_rows = {}
    local match_count = 0
    local truncated = false

    for _, row in ipairs(base_rows or {}) do
        if string.find(get_row_search_text_lower(row), query_lower, 1, true) ~= nil then
            match_count = match_count + 1
            if match_count <= SEARCH_VIEW.search_early_stop_limit then
                matched_rows[#matched_rows + 1] = row
            end
            if match_count <= SEARCH_VIEW.max_tree_render_rows then
                visible_rows[#visible_rows + 1] = row
            end
            if match_count > SEARCH_VIEW.search_early_stop_limit then
                truncated = true
                break
            end
        end
    end

    SEARCH_VIEW.cache.last_query = query_lower
    SEARCH_VIEW.cache.last_match_rows = matched_rows
    SEARCH_VIEW.cache.last_dataset_revision = SEARCH_VIEW.dataset_revision
    SEARCH_VIEW.cache.last_was_truncated = truncated

    if truncated then
        return {
            mode = SEARCH_VIEW.modes.search,
            query = query,
            visible_rows = visible_rows,
            visible_count = #visible_rows,
            total_count = SEARCH_VIEW.search_early_stop_limit + 1,
            truncated = true,
            status_text = string.format(
                "命中超过 %d 条，当前显示前 %d 条",
                SEARCH_VIEW.search_early_stop_limit,
                SEARCH_VIEW.max_tree_render_rows
            )
        }
    end

    return {
        mode = SEARCH_VIEW.modes.search,
        query = query,
        visible_rows = matched_rows,
        visible_count = #matched_rows,
        total_count = #matched_rows,
        truncated = false,
        status_text = string.format("找到 %d 条匹配", #matched_rows)
    }
end

local function get_selected_tree_node(tree)
    if not tree then return nil end

    local selected = nil
    do
        local ok_m, ret = pcall(function() return tree:CurrentItem() end)
        if ok_m and ret then selected = ret end
    end
    if not selected then
        local ok_p, ret = pcall(function() return tree.CurrentItem end)
        if ok_p and ret then selected = ret end
    end
    if not selected then
        local ok_sel, ret = pcall(function() return tree.SelectedNode end)
        if ok_sel and ret then selected = ret end
    end
    if not selected then
        local ok_ch, children = pcall(function() return tree.Children end)
        if ok_ch and children and #children > 0 then
            selected = children[1]
        end
    end

    return selected
end

local function get_tree_event_value(ev, keys)
    if type(ev) ~= "table" then
        return nil
    end

    for _, key in ipairs(keys or {}) do
        local value = ev[key]
        if value ~= nil then
            return value
        end
    end

    return nil
end

local function set_tree_current_item(tree, item)
    if not tree or not item then
        return false
    end

    local setters = {
        function() tree:SetCurrentItem(item) end,
        function() tree.CurrentItem = item end,
        function() tree:SetSelectedNode(item) end,
        function() tree.SelectedNode = item end,
        function() item.Selected = true end,
    }

    for _, setter in ipairs(setters) do
        local ok = pcall(setter)
        if ok then
            return true
        end
    end

    return false
end

local function find_row_by_id(row_id)
    if not row_id or not current_rows then return nil end
    for _, row in ipairs(current_rows) do
        if row and row.id == row_id then
            return row
        end
    end
    return nil
end

local function get_pending_change_key(pending_change)
    if type(pending_change) ~= "table" then
        return nil
    end

    if trim_text(pending_change.key) ~= "" then
        return trim_text(pending_change.key)
    end

    if pending_change.kind == "pair" then
        local row_id_1 = trim_text(pending_change.row_id_1)
        local row_id_2 = trim_text(pending_change.row_id_2)
        if row_id_1 ~= "" and row_id_2 ~= "" then
            return row_id_1 .. "||" .. row_id_2
        end
        return nil
    end

    return trim_text(pending_change.row_id)
end

function get_pending_change_row_label(pending_change)
    if type(pending_change) ~= "table" then
        return ""
    end
    if trim_text(pending_change.row_index_label) ~= "" then
        return trim_text(pending_change.row_index_label)
    end
    return tostring(pending_change.row_index or "")
end

function get_pending_change_summary_text(pending_change)
    if type(pending_change) ~= "table" then
        return ""
    end

    if pending_change.kind == "pair" then
        return string.format(
            "原: %s-%s 边界重分配\n建议: 双击查看详情",
            tostring(pending_change.row_index_1 or pending_change.row_index or ""),
            tostring(pending_change.row_index_2 or "")
        )
    end

    return report_helpers.format_tree_multiline_overview_text(
        pending_change.original or "",
        pending_change.suggestion or "",
        {
            original_label = "原",
            updated_label = "建议",
            max_chars_per_line = 20
        }
    )
end

local function format_pending_change_reason_tag(reason, pending_change)
    local text = trim_text(reason)
    if text == "" then
        return "需复核"
    end

    if text:find("字数发生变化", 1, true) then
        return "字数变化"
    end
    if text:find("边界重分配", 1, true) or text:find("边界错位", 1, true) then
        return "边界重分配"
    end
    if text:find("重合度", 1, true) or text:find("改动幅度过大", 1, true) then
        return "改动过大"
    end
    if text:find("高风险词保护", 1, true) then
        return "高风险词"
    end
    if text:find("近义词替换", 1, true) then
        return "近义词改写"
    end
    if text:find("逻辑词被改写", 1, true) then
        return "逻辑词改写"
    end
    if text:find("英文、数字或快捷键内容被改动", 1, true) or text:find("英文", 1, true) and text:find("快捷键", 1, true) then
        return "英文/数字改动"
    end
    if text:find("仅修改了英文字母大小写", 1, true) then
        return "大小写改动"
    end
    if text:find("仅修改了英文或数字周围空格", 1, true) then
        return "空格改动"
    end
    if text:find("非法修正项", 1, true) then
        return "非法修正"
    end
    if text:find("需人工复核", 1, true) then
        return "需复核"
    end

    if type(pending_change) == "table" and pending_change.kind == "pair" then
        return "边界重分配"
    end

    return "需复核"
end

function get_pending_change_detail_text(pending_change, output_format)
    local wants_html = output_format == "html"
    if type(pending_change) ~= "table" then
        if wants_html then
            local _, html = report_helpers.build_report_payload({}, "请选择一条待审核建议查看完整内容。")
            return html
        end
        return "请选择一条待审核建议查看完整内容。"
    end

    local detail_entries = {}
    if pending_change.kind == "pair" then
        detail_entries[#detail_entries + 1] = report_helpers.build_report_entry(
            "pending_pair",
            tostring(pending_change.row_index_1 or pending_change.row_index or ""),
            tostring(pending_change.original_1 or ""),
            tostring(pending_change.suggestion_1 or ""),
            {
                updated_label = "建议",
                reason = pending_change.reason,
                status = "待人工复核"
            }
        )
        detail_entries[#detail_entries + 1] = report_helpers.build_report_entry(
            "pending_pair",
            tostring(pending_change.row_index_2 or ""),
            tostring(pending_change.original_2 or ""),
            tostring(pending_change.suggestion_2 or ""),
            {
                updated_label = "建议",
                reason = pending_change.reason,
                status = "待人工复核"
            }
        )
    else
        detail_entries[#detail_entries + 1] = report_helpers.build_report_entry(
            "pending_single",
            tostring(pending_change.row_index_label or pending_change.row_index or ""),
            tostring(pending_change.original or ""),
            tostring(pending_change.suggestion or ""),
            {
                updated_label = "建议",
                reason = pending_change.reason,
                status = "待人工复核"
            }
        )
    end

    local plain_text, html_text = report_helpers.build_report_payload(detail_entries, "请选择一条待审核建议查看完整内容。")
    return wants_html and html_text or plain_text
end

local function build_pending_change(row, row_index, original, suggestion, reason)
    local safe_row = type(row) == "table" and row or {}
    local safe_row_index = tonumber(safe_row.index) or tonumber(row_index) or 0
    local row_id = trim_text(safe_row.id)
    if row_id == "" then
        row_id = build_row_id(
            current_track,
            safe_row_index,
            safe_row.start_frame,
            safe_row.end_frame
        )
    end

    return {
        kind = "single",
        key = row_id,
        row_id = row_id,
        row_index = safe_row_index,
        row_index_label = tostring(safe_row_index),
        original = tostring(original or ""),
        suggestion = tostring(suggestion or ""),
        reason = tostring(reason or ""),
        is_approved = false
    }
end

local function build_pending_pair_change(row_1, row_2, row_index_1, row_index_2, original_1, original_2, suggestion_1, suggestion_2, reason)
    local safe_row_1 = type(row_1) == "table" and row_1 or {}
    local safe_row_2 = type(row_2) == "table" and row_2 or {}
    local safe_index_1 = tonumber(safe_row_1.index) or tonumber(row_index_1) or 0
    local safe_index_2 = tonumber(safe_row_2.index) or tonumber(row_index_2) or (safe_index_1 + 1)
    local row_id_1 = trim_text(safe_row_1.id)
    local row_id_2 = trim_text(safe_row_2.id)

    if row_id_1 == "" then
        row_id_1 = build_row_id(current_track, safe_index_1, safe_row_1.start_frame, safe_row_1.end_frame)
    end
    if row_id_2 == "" then
        row_id_2 = build_row_id(current_track, safe_index_2, safe_row_2.start_frame, safe_row_2.end_frame)
    end

    return {
        kind = "pair",
        key = row_id_1 .. "||" .. row_id_2,
        row_id_1 = row_id_1,
        row_id_2 = row_id_2,
        row_index = safe_index_1,
        row_index_1 = safe_index_1,
        row_index_2 = safe_index_2,
        row_index_label = string.format("%d-%d", safe_index_1, safe_index_2),
        original = tostring(original_1 or "") .. " / " .. tostring(original_2 or ""),
        suggestion = tostring(suggestion_1 or "") .. " / " .. tostring(suggestion_2 or ""),
        original_1 = tostring(original_1 or ""),
        original_2 = tostring(original_2 or ""),
        suggestion_1 = tostring(suggestion_1 or ""),
        suggestion_2 = tostring(suggestion_2 or ""),
        reason = tostring(reason or ""),
        is_approved = false
    }
end

local function safe_refresh_tree_widget(tree)
    if not tree then return end
    pcall(function() tree:Update() end)
    pcall(function() tree:Repaint() end)
end

local get_tree_item_text
local set_tree_item_text

local function set_widget_updates_enabled(widget, enabled)
    if not widget then
        return false
    end

    local desired = enabled == true
    local attempts = {
        function() widget:SetUpdatesEnabled(desired) end,
        function() widget:setProperty("UpdatesEnabled", desired) end,
        function() widget.UpdatesEnabled = desired end,
    }

    for _, attempt in ipairs(attempts) do
        local ok = pcall(attempt)
        if ok then
            return true
        end
    end

    return false
end

local function with_tree_updates_suspended(tree, target_window, fn)
    if type(fn) ~= "function" then
        return false, "缺少刷新回调"
    end

    local window = resolve_window(target_window)
    local tree_updates_suspended = set_widget_updates_enabled(tree, false)
    local window_updates_suspended = false
    if not tree_updates_suspended and window then
        window_updates_suspended = set_widget_updates_enabled(window, false)
    end

    local ok, result = xpcall(fn, function(err)
        if debug and debug.traceback then
            return debug.traceback(err, 2)
        end
        return tostring(err)
    end)

    if tree_updates_suspended then
        set_widget_updates_enabled(tree, true)
    end
    if window_updates_suspended then
        set_widget_updates_enabled(window, true)
    end

    if tree then
        pcall(function() tree:Update() end)
        pcall(function() tree:Repaint() end)
    end

    if not ok then
        return false, result
    end

    return true, result
end

local function queue_tree_node_text_update(update_entries, node, display_text)
    if type(update_entries) ~= "table" or not node then
        return false
    end

    local next_text = tostring(display_text or "")
    local ok_current_text, current_text = pcall(function()
        if type(get_tree_item_text) == "function" then
            return get_tree_item_text(node, 0)
        end
        return nil
    end)
    if ok_current_text and tostring(current_text or "") == next_text then
        return false
    end

    invalidate_search_cache(nil, {skip_revision = true})
    update_entries[#update_entries + 1] = {
        node = node,
        text = next_text
    }
    return true
end

local function apply_tree_node_text_updates(target_window, tree, update_entries)
    if not tree or type(update_entries) ~= "table" or #update_entries == 0 then
        return 0
    end

    local ok, err = with_tree_updates_suspended(tree, target_window, function()
        for _, entry in ipairs(update_entries) do
            if entry and entry.node then
                set_tree_node_display_text(entry.node, entry.text)
            end
        end
    end)

    if not ok then
        local warning_msg = "[Warning] 批量刷新字幕树失败，已回退逐项更新: " .. tostring(err)
        print(warning_msg)
        if type(LogMsg) == "function" then
            pcall(function() LogMsg(warning_msg) end)
        end
        for _, entry in ipairs(update_entries) do
            if entry and entry.node then
                set_tree_node_display_text(entry.node, entry.text)
            end
        end
        pcall(function() tree:Update() end)
        pcall(function() tree:Repaint() end)
    end

    return #update_entries
end

get_tree_item_text = function(item, column_index)
    if not item then
        return ""
    end

    local idx = tonumber(column_index) or 0
    local ok_indexed, value = pcall(function() return item.Text[idx] end)
    if ok_indexed and value ~= nil then
        return tostring(value)
    end

    local ok_plain, plain_value = pcall(function() return item.Text end)
    if ok_plain and type(plain_value) == "string" then
        return plain_value
    end

    return ""
end

set_tree_item_text = function(item, column_index, value)
    if not item then
        return
    end

    local idx = tonumber(column_index) or 0
    local text = tostring(value or "")
    pcall(function() item.Text[idx] = text end)
end

local function get_pending_checkbox_mark(is_approved)
    return is_approved and PENDING_CHECKED_MARK or PENDING_UNCHECKED_MARK
end

local function get_pending_tree_event_item(ev)
    local item = get_tree_event_value(ev, {"item", "Item", "currentItem", "CurrentItem"})
    if item then
        return item
    end
    return get_selected_tree_node(pending_report_tree)
end

local function get_pending_tree_event_column(ev)
    local value = get_tree_event_value(ev, {"column", "Column", "col", "Col"})
    return tonumber(value)
end

local function get_pending_change_for_item(item)
    if not item or not pending_change_item_map then
        return nil, nil
    end

    local change_key = pending_change_item_map[item]
    if not change_key then
        return nil, nil
    end

    return pending_change_by_key[change_key], change_key
end

local function set_pending_tree_current_item(tree, item)
    return set_tree_current_item(tree, item)
end

function refresh_pending_tree_item(item, pending_change)
    if not item or not pending_change then
        return
    end

    set_tree_item_text(item, 0, get_pending_checkbox_mark(pending_change.is_approved == true))
    set_tree_item_text(item, 1, get_pending_change_row_label(pending_change))
    set_tree_item_text(item, 2, get_pending_change_summary_text(pending_change))
    set_tree_item_text(item, 3, format_pending_change_reason_tag(pending_change.reason, pending_change))
    set_tree_item_text(item, 4, "[ ▶ ]")
    pcall(function() item.TextColor[0] = pending_change.is_approved and {R = 120, G = 235, B = 160, A = 255} or {R = 210, G = 210, B = 210, A = 255} end)
    pcall(function() item.TextColor[2] = {R = 210, G = 220, B = 235, A = 255} end)
    pcall(function() item.TextColor[3] = {R = 170, G = 170, B = 170, A = 255} end)
    pcall(function() item.TextColor[4] = {R = 70, G = 120, B = 170, A = 255} end)
end

function set_pending_report_detail_text(text, html)
    if not pending_report_detail_view then
        return
    end
    report_helpers.set_textedit_rich_content(
        pending_report_detail_view,
        tostring(text or "请选择一条待审核建议查看完整内容。"),
        html
    )
end

function update_pending_report_detail_for_item(item)
    local pending_change = select(1, get_pending_change_for_item(item))
    set_pending_report_detail_text(
        get_pending_change_detail_text(pending_change),
        get_pending_change_detail_text(pending_change, "html")
    )
end

function show_pending_detail_window_for_item(item)
    local pending_change = select(1, get_pending_change_for_item(item))
    if not pending_change then
        return
    end

    if not pending_detail_window then
        pending_detail_window = dispatcher:AddWindow({
            ID = "PendingDetailWindow",
            WindowTitle = "待审核详情",
            Geometry = {440, 170, 520, 320},
        },
        ui:VGroup{
            Spacing = 8,
            ContentsMargins = 10,
            ui:TextEdit{
                ID = "PendingDetailText",
                Text = "",
                ReadOnly = true,
                Weight = 1,
                MinimumSize = {0, 160}
            },
            ui:HGroup{
                Weight = 0,
                ui:HGap(0, 1),
                ui:Button{ ID = "ClosePendingDetailBtn", Text = "确认", Weight = 0, MinimumSize = {88, 26} }
            }
        })

        function pending_detail_window.On.PendingDetailWindow.Close(ev)
            if pending_detail_window then
                pcall(function() pending_detail_window:Hide() end)
            end
            pending_detail_window = nil
        end

        function pending_detail_window.On.ClosePendingDetailBtn.Clicked(ev)
            if pending_detail_window then
                pcall(function() pending_detail_window:Hide() end)
            end
            pending_detail_window = nil
        end
    end

    local detail_items = pending_detail_window:GetItems()
    local detail_view = detail_items and detail_items.PendingDetailText or nil
    if detail_view then
        report_helpers.set_textedit_rich_content(
            detail_view,
            get_pending_change_detail_text(pending_change),
            get_pending_change_detail_text(pending_change, "html")
        )
    end
    pcall(function() pending_detail_window:Show() end)
    pcall(function() pending_detail_window:Raise() end)
    pcall(function() pending_detail_window:ActivateWindow() end)
end

function set_applied_report_detail_text(text, html)
    if not applied_report_detail_view then
        return
    end
    report_helpers.set_textedit_rich_content(applied_report_detail_view, tostring(text or ""), html)
end

function show_applied_report_detail_window(task_name, applied_report_entries, fix_count)
    local applied_report_plain, applied_report_html = report_helpers.build_report_payload(
        applied_report_entries,
        "🎉 本轮未产生任何改动。"
    )
    if trim_text(applied_report_plain) == "" then
        return
    end

    if not applied_report_detail_window then
        applied_report_detail_window = dispatcher:AddWindow({
            ID = "AppliedReportDetailWindow",
            WindowTitle = tostring(task_name or "AI 纠错") .. "报告 · 已自动应用详情",
            Geometry = {440, 170, 520, 360},
        },
        ui:VGroup{
            Spacing = 8,
            ContentsMargins = 10,
            ui:VGroup{
                Weight = 0,
                Spacing = 2,
                MinimumSize = {0, 44},
                ui:Label{
                    ID = "AppliedReportDetailKicker",
                    Text = format_ai_applied_kicker_text(),
                    Weight = 0,
                    WordWrap = false,
                    Alignment = {AlignLeft = true, AlignVCenter = true},
                    MinimumSize = {0, 18}
                },
                ui:Label{
                    ID = "AppliedReportDetailSummary",
                    Text = format_ai_applied_result_text(fix_count, "，以下为完整详情。"),
                    Weight = 0,
                    WordWrap = true,
                    Alignment = {AlignLeft = true, AlignVCenter = true},
                    MinimumSize = {0, 26}
                }
            },
            ui:TextEdit{
                ID = "AppliedReportDetailText",
                Text = "",
                ReadOnly = true,
                Weight = 1,
                MinimumSize = {0, 240}
            },
            ui:HGroup{
                Weight = 0,
                Spacing = 6,
                ui:HGap(0, 1),
                ui:Button{ ID = "RevertAppliedDetailBtn", Text = "取消部分应用", Weight = 0, MinimumSize = {110, 26} },
                ui:Button{ ID = "CloseAppliedReportDetailBtn", Text = "确认", Weight = 0, MinimumSize = {88, 26} }
            }
        })

        local detail_items = applied_report_detail_window:GetItems()
        applied_report_detail_view = detail_items and detail_items.AppliedReportDetailText or nil

        function applied_report_detail_window.On.RevertAppliedDetailBtn.Clicked(ev)
            show_revert_applied_dialog(applied_report_entries)
        end

        function applied_report_detail_window.On.AppliedReportDetailWindow.Close(ev)
            if applied_report_detail_window then
                pcall(function() applied_report_detail_window:Hide() end)
            end
            applied_report_detail_window = nil
            applied_report_detail_view = nil
        end

        function applied_report_detail_window.On.CloseAppliedReportDetailBtn.Clicked(ev)
            if applied_report_detail_window then
                pcall(function() applied_report_detail_window:Hide() end)
            end
            applied_report_detail_window = nil
            applied_report_detail_view = nil
        end
    else
        local detail_items = applied_report_detail_window:GetItems()
        if detail_items and detail_items.AppliedReportDetailSummary then
            pcall(function()
                detail_items.AppliedReportDetailSummary.Text = format_ai_applied_result_text(fix_count, "，以下为完整详情。")
            end)
        end
    end

    set_applied_report_detail_text(applied_report_plain, applied_report_html)
    pcall(function() applied_report_detail_window:Show() end)
    pcall(function() applied_report_detail_window:Raise() end)
    pcall(function() applied_report_detail_window:ActivateWindow() end)
end

function format_ai_report_overview_text(task_name, fix_count, pending_count)
    local safe_task_name = trim_text(task_name)
    if safe_task_name == "" then
        safe_task_name = "AI 纠错"
    end
    return string.format(
        "<span style='color:#AEB5BF;'>%s完成：</span><span style='color:#C4CAD3;'>已自动应用 %d 条，待人工确认 %d 条。</span>",
        safe_task_name,
        tonumber(fix_count) or 0,
        tonumber(pending_count) or 0
    )
end

function format_ai_applied_kicker_text()
    return "<span style='color:#BFC6D1; font-weight:600;'>自动应用结果</span>"
end

function format_ai_applied_result_text(fix_count, suffix_text)
    local suffix = trim_text(suffix_text)
    local suffix_html = ""
    if suffix ~= "" then
        suffix_html = string.format("<span style='color:#97A0AD; font-size:12px;'>%s</span>", suffix)
    end
    return string.format(
        "<span style='color:#E8EEF8; font-size:18px; font-weight:700;'>%d 条</span> <span style='color:#DFF7E6; font-size:17px; font-weight:700;'>已自动应用</span>%s",
        tonumber(fix_count) or 0,
        suffix_html
    )
end

function format_ai_section_label_text(text)
    return string.format("<font color='#CBD2DC'><b>%s</b></font>", tostring(text or ""))
end

function format_ai_applied_hint_text()
    return "<span style='color:#AEB7C3; font-size:12px;'>完整修改内容在右侧，点击按钮查看 →</span>"
end

local function update_row_preview_display(row)
    if type(row) ~= "table" then
        return
    end

    row.text = tostring(row.text or "")
    get_row_search_text_lower(row)
    local row_index = tonumber(row.index) or 0
    local tc_start, tc_end = get_row_timecodes(row)
    if tc_start and tc_end then
        row.timecode = tc_start .. " --> " .. tc_end
        row.display_text = build_tree_display_text(row_index, tostring(tc_start), tostring(tc_end), row.text)
    else
        row.timecode = row.timecode or ""
        row.display_text = build_tree_display_text(row_index, tostring(row.timecode or ""), nil, row.text)
    end
end

local function log_pending_review_event(msg)
    local line = "[Hooper AI 2.0] " .. tostring(msg or "")
    print(line)
    if type(LogMsg) == "function" then
        pcall(function() LogMsg(tostring(msg or "")) end)
    end
end

refresh_preview_windows = function()
    local context = SEARCH_VIEW.build_current_view_context()

    local function refresh_window_tree(target_window)
        if not target_window or not render_rows_to_window then
            return
        end
        pcall(function()
            render_rows_to_window(target_window, context.visible_rows)
            if context.status_text and context.status_text ~= "" then
                update_shared_status(target_window, context.status_text)
            end
        end)
    end

    if win then
        refresh_window_tree(win)
    end
    if mini_win then
        refresh_window_tree(mini_win)
    end
end

local function mark_dirty_row(dirty_row_ids, row)
    if type(dirty_row_ids) ~= "table" or type(row) ~= "table" then
        return
    end

    local row_id = trim_text(row.id)
    if row_id ~= "" then
        dirty_row_ids[row_id] = true
    end
end

local function sync_current_preview_tree(target_window, dirty_row_ids)
    local window = resolve_window(target_window)
    if not window then
        return 0
    end
    if active_window and window ~= active_window then
        return 0
    end

    local tree = find_window_item(window, "SubtitleTree", "MiniSubtitleTree")
    if not tree or type(subtitle_data_map) ~= "table" then
        return 0
    end

    if type(dirty_row_ids) == "table" and next(dirty_row_ids) == nil then
        return 0
    end

    local update_entries = {}
    if type(dirty_row_ids) == "table" then
        for row_id in pairs(dirty_row_ids) do
            local clean_row_id = trim_text(row_id)
            local node = clean_row_id ~= "" and subtitle_row_id_node_map[clean_row_id] or nil
            local row = node and subtitle_data_map[node] or nil
            if node and type(row) == "table" then
                queue_tree_node_text_update(update_entries, node, row.display_text or "")
            end
        end
    else
        for node, row in pairs(subtitle_data_map) do
            if node and type(row) == "table" then
                queue_tree_node_text_update(update_entries, node, row.display_text or "")
            end
        end
    end

    return apply_tree_node_text_updates(window, tree, update_entries)
end

function build_snapshot_record(action_label, rows)
    local row_list = rows or current_rows or {}
    return {
        action_label = trim_text(action_label),
        created_at = os.date("%Y-%m-%d %H:%M:%S"),
        track = tonumber(current_track) or 1,
        row_count = #row_list,
        rows = clone_table(row_list)
    }
end

function update_undo_redo_button_states()
    local undo_btn = win and win:Find("UndoBtn")
    if undo_btn then
        pcall(function() undo_btn.Enabled = (#undo_stack > 0) end)
    end
end

function push_stack_snapshot(target_stack, snapshot)
    if type(target_stack) ~= "table" or type(snapshot) ~= "table" then
        return
    end

    target_stack[#target_stack + 1] = snapshot
    if #target_stack > BACKUP_HISTORY_STORE_LIMIT then
        table.remove(target_stack, 1)
    end
end

function prepare_mutation_snapshot(action_label)
    if type(current_rows) ~= "table" or #current_rows == 0 then
        return nil
    end
    return build_snapshot_record(action_label)
end

function commit_mutation_snapshot(snapshot)
    if type(snapshot) ~= "table" then
        return false
    end
    push_stack_snapshot(undo_stack, snapshot)
    redo_stack = {}
    update_undo_redo_button_states()
    return true
end

function restore_rows_from_snapshot(snapshot)
    if type(snapshot) ~= "table" then
        return false
    end

    rebuild_tree_from_rows(clone_table(snapshot.rows or {}), active_window or win)
    update_undo_redo_button_states()
    return true
end

function restore_rows_from_backup_file(real_path)
    local file = io.open(real_path, "r")
    if not file then
        return nil, "无法打开文件: " .. tostring(real_path)
    end

    local new_subtitles = {}
    local current_sub = {}

    for line in file:lines() do
        line = tostring(line or ""):gsub("\r", "")

        if line == "" then
            if current_sub.text then
                new_subtitles[#new_subtitles + 1] = current_sub
            end
            current_sub = {}
        elseif line:match("^%d+$") and not current_sub.index then
            current_sub.index = tonumber(line)
        elseif line:match("%->") then
            current_sub.timecode = line
        else
            if current_sub.text then
                current_sub.text = current_sub.text .. "\n" .. line
            else
                current_sub.text = line
            end
        end
    end
    if current_sub.text then
        new_subtitles[#new_subtitles + 1] = current_sub
    end
    file:close()

    if #new_subtitles == 0 then
        return nil, "未能解析出任何字幕"
    end

    local restored_rows = {}
    for i, sub in ipairs(new_subtitles) do
        local start_frame = 0
        local end_frame = 0
        if sub.timecode then
            local start_t, end_t = sub.timecode:match("(%d+:%d+:%d+,%d+)%s*%-%->%s*(%d+:%d+:%d+,%d+)")
            if start_t and end_t then
                start_frame = srt_time_to_frames(start_t, current_fps)
                end_frame = srt_time_to_frames(end_t, current_fps)
            end
        end

        restored_rows[#restored_rows + 1] = {
            index = i,
            timecode = sub.timecode or "",
            text = sub.text,
            start_frame = start_frame,
            end_frame = end_frame,
            fps = current_fps
        }
    end

    table.sort(restored_rows, function(a, b)
        return tostring(a.timecode or "") < tostring(b.timecode or "")
    end)

    return restored_rows
end

function restore_history_entry(entry)
    local status = win and win:Find("StatusLabel")
    if type(entry) ~= "table" or tostring(entry.full_path or "") == "" then
        if status then status:Set("Text", "未找到可恢复的历史版本") end
        return false
    end

    local restored_rows, err = restore_rows_from_backup_file(entry.full_path)
    if not restored_rows then
        if status then status:Set("Text", "恢复失败: " .. tostring(err)) end
        return false
    end

    redo_stack = {}
    if type(current_rows) == "table" and #current_rows > 0 then
        push_stack_snapshot(redo_stack, build_snapshot_record("历史恢复回退"))
    end

    rebuild_tree_from_rows(restored_rows, active_window or win)
    set_current_preview_source(PREVIEW_SOURCE_HISTORY, entry)
    update_undo_redo_button_states()

    local message = "已恢复历史版本：" .. tostring(entry.action_label or "") .. "，未写回时间线，需手动点更新时间线"
    if status then status:Set("Text", message) end
    print("[Hooper AI 2.0] " .. message)
    return true
end

function perform_undo()
    local status = win and win:Find("StatusLabel")
    local snapshot = table.remove(undo_stack)
    if not snapshot then
        update_undo_redo_button_states()
        if status then status:Set("Text", "没有可撤回的操作") end
        return
    end

    push_stack_snapshot(redo_stack, build_snapshot_record(snapshot.action_label))
    restore_rows_from_snapshot(snapshot)
    local message = "已撤回：" .. tostring(snapshot.action_label or "上一步") .. "，未写回时间线，需手动点更新时间线"
    if status then status:Set("Text", message) end
end

function perform_redo()
    local status = win and win:Find("StatusLabel")
    local snapshot = table.remove(redo_stack)
    if not snapshot then
        update_undo_redo_button_states()
        if status then status:Set("Text", "没有可重做的操作") end
        return
    end

    push_stack_snapshot(undo_stack, build_snapshot_record(snapshot.action_label))
    restore_rows_from_snapshot(snapshot)
    local message = "已重做：" .. tostring(snapshot.action_label or "上一步") .. "，未写回时间线，需手动点更新时间线"
    if status then status:Set("Text", message) end
end

local function build_pending_row_map(rows)
    local row_map = {}
    for _, row in ipairs(rows or {}) do
        if type(row) == "table" then
            local row_id = trim_text(row.id)
            if row_id ~= "" then
                row_map[row_id] = row
            end
        end
    end
    return row_map
end

local function apply_single_pending_change_to_row_map(pending_change, row_map, warning_prefix, dirty_row_ids)
    local row = row_map[trim_text(pending_change and pending_change.row_id)]
    if not row then
        local warning_msg = string.format("%s%s", tostring(warning_prefix or "[Warning] Pending preview row_id not found: "), tostring(pending_change and pending_change.row_id))
        print(warning_msg)
        return 0
    end

    row.text = tostring((pending_change.is_approved and pending_change.suggestion) or pending_change.original or "")
    update_row_preview_display(row)
    mark_dirty_row(dirty_row_ids, row)
    return 1
end

local function apply_pair_pending_change_to_row_map(pending_change, row_map, warning_prefix, dirty_row_ids)
    local row_1 = row_map[trim_text(pending_change and pending_change.row_id_1)]
    local row_2 = row_map[trim_text(pending_change and pending_change.row_id_2)]
    if not row_1 or not row_2 then
        local warning_msg = string.format(
            "%s%s / %s",
            tostring(warning_prefix or "[Warning] Pending preview pair row_id not found: "),
            tostring(pending_change and pending_change.row_id_1),
            tostring(pending_change and pending_change.row_id_2)
        )
        print(warning_msg)
        return 0
    end

    row_1.text = tostring((pending_change.is_approved and pending_change.suggestion_1) or pending_change.original_1 or "")
    row_2.text = tostring((pending_change.is_approved and pending_change.suggestion_2) or pending_change.original_2 or "")
    update_row_preview_display(row_1)
    update_row_preview_display(row_2)
    mark_dirty_row(dirty_row_ids, row_1)
    mark_dirty_row(dirty_row_ids, row_2)
    return 2
end

local function apply_pending_change_to_row_map(pending_change, row_map, warning_prefix, dirty_row_ids)
    if type(pending_change) ~= "table" then
        return 0
    end

    if pending_change.kind == "pair" then
        return apply_pair_pending_change_to_row_map(pending_change, row_map, warning_prefix, dirty_row_ids)
    end
    return apply_single_pending_change_to_row_map(pending_change, row_map, warning_prefix, dirty_row_ids)
end

local function apply_pending_changes_to_preview(change_key_or_nil)
    if type(current_rows) ~= "table" or #current_rows == 0 then
        return 0, {}
    end

    local target_change_key = trim_text(change_key_or_nil)
    local row_map = build_pending_row_map(current_rows)
    local applied_count = 0
    local dirty_row_ids = {}

    for _, pending_change in ipairs(PendingChanges or {}) do
        local pending_change_key = get_pending_change_key(pending_change)
        if target_change_key == "" or pending_change_key == target_change_key then
            applied_count = applied_count + apply_pending_change_to_row_map(pending_change, row_map, "[Warning] Pending preview row_id not found: ", dirty_row_ids)
        end
    end

    return applied_count, dirty_row_ids
end

local function sync_pending_preview_changes(change_key_or_nil)
    local ok, result, dirty_row_ids = pcall(function()
        return apply_pending_changes_to_preview(change_key_or_nil)
    end)
    if not ok then
        log_pending_review_event("待审核预览同步失败: " .. tostring(result))
        return false
    end

    if tonumber(result) and result > 0 then
        sync_current_preview_tree(active_window, dirty_row_ids)
        return true
    end
    return false
end

local function toggle_pending_change_item(item)
    local pending_change = select(1, get_pending_change_for_item(item))
    if not pending_change then
        return
    end

    pending_change.is_approved = not pending_change.is_approved
    refresh_pending_tree_item(item, pending_change)
    log_pending_review_event(string.format("待审核建议%s: %s", pending_change.is_approved and "已选中" or "已忽略", tostring(get_pending_change_key(pending_change))))
    sync_pending_preview_changes(get_pending_change_key(pending_change))
    safe_refresh_tree_widget(pending_report_tree)
end

local function sync_pending_changes_from_tree()
    if not pending_change_item_map or next(pending_change_item_map) == nil then
        return
    end

    for item, change_key in pairs(pending_change_item_map) do
        local pending_change = change_key and pending_change_by_key[change_key] or nil
        if pending_change then
            local mark = trim_text(get_tree_item_text(item, 0))
            if mark == PENDING_CHECKED_MARK then
                pending_change.is_approved = true
            elseif mark == PENDING_UNCHECKED_MARK then
                pending_change.is_approved = false
            end
        end
    end
end

local function set_all_pending_changes_approved(approved)
    if not pending_change_item_map then
        return
    end

    for item, change_key in pairs(pending_change_item_map) do
        local pending_change = change_key and pending_change_by_key[change_key] or nil
        if pending_change then
            pending_change.is_approved = approved == true
            refresh_pending_tree_item(item, pending_change)
        end
    end
    log_pending_review_event(string.format("%s %d 条待审核建议", approved == true and "全选" or "全部忽略", tonumber(#(PendingChanges or {})) or 0))
    sync_pending_preview_changes(nil)
    safe_refresh_tree_widget(pending_report_tree)
end

function release_pending_report_ui(should_sync)
    if is_releasing_pending_report_ui then
        return
    end

    is_releasing_pending_report_ui = true
    local window_to_hide = pending_report_window

    if should_sync ~= false then
        sync_pending_changes_from_tree()
        sync_pending_preview_changes(nil)
    end

    -- 清理 applied toggle 状态
    applied_toggle_tree = nil
    applied_toggle_entries = {}
    applied_toggle_item_map = {}

    pending_report_tree = nil
    pending_report_window = nil
    pending_report_detail_view = nil
    applied_report_detail_view = nil
    pending_change_item_map = {}
    pending_item_tc_map = {}
    if window_to_hide then
        pcall(function() window_to_hide:Hide() end)
    end
    if pending_detail_window then
        pcall(function() pending_detail_window:Hide() end)
    end
    pending_detail_window = nil
    if applied_report_detail_window then
        pcall(function() applied_report_detail_window:Hide() end)
    end
    applied_report_detail_window = nil
    collectgarbage("collect")
    is_releasing_pending_report_ui = false
end

function reset_pending_review_session()
    release_pending_report_ui(false)
    PendingChanges = {}
    pending_change_by_key = {}
    pending_change_item_map = {}
    pending_item_tc_map = {}
    pending_report_detail_view = nil
    pending_detail_window = nil
    applied_report_detail_view = nil
    applied_report_detail_window = nil
    applied_toggle_tree = nil
    applied_toggle_entries = {}
    applied_toggle_item_map = {}
    pending_report_summary_text = ""
end

function apply_pending_report_tree_layout(tree)
    if not tree then
        return
    end

    pcall(function() tree.ColumnCount = 5 end)
    pcall(function() tree.HeaderHidden = false end)
    pcall(function() tree.RootIsDecorated = false end)
    pcall(function() tree.ItemsExpandable = false end)
    pcall(function() tree.UniformRowHeights = false end)
    pcall(function() tree.AlternatingRowColors = true end)
    pcall(function() tree.WordWrap = true end)
    pcall(function() tree:SetHeaderLabels({"状态", "行号", "概览", "原因", ""}) end)
    pcall(function() tree.ColumnWidth[0] = 42 end)
    pcall(function() tree.ColumnWidth[1] = 66 end)
    pcall(function() tree.ColumnWidth[2] = 290 end)
    pcall(function() tree.ColumnWidth[3] = 82 end)
    pcall(function() tree.ColumnWidth[4] = 52 end)
    safe_refresh_tree_widget(tree)
end

-- 预计算 pending_change 的跳转时间码
pending_item_tc_map = {}

local function precompute_pending_timecode(pending_change)
    local row_id = pending_change.row_id or pending_change.row_id_1
    if not row_id then return nil end
    local row = find_row_by_id(row_id)
    if not row or not row.start_frame then return nil end
    local abs_start = tonumber(row.start_frame) or 0
    local fps = row.fps or current_fps
    if fps <= 0 then fps = 24.0 end
    local total_sec = abs_start / fps
    local hh = math.floor(total_sec / 3600)
    local rem = total_sec % 3600
    local mm = math.floor(rem / 60)
    local ss = math.floor(rem % 60)
    local ff = abs_start - math.floor((hh * 3600 + mm * 60 + ss) * fps)
    ff = math.max(0, math.min(ff, math.max(1, math.floor(fps + 0.5)) - 1))
    return string.format("%02d:%02d:%02d:%02d", hh, mm, ss, ff)
end

function render_pending_changes_to_tree(tree)
    if not tree then
        return nil
    end

    pcall(function() tree:Clear() end)
    pending_change_item_map = {}
    pending_item_tc_map = {}
    apply_pending_report_tree_layout(tree)
    local first_item = nil

    for _, pending_change in ipairs(PendingChanges or {}) do
        local ok_item, item = pcall(function() return tree:NewItem() end)
        if ok_item and item then
            refresh_pending_tree_item(item, pending_change)
            if pcall(function() tree:AddTopLevelItem(item) end) then
                pending_change_item_map[item] = get_pending_change_key(pending_change)
                pending_item_tc_map[item] = precompute_pending_timecode(pending_change)
                if not first_item then
                    first_item = item
                end
            end
        end
    end

    safe_refresh_tree_widget(tree)
    return first_item
end

function show_revert_applied_dialog(report_entries)
    if not report_entries or #report_entries == 0 then return end

    -- 收集有 row_id 的已应用条目
    local revertable = {}
    for idx, entry in ipairs(report_entries) do
        if entry.row_id then
            revertable[#revertable + 1] = { idx = idx, entry = entry }
        end
    end
    if #revertable == 0 then return end

    local dlg_height = math.min(400, math.max(200, #revertable * 28 + 110))
    local revert_dlg = dispatcher:AddWindow({
        ID = "RevertAppliedDialog",
        WindowTitle = "取消部分自动应用",
        Geometry = {460, 200, 480, dlg_height},
    },
    ui:VGroup{
        Spacing = 8,
        ContentsMargins = 10,
        ui:Label{
            ID = "RevertHintLabel",
            Text = "取消勾选后点击「确认取消」，对应行将恢复原文：",
            Weight = 0,
            WordWrap = true,
            MinimumSize = {0, 22}
        },
        ui:Tree{
            ID = "RevertTree",
            Weight = 1,
            MinimumSize = {0, 120},
            Events = { ItemClicked = true }
        },
        ui:HGroup{
            Weight = 0,
            Spacing = 6,
            ui:HGap(0, 1),
            ui:Button{ ID = "RevertSelectAllBtn", Text = "全选", Weight = 0, MinimumSize = {72, 28} },
            ui:Button{ ID = "RevertUnselectAllBtn", Text = "全不选", Weight = 0, MinimumSize = {72, 28} },
            ui:Button{ ID = "RevertConfirmBtn", Text = "确认取消", Weight = 0, MinimumSize = {88, 28} },
            ui:Button{ ID = "RevertCancelBtn", Text = "返回", Weight = 0, MinimumSize = {72, 28} }
        }
    })

    local dlg_items = revert_dlg:GetItems()
    local revert_tree = dlg_items and dlg_items.RevertTree or nil
    if not revert_tree then
        pcall(function() revert_dlg:Hide() end)
        return
    end

    -- 配置 Tree
    pcall(function() revert_tree.ColumnCount = 4 end)
    pcall(function() revert_tree.HeaderHidden = false end)
    pcall(function() revert_tree.RootIsDecorated = false end)
    pcall(function() revert_tree.ItemsExpandable = false end)
    pcall(function() revert_tree.AlternatingRowColors = true end)
    pcall(function() revert_tree:SetHeaderLabels({"", "行号", "原句", "修正"}) end)
    pcall(function() revert_tree.ColumnWidth[0] = 28 end)
    pcall(function() revert_tree.ColumnWidth[1] = 42 end)
    pcall(function() revert_tree.ColumnWidth[2] = 190 end)
    pcall(function() revert_tree.ColumnWidth[3] = 190 end)

    -- 填充
    local item_map = {}  -- tree item -> revertable index
    for i, r in ipairs(revertable) do
        local ok_item, item = pcall(function() return revert_tree:NewItem() end)
        if ok_item and item then
            r.checked = true
            set_tree_item_text(item, 0, PENDING_CHECKED_MARK)
            set_tree_item_text(item, 1, tostring(r.entry.row_label or ""))
            set_tree_item_text(item, 2, tostring(r.entry.original or ""))
            set_tree_item_text(item, 3, tostring(r.entry.updated or ""))
            pcall(function() item.TextColor[0] = {R = 120, G = 235, B = 160, A = 255} end)
            pcall(function() item.TextColor[2] = {R = 230, G = 180, B = 170, A = 255} end)
            pcall(function() item.TextColor[3] = {R = 160, G = 230, B = 180, A = 255} end)
            pcall(function() revert_tree:AddTopLevelItem(item) end)
            item_map[item] = i
        end
    end
    safe_refresh_tree_widget(revert_tree)

    -- 事件: toggle
    function revert_dlg.On.RevertTree.ItemClicked(ev)
        local item = get_tree_event_value(ev, {"item", "Item", "currentItem", "CurrentItem"})
        if not item then item = get_selected_tree_node(revert_tree) end
        if not item then return end
        local col = get_tree_event_value(ev, {"column", "Column", "col", "Col"})
        if tonumber(col) == 0 then
            local ri = item_map[item]
            if ri and revertable[ri] then
                revertable[ri].checked = not revertable[ri].checked
                set_tree_item_text(item, 0, revertable[ri].checked and PENDING_CHECKED_MARK or PENDING_UNCHECKED_MARK)
                pcall(function()
                    item.TextColor[0] = revertable[ri].checked
                        and {R = 120, G = 235, B = 160, A = 255}
                        or {R = 210, G = 210, B = 210, A = 255}
                end)
                safe_refresh_tree_widget(revert_tree)
            end
        end
    end

    -- 全选 / 全不选
    function revert_dlg.On.RevertSelectAllBtn.Clicked(ev)
        for item, ri in pairs(item_map) do
            revertable[ri].checked = true
            set_tree_item_text(item, 0, PENDING_CHECKED_MARK)
            pcall(function() item.TextColor[0] = {R = 120, G = 235, B = 160, A = 255} end)
        end
        safe_refresh_tree_widget(revert_tree)
    end

    function revert_dlg.On.RevertUnselectAllBtn.Clicked(ev)
        for item, ri in pairs(item_map) do
            revertable[ri].checked = false
            set_tree_item_text(item, 0, PENDING_UNCHECKED_MARK)
            pcall(function() item.TextColor[0] = {R = 210, G = 210, B = 210, A = 255} end)
        end
        safe_refresh_tree_widget(revert_tree)
    end

    -- 确认取消: 将未勾选的条目恢复原文
    function revert_dlg.On.RevertConfirmBtn.Clicked(ev)
        local reverted_count = 0
        for _, r in ipairs(revertable) do
            if not r.checked and r.entry.row_id and r.entry.original then
                for _, row in ipairs(current_rows or {}) do
                    if row.id == r.entry.row_id then
                        row.text = r.entry.original
                        local tc_start, tc_end = get_row_timecodes(row)
                        row.display_text = build_tree_display_text(row.index, tc_start, tc_end, row.text)
                        get_row_search_text_lower(row)
                        reverted_count = reverted_count + 1
                        break
                    end
                end
            end
        end

        pcall(function() revert_dlg:Hide() end)

        if reverted_count > 0 then
            print(string.format("[Hooper AI 2.0] 用户取消了 %d 条自动应用，已恢复原文", reverted_count))
            invalidate_search_cache("applied_revert")
            SEARCH_VIEW.render_current_view(active_window or win)
            local status_label = (active_window or win) and (active_window or win):Find("StatusLabel")
            if status_label then
                status_label:Set("Text", string.format("已取消 %d 条自动应用，已恢复原文", reverted_count))
            end
        end
    end

    -- 返回
    function revert_dlg.On.RevertCancelBtn.Clicked(ev)
        pcall(function() revert_dlg:Hide() end)
    end

    function revert_dlg.On.RevertAppliedDialog.Close(ev)
        pcall(function() revert_dlg:Hide() end)
    end

    revert_dlg:Show()
end

-- ============================================================
-- 精修工具批量结果回顾对话框（与 AI 取消单个修改 UI 等价）
-- 入口：show_batch_review_dialog(task_name, report_entries)
-- - 默认每行勾选 = 保留修改；取消勾选 = 还原原文/原帧
-- - 支持 revert_kind: "text" 还原 row.text；"end_frame" 还原 row.end_frame
-- 故意不加 local，避免顶到 main chunk 200 local 上限
-- ============================================================
function show_batch_review_dialog(task_name, report_entries)
    if not report_entries or #report_entries == 0 then return end

    -- 收集有 row_id 的可还原条目
    local revertable = {}
    for idx, entry in ipairs(report_entries) do
        if entry and entry.row_id then
            revertable[#revertable + 1] = { idx = idx, entry = entry }
        end
    end
    if #revertable == 0 then return end

    local title = tostring(task_name or "批量修改") .. " · 回顾与撤销"
    local dlg_uid = tostring(os.time()) .. tostring(math.random(1000, 9999))
    local dlg_id = "BatchReviewDialog_" .. dlg_uid
    local dlg_height = math.min(440, math.max(220, #revertable * 28 + 130))

    local review_dlg = dispatcher:AddWindow({
        ID = dlg_id,
        WindowTitle = title,
        Geometry = {440, 220, 520, dlg_height},
    },
    ui:VGroup{
        Spacing = 8,
        ContentsMargins = 10,
        ui:Label{
            ID = "BatchReviewHint_" .. dlg_uid,
            Text = "默认全部保留。取消勾选某行后点「确认取消」即可还原该行。",
            Weight = 0,
            WordWrap = true,
            MinimumSize = {0, 22}
        },
        ui:Tree{
            ID = "BatchReviewTree_" .. dlg_uid,
            Weight = 1,
            MinimumSize = {0, 140},
            Events = { ItemClicked = true }
        },
        ui:HGroup{
            Weight = 0,
            Spacing = 6,
            ui:HGap(0, 1),
            ui:Button{ ID = "BatchReviewSelectAllBtn_" .. dlg_uid, Text = "全选", Weight = 0, MinimumSize = {72, 28} },
            ui:Button{ ID = "BatchReviewUnselectAllBtn_" .. dlg_uid, Text = "全不选", Weight = 0, MinimumSize = {72, 28} },
            ui:Button{ ID = "BatchReviewConfirmBtn_" .. dlg_uid, Text = "确认取消", Weight = 0, MinimumSize = {88, 28} },
            ui:Button{ ID = "BatchReviewCloseBtn_" .. dlg_uid, Text = "关闭", Weight = 0, MinimumSize = {72, 28} }
        }
    })

    local dlg_items = review_dlg:GetItems()
    local tree_key = "BatchReviewTree_" .. dlg_uid
    local review_tree = dlg_items and dlg_items[tree_key] or nil
    if not review_tree then
        pcall(function() review_dlg:Hide() end)
        return
    end

    pcall(function() review_tree.ColumnCount = 4 end)
    pcall(function() review_tree.HeaderHidden = false end)
    pcall(function() review_tree.RootIsDecorated = false end)
    pcall(function() review_tree.ItemsExpandable = false end)
    pcall(function() review_tree.AlternatingRowColors = true end)
    pcall(function() review_tree:SetHeaderLabels({"", "行号", "原句", "修改后"}) end)
    pcall(function() review_tree.ColumnWidth[0] = 28 end)
    pcall(function() review_tree.ColumnWidth[1] = 42 end)
    pcall(function() review_tree.ColumnWidth[2] = 210 end)
    pcall(function() review_tree.ColumnWidth[3] = 210 end)

    local item_map = {}
    for i, r in ipairs(revertable) do
        local ok_item, item = pcall(function() return review_tree:NewItem() end)
        if ok_item and item then
            r.checked = true
            set_tree_item_text(item, 0, PENDING_CHECKED_MARK)
            set_tree_item_text(item, 1, tostring(r.entry.row_label or ""))
            set_tree_item_text(item, 2, tostring(r.entry.original or ""))
            set_tree_item_text(item, 3, tostring(r.entry.updated or ""))
            pcall(function() item.TextColor[0] = {R = 120, G = 235, B = 160, A = 255} end)
            pcall(function() item.TextColor[2] = {R = 230, G = 180, B = 170, A = 255} end)
            pcall(function() item.TextColor[3] = {R = 160, G = 230, B = 180, A = 255} end)
            pcall(function() review_tree:AddTopLevelItem(item) end)
            item_map[item] = i
        end
    end
    safe_refresh_tree_widget(review_tree)

    local function set_item_checked(item, checked)
        local ri = item_map[item]
        if not ri or not revertable[ri] then return end
        revertable[ri].checked = checked and true or false
        set_tree_item_text(item, 0, checked and PENDING_CHECKED_MARK or PENDING_UNCHECKED_MARK)
        pcall(function()
            item.TextColor[0] = checked
                and {R = 120, G = 235, B = 160, A = 255}
                or {R = 210, G = 210, B = 210, A = 255}
        end)
    end

    review_dlg.On[tree_key].ItemClicked = function(ev)
        local item = get_tree_event_value(ev, {"item", "Item", "currentItem", "CurrentItem"})
        if not item then item = get_selected_tree_node(review_tree) end
        if not item then return end
        local col = get_tree_event_value(ev, {"column", "Column", "col", "Col"})
        if tonumber(col) == 0 then
            local ri = item_map[item]
            if ri and revertable[ri] then
                set_item_checked(item, not revertable[ri].checked)
                safe_refresh_tree_widget(review_tree)
            end
        end
    end

    review_dlg.On["BatchReviewSelectAllBtn_" .. dlg_uid].Clicked = function(ev)
        for item, _ in pairs(item_map) do set_item_checked(item, true) end
        safe_refresh_tree_widget(review_tree)
    end

    review_dlg.On["BatchReviewUnselectAllBtn_" .. dlg_uid].Clicked = function(ev)
        for item, _ in pairs(item_map) do set_item_checked(item, false) end
        safe_refresh_tree_widget(review_tree)
    end

    review_dlg.On["BatchReviewConfirmBtn_" .. dlg_uid].Clicked = function(ev)
        local reverted = 0
        local dirty_row_ids = {}
        for _, r in ipairs(revertable) do
            if not r.checked and r.entry and r.entry.row_id then
                local target_row
                for _, row in ipairs(current_rows or {}) do
                    if row and row.id == r.entry.row_id then
                        target_row = row
                        break
                    end
                end
                if target_row then
                    local kind = r.entry.revert_kind or "text"
                    if kind == "end_frame" and r.entry.original_end_frame ~= nil then
                        target_row.end_frame = tonumber(r.entry.original_end_frame) or target_row.end_frame
                        reverted = reverted + 1
                        mark_dirty_row(dirty_row_ids, target_row)
                    elseif kind == "text" and r.entry.original ~= nil then
                        target_row.text = r.entry.original
                        local tc_start, tc_end = get_row_timecodes(target_row)
                        target_row.display_text = build_tree_display_text(target_row.index, tc_start, tc_end, target_row.text)
                        get_row_search_text_lower(target_row)
                        reverted = reverted + 1
                        mark_dirty_row(dirty_row_ids, target_row)
                    end
                end
            end
        end

        pcall(function() review_dlg:Hide() end)

        if reverted > 0 then
            print(string.format("[Hooper AI 2.0] 用户取消了 %d 条「%s」修改", reverted, tostring(task_name or "批量")))
            invalidate_search_cache("batch_revert")
            -- end_frame 类型需要整树重建以更新时间显示
            local need_rebuild = false
            for _, r in ipairs(revertable) do
                if not r.checked and (r.entry.revert_kind == "end_frame") then
                    need_rebuild = true
                    break
                end
            end
            if need_rebuild then
                pcall(function() rebuild_tree_from_rows(current_rows, win) end)
            else
                pcall(function() sync_current_preview_tree(win, dirty_row_ids) end)
            end
            local status_label = win and win:Find("StatusLabel")
            if status_label then
                status_label:Set("Text", string.format("已取消 %d 条「%s」修改", reverted, tostring(task_name or "批量")))
            end
        end
    end

    review_dlg.On["BatchReviewCloseBtn_" .. dlg_uid].Clicked = function(ev)
        pcall(function() review_dlg:Hide() end)
    end

    review_dlg.On[dlg_id].Close = function(ev)
        pcall(function() review_dlg:Hide() end)
    end

    review_dlg:Show()
end

function show_ai_fix_report_window(task_name, fix_count, pending_count, report_entries)
    local has_applied_report = fix_count > 0 and #report_entries > 0
    local has_pending_report = pending_count > 0
    local is_mixed_report = has_applied_report and has_pending_report
    local is_empty_report = not has_applied_report and not has_pending_report
    local applied_report_plain, applied_report_html = report_helpers.build_report_payload(
        report_entries,
        "🎉 本轮未产生任何改动。"
    )
    local report_spacing = is_mixed_report and 8 or 8
    local report_margin = is_mixed_report and 12 or 10
    local pending_tree_min_height = is_mixed_report and 260 or (has_applied_report and 280 or 300)

    pending_report_summary_text = format_ai_report_overview_text(
        task_name,
        fix_count,
        pending_count
    )

    local report_geometry = {420, 220, 560, 240}
    if is_empty_report then
        report_geometry = {470, 210, 420, 170}
    elseif is_mixed_report then
        report_geometry = {410, 130, 560, 460}
    elseif has_pending_report then
        report_geometry = {420, 140, 540, 420}
    elseif has_applied_report then
        report_geometry = {410, 160, 620, 300}
    end

    local report_contents = {
        Spacing = report_spacing,
        ContentsMargins = report_margin,
        ui:Label{
            ID = "PendingSummaryLabel",
            Text = pending_report_summary_text,
            Weight = 0,
            WordWrap = true,
            Alignment = {AlignLeft = true, AlignVCenter = true},
            MinimumSize = {0, 24}
        },
    }

    if is_empty_report then
        report_contents = {
            Spacing = 8,
            ContentsMargins = 14,
            ui:Label{
                ID = "PendingSummaryLabel",
                Text = pending_report_summary_text,
                Weight = 0,
                WordWrap = false,
                Alignment = {AlignLeft = true, AlignVCenter = true},
                MinimumSize = {0, 24}
            },
            ui:VGap(4, 0),
            ui:Label{
                ID = "EmptyReportResult",
                Text = format_ai_applied_result_text(0, "本轮无需处理"),
                Weight = 0,
                WordWrap = false,
                Alignment = {AlignLeft = true, AlignVCenter = true},
                MinimumSize = {0, 28}
            },
            ui:Label{
                ID = "EmptyReportHint",
                Text = "<span style='color:#AEB7C3; font-size:12px;'>未发现需要自动应用或人工审核的字幕修改。</span>",
                Weight = 0,
                WordWrap = true,
                Alignment = {AlignLeft = true, AlignVCenter = true},
                MinimumSize = {0, 18}
            },
            ui:VGap(6, 0),
            ui:HGroup{
                Weight = 0,
                Spacing = 6,
                ui:HGap(0, 1),
                ui:Button{ ID = "CloseReportBtn", Text = "确认", Weight = 0, MinimumSize = {96, 30} }
            }
        }
    elseif has_applied_report then
        if is_mixed_report then
            table.insert(report_contents, ui:HGroup{
                Weight = 0,
                Spacing = 12,
                MinimumSize = {0, 62},
                ui:VGroup{
                    Weight = 1,
                    Spacing = 3,
                    MinimumSize = {0, 58},
                    ui:Label{
                        ID = "AppliedReportKicker",
                        Text = format_ai_applied_kicker_text(),
                        Weight = 0,
                        WordWrap = false,
                        Alignment = {AlignLeft = true, AlignVCenter = true},
                        MinimumSize = {0, 18}
                    },
                    ui:Label{
                        ID = "AppliedReportSummary",
                        Text = format_ai_applied_result_text(fix_count),
                        Weight = 0,
                        WordWrap = false,
                        Alignment = {AlignLeft = true, AlignVCenter = true},
                        MinimumSize = {0, 26}
                    },
                    ui:Label{
                        ID = "AppliedReportHint",
                        Text = format_ai_applied_hint_text(),
                        Weight = 0,
                        WordWrap = false,
                        Alignment = {AlignLeft = true, AlignVCenter = true},
                        MinimumSize = {0, 18}
                    }
                },
                ui:Button{
                    ID = "ShowAppliedReportBtn",
                    Text = "查看已自动应用详情",
                    Weight = 0,
                    MinimumSize = {150, 34}
                }
            })
        else
            table.insert(report_contents, ui:Label{
                ID = "AppliedReportLabel",
                Text = format_ai_section_label_text("已自动应用"),
                Weight = 0,
                Alignment = {AlignLeft = true, AlignVCenter = true},
                MinimumSize = {0, 24}
            })
            table.insert(report_contents, ui:TextEdit{
                ID = "AppliedReportContent",
                Text = applied_report_plain,
                ReadOnly = true,
                Weight = 1,
                MinimumSize = {0, 180}
            })
        end
    end

    if has_pending_report then
        table.insert(report_contents, ui:Label{
            ID = "PendingReportLabel",
            Text = format_ai_section_label_text("待人工审核"),
            Weight = 0,
            Alignment = {AlignLeft = true, AlignVCenter = true},
            MinimumSize = {0, 24}
        })
        table.insert(report_contents, ui:VGroup{
            Weight = 1,
            Spacing = 6,
            ui:Tree{
                ID = "PendingReviewTree",
                Weight = 1,
                MinimumSize = {0, pending_tree_min_height},
                Events = { ItemClicked = true, ItemDoubleClicked = true }
            },
            ui:HGroup{
                Weight = 0,
                Spacing = 6,
                ui:HGap(0, 1),
                ui:Button{ ID = "SelectAllPendingBtn", Text = "全选建议", Weight = 0, MinimumSize = {96, 28} },
                ui:Button{ ID = "IgnoreAllPendingBtn", Text = "全部忽略", Weight = 0, MinimumSize = {96, 28} },
                ui:Button{ ID = "CloseReportBtn", Text = "确认", Weight = 0, MinimumSize = {88, 28} }
            }
        })
    elseif not is_empty_report then
        table.insert(report_contents, ui:HGroup{
            Weight = 0,
            Spacing = 6,
            ui:HGap(0, 1),
            ui:Button{ ID = "RevertAppliedBtn", Text = "取消部分应用", Weight = 0, MinimumSize = {110, 28} },
            ui:Button{ ID = "CloseReportBtn", Text = "确认", Weight = 0, MinimumSize = {88, 28} }
        })
    end

    pending_report_window = dispatcher:AddWindow({
        ID = "ReportWindow",
        WindowTitle = task_name .. "报告",
        Geometry = report_geometry,
    },
    ui:VGroup(report_contents))

    local report_items = pending_report_window:GetItems()
    pending_report_tree = report_items and report_items.PendingReviewTree or nil
    pending_report_detail_view = nil
    if report_items and report_items.AppliedReportContent then
        report_helpers.set_textedit_rich_content(
            report_items.AppliedReportContent,
            applied_report_plain,
            applied_report_html
        )
    end
    if report_items and report_items.ShowAppliedReportBtn then
        function pending_report_window.On.ShowAppliedReportBtn.Clicked(ev)
            show_applied_report_detail_window(task_name, report_entries, fix_count)
        end
    end

    -- 保存 report_entries 供"取消部分应用"弹窗使用
    applied_toggle_tree = nil
    applied_toggle_entries = {}
    applied_toggle_item_map = {}
    if has_applied_report then
        for idx, entry in ipairs(report_entries) do
            if entry.row_id then
                entry._toggle_approved = true
                applied_toggle_entries[idx] = entry
            end
        end
    end

    if has_pending_report then
        local first_pending_item = render_pending_changes_to_tree(pending_report_tree)
        if first_pending_item then
            set_pending_tree_current_item(pending_report_tree, first_pending_item)
        end
    end

    function pending_report_window.On.ReportWindow.Close(ev)
        release_pending_report_ui(true)
    end

    if has_pending_report then
        -- 缓存 timeline 引用 + 起始时间码，避免每次点击都走 API 链
        local cached_timeline = nil
        local cached_start_tc = nil
        local report_title = task_name .. "报告"
        pcall(function()
            if resolve then
                local pm = resolve:GetProjectManager()
                local proj = pm and pm:GetCurrentProject()
                cached_timeline = proj and proj:GetCurrentTimeline()
                if cached_timeline then
                    cached_start_tc = cached_timeline:GetStartTimecode()
                    -- 预热 API：读取当前时间码，避免首次跳转延迟
                    cached_timeline:GetCurrentTimecode()
                end
            end
        end)

        local last_jumped_item = nil

        local function do_jump_with_feedback(item)
            -- 恢复上一个跳转项图标
            if last_jumped_item then
                pcall(function() set_tree_item_text(last_jumped_item, 4, "[ ▶ ]") end)
            end
            -- 当前项标记为 ✓
            set_tree_item_text(item, 4, "[ ✓ ]")
            last_jumped_item = item
            -- 执行跳转
            local tc = pending_item_tc_map[item]
            if tc then
                local ok = cached_timeline:SetCurrentTimecode(tc)
                if not ok and cached_start_tc then
                    cached_timeline:SetCurrentTimecode(cached_start_tc)
                end
            end
        end

        function pending_report_window.On.PendingReviewTree.ItemClicked(ev)
            local item = get_pending_tree_event_item(ev)
            if not item then return end
            set_pending_tree_current_item(pending_report_tree, item)
            local col = get_pending_tree_event_column(ev)
            if col == 0 then
                toggle_pending_change_item(item)
            elseif col == 4 and cached_timeline then
                do_jump_with_feedback(item)
            end
        end

        function pending_report_window.On.PendingReviewTree.ItemDoubleClicked(ev)
            local item = get_pending_tree_event_item(ev)
            if not item then return end
            set_pending_tree_current_item(pending_report_tree, item)
            local col = get_pending_tree_event_column(ev)
            if col == 4 and cached_timeline then
                do_jump_with_feedback(item)
            elseif col ~= 0 then
                show_pending_detail_window_for_item(item)
            end
        end

        function pending_report_window.On.SelectAllPendingBtn.Clicked(ev)
            set_all_pending_changes_approved(true)
        end

        function pending_report_window.On.IgnoreAllPendingBtn.Clicked(ev)
            set_all_pending_changes_approved(false)
        end
    end

    -- "取消部分应用" 按钮 → 打开独立弹窗
    if report_items and report_items.RevertAppliedBtn then
        if has_applied_report and next(applied_toggle_entries) then
            function pending_report_window.On.RevertAppliedBtn.Clicked(ev)
                show_revert_applied_dialog(report_entries)
            end
        else
            pcall(function() report_items.RevertAppliedBtn.Enabled = false end)
        end
    end

    function pending_report_window.On.CloseReportBtn.Clicked(ev)
        release_pending_report_ui(true)
    end

    pending_report_window:Show()
    if has_pending_report then
        apply_pending_report_tree_layout(pending_report_tree)
        safe_refresh_tree_widget(pending_report_tree)
    end
end

local function apply_approved_pending_changes_to_rows(rows)
    if pending_report_tree then
        sync_pending_changes_from_tree()
    end

    local row_list = rows or current_rows
    if type(row_list) ~= "table" or #row_list == 0 then
        return 0
    end

    local row_map = build_pending_row_map(row_list)

    local applied_count = 0
    local dirty_row_ids = {}
    for _, pending_change in ipairs(PendingChanges or {}) do
        local applied_row_count = apply_pending_change_to_row_map(pending_change, row_map, "[Warning] PendingChanges row_id not found: ", dirty_row_ids)
        if applied_row_count > 0 and pending_change.is_approved then
            applied_count = applied_count + 1
        elseif applied_row_count == 0 then
            local warning_msg = "[Warning] PendingChanges row_id not found: " .. tostring(get_pending_change_key(pending_change))
            print(warning_msg)
            LogMsg(warning_msg)
        end
    end

    return applied_count, dirty_row_ids
end

local function get_row_from_tree_selection(target_window)
    local tree = find_window_item(target_window, "SubtitleTree", "MiniSubtitleTree")
    if not tree then return nil, nil end

    local selected = get_selected_tree_node(tree)
    if not selected then
        return nil, nil
    end

    local data = subtitle_data_map[selected]
    if not data then
        local t0 = ""
        local ok_t, tx = pcall(function() return (selected.Text and selected.Text[0]) end)
        if ok_t and tx then t0 = tostring(tx) end
        local idx = tonumber(string.match(t0, "^%[(%d+)%]"))
        if idx and current_rows and current_rows[idx] then
            data = current_rows[idx]
        end
    end

    if data and data.id then
        current_selected_row_id = data.id
    end

    return data, selected
end

render_rows_to_window = function(target_window, rows_override)
    local window = resolve_window(target_window)
    local tree = find_window_item(window, "SubtitleTree", "MiniSubtitleTree")
    local rows = type(rows_override) == "table" and rows_override or {}
    local count = 0
    local next_map = {}
    local next_row_id_map = {}
    local selected_node = nil

    if window == active_window then
        subtitle_data_map = {}
        subtitle_row_id_node_map = {}
    end

    if not tree then
        return 0
    end

    local function populate_tree()
        pcall(function() tree:Clear() end)

        for _, row in ipairs(rows) do
            local ok_item, item = pcall(function() return tree:NewItem() end)
            if ok_item and item then
                set_tree_node_display_text(item, row.display_text or "")
                if pcall(function() tree:AddTopLevelItem(item) end) then
                    next_map[item] = row
                    local row_id = trim_text(row.id)
                    if row_id ~= "" then
                        next_row_id_map[row_id] = item
                    end
                    count = count + 1
                    if row.id and row.id == current_selected_row_id then
                        selected_node = item
                    end
                end
            end
        end

        if selected_node then
            pcall(function() tree:SetSelectedNode(selected_node) end)
        end
    end

    local ok_render, render_err = with_tree_updates_suspended(tree, window, populate_tree)
    if not ok_render then
        local warning_msg = "[Warning] 全量刷新字幕树失败，已回退普通重建: " .. tostring(render_err)
        print(warning_msg)
        if type(LogMsg) == "function" then
            pcall(function() LogMsg(warning_msg) end)
        end
        populate_tree()
    end

    if window == active_window then
        subtitle_data_map = next_map
        subtitle_row_id_node_map = next_row_id_map
    end

    -- 直接调用 render_rows_to_window 的路径不一定走 SEARCH_VIEW，这里清掉指纹/基线
    -- 避免后续 SEARCH_VIEW.render_current_view 误判"已渲染相同内容"而跳过重建
    if window and SEARCH_VIEW then
        if SEARCH_VIEW.rendered_signatures then
            SEARCH_VIEW.rendered_signatures[window] = nil
        end
        if SEARCH_VIEW.tree_baselines then
            SEARCH_VIEW.tree_baselines[window] = nil
        end
    end

    return count
end

-- 全局函数（避免主 chunk local 数量逼近 Lua 5.1 的 200 上限）
function compute_render_signature(context)
    if type(context) ~= "table" then
        return ""
    end
    return string.format(
        "%d|%s|%s|%d|%s",
        SEARCH_VIEW.dataset_revision,
        tostring(context.mode or ""),
        tostring(context.query or ""),
        tonumber(context.visible_count) or 0,
        context.truncated and "1" or "0"
    )
end

-- 在 Fusion TreeItem 上设置可见性。Fusion 在不同版本中暴露的接口不一致，
-- 因此尝试多种方式，只要任何一种成功即视为成功。返回 true 表示"我们做了尝试且没有抛错"。
function set_tree_node_hidden(node, hidden)
    if not node then return false end
    local any_ok = false
    if pcall(function() node.Hidden = hidden end) then any_ok = true end
    if node.SetAttrs then
        if pcall(function() node:SetAttrs({Hidden = hidden}) end) then any_ok = true end
    end
    if hidden then
        if node.Hide and pcall(function() node:Hide() end) then any_ok = true end
    else
        if node.Show and pcall(function() node:Show() end) then any_ok = true end
    end
    return any_ok
end

-- 探测当前 Fusion 版本是否真的支持在 TreeItem 上隐藏节点。
-- 取一个样本节点 set hidden=true，然后读回判断。
function detect_tree_hide_support(sample_node)
    if not sample_node then return false end
    local original = nil
    pcall(function() original = sample_node.Hidden end)
    local supported = false
    pcall(function() sample_node.Hidden = true end)
    pcall(function() supported = (sample_node.Hidden == true) end)
    -- 还原
    pcall(function() sample_node.Hidden = original or false end)
    return supported
end

-- 把 visible_set 中包含的 row.id 对应节点 Show，其它节点 Hide。
-- 返回 true 表示成功应用，false 表示条件不满足或失败需回退。
function apply_visibility_filter_to_window(window, visible_set, selected_row_id)
    if not window or window ~= active_window then return false end
    if type(subtitle_row_id_node_map) ~= "table" then return false end
    local tree = find_window_item(window, "SubtitleTree", "MiniSubtitleTree")
    if not tree then return false end

    local first_visible_node = nil
    local ok = with_tree_updates_suspended(tree, window, function()
        for row_id, node in pairs(subtitle_row_id_node_map) do
            local should_show = visible_set[row_id] == true
            set_tree_node_hidden(node, not should_show)
            if should_show and not first_visible_node then
                first_visible_node = node
            end
        end
        if selected_row_id and subtitle_row_id_node_map[selected_row_id] and visible_set[selected_row_id] then
            pcall(function() tree:SetSelectedNode(subtitle_row_id_node_map[selected_row_id]) end)
        end
    end)
    return ok == true
end

SEARCH_VIEW.invalidate_rendered_signature = function(target_window)
    if not SEARCH_VIEW.rendered_signatures then
        SEARCH_VIEW.rendered_signatures = {}
        return
    end
    if target_window == nil then
        SEARCH_VIEW.rendered_signatures = {}
    else
        SEARCH_VIEW.rendered_signatures[target_window] = nil
    end
end

SEARCH_VIEW.invalidate_tree_baseline = function(target_window)
    if not SEARCH_VIEW.tree_baselines then
        SEARCH_VIEW.tree_baselines = {}
        return
    end
    if target_window == nil then
        SEARCH_VIEW.tree_baselines = {}
    else
        SEARCH_VIEW.tree_baselines[target_window] = nil
    end
end

SEARCH_VIEW.render_current_view = function(target_window, options)
    local context = SEARCH_VIEW.build_current_view_context()
    local window = resolve_window(target_window)
    options = options or {}

    if not window then return context end

    local current_sig = compute_render_signature(context)
    local prev_sig = SEARCH_VIEW.rendered_signatures and SEARCH_VIEW.rendered_signatures[window]

    -- 1) 完全相同的视图 → 直接跳过
    if options.force_render ~= true and prev_sig ~= nil and prev_sig == current_sig then
        if options.update_status ~= false and context.status_text and context.status_text ~= "" then
            update_shared_status(window, context.status_text)
        end
        return context
    end

    -- 2) 如果当前 tree 已经"全量铺好"且基线仍然有效，
    --    走 Hidden 切换的快路径——这是消除 587 行重建卡顿的关键。
    local rows_total = current_rows and #current_rows or 0
    local baseline = SEARCH_VIEW.tree_baselines and SEARCH_VIEW.tree_baselines[window]
    local can_use_hidden_path = (
        options.force_rebuild ~= true
        and baseline ~= nil
        and baseline.dataset_revision == SEARCH_VIEW.dataset_revision
        and baseline.total_rendered == rows_total
        and baseline.hide_supported == true
        and window == active_window
        and rows_total > 0
    )

    if can_use_hidden_path then
        local visible_set = {}
        for _, row in ipairs(context.visible_rows or {}) do
            if row and row.id then visible_set[row.id] = true end
        end
        if apply_visibility_filter_to_window(window, visible_set, current_selected_row_id) then
            if SEARCH_VIEW.rendered_signatures then
                SEARCH_VIEW.rendered_signatures[window] = current_sig
            end
            if options.update_status ~= false and context.status_text and context.status_text ~= "" then
                update_shared_status(window, context.status_text)
            end
            return context
        end
        -- 失败则回退到重建分支
    end

    -- 3) 重建分支。如果 current_rows 数量在阈值以内，
    --    我们一次性渲染所有 rows 然后隐藏非匹配项，建立"全量基线"，
    --    后续过滤就能走快路径。
    local should_build_baseline = (
        options.force_rebuild ~= true
        and rows_total > 0
        and rows_total <= SEARCH_VIEW.large_dataset_threshold
        and window == active_window
    )

    if should_build_baseline then
        render_rows_to_window(window, current_rows)
        -- 探测是否支持 Hidden（一次性，缓存到基线里）
        local hide_supported = false
        local probe_node = nil
        for _, node in pairs(subtitle_row_id_node_map or {}) do
            probe_node = node
            break
        end
        if probe_node then
            hide_supported = detect_tree_hide_support(probe_node)
        end

        if hide_supported then
            local visible_set = {}
            for _, row in ipairs(context.visible_rows or {}) do
                if row and row.id then visible_set[row.id] = true end
            end
            -- 如果当前视图就是全量（mode=full 且无 truncated），所有都可见，不需要隐藏
            local need_filter = not (context.mode == SEARCH_VIEW.modes.full and not context.truncated and (context.visible_count or 0) == rows_total)
            if need_filter then
                apply_visibility_filter_to_window(window, visible_set, current_selected_row_id)
            end
            SEARCH_VIEW.tree_baselines[window] = {
                dataset_revision = SEARCH_VIEW.dataset_revision,
                total_rendered = rows_total,
                hide_supported = true
            }
        else
            -- 不支持 Hidden：只能退回原来的"按 visible_rows 重建"策略，每次过滤都重建。
            -- 当前 tree 已经铺了所有 rows，需要重新只渲染 visible_rows。
            if context.visible_count ~= rows_total then
                render_rows_to_window(window, context.visible_rows)
            end
            SEARCH_VIEW.tree_baselines[window] = nil
        end
    else
        -- 数据集过大或非活动窗口 → 老路径，只渲染 visible_rows
        render_rows_to_window(window, context.visible_rows)
        SEARCH_VIEW.tree_baselines[window] = nil
    end

    if SEARCH_VIEW.rendered_signatures then
        SEARCH_VIEW.rendered_signatures[window] = current_sig
    end
    if options.update_status ~= false and context.status_text and context.status_text ~= "" then
        update_shared_status(window, context.status_text)
    end
    return context
end

local function clear_tree_for_window(target_window)
    local window = resolve_window(target_window)
    local tree = find_window_item(window, "SubtitleTree", "MiniSubtitleTree")
    if tree then
        pcall(function() tree:Clear() end)
    end
    if window == active_window then
        subtitle_data_map = {}
        subtitle_row_id_node_map = {}
    end
    if window and SEARCH_VIEW then
        if SEARCH_VIEW.invalidate_rendered_signature then
            SEARCH_VIEW.invalidate_rendered_signature(window)
        end
        if SEARCH_VIEW.invalidate_tree_baseline then
            SEARCH_VIEW.invalidate_tree_baseline(window)
        end
    end
end

local function apply_shared_state_to_window(target_window)
    local window = resolve_window(target_window)
    if not window then return end

    sync_track_control(window)
    sync_search_control(window)

    if not is_mini_window(window) then
        sync_target_track_control()
    end

    if current_rows and #current_rows > 0 then
        SEARCH_VIEW.render_current_view(window)
    else
        clear_tree_for_window(window)
    end

    set_subtitle_loaded_state(is_subtitle_loaded, nil, window)
    update_shared_status(window, shared_status_text)
end

function apply_lightweight_shared_state_to_window(target_window)
    local window = resolve_window(target_window)
    if not window then return end

    sync_track_control(window)
    sync_search_control(window)

    if not is_mini_window(window) then
        sync_target_track_control()
    end

    set_subtitle_loaded_state(is_subtitle_loaded, nil, window)
    update_shared_status(window, shared_status_text)
end

function register_ui_timer(timer, handler)
    if not timer or type(handler) ~= "function" then
        return false
    end

    local timer_id = tostring(timer.ID or "")
    if timer_id == "" then
        return false
    end

    ui_timer_handlers[timer_id] = handler
    return true
end

function restart_ui_timer(timer)
    if not timer then
        return false
    end

    pcall(function() timer:Stop() end)
    local ok = pcall(function() timer:Start() end)
    return ok == true
end

function disp.On.Timeout(ev)
    local timer_id = tostring(ev and ev.who or "")
    local handler = ui_timer_handlers[timer_id]
    if handler then
        handler(ev)
    end
end

local function shell_quote(value)
    local s = tostring(value or "")
    return "'" .. s:gsub("'", "'\\''") .. "'"
end

local function run_shell_capture(cmd)
    local handle = io.popen(cmd .. " 2>&1")
    if not handle then
        return false, "无法启动命令"
    end

    local output = handle:read("*a") or ""
    local ok, _, code = handle:close()
    if ok == true or code == 0 then
        return true, output
    end

    return false, output
end

local function parse_fps(fps_str)
    if not fps_str then return 24.0 end
    local s = trim(tostring(fps_str))
    if EXACT_FPS[s] then
        return EXACT_FPS[s]
    end
    local f = tonumber(s)
    return f or 24.0
end

-- ========== 帧 -> SMPTE 时间码 ==========
local function frames_to_timecode(frames, fps)
    if fps <= 0 then fps = 24.0 end
    local fps_int = math.max(1, math.floor(fps + 0.5))
    frames = math.max(0, math.floor(frames))
    
    local total_sec = frames / fps
    local h = math.floor(total_sec / 3600)
    local remaining = total_sec % 3600
    local m = math.floor(remaining / 60)
    local s = math.floor(remaining % 60)
    local ff = frames - math.floor((h * 3600 + m * 60 + s) * fps)
    ff = math.max(0, math.min(ff, fps_int - 1))
    
    return string.format("%02d:%02d:%02d:%02d", h, m, s, ff)
end

-- ========== 获取 Resolve API ==========
local function get_resolve()
    if not resolve then
        print("[Hooper AI 2.0] ERROR: resolve 未注入")
        return nil
    end
    return resolve
end

local function get_subtitle_track_type_and_count(timeline)
    if not timeline then
        return "subtitle", 0
    end

    local candidates = {"subtitle", 3, "Subtitle"}
    for _, track_type in ipairs(candidates) do
        local ok, track_count = pcall(function() return timeline:GetTrackCount(track_type) end)
        if ok and track_count ~= nil and track_count ~= false then
            return track_type, tonumber(track_count) or 0
        end
    end

    return "subtitle", 0
end

local function get_subtitle_track_items(track_index, timeline_override)
    if not track_index or track_index < 1 then
        return nil, "字幕轨索引无效"
    end

    local timeline = timeline_override
    if not timeline then
        local resolve = get_resolve()
        if not resolve then return nil, "无法获取 Resolve" end

        local pm = resolve:GetProjectManager()
        if not pm then return nil, "无法获取 ProjectManager" end

        local project = pm:GetCurrentProject()
        if not project then return nil, "没有打开的项目" end

        timeline = project:GetCurrentTimeline()
        if not timeline then return nil, "没有时间线" end
    end

    local track_type, track_count = get_subtitle_track_type_and_count(timeline)
    if track_index > track_count then
        return nil, "轨道 " .. tostring(track_index) .. " 不存在", track_count, track_type
    end

    local tried = {}
    local candidates = {track_type, "subtitle", 3, "Subtitle"}
    for _, candidate in ipairs(candidates) do
        local key = type(candidate) .. ":" .. tostring(candidate)
        if not tried[key] then
            tried[key] = true
            local ok_items, items_ret = pcall(function() return timeline:GetItemListInTrack(candidate, track_index) end)
            if ok_items then
                return items_ret or {}, nil, track_count, candidate
            end
        end
    end

    return nil, "无法读取轨道 " .. tostring(track_index) .. " 的字幕片段", track_count, track_type
end

local function add_subtitle_track(timeline)
    if not timeline then return false end

    local tried = {}
    local candidates = {"subtitle", "Subtitle", 3}
    for _, track_type in ipairs(candidates) do
        local key = type(track_type) .. ":" .. tostring(track_type)
        if not tried[key] then
            tried[key] = true
            local ok, ret = pcall(function() return timeline:AddTrack(track_type) end)
            if ok and ret ~= false then
                return true
            end
        end
    end

    return false
end

local function ensure_subtitle_track_exists(track_index, timeline_override)
    if not track_index or track_index < 1 then
        return nil, "字幕轨索引无效"
    end

    local timeline = timeline_override
    if not timeline then
        local resolve = get_resolve()
        if not resolve then return nil, "无法获取 Resolve" end

        local pm = resolve:GetProjectManager()
        if not pm then return nil, "无法获取 ProjectManager" end

        local project = pm:GetCurrentProject()
        if not project then return nil, "没有打开的项目" end

        timeline = project:GetCurrentTimeline()
        if not timeline then return nil, "没有时间线" end
    end

    local _, track_count = get_subtitle_track_type_and_count(timeline)
    while track_count < track_index do
        local previous_count = track_count
        if not add_subtitle_track(timeline) then
            return nil, "无法创建字幕轨 " .. tostring(track_index)
        end

        _, track_count = get_subtitle_track_type_and_count(timeline)
        if track_count <= previous_count then
            return nil, "创建字幕轨后轨道数未变化"
        end
    end

    return get_subtitle_track_items(track_index, timeline)
end

local function get_subtitle_track_state_snapshot(timeline_override)
    local timeline = timeline_override
    if not timeline then
        local resolve = get_resolve()
        if not resolve then return nil, "无法获取 Resolve" end

        local pm = resolve:GetProjectManager()
        if not pm then return nil, "无法获取 ProjectManager" end

        local project = pm:GetCurrentProject()
        if not project then return nil, "没有打开的项目" end

        timeline = project:GetCurrentTimeline()
        if not timeline then return nil, "没有时间线" end
    end

    local track_type, track_count = get_subtitle_track_type_and_count(timeline)
    local snapshot = {
        track_type = track_type,
        track_count = track_count,
        enabled = {}
    }

    for i = 1, track_count do
        local ok, enabled = pcall(function() return timeline:GetIsTrackEnabled(track_type, i) end)
        snapshot.enabled[i] = ok and (enabled and true or false) or nil
    end

    return snapshot
end

local function format_subtitle_track_state_snapshot(snapshot)
    if not snapshot or not snapshot.track_count then
        return "无字幕轨状态"
    end

    local parts = {}
    for i = 1, snapshot.track_count do
        local enabled = snapshot.enabled and snapshot.enabled[i]
        local enabled_text = enabled == nil and "?" or (enabled and "on" or "off")
        table.insert(parts, string.format("%d[E:%s]", i, enabled_text))
    end
    return table.concat(parts, " ")
end

local function restore_subtitle_track_state_snapshot(snapshot, timeline_override)
    if not snapshot then
        return false, "缺少字幕轨状态快照"
    end

    local timeline = timeline_override
    if not timeline then
        local resolve = get_resolve()
        if not resolve then return false, "无法获取 Resolve" end

        local pm = resolve:GetProjectManager()
        if not pm then return false, "无法获取 ProjectManager" end

        local project = pm:GetCurrentProject()
        if not project then return false, "没有打开的项目" end

        timeline = project:GetCurrentTimeline()
        if not timeline then return false, "没有时间线" end
    end

    local track_type = snapshot.track_type or "subtitle"
    local failures = {}

    for i = 1, snapshot.track_count or 0 do
        local enabled = snapshot.enabled and snapshot.enabled[i]
        if enabled ~= nil then
            local ok, ret = pcall(function() return timeline:SetTrackEnable(track_type, i, enabled) end)
            if not ok or ret == false then
                table.insert(failures, "E" .. tostring(i))
            end
        end
    end

    if #failures > 0 then
        return false, "恢复字幕轨状态失败: " .. table.concat(failures, ", ")
    end

    return true
end

local function unlock_all_subtitle_tracks(timeline_override)
    local timeline = timeline_override
    if not timeline then
        local resolve = get_resolve()
        if not resolve then return false, "无法获取 Resolve" end

        local pm = resolve:GetProjectManager()
        if not pm then return false, "无法获取 ProjectManager" end

        local project = pm:GetCurrentProject()
        if not project then return false, "没有打开的项目" end

        timeline = project:GetCurrentTimeline()
        if not timeline then return false, "没有时间线" end
    end

    local track_type, track_count = get_subtitle_track_type_and_count(timeline)
    for i = 1, track_count do
        pcall(function() return timeline:SetTrackLock(track_type, i, false) end)
    end

    return true
end

local function get_enabled_subtitle_tracks(snapshot)
    local tracks = {}
    if not snapshot or not snapshot.track_count then
        return tracks
    end

    for i = 1, snapshot.track_count do
        if snapshot.enabled and snapshot.enabled[i] == true then
            table.insert(tracks, i)
        end
    end

    return tracks
end

local function lock_non_target_subtitle_tracks(track_index, timeline_override)
    local timeline = timeline_override
    if not timeline then
        local resolve = get_resolve()
        if not resolve then return false, "无法获取 Resolve" end

        local pm = resolve:GetProjectManager()
        if not pm then return false, "无法获取 ProjectManager" end

        local project = pm:GetCurrentProject()
        if not project then return false, "没有打开的项目" end

        timeline = project:GetCurrentTimeline()
        if not timeline then return false, "没有时间线" end
    end

    local track_type, track_count = get_subtitle_track_type_and_count(timeline)
    local failures = {}
    for i = 1, track_count do
        if i ~= track_index then
            local ok, ret = pcall(function() return timeline:SetTrackLock(track_type, i, true) end)
            if not ok or ret == false then
                table.insert(failures, tostring(i))
            end
        end
    end

    if #failures > 0 then
        return false, "锁定非目标字幕轨失败: " .. table.concat(failures, ", ")
    end

    return true
end

local function isolate_subtitle_target_track(track_index, timeline_override)
    if not track_index or track_index < 1 then
        return false, "字幕目标轨无效"
    end

    local timeline = timeline_override
    if not timeline then
        local resolve = get_resolve()
        if not resolve then return false, "无法获取 Resolve" end

        local pm = resolve:GetProjectManager()
        if not pm then return false, "无法获取 ProjectManager" end

        local project = pm:GetCurrentProject()
        if not project then return false, "没有打开的项目" end

        timeline = project:GetCurrentTimeline()
        if not timeline then return false, "没有时间线" end
    end

    local track_type, track_count = get_subtitle_track_type_and_count(timeline)
    if track_index > track_count then
        return false, "目标字幕轨不存在", false
    end

    for i = 1, track_count do
        local desired_enabled = (i == track_index)
        pcall(function() return timeline:SetTrackEnable(track_type, i, desired_enabled) end)
    end

    local verify_ok, actual_enabled = pcall(function() return timeline:GetIsTrackEnabled(track_type, track_index) end)
    local state_snapshot = select(1, get_subtitle_track_state_snapshot(timeline))
    local summary = format_subtitle_track_state_snapshot(state_snapshot)
    local enabled_tracks = get_enabled_subtitle_tracks(state_snapshot)

    if not verify_ok or actual_enabled == false then
        return false, "目标字幕轨未成功启用，当前状态: " .. summary, false
    end

    if #enabled_tracks == 1 and enabled_tracks[1] == track_index then
        return true, "目标轨已独占启用；当前状态: " .. summary, false
    end

    local lock_ok, lock_err = lock_non_target_subtitle_tracks(track_index, timeline)
    local locked_snapshot = select(1, get_subtitle_track_state_snapshot(timeline))
    local locked_summary = format_subtitle_track_state_snapshot(locked_snapshot)
    if not lock_ok then
        return false, "目标轨未独占启用，且锁轨兜底失败: " .. tostring(lock_err) .. "；当前状态: " .. locked_summary, false
    end

    return true, "目标轨已启用，并进入锁轨兜底；当前状态: " .. locked_summary, true
end

local function clear_subtitle_track_clips(track_index, timeline_override)
    local timeline = timeline_override
    if not timeline then
        local resolve = get_resolve()
        if not resolve then return false, 0, 0, "无法获取 Resolve" end

        local pm = resolve:GetProjectManager()
        if not pm then return false, 0, 0, "无法获取 ProjectManager" end

        local project = pm:GetCurrentProject()
        if not project then return false, 0, 0, "没有打开的项目" end

        timeline = project:GetCurrentTimeline()
        if not timeline then return false, 0, 0, "没有时间线" end
    end

    local items, err = get_subtitle_track_items(track_index, timeline)
    if not items then
        return false, 0, 0, err
    end

    local initial_count = #items
    if initial_count == 0 then
        return true, 0, 0
    end

    local delete_ok = false
    local attempts = {
        function() return timeline:DeleteClips(items, false) end,
        function() return timeline:DeleteClips(items) end,
    }

    for _, delete_fn in ipairs(attempts) do
        local ok, ret = pcall(delete_fn)
        if ok and ret ~= false then
            delete_ok = true
            break
        end
    end

    if not delete_ok then
        for _, item in ipairs(items) do
            local removed = false

            local ok_one, ret_one = pcall(function() return timeline:DeleteClips({item}, false) end)
            if ok_one and ret_one ~= false then
                removed = true
            else
                local ok_fallback, ret_fallback = pcall(function() return timeline:DeleteClips({item}) end)
                if ok_fallback and ret_fallback ~= false then
                    removed = true
                end
            end
        end
    end

    local remaining_items = select(1, get_subtitle_track_items(track_index, timeline)) or {}
    local remaining_count = #remaining_items
    local deleted_count = math.max(0, initial_count - remaining_count)

    if remaining_count > 0 then
        return false, deleted_count, remaining_count, "轨道 " .. tostring(track_index) .. " 仍残留 " .. tostring(remaining_count) .. " 条字幕"
    end

    return true, deleted_count, 0
end

local function snapshot_subtitle_tracks(timeline_override)
    local timeline = timeline_override
    if not timeline then
        local resolve = get_resolve()
        if not resolve then return nil, "无法获取 Resolve" end

        local pm = resolve:GetProjectManager()
        if not pm then return nil, "无法获取 ProjectManager" end

        local project = pm:GetCurrentProject()
        if not project then return nil, "没有打开的项目" end

        timeline = project:GetCurrentTimeline()
        if not timeline then return nil, "没有时间线" end
    end

    local _, track_count = get_subtitle_track_type_and_count(timeline)
    local snapshot = {
        track_count = track_count,
        tracks = {}
    }

    for i = 1, track_count do
        local items = select(1, get_subtitle_track_items(i, timeline)) or {}
        snapshot.tracks[i] = #items
    end

    return snapshot
end

local function detect_subtitle_track_delta(before_snapshot, after_snapshot)
    local result = {
        target_track = current_subtitle_target_track,
        target_track_delta = 0,
        total_added = 0,
        detected_track = nil,
        added_tracks = {},
        track_deltas = {}
    }

    local max_tracks = math.max(
        before_snapshot and before_snapshot.track_count or 0,
        after_snapshot and after_snapshot.track_count or 0
    )

    for i = 1, max_tracks do
        local before_count = (before_snapshot and before_snapshot.tracks and before_snapshot.tracks[i]) or 0
        local after_count = (after_snapshot and after_snapshot.tracks and after_snapshot.tracks[i]) or 0
        local delta = after_count - before_count

        result.track_deltas[i] = delta
        if delta > 0 then
            table.insert(result.added_tracks, {track_index = i, count = delta})
            result.total_added = result.total_added + delta
        end
    end

    result.target_track_delta = result.track_deltas[result.target_track] or 0

    if #result.added_tracks == 1 then
        result.detected_track = result.added_tracks[1].track_index
    elseif result.target_track_delta > 0 and result.target_track_delta == result.total_added then
        result.detected_track = result.target_track
    elseif #result.added_tracks > 1 then
        table.sort(result.added_tracks, function(a, b)
            if a.count ~= b.count then
                return a.count > b.count
            end
            return a.track_index < b.track_index
        end)
        result.detected_track = result.added_tracks[1].track_index
    end

    result.reused_target_track = result.detected_track == result.target_track and result.target_track_delta > 0

    return result
end

local function get_subtitle_track_label_candidates(track_index, timeline)
    local labels = {}
    local seen = {}

    local function add_label(label)
        local value = trim(label or "")
        if value == "" then return end
        if not seen[value] then
            seen[value] = true
            table.insert(labels, value)
        end
    end

    add_label("ST" .. tostring(track_index))
    add_label("字幕" .. tostring(track_index))
    add_label("字幕 " .. tostring(track_index))
    add_label("Subtitle " .. tostring(track_index))
    add_label("Subtitle" .. tostring(track_index))

    if timeline then
        local ok_name, track_name = pcall(function() return timeline:GetTrackName("subtitle", track_index) end)
        if ok_name and track_name then
            local resolved_name = trim(track_name)
            add_label(resolved_name)
            add_label(resolved_name:gsub("%s+", ""))
        end
    end

    return labels
end

local function activate_subtitle_target_track_via_ui(track_index, timeline)
    if not track_index or track_index < 1 then
        return false, "字幕目标轨无效"
    end

    if package.config:sub(1, 1) ~= "/" then
        return false, "仅 macOS 支持字幕轨 UI 自动切换"
    end

    local label_candidates = get_subtitle_track_label_candidates(track_index, timeline)
    local candidates_literal = table.concat(label_candidates, "||")
    local script_path = (os.getenv("TMPDIR") or "/tmp/") .. "hooper_set_subtitle_target_track.js"
    local script_file = io.open(script_path, "w")
    if not script_file then
        return false, "无法创建 UI 自动化脚本"
    end

    local jxa_script = [[
ObjC.import('Cocoa');
ObjC.import('ApplicationServices');

function safeCall(fn, fallback) {
    try { return fn(); } catch (e) { return fallback; }
}

function asString(value) {
    return value === undefined || value === null ? '' : String(value);
}

function rectForElement(el) {
    var pos = safeCall(function () { return el.position(); }, null);
    var size = safeCall(function () { return el.size(); }, null);
    if (!pos || !size) return null;
    return {
        x: Number(pos[0]),
        y: Number(pos[1]),
        w: Number(size[0]),
        h: Number(size[1])
    };
}

function centerY(rect) {
    return rect.y + rect.h / 2;
}

function clickAt(x, y) {
    function post(type) {
        var event = $.CGEventCreateMouseEvent($(), type, $.CGPointMake(x, y), $.kCGMouseButtonLeft);
        $.CGEventPost($.kCGHIDEventTap, event);
    }

    post($.kCGEventMouseMoved);
    delay(0.03);
    post($.kCGEventLeftMouseDown);
    delay(0.03);
    post($.kCGEventLeftMouseUp);
    delay(0.15);
}

function run(argv) {
    var targetCandidates = String(argv[0] || '').split('||').filter(function (item) { return item.length > 0; });
    function normalized(text) {
        return asString(text).replace(/\s+/g, '').toLowerCase();
    }

    var se = Application('System Events');
    se.includeStandardAdditions = true;
    var proc = se.processes.byName('DaVinci Resolve');
    if (!proc.exists()) {
        throw new Error('找不到 DaVinci Resolve 进程');
    }

    proc.frontmost = true;
    delay(0.10);

    var windows = proc.windows();
    if (!windows || windows.length === 0) {
        throw new Error('找不到 DaVinci Resolve 窗口');
    }

    var win = windows[0];
    var maxArea = 0;
    for (var w = 0; w < windows.length; w++) {
        var candidateRect = rectForElement(windows[w]);
        if (candidateRect) {
            var area = candidateRect.w * candidateRect.h;
            if (area > maxArea) {
                maxArea = area;
                win = windows[w];
            }
        }
    }

    var elements = win.entireContents();
    var labelRect = null;

    for (var i = 0; i < elements.length; i++) {
        var el = elements[i];
        var role = asString(safeCall(function () { return el.role(); }, ''));
        if (role !== 'AXStaticText' && role !== 'AXTextField' && role !== 'AXButton') {
            continue;
        }

        var candidates = [
            asString(safeCall(function () { return el.name(); }, '')),
            asString(safeCall(function () { return el.value(); }, '')),
            asString(safeCall(function () { return el.description(); }, ''))
        ];

        var matched = false;
        for (var t = 0; t < targetCandidates.length; t++) {
            var targetNorm = normalized(targetCandidates[t]);
            for (var c = 0; c < candidates.length; c++) {
                if (normalized(candidates[c]) === targetNorm) {
                    matched = true;
                    break;
                }
            }
            if (matched) {
                break;
            }
        }

        if (matched) {
            var rect = rectForElement(el);
            if (rect && rect.w > 0 && rect.h > 0) {
                if (!labelRect || rect.x < labelRect.x) {
                    labelRect = rect;
                }
            }
        }
    }

    if (!labelRect) {
        throw new Error('找不到字幕轨标签 ' + targetCandidates.join(', '));
    }

    var buttonRects = [];
    for (var j = 0; j < elements.length; j++) {
        var el2 = elements[j];
        var role2 = asString(safeCall(function () { return el2.role(); }, ''));
        if (role2 !== 'AXButton' && role2 !== 'AXCheckBox' && role2 !== 'AXRadioButton') {
            continue;
        }

        var rect2 = rectForElement(el2);
        if (!rect2 || rect2.w <= 0 || rect2.h <= 0) {
            continue;
        }

        var sameRow = Math.abs(centerY(rect2) - centerY(labelRect)) <= Math.max(14, labelRect.h * 1.3);
        var nearHeader = rect2.x >= (labelRect.x - 10) && rect2.x <= (labelRect.x + 180);
        if (sameRow && nearHeader) {
            buttonRects.push(rect2);
        }
    }

    buttonRects.sort(function (a, b) { return a.x - b.x; });

    var targetRect = buttonRects.length > 0 ? buttonRects[buttonRects.length - 1] : null;
    var clickX = targetRect ? (targetRect.x + targetRect.w / 2) : (labelRect.x + labelRect.w + 52);
    var clickY = targetRect ? (targetRect.y + targetRect.h / 2) : centerY(labelRect);

    clickAt(clickX, clickY);
    return 'OK ' + targetCandidates.join('|') + ' ' + Math.round(clickX) + ',' + Math.round(clickY) + ' buttons=' + buttonRects.length;
}
]]

    script_file:write(jxa_script)
    script_file:close()

    local cmd = "osascript -l JavaScript " .. shell_quote(script_path) .. " " .. shell_quote(candidates_literal)
    local ok, output = run_shell_capture(cmd)
    pcall(function() os.remove(script_path) end)

    output = trim(output or "")
    if ok and output:match("^OK%s") then
        return true, output
    end

    if output:find("not allowed assistive access", 1, true) or output:find("辅助访问", 1, true) or output:find("辅助功能", 1, true) or output:find("-1719", 1, true) then
        return false, "macOS 未授予辅助功能权限，请先允许 Resolve 或脚本宿主控制界面"
    end

    return false, output ~= "" and output or ("切换字幕目标轨失败，尝试标签: " .. candidates_literal)
end

local function sort_rows_by_timing(rows)
    table.sort(rows, function(a, b)
        local a_start = tonumber(a and a.start_frame) or math.huge
        local b_start = tonumber(b and b.start_frame) or math.huge
        if a_start ~= b_start then
            return a_start < b_start
        end

        local a_end = tonumber(a and a.end_frame) or math.huge
        local b_end = tonumber(b and b.end_frame) or math.huge
        if a_end ~= b_end then
            return a_end < b_end
        end

        local a_index = tonumber(a and a.index) or math.huge
        local b_index = tonumber(b and b.index) or math.huge
        return a_index < b_index
    end)
end

-- ========== 刷新字幕列表 ==========
local function refresh_subtitles(target_window, options)
    local window = resolve_window(target_window)
    options = options or {}
    print("[Hooper AI 2.0] 开始刷新字幕列表...")
    local refresh_total_started_at = os.clock()

    local function fail_refresh(message)
        invalidate_search_cache("refresh_failed")
        if message and message ~= "" then
            update_shared_status(window, message)
        end
        current_rows = {}
        current_selected_row_id = nil
        undo_stack = {}
        redo_stack = {}
        update_undo_redo_button_states()
        set_subtitle_loaded_state(false, nil, window)
        clear_tree_for_window(window)
        set_current_preview_source(PREVIEW_SOURCE_TIMELINE)
        reset_backup_selector_to_placeholder()
        if options.show_loading_placeholder then
            set_mini_subtitle_area_state(window, false, message or "字幕加载失败，请稍后重试")
        end
        return false
    end

    update_search_query_from_window(window)
    
    local resolve = get_resolve()
    if not resolve then
        print("[Hooper AI 2.0] 无法获取 Resolve")
        return fail_refresh("无法获取 Resolve")
    end
    
    local pm = resolve:GetProjectManager()
    if not pm then
        print("[Hooper AI 2.0] 无法获取 ProjectManager")
        return fail_refresh("无法获取 ProjectManager")
    end
    
    local project = pm:GetCurrentProject()
    if not project then
        print("[Hooper AI 2.0] 没有打开的项目")
        return fail_refresh("没有打开的项目")
    end
    
    local timeline = project:GetCurrentTimeline()
    if not timeline then
        print("[Hooper AI 2.0] 没有时间线")
        return fail_refresh("没有时间线")
    end
    
    -- 获取帧率
    local timeline_fps_str = timeline:GetSetting("timelineFrameRate") or "24"
    current_fps = parse_fps(timeline_fps_str)
    print("[Hooper AI 2.0] 时间线帧率: " .. current_fps)
    
    -- 获取时间线起始帧（注意：字幕 item 的 GetStart/GetEnd 在部分版本中已是绝对帧）
    local tl_start_frame = timeline:GetStartFrame() or 0
    current_tl_start_frame = tl_start_frame
    print("[Hooper AI 2.0] 时间线起始帧: " .. tl_start_frame)
    
    -- 获取轨道上的字幕 (兼容 DaVinci Resolve 20)
    local track_type, track_count = get_subtitle_track_type_and_count(timeline)
    print("[Hooper AI 2.0] 字幕轨道数: " .. tostring(track_count) .. " (type: " .. tostring(track_type) .. ")")
    
    if track_count == 0 then
        print("[Hooper AI 2.0] 没有字幕轨道")
        return fail_refresh("没有字幕轨道")
    end
    
    if current_track > track_count then
        print("[Hooper AI 2.0] 轨道 " .. tostring(current_track) .. " 不存在")
        return fail_refresh("轨道 " .. tostring(current_track) .. " 不存在")
    end

    local fetch_started_at = os.clock()
    local items, items_err = get_subtitle_track_items(current_track, timeline)
    if not items then
        print("[Hooper AI 2.0] " .. tostring(items_err))
        return fail_refresh(items_err or ("无法读取轨道 " .. tostring(current_track)))
    end
    if not items or #items == 0 then
        print("[Hooper AI 2.0] 轨道 " .. tostring(current_track) .. " 上没有字幕")
        return fail_refresh("轨道 " .. tostring(current_track) .. " 上没有字幕")
    end
    
    print("[Hooper AI 2.0] 找到 " .. #items .. " 条字幕")
    print(string.format("[Hooper AI 2.0] 字幕轨读取耗时: %d ms", math.floor(((os.clock() - fetch_started_at) * 1000) + 0.5)))

    local rows = {}
    local normalize_started_at = os.clock()
    
    -- 遍历字幕
    for i, item in ipairs(items) do
        local name = item:GetName() or ""
        local start_frame = item:GetStart() or 0
        local end_frame = item:GetEnd() or 0
        
        -- 计算绝对帧（中心帧）
        -- 这里的 start_frame/end_frame 视为"绝对帧"，不要再叠加 tl_start_frame
        local target_abs_frame = math.floor(((tonumber(start_frame) or 0) + (tonumber(end_frame) or 0)) / 2)
        
        -- 计算时间码字符串
        local tc_start = frames_to_timecode(start_frame, current_fps)
        local tc_end = frames_to_timecode(end_frame, current_fps)
        
        -- 存储数据
        local row_data = {
            id = build_row_id(current_track, i, start_frame, end_frame),
            index = i,
            target_abs_frame = target_abs_frame,
            fps = current_fps,
            start_frame = start_frame,
            end_frame = end_frame,
            text = name,
            timecode = tc_start .. " --> " .. tc_end  -- 存储原始时间码字符串
        }
        row_data.display_text = build_tree_display_text(i, tostring(tc_start), tostring(tc_end), name)
        table.insert(rows, row_data)
    end

    sort_rows_by_timing(rows)
    print(string.format("[Hooper AI 2.0] 字幕归一化与排序耗时: %d ms", math.floor(((os.clock() - normalize_started_at) * 1000) + 0.5)))

    local render_started_at = os.clock()
    rebuild_tree_from_rows(rows, window, {skip_sort = true})
    print(string.format("[Hooper AI 2.0] 字幕树渲染耗时: %d ms", math.floor(((os.clock() - render_started_at) * 1000) + 0.5)))

    -- 不在这里预渲染另一个窗口（曾经做过双窗口预填充以避免切换卡顿，
    -- 但实测会在初次刷新时多花一倍渲染时间，让用户感觉"刷新很慢"）。
    -- rebuild_tree_from_rows 已经把 full_window_tree_dirty 置为 true，
    -- 真正切换到完整窗口时会按需渲染（见 toggle/switch 时的 dirty 检查）。
    if is_mini_window(window) and win then
        full_window_tree_dirty = true
    end

    undo_stack = {}
    redo_stack = {}
    set_current_preview_source(PREVIEW_SOURCE_TIMELINE)
    reset_backup_selector_to_placeholder()
    update_undo_redo_button_states()
    if is_mini_window(window) then
        set_mini_subtitle_area_state(window, true)
    end

    -- 完整版预热改走异步路径（见 full_window_warmup_timer，间隔 50 ms）。
    -- 之前在这里同步预热是为了让「已加载」=完全就绪，但实测会让用户感知的
    -- 「正在自动加载」状态多 70 ms（同步 render_rows_to_window 阻塞 RunLoop）。
    -- 现在的折中：「已加载」尽早出现 → mini 立刻可交互 → 50 ms 后异步铺完整版。
    -- 50 ms 内用户若已点切换，open_full_window 的 dirty 检查会兜底同步渲染。
    -- rebuild_tree_from_rows 已经调过 restart_ui_timer(full_window_warmup_timer)，
    -- 这里不重复 schedule。

    set_subtitle_loaded_state(true, nil, window)

    print(string.format("[Hooper AI 2.0] 刷新完成 (总耗时: %d ms, 距脚本启动: +%d ms)",
        math.floor(((os.clock() - refresh_total_started_at) * 1000) + 0.5),
        startup_elapsed_ms()))

    return true
end

-- ========== 全局刷新函数 (供弹窗事件调用) ==========
function RefreshSubtitleTree(target_window)
    local window = resolve_window(target_window)
    SEARCH_VIEW.render_current_view(window)
end

local function update_log_window_view()
    if not workflow_log_window then return end
    local ok_items, items = pcall(function() return workflow_log_window:GetItems() end)
    if ok_items and items and items.WorkflowLogView then
        items.WorkflowLogView.Text = workflow_log_buffer
    end
end

local function show_log_window()
    if workflow_log_window then
        workflow_log_window:Show()
        update_log_window_view()
        return
    end

    workflow_log_window = dispatcher:AddWindow({
        ID = "WorkflowLogWindow",
        WindowTitle = "Hooper AI 2.0 · 运行日志",
        Geometry = {260, 180, 700, 460},
    },
    ui:VGroup{
        Spacing = 8,
        ContentsMargins = 10,
        ui:TextEdit{
            ID = "WorkflowLogView",
            ReadOnly = true,
            Weight = 1,
            Text = workflow_log_buffer,
            PlaceholderText = "这里会显示完整运行日志。"
        },
        ui:HGroup{
            Weight = 0,
            Spacing = 8,
            ui:HGap(0, 1),
            ui:Button{ID = "CopyWorkflowLogBtn", Text = "复制日志", Weight = 0, MinimumSize = {96, 28}},
            ui:Button{ID = "CloseWorkflowLogBtn", Text = "关闭", Weight = 0, MinimumSize = {96, 28}}
        }
    })

    function workflow_log_window.On.WorkflowLogWindow.Close(ev)
        workflow_log_window:Hide()
    end

    function workflow_log_window.On.CopyWorkflowLogBtn.Clicked(ev)
        pcall(function() bmd.setclipboard(workflow_log_buffer or "") end)
        local status = win and win:Find("StatusLabel")
        if status then status:Set("Text", "日志已复制到剪贴板") end
    end

    function workflow_log_window.On.CloseWorkflowLogBtn.Clicked(ev)
        workflow_log_window:Hide()
    end

    workflow_log_window:Show()
    update_log_window_view()
end

local function extract_timecode_bounds(value)
    if type(value) ~= "string" then return nil, nil end
    return value:match("(%d+:%d+:%d+[:;,]%d+).-(%d+:%d+:%d+[:;,]%d+)")
end

local function timecode_to_srt_timestamp(tc, fps)
    if type(tc) ~= "string" or tc == "" then return nil end
    fps = tonumber(fps) or current_fps or 24.0

    local h, m, s, ms = tc:match("^(%d+):(%d+):(%d+),(%d+)$")
    if h then
        return string.format("%02d:%02d:%02d,%03d", tonumber(h), tonumber(m), tonumber(s), tonumber(ms))
    end

    local hh, mm, ss, ff = tc:match("^(%d+):(%d+):(%d+)[:;](%d+)$")
    if hh then
        local ms_num = math.floor(((tonumber(ff) or 0) / fps) * 1000 + 0.5)
        return string.format("%02d:%02d:%02d,%03d", tonumber(hh), tonumber(mm), tonumber(ss), ms_num)
    end

    return tc
end

local function get_row_text(row)
    if type(row) ~= "table" then return "" end
    local text = row.text or row.Text or row.content or row[3] or ""
    if type(text) ~= "string" then
        text = tostring(text or "")
    end

    local _, _, embedded_text = text:match("(%d+:%d+:%d+[:;,]%d+).-(%d+:%d+:%d+[:;,]%d+).-|%s*(.*)")
    if embedded_text and embedded_text ~= "" then
        return embedded_text
    end

    return text
end

get_row_timecodes = function(row)
    if type(row) ~= "table" then return nil, nil end

    local fps = tonumber(row.fps) or current_fps
    local start_frame = tonumber(row.start_frame)
    local end_frame = tonumber(row.end_frame)
    if start_frame and end_frame then
        return frames_to_timecode(start_frame, fps), frames_to_timecode(end_frame, fps)
    end

    local start_tc = row.Start or row.start or row.StartTC or row[1]
    local end_tc = row.End or row["end"] or row.EndTC or row.end_tc or row[2]
    if type(start_tc) == "string" and type(end_tc) == "string" then
        return start_tc, end_tc
    end

    local tc_start, tc_end = extract_timecode_bounds(row.timecode or row.Timecode or "")
    if tc_start and tc_end then
        return tc_start, tc_end
    end

    local text_start, text_end = extract_timecode_bounds(get_row_text(row))
    if text_start and text_end then
        return text_start, text_end
    end

    return nil, nil
end

rebuild_tree_from_rows = function(rows, target_window, options)
    options = options or {}
    if options.skip_sort ~= true and type(rows) == "table" then
        sort_rows_by_timing(rows)
    end

    current_rows = {}

    for i, row in ipairs(rows or {}) do
        if type(row) == "table" then
            row.index = i
            row.fps = tonumber(row.fps) or current_fps
            row.text = get_row_text(row)
            row.id = build_row_id(current_track, i, row.start_frame, row.end_frame)

            local tc_start, tc_end = get_row_timecodes(row)
            if tc_start and tc_end then
                row.timecode = tc_start .. " --> " .. tc_end
                row.display_text = build_tree_display_text(i, tc_start, tc_end, row.text)
            else
                row.timecode = row.timecode or ""
                row.display_text = build_tree_display_text(i, row.timecode, nil, row.text)
            end

            get_row_search_text_lower(row)
            current_rows[i] = row
        end
    end

    if current_selected_row_id and not find_row_by_id(current_selected_row_id) then
        current_selected_row_id = nil
    end

    invalidate_search_cache("rebuild_tree")
    SEARCH_VIEW.render_current_view(target_window or resolve_window())
    -- 标记另一个窗口的字幕树需要刷新（延迟到切换时再渲染，避免每次操作都双重渲染）
    local current_window = target_window or resolve_window()
    if current_window and is_mini_window(current_window) and win then
        full_window_tree_dirty = true
        -- 顺便给完整版字幕树排一次空闲预热，下次切换零等待
        if full_window_warmup_timer then
            restart_ui_timer(full_window_warmup_timer)
        end
    end
end

local function collect_exportable_subtitles()
    local rows = {}
    local seen = {}

    local function append_entry(entry)
        if type(entry) ~= "table" and type(entry) ~= "string" then
            return
        end
        if type(entry) == "table" then
            if seen[entry] then return end
            seen[entry] = true
        end
        table.insert(rows, entry)
    end

    -- 核心修复：优先使用 current_rows（全量数据），避免搜索过滤导致数据丢失
    if current_rows and #current_rows > 0 then
        for _, entry in ipairs(current_rows) do
            append_entry(entry)
        end
    end

    -- 兜底：如果 current_rows 为空，再尝试 subtitle_data_map
    if #rows == 0 and next(subtitle_data_map) then
        for _, entry in pairs(subtitle_data_map) do
            append_entry(entry)
        end
    end

    -- 最后的兜底：使用全局 subtitle_data
    if #rows == 0 and type(subtitle_data) == "table" then
        for _, entry in pairs(subtitle_data) do
            append_entry(entry)
        end
    end

    -- 核心修复：按 start_frame 排序，确保导出的 SRT 时序正确
    table.sort(rows, function(a, b)
        return (tonumber(a.start_frame) or 0) < (tonumber(b.start_frame) or 0)
    end)

    return rows
end

local function normalize_export_subtitle(entry, fallback_index)
    local row_type = type(entry)
    local fps = current_fps
    local start_tc, end_tc, text
    local sort_frame = nil
    local row_index = fallback_index or 0

    if row_type == "string" then
        start_tc, end_tc, text = entry:match("(%d+:%d+:%d+[:;,]%d+).-(%d+:%d+:%d+[:;,]%d+).-|%s*(.*)")
        text = text or entry
    elseif row_type == "table" then
        fps = tonumber(entry.fps) or current_fps
        row_index = tonumber(entry.index) or row_index
        text = get_row_text(entry)

        local start_frame = tonumber(entry.start_frame)
        local end_frame = tonumber(entry.end_frame)
        if start_frame and end_frame then
            sort_frame = start_frame
            start_tc = frames_to_srt_time(start_frame, fps)
            end_tc = frames_to_srt_time(end_frame, fps)
        else
            local raw_start, raw_end = get_row_timecodes(entry)
            start_tc = timecode_to_srt_timestamp(raw_start, fps)
            end_tc = timecode_to_srt_timestamp(raw_end, fps)
        end

        if (not start_tc or not end_tc) and type(text) == "string" then
            local embedded_start, embedded_end, embedded_text = text:match("(%d+:%d+:%d+[:;,]%d+).-(%d+:%d+:%d+[:;,]%d+).-|%s*(.*)")
            if embedded_start and embedded_end then
                start_tc = start_tc or timecode_to_srt_timestamp(embedded_start, fps)
                end_tc = end_tc or timecode_to_srt_timestamp(embedded_end, fps)
                text = embedded_text or text
            end
        end
    else
        return nil
    end

    text = trim(text or "")
    if text == "" then
        return nil
    end

    if not start_tc or not end_tc then
        return nil
    end

    return {
        index = row_index,
        sort_frame = sort_frame,
        Start = start_tc,
        End = end_tc,
        Text = text
    }
end

-- ========== 日志输出辅助函数 ==========
LogMsg = function(msg)
    local time_str = os.date("%H:%M:%S")
    local line = "[" .. time_str .. "] " .. msg
    if workflow_log_buffer == "" then
        workflow_log_buffer = line
    else
        workflow_log_buffer = line .. "\n" .. workflow_log_buffer
    end
    update_log_window_view()
end

local function format_subtitle_track_delta_summary(delta_result)
    if not delta_result or not delta_result.added_tracks or #delta_result.added_tracks == 0 then
        return "未检测到新增字幕"
    end

    local parts = {}
    for _, info in ipairs(delta_result.added_tracks) do
        table.insert(parts, "轨道 " .. tostring(info.track_index) .. " +" .. tostring(info.count))
    end
    return table.concat(parts, "，")
end

-- ========== 搜索过滤 ==========
local function do_search(target_window)
    local window = resolve_window(target_window)
    update_search_query_from_window(window)
    SEARCH_VIEW.render_current_view(window)
end

-- ========== 定位跳转 ==========
local function go_to_subtitle(target_window)
    local window = resolve_window(target_window)
    print("[Hooper AI 2.0] 定位跳转触发")

    if not find_window_item(window, "SubtitleTree", "MiniSubtitleTree") then
        print("[Hooper AI 2.0] 无法获取 Tree")
        return
    end

    local data = select(1, get_row_from_tree_selection(window))
    if not data then
        print("[Hooper AI 2.0] 没有选中项")
        return
    end
    
    print(string.format("[Hooper AI 2.0] 跳转: start_frame=%s, fps=%.3f",
        tostring(data.start_frame), data.fps or current_fps))
    
    local resolve = get_resolve()
    if not resolve then return end
    
    local pm = resolve:GetProjectManager()
    local project = pm:GetCurrentProject()
    if not project then
        print("[Hooper AI 2.0] 没有项目")
        return
    end
    
    local timeline = project:GetCurrentTimeline()
    if not timeline then
        print("[Hooper AI 2.0] 没有时间线")
        return
    end
    
    local abs_start = (tonumber(data.start_frame) or 0)
    local tc = frames_to_timecode(abs_start, data.fps or current_fps)
    print("[Hooper AI 2.0] 转换时间码: " .. tc)
    
    local ok, err = timeline:SetCurrentTimecode(tc)
    if ok then
        print("[Hooper AI 2.0] 跳转成功: " .. tc)
        update_shared_status(window, "已跳转到: " .. tc)
    else
        print("[Hooper AI 2.0] 跳转失败: " .. tostring(err))
    end
end

-- ========== 批量替换 ==========
local function do_replace()
    print("[Hooper AI 2.0] 批量替换按钮点击")

    local find_input = win:Find("FindInput")
    local replace_input = win:Find("ReplaceInput")
    if not find_input or not replace_input then
        print("[Hooper AI 2.0] 无法获取输入框")
        return
    end

    local find_text = trim(find_input.Text or "")
    local replace_text = trim(replace_input.Text or "")

    local has_current_rows = type(current_rows) == "table" and #current_rows > 0
    local has_subtitle_map = type(subtitle_data_map) == "table" and next(subtitle_data_map) ~= nil
    if not has_current_rows and not has_subtitle_map then
        print("[Hooper AI 2.0] 没有字幕数据")
        local status = win:Find("StatusLabel")
        if status then status:Set("Text", "没有字幕数据") end
        return
    end

    local escaped_find = find_text:gsub("([%%%^%$%(%)%%.%[%]%*%+%-%?])", "%%%1")

    if find_text == "" then
        print("[Hooper AI 2.0] 请输入要查找的文字")
        local status = win:Find("StatusLabel")
        if status then status:Set("Text", "请输入要查找的文字") end
        return
    end

    local tree = win:Find("SubtitleTree")
    if not tree then return end

    local action_label = '替换"' .. find_text .. '"为"' .. replace_text .. '"'
    local mutation_snapshot = prepare_mutation_snapshot(action_label)
    local count = 0
    local dirty_row_ids = {}
    local report_entries = {}
    if has_current_rows then
        for i, data in ipairs(current_rows) do
            if data and data.text then
                local old_text = tostring(data.text or "")
                local new_text = string.gsub(old_text, escaped_find, replace_text)
                if new_text ~= old_text then
                    report_entries[#report_entries + 1] = report_helpers.format_batch_change_report_line(
                        data.index or i,
                        old_text,
                        new_text,
                        {row_id = data.id}
                    )
                    data.text = new_text
                    count = count + 1

                    local start_frame = data.start_frame
                    local end_frame = data.end_frame
                    local tc_start = frames_to_timecode(start_frame, current_fps)
                    local tc_end = frames_to_timecode(end_frame, current_fps)
                    data.display_text = build_tree_display_text(data.index or i, tostring(tc_start), tostring(tc_end), new_text)
                    mark_dirty_row(dirty_row_ids, data)
                end
            end
        end
        if count > 0 then
            sync_current_preview_tree(win, dirty_row_ids)
        end
    else
        local update_entries = {}
        for node, data in pairs(subtitle_data_map) do
            if data and data.text then
                local old_text = tostring(data.text or "")
                local new_text = string.gsub(old_text, escaped_find, replace_text)
                if new_text ~= old_text then
                    report_entries[#report_entries + 1] = report_helpers.format_batch_change_report_line(
                        data.index,
                        old_text,
                        new_text,
                        {row_id = data.id}
                    )
                    data.text = new_text
                    count = count + 1

                    local start_frame = data.start_frame
                    local end_frame = data.end_frame
                    local tc_start = frames_to_timecode(start_frame, current_fps)
                    local tc_end = frames_to_timecode(end_frame, current_fps)
                    local display_text = build_tree_display_text(data.index, tostring(tc_start), tostring(tc_end), new_text)
                    data.display_text = display_text
                    queue_tree_node_text_update(update_entries, node, display_text)
                end
            end
        end
        apply_tree_node_text_updates(win, tree, update_entries)
    end

    if count > 0 then
        commit_mutation_snapshot(mutation_snapshot)
    end

    print("[Hooper AI 2.0] 批量替换完成，修改了 " .. count .. " 条")
    local status = win:Find("StatusLabel")
    if status then status:Set("Text", "已替换 " .. count .. " 条") end
    report_helpers.show_batch_result_report(action_label, report_entries, count)
end

-- ========== 更新时间线 ==========
local function update_timeline()
    print("[Hooper AI 2.0] 更新时间线按钮点击")
    LogMsg("开始更新时间线，目标字幕轨 " .. tostring(current_subtitle_target_track))
    
    local resolve = get_resolve()
    if not resolve then return end
    
    local pm = resolve:GetProjectManager()
    local project = pm:GetCurrentProject()
    if not project then
        print("[Hooper AI 2.0] 没有项目")
        return
    end
    
    local mediaPool = project:GetMediaPool()
    local timeline = project:GetCurrentTimeline()
    if not timeline then
        print("[Hooper AI 2.0] 没有时间线")
        return
    end
    
    -- 修复：优先使用 current_rows 检查数据存在性
    if not current_rows or #current_rows == 0 then
        print("[Hooper AI 2.0] 没有字幕数据")
        return
    end

    if current_track ~= current_subtitle_target_track then
        local mismatch_msg = string.format("当前加载轨道 %d，更新时间线目标轨为 %d", current_track, current_subtitle_target_track)
        print("[Hooper AI 2.0] " .. mismatch_msg)
        LogMsg(mismatch_msg)
    end
    
    -- 生成绝对唯一的 SRT 文件名（打破 DaVinci 缓存）- 使用 os.time()
    local unique_id = os.time() .. "_" .. math.floor(os.clock() * 1000)
    local srt_filename = "Timeline_Update_" .. unique_id .. ".srt"
    local srt_path = current_backup_path .. "/" .. srt_filename

    local status = win:Find("StatusLabel")
    if status then status:Set("Text", "准备更新字幕轨 " .. tostring(current_subtitle_target_track)) end
    LogMsg("准备更新字幕轨 " .. tostring(current_subtitle_target_track))

    local approved_pending_count, pending_dirty_row_ids = apply_approved_pending_changes_to_rows(current_rows)
    if approved_pending_count > 0 then
        print("[Hooper AI 2.0] 已合并 " .. approved_pending_count .. " 条人工批准建议到当前字幕")
        LogMsg("已合并 " .. approved_pending_count .. " 条人工批准建议到当前字幕")
        sync_current_preview_tree(active_window, pending_dirty_row_ids)
    end
    
    local file = io.open(srt_path, "w")
    if not file then
        print("[Hooper AI 2.0] 无法创建 SRT 文件")
        LogMsg("无法创建 SRT 文件: " .. tostring(srt_path))
        if status then status:Set("Text", "无法创建更新用 SRT") end
        return
    end

    -- 获取精确的时间线起始帧
    local tl_start_frame = current_tl_start_frame or 0
    if timeline then
        tl_start_frame = timeline:GetStartFrame() or tl_start_frame
    end

    -- 获取时间线帧率
    local fps = 29.97
    if timeline then
        fps = tonumber(timeline:GetSetting("timelineFrameRate")) or 29.97
    end

    -- 提取并强制按起始帧排序
    local export_list = {}
    if current_rows and #current_rows > 0 then
        for _, row in ipairs(current_rows) do
            if type(row) == "table" and row.text then
                table.insert(export_list, row)
            end
        end
    end
    sort_rows_by_timing(export_list)

    local index = 1
    for i, data in ipairs(export_list) do
        local next_data = export_list[i + 1]
        local start_f = tonumber(data.start_frame) or 0
        local end_f = tonumber(data.end_frame) or 0
        
        -- 核心修复：纯物理帧数相减，彻底杜绝字符串时间码跨小时解析导致的负数和倒挂
        local rel_start = math.max(0, start_f - tl_start_frame)
        local rel_end = math.max(0, end_f - tl_start_frame)
        
        -- 兜底防错：如果时间极短或倒挂，强制给 1 帧长度，防止达芬奇吞字幕
        if rel_end <= rel_start then
            rel_end = rel_start + 1
        end

        local start_ms = math.floor((rel_start / fps) * 1000 + 0.5)
        local raw_end_ms = math.floor((rel_end / fps) * 1000 + 0.5)
        if raw_end_ms <= start_ms then
            raw_end_ms = start_ms + 1
        end

        local safe_end_ms = raw_end_ms
        if next_data then
            local next_start_f = tonumber(next_data.start_frame)
            if next_start_f then
                local rel_next_start = math.max(0, next_start_f - tl_start_frame)
                local next_start_ms = math.floor((rel_next_start / fps) * 1000 + 0.5)
                local bounded_end_ms = math.min(raw_end_ms, next_start_ms - 1)
                if bounded_end_ms > start_ms then
                    safe_end_ms = bounded_end_ms
                end
            end
        end
        
        local srt_time_line = milliseconds_to_srt_time(start_ms) .. " --> " .. milliseconds_to_srt_time(safe_end_ms)
        
        file:write(index .. "\n")
        file:write(srt_time_line .. "\n")
        file:write(data.text .. "\n")
        file:write("\n")
        
        index = index + 1
    end
    file:close()
    
    if index == 1 then
        print("[Hooper AI 2.0] 没有生成任何字幕")
        LogMsg("没有生成任何字幕，已取消更新时间线")
        if status then status:Set("Text", "没有可导入的字幕") end
        return
    end
    
    print("[Hooper AI 2.0] 已生成 SRT: " .. srt_path .. "，共 " .. (index - 1) .. " 条")
    LogMsg("已生成新的 SRT 临时文件，共 " .. tostring(index - 1) .. " 条")
    
    -- ========== 导入字幕到时间线（保留目标轨样式，仅清空轨内片段）==========
    local rootFolder = mediaPool:GetRootFolder()
    mediaPool:SetCurrentFolder(rootFolder)

    local srtFileName = srt_path:match("([^/\\]+)$")
    print("[Hooper AI 2.0] 检查媒体池: " .. srtFileName)

    -- 清理媒体池中的旧 SRT 文件
    local existingItems = rootFolder:GetClipList()
    if existingItems then
        for _, item in ipairs(existingItems) do
            if item:GetName() == srtFileName then
                print("[Hooper AI 2.0] 删除旧字幕: " .. srtFileName)
                mediaPool:DeleteClips({item})
                break
            end
        end
    end

    local ensured_items, ensure_err = ensure_subtitle_track_exists(current_subtitle_target_track, timeline)
    if not ensured_items then
        print("[Hooper AI 2.0] " .. tostring(ensure_err))
        LogMsg("确保目标字幕轨存在失败: " .. tostring(ensure_err))
        if status then status:Set("Text", "目标字幕轨准备失败") end
        return
    end

    LogMsg("已确认目标字幕轨存在: 轨道 " .. tostring(current_subtitle_target_track) .. "，当前 " .. tostring(#ensured_items) .. " 条字幕")

    local original_track_state_snapshot = select(1, get_subtitle_track_state_snapshot(timeline))
    if original_track_state_snapshot then
        local before_state_msg = "导入前字幕轨状态: " .. format_subtitle_track_state_snapshot(original_track_state_snapshot)
        print("[Hooper AI 2.0] " .. before_state_msg)
        LogMsg(before_state_msg)
    end

    local unlock_ok, unlock_msg = unlock_all_subtitle_tracks(timeline)
    if not unlock_ok then
        print("[Hooper AI 2.0] 清理字幕轨锁定失败: " .. tostring(unlock_msg))
        LogMsg("清理字幕轨锁定失败: " .. tostring(unlock_msg))
    end

    local isolate_ok, isolate_msg, fallback_locked = isolate_subtitle_target_track(current_subtitle_target_track, timeline)
    if not isolate_ok then
        print("[Hooper AI 2.0] 切换字幕启用轨失败: " .. tostring(isolate_msg))
        LogMsg("切换字幕启用轨失败: " .. tostring(isolate_msg))
        if status then
            status:Set("Text", "无法切换到字幕轨 " .. tostring(current_subtitle_target_track))
        end
        return
    end
    print("[Hooper AI 2.0] 已切换字幕启用轨: " .. tostring(isolate_msg))
    LogMsg("已切换字幕启用轨: " .. tostring(isolate_msg))

    local clear_ok, deleted_count, remaining_count, clear_err = clear_subtitle_track_clips(current_subtitle_target_track, timeline)
    if not clear_ok then
        print("[Hooper AI 2.0] 清空目标字幕轨失败: " .. tostring(clear_err))
        LogMsg("清空目标字幕轨失败: " .. tostring(clear_err))
        if status then
            status:Set("Text", "清空轨道 " .. tostring(current_subtitle_target_track) .. " 失败")
        end
        if fallback_locked then
            unlock_all_subtitle_tracks(timeline)
        end
        return
    end

    local cleared_msg = string.format("已清空轨道 %d 旧字幕 %d 条", current_subtitle_target_track, deleted_count or 0)
    print("[Hooper AI 2.0] " .. cleared_msg)
    LogMsg(cleared_msg)
    if status then status:Set("Text", cleared_msg) end

    local before_snapshot, before_err = snapshot_subtitle_tracks(timeline)
    if not before_snapshot then
        print("[Hooper AI 2.0] 导入前快照失败: " .. tostring(before_err))
        LogMsg("导入前快照失败: " .. tostring(before_err))
        if status then status:Set("Text", "导入前轨道快照失败") end
        if fallback_locked then
            unlock_all_subtitle_tracks(timeline)
        end
        return
    end

    -- 导入 SRT 到媒体池
    local mediaPoolItems = mediaPool:ImportMedia({srt_path})
    if not mediaPoolItems or #mediaPoolItems == 0 then
        print("[Hooper AI 2.0] 导入字幕到媒体池失败")
        LogMsg("导入字幕到媒体池失败")
        if status then status:Set("Text", "导入字幕到媒体池失败") end
        if fallback_locked then
            unlock_all_subtitle_tracks(timeline)
        end
        return
    end
    local mediaPoolItem = mediaPoolItems[1]
    print("[Hooper AI 2.0] 字幕已导入媒体池")
    LogMsg("已导入新字幕到媒体池，共 " .. tostring(index - 1) .. " 条")

    LogMsg("使用稳定模式追加字幕到时间线，Resolve 将自行决定落轨")
    if start_tc then
        timeline:SetCurrentTimecode(start_tc)
    end

    local append_ok, append_result = pcall(function() return mediaPool:AppendToTimeline({mediaPoolItem}) end)
    if not append_ok or append_result == false or append_result == nil then
        print("[Hooper AI 2.0] 插入失败")
        LogMsg("AppendToTimeline 插入失败")
        if status then status:Set("Text", "字幕追加到时间线失败") end
        if fallback_locked then
            unlock_all_subtitle_tracks(timeline)
        end
        return
    end

    local final_after_snapshot, after_err = snapshot_subtitle_tracks(timeline)
    if not final_after_snapshot then
        print("[Hooper AI 2.0] 导入后快照失败: " .. tostring(after_err))
        LogMsg("导入后快照失败: " .. tostring(after_err))
        if status then status:Set("Text", "字幕已导入，但无法验证落轨") end
        if fallback_locked then
            unlock_all_subtitle_tracks(timeline)
        end
        return
    end

    local final_delta = detect_subtitle_track_delta(before_snapshot, final_after_snapshot)

    local delta_summary = format_subtitle_track_delta_summary(final_delta)
    local imported_msg = string.format("已导入新字幕 %d 条", index - 1)
    print("[Hooper AI 2.0] " .. imported_msg)
    LogMsg(imported_msg)
    LogMsg("导入后轨道变化: " .. delta_summary)

    if final_delta.reused_target_track then
        local ok_msg = string.format("字幕已更新，样式轨复用成功（轨道 %d）", current_subtitle_target_track)
        print("[Hooper AI 2.0] " .. ok_msg)
        LogMsg(ok_msg)
        if status then status:Set("Text", ok_msg) end
    elseif final_delta.detected_track then
        local warn_msg = string.format(
            "字幕已更新，但 Resolve 将新字幕放入了轨道 %d，未复用目标轨样式",
            final_delta.detected_track
        )
        print("[Hooper AI 2.0] " .. warn_msg)
        LogMsg(warn_msg)
        if status then status:Set("Text", warn_msg) end
    else
        local unknown_msg = "字幕已导入，但未检测到新增字幕轨变化，请手动检查时间线"
        print("[Hooper AI 2.0] " .. unknown_msg)
        LogMsg(unknown_msg)
        if status then status:Set("Text", unknown_msg) end
    end

    if fallback_locked then
        local unlock_ok_after, unlock_err_after = unlock_all_subtitle_tracks(timeline)
        if unlock_ok_after then
            LogMsg("已解除锁轨兜底")
        else
            print("[Hooper AI 2.0] 解除锁轨兜底失败: " .. tostring(unlock_err_after))
            LogMsg("解除锁轨兜底失败: " .. tostring(unlock_err_after))
        end
    end

end

-- ========== AI 处理引擎（黑科技：临时文件 + curl）==========
local function do_ai_fix()
    print("[Hooper AI 2.0] AI 处理按钮点击")

    -- 重置取消标志，新一次 AI 流程从干净状态开始
    AI_CANCEL_REQUESTED = false

    local status = win:Find("StatusLabel")
    if status then status:Set("Text", "正在调用 AI 处理...") end
    reset_pending_review_session()
    
    -- 获取 AI 配置；配置弹窗按需创建，未打开时直接读取本地持久化配置。
    local provider_def = get_provider_def(current_ai_provider_id)
    local provider_config = read_provider_config_from_ui(provider_def.id)
    local provider_protocol = tostring(provider_def.protocol or "openai_compatible")
    local api_url = provider_allows_api_url_edit(provider_def) and provider_config.api_url or provider_def.api_url
    local api_key = trim_text(provider_config.api_key or "")
    local model = trim_text(provider_config.model or "")
    local shared_config = read_shared_config_from_ui()
    update_reference_script_risk_label(shared_config.script_content)

    print("[Hooper AI 2.0] 去除空格后的 API URL: " .. tostring(api_url))
    print("[Hooper AI 2.0] 去除空格后的 Model: " .. tostring(model))

    SaveProviderConfig(provider_def.id, provider_config)
    SaveActiveProviderId(provider_def.id)
    SaveSharedConfig(shared_config.script_content, shared_config.is_script_enabled)

    api_url = normalize_api_url_for_request(api_url)
    if model == "" and not provider_def.is_custom then
        model = tostring(provider_def.default_model or "")
    end

    if provider_protocol == "gemini_native" then
        api_url = normalize_api_url_for_request(tostring(provider_def.api_url or api_url))
    elseif provider_protocol == "openai_compatible" then
        api_url = build_openai_compatible_request_url(api_url)
    end

    if provider_allows_api_url_edit(provider_def) then
        if api_url == "" then
            print("[Hooper AI 2.0] 请输入 API Base URL")
            if status then status:Set("Text", "请输入 API Base URL") end
            return
        end
        if model == "" then
            print("[Hooper AI 2.0] 请输入模型名称")
            if status then status:Set("Text", "请输入模型名称") end
            return
        end
    elseif provider_protocol == "gemini_native" and model == "" then
        print("[Hooper AI 2.0] 请输入 Gemini 模型名称")
        if status then status:Set("Text", "请输入 Gemini 模型名称") end
        return
    end

    if api_key == "" then
        print("[Hooper AI 2.0] 请输入 API Key")
        if status then status:Set("Text", "请输入 API Key") end
        return
    end
    
    -- 修复：优先使用 current_rows，避免搜索过滤导致 AI 处理数据丢失
    if not current_rows or #current_rows == 0 then
        print("[Hooper AI 2.0] 没有字幕数据")
        if status then status:Set("Text", "没有字幕数据") end
        return
    end
    
    -- 提取 subtitles 文本，组装成 序号|文本 格式
    local sorted_list = {}
    for _, row in ipairs(current_rows) do
        if type(row) == "table" and row.text then
            table.insert(sorted_list, row)
        end
    end
    -- 核心修复：按 start_frame 排序，确保 AI 处理的时序正确
    table.sort(sorted_list, function(a, b)
        return (tonumber(a.start_frame) or 0) < (tonumber(b.start_frame) or 0)
    end)
    
    local function build_subtitle_list(start_idx, end_idx)
        local lines = {}
        local from_idx = math.max(1, tonumber(start_idx) or 1)
        local to_idx = math.min(#sorted_list, tonumber(end_idx) or #sorted_list)
        for i = from_idx, to_idx do
            lines[#lines + 1] = i .. "|" .. tostring(sorted_list[i].text or "")
        end
        return table.concat(lines, "\n")
    end

    local subtitle_list = build_subtitle_list(1, #sorted_list)
    
    print("[Hooper AI 2.0] 提取了 " .. #sorted_list .. " 条字幕")
    
    -- 第二步：升级 JSON 转义与解析能力
    local ai_helpers = (function()
    local AUTO_APPLY_CONFIDENCE = 0.90
    local AUTO_APPLY_ERROR_TYPES = {
        homophone = true,
        particle = true,
        repetition = true,
        missing_char = true,
        punctuation = true
    }
    local PROTECTED_TERMS = {
        "达芬奇",
        "时间线",
        "音频尾迹",
        "蒙版",
        "调色页",
        "小潘",
        "B-roll",
        "B Roll",
        "BROLL"
    }

    local function escape_json(str)
        if not str then return "" end
        str = str:gsub("\\", "\\\\")
        str = str:gsub('"', '\\"')
        str = str:gsub("\n", "\\n")
        str = str:gsub("\r", "")
        str = str:gsub("\t", "\\t")
        return str
    end

    local function strip_all_spaces(str)
        return tostring(str or ""):gsub("%s+", "")
    end

    local function escape_lua_pattern(str)
        return tostring(str or ""):gsub("([^%w])", "%%%1")
    end

    local function extract_ascii_tokens(text)
        local tokens = {}
        for token in tostring(text or ""):gmatch("[A-Za-z0-9][A-Za-z0-9%-%._+/]*") do
            tokens[#tokens + 1] = string.lower(token)
        end
        return tokens
    end

    local function ascii_tokens_equal(left, right)
        if #left ~= #right then
            return false
        end

        for i = 1, #left do
            if left[i] ~= right[i] then
                return false
            end
        end

        return true
    end

    local function is_ascii_case_only_change(original_text, corrected_text)
        local original = tostring(original_text or "")
        local corrected = tostring(corrected_text or "")
        if original == corrected then
            return false
        end
        if not original:match("[A-Za-z]") and not corrected:match("[A-Za-z]") then
            return false
        end
        return string.lower(original) == string.lower(corrected)
    end

    local GREETING_NORMALIZATION_BLOCK_REASON = "疑似把口播开场白整体改写成标准问候语"

    local function starts_with_literal(text, prefix)
        local source = tostring(text or "")
        local needle = tostring(prefix or "")
        if needle == "" then
            return false
        end
        return source:sub(1, #needle) == needle
    end

    local function is_forbidden_greeting_normalization(original_text, corrected_text)
        local original = trim_text(original_text)
        local corrected = trim_text(corrected_text)
        if original == "" or corrected == "" or original == corrected then
            return false
        end

        local standard_greetings = {"各位好", "大家好", "你好"}
        local corrected_has_standard_greeting = false

        local original_intro_start = original:find("我是", 1, true)
        local corrected_intro_start = corrected:find("我是", 1, true)
        if not original_intro_start or not corrected_intro_start then
            return false
        end

        local original_suffix = strip_all_spaces(original:sub(original_intro_start))
        local corrected_suffix = strip_all_spaces(corrected:sub(corrected_intro_start))
        if original_suffix ~= corrected_suffix then
            return false
        end

        local original_prefix = strip_all_spaces(original:sub(1, original_intro_start - 1))
        local corrected_prefix = strip_all_spaces(corrected:sub(1, corrected_intro_start - 1))
        if original_prefix == "" or corrected_prefix == "" or original_prefix == corrected_prefix then
            return false
        end

        for _, greeting in ipairs(standard_greetings) do
            local original_has_standard_greeting = original_prefix == greeting
            local corrected_has_this_greeting = corrected_prefix == greeting
            if original_has_standard_greeting then
                return false
            end
            if corrected_has_this_greeting then
                corrected_has_standard_greeting = true
                break
            end
        end
        if not corrected_has_standard_greeting then
            return false
        end

        return true, GREETING_NORMALIZATION_BLOCK_REASON
    end

    local function is_style_only_rewrite(original_text, corrected_text)
        local original = tostring(original_text or "")
        local corrected = tostring(corrected_text or "")

        if original == corrected then
            return false
        end

        if original:gsub("一个事情", "一件事情") == corrected then
            return true, "口语被书面化：一个事情 -> 一件事情"
        end

        if original:gsub("一个事", "一件事") == corrected then
            return true, "口语被书面化：一个事 -> 一件事"
        end

        return false
    end

    local FORBIDDEN_LITERAL_REWRITES = {
        {from = "珍品", to = "精品", reason = "非同音普通词互换：珍品 -> 精品"},
        {from = "作用", to = "施加", reason = "近义词替换：作用 -> 施加"},
        {from = "作用", to = "应用", reason = "近义词替换：作用 -> 应用"},
        {from = "然后", to = "接着", reason = "近义词替换：然后 -> 接着"},
        {from = "那回到", to = "就回到", reason = "逻辑词被改写：那回到 -> 就回到"},
    }

    local function find_forbidden_literal_rewrite(original_text, corrected_text)
        local original = tostring(original_text or "")
        local corrected = tostring(corrected_text or "")
        if original == "" or corrected == "" or original == corrected then
            return nil
        end

        for _, item in ipairs(FORBIDDEN_LITERAL_REWRITES) do
            if original:find(item.from, 1, true) and corrected:find(item.to, 1, true) then
                local original_without = original:gsub(escape_lua_pattern(item.from), "", 1)
                local corrected_without = corrected:gsub(escape_lua_pattern(item.to), "", 1)
                if original_without == corrected_without then
                    return item.reason
                end
            end
        end

        return nil
    end

    local function is_shortcut_key_token(token)
        local value = string.lower(tostring(token or ""))
        if value == "" then return false end

        local common_keys = {
            alt = true, option = true, opt = true,
            ctrl = true, control = true,
            shift = true,
            cmd = true, command = true,
            enter = true, ["return"] = true,
            esc = true, escape = true,
            tab = true, space = true,
            del = true, delete = true, backspace = true,
            home = true, ["end"] = true,
            left = true, right = true, up = true, down = true
        }

        if common_keys[value] then
            return true
        end

        if value:match("^[a-z]$") then
            return true
        end

        if value:match("^f%d%d?$") then
            return true
        end

        if value:match("^%d$") then
            return true
        end

        return false
    end

    local function looks_like_shortcut_context(text)
        local content = tostring(text or "")
        local ascii_tokens = extract_ascii_tokens(content)
        if #ascii_tokens == 0 then
            return false
        end

        if content:find("快捷键", 1, true) or content:find("组合键", 1, true) or content:find("按键", 1, true) then
            return true
        end

        if content:find("选择", 1, true) and (content:find("加", 1, true) or content:find("+", 1, true)) then
            return true
        end

        if #ascii_tokens >= 2 and (content:find("加", 1, true) or content:find("+", 1, true)) then
            return true
        end

        return false
    end

    local function allows_shortcut_ascii_correction(original_text, corrected_text)
        if not looks_like_shortcut_context(original_text) and not looks_like_shortcut_context(corrected_text) then
            return false
        end

        local original_tokens = extract_ascii_tokens(original_text)
        local corrected_tokens = extract_ascii_tokens(corrected_text)
        if #original_tokens == 0 or #original_tokens ~= #corrected_tokens then
            return false
        end

        local diff_count = 0
        for i = 1, #original_tokens do
            if original_tokens[i] ~= corrected_tokens[i] then
                diff_count = diff_count + 1
                if not is_shortcut_key_token(corrected_tokens[i]) then
                    return false
                end
            end
        end

        return diff_count > 0
    end

    local function classify_domain_ascii_token(token)
        local value = string.lower(tostring(token or ""))
        if value == "" then
            return nil
        end

        local compact = value:gsub("[%._%-%+/]", "")
        if compact == "h264" or compact == "h164" then
            return "h264"
        end
        if compact == "h265" or compact == "h165" then
            return "h265"
        end

        return nil
    end

    local function allows_domain_ascii_correction(original_text, corrected_text)
        local original_tokens = extract_ascii_tokens(original_text)
        local corrected_tokens = extract_ascii_tokens(corrected_text)
        if #original_tokens == 0 or #original_tokens ~= #corrected_tokens then
            return false
        end

        local diff_count = 0
        for i = 1, #original_tokens do
            if original_tokens[i] ~= corrected_tokens[i] then
                diff_count = diff_count + 1
                local original_domain = classify_domain_ascii_token(original_tokens[i])
                local corrected_domain = classify_domain_ascii_token(corrected_tokens[i])
                if not original_domain or not corrected_domain or original_domain ~= corrected_domain then
                    return false
                end
            end
        end

        return diff_count > 0
    end

    local function is_plain_english_word(token)
        local value = tostring(token or "")
        return value:match("^[a-z]+$") ~= nil
    end

    local function levenshtein_distance(a, b)
        local left = tostring(a or "")
        local right = tostring(b or "")
        local left_len = #left
        local right_len = #right

        if left == right then
            return 0
        end
        if left_len == 0 then
            return right_len
        end
        if right_len == 0 then
            return left_len
        end

        local prev = {}
        local curr = {}
        for j = 0, right_len do
            prev[j] = j
        end

        for i = 1, left_len do
            curr[0] = i
            local left_char = left:sub(i, i)
            for j = 1, right_len do
                local cost = left_char == right:sub(j, j) and 0 or 1
                local deletion = prev[j] + 1
                local insertion = curr[j - 1] + 1
                local substitution = prev[j - 1] + cost
                local best = math.min(deletion, insertion, substitution)
                if i > 1 and j > 1
                    and left_char == right:sub(j - 1, j - 1)
                    and left:sub(i - 1, i - 1) == right:sub(j, j) then
                    best = math.min(best, prev[j - 2] + 1)
                end
                curr[j] = best
            end
            prev, curr = curr, prev
        end

        return prev[right_len]
    end

    local function allows_english_spelling_correction(original_text, corrected_text)
        if looks_like_shortcut_context(original_text) or looks_like_shortcut_context(corrected_text) then
            return false
        end

        local original_tokens = extract_ascii_tokens(original_text)
        local corrected_tokens = extract_ascii_tokens(corrected_text)
        if #original_tokens == 0 or #original_tokens ~= #corrected_tokens then
            return false
        end

        local diff_count = 0
        for i = 1, #original_tokens do
            local original_token = original_tokens[i]
            local corrected_token = corrected_tokens[i]
            if original_token ~= corrected_token then
                diff_count = diff_count + 1
                if diff_count > 2 then
                    return false
                end
                if not is_plain_english_word(original_token) or not is_plain_english_word(corrected_token) then
                    return false
                end
                if #original_token < 5 or #corrected_token < 5 then
                    return false
                end
                if is_shortcut_key_token(original_token) or is_shortcut_key_token(corrected_token) then
                    return false
                end
                if classify_domain_ascii_token(original_token) or classify_domain_ascii_token(corrected_token) then
                    return false
                end
                if math.abs(#original_token - #corrected_token) > 1 then
                    return false
                end
                if levenshtein_distance(original_token, corrected_token) > 2 then
                    return false
                end
            end
        end

        return diff_count > 0
    end

    local function split_text_chars_for_diff_local(str)
        local chars = {}
        local value = tostring(str or "")
        if value == "" then
            return chars
        end

        if utf8 and utf8.codes and utf8.char then
            local ok = pcall(function()
                for _, codepoint in utf8.codes(value) do
                    chars[#chars + 1] = utf8.char(codepoint)
                end
            end)
            if ok then
                return chars
            end
        end

        local index = 1
        while index <= #value do
            local char_len = get_utf8_fallback_char_len(string.byte(value, index))
            chars[#chars + 1] = value:sub(index, index + char_len - 1)
            index = index + char_len
        end
        if #chars > 0 then
            return chars
        end

        for i = 1, #value do
            chars[#chars + 1] = value:sub(i, i)
        end
        return chars
    end

    local function is_particle_char(ch)
        return ch == "的" or ch == "地" or ch == "得"
    end

    local function is_particle_only_change(original_text, corrected_text)
        local original_chars = split_text_chars_for_diff_local(original_text or "")
        local corrected_chars = split_text_chars_for_diff_local(corrected_text or "")

        if #original_chars ~= #corrected_chars then
            return false
        end

        local changed = false
        for i = 1, #original_chars do
            local old_ch = original_chars[i]
            local new_ch = corrected_chars[i]
            if old_ch ~= new_ch then
                if not is_particle_char(old_ch) or not is_particle_char(new_ch) then
                    return false
                end
                changed = true
            end
        end

        return changed
    end

    local function is_single_particle_insertion(original_text, corrected_text)
        local original_chars = split_text_chars_for_diff_local(original_text or "")
        local corrected_chars = split_text_chars_for_diff_local(corrected_text or "")

        if #corrected_chars ~= #original_chars + 1 then
            return false
        end

        local i = 1
        local j = 1
        local inserted = false

        while i <= #original_chars and j <= #corrected_chars do
            if original_chars[i] == corrected_chars[j] then
                i = i + 1
                j = j + 1
            elseif not inserted and is_particle_char(corrected_chars[j]) then
                inserted = true
                j = j + 1
            else
                return false
            end
        end

        if not inserted and j <= #corrected_chars and is_particle_char(corrected_chars[j]) then
            inserted = true
            j = j + 1
        end

        return inserted and i > #original_chars and j > #corrected_chars
    end

    local function is_single_particle_deletion(original_text, corrected_text)
        return is_single_particle_insertion(corrected_text, original_text)
    end

    local compute_char_overlap_ratio

    local function strip_title_brackets(text)
        return tostring(text or ""):gsub("《", ""):gsub("》", "")
    end

    local function text_char_suffix(str, count)
        local chars = split_text_chars_for_diff_local(str or "")
        local need = math.max(0, math.min(tonumber(count) or 0, #chars))
        if need <= 0 then
            return ""
        end
        local out = {}
        for i = #chars - need + 1, #chars do
            out[#out + 1] = chars[i]
        end
        return table.concat(out)
    end

    local function is_safe_spacing_change(original_text, corrected_text)
        local original = tostring(original_text or "")
        local corrected = tostring(corrected_text or "")
        if original == corrected then
            return false
        end
        return strip_all_spaces(original) == strip_all_spaces(corrected)
    end

    local function has_adjacent_same_char(chars, index, ch)
        if type(chars) ~= "table" or type(ch) ~= "string" or ch == "" then
            return false
        end
        if index > 1 and chars[index - 1] == ch then
            return true
        end
        if index < #chars and chars[index + 1] == ch then
            return true
        end
        return false
    end

    local function is_safe_single_char_delta(original_text, corrected_text)
        local original_chars = split_text_chars_for_diff_local(original_text or "")
        local corrected_chars = split_text_chars_for_diff_local(corrected_text or "")
        local diff = #corrected_chars - #original_chars
        if math.abs(diff) ~= 1 then
            return false
        end

        local i = 1
        local j = 1
        local skipped = false
        local skipped_char = nil
        local skipped_index = nil
        local skipped_from = nil

        while i <= #original_chars and j <= #corrected_chars do
            if original_chars[i] == corrected_chars[j] then
                i = i + 1
                j = j + 1
            elseif skipped then
                return false
            elseif diff == 1 then
                skipped = true
                skipped_char = corrected_chars[j]
                skipped_index = j
                skipped_from = "corrected"
                j = j + 1
            else
                skipped = true
                skipped_char = original_chars[i]
                skipped_index = i
                skipped_from = "original"
                i = i + 1
            end
        end

        if not skipped then
            if diff == 1 and j <= #corrected_chars then
                skipped = true
                skipped_char = corrected_chars[j]
                skipped_index = j
                skipped_from = "corrected"
                j = j + 1
            elseif diff == -1 and i <= #original_chars then
                skipped = true
                skipped_char = original_chars[i]
                skipped_index = i
                skipped_from = "original"
                i = i + 1
            end
        end

        if not (skipped and i > #original_chars and j > #corrected_chars) then
            return false
        end

        if skipped_char == "《" or skipped_char == "》" then
            return true
        end

        if is_particle_char(skipped_char) then
            return true
        end

        if skipped_from == "original" and has_adjacent_same_char(original_chars, skipped_index, skipped_char) then
            return true
        end

        if skipped_from == "corrected" and has_adjacent_same_char(corrected_chars, skipped_index, skipped_char) then
            return true
        end

        if type(skipped_char) == "string" and skipped_char ~= "" and not skipped_char:match("[%w]") then
            return false
        end
        return false
    end

    local function is_safe_title_reference_change(original_text, corrected_text)
        local original = tostring(original_text or "")
        local corrected = tostring(corrected_text or "")
        if corrected == "" or corrected == original then
            return false
        end
        if not corrected:find("《", 1, true) or not corrected:find("》", 1, true) then
            return false
        end
        if original:find("《", 1, true) or original:find("》", 1, true) then
            return false
        end

        local stripped_corrected = strip_title_brackets(corrected)
        local stripped_original = strip_title_brackets(original)
        local overlap_ratio = compute_char_overlap_ratio(stripped_original, stripped_corrected)
        if overlap_ratio < 0.34 then
            return false
        end

        if stripped_original == stripped_corrected then
            return true
        end

        if corrected:find("电影《", 1, true) or corrected:find("影片《", 1, true) or corrected:find("片中《", 1, true) then
            return true
        end

        local title = corrected:match("《([^《》]+)》")
        if title and title ~= "" then
            if stripped_original:find(title, 1, true) then
                return true
            end
            if title:find(stripped_original, 1, true) then
                return true
            end
            local tail_len = math.min(3, count_utf8_chars(title), count_utf8_chars(stripped_original))
            if tail_len >= 2 then
                local original_tail = text_char_suffix(stripped_original, tail_len)
                local title_tail = text_char_suffix(title, tail_len)
                if original_tail ~= "" and original_tail == title_tail and overlap_ratio >= 0.40 then
                    return true
                end
            end
        end

        return false
    end

    compute_char_overlap_ratio = function(a, b)
        local counts = {}
        for _, ch in ipairs(split_text_chars_for_diff_local(a or "")) do
            counts[ch] = (counts[ch] or 0) + 1
        end

        local overlap = 0
        for _, ch in ipairs(split_text_chars_for_diff_local(b or "")) do
            local remain = counts[ch] or 0
            if remain > 0 then
                counts[ch] = remain - 1
                overlap = overlap + 1
            end
        end

        return overlap / math.max(count_utf8_chars(a), count_utf8_chars(b), 1)
    end

    local function count_literal_occurrences(text, literal)
        local count = 0
        local start_pos = 1
        while true do
            local s, e = tostring(text or ""):find(literal, start_pos, true)
            if not s then break end
            count = count + 1
            start_pos = e + 1
        end
        return count
    end

    local function has_balanced_pairs(text)
        local pair_list = {
            {"(", ")"},
            {"[", "]"},
            {"{", "}"},
            {"（", "）"},
            {"【", "】"},
            {"《", "》"},
            {"「", "」"},
            {"『", "』"},
            {"“", "”"}
        }

        for _, pair in ipairs(pair_list) do
            if count_literal_occurrences(text, pair[1]) ~= count_literal_occurrences(text, pair[2]) then
                return false, pair[1] .. pair[2]
            end
        end

        if count_literal_occurrences(text, '"') % 2 ~= 0 then
            return false, '"'
        end

        return true
    end

    local function looks_like_only_punctuation(text)
        local stripped = trim_text(text)
        if stripped == "" then
            return true
        end

        stripped = stripped:gsub("%s+", "")
        local punctuations = {
            ".", ",", "!", "?", ":", ";", "/", "\\", '"',
            "，", "。", "！", "？", "：", "；", "、",
            "“", "”", "‘", "’", "（", "）", "【", "】", "《", "》",
            "(", ")", "[", "]", "{", "}"
        }

        for _, token in ipairs(punctuations) do
            stripped = stripped:gsub(escape_lua_pattern(token), "")
        end

        return stripped == ""
    end

    local function contains_literal_case_insensitive(text, token)
        local source = string.lower(tostring(text or ""))
        local needle = string.lower(tostring(token or ""))
        if needle == "" then return false end
        return source:find(needle, 1, true) ~= nil
    end

    local function find_changed_protected_term(original_text, corrected_text)
        for _, term in ipairs(PROTECTED_TERMS) do
            local in_original = contains_literal_case_insensitive(original_text, term)
            local in_corrected = contains_literal_case_insensitive(corrected_text, term)
            if in_original ~= in_corrected then
                return term
            end
        end
        return nil
    end

    local function decode_json_text(json_text)
        if type(json_text) ~= "string" or json_text == "" then
            return nil, "JSON 为空或不是字符串"
        end

        local pos = 1
        local json_len = #json_text
        local parse_value

        local function fail(msg)
            error(msg .. "（位置 " .. tostring(pos) .. "）", 0)
        end

        local function skip_whitespace()
            while pos <= json_len do
                local ch = json_text:sub(pos, pos)
                if ch == " " or ch == "\n" or ch == "\r" or ch == "\t" then
                    pos = pos + 1
                else
                    break
                end
            end
        end

        local function codepoint_to_utf8(code)
            if code <= 127 then
                return string.char(code)
            elseif code <= 2047 then
                local byte1 = 192 + math.floor(code / 64)
                local byte2 = 128 + (code % 64)
                return string.char(byte1, byte2)
            elseif code <= 65535 then
                local byte1 = 224 + math.floor(code / 4096)
                local byte2 = 128 + (math.floor(code / 64) % 64)
                local byte3 = 128 + (code % 64)
                return string.char(byte1, byte2, byte3)
            elseif code <= 1114111 then
                local byte1 = 240 + math.floor(code / 262144)
                local byte2 = 128 + (math.floor(code / 4096) % 64)
                local byte3 = 128 + (math.floor(code / 64) % 64)
                local byte4 = 128 + (code % 64)
                return string.char(byte1, byte2, byte3, byte4)
            end

            return ""
        end

        local function parse_string()
            if json_text:sub(pos, pos) ~= '"' then
                fail("JSON 字符串必须以双引号开始")
            end

            pos = pos + 1
            local parts = {}
            local chunk_start = pos

            while pos <= json_len do
                local ch = json_text:sub(pos, pos)
                if ch == '"' then
                    if pos > chunk_start then
                        table.insert(parts, json_text:sub(chunk_start, pos - 1))
                    end
                    pos = pos + 1
                    return table.concat(parts)
                elseif ch == "\\" then
                    if pos > chunk_start then
                        table.insert(parts, json_text:sub(chunk_start, pos - 1))
                    end

                    local esc = json_text:sub(pos + 1, pos + 1)
                    if esc == "" then
                        fail("JSON 字符串转义不完整")
                    elseif esc == '"' or esc == "\\" or esc == "/" then
                        table.insert(parts, esc)
                        pos = pos + 2
                    elseif esc == "b" then
                        table.insert(parts, "\b")
                        pos = pos + 2
                    elseif esc == "f" then
                        table.insert(parts, "\f")
                        pos = pos + 2
                    elseif esc == "n" then
                        table.insert(parts, "\n")
                        pos = pos + 2
                    elseif esc == "r" then
                        table.insert(parts, "\r")
                        pos = pos + 2
                    elseif esc == "t" then
                        table.insert(parts, "\t")
                        pos = pos + 2
                    elseif esc == "u" then
                        local hex = json_text:sub(pos + 2, pos + 5)
                        if #hex < 4 or not hex:match("^[0-9a-fA-F]+$") then
                            fail("JSON Unicode 转义无效")
                        end

                        local code = tonumber(hex, 16)
                        pos = pos + 6

                        if code >= 55296 and code <= 56319 and json_text:sub(pos, pos + 1) == "\\u" then
                            local low_hex = json_text:sub(pos + 2, pos + 5)
                            local low_code = low_hex:match("^[0-9a-fA-F]+$") and tonumber(low_hex, 16) or nil
                            if low_code and low_code >= 56320 and low_code <= 57343 then
                                code = 65536 + (code - 55296) * 1024 + (low_code - 56320)
                                pos = pos + 6
                            end
                        end

                        table.insert(parts, codepoint_to_utf8(code))
                    else
                        fail("遇到不支持的 JSON 转义字符")
                    end

                    chunk_start = pos
                else
                    local byte = string.byte(json_text, pos)
                    if byte and byte < 32 then
                        fail("JSON 字符串包含非法控制字符")
                    end
                    pos = pos + 1
                end
            end

            fail("JSON 字符串未正确闭合")
        end

        local function parse_number()
            local tail = json_text:sub(pos)
            local number_text = tail:match("^%-?%d+%.%d+[eE][%+%-]?%d+")
                or tail:match("^%-?%d+%.%d+")
                or tail:match("^%-?%d+[eE][%+%-]?%d+")
                or tail:match("^%-?%d+")

            if not number_text then
                fail("JSON 数字格式无效")
            end

            local value = tonumber(number_text)
            if not value then
                fail("JSON 数字无法转换")
            end

            pos = pos + #number_text
            return value
        end

        local function parse_array()
            pos = pos + 1
            skip_whitespace()

            local result = {}
            if json_text:sub(pos, pos) == "]" then
                pos = pos + 1
                return result
            end

            while true do
                result[#result + 1] = parse_value()
                skip_whitespace()

                local ch = json_text:sub(pos, pos)
                if ch == "," then
                    pos = pos + 1
                    skip_whitespace()
                elseif ch == "]" then
                    pos = pos + 1
                    break
                else
                    fail("JSON 数组缺少逗号或右中括号")
                end
            end

            return result
        end

        local function parse_object()
            pos = pos + 1
            skip_whitespace()

            local result = {}
            if json_text:sub(pos, pos) == "}" then
                pos = pos + 1
                return result
            end

            while true do
                skip_whitespace()
                if json_text:sub(pos, pos) ~= '"' then
                    fail("JSON 对象键必须是字符串")
                end

                local key = parse_string()
                skip_whitespace()
                if json_text:sub(pos, pos) ~= ":" then
                    fail("JSON 对象键值之间缺少冒号")
                end

                pos = pos + 1
                result[key] = parse_value()
                skip_whitespace()

                local ch = json_text:sub(pos, pos)
                if ch == "," then
                    pos = pos + 1
                    skip_whitespace()
                elseif ch == "}" then
                    pos = pos + 1
                    break
                else
                    fail("JSON 对象缺少逗号或右大括号")
                end
            end

            return result
        end

        parse_value = function()
            skip_whitespace()
            local ch = json_text:sub(pos, pos)

            if ch == "" then
                fail("JSON 提前结束")
            elseif ch == '"' then
                return parse_string()
            elseif ch == "{" then
                return parse_object()
            elseif ch == "[" then
                return parse_array()
            elseif ch == "t" and json_text:sub(pos, pos + 3) == "true" then
                pos = pos + 4
                return true
            elseif ch == "f" and json_text:sub(pos, pos + 4) == "false" then
                pos = pos + 5
                return false
            elseif ch == "n" and json_text:sub(pos, pos + 3) == "null" then
                pos = pos + 4
                return nil
            elseif ch == "-" or ch:match("%d") then
                return parse_number()
            end

            fail("无法识别的 JSON 值")
        end

        local ok, result = pcall(function()
            skip_whitespace()
            local value = parse_value()
            skip_whitespace()
            if pos <= json_len then
                fail("JSON 尾部存在多余内容")
            end
            return value
        end)

        if ok then
            return result
        end

        return nil, tostring(result)
    end

    local function strip_code_block(text)
        local trimmed = trim_text(text)
        return trimmed:match("^```json%s*(.-)%s*```$")
            or trimmed:match("^```JSON%s*(.-)%s*```$")
            or trimmed:match("^```%s*(.-)%s*```$")
            or trimmed
    end

    local function normalize_action(action)
        local value = trim_text(action):lower()
        if value == "correct" or value == "fix" or value == "change" then
            return "correct"
        elseif value == "keep" or value == "none" or value == "unchanged" then
            return "keep"
        end
        return "review"
    end

    local function normalize_error_type(error_type)
        local value = trim_text(error_type):lower()
        value = value:gsub("[%s%-]+", "_")
        if value == "" then
            return "other"
        end
        return value
    end

    local function parse_ai_fix_payload(ai_text)
        local cleaned_text = strip_code_block(ai_text)
        local payload, parse_err = decode_json_text(cleaned_text)
        if not payload then
            return nil, "AI 返回不是合法 JSON：" .. tostring(parse_err)
        end

        if type(payload.items) ~= "table" then
            return nil, "AI 返回缺少 items 数组"
        end

        local item_map = {}
        local seen_indices = {}
        local skipped_items = {}

        local function add_skipped_item(idx, source_item, skip_reason)
            local fallback_text = ""
            if idx and sorted_list[idx] then
                fallback_text = sorted_list[idx].text or ""
            end

            table.insert(skipped_items, {
                index = idx,
                original = type(source_item) == "table" and type(source_item.original) == "string" and source_item.original or fallback_text,
                corrected = type(source_item) == "table" and type(source_item.corrected) == "string" and source_item.corrected or "",
                reason = type(source_item) == "table" and trim_text(source_item.reason) or "",
                confidence = type(source_item) == "table" and math.max(0, math.min(1, tonumber(source_item.confidence) or 0)) or 0,
                error_type = type(source_item) == "table" and normalize_error_type(source_item.error_type) or "other",
                skip_reason = skip_reason or "非法修正项"
            })
        end

        for _, item in ipairs(payload.items) do
            if type(item) ~= "table" then
                add_skipped_item(nil, nil, "items 中存在非对象项")
                goto continue
            end

            local idx = tonumber(item.index)
            if not idx or idx ~= math.floor(idx) then
                add_skipped_item(nil, item, "items 中存在非法 index")
                goto continue
            end
            if idx < 1 or idx > #sorted_list then
                add_skipped_item(idx, item, "items 中存在越界 index")
                goto continue
            end
            if seen_indices[idx] then
                add_skipped_item(idx, item, "items 中存在重复 index")
                goto continue
            end
            seen_indices[idx] = true

            local normalized_item = {
                index = idx,
                original = type(item.original) == "string" and item.original or "",
                corrected = type(item.corrected) == "string" and item.corrected or "",
                action = item.action ~= nil and normalize_action(item.action) or "correct",
                reason = trim_text(item.reason),
                confidence = math.max(0, math.min(1, tonumber(item.confidence) or 0)),
                error_type = normalize_error_type(item.error_type)
            }

            if normalized_item.action == "correct" then
                if normalized_item.original ~= (sorted_list[idx].text or "") then
                    add_skipped_item(idx, item, string.format("第 %d 行 original 与输入不一致", idx))
                    goto continue
                end
                if trim_text(normalized_item.corrected) == "" then
                    add_skipped_item(idx, item, string.format("第 %d 行 corrected 为空", idx))
                    goto continue
                end
                item_map[idx] = normalized_item
            end

            ::continue::
        end

        return item_map, nil, skipped_items
    end

    local function parse_ai_line_payload(ai_text, expected_count, start_index, allow_missing_keep_original)
        local cleaned_text = strip_code_block(ai_text)
        local item_map = {}
        local actual_count = 0
        local first_index = tonumber(start_index) or 1
        local last_index = first_index + expected_count - 1
        local missing_indices = {}

        for line in cleaned_text:gmatch("[^\r\n]+") do
            local trimmed_line = trim_text(line)
            if trimmed_line ~= "" then
                local idx, text = trimmed_line:match("^(%d+)|(.+)$")
                if not idx or text == nil then
                    return nil, "存在无法解析的输出行：" .. trimmed_line
                end

                idx = tonumber(idx)
                if idx < first_index or idx > last_index then
                    return nil, "返回 index 越界：" .. tostring(idx)
                end
                if item_map[idx] then
                    return nil, "返回中存在重复 index：" .. tostring(idx)
                end

                item_map[idx] = text
                actual_count = actual_count + 1
            end
        end

        for i = first_index, last_index do
            if type(item_map[i]) ~= "string" or trim_text(item_map[i]) == "" then
                if allow_missing_keep_original and sorted_list[i] and type(sorted_list[i].text) == "string" and trim_text(sorted_list[i].text) ~= "" then
                    item_map[i] = sorted_list[i].text
                    missing_indices[#missing_indices + 1] = i
                else
                    if actual_count ~= expected_count then
                        return nil, string.format("返回行数不一致：期望 %d，实际 %d", expected_count, actual_count)
                    end
                    return nil, "返回缺少或存在空文本：index=" .. tostring(i)
                end
            end
        end

        if actual_count ~= expected_count and not allow_missing_keep_original then
            return nil, string.format("返回行数不一致：期望 %d，实际 %d", expected_count, actual_count)
        end

        return item_map, nil, missing_indices
    end

    local function validate_ai_fix_candidate(original_text, item)
        local corrected = tostring(item.corrected or "")
        local trimmed = trim_text(corrected)

        if corrected == original_text then
            return false, "修正结果与原文一致"
        end
        if trimmed == "" then
            return false, "修正结果为空"
        end
        if trimmed == "\\" or trimmed == "/" then
            return false, "修正结果是脏值"
        end
        if trimmed == '"' or trimmed == "“" or trimmed == "”" then
            return false, "修正结果只剩单个引号"
        end
        if count_utf8_chars(trimmed) <= 2 and looks_like_only_punctuation(trimmed) then
            return false, "修正结果只剩标点"
        end

        local balanced, pair_label = has_balanced_pairs(corrected)
        if not balanced then
            return false, "括号或引号不平衡：" .. tostring(pair_label)
        end

        local original_len = count_utf8_chars(trim_text(original_text))
        local corrected_len = count_utf8_chars(trimmed)
        if item.error_type ~= "repetition" and original_len >= 4 and corrected_len <= math.max(1, math.floor(original_len * 0.35)) then
            return false, "改动后长度异常缩短"
        end

        local overlap_ratio = compute_char_overlap_ratio(original_text, corrected)
        if original_len >= 6 and overlap_ratio < 0.35 then
            return false, string.format("改动幅度过大（重合度 %.2f）", overlap_ratio)
        end

        local changed_protected_term = find_changed_protected_term(original_text, corrected)
        if changed_protected_term then
            return false, "触发高风险词保护：" .. changed_protected_term
        end

        local forbidden_literal_rewrite_reason = find_forbidden_literal_rewrite(original_text, corrected)
        if forbidden_literal_rewrite_reason then
            return false, forbidden_literal_rewrite_reason
        end

        local is_style_rewrite, style_reason = is_style_only_rewrite(original_text, corrected)
        if is_style_rewrite then
            return false, style_reason
        end

        if is_ascii_case_only_change(original_text, corrected) then
            return false, "仅修改了英文字母大小写"
        end

        local original_ascii_tokens = extract_ascii_tokens(original_text)
        local allows_domain_ascii_change = allows_domain_ascii_correction(original_text, corrected)
        local allows_english_spelling_change = allows_english_spelling_correction(original_text, corrected)
        if #original_ascii_tokens > 0 then
            local corrected_ascii_tokens = extract_ascii_tokens(corrected)
            if not ascii_tokens_equal(original_ascii_tokens, corrected_ascii_tokens) then
                if not allows_shortcut_ascii_correction(original_text, corrected)
                    and not allows_domain_ascii_change
                    and not allows_english_spelling_change then
                    return false, "英文、数字或快捷键内容被改动"
                end
            end
            if original_text ~= corrected and strip_all_spaces(original_text) == strip_all_spaces(corrected) then
                return false, "仅修改了英文或数字周围空格"
            end
        end

        if not AUTO_APPLY_ERROR_TYPES[item.error_type] then
            return false, "错误类型不在自动应用白名单：" .. tostring(item.error_type)
        end

        if item.confidence < AUTO_APPLY_CONFIDENCE then
            return false, string.format("置信度不足：%.2f", item.confidence)
        end

        if item.reason == "" then
            return false, "缺少修改理由"
        end

        return true
    end

    local function validate_basic_correction(original_text, corrected_text)
        local corrected = tostring(corrected_text or "")
        local trimmed = trim_text(corrected)

        if corrected == original_text then
            return false, "修正结果与原文一致"
        end
        if trimmed == "" then
            return false, "修正结果为空"
        end
        if trimmed == "\\" or trimmed == "/" then
            return false, "修正结果是脏值"
        end
        if trimmed == '"' or trimmed == "“" or trimmed == "”" then
            return false, "修正结果只剩单个引号"
        end
        if count_utf8_chars(trimmed) <= 2 and looks_like_only_punctuation(trimmed) then
            return false, "修正结果只剩标点"
        end

        local balanced, pair_label = has_balanced_pairs(corrected)
        if not balanced then
            return false, "括号或引号不平衡：" .. tostring(pair_label)
        end

        local forbidden_greeting_normalization, greeting_block_reason = is_forbidden_greeting_normalization(original_text, corrected)
        if forbidden_greeting_normalization then
            return false, greeting_block_reason
        end

        local original_len = count_utf8_chars(trim_text(original_text))
        local corrected_len = count_utf8_chars(trimmed)
        local allows_single_particle_insertion = is_single_particle_insertion(original_text, corrected)
        local allows_single_char_delta = is_safe_single_char_delta(original_text, corrected)
        local allows_title_reference_change = is_safe_title_reference_change(original_text, corrected)
        local allows_spacing_change = is_safe_spacing_change(original_text, corrected)
        local allows_domain_ascii_change = allows_domain_ascii_correction(original_text, corrected)
        local allows_english_spelling_change = allows_english_spelling_correction(original_text, corrected)
        if original_len >= 4 and corrected_len <= math.max(1, math.floor(original_len * 0.35)) then
            return false, "改动后长度异常缩短"
        end

        if corrected_len ~= original_len then
            if not allows_single_particle_insertion
                and not allows_single_char_delta
                and not allows_title_reference_change
                and not allows_spacing_change
                and not allows_domain_ascii_change
                and not allows_english_spelling_change then
                return false, "字数发生变化，可能改变原意，需人工复核"
            end
        end

        local overlap_ratio = compute_char_overlap_ratio(original_text, corrected)
        if not is_particle_only_change(original_text, corrected)
            and not allows_single_particle_insertion
            and not allows_single_char_delta
            and not allows_title_reference_change
            and not allows_spacing_change
            and not allows_domain_ascii_change
            and not allows_english_spelling_change
            and original_len >= 6
            and overlap_ratio < 0.22 then
            return false, string.format("改动幅度过大（重合度 %.2f）", overlap_ratio)
        end

        local changed_protected_term = find_changed_protected_term(original_text, corrected)
        if changed_protected_term then
            return false, "触发高风险词保护：" .. changed_protected_term
        end

        local forbidden_literal_rewrite_reason = find_forbidden_literal_rewrite(original_text, corrected)
        if forbidden_literal_rewrite_reason then
            return false, forbidden_literal_rewrite_reason
        end

        local is_style_rewrite, style_reason = is_style_only_rewrite(original_text, corrected)
        if is_style_rewrite then
            return false, style_reason
        end

        if is_ascii_case_only_change(original_text, corrected) then
            return false, "仅修改了英文字母大小写"
        end

        local original_ascii_tokens = extract_ascii_tokens(original_text)
        if #original_ascii_tokens > 0 then
            local corrected_ascii_tokens = extract_ascii_tokens(corrected)
            if not ascii_tokens_equal(original_ascii_tokens, corrected_ascii_tokens) then
                if not allows_shortcut_ascii_correction(original_text, corrected)
                    and not allows_domain_ascii_change then
                    if allows_english_spelling_change then
                        -- pass
                    else
                        return false, "英文、数字或快捷键内容被改动"
                    end
                end
            end
            if original_text ~= corrected and strip_all_spaces(original_text) == strip_all_spaces(corrected) then
                return false, "仅修改了英文或数字周围空格"
            end
        end

        return true
    end

    local function validate_particle_scope_correction(original_text, corrected_text)
        local corrected = tostring(corrected_text or "")
        local can_apply, block_reason = validate_basic_correction(original_text, corrected)
        if not can_apply then
            return false, block_reason
        end

        if is_particle_only_change(original_text, corrected)
            or is_single_particle_insertion(original_text, corrected)
            or is_single_particle_deletion(original_text, corrected) then
            return true
        end

        return false, "超出“的 / 地 / 得”专项检测范围"
    end

    local function build_pair_combined_text(text_1, text_2)
        return tostring(text_1 or "") .. tostring(text_2 or "")
    end

    local function has_adjacent_boundary_shift_shape(original_1, corrected_1, original_2, corrected_2)
        local delta_1 = count_utf8_chars(trim_text(corrected_1)) - count_utf8_chars(trim_text(original_1))
        local delta_2 = count_utf8_chars(trim_text(corrected_2)) - count_utf8_chars(trim_text(original_2))
        if delta_1 == 0 or delta_2 == 0 then
            return false
        end
        return (delta_1 > 0 and delta_2 < 0) or (delta_1 < 0 and delta_2 > 0)
    end

    local function normalize_particle_variants(text)
        return tostring(text or ""):gsub("[地得]", "的")
    end

    local function build_particle_priority_single_candidate(original_text, candidate_text)
        local original = tostring(original_text or "")
        local candidate = tostring(candidate_text or original)
        if candidate == original then
            return nil
        end

        local normalized_original = strip_all_spaces(normalize_particle_variants(original))
        local normalized_candidate = strip_all_spaces(normalize_particle_variants(candidate))
        local looks_like_particle_priority = (
                normalized_original ~= ""
                and normalized_original == normalized_candidate
            )
            or is_single_particle_insertion(original, candidate)
            or is_particle_only_change(original, candidate)

        if not looks_like_particle_priority then
            return nil
        end

        local can_apply = validate_basic_correction(original, candidate)
        if not can_apply then
            return nil
        end

        return candidate
    end

    local function analyze_particle_bridge_priority_case(original_1, corrected_1, original_2, corrected_2)
        local clean_original_1 = trim_text(original_1)
        local clean_corrected_1 = trim_text(corrected_1)
        local clean_original_2 = trim_text(original_2)
        local clean_corrected_2 = trim_text(corrected_2)

        if clean_original_1 == "" or clean_corrected_1 == "" or clean_original_2 == "" or clean_corrected_2 == "" then
            return nil
        end
        if clean_corrected_1 == clean_original_1 or clean_corrected_2 == clean_original_2 then
            return nil
        end
        if not starts_with_literal(clean_corrected_1, clean_original_1) then
            return nil
        end

        local corrected_extension = trim_text(clean_corrected_1:sub(#clean_original_1 + 1))
        local normalized_extension = strip_all_spaces(normalize_particle_variants(corrected_extension))
        if normalized_extension == "" then
            return nil
        end

        local original_2_chars = split_text_chars_for_diff_local(clean_original_2)
        for prefix_len = math.max(1, #original_2_chars - 1), 1, -1 do
            local prefix_chars = {}
            for idx = 1, prefix_len do
                prefix_chars[#prefix_chars + 1] = original_2_chars[idx]
            end

            local original_prefix = table.concat(prefix_chars)
            if original_prefix:find("[地得]") then
                local normalized_prefix = trim_text(normalize_particle_variants(original_prefix))
                local normalized_prefix_compact = strip_all_spaces(normalized_prefix)
                if normalized_prefix_compact ~= "" and starts_with_literal(normalized_extension, normalized_prefix_compact) then
                    local tail_chars = {}
                    for idx = prefix_len + 1, #original_2_chars do
                        tail_chars[#tail_chars + 1] = original_2_chars[idx]
                    end

                    local tail_text = trim_text(table.concat(tail_chars))
                    local reconstructed_line_2 = trim_text(normalized_prefix .. table.concat(tail_chars))
                    if reconstructed_line_2 ~= ""
                        and reconstructed_line_2 ~= clean_original_2
                        and validate_basic_correction(clean_original_2, reconstructed_line_2)
                    then
                        return {
                            base_line_1 = clean_original_1,
                            base_line_2_auto_applied = reconstructed_line_2
                        }
                    end
                end
            end
        end

        return nil
    end

    local function analyze_particle_bridge_split_case(original_1, corrected_1, original_2, corrected_2)
        local clean_original_1 = trim_text(original_1)
        local clean_corrected_1 = trim_text(corrected_1)
        local clean_original_2 = trim_text(original_2)
        local clean_corrected_2 = trim_text(corrected_2)

        if clean_original_1 == "" or clean_corrected_1 == "" or clean_original_2 == "" or clean_corrected_2 == "" then
            return nil
        end
        if clean_corrected_1 == clean_original_1 or clean_corrected_2 == clean_original_2 then
            return nil
        end
        if not has_adjacent_boundary_shift_shape(clean_original_1, clean_corrected_1, clean_original_2, clean_corrected_2) then
            return nil
        end
        if not starts_with_literal(clean_corrected_1, clean_original_1) then
            return nil
        end

        local corrected_extension = trim_text(clean_corrected_1:sub(#clean_original_1 + 1))
        local normalized_corrected_extension = normalize_particle_variants(strip_all_spaces(corrected_extension))
        if normalized_corrected_extension == "" then
            return nil
        end

        local original_2_chars = split_text_chars_for_diff_local(clean_original_2)
        local corrected_2_compact = strip_all_spaces(clean_corrected_2)
        for prefix_len = 1, math.max(1, #original_2_chars - 1) do
            local prefix_chars = {}
            for idx = 1, prefix_len do
                prefix_chars[#prefix_chars + 1] = original_2_chars[idx]
            end

            local original_prefix = table.concat(prefix_chars)
            if original_prefix:find("[的地得]") then
                local normalized_prefix = trim_text(normalize_particle_variants(original_prefix))
                local normalized_prefix_compact = strip_all_spaces(normalized_prefix)
                if normalized_prefix_compact ~= "" and starts_with_literal(normalized_corrected_extension, normalized_prefix_compact) then
                    local tail_chars = {}
                    for idx = prefix_len + 1, #original_2_chars do
                        tail_chars[#tail_chars + 1] = original_2_chars[idx]
                    end

                    local pair_suggestion_2 = trim_text(table.concat(tail_chars))
                    local tail_compact = strip_all_spaces(pair_suggestion_2)
                    local auto_applied_line_2 = trim_text(normalized_prefix .. table.concat(tail_chars))
                    if auto_applied_line_2 ~= "" and auto_applied_line_2 ~= clean_original_2 then
                        local corrected_2_matches_tail = corrected_2_compact == tail_compact
                        if not corrected_2_matches_tail and corrected_2_compact ~= "" and tail_compact ~= "" and #corrected_2_compact <= #tail_compact then
                            corrected_2_matches_tail = tail_compact:sub(-#corrected_2_compact) == corrected_2_compact
                        end

                        if corrected_2_matches_tail then
                            local can_apply_as_single = validate_basic_correction(clean_original_2, auto_applied_line_2)
                            if can_apply_as_single then
                                return {
                                    base_line_1 = clean_original_1,
                                    base_line_2_auto_applied = auto_applied_line_2,
                                    pair_suggestion_1 = trim_text(clean_original_1 .. normalized_prefix),
                                    pair_suggestion_2 = pair_suggestion_2,
                                    should_create_pair_pending = false,
                                    suppress_pair_pending = true
                                }
                            end
                        end
                    end
                end
            end
        end

        return nil
    end

    local function analyze_particle_bridge_mixed_case(original_1, corrected_1, original_2, corrected_2)
        local clean_original_1 = trim_text(original_1)
        local clean_corrected_1 = trim_text(corrected_1)
        local clean_original_2 = trim_text(original_2)
        local clean_corrected_2 = trim_text(corrected_2)

        if clean_original_1 == "" or clean_corrected_1 == "" or clean_original_2 == "" or clean_corrected_2 == "" then
            return nil
        end
        if not starts_with_literal(clean_corrected_1, clean_original_1) then
            return nil
        end

        local moved_fragment = trim_text(clean_corrected_1:sub(#clean_original_1 + 1))
        if moved_fragment == "" or count_utf8_chars(moved_fragment) > 3 then
            return nil
        end
        if not moved_fragment:find("[的地得]") then
            return nil
        end

        local normalized_original_2 = normalize_particle_variants(strip_all_spaces(clean_original_2))
        local normalized_fragment = normalize_particle_variants(strip_all_spaces(moved_fragment))
        if normalized_fragment == "" or not starts_with_literal(normalized_original_2, normalized_fragment) then
            return nil
        end

        local recombined_line_2 = moved_fragment .. clean_corrected_2
        local auto_applied_line_2 = trim_text(normalize_particle_variants(recombined_line_2))
        if auto_applied_line_2 == "" or auto_applied_line_2 == clean_original_2 then
            return nil
        end
        local can_apply_as_single = validate_basic_correction(clean_original_2, auto_applied_line_2)
        if not can_apply_as_single then
            return nil
        end

        local normalized_fragment_text = trim_text(normalize_particle_variants(moved_fragment))
        local pair_suggestion_1 = trim_text(clean_original_1 .. normalized_fragment_text)
        local pair_suggestion_2 = clean_corrected_2
        local should_create_pair_pending = strip_all_spaces(pair_suggestion_1) ~= strip_all_spaces(clean_original_1)
            or strip_all_spaces(pair_suggestion_2) ~= strip_all_spaces(auto_applied_line_2)

        return {
            base_line_1 = clean_original_1,
            base_line_2_auto_applied = auto_applied_line_2,
            pair_suggestion_1 = pair_suggestion_1,
            pair_suggestion_2 = pair_suggestion_2,
            should_create_pair_pending = false,
            suppress_pair_pending = should_create_pair_pending
        }
    end

    local function looks_like_particle_bridge_false_positive(original_1, corrected_1, original_2, corrected_2)
        return analyze_particle_bridge_mixed_case(original_1, corrected_1, original_2, corrected_2) ~= nil
    end

    local function validate_adjacent_boundary_pair(original_1, corrected_1, original_2, corrected_2)
        local clean_original_1 = trim_text(original_1)
        local clean_corrected_1 = trim_text(corrected_1)
        local clean_original_2 = trim_text(original_2)
        local clean_corrected_2 = trim_text(corrected_2)
        if clean_original_1 == clean_corrected_1 or clean_original_2 == clean_corrected_2 then
            return nil, nil
        end
        if not has_adjacent_boundary_shift_shape(clean_original_1, clean_corrected_1, clean_original_2, clean_corrected_2) then
            return nil, nil
        end
        if looks_like_particle_bridge_false_positive(clean_original_1, clean_corrected_1, clean_original_2, clean_corrected_2) then
            return nil, nil
        end

        local original_combined = build_pair_combined_text(clean_original_1, clean_original_2)
        local corrected_combined = build_pair_combined_text(clean_corrected_1, clean_corrected_2)
        local normalized_original_combined = strip_all_spaces(original_combined)
        local normalized_corrected_combined = strip_all_spaces(corrected_combined)
        if normalized_original_combined == "" or normalized_corrected_combined == "" then
            return nil, nil
        end

        local balanced, pair_label = has_balanced_pairs(corrected_combined)
        if not balanced then
            return nil, "相邻两行边界修复后括号或引号不平衡：" .. tostring(pair_label)
        end

        local changed_protected_term = find_changed_protected_term(original_combined, corrected_combined)
        if changed_protected_term then
            return nil, "相邻两行边界修复触发高风险词保护：" .. changed_protected_term
        end

        local forbidden_literal_rewrite_reason = find_forbidden_literal_rewrite(original_combined, corrected_combined)
        if forbidden_literal_rewrite_reason then
            return nil, forbidden_literal_rewrite_reason
        end

        local is_style_rewrite, style_reason = is_style_only_rewrite(original_combined, corrected_combined)
        if is_style_rewrite then
            return nil, style_reason
        end

        local original_ascii_tokens = extract_ascii_tokens(original_combined)
        if #original_ascii_tokens > 0 then
            local corrected_ascii_tokens = extract_ascii_tokens(corrected_combined)
            if not ascii_tokens_equal(original_ascii_tokens, corrected_ascii_tokens) then
                local allows_domain_ascii_change = allows_domain_ascii_correction(original_combined, corrected_combined)
                local allows_english_spelling_change = allows_english_spelling_correction(original_combined, corrected_combined)
                if not allows_shortcut_ascii_correction(original_combined, corrected_combined)
                    and not allows_domain_ascii_change
                    and not allows_english_spelling_change then
                    return nil, "相邻两行边界修复改动了英文、数字或快捷键内容"
                end
            end
        end

        local overlap_ratio = compute_char_overlap_ratio(normalized_original_combined, normalized_corrected_combined)
        local combined_original_len = count_utf8_chars(normalized_original_combined)
        local combined_corrected_len = count_utf8_chars(normalized_corrected_combined)
        local combined_length_delta = math.abs(combined_corrected_len - combined_original_len)

        if combined_original_len >= 6 and overlap_ratio >= 0.72 and combined_length_delta <= 2 then
            if normalized_original_combined == normalized_corrected_combined then
                return "pending", "相邻两行边界错位，涉及字数重分配，需人工复核"
            end
            return "pending", string.format("疑似相邻两行边界错位（重合度 %.2f），需人工复核", overlap_ratio)
        end

        return nil, nil
    end

    local MAX_BOUNDARY_REDISTRIBUTION_CHARS = 6

    local function ends_with_literal(text, suffix)
        local source = tostring(text or "")
        local needle = tostring(suffix or "")
        if needle == "" or #needle > #source then
            return false
        end
        return source:sub(-#needle) == needle
    end

    local function extract_compact_boundary_fragment(original_1, corrected_1, original_2, corrected_2)
        local compact_original_1 = strip_all_spaces(trim_text(original_1))
        local compact_corrected_1 = strip_all_spaces(trim_text(corrected_1))
        local compact_original_2 = strip_all_spaces(trim_text(original_2))
        local compact_corrected_2 = strip_all_spaces(trim_text(corrected_2))

        if compact_original_1 == "" or compact_corrected_1 == "" or compact_original_2 == "" or compact_corrected_2 == "" then
            return nil, "边界建议包含空文本"
        end

        if compact_original_1 .. compact_original_2 ~= compact_corrected_1 .. compact_corrected_2 then
            return nil, "边界建议不是纯文本重分配"
        end

        if #compact_corrected_1 > #compact_original_1 and #compact_corrected_2 < #compact_original_2 then
            if not starts_with_literal(compact_corrected_1, compact_original_1) or not ends_with_literal(compact_original_2, compact_corrected_2) then
                return nil, "边界建议不是连续片段尾首重分配"
            end

            local moved_fragment = compact_corrected_1:sub(#compact_original_1 + 1)
            local expected_line_2 = compact_original_2:sub(#moved_fragment + 1)
            if moved_fragment == "" or expected_line_2 ~= compact_corrected_2 then
                return nil, "边界建议不是连续片段尾首重分配"
            end

            return moved_fragment, "forward"
        end

        if #compact_corrected_1 < #compact_original_1 and #compact_corrected_2 > #compact_original_2 then
            if not starts_with_literal(compact_original_1, compact_corrected_1) or not ends_with_literal(compact_corrected_2, compact_original_2) then
                return nil, "边界建议不是连续片段尾首重分配"
            end

            local moved_fragment = compact_corrected_2:sub(1, #compact_corrected_2 - #compact_original_2)
            local expected_line_1 = compact_original_1:sub(1, #compact_original_1 - #moved_fragment)
            if moved_fragment == "" or expected_line_1 ~= compact_corrected_1 then
                return nil, "边界建议不是连续片段尾首重分配"
            end

            return moved_fragment, "backward"
        end

        return nil, "边界建议没有形成有效的相邻行重分配"
    end

    local function validate_pure_boundary_redistribution(original_1, corrected_1, original_2, corrected_2)
        local pair_mode, pair_reason = validate_adjacent_boundary_pair(original_1, corrected_1, original_2, corrected_2)
        if pair_mode ~= "pending" then
            return nil, pair_reason
        end

        local moved_fragment, move_direction = extract_compact_boundary_fragment(original_1, corrected_1, original_2, corrected_2)
        if not moved_fragment then
            return nil, move_direction
        end

        local moved_chars = count_utf8_chars(moved_fragment)
        if moved_chars <= 0 then
            return nil, "边界建议未提取出移动片段"
        end
        if moved_chars > MAX_BOUNDARY_REDISTRIBUTION_CHARS then
            return nil, string.format("边界建议移动片段过长（%d 字）", moved_chars)
        end

        return {
            moved_fragment = moved_fragment,
            move_direction = move_direction,
            base_reason = trim_text(pair_reason)
        }
    end

    local function build_particle_bridge_single_resolution(original_1, original_2, result)
        if not result then
            return nil
        end

        return {
            mode = "single_only",
            base_line_1 = result.base_line_1 or trim_text(original_1),
            base_line_2 = result.base_line_2_auto_applied or trim_text(original_2),
            suppress_pair_pending = result.suppress_pair_pending == true
        }
    end

    local function build_adjacent_pair_pending_candidate(original_1, corrected_1, original_2, corrected_2)
        local redistribution_meta = validate_pure_boundary_redistribution(original_1, corrected_1, original_2, corrected_2)
        if not redistribution_meta then
            return nil
        end

        return {
            suggestion_1 = trim_text(corrected_1),
            suggestion_2 = trim_text(corrected_2),
            reason = redistribution_meta.base_reason or "",
            moved_fragment = redistribution_meta.moved_fragment,
            move_direction = redistribution_meta.move_direction
        }
    end

    local function has_same_adjacent_pair_suggestion(left_candidate, right_candidate)
        if not left_candidate or not right_candidate then
            return false
        end

        return strip_all_spaces(left_candidate.suggestion_1) == strip_all_spaces(right_candidate.suggestion_1)
            and strip_all_spaces(left_candidate.suggestion_2) == strip_all_spaces(right_candidate.suggestion_2)
    end

    local function format_boundary_pair_pending_reason(has_directional_support)
        if has_directional_support then
            return "边界专用模型识别为纯断句重分配，普通纠错结果同向支持，需人工复核"
        end
        return "边界专用模型识别为纯断句重分配，需人工复核"
    end

    local function decide_adjacent_pair_resolution(options)
        local ctx = type(options) == "table" and options or {}
        local original_1 = tostring(ctx.original_1 or "")
        local original_2 = tostring(ctx.original_2 or "")
        local new_text_1 = tostring(ctx.new_text_1 or original_1)
        local new_text_2 = tostring(ctx.new_text_2 or original_2)
        local boundary_text_1 = tostring(ctx.boundary_text_1 or original_1)
        local boundary_text_2 = tostring(ctx.boundary_text_2 or original_2)

        local single_resolution = build_particle_bridge_single_resolution(
            original_1,
            original_2,
            analyze_particle_bridge_priority_case(original_1, boundary_text_1, original_2, boundary_text_2)
        ) or build_particle_bridge_single_resolution(
            original_1,
            original_2,
            analyze_particle_bridge_priority_case(original_1, new_text_1, original_2, new_text_2)
        ) or build_particle_bridge_single_resolution(
            original_1,
            original_2,
            analyze_particle_bridge_split_case(original_1, boundary_text_1, original_2, boundary_text_2)
        ) or build_particle_bridge_single_resolution(
            original_1,
            original_2,
            analyze_particle_bridge_split_case(original_1, new_text_1, original_2, new_text_2)
        ) or build_particle_bridge_single_resolution(
            original_1,
            original_2,
            analyze_particle_bridge_mixed_case(original_1, boundary_text_1, original_2, boundary_text_2)
        ) or build_particle_bridge_single_resolution(
            original_1,
            original_2,
            analyze_particle_bridge_mixed_case(original_1, new_text_1, original_2, new_text_2)
        )

        if single_resolution then
            return single_resolution
        end

        local boundary_pair_candidate = build_adjacent_pair_pending_candidate(original_1, boundary_text_1, original_2, boundary_text_2)
        if not boundary_pair_candidate then
            return {mode = "none"}
        end

        local new_pair_candidate = build_adjacent_pair_pending_candidate(original_1, new_text_1, original_2, new_text_2)
        local has_directional_support = has_same_adjacent_pair_suggestion(boundary_pair_candidate, new_pair_candidate)

        return {
            mode = "pair_pending",
            suggestion_1 = boundary_pair_candidate.suggestion_1,
            suggestion_2 = boundary_pair_candidate.suggestion_2,
            reason = format_boundary_pair_pending_reason(has_directional_support)
        }
    end

    local function apply_fixed_phrase_corrections(original_text, text)
        local original = tostring(original_text or "")
        local corrected = tostring(text or "")

        -- 常见口播开场白的同音误识别兜底
        corrected = corrected:gsub("^好%s*格外好%s*我是", "好 各位好 我是")
        corrected = corrected:gsub("^格外好%s*我是", "各位好 我是")
        corrected = corrected:gsub("^好%s*各位耗%s*我是", "好 各位好 我是")
        corrected = corrected:gsub("^各位耗%s*我是", "各位好 我是")

        -- 高频口语字幕里，“更加地...” 更适合回正为“更加的...”
        if original:find("更加地", 1, true) or corrected:find("更加地", 1, true) then
            corrected = corrected:gsub("更加地", "更加的")
        end

        -- 狭义兜底：状语“进一步”后紧跟动作动词时，优先修正为“进一步地”
        -- 只覆盖当前已确认高频的动作动词，避免把名词短语“一步的调整”类场景误改。
        corrected = corrected:gsub("进一步的(调整)", "进一步地%1")
        corrected = corrected:gsub("进一步的(实现)", "进一步地%1")
        corrected = corrected:gsub("进一步的(模拟)", "进一步地%1")
        corrected = corrected:gsub("进一步的(使用)", "进一步地%1")
        corrected = corrected:gsub("进一步的(操作)", "进一步地%1")
        corrected = corrected:gsub("进一步的(处理)", "进一步地%1")
        corrected = corrected:gsub("进一步的(控制)", "进一步地%1")
        corrected = corrected:gsub("进一步的(优化)", "进一步地%1")
        corrected = corrected:gsub("进一步的(对齐)", "进一步地%1")
        corrected = corrected:gsub("进一步的(衔接)", "进一步地%1")

        -- 程度补语链条兜底：防止“自然的多的多 / 自然得多的多”这类半改半错
        corrected = corrected:gsub("的多的多", "得多得多")
        corrected = corrected:gsub("的多得多", "得多得多")
        corrected = corrected:gsub("得多的多", "得多得多")

        -- 剪辑语境高频误听：这里通常是“复用到视频的开场”，不是“服用/应用到视频的开场”
        corrected = corrected:gsub("服用到视频的开场", "复用到视频的开场")
        corrected = corrected:gsub("应用到视频的开场", "复用到视频的开场")

        -- 参数/节奏语境里，“不同一”通常应回正为“不统一”，不能误改成“不同步”
        if (original:find("参数", 1, true) or original:find("节奏", 1, true) or original:find("节拍", 1, true) or original:find("速度", 1, true))
            and original:find("不同一", 1, true)
            and corrected:find("不同步", 1, true) then
            corrected = corrected:gsub("不同步", "不统一")
        end

        -- 如果模型只是把“的 / 地 / 得”删掉，优先回补成“的”，避免直接丢字
        if count_utf8_chars(original) == count_utf8_chars(corrected) + 1 then
            local particles = {"的", "地", "得"}
            for _, particle in ipairs(particles) do
                local start_pos = 1
                while true do
                    local s = original:find(particle, start_pos, true)
                    if not s then break end
                    local candidate = original:sub(1, s - 1) .. original:sub(s + #particle)
                    if candidate == corrected then
                        corrected = original:sub(1, s - 1) .. "的" .. original:sub(s + #particle)
                        return corrected
                    end
                    start_pos = s + #particle
                end
            end
        end

        return corrected
    end
    
        return {
            parse_ai_line_payload = parse_ai_line_payload,
            validate_basic_correction = validate_basic_correction,
            validate_particle_scope_correction = validate_particle_scope_correction,
            build_particle_priority_single_candidate = build_particle_priority_single_candidate,
            decide_adjacent_pair_resolution = decide_adjacent_pair_resolution,
            apply_fixed_phrase_corrections = apply_fixed_phrase_corrections,
            escape_json = escape_json,
            strip_all_spaces = strip_all_spaces,
            greeting_normalization_block_reason = GREETING_NORMALIZATION_BLOCK_REASON
        }
    end)()

    -- 获取用户选择的任务
    local task_idx = 0
    local itms = win:GetItems()
    if itms and itms.AITaskSelect then
        task_idx = itms.AITaskSelect.CurrentIndex or 0
    end
    local task_type = "full_fix"
    if task_idx == 1 then
        task_type = "particle_fix"
    elseif task_idx == 2 then
        task_type = "zh_to_en"
    elseif task_idx == 3 then
        task_type = "en_to_zh"
    end
    local is_full_correction_task = task_type == "full_fix"
    local is_particle_correction_task = task_type == "particle_fix"
    local is_correction_task = is_full_correction_task or is_particle_correction_task

    local script_context = sanitize_reference_script_text(shared_config.script_content)
    local use_script_context = is_full_correction_task and shared_config.is_script_enabled and script_context ~= ""
    local script_char_count = count_utf8_chars(script_context)
    if use_script_context and script_char_count > REFERENCE_SCRIPT_HARD_LIMIT then
        local err_msg = string.format("参考文稿过长（%d 字），超过 %d 字上限，请精简后重试。", script_char_count, REFERENCE_SCRIPT_HARD_LIMIT)
        print("[Hooper AI 2.0] " .. err_msg)
        LogMsg("[AI] " .. err_msg)
        if status then status:Set("Text", err_msg) end
        return
    end

    local task_name = "完整纠错"
    local sys_prompt = [[你是一个专业的泛用型视频字幕纠错专家。

【前置语境侦测】（最高优先级）
开始纠错前，先通读整批字幕，推断当前视频的主领域与上下文场景。你的纠错必须优先服从该领域的常识、专业术语和操作逻辑。

【核心盾牌：发音比对强制锁（防润色）】
语音识别（ASR）只会“听错”，不会“自己换近义词”。
在修改任何非「的 / 地 / 得」的词汇前，必须先默读原词和修改词的发音。
如果发音明显不同，例如把“作用”改成“施加”或“应用”，把“然后”改成“接着”，这属于近义词润色，绝对禁止修改，立即原样返回。
只有发音相同或极度相近，例如“浮轨”→“副轨”“观念针”→“关键帧”，且当前领域语境下原词明显荒谬时，才允许修改。

【核心原则：最小必要改动】
绝不进行任何润色、重写或顺句。没有把握的词汇一律原样返回。宁可漏改，绝不错改。
绝对禁止为了所谓通顺，替换口语中的逻辑连词或语气词，如把“那”改成“就”、把“然后”改成“接着”。

【“的 / 地 / 得”高压红线规则】
修改“的 / 地 / 得”时必须极其谨慎，绝对不能改出新的语法错误：
1. 动词前用“地”，如“系统地学习”“更细致地涂抹”。
2. 补语前用“得”，如“用得好”“拉得太满”“练得越多”。
3. 名词前用“的”，如“完美的作品”。

【免死金牌（极其重要）】
“的话”“的时候”“的目的”“的的确确”这几个词里的“的”拥有绝对免死金牌，绝对禁止修改为“地话”“得话”等生造词。
例如原句是“用的好的话”，只允许修正前半部分变为“用得好的话”，后面的“的话”绝对不许动。

在修正“的 / 地 / 得”时，只允许替换、补回或删除这三个字本身；若存在极少数固定补语结构，只允许做与该助词直接相邻的最小改动，如“的多”→“得多”“好的很”→“好得很”。
绝对不许为了迁就“的 / 地 / 得”的语法，去修改原句前后的核心动词、名词，或“那 / 就 / 然后”等逻辑连词。
如果不改动前后核心词就读不通，立刻放弃修改，保持原样。

### A. 必须改（满足任一即改）
1. 明显的错别字（尤其是同音、近音误听导致的错字）；但绝对禁止把发音差异明显、只是语义更顺的普通词互换，例如不要把“珍品”改成“精品”。
2. 领域内专业术语的同音/近音误识别，且严格服从上文【发音比对强制锁】。
3. 伪装成生活常用词、但在当前专业语境里明显破坏逻辑的同音错听，如“这一颗”→“这一刻”。
4. 「的 / 地 / 得」三选一明显用错：严格遵守上方红线规则，保护好“的话”“的时候”等固定结构。
5. 开场白/招呼语的严重误听：按最小改动修正，如“好歌舞号 我是Tim”→“好 各位好 我是Tim”。

### B. 一定不改（哪怕你觉得别扭也不改）
1. 口语化表达与承接词，只要不存在高确定性的同音错听，就保持原样，如“那回到…”“这个事情”等。
2. 语气词、感叹词、重复词，如“嗯”“啊”“哈哈”。
3. 英文单词、数字、快捷键默认不改；不允许仅因大小写变化而修改；但允许高确定性的英文拼写错误纠正，以及高确定性的编码格式误听修正。
4. 跨行导致语法不完整、上下文不足以唯一判断的句子，宁可不改，也不要瞎猜；但如果能明确判断只是“上一句尾巴误挂到下一句开头”，允许仅在相邻两行之间做最小必要的尾首重分配。

### C. 绝对禁区（碰了即为错误）
1. 近义词替换：绝对禁止改变原词读音去做同义替换，如严禁把“作用”改成“施加”。
2. 增删任何非错别字的逻辑词：严禁把“那”改成“就”。
3. 改变原句式或顺句：原文语病如果不涉及明确的同音错字，坚决不碰。
4. 生造中文词汇：严禁造出“地话”“得话”等荒谬词组。
5. 严禁大范围合并或拆分字幕行；仅允许在相邻两行之间做最小必要的尾首重分配，用于修复上一句尾巴误挂到下一句开头的 ASR 分段错误。
	
	### D. 输出要求
	1. 收到一批连续字幕，逐行检查，每行都要给出结果（改或不改）。
		2. 严格按照『序号|文本』格式返回所有行。
		3. 不要输出任何解释、JSON、代码块或分析过程。]]
    local particle_fix_sys_prompt = [[你是一个专业的中文视频字幕“的 / 地 / 得”专项检测员。

【唯一任务】
只检查并修正“的 / 地 / 得”的误用、漏字、冗余，以及少量与这三个助词直接相邻的固定补语结构。

【允许修改】
1. “的 / 地 / 得”三字之间的替换，如“系统的学习”→“系统地学习”“完美地作品”→“完美的作品”。
2. 补回明显漏掉的“的 / 地 / 得”，或删除明显多余的“的 / 地 / 得”。
3. 极少数固定结构里与助词直接相关的最小改动，如“用的好的话”→“用得好的话”“的多”→“得多”“好的很”→“好得很”。

【绝对禁止】
1. 禁止修改任何非“的 / 地 / 得”的核心文字、标点、空格、数字、英文、专有名词和语气词。
2. 禁止普通错别字纠正、近义词替换、润色、顺句、扩写、删减或改变原句式。
3. 禁止合并、拆分或重分配字幕行；每一行必须独立判断。
4. “的话”“的时候”“的目的”“的的确确”里的“的”必须保护，禁止改成“地话”“得话”等生造词。
5. 只要不能在不动前后核心词的前提下确定修正，就必须原样返回。

【输出要求】
1. 收到一批连续字幕，逐行检查，每行都要给出结果（改或不改）。
2. 严格按照『序号|文本』格式返回所有行。
3. 不要输出任何解释、JSON、代码块或分析过程。]]
    local boundary_fix_sys_prompt = [[你是一个专业的字幕分段边界修复专家。

你的唯一任务是修复“相邻两行之间的尾首错位”：
1. 只允许在相邻两行之间做最小必要的尾首重分配。
2. 只允许两种操作：
   - 保持原样；
   - 把第二行开头的少量连续文本移到第一行结尾，或把第一行结尾的少量连续文本移到第二行开头。
3. 严禁普通润色、近义词替换、顺句、扩写、删减、改写语气词。
4. 严禁改变行数，严禁新增序号，严禁丢行。
5. 若无法高置信判断，只能原样返回。

示例：
10|带你穿越三国
11|战场还不够沉浸

若实际连续口播更合理的切分是“带你穿越三国战场 / 还不够沉浸”，则应输出：
10|带你穿越三国战场
11|还不够沉浸

严格按照『序号|文本』格式返回所有行，不要输出任何解释、JSON、代码块或分析过程。]]

    if is_particle_correction_task then
        task_name = "的地得专项检测"
        sys_prompt = particle_fix_sys_prompt
    elseif task_type == "zh_to_en" then
        task_name = "中译英"
        -- 中译英
        sys_prompt = [[你是一个专业的影视字幕翻译专家。请将以下中文字幕翻译为地道、简练、符合海外观众阅读习惯的英文字幕。

【翻译规则】：
1. 保持简洁，符合字幕阅读习惯（每行不超过80字符）
2. 专有名词使用通用翻译，如"小潘"翻译为 "Xiao Pan"
3. 中文口语化表达转化为自然英文
4. 中英文之间不加空格
5. 直接返回纯英文翻译，按『序号|英文』格式输出，不要有任何中文或解释]]
    elseif task_type == "en_to_zh" then
        task_name = "英译中"
        -- 英译中
        sys_prompt = [[你是一个专业的影视字幕翻译专家。请将以下英文字幕翻译为流畅、自然、符合中文母语口语习惯的中文字幕。

【翻译规则】：
1. 保持口语化，符合中文说话习惯
2. 英文专有名词可保留英文或意译
3. 俚语和习语翻译为地道中文表达
4. 每行字幕控制在20个中文字符以内
	5. 直接返回纯中文翻译，按『序号|中文』格式输出，不要有任何英文或解释]]
    end

    local base_sys_prompt = sys_prompt
    
    -- 写入系统的临时目录
    local temp_req_file = "/tmp/hooper_req.json"
    local temp_resp_file = "/tmp/hooper_resp.json"

    local function truncate_error_preview(value, max_len)
        local cleaned = trim_text(value or "")
        local limit = math.max(40, tonumber(max_len) or 180)
        if cleaned == "" then
            return ""
        end
        if #cleaned > limit then
            return string.sub(cleaned, 1, limit) .. "..."
        end
        return cleaned
    end

    local function extract_error_message_from_json_value(value, depth)
        local current_depth = tonumber(depth) or 0
        if current_depth > 3 then
            return nil
        end

        if type(value) == "string" then
            local text = trim_text(value)
            return text ~= "" and text or nil
        end

        if type(value) ~= "table" then
            return nil
        end

        local direct_keys = {"message", "msg", "error_msg", "errorMessage", "detail", "details", "code", "type"}
        for _, key in ipairs(direct_keys) do
            local candidate = value[key]
            if type(candidate) == "string" and trim_text(candidate) ~= "" then
                return trim_text(candidate)
            end
        end

        local nested_keys = {"error", "err", "details", "data"}
        for _, key in ipairs(nested_keys) do
            local nested = value[key]
            local nested_msg = extract_error_message_from_json_value(nested, current_depth + 1)
            if nested_msg then
                return nested_msg
            end
        end

        return nil
    end

    local function extract_gemini_text_from_parts(parts)
        if type(parts) ~= "table" then
            return nil
        end

        local text_blocks = {}
        for _, part in ipairs(parts) do
            if type(part) == "table" and type(part.text) == "string" and trim_text(part.text) ~= "" then
                table.insert(text_blocks, part.text)
            end
        end

        if #text_blocks > 0 then
            return table.concat(text_blocks, "\n")
        end
        return nil
    end
    
    local function execute_ai_request(user_content, request_label, request_options)
        local options = type(request_options) == "table" and request_options or {}
        local request_use_script_context = options.use_script_context == true and trim_text(options.script_context) ~= ""
        local request_script_context = request_use_script_context and tostring(options.script_context or "") or ""
        local batch_line_count = math.max(1, tonumber(options.batch_line_count) or 20)
        local final_sys_prompt = tostring(options.sys_prompt_override or base_sys_prompt)
        local final_user_content = tostring(user_content or "")

        if request_use_script_context and request_script_context ~= "" then
            final_sys_prompt = string.format([[【重要背景：录制参考文稿】
---
%s
---

【纠错指令补充】
1. 上方文稿仅作为你核对“专有名词、特定术语、人名型号”的唯一标准字典。
2. 严禁对齐：视频中存在大量即兴发挥，如果 ASR 听写的内容在文稿中没有，说明是主讲人临时加的，必须保留！
3. 严禁删减：绝对不允许按照文稿的简洁度去删减字幕中的口语词（如“然后”、“其实”）。
4. 读音优先：只有当 ASR 听写的词与文稿中某个词“读音高度接近”且“语义更通顺”时，才允许参考文稿修正。
5. 文稿可作为判断相邻两行边界是否错位的辅助上下文，但不能借机把整句强行对齐到稿子。

%s]], request_script_context, final_sys_prompt)
            final_user_content = string.format(
                "请结合上方参考文稿，对以下 %d 行字幕进行纠错；允许仅在相邻两行之间做最小必要的尾首重分配，用于修复上一句尾巴误挂到下一句开头；若文稿与实际口播不一致，优先保留实际口播。\n\n%s",
                batch_line_count,
                final_user_content
            )
        end

        if is_full_correction_task then
            final_user_content = string.format(
                "以下是连续字幕。允许仅在相邻两行之间做最小必要的尾首重分配，用于修复上一句尾巴误挂到下一句开头；除此之外，严禁改动行边界。\n\n%s",
                final_user_content
            )
        elseif is_particle_correction_task then
            final_user_content = string.format(
                "以下是连续字幕。只做“的 / 地 / 得”专项检测；严禁普通错别字纠正、润色或改动行边界。\n\n%s",
                final_user_content
            )
        end

        local escaped_subs = ai_helpers.escape_json(final_user_content)
        local escaped_sys = ai_helpers.escape_json(final_sys_prompt)
        local escaped_model = ai_helpers.escape_json(model)
        local request_temperature = 0.3

        -- 部分新模型（Claude 4+ 系列、OpenAI o-series、GPT-5 等）已弃用 temperature 参数，
        -- 强行发送会被服务端拒绝："`temperature` is deprecated for this model."。
        -- 以下黑名单按 model 名子串匹配跳过 temperature 字段。未来如再出新型号未覆盖，
        -- 用户会看到同样的报错，把对应关键字加进 omit_temperature_keywords 即可。
        local omit_temperature = false
        local lower_model = string.lower(tostring(model or ""))
        local omit_temperature_keywords = {
            "opus-4", "sonnet-4", "haiku-4", "claude-4",
            "opus-5", "sonnet-5", "haiku-5", "claude-5",
            "o1-", "o3-", "o4-",  -- OpenAI o-series（o1, o3, o4 命名前缀）
            "gpt-5",
        }
        for _, kw in ipairs(omit_temperature_keywords) do
            if string.find(lower_model, kw, 1, true) then
                omit_temperature = true
                break
            end
        end

        local json_payload = nil
        local request_url = api_url
        local curl_cmd = nil

        if provider_protocol == "gemini_native" then
            local gemini_model = trim_text(model)
            if not gemini_model:match("^models/") then
                gemini_model = "models/" .. gemini_model
            end
            request_url = normalize_api_url_for_request(api_url) .. "/" .. gemini_model .. ":generateContent"
            local gen_config = omit_temperature
                and '{"maxOutputTokens":8192}'
                or ('{"temperature":' .. tostring(request_temperature) .. ',"maxOutputTokens":8192}')
            json_payload = '{"systemInstruction":{"parts":[{"text":"' .. escaped_sys .. '"}]},"generationConfig":' .. gen_config .. ',"contents":[{"role":"user","parts":[{"text":"' .. escaped_subs .. '"}]}]}'
            curl_cmd = string.format(
                'curl -sS --connect-timeout 10 --max-time 180 -X POST %s -H %s -H %s -d @%s -o %s -w %s 2>&1',
                shell_quote(request_url),
                shell_quote("Content-Type: application/json"),
                shell_quote("x-goog-api-key: " .. api_key),
                shell_quote(temp_req_file),
                shell_quote(temp_resp_file),
                shell_quote("__HTTP_STATUS__:%{http_code}")
            )
        else
            local temp_field = omit_temperature
                and ""
                or ('"temperature": ' .. tostring(request_temperature) .. ', ')
            json_payload = '{"model": "' .. escaped_model .. '", ' .. temp_field .. '"max_tokens": 8192, "messages": [{"role": "system", "content": "' .. escaped_sys .. '"}, {"role": "user", "content": "' .. escaped_subs .. '"}]}'
            curl_cmd = string.format(
                'curl -sS --connect-timeout 10 --max-time 180 -X POST %s -H %s -H %s -d @%s -o %s -w %s 2>&1',
                shell_quote(request_url),
                shell_quote("Content-Type: application/json"),
                shell_quote("Authorization: Bearer " .. api_key),
                shell_quote(temp_req_file),
                shell_quote(temp_resp_file),
                shell_quote("__HTTP_STATUS__:%{http_code}")
            )
        end

        print("[Hooper AI 2.0] JSON payload 长度: " .. #json_payload)
        print("[Hooper AI 2.0] 转义后的字幕(前200字符): " .. string.sub(escaped_subs, 1, 200))
        print("[Hooper AI 2.0] 请求标签: " .. tostring(request_label or "default"))
        print("[Hooper AI 2.0] 请求协议: " .. tostring(provider_protocol))
        print("[Hooper AI 2.0] 请求地址: " .. tostring(request_url))

        local req_file = io.open(temp_req_file, "w")
        if not req_file then
            return nil, nil, "错误：无法创建临时文件"
        end
        req_file:write(json_payload)
        req_file:close()

        local req_debug_f = io.open(temp_req_file, "r")
        if req_debug_f then
            local req_debug = req_debug_f:read("*a")
            req_debug_f:close()
            print("[Hooper AI 2.0] 请求文件内容(前300字符):\n" .. string.sub(req_debug, 1, 300))
        end

        print("[Hooper AI 2.0] 执行 curl 请求 (后台 + 嵌套 RunLoop 等待)...")

        -- ====== B 方案：后台 curl + UI Timer 轮询 + 嵌套 RunLoop ======
        -- 之前是 io.popen + read("*a") 全程阻塞 RunLoop（最长 180 s），
        -- 期间 ⏻ 等任何按钮 click 都派发不进来。现在把 curl 丢到后台，
        -- 主线程进入嵌套 dispatcher:RunLoop()，UI Timer 50 ms 轮询完成 / 取消信号，
        -- ⏻ 处理函数可以正常 fire，set AI_CANCEL_REQUESTED + kill PID 即时返回。
        local curl_output = ""
        do
            local request_was_cancelled = false
            local nested_runloop_failed = false
            local req_uid = tostring(os.time()) .. "_" .. tostring(math.random(100000, 999999))
            local stdout_file = "/tmp/hooper_curl_stdout_" .. req_uid
            local pid_file    = "/tmp/hooper_curl_pid_"    .. req_uid
            local done_file   = "/tmp/hooper_curl_done_"   .. req_uid

            -- 清理可能残留的旧文件
            os.execute(string.format("rm -f %s %s %s 2>/dev/null",
                shell_quote(stdout_file), shell_quote(pid_file), shell_quote(done_file)))

            -- 注意 curl_cmd 末尾已经带 2>&1；外层再加 > stdout_file 把全部合并输出落盘。
            -- 子 shell 形式 ( ... ) & 让 echo $! 拿到的是子 shell 的 PID，
            -- curl 是它直接子进程，pkill -P <子shell PID> 可以连带杀掉 curl。
            local bg_cmd = string.format(
                "(%s > %s; touch %s) & echo $! > %s",
                curl_cmd,
                shell_quote(stdout_file),
                shell_quote(done_file),
                shell_quote(pid_file)
            )
            os.execute(bg_cmd)

            AI_RUNNING = true
            AI_CURL_PID_FILE = pid_file

            local poll_timer_id = "AICurlPollTimer_" .. req_uid
            local poll_timer = ui:Timer({
                ID = poll_timer_id,
                Interval = 50,
                SingleShot = false
            })

            local function stop_and_exit_nested()
                pcall(function() poll_timer:Stop() end)
                -- handler 用完即释放，避免 ui_timer_handlers 表里堆积每次请求的旧 handler
                if ui_timer_handlers then
                    ui_timer_handlers[poll_timer_id] = nil
                end
                if dispatcher and dispatcher.ExitLoop then
                    pcall(function() dispatcher:ExitLoop() end)
                end
            end

            -- Fusion 的 timer 事件通过 disp.On.Timeout 全局路由到 ui_timer_handlers[timer.ID]，
            -- 必须用 register_ui_timer 注册，不能 poll_timer.On.Timeout = ...（timer 对象上没 On 字段）
            register_ui_timer(poll_timer, function()
                if AI_CANCEL_REQUESTED then
                    request_was_cancelled = true
                    -- kill 后台子 shell + 它的 curl 子进程
                    local pf = io.open(pid_file, "r")
                    if pf then
                        local pid = pf:read("*l")
                        pf:close()
                        if pid and trim_text(pid) ~= "" then
                            local clean_pid = trim_text(pid)
                            os.execute(string.format(
                                "pkill -P %s 2>/dev/null; kill -9 %s 2>/dev/null",
                                clean_pid, clean_pid))
                        end
                    end
                    stop_and_exit_nested()
                    return
                end
                local df = io.open(done_file, "r")
                if df then
                    df:close()
                    stop_and_exit_nested()
                end
            end)
            pcall(function() poll_timer:Start() end)

            -- 进入嵌套事件循环；click 事件在这里能正常派发
            local nested_ok, nested_err = pcall(function()
                if dispatcher and dispatcher.RunLoop then
                    dispatcher:RunLoop()
                else
                    error("dispatcher 不支持 RunLoop")
                end
            end)
            if not nested_ok then
                nested_runloop_failed = true
                print("[Hooper AI 2.0] 嵌套 RunLoop 失败，回退为同步轮询: " .. tostring(nested_err))
            end

            pcall(function() poll_timer:Stop() end)
            -- 即使 stop_and_exit_nested 已清过 handler，这里再 nil 一次兜底（pcall 路径可能漏）
            if ui_timer_handlers then
                ui_timer_handlers[poll_timer_id] = nil
            end
            -- 解除全局引用，避免后续 force_quit 误杀已结束请求
            AI_RUNNING = false
            AI_CURL_PID_FILE = nil

            -- 嵌套 RunLoop 跑不通时退化为同步等 sentinel（至少不会爆错）
            if nested_runloop_failed then
                while true do
                    if AI_CANCEL_REQUESTED then
                        request_was_cancelled = true
                        local pf = io.open(pid_file, "r")
                        if pf then
                            local pid = pf:read("*l"); pf:close()
                            if pid and trim_text(pid) ~= "" then
                                local clean_pid = trim_text(pid)
                                os.execute(string.format(
                                    "pkill -P %s 2>/dev/null; kill -9 %s 2>/dev/null",
                                    clean_pid, clean_pid))
                            end
                        end
                        break
                    end
                    local df = io.open(done_file, "r")
                    if df then df:close(); break end
                    os.execute("sleep 0.05")
                end
            end

            -- 用户取消：清理临时文件并以特殊错误返回，让上层 batch 循环识别 cancelled
            if request_was_cancelled or AI_CANCEL_REQUESTED then
                os.execute(string.format("rm -f %s %s %s 2>/dev/null",
                    shell_quote(stdout_file), shell_quote(pid_file), shell_quote(done_file)))
                return nil, nil, "❌ 已取消 AI 请求", "cancelled"
            end

            -- 读取 curl 标准输出（含 __HTTP_STATUS__:%{http_code} 行 + 任何 stderr）
            local of = io.open(stdout_file, "r")
            if of then
                curl_output = of:read("*a") or ""
                of:close()
            end

            -- 清理临时文件
            os.execute(string.format("rm -f %s %s %s 2>/dev/null",
                shell_quote(stdout_file), shell_quote(pid_file), shell_quote(done_file)))
        end

        print("[Hooper AI 2.0] curl 输出: " .. curl_output)
        local http_status = tostring(curl_output and curl_output:match("__HTTP_STATUS__:(%d%d%d)") or "000")
        local curl_error_output = trim_text((tostring(curl_output or "")):gsub("__HTTP_STATUS__:%d%d%d", ""))
        print("[Hooper AI 2.0] HTTP 状态码: " .. tostring(http_status))

        local resp_file = io.open(temp_resp_file, "r")
        if not resp_file then
            return nil, nil, "错误：无法读取 API 响应"
        end
        local resp_content = resp_file:read("*a")
        resp_file:close()

        print("[Hooper AI 2.0] API 响应长度: " .. #resp_content)
        print("[Hooper AI 2.0] API 原始响应内容:\n" .. tostring(resp_content))

        local curl_output_lower = string.lower(tostring(curl_error_output or ""))
        local resp_content_lower = string.lower(tostring(resp_content or ""))
        if curl_output_lower:find("timed out", 1, true)
            or resp_content_lower:find("timed out", 1, true)
            or curl_output_lower:find("operation timeout", 1, true)
            or resp_content_lower:find("operation timeout", 1, true) then
            return nil, nil, "❌ 请求超时，服务器当前可能拥堵，请稍后再试。", "timeout"
        end

        if curl_output_lower:find("could not resolve host", 1, true)
            or resp_content_lower:find("could not resolve host", 1, true) then
            return nil, nil, "❌ 无法解析服务器地址，请检查接口地址或网络连接。"
        end

        if curl_output_lower:find("failed to connect", 1, true)
            or resp_content_lower:find("failed to connect", 1, true)
            or curl_output_lower:find("connection refused", 1, true)
            or resp_content_lower:find("connection refused", 1, true)
            or curl_output_lower:find("connection reset", 1, true)
            or resp_content_lower:find("connection reset", 1, true)
            or curl_output_lower:find("empty reply from server", 1, true)
            or resp_content_lower:find("empty reply from server", 1, true) then
            return nil, nil, "❌ 网络请求失败，服务器当前不可达或连接被中断，请稍后再试。"
        end

        if curl_output_lower:find("curl:", 1, true) or resp_content_lower:find("curl:", 1, true) then
            local raw_error = trim_text(resp_content) ~= "" and trim_text(resp_content) or trim_text(curl_error_output)
            if raw_error ~= "" then
                return nil, nil, "❌ 网络请求失败：" .. raw_error
            end
            return nil, nil, "❌ 网络请求失败，请检查接口地址、API Key 或网络连接。"
        end

        if resp_content_lower:find("<html", 1, true)
            or resp_content_lower:find("<!doctype html", 1, true) then
            return nil, nil, "❌ 接口返回了网页内容而不是 JSON，请检查接口地址是否正确。"
        end

        if resp_content:match("balance is insufficient") or resp_content:match("insufficient_quota") then
            return nil, nil, "❌ 账户余额不足！该模型需要付费，请前往平台充值，或更换为免费模型。"
        end

        local resp_json, resp_err = decode_json_text(resp_content)
        if not resp_json then
            local raw_preview = truncate_error_preview(resp_content, 160)
            if raw_preview ~= "" then
                if http_status ~= "000" then
                    return nil, nil, "❌ API 响应不是合法 JSON（HTTP " .. tostring(http_status) .. "）：" .. raw_preview
                end
                return nil, nil, "❌ API 响应不是合法 JSON：" .. raw_preview
            end
            return nil, nil, "❌ API 响应不是合法 JSON，未执行替换。"
        end

        if type(resp_json) == "string" then
            local plain_error = trim_text(resp_json)
            if plain_error ~= "" then
                if plain_error:lower():find("invalid token", 1, true) then
                    return nil, nil, "❌ Token 无效。当前接口地址和 API Key 可能不匹配。"
                end
                return nil, nil, "❌ API 返回错误：" .. plain_error
            end
        end

        if type(resp_json.error) == "table" or resp_json.error ~= nil then
            local api_error_msg = extract_error_message_from_json_value(resp_json.error)
            if api_error_msg then
                return nil, nil, "❌ API 请求失败：" .. tostring(api_error_msg)
            end
        end

        local extracted_error_message = extract_error_message_from_json_value(resp_json)
        local http_status_num = tonumber(http_status) or 0
        if http_status_num >= 400 then
            local raw_preview = truncate_error_preview(resp_content, 200)
            local fallback_text = extracted_error_message or raw_preview
            if fallback_text ~= "" then
                return nil, nil, "❌ API 请求失败（HTTP " .. tostring(http_status) .. "）：" .. fallback_text
            end
            return nil, nil, "❌ API 请求失败（HTTP " .. tostring(http_status) .. "）"
        end

        local ai_content = nil
        local finish_reason = nil

        if provider_protocol == "gemini_native" then
            local candidates = resp_json.candidates
            if type(candidates) == "table" and type(candidates[1]) == "table" then
                local candidate = candidates[1]
                finish_reason = candidate.finishReason or candidate.finish_reason
                if type(candidate.content) == "table" then
                    ai_content = extract_gemini_text_from_parts(candidate.content.parts)
                end
            end

            if (not ai_content or #ai_content == 0) and type(resp_json.promptFeedback) == "table" then
                local prompt_feedback = resp_json.promptFeedback
                local block_reason = trim_text(prompt_feedback.blockReason or prompt_feedback.block_reason or "")
                if block_reason ~= "" then
                    return nil, finish_reason, "❌ Gemini 请求被拦截：" .. block_reason
                end
            end
        else
            local choices = resp_json.choices
            if type(choices) == "table" and type(choices[1]) == "table" then
                finish_reason = choices[1].finish_reason
                local message = choices[1].message
                if type(message) == "table" then
                    local content = message.content
                    if type(content) == "string" then
                        ai_content = content
                    elseif type(content) == "table" then
                        local blocks = {}
                        for _, block in ipairs(content) do
                            if type(block) == "table" then
                                if type(block.text) == "string" then
                                    table.insert(blocks, block.text)
                                elseif type(block.content) == "string" then
                                    table.insert(blocks, block.content)
                                end
                            end
                        end
                        if #blocks > 0 then
                            ai_content = table.concat(blocks, "\n")
                        end
                    end
                end
            end
        end

        if (not ai_content or #ai_content == 0) and extracted_error_message then
            return nil, finish_reason, "❌ API 请求失败：" .. extracted_error_message
        end

        if not ai_content or #ai_content == 0 then
            return nil, finish_reason, "AI 返回内容为空"
        end

        print("[Hooper AI 2.0] AI 返回结果长度: " .. #ai_content)
        return ai_content, finish_reason, nil
    end

    local ai_content = nil
    local finish_reason = nil
    
    -- 收集对比报告
    local report_entries = {}
    local fix_count = 0
    local pending_count = 0
    local applied_any_change = false
    local mutation_snapshot = prepare_mutation_snapshot(task_name)
    
    if is_full_correction_task then
        local batch_size = 20
        local total_batches = math.max(1, math.ceil(#sorted_list / batch_size))
        local new_subtitle_map = {}
        local boundary_subtitle_map = {}

        for batch_idx = 1, total_batches do
            -- 用户在上一批后按了 ⏻ → 直接退出，不再发起新一批
            if AI_CANCEL_REQUESTED then
                print("[Hooper AI 2.0] 完整纠错被用户取消（第 " .. batch_idx .. " 批前）")
                if status then status:Set("Text", "❌ AI 处理已取消") end
                return
            end

            local batch_start = (batch_idx - 1) * batch_size + 1
            local batch_end = math.min(#sorted_list, batch_start + batch_size - 1)
            local batch_subtitle_list = build_subtitle_list(batch_start, batch_end)
            local request_err = nil
            local request_err_type = nil

            if status then
                status:Set("Text", string.format("正在调用完整纠错...（第 %d/%d 批）", batch_idx, total_batches))
            end

            ai_content, finish_reason, request_err, request_err_type = execute_ai_request(batch_subtitle_list, "fix_batch_" .. tostring(batch_idx), {
                script_context = script_context,
                use_script_context = use_script_context,
                batch_line_count = batch_end - batch_start + 1
            })
            if not ai_content and request_err_type == "timeout" and use_script_context then
                local fallback_msg = "参考文稿过长，已自动切换为无文稿模式重试。"
                print("[Hooper AI 2.0] " .. fallback_msg .. "（第 " .. batch_idx .. "/" .. total_batches .. " 批）")
                LogMsg("[AI] " .. fallback_msg .. "（第 " .. batch_idx .. "/" .. total_batches .. " 批）")
                if status then status:Set("Text", fallback_msg) end
                ai_content, finish_reason, request_err, request_err_type = execute_ai_request(
                    batch_subtitle_list,
                    "fix_batch_" .. tostring(batch_idx) .. "_fallback",
                    {
                        script_context = "",
                        use_script_context = false,
                        batch_line_count = batch_end - batch_start + 1
                    }
                )
            end
            if not ai_content then
                print("[Hooper AI 2.0] 请求失败: " .. tostring(request_err))
                if status then status:Set("Text", tostring(request_err)) end
                return
            end

            if finish_reason == "length" then
                print("[Hooper AI 2.0] AI 输出被截断，未执行替换。")
                if status then status:Set("Text", "❌ AI 输出被截断，请缩小批次或重试。") end
                return
            end

            local batch_subtitle_map, payload_err, missing_indices = ai_helpers.parse_ai_line_payload(ai_content, batch_end - batch_start + 1, batch_start, true)
            if not batch_subtitle_map then
                print("[Hooper AI 2.0] AI 行文本结果校验失败: " .. tostring(payload_err))
                if status then status:Set("Text", "❌ AI 返回结果校验失败，未覆盖字幕。") end
                return
            end

            if missing_indices and #missing_indices > 0 then
                print("[Hooper AI 2.0] AI 漏回 " .. tostring(#missing_indices) .. " 行，已自动保留原文: " .. table.concat(missing_indices, ","))
            end

            for idx, text in pairs(batch_subtitle_map) do
                new_subtitle_map[idx] = text
            end

            local boundary_ai_content, boundary_finish_reason, boundary_request_err = execute_ai_request(
                batch_subtitle_list,
                "boundary_fix_batch_" .. tostring(batch_idx),
                {
                    script_context = script_context,
                    use_script_context = use_script_context,
                    batch_line_count = batch_end - batch_start + 1,
                    sys_prompt_override = boundary_fix_sys_prompt
                }
            )
            if boundary_ai_content then
                if boundary_finish_reason == "length" then
                    print("[Hooper AI 2.0] 相邻两行边界修复输出被截断，本批次忽略边界修复结果。")
                else
                    local batch_boundary_map, boundary_payload_err = ai_helpers.parse_ai_line_payload(boundary_ai_content, batch_end - batch_start + 1, batch_start, true)
                    if batch_boundary_map then
                        for idx, text in pairs(batch_boundary_map) do
                            boundary_subtitle_map[idx] = text
                        end
                    else
                        print("[Hooper AI 2.0] 相邻两行边界修复结果校验失败，本批次忽略： " .. tostring(boundary_payload_err))
                    end
                end
            elseif boundary_request_err then
                print("[Hooper AI 2.0] 相邻两行边界修复请求失败，本批次忽略： " .. tostring(boundary_request_err))
            end
        end

        local i = 1
        while i <= #sorted_list do
            local data = sorted_list[i]
            local old_text = data.text or ""
            local new_text = ai_helpers.apply_fixed_phrase_corrections(old_text, new_subtitle_map[i] or old_text)
            local boundary_text = ai_helpers.apply_fixed_phrase_corrections(old_text, boundary_subtitle_map[i] or old_text)
            local particle_priority_new_text = ai_helpers.build_particle_priority_single_candidate(old_text, new_text)
            if particle_priority_new_text then
                new_text = particle_priority_new_text
            end
            local particle_priority_boundary_text = ai_helpers.build_particle_priority_single_candidate(old_text, boundary_text)
            if particle_priority_boundary_text then
                boundary_text = particle_priority_boundary_text
            end
            local clean_old_text = trim_text(old_text)
            local clean_new_text = trim_text(new_text)

            local pair_handled = false
            if i < #sorted_list then
                local next_data = sorted_list[i + 1]
                local old_text_2 = next_data.text or ""
                local new_text_2 = ai_helpers.apply_fixed_phrase_corrections(old_text_2, new_subtitle_map[i + 1] or old_text_2)
                local boundary_text_2 = ai_helpers.apply_fixed_phrase_corrections(old_text_2, boundary_subtitle_map[i + 1] or old_text_2)
                local particle_priority_new_text_2 = ai_helpers.build_particle_priority_single_candidate(old_text_2, new_text_2)
                if particle_priority_new_text_2 then
                    new_text_2 = particle_priority_new_text_2
                end
                local particle_priority_boundary_text_2 = ai_helpers.build_particle_priority_single_candidate(old_text_2, boundary_text_2)
                if particle_priority_boundary_text_2 then
                    boundary_text_2 = particle_priority_boundary_text_2
                end
                local pair_resolution = ai_helpers.decide_adjacent_pair_resolution({
                    original_1 = old_text,
                    original_2 = old_text_2,
                    new_text_1 = new_text,
                    new_text_2 = new_text_2,
                    boundary_text_1 = boundary_text,
                    boundary_text_2 = boundary_text_2
                })

                if pair_resolution.mode == "single_only" then
                    local base_line_1 = pair_resolution.base_line_1 or trim_text(old_text)
                    local base_line_2 = pair_resolution.base_line_2 or trim_text(old_text_2)
                    if report_helpers.append_basic_report_entry(
                        report_entries,
                        tonumber(data.index) or i,
                        old_text,
                        base_line_1,
                        { normalize_fn = ai_helpers.strip_all_spaces, updated_label = "修正", status = "已自动应用", row_id = data.id }
                    ) then
                        fix_count = fix_count + 1
                        applied_any_change = true
                    end
                    if report_helpers.append_basic_report_entry(
                        report_entries,
                        tonumber(next_data.index) or (i + 1),
                        old_text_2,
                        base_line_2,
                        { normalize_fn = ai_helpers.strip_all_spaces, updated_label = "修正", status = "已自动应用", row_id = next_data.id }
                    ) then
                        fix_count = fix_count + 1
                        applied_any_change = true
                    end

                    data.text = base_line_1
                    next_data.text = base_line_2
                    new_subtitle_map[i] = base_line_1
                    new_subtitle_map[i + 1] = base_line_2
                    boundary_subtitle_map[i] = base_line_1
                    boundary_subtitle_map[i + 1] = base_line_2
                    pair_handled = true
                    i = i + 2
                elseif pair_resolution.mode == "pair_pending" then
                    local pending_change = build_pending_pair_change(
                        data,
                        next_data,
                        i,
                        i + 1,
                        old_text,
                        old_text_2,
                        pair_resolution.suggestion_1,
                        pair_resolution.suggestion_2,
                        pair_resolution.reason
                    )
                    pending_count = pending_count + 1
                    PendingChanges[#PendingChanges + 1] = pending_change
                    pending_change_by_key[get_pending_change_key(pending_change)] = pending_change
                    data.text = old_text
                    next_data.text = old_text_2
                    pair_handled = true
                    i = i + 2
                end
            end

            if not pair_handled then
                if clean_old_text ~= clean_new_text then
                    local can_apply, block_reason = ai_helpers.validate_basic_correction(old_text, new_text)
                    if can_apply then
                        if report_helpers.append_basic_report_entry(
                            report_entries,
                            i,
                            old_text,
                            new_text,
                            { updated_label = "修正", status = "已自动应用", row_id = data.id }
                        ) then
                            fix_count = fix_count + 1
                            applied_any_change = true
                        end
                    else
                        if block_reason ~= ai_helpers.greeting_normalization_block_reason then
                            local pending_change = build_pending_change(data, i, old_text, new_text, block_reason)
                            pending_count = pending_count + 1
                            PendingChanges[#PendingChanges + 1] = pending_change
                            pending_change_by_key[get_pending_change_key(pending_change)] = pending_change
                        else
                            print(string.format("[Hooper AI 2.0] 忽略第 %d 行疑似开场白归一化建议：%s -> %s", i, old_text, new_text))
                        end
                        new_text = old_text
                    end
                else
                    new_text = old_text
                end

                data.text = new_text
                i = i + 1
            end
        end
    elseif is_particle_correction_task then
        local batch_size = 20
        local total_batches = math.max(1, math.ceil(#sorted_list / batch_size))
        local new_subtitle_map = {}

        for batch_idx = 1, total_batches do
            -- 用户在上一批后按了 ⏻ → 直接退出，不再发起新一批
            if AI_CANCEL_REQUESTED then
                print("[Hooper AI 2.0] 的地得专项检测被用户取消（第 " .. batch_idx .. " 批前）")
                if status then status:Set("Text", "❌ AI 处理已取消") end
                return
            end

            local batch_start = (batch_idx - 1) * batch_size + 1
            local batch_end = math.min(#sorted_list, batch_start + batch_size - 1)
            local batch_subtitle_list = build_subtitle_list(batch_start, batch_end)
            local request_err = nil

            if status then
                status:Set("Text", string.format("正在调用的地得专项检测...（第 %d/%d 批）", batch_idx, total_batches))
            end

            ai_content, finish_reason, request_err = execute_ai_request(batch_subtitle_list, "particle_fix_batch_" .. tostring(batch_idx), {
                script_context = "",
                use_script_context = false,
                batch_line_count = batch_end - batch_start + 1
            })
            if not ai_content then
                print("[Hooper AI 2.0] 请求失败: " .. tostring(request_err))
                if status then status:Set("Text", tostring(request_err)) end
                return
            end

            if finish_reason == "length" then
                print("[Hooper AI 2.0] 的地得专项检测输出被截断，未执行替换。")
                if status then status:Set("Text", "❌ AI 输出被截断，请缩小批次或重试。") end
                return
            end

            local batch_subtitle_map, payload_err, missing_indices = ai_helpers.parse_ai_line_payload(ai_content, batch_end - batch_start + 1, batch_start, true)
            if not batch_subtitle_map then
                print("[Hooper AI 2.0] AI 行文本结果校验失败: " .. tostring(payload_err))
                if status then status:Set("Text", "❌ AI 返回结果校验失败，未覆盖字幕。") end
                return
            end

            if missing_indices and #missing_indices > 0 then
                print("[Hooper AI 2.0] AI 漏回 " .. tostring(#missing_indices) .. " 行，已自动保留原文: " .. table.concat(missing_indices, ","))
            end

            for idx, text in pairs(batch_subtitle_map) do
                new_subtitle_map[idx] = text
            end
        end

        for i, data in ipairs(sorted_list) do
            local old_text = data.text or ""
            local new_text = new_subtitle_map[i] or old_text
            local clean_old_text = trim_text(old_text)
            local clean_new_text = trim_text(new_text)

            if clean_old_text ~= clean_new_text then
                local can_apply, block_reason = ai_helpers.validate_particle_scope_correction(old_text, new_text)
                if can_apply then
                    if report_helpers.append_basic_report_entry(
                        report_entries,
                        tonumber(data.index) or i,
                        old_text,
                        new_text,
                        { updated_label = "修正", status = "已自动应用", reason = "的地得专项检测", row_id = data.id }
                    ) then
                        fix_count = fix_count + 1
                        applied_any_change = true
                    end
                    data.text = new_text
                else
                    local pending_change = build_pending_change(data, i, old_text, new_text, block_reason)
                    pending_count = pending_count + 1
                    PendingChanges[#PendingChanges + 1] = pending_change
                    pending_change_by_key[get_pending_change_key(pending_change)] = pending_change
                    data.text = old_text
                end
            else
                data.text = old_text
            end
        end
    else
        local request_err = nil
        ai_content, finish_reason, request_err = execute_ai_request(subtitle_list, task_name)
        if not ai_content then
            print("[Hooper AI 2.0] 请求失败: " .. tostring(request_err))
            if status then status:Set("Text", tostring(request_err)) end
            return
        end

        -- 翻译/其他单次任务一次性发整批字幕（常见 400+ 行），AI 偶尔会漏回几行（finish_reason=stop 但行数对不上）。
        -- 启用 allow_missing_keep_original=true：漏回的行用原文兜底，不再因为漏几行就把整批结果作废。
        local new_subtitle_map, payload_err, missing_indices = ai_helpers.parse_ai_line_payload(ai_content, #sorted_list, 1, true)
        if not new_subtitle_map then
            print("[Hooper AI 2.0] AI 行文本结果校验失败: " .. tostring(payload_err))
            if status then status:Set("Text", "❌ AI 返回结果校验失败，未覆盖字幕。") end
            return
        end

        if missing_indices and #missing_indices > 0 then
            local preview = table.concat(missing_indices, ",", 1, math.min(20, #missing_indices))
            if #missing_indices > 20 then preview = preview .. ",..." end
            print(string.format("[Hooper AI 2.0] %s：AI 漏回 %d 行，已自动保留原文（index: %s）",
                tostring(task_name or "AI 任务"), #missing_indices, preview))
            LogMsg(string.format("[AI] AI 漏回 %d 行，已自动保留原文", #missing_indices))
        end

        for i, data in ipairs(sorted_list) do
            local old_text = data.text or ""
            local new_text = new_subtitle_map[i] or old_text
            local clean_old_text = trim_text(old_text)
            local clean_new_text = trim_text(new_text)

            if clean_old_text ~= clean_new_text then
                fix_count = fix_count + 1
                applied_any_change = true
                table.insert(report_entries, report_helpers.build_report_entry(
                    "translated",
                    i,
                    old_text,
                    new_text,
                    {
                        updated_label = "结果",
                        status = "已更新"
                    }
                ))
            else
                new_text = old_text
            end

            data.text = new_text
        end
    end
    
    print("[Hooper AI 2.0] 自动应用 " .. fix_count .. " 条，待复核 " .. pending_count .. " 条")

    if applied_any_change then
        rebuild_tree_from_rows(sorted_list, win)
        commit_mutation_snapshot(mutation_snapshot)
    end

    local status_text = ""
    if is_correction_task then
        if fix_count == 0 and pending_count == 0 then
            status_text = task_name .. "完成，未发现可自动应用的明显错误"
        elseif fix_count == 0 then
            status_text = task_name .. "完成，0 条自动应用，" .. pending_count .. " 条建议待复核"
        elseif pending_count == 0 then
            status_text = task_name .. "完成，自动应用 " .. fix_count .. " 条"
        else
            status_text = task_name .. "完成，自动应用 " .. fix_count .. " 条，另有 " .. pending_count .. " 条待复核"
        end
    else
        status_text = task_name .. "完成，更新了 " .. fix_count .. " 条"
    end

    print("[Hooper AI 2.0] " .. status_text)
    if status then status:Set("Text", status_text) end
    LogMsg("[AI] " .. status_text)
    
    -- 弹出纠错报告窗口
    if is_correction_task then
        show_ai_fix_report_window(task_name, fix_count, pending_count, report_entries)
    else
        report_helpers.show_standard_ai_result_report(task_name, fix_count, report_entries)
    end
end

-- ========== 导出 SRT ==========
local function export_srt()
    local status = win:Find("StatusLabel")
    local export_rows = collect_exportable_subtitles()
    local valid_subs = {}

    for i, sub in ipairs(export_rows) do
        local normalized = normalize_export_subtitle(sub, i)
        if normalized then
            table.insert(valid_subs, normalized)
        end
    end

    if #valid_subs == 0 then
        if status then status:Set("Text", "❌ 没有可导出的字幕") end
        return false
    end

    if current_backup_path == "" then
        if status then status:Set("Text", "❌ 备份目录为空") end
        return false
    end

    os.execute('mkdir -p "' .. current_backup_path .. '" 2>/dev/null')
    os.execute('mkdir "' .. current_backup_path .. '" 2>nul')

    local sep = (current_backup_path:sub(-1) == "\\" or current_backup_path:sub(-1) == "/") and "" or "/"
    local file_name = "HooperAI_导出_" .. os.date("%m%d_%H%M%S") .. ".srt"
    local save_path = current_backup_path .. sep .. file_name
    local file = io.open(save_path, "w")
    if not file then
        if status then status:Set("Text", "❌ 导出SRT失败") end
        return false
    end

    for i, sub in ipairs(valid_subs) do
        file:write(i .. "\n")
        file:write(sub.Start .. " --> " .. sub.End .. "\n")
        file:write(sub.Text .. "\n\n")
    end
    file:close()

    if status then status:Set("Text", "✅ 已导出SRT") end
    return true
end

local WINDOW_META = {
    tab_switch_old_code_removed = true,
    orphaned_tabs_removed = true,
    mini_window_id = "HooperAI_v2_minimal",
    mini_window_title = "找个字幕",
    main_window_id = "HooperAI_v2_compact_narrow500_final_h960",
    main_window_title = "改个字幕",
}

local mini_content = ui:VGroup({
    Weight = 1,
    ID = "MiniRoot",
    ContentsMargins = {10, 8, 10, 8},
    Spacing = 4,

    ui:HGroup({
        ID = "MiniTopBar",
        Weight = 0,
        MinimumSize = {0, 34},
        Spacing = 4,
        ui:Label({ID = "MiniTrackLabel", Text = "字幕轨", Weight = 0}),
        ui:HGroup({
            ID = "MiniTrackSpinWrap",
            Weight = 0,
            Spacing = 4,
            MinimumSize = {64, 32},
            ui:LineEdit({
                ID = "MiniTrackSpin",
                Text = "1",
                Weight = 1,
                MinimumSize = {40, 32},
                Alignment = {AlignHCenter = true, AlignVCenter = true}
            }),
            ui:VGroup({
                ID = "MiniTrackStepGroup",
                Weight = 0,
                Spacing = 0,
                MinimumSize = {20, 32},
                ui:Button({ID = "MiniTrackSpinUp", Text = "▲", Weight = 1, MinimumSize = {20, 16}}),
                ui:Button({ID = "MiniTrackSpinDown", Text = "▼", Weight = 1, MinimumSize = {20, 16}})
            })
        }),
        ui:Button({ID = "MiniRefreshBtn", Text = "刷新字幕", Weight = 0}),
        ui:Button({ID = "MiniOpenFullBtn", Text = "打开完整版", Weight = 0, MinimumSize = {120, 28}}),
        ui:Label({
            ID = "MiniLoadStatusLabel",
            Text = "⚠️ 请先刷新字幕",
            Weight = 1,
            MinimumSize = {60, 20},
            Alignment = {AlignLeft = true, AlignVCenter = true}
        })
    }),

        ui:HGroup({
            ID = "MiniSearchRow",
            Weight = 0,
            Spacing = 0,
            MinimumSize = {0, 38},
            MaximumSize = {16777215, 38},
            ui:VGroup({
                Weight = 1,
                MinimumSize = {0, 38},
                MaximumSize = {16777215, 38},
                ContentsMargins = {0, 3, 0, 3},
                Spacing = 0,
                ui:LineEdit({
                    ID = "MiniSearchBox",
                    PlaceholderText = "搜索字幕内容（双击列表行可跳转定位）",
                    Weight = 0,
                    MinimumSize = {0, 32},
                    MaximumSize = {16777215, 32}
                })
            })
        }),

    ui:VGap(6),

    ui:Stack({
        ID = "MiniSubtitleAreaStack",
        Weight = 1,
        CurrentIndex = 0,
        ui:VGroup({
            ID = "MiniSubtitlePlaceholder",
            Weight = 1,
            Spacing = 8,
            ui:VGap(0, 1),
            ui:Label({
                ID = "MiniSubtitlePlaceholderLabel",
                Text = "正在自动加载字幕…",
                Weight = 0,
                Alignment = {AlignHCenter = true, AlignVCenter = true},
                WordWrap = true
            }),
            ui:VGap(0, 1)
        }),
        ui:HGroup({
            ID = "MiniSubtitleTreeWrap",
            Weight = 1,
            Spacing = 0,
            ui:Tree({
                ID = "MiniSubtitleTree",
                Weight = 1,
                Header = {Text = "字幕预览  ·  双击可跳转"},
                Events = { ItemDoubleClicked = true }
            })
        })
    })
})

local main_content = ui:VGroup({
    Weight = 1,
    ID = "MainRoot",
    ContentsMargins = 8,
    Spacing = 5,
    
    -- 1. 顶部工具区
    ui:VGroup({
        ID = "TopArea",
        Weight = 0,
        Spacing = 5,
        ui:HGroup({
            ID = "TopBar",
            Weight = 0,
            Spacing = 10,
            ui:Label({ID = "TrackLabel", Text = "字幕轨", Weight = 0}),
            ui:HGroup({
                ID = "TrackSpinWrap",
                Weight = 0,
                Spacing = 4,
                MinimumSize = {64, 32},
                ui:LineEdit({
                    ID = "TrackSpin",
                    Text = "1",
                    Weight = 1,
                    MinimumSize = {40, 32},
                    Alignment = {AlignHCenter = true, AlignVCenter = true}
                }),
                ui:VGroup({
                    ID = "TrackStepGroup",
                    Weight = 0,
                    Spacing = 0,
                    MinimumSize = {20, 32},
                    ui:Button({ID = "TrackSpinUp", Text = "▲", Weight = 1, MinimumSize = {20, 16}}),
                    ui:Button({ID = "TrackSpinDown", Text = "▼", Weight = 1, MinimumSize = {20, 16}})
                })
            }),
            ui:Button({ID = "RefreshBtn", Text = "刷新字幕", Weight = 0}),
            -- 加载状态标签已删除（底部「已加载 N 条」更准确）。HGap 保留右侧空间。
            ui:HGap(0, 1.0),
            ui:Button({
                ID = "ForceQuitBtn",
                Text = "⏻",
                Weight = 0,
                MinimumSize = {44, 40},
                MaximumSize = {44, 40},
                ToolTip = "强制退出 SubFix（关闭所有 SubFix 窗口，不可撤销）"
            })
        }),
        ui:HGroup({
            ID = "SearchRow",
            Weight = 0,
            Spacing = 5,
            MinimumSize = {0, 38},
            MaximumSize = {16777215, 38},
            ui:VGroup({
                Weight = 1,
                MinimumSize = {0, 38},
                MaximumSize = {16777215, 38},
                ContentsMargins = {0, 4, 0, 4},
                Spacing = 0,
                ui:LineEdit({
                    ID = "SearchBox",
                    PlaceholderText = "搜索字幕内容（双击列表行可跳转定位）",
                    Weight = 0,
                    MinimumSize = {0, 30},
                    MaximumSize = {16777215, 30}
                })
            })
        }),
        ui:VGap(0)
    }),
    
    -- 3. 选项卡面板
    ui:HGroup({
        Weight = 0,
        Spacing = 0,
        ui:TabBar({
            ID = "MainTabs",
            Weight = 1
        })
    }),
    
    -- 4. 面板堆叠区
    ui:Stack({
            ID = "TabStack",
            Weight = 0,
            
            -- 面板 A：精修工具
            ui:VGroup({
                ID = "ToolTabPage",
                Weight = 0,
                Spacing = 4,
                ui:HGroup({
                    Weight = 0,
                    Spacing = 8,
                    ui:Button({ID = "BtnStep1", Text = "修改英文格式", Weight = 1}),
                    ui:Button({ID = "BtnStep2", Text = "中文数字互转", Weight = 1})
                }),
                ui:HGroup({
                    Weight = 0,
                    Spacing = 8,
                    ui:Button({ID = "BtnStep3", Text = "消除字幕空隙", Weight = 1}),
                    ui:Button({ID = "BtnStep4", Text = "中英间加空格", Weight = 1})
                }),
                ui:HGroup({
                    Weight = 0,
                    Spacing = 5,
                    ui:Label({Text = "查找:", Weight = 0, MinimumSize = {35, 24}, Alignment = {AlignRight = true, AlignVCenter = true}}),
                    ui:LineEdit({ID = "FindInput", PlaceholderText = "例如：错别字", Weight = 1}),
                    ui:Label({Text = "替换为:", Weight = 0, MinimumSize = {45, 24}, Alignment = {AlignRight = true, AlignVCenter = true}}),
                    ui:LineEdit({ID = "ReplaceInput", Weight = 1}),
                    ui:Button({ID = "BatchReplaceBtn", Text = "执行批量替换", Weight = 0, MinimumSize = {112, 28}})
                })
            }),
            
            -- 面板 B：AI 工作台
            ui:VGroup({
                ID = "AITabPage",
                Weight = 0,
                Spacing = 3,
                ui:HGroup({
                    Weight = 0,
                    Spacing = 5,
                    ui:Label({Text = "任务:", Weight = 0, MinimumSize = {35, 24}, Alignment = {AlignRight = true, AlignVCenter = true}}),
                    ui:ComboBox({ID = "AITaskSelect", Weight = 1})
                }),
                ui:HGroup({
                    Weight = 0,
                    Spacing = 5,
                    ui:Label({Text = "引擎:", Weight = 0, MinimumSize = {35, 24}, Alignment = {AlignRight = true, AlignVCenter = true}}),
                    ui:ComboBox({ID = "PresetCombo", Weight = 1})
                }),
                ui:HGroup({
                    Weight = 0,
                    Spacing = 5,
                    MinimumSize = {0, 34},
                    ui:Button({ID = "ConfigBtn", Text = "⚙️ 配置", Weight = 1, MinimumSize = {0, 28}}),
                    ui:Button({ID = "AIFixBtn", Text = "开始 AI 处理", Weight = 1, MinimumSize = {0, 28}})
                }),
                ui:VGap(4)
            })
        }),
    
    -- 5. 核心字幕列表区
    ui:HGroup({
        Weight = 1,
        Spacing = 0,
        ui:Tree({
            ID = "SubtitleTree",
            Weight = 1,
            Header = {Text = "字幕预览  ·  双击可跳转"},
            Events = { ItemDoubleClicked = true }
        })
    }),
    
    -- 6. 底部操作栏
    ui:VGroup({
        ID = "BottomBar",
        Weight = 0,
        ContentsMargins = {0, 6, 0, 6},
        Spacing = 6,
        ui:HGroup({
            ID = "BackupRow",
            Weight = 0,
            Spacing = 5,
            ui:Label({Text = "备份", Weight = 0}),
            ui:Button({ID = "BackupFolderBtn", Text = "📁", Weight = 0}),
            ui:ComboBox({ID = "BackupPathInput", Weight = 1})
        }),
        ui:HGroup({
            ID = "BackupActionRow",
            Weight = 0,
            Spacing = 8,
            MinimumSize = {0, 34},
            ui:Button({ID = "UndoBtn", Text = "撤回", Weight = 1, MinimumSize = {0, 30}}),
            ui:Button({ID = "CleanBtn", Text = "清空", Weight = 1, MinimumSize = {0, 30}})
        }),
        ui:VGap(4),
        ui:HGroup({
            ID = "TargetTrackRow",
            Weight = 0,
            MinimumSize = {0, 36},
            Spacing = 8,
            ui:HGroup({
                ID = "TargetTrackControlGroup",
                Weight = 0,
                MinimumSize = {0, 36},
                Spacing = 6,
                ui:Label({
                    Text = "更新到轨",
                    Weight = 0,
                    Alignment = {AlignLeft = true, AlignVCenter = true}
                }),
                ui:HGroup({
                    ID = "TargetTrackSpinWrap",
                    Weight = 0,
                    Spacing = 8,
                    MinimumSize = {80, 36},
                    ui:LineEdit({
                        ID = "TargetTrackSpin",
                        Text = "1",
                        Weight = 1,
                        MinimumSize = {48, 36},
                        Alignment = {AlignHCenter = true, AlignVCenter = true}
                    }),
                    ui:VGroup({
                        ID = "TargetTrackStepGroup",
                        Weight = 0,
                        Spacing = 0,
                        MinimumSize = {24, 36},
                        ui:Button({ID = "TargetTrackSpinUp", Text = "▲", Weight = 1, MinimumSize = {24, 18}}),
                        ui:Button({ID = "TargetTrackSpinDown", Text = "▼", Weight = 1, MinimumSize = {24, 18}})
                    })
                })
            }),
            ui:Button({ID = "UpdateBtn", Text = "📝 更新时间线", Weight = 1, MinimumSize = {0, 36}})
        }),
        ui:VGap(2),
        ui:Label({
            ID = "StatusLabel",
            Text = "准备就绪",
            Weight = 0,
            MinimumSize = {0, 18},
            Alignment = {AlignLeft = true, AlignVCenter = true}
        })
    })
})

local function create_mini_window()
    return dispatcher:AddWindow({
        ID = WINDOW_META.mini_window_id,
        WindowTitle = WINDOW_META.mini_window_title,
        Geometry = {500, 120, 435, 382}
    }, mini_content)
end

local function create_full_window()
    return dispatcher:AddWindow({
        ID = WINDOW_META.main_window_id,
        WindowTitle = WINDOW_META.main_window_title,
        Geometry = {500, 120, 500, 700}
    }, main_content)
end

-- 创建窗口
mini_win = create_mini_window()
win = create_full_window()

ensure_ai_config_window = function()
    if AIConfigPopWin then
        return AIConfigPopWin
    end

    AIConfigPopWin = dispatcher:AddWindow({
        ID = "AIConfigPopWin",
        WindowTitle = "AI 配置",
        Geometry = {320, 180, 520, 420}
    },
    ui:VGroup{
        ContentsMargins = 10,
        Spacing = 8,
        ui:HGroup{
            Weight = 0,
            Spacing = 6,
            ui:Label{ID = "AIConfigHintLabel", Text = "当前配置会在点击完成或开始 AI 处理时自动保存。", Weight = 1}
        },
        ui:HGroup{
            Weight = 0,
            Spacing = 6,
            ui:Label{Text = "模型", Weight = 0, MinimumSize = {48, 24}},
            ui:LineEdit{ID = "ModelInput", Weight = 1}
        },
        ui:HGroup{
            Weight = 0,
            Spacing = 6,
            ui:Label{Text = "API", Weight = 0, MinimumSize = {48, 24}},
            ui:LineEdit{ID = "ApiUrlInput", Weight = 1}
        },
        ui:HGroup{
            Weight = 0,
            Spacing = 6,
            ui:Label{Text = "Key", Weight = 0, MinimumSize = {48, 24}},
            ui:LineEdit{ID = "ApiKeyInput", PlaceholderText = "sk-...", Weight = 1}
        },
        ui:HGroup{
            Weight = 0,
            Spacing = 6,
            ui:CheckBox{ID = "EnableScriptAssistCheckbox", Text = "启用文稿辅助纠错", Checked = false, Weight = 1}
        },
        ui:VGroup{
            Weight = 1,
            Spacing = 4,
            ui:HGroup{
                Weight = 0,
                Spacing = 8,
                ui:Label{Text = "参考文稿（可选）", Weight = 0},
                ui:Label{ID = "ReferenceScriptRiskLabel", Text = "<font color='#00AA55'>当前字数：0 · 影响较小</font>", Weight = 1, Alignment = {AlignLeft = true, AlignVCenter = true}}
            },
            ui:TextEdit{ID = "ReferenceScriptInput", Text = "", Weight = 1, MinimumSize = {0, 180}},
        },
        ui:HGroup{
            Weight = 0,
            Spacing = 6,
            ui:HGap(0, 1),
            ui:Button{ID = "CloseAIConfigBtn", Text = "完成", Weight = 0, MinimumSize = {300, 28}},
            ui:HGap(0, 1)
        }
    })

    function AIConfigPopWin.On.AIConfigPopWin.Close(ev)
        ai_config_popup_visible = false
        save_ai_popup_config_state()
        pcall(function() win.Enabled = true end)
        AIConfigPopWin:Hide()
    end

    function AIConfigPopWin.On.CloseAIConfigBtn.Clicked(ev)
        ai_config_popup_visible = false
        save_ai_popup_config_state()
        pcall(function() win.Enabled = true end)
        AIConfigPopWin:Hide()
    end

    function AIConfigPopWin.On.ReferenceScriptInput.TextChanged(ev)
        update_reference_script_risk_label(ev and ev.Text or nil)
    end

    return AIConfigPopWin
end

function get_selected_backup_entry()
    local combo = win and win:Find("BackupPathInput")
    if not combo then
        return nil
    end
    local idx = tonumber(combo.CurrentIndex) or -1
    if idx <= 0 then
        return nil
    end
    return BackupHistoryEntries[idx]
end

function sync_backup_selector(preferred_display_name, options)
    local combo = win and win:Find("BackupPathInput")
    if not combo then
        return false
    end
    repopulate_backup_combo(combo, preferred_display_name, options)
    backup_selector_dirty = false
    return true
end

save_ai_popup_config_state = function()
    SaveProviderConfig(current_ai_provider_id, read_provider_config_from_ui(current_ai_provider_id))
    SaveActiveProviderId(current_ai_provider_id)
    return save_shared_config_from_ui()
end

startup_refresh_timer = ui:Timer({
    ID = "StartupRefreshTimer",
    -- 这里只需要让出一帧给 Fusion 把占位符画出来，10 ms 就够了；
    -- 之前 80 ms 是凭感觉给的余量，实测会让"正在自动加载"那段总时长多 80 ms。
    Interval = 10,
    SingleShot = true
})

full_window_deferred_sync_timer = ui:Timer({
    ID = "FullWindowDeferredSyncTimer",
    Interval = 60,
    SingleShot = true
})

-- 完整版字幕树空闲预热：极简版加载完字幕后稍等片刻，
-- 在用户还在看极简版的间隙把完整版字幕树先铺好，
-- 这样真正切换时不会再卡住主线程渲染 480 行。
-- 50 ms：初始加载完 mini 立刻显示「已加载」，再过 ~50 ms 让 RunLoop 回到空闲，
-- 此时同步渲染完整版。50 ms 内用户若已点切换，open_full_window 的 dirty 检查兜底。
-- 之前是 250 ms（保守的"等用户看完 mini 再静默渲染"），现在初始加载和编辑后共用此值。
full_window_warmup_timer = ui:Timer({
    ID = "FullWindowWarmupTimer",
    Interval = 50,
    SingleShot = true
})

-- 搜索防抖：连续输入或退格时只在停顿后真正重渲染一次
search_debounce_timer = ui:Timer({
    ID = "SearchDebounceTimer",
    Interval = 120,
    SingleShot = true
})

-- 全局变量（避免主 chunk local 数量再次逼近 Lua 5.1 的 200 上限）
pending_search_window = nil

register_ui_timer(startup_refresh_timer, function()
    if not mini_win then
        return
    end

    set_mini_subtitle_area_state(mini_win, false, "正在自动加载字幕…")
    update_shared_status(mini_win, "正在自动加载字幕...")
    set_load_status_label(false, "<font color='#FA8C16'>⏳ 正在自动加载</font>", mini_win)
    refresh_subtitles(mini_win, {skip_backup = true, show_loading_placeholder = true})
    print("[Hooper AI 2.0] 极简版窗口已显示，并已尝试自动加载字幕。")
end)

register_ui_timer(full_window_deferred_sync_timer, function()
    if not win then
        return
    end
    ensure_backup_selector_fresh(nil, { preserve_current_selection = false, default_index = 0 })
    apply_lightweight_shared_state_to_window(win)
end)

register_ui_timer(full_window_warmup_timer, function()
    -- 仅在完整版窗口还没渲染、用户还没切过去的情况下做预热
    if not win then return end
    if active_window == win then return end
    if not full_window_tree_dirty then return end
    if not current_rows or #current_rows == 0 then return end

    local context = SEARCH_VIEW.build_current_view_context()
    if not (context and context.visible_rows) then return end

    local started_at = os.clock()
    render_rows_to_window(win, context.visible_rows)
    full_window_tree_dirty = false
    print(string.format("[Hooper AI 2.0] 完整版字幕树空闲预热: %d ms (后台静默)",
        math.floor(((os.clock() - started_at) * 1000) + 0.5)))
end)

register_ui_timer(search_debounce_timer, function()
    local target_window = pending_search_window or active_window or mini_win or win
    pending_search_window = nil
    if target_window then
        do_search(target_window)
    end
end)

function schedule_debounced_search(target_window)
    pending_search_window = target_window or active_window or mini_win or win
    restart_ui_timer(search_debounce_timer)
end

local function open_full_window()
    update_search_query_from_window(mini_win)
    get_row_from_tree_selection(mini_win)

    if not full_window_ai_controls_initialized then
        local preset_combo = win and win:Find("PresetCombo")
        if preset_combo then
            provider_combo_bootstrap_in_progress = true
            for _, provider_def in ipairs(AI_PROVIDER_DEFS) do
                preset_combo:AddItem(provider_def.label)
            end
        end

        local full_items = win and win:GetItems()
        if full_items and full_items.AITaskSelect then
            full_items.AITaskSelect:AddItem("1. 🧠 完整纠错")
            full_items.AITaskSelect:AddItem("2. 🔎 的地得专项检测")
            full_items.AITaskSelect:AddItem("3. 🇨🇳 翻译：中 -> 英")
            full_items.AITaskSelect:AddItem("4. 🇺🇸 翻译：英 -> 中")
        end

        full_window_ai_controls_initialized = true
        sync_provider_combo_selection(current_ai_provider_id)
        provider_combo_bootstrap_in_progress = false
    else
        sync_provider_combo_selection(current_ai_provider_id)
    end

    active_window = win
    pcall(function()
        if win.SetAttrs then
            win:SetAttrs({Geometry = {500, 120, 500, 700}})
        end
    end)

    local switch_started_at = os.clock()

    -- 如果空闲预热定时器还没来得及跑（用户切得很快），先取消它避免之后做无用功
    if full_window_warmup_timer then
        pcall(function() full_window_warmup_timer:Stop() end)
    end

    -- 如果字幕树有变更（撤回/重做/AI 操作等）且预热没赶上，在切换时同步渲染
    if full_window_tree_dirty then
        local render_start = os.clock()
        local context = SEARCH_VIEW.build_current_view_context()
        if context and context.visible_rows then
            render_rows_to_window(win, context.visible_rows)
        end
        full_window_tree_dirty = false
        print(string.format("[Hooper AI 2.0] 切换到完整版-同步渲染字幕树: %d ms",
            math.floor(((os.clock() - render_start) * 1000) + 0.5)))
    end

    local show_start = os.clock()
    apply_lightweight_shared_state_to_window(win)
    win:Show()
    if mini_win then
        mini_win:Hide()
    end
    print(string.format("[Hooper AI 2.0] 切换到完整版-显示窗口: %d ms",
        math.floor(((os.clock() - show_start) * 1000) + 0.5)))
    print(string.format("[Hooper AI 2.0] 切换到完整版-总耗时: %d ms",
        math.floor(((os.clock() - switch_started_at) * 1000) + 0.5)))

    restart_ui_timer(full_window_deferred_sync_timer)
end

-- ========== 事件绑定 ==========

-- Tab 切换由 MainTabs.CurrentChanged 统一控制

mini_win.On[WINDOW_META.mini_window_id].Close = function(ev)
    handle_main_window_close()
end

function mini_win.On.MiniTrackSpin.TextChanged(ev)
    if suppress_track_change_events then return end
    local text = trim(ev.Text or "")
    if text == "" then
        return
    end
    if not text:match("^%d+$") then
        sync_track_control(mini_win)
        return
    end
    current_track = math.max(1, math.min(10, math.floor(tonumber(text) or current_track or 1)))
    print("[Hooper AI 2.0] 极简版轨道切换: " .. current_track)
    refresh_subtitles(mini_win)
end

function mini_win.On.MiniTrackSpinUp.Clicked(ev)
    current_track = math.max(1, math.min(10, (current_track or 1) + 1))
    sync_track_control(mini_win)
    print("[Hooper AI 2.0] 极简版轨道切换: " .. current_track)
    refresh_subtitles(mini_win)
end

function mini_win.On.MiniTrackSpinDown.Clicked(ev)
    current_track = math.max(1, math.min(10, (current_track or 1) - 1))
    sync_track_control(mini_win)
    print("[Hooper AI 2.0] 极简版轨道切换: " .. current_track)
    refresh_subtitles(mini_win)
end

function mini_win.On.MiniRefreshBtn.Clicked(ev)
    print("[Hooper AI 2.0] 极简版刷新按钮点击")
    update_shared_status(mini_win, "正在刷新...")
    set_load_status_label(false, "<font color='#FA8C16'>⏳ 正在刷新字幕...</font>", mini_win)
    refresh_subtitles(mini_win)
end

function mini_win.On.MiniSearchBox.TextChanged(ev)
    if suppress_search_change_events then return end
    -- 通过防抖：连续按键/退格时只在停顿后做一次重渲染，避免中间态多次重建树
    schedule_debounced_search(mini_win)
end

function mini_win.On.MiniOpenFullBtn.Clicked(ev)
    open_full_window()
end

function mini_win.On.MiniSubtitleTree.ItemDoubleClicked(ev)
    print("[Hooper AI 2.0] 极简版字幕列表双击")
    local tree = mini_win:Find("MiniSubtitleTree")
    local item = get_tree_event_value(ev, {"item", "Item", "currentItem", "CurrentItem", "node", "Node"})
    if tree and item then
        set_tree_current_item(tree, item)
    end
    go_to_subtitle(mini_win)
end

-- 轨道选择变化（LineEdit + ▲▼，与极简窗口、目标轨控件统一样式）
function win.On.TrackSpin.TextChanged(ev)
    if suppress_track_change_events then return end
    local text = trim(ev.Text or "")
    if text == "" then
        return
    end
    if not text:match("^%d+$") then
        sync_track_control(win)
        return
    end
    current_track = math.max(1, math.min(10, math.floor(tonumber(text) or current_track or 1)))
    print("[Hooper AI 2.0] 轨道切换: " .. current_track)
    refresh_subtitles(win)
end

function win.On.TrackSpinUp.Clicked(ev)
    current_track = math.max(1, math.min(10, (current_track or 1) + 1))
    sync_track_control(win)
    print("[Hooper AI 2.0] 轨道切换: " .. current_track)
    refresh_subtitles(win)
end

function win.On.TrackSpinDown.Clicked(ev)
    current_track = math.max(1, math.min(10, (current_track or 1) - 1))
    sync_track_control(win)
    print("[Hooper AI 2.0] 轨道切换: " .. current_track)
    refresh_subtitles(win)
end

function win.On.TargetTrackSpin.TextChanged(ev)
    local text = trim(ev.Text or "")
    if text == "" then
        return
    end
    if not text:match("^%d+$") then
        sync_target_track_control()
        return
    end
    set_target_track_value(text, true)
end

function win.On.TargetTrackSpinUp.Clicked(ev)
    set_target_track_value((current_subtitle_target_track or 1) + 1, true)
end

function win.On.TargetTrackSpinDown.Clicked(ev)
    set_target_track_value((current_subtitle_target_track or 1) - 1, true)
end

-- 刷新按钮
function win.On.RefreshBtn.Clicked(ev)
    print("[Hooper AI 2.0] 刷新按钮点击")
    update_shared_status(win, "正在刷新...")
    set_load_status_label(false, "<font color='#FA8C16'>⏳ 正在刷新字幕...</font>", win)
    refresh_subtitles(win)
end

-- 搜索框回车
function win.On.SearchBox.TextChanged(ev)
    if suppress_search_change_events then return end
    -- 同样走防抖，主窗口列表行数也很多时同样受益
    schedule_debounced_search(win)
end

-- 批量替换按钮
function win.On.BatchReplaceBtn.Clicked(ev)
    do_replace()
end

function win.On.ConfigBtn.Clicked(ev)
    local config_window = ensure_ai_config_window()
    if config_window then
        update_reference_script_risk_label()
        ai_config_popup_visible = true
        apply_provider_config_to_ui(current_ai_provider_id, LoadConfig(current_ai_provider_id))
        apply_shared_config_to_ui(LoadSharedConfig())
        pcall(function() win.Enabled = false end)
        config_window:Show()
    end
end

-- ========== 八步流水线 ==========

-- 1️⃣ 修改英文格式（复用原 [5] 英文大小写能力）
function win.On.BtnStep1.Clicked(ev)
    print("[Hooper AI 2.0] [1] 修改英文格式")
    if win and win.On and win.On.BtnStep5 and win.On.BtnStep5.Clicked then
        return win.On.BtnStep5.Clicked(ev)
    end
    local status = win:Find("StatusLabel")
    if status then status:Set("Text", "英文格式功能不可用") end
end

-- 2️⃣ 中阿数字智能互转（单按钮双向切换）
function win.On.BtnStep2.Clicked(ev)
    -- 修复：优先使用 current_rows
    if not current_rows or #current_rows == 0 then
        local status = win:Find("StatusLabel")
        if status then status:Set("Text", "没有字幕数据") end
        return
    end

    -- 1. 初始化 Toggle 状态 (默认为 中转阿)
    if not win.NumToggleState then win.NumToggleState = "to_arabic" end

    -- 2. 核心映射与解析
    local map_c2a = {
        ["零"]="0", ["一"]="1", ["二"]="2", ["两"]="2", ["三"]="3", ["四"]="4",
        ["五"]="5", ["六"]="6", ["七"]="7", ["八"]="8", ["九"]="9"
    }
    local map_a2c = {
        ["0"]="零", ["1"]="一", ["2"]="二", ["3"]="三", ["4"]="四",
        ["5"]="五", ["6"]="六", ["7"]="七", ["8"]="八", ["9"]="九"
    }
    local digit_value = {
        ["零"]=0, ["一"]=1, ["二"]=2, ["两"]=2, ["三"]=3, ["四"]=4,
        ["五"]=5, ["六"]=6, ["七"]=7, ["八"]=8, ["九"]=9
    }
    local small_unit = {["十"]=10, ["百"]=100, ["千"]=1000}
    local big_unit = {["万"]=10000, ["亿"]=100000000}
    local cn_num_chars = {
        ["零"]=true, ["一"]=true, ["二"]=true, ["两"]=true, ["三"]=true, ["四"]=true,
        ["五"]=true, ["六"]=true, ["七"]=true, ["八"]=true, ["九"]=true,
        ["十"]=true, ["百"]=true, ["千"]=true, ["万"]=true, ["亿"]=true
    }
    local utf8_pat = "[%z\1-\127\194-\244][\128-\191]*"

    local function split_utf8_chars(s)
        local chars = {}
        for ch in tostring(s or ""):gmatch(utf8_pat) do
            chars[#chars + 1] = ch
        end
        return chars
    end

    local function join_chars(chars, i, j)
        local out = {}
        for idx = i, j do
            out[#out + 1] = chars[idx]
        end
        return table.concat(out)
    end

    local function is_all_digit_chars(token)
        local chars = split_utf8_chars(token)
        if #chars == 0 then return false end
        for _, ch in ipairs(chars) do
            if digit_value[ch] == nil then return false end
        end
        return true
    end

    local function has_unit_chars(token)
        for ch in tostring(token):gmatch(utf8_pat) do
            if small_unit[ch] or big_unit[ch] then
                return true
            end
        end
        return false
    end

    local function is_digit_char(ch)
        return digit_value[ch] ~= nil
    end

    local digit_connectors = {
        [" "] = true, ["　"] = true, ["-"] = true, ["—"] = true, ["–"] = true,
        ["~"] = true, ["～"] = true, [","] = true, ["，"] = true,
        ["、"] = true, ["."] = true, ["。"] = true, ["·"] = true,
        ["…"] = true, ["/"] = true
    }

    local function is_countdown_digit(chars, idx)
        local ch = chars[idx]
        if not is_digit_char(ch) then return false end

        local prev = chars[idx - 1]
        local nextc = chars[idx + 1]
        local prev2 = chars[idx - 2]
        local next2 = chars[idx + 2]

        if is_digit_char(prev) or is_digit_char(nextc) then
            return true
        end
        if digit_connectors[prev] and is_digit_char(prev2) then
            return true
        end
        if digit_connectors[nextc] and is_digit_char(next2) then
            return true
        end
        return false
    end

    local function chinese_to_arabic(token)
        token = tostring(token or "")
        if token == "" then return nil end

        -- 纯数字读法，例如 二零二六 -> 2026
        if is_all_digit_chars(token) then
            local out = {}
            for ch in token:gmatch(utf8_pat) do
                out[#out + 1] = tostring(digit_value[ch])
            end
            return table.concat(out)
        end

        local total, section, number = 0, 0, 0
        local valid = false
        for ch in token:gmatch(utf8_pat) do
            if digit_value[ch] ~= nil then
                number = digit_value[ch]
                valid = true
            elseif small_unit[ch] then
                local unit = small_unit[ch]
                if number == 0 then number = 1 end
                section = section + number * unit
                number = 0
                valid = true
            elseif big_unit[ch] then
                local unit = big_unit[ch]
                section = section + number
                if section == 0 then section = 1 end
                total = total + section * unit
                section = 0
                number = 0
                valid = true
            else
                return nil
            end
        end
        if not valid then return nil end
        return tostring(total + section + number)
    end

    local function section_to_chinese(num)
        local digits = {"零","一","二","三","四","五","六","七","八","九"}
        local units = {"", "十", "百", "千"}
        local out = {}
        local zero_pending = false
        local pos = 1
        while num > 0 do
            local d = num % 10
            if d == 0 then
                zero_pending = (#out > 0)
            else
                if zero_pending then
                    table.insert(out, 1, "零")
                    zero_pending = false
                end
                table.insert(out, 1, digits[d + 1] .. units[pos])
            end
            num = math.floor(num / 10)
            pos = pos + 1
        end
        local result = table.concat(out)
        result = result:gsub("^一十", "十")
        return result
    end

    local function arabic_to_chinese(numstr)
        numstr = tostring(numstr or "")
        if numstr == "" then return numstr end
        if numstr:find("^0%d+$") then
            return (numstr:gsub("%d", map_a2c))
        end
        local num = tonumber(numstr)
        if not num then
            return (numstr:gsub("%d", map_a2c))
        end
        if num == 0 then return "零" end

        local section_units = {"", "万", "亿"}
        local parts = {}
        local unit_index = 1
        local need_zero = false

        while num > 0 do
            local section = num % 10000
            if section == 0 then
                need_zero = (#parts > 0)
            else
                local section_text = section_to_chinese(section) .. section_units[unit_index]
                if need_zero then
                    table.insert(parts, 1, "零")
                    need_zero = false
                end
                table.insert(parts, 1, section_text)
                if section < 1000 and num >= 10000 then
                    need_zero = true
                end
            end
            num = math.floor(num / 10000)
            unit_index = unit_index + 1
        end

        local result = table.concat(parts)
        result = result:gsub("零+", "零")
        result = result:gsub("零万", "万")
        result = result:gsub("零亿", "亿")
        result = result:gsub("亿万", "亿")
        result = result:gsub("零$", "")
        result = result:gsub("^一十", "十")
        return result
    end

    local function replace_cn_numbers(text)
        local chars = split_utf8_chars(text)
        local out = {}
        local i = 1
        while i <= #chars do
            if i + 2 <= #chars and chars[i] == "百" and chars[i + 1] == "分" and chars[i + 2] == "之" then
                local j = i + 3
                while j <= #chars and cn_num_chars[chars[j]] do
                    j = j + 1
                end
                if j > i + 3 then
                    local token = join_chars(chars, i + 3, j - 1)
                    local num = chinese_to_arabic(token)
                    if num then
                        out[#out + 1] = num .. "%"
                        i = j
                    else
                        out[#out + 1] = chars[i]
                        i = i + 1
                    end
                else
                    out[#out + 1] = chars[i]
                    i = i + 1
                end
            elseif cn_num_chars[chars[i]] then
                local j = i
                while j <= #chars and cn_num_chars[chars[j]] do
                    j = j + 1
                end
                local token = join_chars(chars, i, j - 1)
                local token_len = #split_utf8_chars(token)
                local converted = nil
                if has_unit_chars(token) or token_len >= 2 then
                    converted = chinese_to_arabic(token)
                elseif is_countdown_digit(chars, i) then
                    converted = tostring(digit_value[chars[i]])
                end
                out[#out + 1] = converted or token
                i = j
            else
                out[#out + 1] = chars[i]
                i = i + 1
            end
        end
        local result = table.concat(out)
        result = result:gsub("摄氏度", "℃")
        return result
    end

    local function replace_arabic_numbers(text)
        local result = tostring(text or "")
        result = result:gsub("(%d+)%%", function(numstr)
            return "百分之" .. arabic_to_chinese(numstr)
        end)
        result = result:gsub("%d+", function(numstr)
            return arabic_to_chinese(numstr)
        end)
        return result
    end

    local function transform_text(text, direction)
        local src = tostring(text or "")
        if direction == "to_arabic" then
            return replace_cn_numbers(src)
        end
        return replace_arabic_numbers(src)
    end

    local function count_changes(direction)
        local count = 0
        -- 修复：优先使用 current_rows
        if current_rows and #current_rows > 0 then
            for _, sub in ipairs(current_rows) do
                local txt = (sub and sub.text) or ""
                if transform_text(txt, direction) ~= txt then
                    count = count + 1
                end
            end
        else
            for _, sub in pairs(subtitle_data_map) do
                local txt = (sub and sub.text) or ""
                if transform_text(txt, direction) ~= txt then
                    count = count + 1
                end
            end
        end
        return count
    end

    local chosen_direction = win.NumToggleState
    local chosen_count = count_changes(chosen_direction)
    if chosen_count == 0 then
        local alternate_direction = (chosen_direction == "to_arabic") and "to_chinese" or "to_arabic"
        local alternate_count = count_changes(alternate_direction)
        if alternate_count > 0 then
            chosen_direction = alternate_direction
            chosen_count = alternate_count
        end
    end

    local modify_count = 0
    local dirty_row_ids = {}
    local report_entries = {}
    local current_action = (chosen_direction == "to_arabic") and "转阿拉伯数字" or "转中文数字"
    local action_label = "中阿数字互转_" .. current_action
    local mutation_snapshot = prepare_mutation_snapshot(action_label)

    -- 3. 遍历并替换 - 修复：优先使用 current_rows，并更新 subtitle_data_map 中的对应项
    if current_rows and #current_rows > 0 then
        for i, sub in ipairs(current_rows) do
            local txt = (sub and sub.text) or ""
            local new_txt = transform_text(txt, chosen_direction)

            if new_txt ~= txt then
                sub.text = new_txt
                modify_count = modify_count + 1
                table.insert(report_entries, report_helpers.format_batch_change_report_line(sub.index or i, txt, new_txt, {row_id = sub.id}))

                local display_text = build_tree_display_text(sub.index or i, sub.timecode or "", nil, new_txt)
                sub.display_text = display_text
                mark_dirty_row(dirty_row_ids, sub)
            end
        end
        if modify_count > 0 then
            sync_current_preview_tree(win, dirty_row_ids)
        end
    else
        local update_entries = {}
        for node, sub in pairs(subtitle_data_map) do
            local txt = (sub and sub.text) or ""
            local new_txt = transform_text(txt, chosen_direction)

            if new_txt ~= txt then
                sub.text = new_txt
                modify_count = modify_count + 1
                table.insert(report_entries, report_helpers.format_batch_change_report_line(sub.index, txt, new_txt, {row_id = sub.id}))

                local display_text = build_tree_display_text(sub.index, sub.timecode or "", nil, new_txt)
                sub.display_text = display_text
                queue_tree_node_text_update(update_entries, node, display_text)
            end
        end
        apply_tree_node_text_updates(win, win:Find("SubtitleTree"), update_entries)
    end

    -- 4. 翻转状态
    if modify_count > 0 then
        commit_mutation_snapshot(mutation_snapshot)
        win.NumToggleState = (chosen_direction == "to_arabic") and "to_chinese" or "to_arabic"
    end
    local next_direction = win.NumToggleState or "to_arabic"
    local next_action = (next_direction == "to_arabic") and "转阿拉伯数字" or "转中文数字"

    -- 5. UI 与反馈
    local status = win:Find("StatusLabel")
    local msg
    if modify_count > 0 then
        msg = "[Hooper AI 2.0] 🔄 执行: " .. current_action .. " | 修改了 " .. modify_count .. " 条。下次将执行: " .. next_action
    else
        msg = "[Hooper AI 2.0] 🔄 执行: " .. current_action .. " | 修改了 0 条。未发现可转换数字。下次将执行: " .. next_action
    end
    if status then status:Set("Text", msg) end
    print(msg)
    if modify_count > 0 then
        report_helpers.show_batch_result_report(current_action, report_entries, modify_count)
    end
end

-- 3️⃣ 字幕无缝吸附（填补空隙）
function win.On.BtnStep3.Clicked(ev)
    print("[Hooper AI 2.0] 3️⃣ 字幕无缝吸附")
    if not current_rows or #current_rows == 0 then
        local status = win:Find("StatusLabel")
        if status then status:Set("Text", "没有字幕数据") end
        return
    end

    local fps = tonumber(current_fps) or 24.0
    local gap_threshold = math.max(1, math.floor(fps * 2 + 0.5))
    local mutation_snapshot = prepare_mutation_snapshot("消除字幕空隙")

    sort_rows_by_timing(current_rows)

    local count = 0
    local total = #current_rows
    local report_entries = {}

    for i = 1, total - 1 do
        local curr = current_rows[i]
        local nxt = current_rows[i + 1]
        if curr and nxt then
            local curr_end = tonumber(curr.end_frame)
            local nxt_start = tonumber(nxt.start_frame)
            local gap = nil

            if curr_end and nxt_start then
                gap = nxt_start - curr_end
            end

            if gap and gap > 0 and gap <= gap_threshold then
                local original_end = curr.end_frame
                curr.end_frame = nxt_start
                count = count + 1
                -- 记录可还原条目：原文/修改后用「原始字幕文本」+「填补 N 帧空隙」作展示
                local entry = report_helpers.format_batch_change_report_line(
                    curr.index or i,
                    tostring(curr.text or ""),
                    string.format("[填补 %d 帧空隙]  %s", gap, tostring(curr.text or "")),
                    {
                        row_id = curr.id,
                        revert_kind = "end_frame",
                        original_end_frame = original_end,
                        updated_end_frame = nxt_start,
                    }
                )
                table.insert(report_entries, entry)
            end
        end
    end

    if count > 0 then
        commit_mutation_snapshot(mutation_snapshot)
        rebuild_tree_from_rows(current_rows, win)
    end

    local status = win:Find("StatusLabel")
    local msg = string.format("[Hooper AI 2.0] 🧲 成功填补了 %d 处字幕空隙！", count)
    if status then status:Set("Text", msg) end
    print(msg)
    LogMsg(string.format("[3] 消除字幕空隙完成，填补了 %d 处", count))
    -- 「消除字幕空隙」按用户要求不弹修改报告窗口（其他精修工具保留弹窗）；
    -- 状态栏已显示填补处数，撤回逻辑通过 UndoBtn 走 mutation_snapshot 即可。
end

-- 4️⃣ 中英加空格（盘古之白）
function win.On.BtnStep4.Clicked(ev)
    print("[Hooper AI 2.0] 4️⃣ 中英加空格")
    -- 修复：优先使用 current_rows
    if not current_rows or #current_rows == 0 then
        local status = win:Find("StatusLabel")
        if status then status:Set("Text", "没有字幕数据") end
        return
    end
    local mutation_snapshot = prepare_mutation_snapshot("中英加空格")
    local count = 0
    local dirty_row_ids = {}
    local report_entries = {}
    if current_rows and #current_rows > 0 then
        for i, data in ipairs(current_rows) do
            if data and data.text then
                local old = data.text
                local t = old
                t = t:gsub("([a-zA-Z0-9])([\xC0-\xFF][\x80-\xBF]*)", "%1 %2")
                t = t:gsub("([\xC0-\xFF][\x80-\xBF]*)([a-zA-Z0-9])", "%1 %2")
                if t ~= old then
                    data.text = t
                    count = count + 1
                    table.insert(report_entries, report_helpers.format_batch_change_report_line(data.index or i, old, t, {row_id = data.id}))
                    data.display_text = build_tree_display_text(data.index or i, data.timecode or "", nil, t)
                    mark_dirty_row(dirty_row_ids, data)
                end
            end
        end
        if count > 0 then
            sync_current_preview_tree(win, dirty_row_ids)
        end
    else
        local update_entries = {}
        for node, data in pairs(subtitle_data_map) do
            if data and data.text then
                local old = data.text
                local t = old
                t = t:gsub("([a-zA-Z0-9])([\xC0-\xFF][\x80-\xBF]*)", "%1 %2")
                t = t:gsub("([\xC0-\xFF][\x80-\xBF]*)([a-zA-Z0-9])", "%1 %2")
                if t ~= old then
                    data.text = t
                    count = count + 1
                    table.insert(report_entries, report_helpers.format_batch_change_report_line(data.index, old, t, {row_id = data.id}))
                    local display_text = build_tree_display_text(data.index, data.timecode or "", nil, t)
                    data.display_text = display_text
                    queue_tree_node_text_update(update_entries, node, display_text)
                end
            end
        end
        apply_tree_node_text_updates(win, win:Find("SubtitleTree"), update_entries)
    end
    if count > 0 then
        commit_mutation_snapshot(mutation_snapshot)
    end
    local status = win:Find("StatusLabel")
    if status then status:Set("Text", "中英加空格完成，修改了 " .. count .. " 条") end
    print("[Hooper AI 2.0] 步骤 4 完成：中英文排版间距已优化。")
    LogMsg("[4] 中英加空格完成，修改了 " .. count .. " 条")
    if count > 0 then report_helpers.show_batch_result_report("中英加空格", report_entries, count) end
end

-- 5️⃣ 英文大写
-- [5] 英文大小写 (动态 UID 防弹窗控件注册冲突)
function win.On.BtnStep5.Clicked(ev)
    print("[Hooper AI 2.0] [5] 英文大小写")
    -- 修复：优先使用 current_rows
    if not current_rows or #current_rows == 0 then
        local status = win:Find("StatusLabel")
        if status then status:Set("Text", "没有字幕数据") end
        return
    end

    local uid = tostring(os.time()) .. tostring(math.random(1000, 9999))
    local dlg = disp:AddWindow({
        ID = "CaseDlg_" .. uid,
        WindowTitle = "选择排版模式",
        Geometry = {400, 300, 250, 150},
        ui:VGroup {
            Spacing = 5, Weight = 1,
            ui:Button { ID = "BtnUpper_" .. uid, Text = "全部大写 (MACBOOK)" },
            ui:Button { ID = "BtnLower_" .. uid, Text = "全部小写 (macbook)" },
            ui:Button { ID = "BtnTitle_" .. uid, Text = "首字母大写 (Macbook)" },
        }
    })

    local upper_key = "BtnUpper_" .. uid
    local lower_key = "BtnLower_" .. uid
    local title_key = "BtnTitle_" .. uid

    dlg.On[upper_key].Clicked = function()
        local mutation_snapshot = prepare_mutation_snapshot("英文全大写")
        local count = 0
        local dirty_row_ids = {}
        local report_entries = {}
        if current_rows and #current_rows > 0 then
            for i, data in ipairs(current_rows) do
                if data and data.text then
                    local old = data.text
                    local t = old:gsub("%a+", string.upper)
                    if t ~= old then
                        data.text = t
                        count = count + 1
                        table.insert(report_entries, report_helpers.format_batch_change_report_line(data.index or i, old, t, {row_id = data.id}))
                        data.display_text = build_tree_display_text(data.index or i, data.timecode or "", nil, t)
                        mark_dirty_row(dirty_row_ids, data)
                    end
                end
            end
            if count > 0 then
                sync_current_preview_tree(win, dirty_row_ids)
            end
        else
            local update_entries = {}
            for node, data in pairs(subtitle_data_map) do
                if data and data.text then
                    local old = data.text
                    local t = old:gsub("%a+", string.upper)
                    if t ~= old then
                        data.text = t
                        count = count + 1
                        table.insert(report_entries, report_helpers.format_batch_change_report_line(data.index, old, t, {row_id = data.id}))
                        local display_text = build_tree_display_text(data.index, data.timecode or "", nil, t)
                        data.display_text = display_text
                        queue_tree_node_text_update(update_entries, node, display_text)
                    end
                end
            end
            apply_tree_node_text_updates(win, win:Find("SubtitleTree"), update_entries)
        end
        if count > 0 then
            commit_mutation_snapshot(mutation_snapshot)
        end
        local status = win:Find("StatusLabel")
        if status then status:Set("Text", "全大写完成，修改了 " .. count .. " 条") end
        print("[Hooper AI 2.0] 英文已全部转换为大写。")
        LogMsg("[5a] 英文全大写完成，修改了 " .. count .. " 条")
        dlg:Hide()
        if count > 0 then report_helpers.show_batch_result_report("英文全大写", report_entries, count) end
    end

    dlg.On[lower_key].Clicked = function()
        local mutation_snapshot = prepare_mutation_snapshot("英文全小写")
        local count = 0
        local dirty_row_ids = {}
        local report_entries = {}
        if current_rows and #current_rows > 0 then
            for i, data in ipairs(current_rows) do
                if data and data.text then
                    local old = data.text
                    local t = old:gsub("%a+", string.lower)
                    if t ~= old then
                        data.text = t
                        count = count + 1
                        table.insert(report_entries, report_helpers.format_batch_change_report_line(data.index or i, old, t, {row_id = data.id}))
                        data.display_text = build_tree_display_text(data.index or i, data.timecode or "", nil, t)
                        mark_dirty_row(dirty_row_ids, data)
                    end
                end
            end
            if count > 0 then
                sync_current_preview_tree(win, dirty_row_ids)
            end
        else
            local update_entries = {}
            for node, data in pairs(subtitle_data_map) do
                if data and data.text then
                    local old = data.text
                    local t = old:gsub("%a+", string.lower)
                    if t ~= old then
                        data.text = t
                        count = count + 1
                        table.insert(report_entries, report_helpers.format_batch_change_report_line(data.index, old, t, {row_id = data.id}))
                        local display_text = build_tree_display_text(data.index, data.timecode or "", nil, t)
                        data.display_text = display_text
                        queue_tree_node_text_update(update_entries, node, display_text)
                    end
                end
            end
            apply_tree_node_text_updates(win, win:Find("SubtitleTree"), update_entries)
        end
        if count > 0 then
            commit_mutation_snapshot(mutation_snapshot)
        end
        local status = win:Find("StatusLabel")
        if status then status:Set("Text", "全小写完成，修改了 " .. count .. " 条") end
        print("[Hooper AI 2.0] 英文已全部转换为小写。")
        LogMsg("[5b] 英文全小写完成，修改了 " .. count .. " 条")
        dlg:Hide()
        if count > 0 then report_helpers.show_batch_result_report("英文全小写", report_entries, count) end
    end

    dlg.On[title_key].Clicked = function()
        local mutation_snapshot = prepare_mutation_snapshot("英文首字母大写")
        local count = 0
        local dirty_row_ids = {}
        local report_entries = {}
        if current_rows and #current_rows > 0 then
            for i, data in ipairs(current_rows) do
                if data and data.text then
                    local old = data.text
                    local t = old:gsub("(%a)(%a*)", function(first, rest)
                        return string.upper(first) .. string.lower(rest)
                    end)
                    if t ~= old then
                        data.text = t
                        count = count + 1
                        table.insert(report_entries, report_helpers.format_batch_change_report_line(data.index or i, old, t, {row_id = data.id}))
                        data.display_text = build_tree_display_text(data.index or i, data.timecode or "", nil, t)
                        mark_dirty_row(dirty_row_ids, data)
                    end
                end
            end
            if count > 0 then
                sync_current_preview_tree(win, dirty_row_ids)
            end
        else
            local update_entries = {}
            for node, data in pairs(subtitle_data_map) do
                if data and data.text then
                    local old = data.text
                    local t = old:gsub("(%a)(%a*)", function(first, rest)
                        return string.upper(first) .. string.lower(rest)
                    end)
                    if t ~= old then
                        data.text = t
                        count = count + 1
                        table.insert(report_entries, report_helpers.format_batch_change_report_line(data.index, old, t, {row_id = data.id}))
                        local display_text = build_tree_display_text(data.index, data.timecode or "", nil, t)
                        data.display_text = display_text
                        queue_tree_node_text_update(update_entries, node, display_text)
                    end
                end
            end
            apply_tree_node_text_updates(win, win:Find("SubtitleTree"), update_entries)
        end
        if count > 0 then
            commit_mutation_snapshot(mutation_snapshot)
        end
        local status = win:Find("StatusLabel")
        if status then status:Set("Text", "首字母大写完成，修改了 " .. count .. " 条") end
        print("[Hooper AI 2.0] 英文已转换为首字母大写。")
        LogMsg("[5c] 英文首字母大写完成，修改了 " .. count .. " 条")
        dlg:Hide()
        if count > 0 then report_helpers.show_batch_result_report("英文首字母大写", report_entries, count) end
    end

    dlg:Show()
end

-- [6] 敏感词替换 (动态 UID + CurrentIndex 防 ComboBox 报错)
function win.On.BtnStep6.Clicked(ev)
    print("[Hooper AI 2.0] [6] 敏感词替换")
    -- 修复：优先使用 current_rows
    if not current_rows or #current_rows == 0 then
        local status = win:Find("StatusLabel")
        if status then status:Set("Text", "没有字幕数据") end
        return
    end

    local bad_words = {
        "微信", "赚钱", "引流", "加粉", "淘宝", "抖音",
        "快手", "小红书", "傻逼", "死", "卧槽", "特么的",
        "牛逼", "最", "第一"
    }
    local found_list = {}

    -- 修复：使用 current_rows 遍历
    if current_rows and #current_rows > 0 then
        for _, data in ipairs(current_rows) do
            if data and data.text then
                for _, bw in ipairs(bad_words) do
                    if data.text:find(bw) then
                        local exists = false
                        for _, f in ipairs(found_list) do
                            if f == bw then exists = true break end
                        end
                        if not exists then table.insert(found_list, bw) end
                    end
                end
            end
        end
    else
        for _, data in pairs(subtitle_data_map) do
            if data and data.text then
                for _, bw in ipairs(bad_words) do
                    if data.text:find(bw) then
                        local exists = false
                        for _, f in ipairs(found_list) do
                            if f == bw then exists = true break end
                        end
                        if not exists then table.insert(found_list, bw) end
                    end
                end
            end
        end
    end

    if #found_list == 0 then
        local status = win:Find("StatusLabel")
        if status then status:Set("Text", "未检出敏感词") end
        print("[Hooper AI 2.0] 恭喜，当前字幕未检出常见敏感词！")
        LogMsg("[6] 未检出敏感词")
        return
    end

    local uid = tostring(os.time()) .. tostring(math.random(1000, 9999))
    local combo_key = "ComboWords_" .. uid
    local edit_key = "EditRep_" .. uid
    local rep_key = "BtnRep_" .. uid

    local dlg = disp:AddWindow({
        ID = "CensorDlg_" .. uid,
        WindowTitle = "发现违禁词",
        Geometry = {400, 300, 300, 160},
        ui:VGroup {
            Spacing = 10, Weight = 1,
            ui:Label { Text = "检出以下违禁词，请选择并替换：" },
            ui:ComboBox { ID = combo_key },
            ui:HGroup {
                Weight = 0,
                ui:Label { Text = "替换为:", Weight = 0 },
                ui:LineEdit { ID = edit_key, Text = "**", Weight = 1 }
            },
            ui:Button { ID = rep_key, Text = "执行替换 (当前词)", Weight = 0 }
        }
    })

    local itms = dlg:GetItems()
    local combo = itms[combo_key]
    local edit = itms[edit_key]

    for _, bw in ipairs(found_list) do
        combo:AddItem(bw)
    end

    dlg.On[rep_key].Clicked = function()
        local idx = combo.CurrentIndex
        local target = found_list[idx + 1]
        local rep = edit.Text or "**"
        if target and target ~= "" then
            local mutation_snapshot = prepare_mutation_snapshot("替换敏感词: " .. target)
            local count = 0
            local dirty_row_ids = {}
            if current_rows and #current_rows > 0 then
                for i, data in ipairs(current_rows) do
                    if data and data.text then
                        local old = data.text
                        local t = old:gsub(target, rep)
                        if t ~= old then
                            data.text = t
                            count = count + 1
                            data.display_text = build_tree_display_text(data.index or i, data.timecode or "", nil, t)
                            mark_dirty_row(dirty_row_ids, data)
                        end
                    end
                end
                if count > 0 then
                    sync_current_preview_tree(win, dirty_row_ids)
                end
            else
                local update_entries = {}
                for node, data in pairs(subtitle_data_map) do
                    if data and data.text then
                        local old = data.text
                        local t = old:gsub(target, rep)
                        if t ~= old then
                            data.text = t
                            count = count + 1
                            local display_text = build_tree_display_text(data.index, data.timecode or "", nil, t)
                            data.display_text = display_text
                            queue_tree_node_text_update(update_entries, node, display_text)
                        end
                    end
                end
                apply_tree_node_text_updates(win, win:Find("SubtitleTree"), update_entries)
            end
            if count > 0 then
                commit_mutation_snapshot(mutation_snapshot)
            end
            local status = win:Find("StatusLabel")
            if status then status:Set("Text", "已替换 '" .. target .. "' -> '" .. rep .. "' (" .. count .. "条)") end
            print("[Hooper AI 2.0] 已将所有 '" .. target .. "' 替换为 '" .. rep .. "'")
            LogMsg("[6] 已替换 '" .. target .. "'，共 " .. count .. " 条")
            dlg:Hide()
        end
    end

    dlg:Show()
end

-- 7️⃣ 清理空行（倒序安全删除）
function win.On.BtnStep7.Clicked(ev)
    print("[Hooper AI 2.0] 7️⃣ 清理空行")
    -- 修复：优先使用 current_rows
    if not current_rows or #current_rows == 0 then
        local status = win:Find("StatusLabel")
        if status then status:Set("Text", "没有字幕数据") end
        return
    end
    local mutation_snapshot = prepare_mutation_snapshot("清理空行")
    local keys_to_remove = {}
    -- 修复：优先遍历 current_rows
    if current_rows and #current_rows > 0 then
        for i = #current_rows, 1, -1 do
            local data = current_rows[i]
            if data and data.text and data.text:match("^%s*$") then
                table.insert(keys_to_remove, i)
            end
        end
        -- 倒序删除以避免索引偏移
        for _, idx in ipairs(keys_to_remove) do
            table.remove(current_rows, idx)
        end
    else
        for node, data in pairs(subtitle_data_map) do
            if data and data.text and data.text:match("^%s*$") then
                table.insert(keys_to_remove, node)
            end
        end
        for _, node in ipairs(keys_to_remove) do
            subtitle_data_map[node] = nil
        end
    end
    local count = #keys_to_remove
    if count > 0 then
        commit_mutation_snapshot(mutation_snapshot)
        rebuild_tree_from_rows(current_rows, win)
    end
    local status = win:Find("StatusLabel")
    if status then status:Set("Text", "清理空行完成，删除了 " .. count .. " 条空字幕") end
    print("[Hooper AI 2.0] 步骤 7 完成：空字幕块已清理。")
    LogMsg("[7] 清理空行完成，删除了 " .. count .. " 条")
end

-- 8️⃣ 提取纯文本（复制到剪贴板）
function win.On.BtnStep8.Clicked(ev)
    print("[Hooper AI 2.0] 8️⃣ 提取纯文本")
    -- 修复：使用 current_rows 替代 subtitle_data_map，避免搜索过滤导致数据丢失
    if not current_rows or #current_rows == 0 then
        local status = win:Find("StatusLabel")
        if status then status:Set("Text", "没有字幕数据") end
        return
    end
    local sorted = {}
    for _, row in ipairs(current_rows) do
        if row and row.text then
            table.insert(sorted, row)
        end
    end
    table.sort(sorted, function(a, b)
        return (tonumber(a.start_frame) or 0) < (tonumber(b.start_frame) or 0)
    end)
    local txt = ""
    for _, data in ipairs(sorted) do
        txt = txt .. data.text .. "\n"
    end
    pcall(function() bmd.setclipboard(txt) end)
    local status = win:Find("StatusLabel")
    if status then status:Set("Text", "纯文本已复制到剪贴板，共 " .. #sorted .. " 条") end
    print("[Hooper AI 2.0] 步骤 8 完成：纯文本已复制到剪贴板！")
    LogMsg("[8] 提取纯文本完成，共 " .. #sorted .. " 条，已复制到剪贴板")
end

function win.On.AIFixBtn.Clicked(ev)
    do_ai_fix()
end

-- 导入媒体池：输出 SRT 后导入，不写入历史备份清单
function win.On.ExportSrtBtn.Clicked(ev)
    local export_rows = collect_exportable_subtitles()
    local valid_subs = {}

    for i, sub in ipairs(export_rows) do
        local normalized = normalize_export_subtitle(sub, i)
        if normalized then
            table.insert(valid_subs, normalized)
        end
    end
    
    if #valid_subs == 0 then
        print("[Hooper AI 2.0] ⚠️ 导入失败：当前内存中的字幕结构未能提取出有效时间和文本。")
        return
    end
    
    table.sort(valid_subs, function(a, b)
        if a.sort_frame and b.sort_frame and a.sort_frame ~= b.sort_frame then
            return a.sort_frame < b.sort_frame
        end
        if a.Start ~= b.Start then
            return a.Start < b.Start
        end
        return (a.index or 0) < (b.index or 0)
    end)
    
    if current_backup_path == "" then
        print("[Hooper AI 2.0] ❌ 导入失败：备份目录为空。")
        local status = win:Find("StatusLabel")
        if status then status:Set("Text", "❌ 备份目录为空") end
        return
    end

    os.execute('mkdir -p "' .. current_backup_path .. '" 2>/dev/null')
    os.execute('mkdir "' .. current_backup_path .. '" 2>nul')

    local sep = (current_backup_path:sub(-1) == "\\" or current_backup_path:sub(-1) == "/") and "" or "/"
    local file_name = "HooperAI_双语_" .. os.date("%m%d_%H%M%S") .. ".srt"
    local save_path = current_backup_path .. sep .. file_name
    
    local file = io.open(save_path, "w")
    if file then
        for i, sub in ipairs(valid_subs) do
            file:write(i .. "\n")
            file:write(sub.Start .. " --> " .. sub.End .. "\n")
            file:write(sub.Text .. "\n\n")
        end
        file:close()
        
        -- 自动导入媒体池
        local resolve = get_resolve()
        if resolve then
            local pm = resolve:GetProjectManager()
            local project = pm and pm:GetCurrentProject()
            local mediaPool = project and project:GetMediaPool()

            if mediaPool then
                local target_folder = mediaPool:GetCurrentFolder() or mediaPool:GetRootFolder()
                if not target_folder then
                    target_folder = mediaPool:GetRootFolder()
                end
                if target_folder then
                    pcall(function() mediaPool:SetCurrentFolder(target_folder) end)
                end

                local importedItems = mediaPool:ImportMedia({save_path})
                if importedItems and #importedItems > 0 then
                    print("[Hooper AI 2.0] ✅ 成功！SRT 已写入备份目录并导入媒体池: " .. save_path)
                    local status = win:Find("StatusLabel")
                    if status then status:Set("Text", "✅ 已导入媒体池") end
                else
                    print("[Hooper AI 2.0] ⚠️ SRT 已生成到备份目录，但媒体池未接收该文件: " .. save_path)
                    local status = win:Find("StatusLabel")
                    if status then status:Set("Text", "⚠️ 已生成到备份目录，但导入媒体池失败") end
                end
            end
        end
    else
        print("[Hooper AI 2.0] ❌ 导入失败：无法写入备份文件，请检查备份目录权限。")
        local status = win:Find("StatusLabel")
        if status then status:Set("Text", "❌ 备份目录写入失败") end
    end
end

-- 清理备份按钮
function win.On.CleanBtn.Clicked(ev)
    print("[Hooper AI 2.0] 清理备份按钮点击")
    local status = win:Find("StatusLabel")

    if current_backup_path and current_backup_path ~= "" then
        if package.config:sub(1,1) == "\\" then
            os.execute('del /Q /F "' .. current_backup_path .. '\\*.srt" 2>nul')
        else
            os.execute('rm -f "' .. current_backup_path .. '"/*.srt')
        end
        os.remove(get_backup_manifest_path())
    end

    BackupFileMap = {}
    BackupHistoryEntries = {}
    sync_backup_selector()

    if status then
        status:Set("Text", "✅ 已彻底清空历史备份！")
    end
    print("[Hooper AI 2.0] 已清理备份目录: " .. (current_backup_path or ""))
end

-- 底部 Folder 按钮 (唤醒系统文件夹选择器)
function win.On.BackupFolderBtn.Clicked(ev)
    local fu = fusion or bmd.scriptapp("Fusion")
    if fu then
        local selectedPath = fu:RequestDir("")
        if selectedPath and selectedPath ~= "" then
            current_backup_path = tostring(selectedPath):gsub("[\r\n]+$", "")
            sync_backup_path_display()
            refresh_backup_history_cache(BACKUP_HISTORY_LIMIT)
            sync_backup_selector()
            local status = win:Find("StatusLabel")
            if status then
                status:Set("Text", "备份目录已切换")
            end
            print("[Hooper AI 2.0] 📂 备份目录已切换: " .. current_backup_path)
        end
    end
end

function win.On.UndoBtn.Clicked(ev)
    perform_undo()
end

function win.On.BackupPathInput.CurrentIndexChanged(ev)
    if suppress_backup_restore_events then
        return
    end

    local status = win and win:Find("StatusLabel")
    if ensure_backup_selector_fresh(nil, { preserve_current_selection = false, default_index = 0 }) then
        local message = "历史列表已刷新，请重新选择版本"
        if status then status:Set("Text", message) end
        print("[Hooper AI 2.0] " .. message)
        return
    end

    local combo = win and win:Find("BackupPathInput")
    local current_index = combo and tonumber(combo.CurrentIndex) or -1
    if current_index <= 0 then
        return
    end

    local entry = get_selected_backup_entry()
    if entry then
        restore_history_entry(entry)
    end
end

-- 设置备份路径按钮
function win.On.SetPathBtn.Clicked(ev)
    return win.On.BackupFolderBtn.Clicked(ev)
end

-- 更新时间线按钮（自动备份后执行）
function win.On.UpdateBtn.Clicked(ev)
    LogMsg("先备份当前内存字幕，再执行目标字幕轨替换")
    persist_timeline_update_backup()
    update_timeline()
end

-- 双击字幕条目跳转
function win.On.SubtitleTree.ItemDoubleClicked(ev)
    print("[Hooper AI 2.0] 字幕列表双击")
    local tree = win:Find("SubtitleTree")
    local item = get_tree_event_value(ev, {"item", "Item", "currentItem", "CurrentItem", "node", "Node"})
    if tree and item then
        set_tree_current_item(tree, item)
    end
    go_to_subtitle(win)
end

handle_main_window_close = function()
    -- 关窗 = 退出 SubFix（单实例插件）。直接复用 force_quit_subfix，
    -- 这样可以同时取消正在跑的 AI 流程（设置 AI_CANCEL_REQUESTED + kill 后台 curl）
    -- 并连续 5 次 ExitLoop 弹出嵌套 RunLoop。
    -- 之前只 Hide + 单次 ExitLoop，AI 跑批时点关闭按钮无法终止 AI。
    force_quit_subfix()
end

-- 强制退出：模拟 macOS Dock 右键 → 强制退出
-- 立即关闭所有 SubFix 窗口并退出事件循环，无确认弹窗
-- 注意：故意不加 local，避免占用 main chunk 的 200 local 名额
function force_quit_subfix()
    pcall(function() print("[Hooper AI 2.0] 强制退出 SubFix") end)

    -- 通知正在跑的 AI 流程取消（B 方案：execute_ai_request 嵌套 RunLoop 的 poll timer 会读这个）
    AI_CANCEL_REQUESTED = true
    -- 立即 kill 当前后台 curl，避免子进程残留浪费配额
    if AI_CURL_PID_FILE then
        pcall(function()
            local pf = io.open(AI_CURL_PID_FILE, "r")
            if pf then
                local pid = pf:read("*l")
                pf:close()
                if pid and trim_text(pid) ~= "" then
                    local clean_pid = trim_text(pid)
                    os.execute(string.format(
                        "pkill -P %s 2>/dev/null; kill -9 %s 2>/dev/null",
                        clean_pid, clean_pid))
                end
            end
        end)
    end

    -- 关闭所有可能存在的弹出/报告窗口
    if pending_report_window then
        pcall(function() pending_report_window:Hide() end)
        pending_report_window = nil
    end
    if workflow_log_window then
        pcall(function() workflow_log_window:Hide() end)
    end
    if AIConfigPopWin then
        pcall(function() AIConfigPopWin:Hide() end)
        AIConfigPopWin = nil
    end
    -- 释放 pending review 缓存
    pcall(function()
        if type(pending_item_tc_map) == "table" then
            pending_item_tc_map = {}
        end
    end)
    -- 隐藏主/迷你窗口
    if mini_win then
        pcall(function() mini_win:Hide() end)
    end
    if win then
        pcall(function() win:Hide() end)
    end
    -- 退出 Fusion 事件循环
    -- 注意：嵌套 RunLoop（execute_ai_request 等待 curl 时）只能 pop 一层，
    -- 所以这里连续投递 5 次 ExitLoop。每次内层 loop 退出、控制权回到外层后，
    -- 后续 ExitLoop 才能逐层 pop。Qt 单次 dispatch 中重复 ExitLoop 不会造成异常，
    -- 多余的调用在没有更多 nested loop 时是 no-op。
    if dispatcher and dispatcher.ExitLoop then
        for _ = 1, 5 do
            pcall(function() dispatcher:ExitLoop() end)
        end
    end
end

-- 窗口关闭时退出事件循环
function win.On.HooperAI_v2_compact_narrow500_final.Close(ev)
    handle_main_window_close()
end

function win.On.HooperAI_v2_compact_narrow500_final_h900.Close(ev)
    handle_main_window_close()
end

function win.On.HooperAI_v2_compact_narrow500_final_h960.Close(ev)
    handle_main_window_close()
end

function win.On.HooperAI_v2_compact_narrow500_fill.Close(ev)
    handle_main_window_close()
end

function win.On.HooperAI_v2_compact_narrow500.Close(ev)
    handle_main_window_close()
end

function win.On.HooperAI_v2_compact_stable.Close(ev)
    handle_main_window_close()
end

function win.On.HooperAI_v2_compact_w500c.Close(ev)
    handle_main_window_close()
end

function win.On.HooperAI_v2_compact_w500b.Close(ev)
    handle_main_window_close()
end

function win.On.HooperAI_v2_compact_w500.Close(ev)
    handle_main_window_close()
end

function win.On.HooperAI_v2_compact_final2.Close(ev)
    handle_main_window_close()
end

function win.On.HooperAI_v2_compact_final.Close(ev)
    handle_main_window_close()
end

function win.On.HooperAI_v2_uicompact3.Close(ev)
    handle_main_window_close()
end

function win.On.HooperAI_v2_uicompact2.Close(ev)
    handle_main_window_close()
end

function win.On.HooperAI_v2_uicompact.Close(ev)
    handle_main_window_close()
end

function win.On.HooperAI_v2_uireset.Close(ev)
    handle_main_window_close()
end

function win.On.HooperAI_v2.Close(ev)
    handle_main_window_close()
end

-- 强制退出按钮（仅主窗口；迷你窗口太窄不放，避免挤变形）
function win.On.ForceQuitBtn.Clicked(ev)
    force_quit_subfix()
end

-- ========== 启动前初始化 ==========
sync_backup_path_display()
BackupFileMap = {}
BackupHistoryEntries = {}
set_current_preview_source(PREVIEW_SOURCE_TIMELINE)
mark_backup_selector_dirty()
update_undo_redo_button_states()
print("[Hooper AI 2.0] 备份历史改为按需加载，启动阶段跳过隐藏主窗下拉初始化")
print(string.format("[Hooper AI 2.0] [STARTUP] 备份系统初始化完成: +%d ms", startup_elapsed_ms()))

-- ========== 启动 ==========

-- 注册选项卡
local itm = {
    MainTabs = win:Find("MainTabs"),
    TabStack = win:Find("TabStack"),
    PresetCombo = win:Find("PresetCombo"),
    ApiUrlInput = find_ui_item("ApiUrlInput"),
    ApiKeyInput = find_ui_item("ApiKeyInput"),
    ModelInput = find_ui_item("ModelInput"),
    EnableScriptAssistCheckbox = find_ui_item("EnableScriptAssistCheckbox"),
    ReferenceScriptInput = find_ui_item("ReferenceScriptInput"),
    ReferenceScriptRiskLabel = find_ui_item("ReferenceScriptRiskLabel"),
    BackupPathInput = win:Find("BackupPathInput")
}

if itm.MainTabs then
    itm.MainTabs:AddTab("精修工具")
    itm.MainTabs:AddTab("AI 工作台")
    itm.MainTabs.CurrentIndex = 0
end
if itm.TabStack then
    switch_stack_page_index_only(win, "TabStack", 0)
end

-- 加载 API 配置并填充到输入框
current_ai_provider_id = LoadActiveProviderId()
itm.config = LoadConfig(current_ai_provider_id)
itm.shared_config = LoadSharedConfig()
apply_provider_config_to_ui(current_ai_provider_id, itm.config)
apply_shared_config_to_ui(itm.shared_config)

-- 绑定下拉框切换事件：自动填写 URL 和模型名称
function win.On.PresetCombo.CurrentIndexChanged(ev)
    if not full_window_ai_controls_initialized then
        return
    end
    if suppress_provider_change_events or provider_sync_in_progress or provider_combo_bootstrap_in_progress then
        return
    end

    local combo = win and win:Find("PresetCombo")
    if not combo then
        return
    end

    local live_index = tonumber(combo.CurrentIndex)
    if live_index == nil or live_index < 0 then
        return
    end

    local event_index = tonumber(ev and ev.Index)
    if event_index ~= nil and event_index ~= live_index then
        print(string.format(
            "[Hooper AI 2.0] PresetCombo stale event ignored: ev=%d, live=%d",
            event_index,
            live_index
        ))
        return
    end

    local target_provider_id = get_provider_id_by_index(live_index)
    if target_provider_id == current_ai_provider_id then
        return
    end
    save_shared_config_from_ui()
    switch_ai_provider(target_provider_id, {save_current = true})
end

-- TabBar 联动逻辑 (强制让 Stack 切换 Index)
function win.On.MainTabs.CurrentChanged(ev)
    if itm.TabStack then
        switch_stack_page_index_only(win, "TabStack", ev and ev.Index or 0)
    end
end

-- 初始化 AI 任务选项
itm.items = win:GetItems()
if itm.items and itm.items.TargetTrackSpin then
    sync_target_track_control()
end

active_window = mini_win
sync_track_control(win)
sync_track_control(mini_win)
sync_search_control(win)
sync_search_control(mini_win)
set_subtitle_loaded_state(false, nil, win)
set_subtitle_loaded_state(false, nil, mini_win)
update_target_track_hint()
update_shared_status(win, shared_status_text)
update_shared_status(mini_win, shared_status_text)

print(string.format("[Hooper AI 2.0] [STARTUP] 即将 Show 极简版窗口: +%d ms", startup_elapsed_ms()))
_subfix_show_started_at = os.clock()  -- 用全局，避免触发 200 local 上限
mini_win:Show()
print(string.format("[Hooper AI 2.0] [STARTUP] mini_win:Show() 用时: %d ms", math.floor(((os.clock() - _subfix_show_started_at) * 1000) + 0.5)))
set_mini_subtitle_area_state(mini_win, false, "正在自动加载字幕…")
update_shared_status(mini_win, "正在自动加载字幕...")
set_load_status_label(false, "<font color='#FA8C16'>⏳ 正在自动加载</font>", mini_win)
restart_ui_timer(startup_refresh_timer)
print(string.format("[Hooper AI 2.0] 极简版窗口已显示，自动加载字幕已排队 (距脚本启动: +%d ms)。", startup_elapsed_ms()))

-- 必须进入事件循环
if dispatcher and dispatcher.RunLoop then
    dispatcher:RunLoop()
end
print("[Hooper AI 2.0] 脚本已退出。")
