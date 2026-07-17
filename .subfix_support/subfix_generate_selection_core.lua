#!/usr/bin/env lua
-- SubFix shared module: generate subtitles for the current DaVinci Resolve In/Out selection.

local SubFixGenerateSelectionCore = {}
local runtime_options = {}
local TARGET_SUBTITLE_TRACK = 1
local DEFAULT_ASR_MODEL = "large-v3-turbo"
local DEFAULT_ASR_LANGUAGE = tostring(os.getenv("SUBFIX_ASR_LANGUAGE") or "zh")
local DEFAULT_ASR_BACKEND = "auto"
local WORK_SCOPE_MODE_SELECTION = "selection"
local TRACK_CHECKED_MARK = "☑"
local TRACK_UNCHECKED_MARK = "☐"
local FALLBACK_ITEM_SCOPE_EXPAND_MAX_GAP_FRAMES = 2
local GENERATE_PROGRESS_BAR_WIDTH = 36
local GENERATE_WRITEBACK_OVERLAY_GEOMETRY = {120, 90, 1680, 880}

local fusion_app = fu or fusion
local ui = fusion_app and fusion_app.UIManager or nil
local dispatcher = (ui and bmd and bmd.UIDispatcher) and bmd.UIDispatcher(ui) or nil
local ui_timer_handlers = {}

if dispatcher and dispatcher.On then
    function dispatcher.On.Timeout(ev)
        local timer_id = tostring(ev and ev.who or "")
        local handler = ui_timer_handlers[timer_id]
        if handler then handler(ev) end
    end
end

local function shell_quote(value)
    local text = tostring(value or "")
    return "'" .. text:gsub("'", "'\\''") .. "'"
end

local function read_text_file(path)
    local file = io.open(tostring(path or ""), "r")
    if not file then return nil end
    local content = file:read("*a")
    file:close()
    return content
end

local function write_text_file(path, content)
    local file = io.open(tostring(path or ""), "w")
    if not file then return false end
    file:write(tostring(content or ""))
    file:close()
    return true
end

local function json_escape(value)
    return tostring(value or "")
        :gsub("\\", "\\\\")
        :gsub('"', '\\"')
        :gsub("\n", "\\n")
        :gsub("\r", "\\r")
        :gsub("\t", "\\t")
end

local function get_checkbox_checked(item)
    if not item then return false end
    local attempts = {
        function() return item.Checked end,
        function() return item.CheckState end,
    }
    for _, getter in ipairs(attempts) do
        local ok, value = pcall(getter)
        if ok then
            if value == true or value == 1 then return true end
            local lowered = tostring(value or ""):lower()
            if lowered == "true" or lowered == "checked" or lowered == "1" then return true end
        end
    end
    return false
end

local function set_checkbox_checked(item, checked)
    if not item then return end
    local next_value = checked == true
    pcall(function() item.Checked = next_value end)
    pcall(function() item.CheckState = next_value and 1 or 0 end)
end

local function set_tree_item_text(item, column, text)
    pcall(function() item.Text[column] = tostring(text or "") end)
end

local function get_tree_event_value(ev, keys)
    for _, key in ipairs(keys or {}) do
        if ev and ev[key] ~= nil then return ev[key] end
    end
    return nil
end

local function get_selected_tree_node(tree)
    if not tree then return nil end
    local attempts = {
        function() return tree.CurrentItem end,
        function() return tree.SelectedItem end,
        function()
            local selected = tree:SelectedItems()
            return selected and selected[1] or nil
        end,
    }
    for _, getter in ipairs(attempts) do
        local ok, item = pcall(getter)
        if ok and item then return item end
    end
    return nil
end

local function safe_refresh_tree_widget(tree)
    if not tree then return end
    pcall(function() tree:Update() end)
    pcall(function() tree:Repaint() end)
end

local function trim_text(value)
    return tostring(value or ""):match("^%s*(.-)%s*$") or ""
end

local function decode_json_text(json_text)
    if type(json_text) ~= "string" or json_text == "" then return nil, "JSON 为空" end
    local pos = 1
    local len = #json_text
    local parse_value

    local function fail(msg)
        error(msg .. " at " .. tostring(pos), 0)
    end

    local function skip_ws()
        while pos <= len and json_text:sub(pos, pos):match("%s") do
            pos = pos + 1
        end
    end

    local function parse_string()
        if json_text:sub(pos, pos) ~= '"' then fail("expected string") end
        pos = pos + 1
        local parts = {}
        while pos <= len do
            local ch = json_text:sub(pos, pos)
            if ch == '"' then
                pos = pos + 1
                return table.concat(parts)
            elseif ch == "\\" then
                local esc = json_text:sub(pos + 1, pos + 1)
                local mapped = ({['"']='"', ["\\"]="\\", ["/"]="/", b="\b", f="\f", n="\n", r="\r", t="\t"})[esc]
                if mapped then
                    parts[#parts + 1] = mapped
                    pos = pos + 2
                elseif esc == "u" then
                    parts[#parts + 1] = "?"
                    pos = pos + 6
                else
                    fail("bad escape")
                end
            else
                parts[#parts + 1] = ch
                pos = pos + 1
            end
        end
        fail("unterminated string")
    end

    local function parse_number()
        local text = json_text:sub(pos):match("^%-?%d+%.?%d*[eE]?[%+%-]?%d*")
        if not text or text == "" then fail("expected number") end
        pos = pos + #text
        return tonumber(text)
    end

    local function parse_array()
        pos = pos + 1
        skip_ws()
        local result = {}
        if json_text:sub(pos, pos) == "]" then pos = pos + 1; return result end
        while true do
            result[#result + 1] = parse_value()
            skip_ws()
            local ch = json_text:sub(pos, pos)
            if ch == "," then
                pos = pos + 1
                skip_ws()
            elseif ch == "]" then
                pos = pos + 1
                return result
            else
                fail("expected array separator")
            end
        end
    end

    local function parse_object()
        pos = pos + 1
        skip_ws()
        local result = {}
        if json_text:sub(pos, pos) == "}" then pos = pos + 1; return result end
        while true do
            skip_ws()
            local key = parse_string()
            skip_ws()
            if json_text:sub(pos, pos) ~= ":" then fail("expected colon") end
            pos = pos + 1
            result[key] = parse_value()
            skip_ws()
            local ch = json_text:sub(pos, pos)
            if ch == "," then
                pos = pos + 1
                skip_ws()
            elseif ch == "}" then
                pos = pos + 1
                return result
            else
                fail("expected object separator")
            end
        end
    end

    parse_value = function()
        skip_ws()
        local ch = json_text:sub(pos, pos)
        if ch == '"' then return parse_string() end
        if ch == "{" then return parse_object() end
        if ch == "[" then return parse_array() end
        if ch == "t" and json_text:sub(pos, pos + 3) == "true" then pos = pos + 4; return true end
        if ch == "f" and json_text:sub(pos, pos + 4) == "false" then pos = pos + 5; return false end
        if ch == "n" and json_text:sub(pos, pos + 3) == "null" then pos = pos + 4; return nil end
        if ch == "-" or ch:match("%d") then return parse_number() end
        fail("unexpected JSON value")
    end

    local ok, result = pcall(function()
        local value = parse_value()
        skip_ws()
        return value
    end)
    if ok then return result end
    return nil, tostring(result)
end

local function file_exists(path)
    local file = io.open(tostring(path or ""), "rb")
    if file then
        file:close()
        return true
    end
    return false
end

local function register_ui_timer(timer, handler)
    if not timer or type(handler) ~= "function" then return false end
    local timer_id = tostring(timer.ID or "")
    if timer_id == "" then return false end
    ui_timer_handlers[timer_id] = handler
    return true
end

local function basename(path)
    local text = tostring(path or "")
    return text:match("([^/\\]+)$") or text
end

local function script_dir()
    local source = debug and debug.getinfo and debug.getinfo(1, "S").source or ""
    source = tostring(source or ""):gsub("^@", "")
    local dir = source:match("^(.*[/\\])")
    if dir and dir ~= "" then
        return dir:gsub("[/\\]$", "")
    end
    return os.getenv("PWD") or "."
end

local function configured_script_root()
    local root = runtime_options and runtime_options.script_root
    root = tostring(root or "")
    if root ~= "" then
        return root:gsub("[/\\]$", "")
    end
    return script_dir()
end

local function resolve_asr_paths()
    local root = configured_script_root()
    local helper_dir = root .. "/.subfix_support"
    local home_dir = os.getenv("HOME") or ""
    local user_support_dir = home_dir ~= "" and (home_dir .. "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility/.subfix_support") or helper_dir
    local user_python = user_support_dir .. "/.subfix_asr_env/bin/python"
    local paths = {
        helper = helper_dir .. "/subfix_asr_transcribe.py",
        setup = helper_dir .. "/setup_asr_env.sh",
        python = helper_dir .. "/.subfix_asr_env/bin/python",
        diagnostic = user_support_dir .. "/last_generate_diagnostic.json"
    }
    if not file_exists(paths.helper) and file_exists(root .. "/subfix_asr_transcribe.py") then
        paths.helper = root .. "/subfix_asr_transcribe.py"
        paths.setup = root .. "/setup_asr_env.sh"
        paths.python = root .. "/.subfix_asr_env/bin/python"
    end
    local module_dir = script_dir()
    if not file_exists(paths.helper) and file_exists(module_dir .. "/subfix_asr_transcribe.py") then
        paths.helper = module_dir .. "/subfix_asr_transcribe.py"
        paths.setup = module_dir .. "/setup_asr_env.sh"
        paths.python = module_dir .. "/.subfix_asr_env/bin/python"
    end
    if not file_exists(paths.python) and file_exists(user_python) then
        paths.python = user_python
    end
    return paths
end

-- 豆包（火山引擎）云端 ASR 密钥文件路径：必须与「实际被执行的」
-- subfix_asr_transcribe.py 同目录，才能与 Python 端
-- _doubao_credentials_path()（Path(__file__).with_name(...)）指向一致，
-- 保证 UI 写盘后立即可读、无需再跑 sync。
local function doubao_credentials_file_path()
    local helper = tostring(resolve_asr_paths().helper or "")
    local dir = helper:gsub("/[^/]*$", "")
    if dir == "" or dir == helper then
        dir = configured_script_root() .. "/.subfix_support"
    end
    return dir .. "/doubao_credentials.json"
end

-- 读回已存的 appid/token（缺失/解析失败/字段空 → 返回空串），用于预填与「是否已配置」判断。
local function read_doubao_credentials()
    local text = read_text_file(doubao_credentials_file_path())
    if not text or text == "" then return "", "" end
    local data = decode_json_text(text)
    if type(data) ~= "table" then return "", "" end
    return trim_text(tostring(data.appid or "")), trim_text(tostring(data.token or ""))
end

-- 是否已配置豆包密钥：环境变量优先（与 Python 三级读取一致），否则看密钥文件。
local function doubao_credentials_configured()
    local env_appid = trim_text(os.getenv("SUBFIX_DOUBAO_APPID") or "")
    local env_token = trim_text(os.getenv("SUBFIX_DOUBAO_TOKEN") or "")
    if env_appid ~= "" and env_token ~= "" then return true end
    local appid, token = read_doubao_credentials()
    return appid ~= "" and token ~= ""
end

-- 写入 appid/token 到密钥文件（json_escape 转义，schema 与 doubao_credentials.json.example 一致）。
local function save_doubao_credentials(appid, token)
    local content = string.format(
        '{\n  "appid": "%s",\n  "token": "%s"\n}\n',
        json_escape(appid), json_escape(token)
    )
    local path = doubao_credentials_file_path()
    if not write_text_file(path, content) then
        return false, "无法写入密钥文件: " .. path
    end
    return true
end

local function temp_dir()
    local root = (os.getenv("TMPDIR") or "/tmp") .. "/SubFix_GenerateSelectionSubtitles"
    os.execute("mkdir -p " .. shell_quote(root) .. " 2>/dev/null")
    return root
end

local function parse_fps(value)
    local text = tostring(value or "")
    if text == "29.97" then return 30000 / 1001 end
    if text == "23.976" or text == "23.98" then return 24000 / 1001 end
    if text == "59.94" then return 60000 / 1001 end
    local fps = tonumber(value)
    if fps and fps > 0 then return fps end
    print(string.format("[SubFix Generate] 无法解析时间线帧率 %q，回退 30.0", text))
    return 30.0
end

local function frames_to_srt_time(frame, fps, base_frame)
    local rel_frame = math.max(0, (tonumber(frame) or 0) - (tonumber(base_frame) or 0))
    local ms = math.floor((rel_frame / math.max(1, tonumber(fps) or 30)) * 1000 + 0.5)
    local hours = math.floor(ms / 3600000)
    ms = ms % 3600000
    local minutes = math.floor(ms / 60000)
    ms = ms % 60000
    local seconds = math.floor(ms / 1000)
    local millis = ms % 1000
    return string.format("%02d:%02d:%02d,%03d", hours, minutes, seconds, millis)
end

local function frames_to_timecode(frame, fps)
    local rate = math.max(1, tonumber(fps) or 30)
    local fps_int = math.max(1, math.floor(rate + 0.5))
    local frames = math.max(0, math.floor(tonumber(frame) or 0))
    local total_seconds = frames / rate
    local hours = math.floor(total_seconds / 3600)
    local remaining = total_seconds % 3600
    local minutes = math.floor(remaining / 60)
    local seconds = math.floor(remaining % 60)
    local ff = frames - math.floor((hours * 3600 + minutes * 60 + seconds) * rate)
    ff = math.max(0, math.min(ff, fps_int - 1))
    return string.format("%02d:%02d:%02d:%02d", hours, minutes, seconds, ff)
end

local function timecode_to_frame(value, fps)
    local hh, mm, ss, ff = tostring(value or ""):match("^(%d+):(%d+):(%d+)[:;](%d+)$")
    if not hh then return nil end
    local rate = math.max(1, tonumber(fps) or 30)
    local total_seconds = (tonumber(hh) or 0) * 3600 + (tonumber(mm) or 0) * 60 + (tonumber(ss) or 0)
    return math.floor(total_seconds * rate + (tonumber(ff) or 0) + 0.5)
end

local function srt_time_to_frame(value, fps, base_frame)
    local hh, mm, ss, ms = tostring(value or ""):match("^(%d+):(%d+):(%d+),(%d+)$")
    if not hh then return nil end
    local total_ms = ((tonumber(hh) or 0) * 3600 + (tonumber(mm) or 0) * 60 + (tonumber(ss) or 0)) * 1000 + (tonumber(ms) or 0)
    return math.floor((total_ms / 1000) * math.max(1, tonumber(fps) or 30) + 0.5) + (tonumber(base_frame) or 0)
end

local function get_resolve()
    if resolve then return resolve end
    if bmd and bmd.scriptapp then
        return bmd.scriptapp("Resolve")
    end
    return nil
end

local function get_mark_value(mark, kind)
    if type(mark) ~= "table" then return nil end
    local keys = kind == "in"
        and {"in", "markIn", "mark_in", "MarkIn", "start", "Start", "startFrame", "start_frame", 1}
        or {"out", "markOut", "mark_out", "MarkOut", "end", "End", "endFrame", "end_frame", 2}
    for _, key in ipairs(keys) do
        if mark[key] ~= nil then return mark[key] end
    end
    return nil
end

local function normalize_mark_frame(value, timeline_start, timeline_end, fps)
    local parsed_from_timecode = false
    local frame = tonumber(value)
    if not frame then
        frame = timecode_to_frame(value, fps)
        parsed_from_timecode = frame ~= nil
    end
    if not frame then return nil end
    local start_frame = tonumber(timeline_start) or 0
    local end_frame = tonumber(timeline_end)
    if parsed_from_timecode then
        if end_frame and frame >= start_frame and frame <= end_frame then
            return math.floor(frame + 0.5)
        end
        local shifted_timecode_frame = frame + start_frame
        if end_frame and shifted_timecode_frame >= start_frame and shifted_timecode_frame <= end_frame then
            return math.floor(shifted_timecode_frame + 0.5)
        end
        return math.floor(frame + 0.5)
    end
    local shifted_frame = frame + start_frame

    -- Resolve may return In/Out as elapsed timeline frames on timelines that start
    -- at 01:00:00. Prefer the shifted candidate when it still lands on the timeline;
    -- this mirrors SubFix's "absolute rows, relative SRT" writeback model and avoids
    -- the classic one-hour-left placement.
    if end_frame and start_frame > 0 and shifted_frame >= start_frame and shifted_frame <= end_frame then
        return math.floor(shifted_frame + 0.5)
    end
    if end_frame and frame >= start_frame and frame <= end_frame then
        return math.floor(frame + 0.5)
    end
    return math.floor(shifted_frame + 0.5)
end

local timeline_item_is_selected

local function get_timeline_frame_bounds(timeline)
    local ok_start, timeline_start = pcall(function() return timeline:GetStartFrame() end)
    local ok_end, timeline_end = pcall(function() return timeline:GetEndFrame() end)
    timeline_start = ok_start and tonumber(timeline_start) or 0
    timeline_end = ok_end and tonumber(timeline_end) or timeline_start
    return timeline_start, timeline_end
end

local function get_timeline_item_name(item)
    local ok_name, raw_name = pcall(function() return item:GetName() end)
    local name = ok_name and trim_text(raw_name) or ""
    if name ~= "" then return name end
    local ok_media, media_item = pcall(function() return item:GetMediaPoolItem() end)
    if ok_media and media_item then
        local ok_clip_name, raw_clip_name = pcall(function() return media_item:GetClipProperty("Clip Name") end)
        name = ok_clip_name and trim_text(raw_clip_name) or ""
        if name ~= "" then return name end
    end
    return ""
end

local function get_timeline_item_media_key(item)
    local ok_media, media_item = pcall(function() return item:GetMediaPoolItem() end)
    if ok_media and media_item then
        local ok_path, raw_path = pcall(function() return media_item:GetClipProperty("File Path") end)
        local path = ok_path and trim_text(raw_path) or ""
        if path ~= "" then return "path:" .. path end
        local ok_clip_name, raw_clip_name = pcall(function() return media_item:GetClipProperty("Clip Name") end)
        local clip_name = ok_clip_name and trim_text(raw_clip_name) or ""
        if clip_name ~= "" then return "clip:" .. clip_name end
    end
    local item_name = get_timeline_item_name(item)
    if item_name ~= "" then return "item:" .. item_name end
    return nil
end

local function build_timeline_item_scope(source, source_label, start_frame, end_frame, timeline_start, timeline_end, item_name)
    start_frame = tonumber(start_frame)
    end_frame = tonumber(end_frame)
    if not start_frame or not end_frame or end_frame <= start_frame then return nil end
    return {
        mode = WORK_SCOPE_MODE_SELECTION,
        source = source,
        source_label = source_label,
        start_frame = math.floor(start_frame + 0.5),
        end_frame = math.floor(end_frame + 0.5),
        timeline_start_frame = tonumber(timeline_start) or 0,
        timeline_end_frame = tonumber(timeline_end) or tonumber(timeline_start) or 0,
        item_name = item_name
    }
end

local function expand_scope_from_seed_item(timeline, track_type, track_index, seed_item, seed_start, seed_end)
    local seed_key = get_timeline_item_media_key(seed_item)
    if not seed_key or seed_key == "" then
        return tonumber(seed_start), tonumber(seed_end), 1
    end
    local ok_items, items = pcall(function() return timeline:GetItemListInTrack(track_type, track_index) end)
    items = ok_items and items or {}
    local records = {}
    local seed_record_index = nil
    for item_index, item in ipairs(items or {}) do
        local ok_start, item_start = pcall(function() return item:GetStart() end)
        local ok_end, item_end = pcall(function() return item:GetEnd() end)
        item_start = ok_start and tonumber(item_start) or nil
        item_end = ok_end and tonumber(item_end) or nil
        if item_start and item_end and item_end > item_start then
            local record = {
                item = item,
                item_index = item_index,
                start_frame = item_start,
                end_frame = item_end,
                media_key = get_timeline_item_media_key(item)
            }
            records[#records + 1] = record
            if item == seed_item or (
                item_start == tonumber(seed_start)
                and item_end == tonumber(seed_end)
                and not seed_record_index
            ) then
                seed_record_index = #records
            end
        end
    end
    if not seed_record_index then
        return tonumber(seed_start), tonumber(seed_end), 1
    end
    table.sort(records, function(a, b)
        if a.start_frame ~= b.start_frame then return a.start_frame < b.start_frame end
        return (tonumber(a.item_index) or 0) < (tonumber(b.item_index) or 0)
    end)
    for index, record in ipairs(records) do
        if record.item == seed_item or (
            record.start_frame == tonumber(seed_start)
            and record.end_frame == tonumber(seed_end)
        ) then
            seed_record_index = index
            break
        end
    end
    local first_index = seed_record_index
    local last_index = seed_record_index
    local max_gap = tonumber(FALLBACK_ITEM_SCOPE_EXPAND_MAX_GAP_FRAMES) or 0
    while first_index > 1 do
        local prev = records[first_index - 1]
        local current = records[first_index]
        local gap = current.start_frame - prev.end_frame
        if prev.media_key ~= seed_key or gap < 0 or gap > max_gap then break end
        first_index = first_index - 1
    end
    while last_index < #records do
        local current = records[last_index]
        local next_record = records[last_index + 1]
        local gap = next_record.start_frame - current.end_frame
        if next_record.media_key ~= seed_key or gap < 0 or gap > max_gap then break end
        last_index = last_index + 1
    end
    return records[first_index].start_frame, records[last_index].end_frame, (last_index - first_index + 1)
end

local function read_timeline_in_out_scope(timeline, fps)
    local timeline_start, timeline_end = get_timeline_frame_bounds(timeline)
    local ok_marks, marks = pcall(function() return timeline:GetMarkInOut() end)
    if not ok_marks or type(marks) ~= "table" then
        return nil, "请先用 I/O 设置 In/Out 选区"
    end
    local mark_candidates = {}
    if marks.video ~= nil then mark_candidates[#mark_candidates + 1] = marks.video end
    if marks.audio ~= nil then mark_candidates[#mark_candidates + 1] = marks.audio end
    if marks.all ~= nil then mark_candidates[#mark_candidates + 1] = marks.all end
    mark_candidates[#mark_candidates + 1] = marks
    for _, mark in ipairs(mark_candidates) do
        local mark_in = get_mark_value(mark, "in")
        local mark_out = get_mark_value(mark, "out")
        local source_label = "In/Out"
        if mark_in == nil and mark_out ~= nil then
            mark_in = timeline_start
            source_label = "In/Out (Out only)"
        elseif mark_in ~= nil and mark_out == nil then
            mark_out = timeline_end
            source_label = "In/Out (In only)"
        end
        if mark_in ~= nil and mark_out ~= nil then
            local start_frame = normalize_mark_frame(mark_in, timeline_start, timeline_end, fps)
            local end_frame = normalize_mark_frame(mark_out, timeline_start, timeline_end, fps)
            if source_label == "In/Out (Out only)" then
                start_frame = timeline_start
            elseif source_label == "In/Out (In only)" then
                end_frame = timeline_end
            end
            if start_frame and end_frame and end_frame > start_frame then
                print(string.format(
                    "[SubFix Generate] In/Out raw=%s-%s timeline_start=%s normalized=%s-%s",
                    tostring(mark_in),
                    tostring(mark_out),
                    tostring(timeline_start),
                    tostring(start_frame),
                    tostring(end_frame)
                ))
                return {
                    mode = WORK_SCOPE_MODE_SELECTION,
                    source = "in_out",
                    source_label = source_label,
                    start_frame = start_frame,
                    end_frame = end_frame,
                    timeline_start_frame = timeline_start,
                    timeline_end_frame = timeline_end
                }
            end
        end
    end
    return nil, "未读取到 O 点；请按 O 设置结束点后再生成"
end

local function read_selected_timeline_item_scope(timeline)
    local timeline_start, timeline_end = get_timeline_frame_bounds(timeline)
    local selected_start = nil
    local selected_end = nil
    local selected_count = 0
    local selected_names = {}
    local track_types = {"video", "audio"}
    for _, track_type in ipairs(track_types) do
        local ok_track_count, track_count = pcall(function() return timeline:GetTrackCount(track_type) end)
        track_count = ok_track_count and tonumber(track_count) or 0
        for track_index = 1, track_count do
            local ok_items, items = pcall(function() return timeline:GetItemListInTrack(track_type, track_index) end)
            items = ok_items and items or {}
            for _, item in ipairs(items or {}) do
                if timeline_item_is_selected and timeline_item_is_selected(item) then
                    local ok_start, item_start = pcall(function() return item:GetStart() end)
                    local ok_end, item_end = pcall(function() return item:GetEnd() end)
                    item_start = ok_start and tonumber(item_start) or nil
                    item_end = ok_end and tonumber(item_end) or nil
                    if item_start and item_end and item_end > item_start then
                        local expanded_start, expanded_end, expanded_count = expand_scope_from_seed_item(
                            timeline,
                            track_type,
                            track_index,
                            item,
                            item_start,
                            item_end
                        )
                        selected_start = selected_start and math.min(selected_start, expanded_start or item_start) or (expanded_start or item_start)
                        selected_end = selected_end and math.max(selected_end, expanded_end or item_end) or (expanded_end or item_end)
                        selected_count = selected_count + 1
                        local item_name = get_timeline_item_name(item)
                        if item_name ~= "" and #selected_names < 3 then selected_names[#selected_names + 1] = item_name end
                        if (tonumber(expanded_count) or 1) > 1 then
                            print(string.format(
                                "[SubFix Generate] 选中片段已按相邻同素材扩展 track=%s:%d %s-%s -> %s-%s count=%d",
                                tostring(track_type),
                                track_index,
                                tostring(item_start),
                                tostring(item_end),
                                tostring(expanded_start),
                                tostring(expanded_end),
                                tonumber(expanded_count) or 1
                            ))
                        end
                    end
                end
            end
        end
    end
    if selected_start and selected_end and selected_end > selected_start then
        print(string.format(
            "[SubFix Generate] 未读到 I/O，使用 Resolve 可读的选中片段范围 %s-%s count=%d",
            tostring(selected_start),
            tostring(selected_end),
            selected_count
        ))
        return build_timeline_item_scope(
            "selected_timeline_items",
            "选中片段",
            selected_start,
            selected_end,
            timeline_start,
            timeline_end,
            table.concat(selected_names, " / ")
        )
    end
    return nil, "Resolve 脚本 API 未返回可读的选中片段"
end

local function find_playhead_item_scope_in_track_type(timeline, fps, track_type, source_label)
    local ok_timecode, current_timecode = pcall(function() return timeline:GetCurrentTimecode() end)
    local playhead_frame = ok_timecode and timecode_to_frame(current_timecode, fps) or nil
    if not playhead_frame then return nil, "无法读取当前播放头时间码" end
    local timeline_start, timeline_end = get_timeline_frame_bounds(timeline)
    playhead_frame = normalize_mark_frame(playhead_frame, timeline_start, timeline_end)
    local ok_track_count, track_count = pcall(function() return timeline:GetTrackCount(track_type) end)
    track_count = ok_track_count and tonumber(track_count) or 0
    local best_item = nil
    for track_index = 1, track_count do
        local ok_items, items = pcall(function() return timeline:GetItemListInTrack(track_type, track_index) end)
        items = ok_items and items or {}
        for item_index, item in ipairs(items or {}) do
            local ok_start, item_start = pcall(function() return item:GetStart() end)
            local ok_end, item_end = pcall(function() return item:GetEnd() end)
            item_start = ok_start and tonumber(item_start) or nil
            item_end = ok_end and tonumber(item_end) or nil
            if item_start and item_end and item_start <= playhead_frame and playhead_frame < item_end then
                local duration = item_end - item_start
                if duration > 0 and (
                    not best_item
                    or duration > best_item.duration
                    or (duration == best_item.duration and track_index < best_item.track_index)
                    or (duration == best_item.duration and track_index == best_item.track_index and item_index < best_item.item_index)
                ) then
                    best_item = {
                        item = item,
                        item_start = item_start,
                        item_end = item_end,
                        duration = duration,
                        track_index = track_index,
                        item_index = item_index
                    }
                end
            end
        end
    end
    if best_item then
        local item_name = get_timeline_item_name(best_item.item)
        local expanded_start, expanded_end, expanded_count = expand_scope_from_seed_item(
            timeline,
            track_type,
            best_item.track_index,
            best_item.item,
            best_item.item_start,
            best_item.item_end
        )
        expanded_start = expanded_start or best_item.item_start
        expanded_end = expanded_end or best_item.item_end
        print(string.format(
            "[SubFix Generate] 未读到 I/O，使用播放头所在%s片段 %s-%s track=%d item=%d，扩展后 %s-%s count=%d",
            track_type,
            tostring(best_item.item_start),
            tostring(best_item.item_end),
            best_item.track_index,
            best_item.item_index,
            tostring(expanded_start),
            tostring(expanded_end),
            tonumber(expanded_count) or 1
        ))
        return build_timeline_item_scope(
            "playhead_" .. track_type .. "_item",
            source_label,
            expanded_start,
            expanded_end,
            timeline_start,
            timeline_end,
            item_name
        )
    end
    return nil, "播放头不在任何" .. tostring(source_label) .. "内"
end

local function read_playhead_timeline_item_scope(timeline, fps)
    local scope = find_playhead_item_scope_in_track_type(timeline, fps, "video", "播放头所在视频片段")
    if scope then return scope end
    return find_playhead_item_scope_in_track_type(timeline, fps, "audio", "播放头所在音频片段")
end

local function read_generation_scope(timeline, fps)
    local scope, scope_err = read_timeline_in_out_scope(timeline, fps)
    if scope and scope.mode == WORK_SCOPE_MODE_SELECTION then
        return scope
    end
    return nil, (scope_err or "请先用 I/O 设置 In/Out 选区") .. "；灰色时间线选择或鼠标选中片段 Resolve 脚本 API 读不到"
end

local function get_audio_item_source_offset_frames(item)
    local ok_source_start, source_start_frames = pcall(function() return item:GetSourceStartFrame() end)
    local offset = ok_source_start and tonumber(source_start_frames) or nil
    if offset and offset >= 0 then return offset end
    local ok_left_offset, left_offset_frames = pcall(function() return item:GetLeftOffset(false) end)
    if not ok_left_offset then
        ok_left_offset, left_offset_frames = pcall(function() return item:GetLeftOffset() end)
    end
    offset = ok_left_offset and tonumber(left_offset_frames) or 0
    if not offset or offset < 0 then return 0 end
    return offset
end

local function get_audio_item_file_path(item)
    local ok_media, media_item = pcall(function() return item:GetMediaPoolItem() end)
    if not ok_media or not media_item then return nil end
    local ok_path, raw_path = pcall(function() return media_item:GetClipProperty("File Path") end)
    if ok_path and raw_path and tostring(raw_path) ~= "" then
        local file_path = tostring(raw_path)
        if file_exists(file_path) then return file_path end
    end
    return nil
end

local function parse_source_audio_channel_mapping(item, media_item, fps, source_offset_frames, fallback_path)
    local result = {
        file_path = fallback_path,
        audio_mapping_source = "media_pool_file",
        audio_mapping_fallback_reason = "",
        linked_offset_samples = nil,
        audio_channel_index = nil,
        audio_mapping_muted = false
    }
    if not item then
        result.audio_mapping_fallback_reason = "mapping_item_missing"
        return result
    end

    local ok_mapping, raw_mapping = pcall(function() return item:GetSourceAudioChannelMapping() end)
    if not ok_mapping or not raw_mapping or tostring(raw_mapping) == "" then
        result.audio_mapping_fallback_reason = ok_mapping and "mapping_empty" or "mapping_api_unavailable"
        return result
    end

    local mapping = decode_json_text(tostring(raw_mapping))
    if type(mapping) ~= "table" then
        result.audio_mapping_fallback_reason = "mapping_json_error"
        return result
    end

    local track_mapping = mapping.track_mapping or {}
    local mapped_track = track_mapping[tostring(1)] or track_mapping[1]
    if type(mapped_track) ~= "table" then
        for _, candidate in pairs(track_mapping) do
            if type(candidate) == "table" then
                mapped_track = candidate
                break
            end
        end
    end
    if type(mapped_track) ~= "table" then
        result.audio_mapping_fallback_reason = "track_mapping_missing"
        return result
    end

    local channel_idx = nil
    if type(mapped_track.channel_idx) == "table" then
        channel_idx = tonumber(mapped_track.channel_idx[1])
    end
    result.audio_mapping_muted = mapped_track.mute == true
        or tostring(mapped_track.mute or "") == "1"
        or tostring(mapped_track.mute or ""):lower() == "true"
    local linked_audio = mapping.linked_audio or {}
    local embedded_count = tonumber(mapping.embedded_audio_channels) or 0
    local linked_channel_number = channel_idx and (channel_idx - embedded_count) or nil
    local linked_keys = {}
    for key, _ in pairs(linked_audio) do
        linked_keys[#linked_keys + 1] = key
    end
    table.sort(linked_keys, function(a, b) return tonumber(a) < tonumber(b) end)

    local linked_info = nil
    local linked_key = nil
    local linked_local_channel_index = nil
    if linked_channel_number and linked_channel_number > 0 then
        local remaining_channel = linked_channel_number
        for _, key in ipairs(linked_keys) do
            local candidate = linked_audio[key]
            local candidate_channels = tonumber(candidate and candidate.channels) or 1
            if remaining_channel <= candidate_channels then
                linked_info = candidate
                linked_key = key
                linked_local_channel_index = remaining_channel
                break
            end
            remaining_channel = remaining_channel - candidate_channels
        end
    end
    if type(linked_info) ~= "table" or not linked_info.path or tostring(linked_info.path) == "" then
        result.audio_mapping_fallback_reason = "linked_audio_missing"
        return result
    end

    local linked_path = tostring(linked_info.path)
    if not file_exists(linked_path) then
        result.audio_mapping_fallback_reason = "linked_audio_not_found"
        return result
    end

    local sample_rate = 48000
    if media_item then
        local ok_sample_rate, raw_sample_rate = pcall(function() return media_item:GetClipProperty("Sample Rate") end)
        sample_rate = tonumber(ok_sample_rate and raw_sample_rate) or sample_rate
    end
    local linked_offset_samples = tonumber(linked_info.offset) or 0
    local effective_fps = math.max(1, tonumber(fps) or 30)
    local source_frames = math.max(0, tonumber(source_offset_frames) or 0)
    result.file_path = linked_path
    result.audio_mapping_source = "linked_audio"
    result.audio_mapping_fallback_reason = ""
    result.linked_offset_samples = linked_offset_samples
    result.source_start_seconds = math.max(0, (source_frames / effective_fps) + (linked_offset_samples / math.max(1, sample_rate)))
    local linked_path_channel_index = math.max(1, tonumber(linked_local_channel_index) or 1)
    local linked_path_channel_count = 0
    for _, key in ipairs(linked_keys) do
        local candidate = linked_audio[key]
        if type(candidate) == "table" and candidate.path == linked_info.path then
            local candidate_channels = tonumber(candidate.channels) or 1
            linked_path_channel_count = linked_path_channel_count + candidate_channels
            if tonumber(key) < tonumber(linked_key) then
                linked_path_channel_index = linked_path_channel_index + candidate_channels
            end
        end
    end
    result.audio_channel_index = linked_path_channel_count > 1 and linked_path_channel_index or nil
    return result
end

local range_intersects_selection

function timeline_item_is_selected(item)
    local keys = {"Selected", "IsSelected", "selected", "isSelected"}
    for _, key in ipairs(keys) do
        local ok, value = pcall(function() return item:GetProperty(key) end)
        if ok and (value == true or tostring(value) == "1" or tostring(value):lower() == "true") then
            return true
        end
    end
    local ok_props, props = pcall(function() return item:GetProperty() end)
    if ok_props and type(props) == "table" then
        for _, key in ipairs(keys) do
            local value = props[key]
            if value == true or tostring(value) == "1" or tostring(value):lower() == "true" then
                return true
            end
        end
    end
    return false
end

local function add_media_name_tokens(tokens, file_path)
    local name = basename(file_path):gsub("%.[^%.]+$", "")
    local preferred = name:match("(R%d+C%d+)") or name:match("([A-Za-z]+%d+)")
    if preferred and #preferred >= 4 then
        tokens[preferred] = true
    end
    for token in name:gmatch("([%w_]+)") do
        if #token >= 6 then
            tokens[token] = true
        end
    end
end

local function collect_video_match_tokens_for_scope(timeline, scope)
    local tokens = {}
    local ok_track_count, track_count = pcall(function() return timeline:GetTrackCount("video") end)
    track_count = ok_track_count and tonumber(track_count) or 0
    for track_index = 1, track_count do
        local ok_items, items = pcall(function() return timeline:GetItemListInTrack("video", track_index) end)
        items = ok_items and items or {}
        for _, item in ipairs(items or {}) do
            local ok_start, item_start = pcall(function() return item:GetStart() end)
            local ok_end, item_end = pcall(function() return item:GetEnd() end)
            if ok_start and ok_end and range_intersects_selection(item_start, item_end, scope) then
                local ok_media, media_item = pcall(function() return item:GetMediaPoolItem() end)
                if ok_media and media_item then
                    local ok_path, raw_path = pcall(function() return media_item:GetClipProperty("File Path") end)
                    if ok_path and raw_path and tostring(raw_path) ~= "" then
                        add_media_name_tokens(tokens, tostring(raw_path))
                    end
                end
            end
        end
    end
    return tokens
end

local function score_audio_video_match(file_path, tokens)
    local name = basename(file_path)
    local score = 0
    for token in pairs(tokens or {}) do
        if token ~= "" and name:find(token, 1, true) then
            score = score + #token
        end
    end
    return score
end

local function get_audio_track_display_name(timeline, track_index)
    local ok_name, raw_name = pcall(function() return timeline:GetTrackName("audio", track_index) end)
    local name = ok_name and trim_text(raw_name) or ""
    if name == "" then
        return string.format("A%d", tonumber(track_index) or 0)
    end
    return string.format("A%d %s", tonumber(track_index) or 0, name)
end

local function compare_audio_source_priority(a, b)
    if a.is_selected ~= b.is_selected then return a.is_selected == true end
    local a_muted = a.audio_mapping_muted == true
    local b_muted = b.audio_mapping_muted == true
    if a_muted ~= b_muted then return a_muted == false end
    local a_match = tonumber(a.video_match_score) or 0
    local b_match = tonumber(b.video_match_score) or 0
    if a_match ~= b_match then return a_match > b_match end
    local a_overlap = tonumber(a.overlap_frames) or 0
    local b_overlap = tonumber(b.overlap_frames) or 0
    if a_overlap ~= b_overlap then return a_overlap > b_overlap end
    return (tonumber(a.item_index) or 0) < (tonumber(b.item_index) or 0)
end

local function collect_audio_sources_for_scope(timeline, scope, fps)
    if type(scope) ~= "table" or scope.mode ~= WORK_SCOPE_MODE_SELECTION then
        return nil, "请先用 I/O 设置 In/Out 选区"
    end
    local scope_start = tonumber(scope.start_frame)
    local scope_end = tonumber(scope.end_frame)
    if not scope_start or not scope_end or scope_end <= scope_start then
        return nil, "请先用 I/O 设置 In/Out 选区"
    end
    local ok_track_count, track_count = pcall(function() return timeline:GetTrackCount("audio") end)
    track_count = ok_track_count and tonumber(track_count) or 0
    if track_count <= 0 then
        return nil, "时间线没有音频轨"
    end

    local audio_sources = {}
    local video_tokens = collect_video_match_tokens_for_scope(timeline, scope)
    local effective_fps = math.max(1, tonumber(fps) or 30)
    for track_index = 1, track_count do
        local track_display_name = get_audio_track_display_name(timeline, track_index)
        local ok_items, items = pcall(function() return timeline:GetItemListInTrack("audio", track_index) end)
        items = ok_items and items or {}
        for item_index, item in ipairs(items or {}) do
            local ok_start, item_start = pcall(function() return item:GetStart() end)
            local ok_end, item_end = pcall(function() return item:GetEnd() end)
            item_start = ok_start and tonumber(item_start) or nil
            item_end = ok_end and tonumber(item_end) or nil
            if item_start and item_end and item_end > item_start then
                local overlap_start = math.max(scope_start, item_start)
                local overlap_end = math.min(scope_end, item_end)
                local overlap_frames = overlap_end - overlap_start
                if overlap_frames > 0 then
                    local file_path = get_audio_item_file_path(item)
                    if file_path then
                        local source_offset_frames = get_audio_item_source_offset_frames(item)
                        local ok_media, media_item = pcall(function() return item:GetMediaPoolItem() end)
                        media_item = ok_media and media_item or nil
                        local mapped_audio = parse_source_audio_channel_mapping(item, media_item, effective_fps, source_offset_frames, file_path)
                        file_path = mapped_audio.file_path or file_path
                        local item_source_start_seconds = tonumber(mapped_audio.source_start_seconds) or (source_offset_frames / effective_fps)
                        local source_start_seconds = item_source_start_seconds + ((overlap_start - item_start) / effective_fps)
                        local source_end_seconds = source_start_seconds + (overlap_frames / effective_fps)
                        local candidate = {
                            file_path = file_path,
                            file_name = basename(file_path),
                            track_index = track_index,
                            track_name = track_display_name,
                            item_index = item_index,
                            item_start_frame = item_start,
                            item_end_frame = item_end,
                            start_frame = overlap_start,
                            end_frame = overlap_end,
                            source_offset_frames = source_offset_frames,
                            source_start_seconds = source_start_seconds,
                            source_end_seconds = source_end_seconds,
                            overlap_frames = overlap_frames
                        }
                        candidate.audio_mapping_source = mapped_audio.audio_mapping_source or "media_pool_file"
                        candidate.audio_mapping_fallback_reason = mapped_audio.audio_mapping_fallback_reason or ""
                        candidate.linked_offset_samples = mapped_audio.linked_offset_samples
                        candidate.audio_channel_index = mapped_audio.audio_channel_index
                        candidate.audio_mapping_muted = mapped_audio.audio_mapping_muted == true
                        candidate.is_selected = timeline_item_is_selected(item)
                        candidate.video_match_score = score_audio_video_match(file_path, video_tokens)
                        audio_sources[#audio_sources + 1] = candidate
                    end
                end
            end
        end
    end

    table.sort(audio_sources, function(a, b)
        local a_track = tonumber(a.track_index) or 0
        local b_track = tonumber(b.track_index) or 0
        if a_track ~= b_track then return a_track < b_track end
        return compare_audio_source_priority(a, b)
    end)

    if #audio_sources == 0 then
        return nil, "未找到与选区重叠的本地音频片段"
    end
    return audio_sources
end

local function build_audio_track_options_for_dialog(audio_sources)
    if type(audio_sources) ~= "table" then
        return audio_sources
    end

    local sources_by_track = {}
    for _, source in ipairs(audio_sources) do
        local track_index = tonumber(source.track_index) or 0
        if not sources_by_track[track_index] then sources_by_track[track_index] = {} end
        sources_by_track[track_index][#sources_by_track[track_index] + 1] = source
    end

    local track_options = {}
    for _, grouped_sources in pairs(sources_by_track) do
        local usable_sources = {}
        for _, source in ipairs(grouped_sources) do
            if source.audio_mapping_muted ~= true then
                usable_sources[#usable_sources + 1] = source
            end
        end
        local sources = #usable_sources > 0 and usable_sources or grouped_sources
        table.sort(sources, function(a, b)
            local a_start = tonumber(a.start_frame) or 0
            local b_start = tonumber(b.start_frame) or 0
            if a_start ~= b_start then return a_start < b_start end
            local a_end = tonumber(a.end_frame) or 0
            local b_end = tonumber(b.end_frame) or 0
            if a_end ~= b_end then return a_end < b_end end
            return (tonumber(a.item_index) or 0) < (tonumber(b.item_index) or 0)
        end)

        local source = sources[1]
        local label = tostring(source.track_name or "")
        if label == "" then
            label = string.format("A%d", tonumber(source.track_index) or 0)
        end
        if #usable_sources == 0 and source.audio_mapping_muted == true then
            label = label .. "（静音）"
        end
        source.display_label = label
        source.track_sources = sources
        track_options[#track_options + 1] = source
    end

    table.sort(track_options, function(a, b)
        return (tonumber(a.track_index) or 0) < (tonumber(b.track_index) or 0)
    end)
    if #track_options > 0 then
        return track_options
    end
    return audio_sources
end

function range_intersects_selection(item_start, item_end, scope)
    local scope_start = tonumber(scope and scope.start_frame)
    local scope_end = tonumber(scope and scope.end_frame)
    if not scope_start or not scope_end then return false end
    local start_frame = tonumber(item_start)
    local end_frame = tonumber(item_end)
    if not start_frame or not end_frame then return false end
    return math.max(start_frame, end_frame) > scope_start and math.min(start_frame, end_frame) < scope_end
end

local get_subtitle_track_items

local function clear_subtitle_track_clips(timeline, track_index)
    local items, items_err = get_subtitle_track_items(track_index, timeline)
    if not items then return false, items_err end
    if #items == 0 then return true, 0 end
    local initial_count = #items
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
        return false, "清空目标字幕轨失败"
    end
    local remaining_items = select(1, get_subtitle_track_items(track_index, timeline)) or {}
    if #remaining_items > 0 then
        return false, "轨道 " .. tostring(track_index) .. " 仍残留 " .. tostring(#remaining_items) .. " 条字幕"
    end
    return true, initial_count
end

local function backup_target_track(timeline, track_index, fps, base_frame)
    local items, items_err = get_subtitle_track_items(track_index, timeline)
    if not items then return nil, items_err or "无法读取目标字幕轨" end
    if #items == 0 then return nil end
    local path = temp_dir() .. "/Backup_GenerateSelection_" .. os.date("%Y%m%d_%H%M%S") .. ".srt"
    local file = io.open(path, "w")
    if not file then return nil, "无法创建目标字幕轨备份" end
    local index = 1
    for _, item in ipairs(items) do
        local ok_start, start_frame = pcall(function() return item:GetStart() end)
        local ok_end, end_frame = pcall(function() return item:GetEnd() end)
        local ok_name, name = pcall(function() return item:GetName() end)
        if ok_start and ok_end then
            file:write(tostring(index) .. "\n")
            file:write(frames_to_srt_time(start_frame, fps, base_frame) .. " --> " .. frames_to_srt_time(end_frame, fps, base_frame) .. "\n")
            file:write(tostring(ok_name and name or "") .. "\n\n")
            index = index + 1
        end
    end
    file:close()
    return path, nil
end

local function get_subtitle_track_type_and_count(timeline)
    if not timeline then return "subtitle", 0 end
    local candidates = {"subtitle", 3, "Subtitle"}
    for _, track_type in ipairs(candidates) do
        local ok, track_count = pcall(function() return timeline:GetTrackCount(track_type) end)
        if ok and track_count ~= nil and track_count ~= false then
            return track_type, tonumber(track_count) or 0
        end
    end
    return "subtitle", 0
end

function get_subtitle_track_items(track_index, timeline)
    if not track_index or track_index < 1 then
        return nil, "字幕轨索引无效"
    end
    local track_type, track_count = get_subtitle_track_type_and_count(timeline)
    if track_index > track_count then
        return nil, "轨道 " .. tostring(track_index) .. " 不存在", track_count, track_type
    end
    local tried = {}
    for _, candidate in ipairs({track_type, "subtitle", 3, "Subtitle"}) do
        local key = type(candidate) .. ":" .. tostring(candidate)
        if not tried[key] then
            tried[key] = true
            local ok_items, items = pcall(function() return timeline:GetItemListInTrack(candidate, track_index) end)
            if ok_items then
                return items or {}, nil, track_count, candidate
            end
        end
    end
    return nil, "无法读取轨道 " .. tostring(track_index) .. " 的字幕片段", track_count, track_type
end

local function add_subtitle_track(timeline)
    for _, track_type in ipairs({"subtitle", "Subtitle", 3}) do
        local ok, ret = pcall(function() return timeline:AddTrack(track_type) end)
        if ok and ret ~= false then return true end
    end
    return false
end

local function ensure_subtitle_track_exists(track_index, timeline)
    if not track_index or track_index < 1 then
        return nil, "字幕轨索引无效"
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

local function unlock_all_subtitle_tracks(timeline)
    local track_type, track_count = get_subtitle_track_type_and_count(timeline)
    for track_index = 1, track_count do
        pcall(function() return timeline:SetTrackLock(track_type, track_index, false) end)
    end
    return true
end

local function isolate_subtitle_target_track(track_index, timeline)
    local track_type, track_count = get_subtitle_track_type_and_count(timeline)
    if track_index > track_count then
        return false, "目标字幕轨不存在"
    end
    for index = 1, track_count do
        local desired_enabled = index == track_index
        pcall(function() return timeline:SetTrackEnable(track_type, index, desired_enabled) end)
    end
    local ok_enabled, enabled = pcall(function() return timeline:GetIsTrackEnabled(track_type, track_index) end)
    if not ok_enabled or enabled == false then
        return false, "目标字幕轨未成功启用"
    end
    return true
end

local function format_audio_source_label(audio_source, fps)
    if audio_source.display_label and tostring(audio_source.display_label) ~= "" then
        return tostring(audio_source.display_label)
    end
    local overlap_seconds = (tonumber(audio_source.overlap_frames) or 0) / math.max(1, tonumber(fps) or 30)
    local marks = {}
    if audio_source.is_selected then marks[#marks + 1] = "选中" end
    if (tonumber(audio_source.video_match_score) or 0) > 0 then marks[#marks + 1] = "匹配画面" end
    if audio_source.audio_mapping_muted == true then marks[#marks + 1] = "mute" end
    if tostring(audio_source.audio_mapping_source or "") ~= "" then marks[#marks + 1] = tostring(audio_source.audio_mapping_source) end
    if tonumber(audio_source.audio_channel_index) then marks[#marks + 1] = "ch" .. tostring(math.floor(tonumber(audio_source.audio_channel_index) or 0)) end
    local prefix = #marks > 0 and ("[" .. table.concat(marks, "/") .. "] ") or ""
    return string.format(
        "%sA%d  #%d  %s  %.1fs  %.3f-%.3fs",
        prefix,
        tonumber(audio_source.track_index) or 0,
        tonumber(audio_source.item_index) or 0,
        tostring(audio_source.file_name or audio_source.file_path or ""),
        overlap_seconds,
        tonumber(audio_source.source_start_seconds) or 0,
        tonumber(audio_source.source_end_seconds) or 0
    )
end

local function write_selected_audio_source_diagnostic(path, audio_source, scope, source_optimization)
    if not path or not audio_source then return false end
    local selected_audio_sources = audio_source[1] and audio_source or {audio_source}
    local first_source = selected_audio_sources[1] or audio_source
    local sources = {}
    for _, selected_source in ipairs(selected_audio_sources) do
        for _, source in ipairs(selected_source.track_sources or {selected_source}) do
            sources[#sources + 1] = source
        end
    end
    local lines = {
        "{",
        '  "audio_source_mode": "source_audio",',
        string.format('  "track_index": %d,', tonumber(first_source.track_index) or 0),
        string.format('  "track_name": "%s",', json_escape(first_source.track_name or first_source.display_label)),
        '  "selected_tracks": ['
    }
    for index, selected_source in ipairs(selected_audio_sources) do
        lines[#lines + 1] = string.format(
            '    {"track_index": %d, "track_name": "%s", "source_count": %d}%s',
            tonumber(selected_source.track_index) or 0,
            json_escape(selected_source.track_name or selected_source.display_label),
            #(selected_source.track_sources or {selected_source}),
            index < #selected_audio_sources and "," or ""
        )
    end
    lines[#lines + 1] = "  ],"
    lines[#lines + 1] = string.format('  "source_count": %d,', #sources)
    local optimization = source_optimization or {}
    lines[#lines + 1] = '  "source_optimization": {'
    lines[#lines + 1] = string.format('    "raw_source_count": %d,', tonumber(optimization.raw_source_count) or #sources)
    lines[#lines + 1] = string.format('    "deduped_source_count": %d,', tonumber(optimization.deduped_source_count) or #sources)
    lines[#lines + 1] = string.format('    "merged_source_count": %d,', tonumber(optimization.merged_source_count) or #sources)
    lines[#lines + 1] = string.format('    "duplicate_source_count": %d,', tonumber(optimization.duplicate_source_count) or 0)
    lines[#lines + 1] = string.format('    "merged_source_group_count": %d,', tonumber(optimization.merged_source_group_count) or 0)
    lines[#lines + 1] = string.format('    "merged_child_source_count": %d', tonumber(optimization.merged_child_source_count) or 0)
    lines[#lines + 1] = "  },"
    lines[#lines + 1] =
        '  "sources": ['
    for index, source in ipairs(sources) do
        lines[#lines + 1] = "    {"
        lines[#lines + 1] = string.format('      "track_index": %d,', tonumber(source.track_index) or 0)
        lines[#lines + 1] = string.format('      "track_name": "%s",', json_escape(source.track_name or first_source.display_label))
        lines[#lines + 1] = string.format('      "item_index": %d,', tonumber(source.item_index) or 0)
        lines[#lines + 1] = string.format('      "file_path": "%s",', json_escape(source.file_path))
        lines[#lines + 1] = string.format('      "file_name": "%s",', json_escape(source.file_name))
        lines[#lines + 1] = string.format('      "audio_mapping_source": "%s",', json_escape(source.audio_mapping_source))
        lines[#lines + 1] = string.format('      "audio_mapping_fallback_reason": "%s",', json_escape(source.audio_mapping_fallback_reason))
        lines[#lines + 1] = string.format('      "audio_mapping_muted": %s,', source.audio_mapping_muted == true and "true" or "false")
        lines[#lines + 1] = string.format('      "audio_channel_index": %s,', tonumber(source.audio_channel_index) and tostring(math.floor(tonumber(source.audio_channel_index) or 0)) or "null")
        lines[#lines + 1] = string.format('      "linked_offset_samples": %s,', tonumber(source.linked_offset_samples) and tostring(tonumber(source.linked_offset_samples)) or "null")
        lines[#lines + 1] = string.format('      "source_offset_frames": %d,', tonumber(source.source_offset_frames) or 0)
        lines[#lines + 1] = string.format('      "source_start_seconds": %.6f,', tonumber(source.source_start_seconds) or 0)
        lines[#lines + 1] = string.format('      "source_end_seconds": %.6f,', tonumber(source.source_end_seconds) or 0)
        lines[#lines + 1] = string.format('      "timeline_start_frame": %d,', tonumber(source.start_frame) or 0)
        lines[#lines + 1] = string.format('      "timeline_end_frame": %d', tonumber(source.end_frame) or 0)
        lines[#lines + 1] = index < #sources and "    }," or "    }"
    end
    lines[#lines + 1] = "  ],"
    lines[#lines + 1] = string.format('  "selected_timeline_start_frame": %d,', tonumber(first_source.start_frame) or 0)
    lines[#lines + 1] = string.format('  "selected_timeline_end_frame": %d,', tonumber(first_source.end_frame) or 0)
    lines[#lines + 1] = string.format('  "timeline_start_frame": %d,', tonumber(scope and scope.timeline_start_frame) or 0)
    lines[#lines + 1] = string.format('  "scope_start_frame": %d,', tonumber(scope and scope.start_frame) or 0)
    lines[#lines + 1] = string.format('  "scope_end_frame": %d', tonumber(scope and scope.end_frame) or 0)
    lines[#lines + 1] = "}"
    return write_text_file(path, table.concat(lines, "\n") .. "\n")
end

local function collect_selected_track_sources(selected_audio_sources)
    local selected_track_sources = {}
    for track_order, audio_source in ipairs(selected_audio_sources or {}) do
        for _, source in ipairs(audio_source.track_sources or {audio_source}) do
            source.track_order = track_order
            source.source_order = #selected_track_sources + 1
            selected_track_sources[#selected_track_sources + 1] = source
        end
    end
    table.sort(selected_track_sources, function(a, b)
        local a_order = tonumber(a.track_order) or 0
        local b_order = tonumber(b.track_order) or 0
        if a_order ~= b_order then return a_order < b_order end
        local a_start = tonumber(a.start_frame) or 0
        local b_start = tonumber(b.start_frame) or 0
        if a_start ~= b_start then return a_start < b_start end
        return (tonumber(a.item_index) or 0) < (tonumber(b.item_index) or 0)
    end)
    return selected_track_sources
end

local function clone_audio_source(source)
    local cloned = {}
    for key, value in pairs(source or {}) do
        if key ~= "track_sources" then cloned[key] = value end
    end
    return cloned
end

local function rounded_number_key(value, precision)
    local multiplier = math.pow(10, tonumber(precision) or 3)
    return tostring(math.floor((tonumber(value) or 0) * multiplier + 0.5) / multiplier)
end

local function duplicate_audio_source_key(source)
    return table.concat({
        tostring(source and source.file_path or ""),
        rounded_number_key(source and source.source_start_seconds, 3),
        rounded_number_key(source and source.source_end_seconds, 3),
        tostring(math.floor(tonumber(source and source.start_frame) or 0)),
        tostring(math.floor(tonumber(source and source.audio_channel_index) or 0)),
    }, "|")
end

local function optimize_selected_track_sources_for_generation(selected_track_sources, fps)
    -- Only exact cross-track duplicates are removed here; keeping clip boundaries avoids long-window ASR drift.
    local stats = {
        raw_source_count = #(selected_track_sources or {}),
        deduped_source_count = 0,
        duplicate_source_count = 0,
        merged_source_count = 0,
        merged_source_group_count = 0,
        merged_child_source_count = 0
    }
    local deduped = {}
    local seen = {}
    for _, source in ipairs(selected_track_sources or {}) do
        local key = duplicate_audio_source_key(source)
        if seen[key] then
            stats.duplicate_source_count = stats.duplicate_source_count + 1
        else
            seen[key] = true
            deduped[#deduped + 1] = clone_audio_source(source)
        end
    end
    stats.deduped_source_count = #deduped
    table.sort(deduped, function(a, b)
        local a_order = tonumber(a.track_order) or 0
        local b_order = tonumber(b.track_order) or 0
        if a_order ~= b_order then return a_order < b_order end
        local a_start = tonumber(a.start_frame) or 0
        local b_start = tonumber(b.start_frame) or 0
        if a_start ~= b_start then return a_start < b_start end
        return (tonumber(a.item_index) or 0) < (tonumber(b.item_index) or 0)
    end)

    for _, source in ipairs(deduped) do
        source.merged_source_count = 1
        source.merged_item_indices = tostring(source.item_index or "")
    end
    stats.merged_source_count = #deduped
    return deduped, stats
end

local function write_generate_batch_plan(path, selected_track_sources, fps)
    local lines = {"{", '  "batches": ['}
    for index, source in ipairs(selected_track_sources or {}) do
        local audio_channel_index = tonumber(source.audio_channel_index)
        lines[#lines + 1] = "    {"
        lines[#lines + 1] = string.format('      "batch_id": "track%d_item%d_%d",', tonumber(source.track_index) or 0, tonumber(source.item_index) or 0, index)
        lines[#lines + 1] = string.format('      "audio": "%s",', json_escape(source.file_path))
        lines[#lines + 1] = string.format('      "source_start": %.6f,', tonumber(source.source_start_seconds) or 0)
        lines[#lines + 1] = string.format('      "source_end": %.6f,', tonumber(source.source_end_seconds) or 0)
        lines[#lines + 1] = string.format('      "timeline_start_frame": %d,', tonumber(source.start_frame) or 0)
        lines[#lines + 1] = string.format('      "timeline_end_frame": %d,', tonumber(source.end_frame) or 0)
        lines[#lines + 1] = string.format('      "fps": %.6f,', tonumber(fps) or 30)
        lines[#lines + 1] = string.format('      "audio_channel_index": %s,', audio_channel_index and tostring(math.floor(audio_channel_index)) or "null")
        lines[#lines + 1] = string.format('      "track_order": %d,', tonumber(source.track_order) or 0)
        lines[#lines + 1] = string.format('      "track_index": %d,', tonumber(source.track_index) or 0)
        lines[#lines + 1] = string.format('      "track_name": "%s",', json_escape(source.track_name or ""))
        lines[#lines + 1] = string.format('      "item_index": %d', tonumber(source.item_index) or 0)
        lines[#lines + 1] = index < #(selected_track_sources or {}) and "    }," or "    }"
    end
    lines[#lines + 1] = "  ]"
    lines[#lines + 1] = "}"
    return write_text_file(path, table.concat(lines, "\n") .. "\n")
end

local function parse_generated_json_subtitle_rows(json_path)
    local payload_text = read_text_file(json_path)
    if not payload_text then return nil, "无法读取生成 JSON" end
    local payload, decode_err = decode_json_text(payload_text)
    if not payload then return nil, decode_err or "生成 JSON 解析失败" end
    if payload.ok == false then return nil, tostring(payload.error or "ASR helper 执行失败") end
    local rows = {}
    for _, row in ipairs(payload.subtitle_rows or {}) do
        local start_frame = tonumber(row.start_frame)
        local end_frame = tonumber(row.end_frame)
        local text = trim_text(row.text or "")
        if start_frame and end_frame and end_frame > start_frame and text ~= "" then
            rows[#rows + 1] = {
                index = #rows + 1,
                start_frame = start_frame,
                end_frame = end_frame,
                text = text,
                batch_id = tostring(row.batch_id or ""),
                track_order = tonumber(row.track_order) or 0,
                track_index = tonumber(row.track_index) or 0,
                item_index = tonumber(row.item_index) or 0,
                speaker_track_index = tonumber(row.speaker_track_index),
                speaker_score_db = tonumber(row.speaker_score_db),
                speaker_decision = tostring(row.speaker_decision or "")
            }
        end
    end
    if #rows == 0 then return nil, "生成 JSON 没有可写回字幕" end
    return rows
end

local function subtract_existing_ranges(row, accepted_ranges)
    local intervals = {{start_frame = tonumber(row.start_frame) or 0, end_frame = tonumber(row.end_frame) or 0}}
    for _, range in ipairs(accepted_ranges or {}) do
        local next_intervals = {}
        local range_start = tonumber(range.start_frame) or 0
        local range_end = tonumber(range.end_frame) or 0
        for _, interval in ipairs(intervals) do
            local start_frame = tonumber(interval.start_frame) or 0
            local end_frame = tonumber(interval.end_frame) or 0
            if range_end <= start_frame or range_start >= end_frame then
                next_intervals[#next_intervals + 1] = interval
            else
                if range_start > start_frame then
                    next_intervals[#next_intervals + 1] = {start_frame = start_frame, end_frame = math.min(range_start, end_frame)}
                end
                if range_end < end_frame then
                    next_intervals[#next_intervals + 1] = {start_frame = math.max(range_end, start_frame), end_frame = end_frame}
                end
            end
        end
        intervals = next_intervals
        if #intervals == 0 then break end
    end
    return intervals
end

local function merge_generated_rows_with_track_priority(rows, scope)
    local sorted_rows = {}
    for _, row in ipairs(rows or {}) do sorted_rows[#sorted_rows + 1] = row end
    table.sort(sorted_rows, function(a, b)
        local a_order = tonumber(a.track_order) or tonumber(a.track_index) or 0
        local b_order = tonumber(b.track_order) or tonumber(b.track_index) or 0
        if a_order ~= b_order then return a_order < b_order end
        local a_start = tonumber(a.start_frame) or 0
        local b_start = tonumber(b.start_frame) or 0
        if a_start ~= b_start then return a_start < b_start end
        return (tonumber(a.end_frame) or 0) < (tonumber(b.end_frame) or 0)
    end)

    local accepted_ranges = {}
    local merged_rows = {}
    local scope_start = tonumber(scope and scope.start_frame)
    local scope_end = tonumber(scope and scope.end_frame)
    for _, row in ipairs(sorted_rows) do
        local row_start = tonumber(row.start_frame)
        local row_end = tonumber(row.end_frame)
        if row_start and row_end and row_end > row_start then
            if scope_start then row_start = math.max(row_start, scope_start) end
            if scope_end then row_end = math.min(row_end, scope_end) end
            if row_end > row_start then
                local candidate = {
                    start_frame = row_start,
                    end_frame = row_end,
                    text = row.text,
                    track_order = row.track_order,
                    track_index = row.track_index,
                    item_index = row.item_index
                }
                for _, interval in ipairs(subtract_existing_ranges(candidate, accepted_ranges)) do
                    if interval.end_frame > interval.start_frame then
                        local kept = {
                            start_frame = interval.start_frame,
                            end_frame = interval.end_frame,
                            text = candidate.text,
                            track_order = candidate.track_order,
                            track_index = candidate.track_index,
                            item_index = candidate.item_index
                        }
                        merged_rows[#merged_rows + 1] = kept
                        accepted_ranges[#accepted_ranges + 1] = {start_frame = kept.start_frame, end_frame = kept.end_frame}
                    end
                end
            end
        end
    end
    table.sort(merged_rows, function(a, b)
        local a_start = tonumber(a.start_frame) or 0
        local b_start = tonumber(b.start_frame) or 0
        if a_start ~= b_start then return a_start < b_start end
        return (tonumber(a.end_frame) or 0) < (tonumber(b.end_frame) or 0)
    end)
    for index, row in ipairs(merged_rows) do row.index = index end
    if #merged_rows == 0 then return nil, "按轨优先去重后没有可写回字幕" end
    return merged_rows
end

local function merge_generated_rows_for_live_writeback(rows, scope)
    for _, row in ipairs(rows or {}) do
        if not tonumber(row.speaker_track_index) then
            return merge_generated_rows_with_track_priority(rows, scope)
        end
    end

    local sorted_rows = {}
    local scope_start = tonumber(scope and scope.start_frame)
    local scope_end = tonumber(scope and scope.end_frame)
    for _, row in ipairs(rows or {}) do
        local start_frame = tonumber(row.start_frame)
        local end_frame = tonumber(row.end_frame)
        if start_frame and end_frame then
            if scope_start then start_frame = math.max(start_frame, scope_start) end
            if scope_end then end_frame = math.min(end_frame, scope_end) end
            if end_frame > start_frame then
                local candidate = {}
                for key, value in pairs(row) do candidate[key] = value end
                candidate.start_frame = start_frame
                candidate.end_frame = end_frame
                sorted_rows[#sorted_rows + 1] = candidate
            end
        end
    end
    table.sort(sorted_rows, function(a, b)
        local a_start = tonumber(a.start_frame) or 0
        local b_start = tonumber(b.start_frame) or 0
        if a_start ~= b_start then return a_start < b_start end
        local a_score = tonumber(a.speaker_score_db) or 0
        local b_score = tonumber(b.speaker_score_db) or 0
        if a_score ~= b_score then return a_score > b_score end
        return (tonumber(a.end_frame) or 0) < (tonumber(b.end_frame) or 0)
    end)

    local merged_rows = {}
    for _, row in ipairs(sorted_rows) do
        local previous = merged_rows[#merged_rows]
        if previous and tonumber(row.start_frame) < tonumber(previous.end_frame) then
            if tonumber(row.end_frame) <= tonumber(previous.end_frame) then
                if (tonumber(row.speaker_score_db) or 0) > (tonumber(previous.speaker_score_db) or 0) then
                    merged_rows[#merged_rows] = row
                end
                goto continue_live_row
            end
            previous.end_frame = math.max(tonumber(previous.start_frame) + 1, tonumber(row.start_frame))
            row.start_frame = tonumber(previous.end_frame)
        end
        if tonumber(row.end_frame) > tonumber(row.start_frame) then
            merged_rows[#merged_rows + 1] = row
        end
        ::continue_live_row::
    end
    for index, row in ipairs(merged_rows) do row.index = index end
    if #merged_rows == 0 then return nil, "现场模式没有可写回字幕" end
    return merged_rows
end

local function show_audio_track_selection_dialog(audio_sources, scope, fps)
    if not dispatcher or not ui then
        return nil, nil, nil, nil, "无法初始化 Resolve UI"
    end
    if type(audio_sources) ~= "table" or #audio_sources == 0 then
        return nil, nil, nil, nil, "未找到与选区重叠的本地音频片段"
    end

    local selected_audio_sources = nil
    -- 模式选择下拉已移除，固定使用现场模式（单轨自然退化为单人识别）
    local subtitle_mode = "live"
    -- 字幕长度：标准（≤25字）/ 短视频（≤10字），默认标准。仅控制断句粒度
    -- （每条字幕最大字数），不涉及回声消除或断句算法本体。
    -- UI 用两个互斥的小勾选项代替下拉框/大按钮：与上方音频轨道列表同款的
    -- ☑/☐ 勾选样式，紧凑、一眼看出当前选中项。
    local SUBTITLE_LENGTH_OPTIONS = {
        {label = "标准（≤25字）", max_chars = 25},
        {label = "短视频（≤10字）", max_chars = 10},
    }
    local SUBTITLE_LENGTH_SELECTED_PREFIX = TRACK_CHECKED_MARK .. " "
    local SUBTITLE_LENGTH_UNSELECTED_PREFIX = TRACK_UNCHECKED_MARK .. " "
    local selected_length_index = 1
    local selected_max_chars = SUBTITLE_LENGTH_OPTIONS[1].max_chars
    -- 识别模型：Qwen（本地，backend "auto"，默认）/ 豆包（云端，backend "doubao_asr"）。
    -- 仅决定把哪个 --backend 传给 ASR helper，不改断句/回声/对齐算法本体。默认
    -- Qwen(auto) 时与现状完全一致（build 传 DEFAULT_ASR_BACKEND == "auto"）。UI 复用
    -- 与字幕长度同款的 ☑/☐ 互斥小勾选样式。
    local SUBTITLE_ENGINE_OPTIONS = {
        {label = "Qwen（本地）", backend = "auto"},
        {label = "豆包（云端）", backend = "doubao_asr"},
    }
    local SUBTITLE_ENGINE_SELECTED_PREFIX = TRACK_CHECKED_MARK .. " "
    local SUBTITLE_ENGINE_UNSELECTED_PREFIX = TRACK_UNCHECKED_MARK .. " "
    local selected_engine_index = 1
    local selected_backend = SUBTITLE_ENGINE_OPTIONS[1].backend
    local dialog_cancelled = false
    local track_rows = {}
    local selection_window = dispatcher:AddWindow({
        ID = "GenerateSelectionWindow",
        WindowTitle = "SubFix · 生成选区字幕",
        Geometry = {460, 250, 420, 284},
    },
    ui:VGroup{
        Spacing = 8,
        ContentsMargins = 12,
        ui:Label{ID = "GenerateSelectionInfoLabel", Text = "选择用于识别的音频轨道", Weight = 0},
        ui:Tree{
            ID = "GenerateAudioTrackTree",
            Weight = 1,
            MinimumSize = {0, 90},
            Events = {ItemClicked = true}
        },
        ui:HGroup{
            Weight = 0,
            Spacing = 8,
            ui:Label{Text = "字幕长度：", Weight = 0},
            ui:Button{ID = "GenerateSubtitleLengthStandardBtn", Text = "标准（≤25字）", Weight = 0, MinimumSize = {0, 20}},
            ui:Button{ID = "GenerateSubtitleLengthShortBtn", Text = "短视频（≤10字）", Weight = 0, MinimumSize = {0, 20}},
            ui:HGap(0, 1)
        },
        ui:HGroup{
            Weight = 0,
            Spacing = 8,
            ui:Label{Text = "识别模型：", Weight = 0},
            ui:Button{ID = "GenerateSubtitleEngineQwenBtn", Text = "Qwen（本地）", Weight = 0, MinimumSize = {0, 20}},
            ui:Button{ID = "GenerateSubtitleEngineDoubaoBtn", Text = "豆包（云端）", Weight = 0, MinimumSize = {0, 20}},
            ui:HGap(0, 1)
        },
        ui:Label{
            ID = "GenerateSelectionRangeLabel",
            Text = string.format(
                "%s：%d - %d%s",
                tostring(scope.source_label or "范围"),
                tonumber(scope.start_frame) or 0,
                tonumber(scope.end_frame) or 0,
                scope.item_name and scope.item_name ~= "" and (" · " .. tostring(scope.item_name)) or ""
            ),
            Weight = 0
        },
        ui:HGroup{
            Weight = 0,
            Spacing = 8,
            ui:HGap(0, 1),
            ui:Button{ID = "GenerateSelectionConfirmBtn", Text = "生成", Weight = 0, MinimumSize = {88, 28}},
            ui:Button{ID = "GenerateSelectionCancelBtn", Text = "取消", Weight = 0, MinimumSize = {88, 28}}
        }
    })

    local items = selection_window:GetItems()
    local track_tree = items and items.GenerateAudioTrackTree or nil
    if not track_tree then return nil, nil, nil, nil, "无法初始化音频轨道列表" end
    pcall(function() track_tree.ColumnCount = 2 end)
    pcall(function() track_tree.HeaderHidden = true end)
    pcall(function() track_tree.RootIsDecorated = false end)
    pcall(function() track_tree.ItemsExpandable = false end)
    pcall(function() track_tree.ColumnWidth[0] = 28 end)
    pcall(function() track_tree.ColumnWidth[1] = 340 end)

    local length_standard_btn = items and items.GenerateSubtitleLengthStandardBtn or nil
    local length_short_btn = items and items.GenerateSubtitleLengthShortBtn or nil

    local function refresh_subtitle_length_buttons()
        if length_standard_btn then
            local prefix = selected_length_index == 1 and SUBTITLE_LENGTH_SELECTED_PREFIX or SUBTITLE_LENGTH_UNSELECTED_PREFIX
            pcall(function() length_standard_btn.Text = prefix .. SUBTITLE_LENGTH_OPTIONS[1].label end)
        end
        if length_short_btn then
            local prefix = selected_length_index == 2 and SUBTITLE_LENGTH_SELECTED_PREFIX or SUBTITLE_LENGTH_UNSELECTED_PREFIX
            pcall(function() length_short_btn.Text = prefix .. SUBTITLE_LENGTH_OPTIONS[2].label end)
        end
    end

    local function select_subtitle_length(index)
        selected_length_index = index
        refresh_subtitle_length_buttons()
    end

    refresh_subtitle_length_buttons()

    local function read_selected_max_chars()
        local option = SUBTITLE_LENGTH_OPTIONS[selected_length_index]
        return option and option.max_chars or SUBTITLE_LENGTH_OPTIONS[1].max_chars
    end

    local engine_qwen_btn = items and items.GenerateSubtitleEngineQwenBtn or nil
    local engine_doubao_btn = items and items.GenerateSubtitleEngineDoubaoBtn or nil

    local function refresh_subtitle_engine_buttons()
        if engine_qwen_btn then
            local prefix = selected_engine_index == 1 and SUBTITLE_ENGINE_SELECTED_PREFIX or SUBTITLE_ENGINE_UNSELECTED_PREFIX
            pcall(function() engine_qwen_btn.Text = prefix .. SUBTITLE_ENGINE_OPTIONS[1].label end)
        end
        if engine_doubao_btn then
            local prefix = selected_engine_index == 2 and SUBTITLE_ENGINE_SELECTED_PREFIX or SUBTITLE_ENGINE_UNSELECTED_PREFIX
            pcall(function() engine_doubao_btn.Text = prefix .. SUBTITLE_ENGINE_OPTIONS[2].label end)
        end
    end

    local function select_subtitle_engine(index)
        selected_engine_index = index
        refresh_subtitle_engine_buttons()
    end

    refresh_subtitle_engine_buttons()

    local function read_selected_backend()
        local option = SUBTITLE_ENGINE_OPTIONS[selected_engine_index]
        return option and option.backend or SUBTITLE_ENGINE_OPTIONS[1].backend
    end

    -- 豆包密钥配置子窗口：点"豆包（云端）"且未配置密钥时按需弹出。复用父对话框
    -- 已在跑的 dispatcher:RunLoop() 作非模态覆盖，子窗口自身不 RunLoop/ExitLoop，
    -- 避免嵌套事件循环。
    local doubao_key_window = dispatcher:AddWindow({
        ID = "GenerateDoubaoKeyWindow",
        WindowTitle = "SubFix · 配置豆包密钥",
        Geometry = {500, 300, 420, 200},
    },
    ui:VGroup{
        Spacing = 8,
        ContentsMargins = 12,
        ui:Label{Text = "填写火山引擎（豆包）语音识别密钥", Weight = 0},
        ui:HGroup{
            Weight = 0,
            Spacing = 8,
            ui:Label{Text = "App ID：", Weight = 0, MinimumSize = {96, 0}},
            ui:LineEdit{ID = "GenerateDoubaoAppIdInput", PlaceholderText = "火山引擎 App ID", Weight = 1}
        },
        ui:HGroup{
            Weight = 0,
            Spacing = 8,
            ui:Label{Text = "Access Token：", Weight = 0, MinimumSize = {96, 0}},
            ui:LineEdit{ID = "GenerateDoubaoTokenInput", PlaceholderText = "火山引擎 Access Token", Weight = 1}
        },
        ui:Label{ID = "GenerateDoubaoKeyStatusLabel", Text = "密钥仅保存在本机，不会上传或进入版本库。", Weight = 0},
        ui:HGroup{
            Weight = 0,
            Spacing = 8,
            ui:HGap(0, 1),
            ui:Button{ID = "GenerateDoubaoKeySaveBtn", Text = "保存", Weight = 0, MinimumSize = {88, 28}},
            ui:Button{ID = "GenerateDoubaoKeyCancelBtn", Text = "取消", Weight = 0, MinimumSize = {88, 28}}
        }
    })

    local doubao_key_items = doubao_key_window:GetItems()
    local doubao_appid_input = doubao_key_items and doubao_key_items.GenerateDoubaoAppIdInput or nil
    local doubao_token_input = doubao_key_items and doubao_key_items.GenerateDoubaoTokenInput or nil
    local doubao_key_status_label = doubao_key_items and doubao_key_items.GenerateDoubaoKeyStatusLabel or nil

    local function set_doubao_key_status(text)
        if doubao_key_status_label then
            pcall(function() doubao_key_status_label.Text = tostring(text or "") end)
        end
    end

    -- 弹出前用已存值预填，方便查看/修改；再 Show（非模态覆盖，父 RunLoop 继续分发事件）。
    local function open_doubao_key_dialog()
        local appid, token = read_doubao_credentials()
        if doubao_appid_input then pcall(function() doubao_appid_input.Text = appid end) end
        if doubao_token_input then pcall(function() doubao_token_input.Text = token end) end
        set_doubao_key_status("密钥仅保存在本机，不会上传或进入版本库。")
        pcall(function() doubao_key_window:Show() end)
    end

    function doubao_key_window.On.GenerateDoubaoKeySaveBtn.Clicked(ev)
        local appid = trim_text(doubao_appid_input and doubao_appid_input.Text or "")
        local token = trim_text(doubao_token_input and doubao_token_input.Text or "")
        if appid == "" or token == "" then
            set_doubao_key_status("App ID 和 Access Token 都要填写")
            return
        end
        local ok, err = save_doubao_credentials(appid, token)
        if not ok then
            set_doubao_key_status(tostring(err or "保存失败"))
            return
        end
        pcall(function() doubao_key_window:Hide() end)
    end

    -- 取消/关闭：未配置就选了豆包会在生成时静默回退，故切回 Qwen 并提示，避免误解。
    local function cancel_doubao_key_dialog()
        pcall(function() doubao_key_window:Hide() end)
        select_subtitle_engine(1)
        if items and items.GenerateSelectionInfoLabel then
            items.GenerateSelectionInfoLabel.Text = "未配置豆包密钥，已切回 Qwen（本地）"
        end
    end

    function doubao_key_window.On.GenerateDoubaoKeyCancelBtn.Clicked(ev)
        cancel_doubao_key_dialog()
    end

    function doubao_key_window.On.GenerateDoubaoKeyWindow.Close(ev)
        cancel_doubao_key_dialog()
    end

    -- 未配置提醒子窗口：单击豆包且未配置时首次提示（引导双击去配置）。同样复用父
    -- RunLoop 作非模态覆盖；"知道了"仅关闭提示、保持豆包选中，等用户双击配置。
    local doubao_reminder_window = dispatcher:AddWindow({
        ID = "GenerateDoubaoReminderWindow",
        WindowTitle = "SubFix · 豆包密钥未配置",
        Geometry = {520, 320, 380, 150},
    },
    ui:VGroup{
        Spacing = 8,
        ContentsMargins = 12,
        ui:Label{Text = "已选择「豆包（云端）」，但尚未配置密钥。", Weight = 0},
        ui:Label{Text = "请双击「豆包（云端）」按钮填写 App ID / Access Token。", Weight = 0},
        ui:HGroup{
            Weight = 0,
            Spacing = 8,
            ui:HGap(0, 1),
            ui:Button{ID = "GenerateDoubaoReminderOkBtn", Text = "知道了", Weight = 0, MinimumSize = {88, 28}}
        }
    })

    function doubao_reminder_window.On.GenerateDoubaoReminderOkBtn.Clicked(ev)
        pcall(function() doubao_reminder_window:Hide() end)
    end

    function doubao_reminder_window.On.GenerateDoubaoReminderWindow.Close(ev)
        pcall(function() doubao_reminder_window:Hide() end)
    end

    -- 秒级近似双击：Resolve 按钮无原生双击事件，用相邻两次点击的秒差(<=阈值)近似。
    local DOUBAO_DOUBLE_CLICK_SECONDS = 1
    local last_doubao_click_time = nil
    local doubao_reminder_shown = false

    local item_map = {}
    for index, source in ipairs(audio_sources) do
        local ok_item, item = pcall(function() return track_tree:NewItem() end)
        if ok_item and item then
            local checked = index == 1
            track_rows[index] = {source = source, checked = checked, item = item}
            set_tree_item_text(item, 0, checked and TRACK_CHECKED_MARK or TRACK_UNCHECKED_MARK)
            set_tree_item_text(item, 1, format_audio_source_label(source, fps))
            pcall(function() track_tree:AddTopLevelItem(item) end)
            item_map[item] = index
        end
    end
    safe_refresh_tree_widget(track_tree)

    local function collect_checked_audio_sources()
        local checked_sources = {}
        for _, row in ipairs(track_rows) do
            if row.checked then
                checked_sources[#checked_sources + 1] = row.source
            end
        end
        return checked_sources
    end

    local function set_track_checked(row, checked)
        row.checked = checked == true
        set_tree_item_text(row.item, 0, row.checked and TRACK_CHECKED_MARK or TRACK_UNCHECKED_MARK)
    end

    -- 现场模式支持多麦：点击即切换该轨勾选状态（单轨场景自然退化为单选）
    function selection_window.On.GenerateAudioTrackTree.ItemClicked(ev)
        local item = get_tree_event_value(ev, {"item", "Item", "currentItem", "CurrentItem"})
        if not item then item = get_selected_tree_node(track_tree) end
        if not item then return end
        local row_index = item_map[item]
        if row_index and track_rows[row_index] then
            set_track_checked(track_rows[row_index], not track_rows[row_index].checked)
            safe_refresh_tree_widget(track_tree)
        end
    end

    -- 字幕长度：两个互斥按钮，点击即切换选中项并高亮当前选择
    function selection_window.On.GenerateSubtitleLengthStandardBtn.Clicked(ev)
        select_subtitle_length(1)
    end

    function selection_window.On.GenerateSubtitleLengthShortBtn.Clicked(ev)
        select_subtitle_length(2)
    end

    -- 识别模型：两个互斥按钮，点击即切换 backend 并高亮当前选择
    function selection_window.On.GenerateSubtitleEngineQwenBtn.Clicked(ev)
        select_subtitle_engine(1)
    end

    function selection_window.On.GenerateSubtitleEngineDoubaoBtn.Clicked(ev)
        -- 单击选中豆包；≤阈值秒内的第二次点击视为双击 → 打开配置窗；单次点击时
        -- 若未配置且本次会话还没提醒过，弹提醒窗引导用户双击去配置。
        local now = os.time()
        local is_double = last_doubao_click_time ~= nil and (now - last_doubao_click_time) <= DOUBAO_DOUBLE_CLICK_SECONDS
        last_doubao_click_time = now
        select_subtitle_engine(2)
        if is_double then
            last_doubao_click_time = nil
            pcall(function() doubao_reminder_window:Hide() end)
            open_doubao_key_dialog()
        elseif not doubao_credentials_configured() and not doubao_reminder_shown then
            doubao_reminder_shown = true
            pcall(function() doubao_reminder_window:Show() end)
        end
    end

    function selection_window.On.GenerateSelectionConfirmBtn.Clicked(ev)
        selected_audio_sources = collect_checked_audio_sources()
        if #selected_audio_sources < 1 then
            selected_audio_sources = nil
            if items and items.GenerateSelectionInfoLabel then
                items.GenerateSelectionInfoLabel.Text = "现场模式至少选择一条音频轨道"
            end
            return
        end
        selected_max_chars = read_selected_max_chars()
        selected_backend = read_selected_backend()
        pcall(function() selection_window:Hide() end)
        pcall(function() dispatcher:ExitLoop() end)
    end

    function selection_window.On.GenerateSelectionCancelBtn.Clicked(ev)
        dialog_cancelled = true
        selected_audio_sources = nil
        pcall(function() selection_window:Hide() end)
        pcall(function() dispatcher:ExitLoop() end)
    end

    function selection_window.On.GenerateSelectionWindow.Close(ev)
        dialog_cancelled = true
        selected_audio_sources = nil
        pcall(function() selection_window:Hide() end)
        pcall(function() dispatcher:ExitLoop() end)
    end

    selection_window:Show()
    dispatcher:RunLoop()
    pcall(function() selection_window:Hide() end)
    pcall(function() doubao_key_window:Hide() end)
    pcall(function() doubao_reminder_window:Hide() end)

    if not selected_audio_sources and not dialog_cancelled then
        selected_audio_sources = collect_checked_audio_sources()
        if #selected_audio_sources > 0 then
            print("[SubFix Generate] 音频轨选择窗口提前退出，使用默认勾选轨道")
        end
    end

    if not selected_audio_sources then
        return nil, nil, nil, nil, "已取消"
    end
    if #selected_audio_sources == 0 then
        return nil, nil, nil, nil, "请至少选择一个音频轨道"
    end
    return selected_audio_sources, subtitle_mode, selected_max_chars, selected_backend, nil
end

local function progress_elapsed_text(started_at)
    local elapsed = math.max(0, os.time() - (tonumber(started_at) or os.time()))
    if elapsed >= 60 then
        return string.format("%dm%02ds", math.floor(elapsed / 60), math.floor(elapsed % 60))
    end
    return string.format("%ds", math.floor(elapsed + 0.5))
end

local function parse_progress_payload(progress_text)
    local text = tostring(progress_text or "")
    if text == "" then return nil end
    local function number_field(name)
        return tonumber(text:match('"' .. name .. '"%s*:%s*([%d%.%-]+)'))
    end
    return {
        stage = text:match('"stage"%s*:%s*"([^"]*)"') or "",
        message = text:match('"message"%s*:%s*"([^"]*)"') or "",
        batch_index = number_field("batch_index"),
        total_batches = number_field("total_batches"),
        progress_index = number_field("progress_index"),
        progress_total = number_field("progress_total")
    }
end

local function progress_bar_text(progress_state, payload)
    local total = tonumber(payload and payload.progress_total)
    local index = tonumber(payload and payload.progress_index)
    if not total or not index or total <= 0 then
        total = tonumber(payload and payload.total_batches)
        index = tonumber(payload and payload.batch_index)
    end
    local fraction = 0
    if total and index and total > 0 then
        fraction = math.max(0, math.min(1, index / total))
    end
    local stage = tostring(payload and payload.stage or "")
    if stage ~= "完成" and stage ~= "已取消" and stage ~= "失败" then
        fraction = math.min(fraction, 0.98)
    end
    progress_state.progress_total = 100
    progress_state.progress_fraction = math.max(tonumber(progress_state.progress_fraction) or 0, fraction)
    local width = GENERATE_PROGRESS_BAR_WIDTH
    local percent = math.floor(progress_state.progress_fraction * 100 + 0.5)
    if progress_state.progress_fraction >= 0.995 then
        return string.rep("■", width) .. "⚑", percent
    end
    local pacman_pos = math.floor(progress_state.progress_fraction * width + 0.5)
    pacman_pos = math.max(1, math.min(width, pacman_pos))
    local cells = {}
    for cell_index = 1, width do
        if cell_index < pacman_pos then
            cells[#cells + 1] = "■"
        elseif cell_index == pacman_pos then
            cells[#cells + 1] = "ᗧ"
        else
            cells[#cells + 1] = "□"
        end
    end
    return table.concat(cells) .. "⚑", percent
end

local function show_generate_progress_window()
    if not dispatcher or not ui then
        return nil, "无法初始化 Resolve UI"
    end
    local progress_window = dispatcher:AddWindow({
        ID = "GenerateProgressWindow",
        WindowTitle = "SubFix · 正在生成选区字幕",
        Geometry = {520, 380, 430, 200},
    },
    ui:VGroup{
        Spacing = 8,
        ContentsMargins = 20,
        ui:Label{ID = "GenerateProgressStatusLabel", Text = "准备中", Weight = 0, MinimumSize = {0, 22}},
        ui:HGroup{
            Weight = 0,
            ui:HGap(0, 1),
            ui:Label{ID = "GenerateProgressBarLabel", Text = "ᗧ" .. string.rep("□", GENERATE_PROGRESS_BAR_WIDTH - 1) .. "⚑", Weight = 0, MinimumSize = {0, 20}},
            ui:HGap(0, 1)
        },
        ui:Label{ID = "GenerateProgressMetaLabel", Text = "进度 0%  ·  用时 0s", Weight = 0, MinimumSize = {0, 18}},
        ui:HGroup{
            Weight = 0,
            ui:HGap(0, 1),
            ui:Label{ID = "GenerateProgressHintLabel", Text = "去摸个鱼吧🐟～\n::)", Weight = 0, MinimumSize = {0, 38}, Alignment = {AlignHCenter = true, AlignVCenter = true}},
            ui:HGap(0, 1)
        },
        ui:VGap(4),
        ui:HGroup{
            Weight = 0,
            Spacing = 8,
            ui:HGap(0, 1),
            ui:Button{ID = "GenerateProgressCancelBtn", Text = "取消", Weight = 0, MinimumSize = {88, 28}},
            ui:HGap(0, 1)
        }
    })
    local progress_state = {
        window = progress_window,
        cancel_requested = false,
        started_at = os.time(),
        progress_fraction = 0,
        finished = false
    }

    function progress_window.On.GenerateProgressCancelBtn.Clicked(ev)
        if progress_state.finished then
            progress_window:Hide()
            return
        end
        progress_state.cancel_requested = true
    end

    function progress_window.On.GenerateProgressWindow.Close(ev)
        progress_state.cancel_requested = true
    end

    progress_window:Show()
    return progress_state
end

local function update_generate_progress_window(progress_state, payload, extra_log)
    local progress_window = progress_state and progress_state.window
    if not progress_window then return end
    local ok_items, items = pcall(function() return progress_window:GetItems() end)
    if not ok_items or not items then return end
    local stage = tostring(payload and payload.stage or "处理中")
    local message = tostring(payload and payload.message or "")
    local elapsed = progress_elapsed_text(progress_state.started_at)
    local bar, percent = progress_bar_text(progress_state, payload)
    local status_text = message ~= "" and message or stage
    if extra_log and extra_log ~= "" then
        status_text = tostring(extra_log)
    end
    if items.GenerateProgressStatusLabel then items.GenerateProgressStatusLabel.Text = status_text end
    if items.GenerateProgressBarLabel then items.GenerateProgressBarLabel.Text = bar end
    if items.GenerateProgressMetaLabel then items.GenerateProgressMetaLabel.Text = "进度 " .. tostring(percent) .. "%  ·  用时 " .. elapsed end
end

local function show_writeback_progress_overlay(progress_state)
    local progress_window = progress_state and progress_state.window
    if not progress_window then return false end
    local ok = pcall(function()
        progress_window:SetAttrs({Geometry = GENERATE_WRITEBACK_OVERLAY_GEOMETRY})
        progress_window:Show()
    end)
    return ok
end

local function finish_generate_progress_window(progress_state, status, message)
    local progress_window = progress_state and progress_state.window
    if not progress_window then return end
    local payload = {stage = status, message = message}
    if status == "完成" then
        payload.progress_index = 100
        payload.progress_total = 100
    end
    update_generate_progress_window(progress_state, payload, message)
    progress_state.finished = true
    local ok_items, items = pcall(function() return progress_window:GetItems() end)
    if ok_items and items and items.GenerateProgressCancelBtn then
        items.GenerateProgressCancelBtn.Text = "关闭"
    end
end

local GENERATE_ENGINE_PROFILE_FILES = {
    v3 = "segmentation_profile.json",
    v4 = "segmentation_profile_v3.json",
    v5 = "segmentation_profile_v4.json",
}

local function resolve_generate_engine()
    local generate_engine = tostring(os.getenv("SUBFIX_GENERATE_ENGINE") or "v5"):lower()
    if generate_engine ~= "v3" and generate_engine ~= "v4" and generate_engine ~= "v5" then
        generate_engine = "v5"
    end
    return generate_engine
end

local function resolve_segmentation_profile_path(paths, engine)
    local file_name = GENERATE_ENGINE_PROFILE_FILES[engine]
    if not file_name then return nil end
    local helper_dir = tostring(paths.helper or ""):match("^(.*)/[^/]+$")
    local candidates = {}
    if helper_dir then candidates[#candidates + 1] = helper_dir .. "/" .. file_name end
    candidates[#candidates + 1] = configured_script_root() .. "/.subfix_support/" .. file_name
    for _, candidate in ipairs(candidates) do
        if candidate and file_exists(candidate) then return candidate end
    end
    print(string.format("[SubFix Generate] 未找到 %s 引擎的断句 profile (%s)，交由 Python 默认路径处理", engine, file_name))
    return nil
end

local function build_asr_helper_command(audio_source, srt_path, json_path, timeline_start_frame, fps, progress_path)
    local paths = resolve_asr_paths()
    if not file_exists(paths.helper) then
        return nil, "缺少 ASR helper: " .. tostring(paths.helper)
    end
    if not file_exists(paths.python) then
        return nil, "ASR 环境未安装，请先运行: " .. tostring(paths.setup)
    end
    local generate_engine = resolve_generate_engine()
    local cmd_parts = {
        shell_quote(paths.python),
        shell_quote(paths.helper),
        "--mode", "generate_subtitles",
        "--backend", shell_quote(DEFAULT_ASR_BACKEND),
        "--generate-engine", shell_quote(generate_engine),
        "--audio", shell_quote(audio_source.file_path),
        "--output", shell_quote(json_path),
        "--srt-output", shell_quote(srt_path),
        "--srt-base-frame", shell_quote(tostring(timeline_start_frame)),
        "--timeline-start-frame", shell_quote(tostring(audio_source.start_frame)),
        "--fps", shell_quote(tostring(fps)),
        "--model", shell_quote(DEFAULT_ASR_MODEL),
        "--language", shell_quote(DEFAULT_ASR_LANGUAGE),
        "--progress-json", shell_quote(progress_path),
        "--source-start", shell_quote(string.format("%.3f", tonumber(audio_source.source_start_seconds) or 0)),
        "--source-end", shell_quote(string.format("%.3f", tonumber(audio_source.source_end_seconds) or 0))
    }
    local audio_channel_index = tonumber(audio_source.audio_channel_index)
    if audio_channel_index and audio_channel_index > 0 then
        cmd_parts[#cmd_parts + 1] = "--audio-channel-index"
        cmd_parts[#cmd_parts + 1] = shell_quote(tostring(math.floor(audio_channel_index)))
    end
    return table.concat(cmd_parts, " "), nil
end

local function build_asr_helper_batch_command(batch_plan_path, srt_path, json_path, timeline_start_frame, fps, progress_path, subtitle_mode, max_chars, backend)
    local paths = resolve_asr_paths()
    if not file_exists(paths.helper) then
        return nil, "缺少 ASR helper: " .. tostring(paths.helper)
    end
    if not file_exists(paths.python) then
        return nil, "ASR 环境未安装，请先运行: " .. tostring(paths.setup)
    end
    subtitle_mode = subtitle_mode == "live" and "live" or "narration"
    -- backend 由生成对话框的"识别模型"选择决定；缺省回退到 DEFAULT_ASR_BACKEND(auto/Qwen 本地)。
    local asr_backend = (type(backend) == "string" and backend ~= "") and backend or DEFAULT_ASR_BACKEND
    local generate_engine = resolve_generate_engine()
    local cmd_parts = {
        shell_quote(paths.python),
        shell_quote(paths.helper),
        "--mode", "generate_subtitles_batch",
        "--backend", shell_quote(asr_backend),
        "--generate-engine", shell_quote(generate_engine),
        "--batch-plan-json", shell_quote(batch_plan_path),
        "--output", shell_quote(json_path),
        "--srt-output", shell_quote(srt_path),
        "--srt-base-frame", shell_quote(tostring(timeline_start_frame)),
        "--timeline-start-frame", shell_quote(tostring(timeline_start_frame)),
        "--fps", shell_quote(tostring(fps)),
        "--model", shell_quote(DEFAULT_ASR_MODEL),
        "--language", shell_quote(DEFAULT_ASR_LANGUAGE),
        "--subtitle-mode", shell_quote(subtitle_mode),
        "--diagnostic-output", shell_quote(paths.diagnostic),
        "--progress-json", shell_quote(progress_path)
    }
    max_chars = tonumber(max_chars) or 25
    cmd_parts[#cmd_parts + 1] = "--max-chars"
    cmd_parts[#cmd_parts + 1] = shell_quote(tostring(max_chars))
    local profile_path = resolve_segmentation_profile_path(paths, generate_engine)
    if profile_path then
        cmd_parts[#cmd_parts + 1] = "--segmentation-profile"
        cmd_parts[#cmd_parts + 1] = shell_quote(profile_path)
    end
    return table.concat(cmd_parts, " "), nil
end

local function kill_background_process(pid_file)
    local pid_text = trim_text(read_text_file(pid_file) or "")
    local pid = tonumber(pid_text)
    if pid and pid > 0 then
        os.execute("kill " .. tostring(pid) .. " 2>/dev/null || true")
    end
end

local function run_background_command_with_progress(cmd, progress_path, progress_state)
    local uid = tostring(os.time()) .. "_" .. tostring(math.random(100000, 999999))
    local root = temp_dir()
    local stdout_file = root .. "/asr_stdout_" .. uid .. ".log"
    local pid_file = root .. "/asr_pid_" .. uid
    local done_file = root .. "/asr_done_" .. uid
    local exit_file = root .. "/asr_exit_" .. uid
    local output = ""
    local cancelled = false

    local bg_cmd = string.format(
        "(%s > %s 2>&1; echo $? > %s; touch %s) & echo $! > %s",
        cmd,
        shell_quote(stdout_file),
        shell_quote(exit_file),
        shell_quote(done_file),
        shell_quote(pid_file)
    )
    os.execute(bg_cmd)

    local timer_id = "GenerateProgressPollTimer_" .. uid
    local poll_timer = ui:Timer({ID = timer_id, Interval = 200, SingleShot = false})
    local last_signature = ""

    local timer_registered = register_ui_timer(poll_timer, function()
        local payload = parse_progress_payload(read_text_file(progress_path) or "")
        local signature = tostring(payload and payload.stage or "") .. "\n" .. tostring(payload and payload.message or "") .. "\n" .. progress_elapsed_text(progress_state.started_at)
        if signature ~= last_signature then
            last_signature = signature
            update_generate_progress_window(progress_state, payload or {stage = "处理中", message = "正在识别音频..."}, payload and payload.message)
        else
            update_generate_progress_window(progress_state, payload or {stage = "处理中", message = "正在识别音频..."})
        end

        if progress_state and progress_state.cancel_requested then
            cancelled = true
            kill_background_process(pid_file)
            pcall(function() poll_timer:Stop() end)
            ui_timer_handlers[timer_id] = nil
            pcall(function() dispatcher:ExitLoop() end)
            return
        end

        if file_exists(done_file) then
            pcall(function() poll_timer:Stop() end)
            ui_timer_handlers[timer_id] = nil
            pcall(function() dispatcher:ExitLoop() end)
        end
    end)
    if not timer_registered then
        kill_background_process(pid_file)
        return false, "无法启动进度轮询", nil
    end

    update_generate_progress_window(progress_state, {stage = "启动 ASR", message = "正在启动字幕识别..."}, "启动 ASR helper")
    local timer_ok = pcall(function() poll_timer:Start() end)
    if not timer_ok then
        ui_timer_handlers[timer_id] = nil
        kill_background_process(pid_file)
        return false, "无法启动进度窗口计时器", nil
    end
    dispatcher:RunLoop()
    pcall(function() poll_timer:Stop() end)
    ui_timer_handlers[timer_id] = nil

    output = read_text_file(stdout_file) or ""
    local exit_code = tonumber(trim_text(read_text_file(exit_file) or "")) or 1
    os.execute(string.format("rm -f %s %s %s %s 2>/dev/null", shell_quote(stdout_file), shell_quote(pid_file), shell_quote(done_file), shell_quote(exit_file)))

    if cancelled then
        return false, "已取消", "cancelled"
    end
    return exit_code == 0, output, nil
end

local function run_asr_helper_with_progress(audio_source, srt_path, json_path, timeline_start_frame, fps, progress_state, source_index, source_count)
    local progress_path = json_path .. ".progress.json"
    local cmd, cmd_err = build_asr_helper_command(audio_source, srt_path, json_path, timeline_start_frame, fps, progress_path)
    if not cmd then return false, cmd_err end
    if source_index and source_count then
        update_generate_progress_window(
            progress_state,
            {stage = string.format("识别 %d/%d", tonumber(source_index) or 0, tonumber(source_count) or 0), message = tostring(audio_source.file_name or audio_source.file_path or "")},
            string.format("识别 %d/%d: %s", tonumber(source_index) or 0, tonumber(source_count) or 0, tostring(audio_source.file_name or audio_source.file_path or ""))
        )
    end
    local ok, output, status = run_background_command_with_progress(cmd, progress_path, progress_state)
    os.execute("rm -f " .. shell_quote(progress_path) .. " 2>/dev/null")
    if not ok then
        return false, tostring(output or "ASR helper 执行失败"), status
    end
    if not file_exists(srt_path) then
        return false, "ASR helper 未生成 SRT"
    end
    return true
end

local function run_asr_helper_batch_with_progress(batch_plan_path, srt_path, json_path, timeline_start_frame, fps, progress_state, source_count, subtitle_mode, max_chars, backend)
    local progress_path = json_path .. ".progress.json"
    local cmd, cmd_err = build_asr_helper_batch_command(batch_plan_path, srt_path, json_path, timeline_start_frame, fps, progress_path, subtitle_mode, max_chars, backend)
    if not cmd then return false, cmd_err end
    update_generate_progress_window(
        progress_state,
        {stage = "启动批量 ASR", message = string.format("准备识别 %d 段音频", tonumber(source_count) or 0)},
        string.format("批量识别 %d 段音频", tonumber(source_count) or 0)
    )
    local ok, output, status = run_background_command_with_progress(cmd, progress_path, progress_state)
    os.execute("rm -f " .. shell_quote(progress_path) .. " 2>/dev/null")
    if not ok then
        return false, tostring(output or "ASR helper 执行失败"), status
    end
    if not file_exists(json_path) then
        return false, "ASR helper 未生成 JSON"
    end
    return true
end

local function parse_generated_srt_rows(srt_path, fps, base_frame)
    local file = io.open(tostring(srt_path or ""), "r")
    if not file then return nil, "无法读取生成的 SRT" end
    local lines = {}
    for line in file:lines() do
        lines[#lines + 1] = line
    end
    file:close()

    local rows = {}
    local index = 1
    while index <= #lines do
        local timing = tostring(lines[index] or "")
        local start_time, end_time = timing:match("^(%d+:%d+:%d+,%d+)%s+%-%-%>%s+(%d+:%d+:%d+,%d+)")
        if not start_time and index < #lines then
            index = index + 1
            timing = tostring(lines[index] or "")
            start_time, end_time = timing:match("^(%d+:%d+:%d+,%d+)%s+%-%-%>%s+(%d+:%d+:%d+,%d+)")
        end
        if start_time and end_time then
            local text_lines = {}
            index = index + 1
            while index <= #lines and tostring(lines[index] or "") ~= "" do
                text_lines[#text_lines + 1] = tostring(lines[index] or "")
                index = index + 1
            end
            local start_frame = srt_time_to_frame(start_time, fps, base_frame)
            local end_frame = srt_time_to_frame(end_time, fps, base_frame)
            if start_frame and end_frame and table.concat(text_lines, "") ~= "" then
                rows[#rows + 1] = {
                    index = #rows + 1,
                    start_frame = start_frame,
                    end_frame = math.max(end_frame, start_frame + 1),
                    text = table.concat(text_lines, "\n")
                }
            end
        end
        index = index + 1
    end
    if #rows == 0 then return nil, "生成的 SRT 没有可写回字幕" end
    return rows
end

local function merge_generated_rows_for_writeback(row_groups, scope)
    local all_rows = {}
    local scope_start = tonumber(scope and scope.start_frame)
    local scope_end = tonumber(scope and scope.end_frame)
    for _, rows in ipairs(row_groups or {}) do
        for _, row in ipairs(rows or {}) do
            local start_frame = tonumber(row.start_frame)
            local end_frame = tonumber(row.end_frame)
            local text = trim_text(tostring(row.text or ""))
            if start_frame and end_frame and text ~= "" then
                if scope_start then start_frame = math.max(start_frame, scope_start) end
                if scope_end then end_frame = math.min(end_frame, scope_end) end
                if end_frame > start_frame then
                    all_rows[#all_rows + 1] = {
                        start_frame = start_frame,
                        end_frame = end_frame,
                        text = text
                    }
                end
            end
        end
    end
    table.sort(all_rows, function(a, b)
        local a_start = tonumber(a.start_frame) or 0
        local b_start = tonumber(b.start_frame) or 0
        if a_start ~= b_start then return a_start < b_start end
        return (tonumber(a.end_frame) or 0) < (tonumber(b.end_frame) or 0)
    end)

    local merged_rows = {}
    local last_end = nil
    for _, row in ipairs(all_rows) do
        local start_frame = tonumber(row.start_frame) or 0
        local end_frame = tonumber(row.end_frame) or 0
        if last_end and start_frame < last_end then
            start_frame = last_end
        end
        if end_frame > start_frame then
            merged_rows[#merged_rows + 1] = {
                index = #merged_rows + 1,
                start_frame = start_frame,
                end_frame = end_frame,
                text = row.text
            }
            last_end = end_frame
        end
    end
    if #merged_rows == 0 then
        return nil, "生成结果没有可写回字幕"
    end
    return merged_rows
end

local function build_composite_rows_for_writeback(timeline, track_index, scope, generated_rows, fps)
    local ok_items, items = pcall(function() return timeline:GetItemListInTrack("subtitle", track_index) end)
    items = ok_items and items or {}
    local rows = {}
    for _, item in ipairs(items or {}) do
        local ok_start, start_frame = pcall(function() return item:GetStart() end)
        local ok_end, end_frame = pcall(function() return item:GetEnd() end)
        local ok_name, name = pcall(function() return item:GetName() end)
        if ok_start and ok_end and not range_intersects_selection(start_frame, end_frame, scope) then
            rows[#rows + 1] = {
                start_frame = tonumber(start_frame) or 0,
                end_frame = tonumber(end_frame) or 0,
                text = ok_name and tostring(name or "") or ""
            }
        end
    end
    for _, row in ipairs(generated_rows or {}) do
        rows[#rows + 1] = {
            start_frame = tonumber(row.start_frame) or 0,
            end_frame = tonumber(row.end_frame) or 0,
            text = tostring(row.text or "")
        }
    end
    table.sort(rows, function(a, b)
        return (tonumber(a.start_frame) or 0) < (tonumber(b.start_frame) or 0)
    end)
    for row_index, row in ipairs(rows) do
        row.index = row_index
        if (tonumber(row.end_frame) or 0) <= (tonumber(row.start_frame) or 0) then
            row.end_frame = (tonumber(row.start_frame) or 0) + 1
        end
    end
    return rows
end

local function write_rows_to_rebuild_srt(srt_path, rows, fps, base_frame)
    local file = io.open(tostring(srt_path or ""), "w")
    if not file then return false, "无法创建重建字幕 SRT" end
    for index, row in ipairs(rows or {}) do
        file:write(tostring(index) .. "\n")
        file:write(frames_to_srt_time(row.start_frame, fps, base_frame) .. " --> " .. frames_to_srt_time(row.end_frame, fps, base_frame) .. "\n")
        file:write(tostring(row.text or "") .. "\n\n")
    end
    file:close()
    return true
end

local function capture_timeline_playhead_timecode(timeline)
    if not timeline then return nil end
    local ok, timecode = pcall(function() return timeline:GetCurrentTimecode() end)
    if ok and trim_text(timecode) ~= "" then
        return tostring(timecode)
    end
    print("[SubFix Generate] 保存播放头失败: " .. tostring(timecode))
    return nil
end

local function restore_timeline_playhead_timecode(timeline, timecode)
    timecode = trim_text(timecode)
    if not timeline or timecode == "" then return false end
    local ok, ret = pcall(function() return timeline:SetCurrentTimecode(timecode) end)
    if ok and ret ~= false then
        print("[SubFix Generate] 已恢复播放头: " .. tostring(timecode))
        return true
    end
    print("[SubFix Generate] 恢复播放头失败: " .. tostring(ret))
    return false
end

local function append_rebuild_srt_to_timeline(media_pool, media_pool_item)
    if not media_pool or not media_pool_item then
        return false, "缺少媒体池或字幕媒体项"
    end

    print("[SubFix Generate] 使用稳定模式追加字幕到时间线")
    local ok_append, ret = pcall(function() return media_pool:AppendToTimeline({media_pool_item}) end)
    if ok_append and ret ~= false and ret ~= nil then
        return true, ret
    end
    return false, "追加重建字幕到时间线失败"
end

local function import_rebuild_srt_with_retry(media_pool, rebuild_srt_path)
    local last_error = ""
    for attempt = 1, 2 do
        local root = media_pool and media_pool:GetRootFolder()
        if root then
            local ok_folder, folder_ret = pcall(function() return media_pool:SetCurrentFolder(root) end)
            if not ok_folder or folder_ret == false then
                last_error = "无法切换媒体池根目录"
            end
        end
        local ok_import, items = pcall(function() return media_pool:ImportMedia({rebuild_srt_path}) end)
        if ok_import and items and #items > 0 then
            if attempt > 1 then
                print("[SubFix Generate] 重试导入重建字幕 SRT 成功")
            end
            return items, nil
        end
        last_error = ok_import and "Resolve 返回空媒体项" or tostring(items)
        if attempt < 2 then
            print("[SubFix Generate] 首次导入重建字幕 SRT 失败，准备重试: " .. tostring(last_error))
            os.execute("sleep 0.25")
        end
    end
    return nil, "导入重建字幕 SRT 失败（已重试）: " .. tostring(last_error)
end

local function rebuild_target_subtitle_track_from_rows(project, timeline, rows, fps, base_frame)
    if not rows or #rows == 0 then return false, "没有可写回字幕" end
    local media_pool = project:GetMediaPool()
    if not media_pool then return false, "无法获取媒体池" end
    local original_playhead_timecode = capture_timeline_playhead_timecode(timeline)
    local function finish_rebuild(result, err)
        restore_timeline_playhead_timecode(timeline, original_playhead_timecode)
        return result, err
    end

    local ensured_items, ensure_err = ensure_subtitle_track_exists(TARGET_SUBTITLE_TRACK, timeline)
    if not ensured_items then return finish_rebuild(false, ensure_err or "目标字幕轨准备失败") end
    unlock_all_subtitle_tracks(timeline)
    local isolate_ok, isolate_err = isolate_subtitle_target_track(TARGET_SUBTITLE_TRACK, timeline)
    if not isolate_ok then return finish_rebuild(false, isolate_err or "无法切换到目标字幕轨") end

    local rebuild_srt_path = temp_dir() .. "/GeneratedSelection_Rebuild_" .. os.time() .. "_" .. tostring(math.random(100000, 999999)) .. ".srt"
    local write_ok, write_err = write_rows_to_rebuild_srt(rebuild_srt_path, rows, fps, base_frame)
    if not write_ok then return finish_rebuild(false, write_err) end

    local items, import_err = import_rebuild_srt_with_retry(media_pool, rebuild_srt_path)
    if not items then
        return finish_rebuild(false, import_err)
    end

    local clear_ok, clear_result = clear_subtitle_track_clips(timeline, TARGET_SUBTITLE_TRACK)
    if not clear_ok then return finish_rebuild(false, clear_result) end
    print("[SubFix Generate] 已重建目标字幕轨，清空旧字幕 " .. tostring(clear_result or 0) .. " 条，保留选区外字幕")
    print("[SubFix Generate] SRT 时间已按 timeline_start_frame 转相对时间")
    local append_ok, append_err = append_rebuild_srt_to_timeline(media_pool, items[1])
    if not append_ok then
        return finish_rebuild(false, append_err)
    end
    print("[SubFix Generate] 已提交写回目标字幕轨 " .. tostring(TARGET_SUBTITLE_TRACK) .. "，SRT 行数 " .. tostring(#rows))
    return finish_rebuild(true)
end

local function generate_selection_subtitles()
    print("[SubFix Generate] 开始生成选区字幕")
    local resolve_obj = get_resolve()
    if not resolve_obj then error("无法获取 Resolve") end
    local pm = resolve_obj:GetProjectManager()
    local project = pm and pm:GetCurrentProject()
    local timeline = project and project:GetCurrentTimeline()
    if not project or not timeline then error("没有打开的时间线") end

    local fps = parse_fps(timeline:GetSetting("timelineFrameRate"))
    local scope, scope_err = read_generation_scope(timeline, fps)
    if not scope or scope.mode ~= WORK_SCOPE_MODE_SELECTION then
        error(scope_err or "请先用 I/O 设置 In/Out 选区")
    end
    print(string.format(
        "[SubFix Generate] 使用范围来源=%s 帧范围 %d-%d，时长 %.2fs%s",
        tostring(scope.source_label or scope.source or "unknown"),
        tonumber(scope.start_frame) or 0,
        tonumber(scope.end_frame) or 0,
        ((tonumber(scope.end_frame) or 0) - (tonumber(scope.start_frame) or 0)) / math.max(1, fps),
        scope.item_name and scope.item_name ~= "" and (" item=" .. tostring(scope.item_name)) or ""
    ))
    local track_items, track_err = ensure_subtitle_track_exists(TARGET_SUBTITLE_TRACK, timeline)
    if not track_items then error(track_err) end

    local audio_sources, audio_err = collect_audio_sources_for_scope(timeline, scope, fps)
    if not audio_sources then error(audio_err) end
    local raw_audio_source_count = #audio_sources
    audio_sources = build_audio_track_options_for_dialog(audio_sources)
    if raw_audio_source_count ~= #audio_sources then
        print(string.format("[SubFix Generate] 音频候选已按轨道折叠: %d -> %d", raw_audio_source_count, #audio_sources))
    end
    local selected_audio_sources = nil
    -- 模式选择下拉已移除，固定使用现场模式
    local subtitle_mode = "live"
    -- 字幕长度：标准（≤25字）/ 短视频（≤10字），默认标准，仅有 1 个音频候选自动跳过弹窗时使用该默认值
    local max_chars = 25
    -- 识别模型 backend：默认 Qwen 本地（DEFAULT_ASR_BACKEND == "auto"），仅有 1 个音频候选
    -- 自动跳过弹窗时也用该默认值，保证与现状零改变。
    local backend = DEFAULT_ASR_BACKEND
    local selection_err = nil
    if #audio_sources == 1 then
        selected_audio_sources = audio_sources
        print("[SubFix Generate] 仅有 1 个音频候选，自动使用: " .. format_audio_source_label(audio_sources[1], fps))
    else
        selected_audio_sources, subtitle_mode, max_chars, backend, selection_err = show_audio_track_selection_dialog(audio_sources, scope, fps)
    end
    if not selected_audio_sources then error(selection_err or "已取消") end
    local selected_track_sources = collect_selected_track_sources(selected_audio_sources)
    if type(selected_track_sources) ~= "table" or #selected_track_sources == 0 then
        error("所选轨道没有可识别音频片段")
    end
    local raw_selected_track_source_count = #selected_track_sources
    local optimized_track_sources, source_optimization = optimize_selected_track_sources_for_generation(selected_track_sources, fps)
    selected_track_sources = optimized_track_sources or selected_track_sources
    print(string.format(
        "[SubFix Generate] 使用 %d 条音频轨；音频 %d → 去重 %d",
        #selected_audio_sources,
        raw_selected_track_source_count,
        tonumber(source_optimization and source_optimization.deduped_source_count) or #selected_track_sources
    ))
    for source_index, source in ipairs(selected_track_sources) do
        print(string.format(
            "[SubFix Generate] 片段 %d/%d: A%d #%d %s timeline=%d-%d source=%.3f-%.3fs mapping=%s channel=%s muted=%s linked_offset=%s fallback=%s",
            source_index,
            #selected_track_sources,
            tonumber(source.track_index) or 0,
            tonumber(source.item_index) or 0,
            tostring(source.file_name or source.file_path or ""),
            tonumber(source.start_frame) or 0,
            tonumber(source.end_frame) or 0,
            tonumber(source.source_start_seconds) or 0,
            tonumber(source.source_end_seconds) or 0,
            tostring(source.audio_mapping_source or ""),
            tostring(source.audio_channel_index or ""),
            tostring(source.audio_mapping_muted == true),
            tostring(source.linked_offset_samples or ""),
            tostring(source.audio_mapping_fallback_reason or "")
        ))
    end

    local progress_state, progress_err = show_generate_progress_window()
    if not progress_state then error(progress_err or "无法打开进度窗口") end

    local root = temp_dir()
    local uid = tostring(os.time()) .. "_" .. tostring(math.random(100000, 999999))
    local json_path = root .. "/GeneratedSelection_" .. uid .. ".json"
    local srt_path = root .. "/GeneratedSelection_" .. uid .. ".srt"
    local batch_plan_path = root .. "/GeneratedSelection_" .. uid .. ".batch_plan.json"
    local audio_diag_path = json_path .. ".audio_source.json"
    if not write_generate_batch_plan(batch_plan_path, selected_track_sources, fps) then
        finish_generate_progress_window(progress_state, "失败", "无法写入批量生成计划")
        error("无法写入批量生成计划")
    end
    if write_selected_audio_source_diagnostic(audio_diag_path, selected_audio_sources, scope, source_optimization) then
        print("[SubFix Generate] 已写入音频源诊断: " .. audio_diag_path)
    end
    local helper_ok, helper_err, helper_status = run_asr_helper_batch_with_progress(
        batch_plan_path,
        srt_path,
        json_path,
        scope.timeline_start_frame,
        fps,
        progress_state,
        #selected_track_sources,
        subtitle_mode,
        max_chars,
        backend
    )
    if not helper_ok then
        finish_generate_progress_window(progress_state, helper_status == "cancelled" and "已取消" or "失败", helper_err)
        error(helper_err)
    end

    update_generate_progress_window(progress_state, {stage = "整理结果", message = "正在整理生成字幕", progress_index = 98, progress_total = 100}, "整理生成字幕")
    local raw_generated_rows, raw_generated_err = parse_generated_json_subtitle_rows(json_path)
    if not raw_generated_rows then
        finish_generate_progress_window(progress_state, "失败", raw_generated_err)
        error(raw_generated_err)
    end
    local priority_rows, priority_err = nil, nil
    if subtitle_mode == "live" then
        priority_rows, priority_err = merge_generated_rows_for_live_writeback(raw_generated_rows, scope)
    else
        priority_rows, priority_err = merge_generated_rows_with_track_priority(raw_generated_rows, scope)
    end
    if not priority_rows then
        finish_generate_progress_window(progress_state, "失败", priority_err)
        error(priority_err)
    end
    local generated_rows, generated_err = merge_generated_rows_for_writeback({priority_rows}, scope)
    if not generated_rows then
        finish_generate_progress_window(progress_state, "失败", generated_err)
        error(generated_err)
    end

    local backup_path, backup_err = backup_target_track(timeline, TARGET_SUBTITLE_TRACK, fps, scope.timeline_start_frame)
    if backup_path then
        print("[SubFix Generate] 已备份目标字幕轨: " .. backup_path)
    elseif backup_err then
        print("[SubFix Generate] 备份目标字幕轨失败: " .. tostring(backup_err))
    end

    update_generate_progress_window(progress_state, {stage = "写回时间线", message = "正在写回目标字幕轨", progress_index = 99, progress_total = 100}, "写回目标字幕轨")
    show_writeback_progress_overlay(progress_state)
    local composite_rows = build_composite_rows_for_writeback(timeline, TARGET_SUBTITLE_TRACK, scope, generated_rows, fps)
    local import_ok, import_err = rebuild_target_subtitle_track_from_rows(project, timeline, composite_rows, fps, scope.timeline_start_frame)
    if not import_ok then
        finish_generate_progress_window(progress_state, "失败", import_err)
        error(import_err)
    end
    finish_generate_progress_window(progress_state, "完成", "选区字幕已生成并写回时间线")
    pcall(function() progress_state.window:Hide() end)
    print("[SubFix Generate] 生成选区字幕完成，诊断: " .. audio_diag_path)
    return true
end

function SubFixGenerateSelectionCore.run(options)
    options = options or {}
    runtime_options = options
    TARGET_SUBTITLE_TRACK = tonumber(options.target_subtitle_track) or 1

    local ok, err = pcall(function()
        return generate_selection_subtitles()
    end)
    runtime_options = {}
    if not ok then
        print("[SubFix Generate] 失败: " .. tostring(err))
        return false, tostring(err)
    end
    return true
end

return SubFixGenerateSelectionCore
