#!/usr/bin/env lua
-- SubFix child plugin: generate subtitles for the current DaVinci Resolve In/Out selection.

local function script_dir()
    local source = debug and debug.getinfo and debug.getinfo(1, "S").source or ""
    source = tostring(source or ""):gsub("^@", "")
    local dir = source:match("^(.*[/\\])")
    if dir and dir ~= "" then
        return dir:gsub("[/\\]$", "")
    end
    return os.getenv("PWD") or "."
end

local function parent_dir(path)
    local cleaned = tostring(path or ""):gsub("[/\\]$", "")
    local parent = cleaned:match("^(.*)[/\\][^/\\]+$")
    if parent and parent ~= "" then
        return parent
    end
    return cleaned
end

local function file_exists(path)
    local file = io.open(tostring(path or ""), "rb")
    if file then
        file:close()
        return true
    end
    return false
end

local function resolve_generate_core_root(root)
    local cleaned = tostring(root or ""):gsub("[/\\]$", "")
    local direct_core = cleaned .. "/.subfix_support/subfix_generate_selection_core.lua"
    if file_exists(direct_core) then
        return cleaned
    end

    local parent = parent_dir(cleaned)
    local parent_core = parent .. "/.subfix_support/subfix_generate_selection_core.lua"
    if file_exists(parent_core) then
        return parent
    end
    return cleaned
end

local function load_generate_core(root)
    local core_path = tostring(root or "") .. "/.subfix_support/subfix_generate_selection_core.lua"
    local chunk, load_err = loadfile(core_path)
    if not chunk then
        error("无法加载生成模块: " .. core_path .. " " .. tostring(load_err or ""))
    end

    local ok, core_or_err = pcall(chunk)
    if not ok then
        error("生成模块初始化失败: " .. tostring(core_or_err))
    end
    if type(core_or_err) ~= "table" or type(core_or_err.run) ~= "function" then
        error("生成模块接口无效: " .. core_path)
    end
    return core_or_err
end

local ok, err = pcall(function()
    local root = resolve_generate_core_root(script_dir())
    local core = load_generate_core(root)
    local run_ok, run_err = core.run({
        script_root = root,
        target_subtitle_track = 1
    })
    if not run_ok then
        error(run_err or "生成选区字幕失败")
    end
end)

if not ok then
    print("[SubFix Generate] 失败: " .. tostring(err))
end
