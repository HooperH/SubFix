-- Model management UI; all I/O and background work is provided by the shared core.
local ModelCenter = {}

local function size_text(bytes)
    local value = tonumber(bytes)
    if not value or value < 0 then return "待确认" end
    if value >= 1024 * 1024 * 1024 then return string.format("%.2f GB", value / (1024 * 1024 * 1024)) end
    return string.format("%.0f MB", value / (1024 * 1024))
end

function ModelCenter.open(context)
    local ui, dispatcher = context.ui, context.dispatcher
    local window = dispatcher:AddWindow({
        ID = "SubFixModelCenter", WindowTitle = "SubFix · 模型中心",
        Geometry = context.geometry({420, 240, 500, 290})
    }, ui:VGroup{
        ContentsMargins = 16, Spacing = 8,
        ui:ComboBox{ID = "ModelChoice", Weight = 0, MinimumSize = {0, 30}},
        ui:VGap(2),
        ui:HGroup{Weight = 0, Spacing = 8,
            ui:Label{Text = "下载源", Weight = 0},
            ui:ComboBox{ID = "ModelSource", Weight = 1}
        },
        ui:TextEdit{ID = "ModelDetails", ReadOnly = true, Weight = 1, MinimumSize = {0, 120}},
        ui:HGroup{Weight = 0, Spacing = 8,
            ui:Button{ID = "ModelRepair", Text = "校验 / 修复", Weight = 1},
            ui:Button{ID = "ModelRefresh", Text = "刷新状态", Weight = 1}
        },
        ui:Label{ID = "ModelMessage", Text = "", Hidden = true, WordWrap = true, Weight = 0, MinimumSize = {0, 32}},
        ui:HGroup{Weight = 0, Spacing = 8,
            ui:Button{ID = "ModelDownload", Text = "下载模型", Weight = 1, MinimumSize = {110, 30}},
            ui:Button{ID = "ModelSelect", Text = "使用此模型", Hidden = true, Weight = 1, MinimumSize = {110, 30}},
            ui:Button{ID = "ModelImport", Text = "离线导入", Weight = 0},
            ui:Button{ID = "ModelClose", Text = "关闭", Weight = 0}
        }
    })
    local items = window:GetItems()
    local models, selected_id, action = {}, context.focus_model_id, nil
    local last_operation_error = nil
    local sources = {"auto", "modelscope", "huggingface"}
    items.ModelSource:AddItem("自动（国内源优先）")
    items.ModelSource:AddItem("魔搭国内源")
    items.ModelSource:AddItem("Hugging Face")

    local function resize_window()
        local geometry = window:GetGeometry()
        local height = 290 + (items.ModelMessage.Hidden and 0 or 40)
        window:RecalcLayout()
        -- SetAttrs({Geometry = ...}) does not resize this native dialog.
        window:Resize({geometry[3], height})
    end

    local function set_message(message)
        items.ModelMessage.Text = message or ""
        items.ModelMessage.Hidden = not message or message == ""
        resize_window()
    end

    local function current_model()
        return models[(tonumber(items.ModelChoice.CurrentIndex) or 0) + 1]
    end

    local function refresh_details()
        local model = current_model()
        if not model then
            items.ModelDetails.PlainText = "暂时无法读取模型清单，请点击刷新。"
            items.ModelDownload.Enabled = false
            items.ModelRepair.Enabled = false
            items.ModelSelect.Enabled = false
            items.ModelImport.Enabled = false
            return
        end
        selected_id = model.id
        local is_asr = model.kind == "asr"
        local state_labels = {installed = "已安装", missing = "未安装", repair_required = "需要修复", environment_missing = "模型已导入，需准备运行环境"}
        local description = model.description or (is_asr and "本地语音识别；生成字幕还需要下方共享对齐模型。" or "精确匹配文字与音频时间；本地识别和云端识别共用。")
        local detail = {
            tostring(model.label or model.id), "", tostring(description),
            "状态：" .. tostring(state_labels[model.state] or model.state or "未安装"),
            "模型下载：" .. size_text(model.download_bytes) .. "    已占用：" .. size_text(model.installed_bytes)
        }
        if is_asr then
            detail[#detail + 1] = "配套依赖：Qwen3-ForcedAligner-0.6B（共享，只需安装一次）"
            detail[#detail + 1] = "运行环境会单独检查；0.6B 参数更少，实际内存还取决于音频与批量设置。"
        end
        if model.model_path and model.model_path ~= "" then detail[#detail + 1] = "位置：" .. tostring(model.model_path) end
        if model.error then detail[#detail + 1] = tostring(model.error) end
        if model.download_available == false then
            detail[#detail + 1] = tostring(model.download_unavailable_reason or "在线下载尚未开放，请导入已有模型。")
        end
        if last_operation_error then detail[#detail + 1] = "\n最近一次操作：\n" .. last_operation_error end
        items.ModelDetails.PlainText = table.concat(detail, "\n")
        local can_install = model.download_available ~= false or model.model_installed == true or model.ready == true
        items.ModelDownload.Enabled = can_install and not model.ready
        items.ModelDownload.Hidden = is_asr and model.ready == true
        items.ModelDownload.Text = model.ready and "已安装" or (model.state == "repair_required" and "修复模型" or (model.model_installed and "准备运行环境" or "下载模型"))
        items.ModelRepair.Enabled = can_install
        items.ModelImport.Enabled = true
        items.ModelSelect.Hidden = not (is_asr and model.ready)
        items.ModelSelect.Enabled = is_asr and model.id ~= context.selected_model_id
        items.ModelSelect.Text = model.id == context.selected_model_id and "当前使用中" or "使用此模型"
        items.ModelSource.Enabled = model.ready ~= true and is_asr
        if not is_asr then items.ModelSource.CurrentIndex = 0 end
    end

    local function refresh()
        local payload, err = context.query("list")
        if not payload then
            set_message("读取失败，请点击刷新状态重试。")
            items.ModelDetails.PlainText = tostring(err)
            items.ModelDownload.Enabled = false
            items.ModelRepair.Enabled = false
            items.ModelImport.Enabled = false
            items.ModelSelect.Enabled = false
            return
        end
        models = payload.models or {}
        context.selected_model_id = payload.selected_model_id
        selected_id = selected_id or payload.selected_model_id
        items.ModelChoice:Clear()
        local index = 0
        for i, model in ipairs(models) do
            local state_text = model.ready and "已安装" or (model.model_installed and "待准备环境" or "未安装")
            items.ModelChoice:AddItem(tostring(model.label or model.id) .. "  ·  " .. state_text)
            if model.id == selected_id then index = i - 1 end
        end
        items.ModelChoice.CurrentIndex = index
        refresh_details()
    end

    local function request_action(kind, input_path)
        local model = current_model()
        if not model then return end
        input_path = tostring(input_path or ""):gsub("^%s+", ""):gsub("%s+$", "")
        if kind == "import" and input_path == "" then
            return
        end
        action = {kind = kind, model_id = model.id, input_path = input_path, source = sources[(tonumber(items.ModelSource.CurrentIndex) or 0) + 1]}
        window:Hide()
        dispatcher:ExitLoop()
    end

    function window.On.ModelChoice.CurrentIndexChanged(ev) refresh_details() end
    function window.On.ModelDownload.Clicked(ev) request_action("install") end
    function window.On.ModelRepair.Clicked(ev) request_action("install") end
    function window.On.ModelSelect.Clicked(ev) request_action("select") end
    function window.On.ModelRefresh.Clicked(ev) refresh() end
    function window.On.ModelImport.Clicked(ev)
        local model = current_model()
        if not model then return end
        local ok, path = pcall(context.browse, model.kind == "asr")
        if not ok then
            set_message("无法打开文件选择器，请重试。")
        elseif path and path ~= "" then
            request_action("import", path)
        end
    end
    local function close()
        action = nil
        window:Hide()
        dispatcher:ExitLoop()
    end
    function window.On.ModelClose.Clicked(ev) close() end
    function window.On.SubFixModelCenter.Close(ev) close() end

    refresh()
    while true do
        action = nil
        window:Show()
        dispatcher:RunLoop()
        window:Hide()
        if not action then break end
        local payload, err = context.perform(action)
        last_operation_error = not payload and tostring(err or "操作失败，请重试或改用离线导入。") or nil
        set_message(payload and (action.kind == "select" and "已切换识别模型。" or "操作完成。") or (last_operation_error:find("已取消", 1, true) and "已取消，可重试。" or "操作失败，可重试或离线导入；原因见上方详情。"))
        refresh()
    end
    return true
end

return ModelCenter
