"""Model center interaction contracts in Resolve's Lua runtime."""
import json
from pathlib import Path

import pytest

from test_normalize_length_options import run_lua  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
UI_PATH = ROOT / ".subfix_support/subfix_model_center.lua"


@pytest.fixture
def center(run_lua):
    run_lua("center = dofile(" + json.dumps(str(UI_PATH)) + ")")
    run_lua('''
        controls = {}; operations = {}; iteration = 0
        ui = setmetatable({}, {__index = function() return function(_, v)
            if type(v) == 'table' and v.ID then
                v.CurrentIndex = 0
                v.AddItem = function() end
                v.Clear = function() end
                controls[v.ID] = v
            end
            return v
        end end})
        dispatcher = {
            AddWindow = function(_, spec)
                dialog = {GetItems = function() return controls end,
                    Show = function() end, Hide = function() end,
                    GetGeometry = function() return {420, 240, 500, 180} end,
                    Resize = function() end, RecalcLayout = function() end,
                    On = setmetatable({}, {__index = function(t, k)
                        local handlers = {}; rawset(t, k, handlers); return handlers
                    end})}
                return dialog
            end,
            RunLoop = function()
                iteration = iteration + 1
                assert(iteration < 5, 'dialog loop failed to close')
                interaction(dialog, controls, iteration)
            end,
            ExitLoop = function() end
        }
        models = {
            {id = 'qwen3-asr-0.6b', label = 'Qwen 0.6B', kind = 'asr',
             state = 'missing', ready = false, download_available = true},
            {id = 'qwen3-forced-aligner-0.6b', label = 'Aligner', kind = 'aligner',
             state = 'missing', ready = false, download_available = false,
             download_unavailable_reason = '请导入配套文件'}
        }
        context = {ui = ui, dispatcher = dispatcher, geometry = function(v) return v end,
            query = function(action)
                assert(action == 'list')
                return {models = models, selected_model_id = 'qwen3-asr-0.6b'}
            end,
            perform = function(action)
                operations[#operations + 1] = action
                if cancel_task then return nil, '已取消；可重试' end
                return {ok = true}
            end,
            browse = function(directory) return directory and '/offline/asr' or '/offline/aligner.gguf' end}
    ''')
    return run_lua


def test_selection_never_starts_a_download(center):
    center('''
        models[1].ready = true
        context.query = function()
            return {models = models, selected_model_id = 'qwen3-asr-1.7b'}
        end
        interaction = function(w, items, step)
            if step == 1 then
                assert(items.ModelSelect.Enabled)
                w.On.ModelSelect.Clicked()
            else w.On.ModelClose.Clicked() end
        end
        assert(center.open(context))
        assert(#operations == 1 and operations[1].kind == 'select')
        assert(operations[1].model_id == 'qwen3-asr-0.6b')
    ''')


@pytest.mark.parametrize("model_index, expected_path", [(1, "/offline/asr"), (2, "/offline/aligner.gguf")])
def test_import_uses_picker_and_imports_in_one_step(center, model_index, expected_path):
    center(f'context.focus_model_id = models[{model_index}].id; expected_path = {json.dumps(expected_path)}')
    center('''
        interaction = function(w, items, step)
            if step == 1 then
                if context.focus_model_id == models[2].id then
                    assert(items.ModelDownload.Enabled == false, 'unpublished URL became a fake download')
                    assert(items.ModelSelect.Enabled == false)
                    assert(items.ModelDetails.PlainText:find('请导入配套文件'))
                end
                w.On.ModelImport.Clicked()
            else w.On.ModelClose.Clicked() end
        end
        assert(center.open(context))
        assert(#operations == 1 and operations[1].kind == 'import')
        assert(operations[1].input_path == expected_path)
    ''')


@pytest.mark.parametrize("picker", ["return nil", "return ''", "error('picker unavailable')"])
def test_cancelled_or_failed_picker_does_not_import(center, picker):
    center('context.browse = function() ' + picker + ' end')
    center('''
        interaction = function(w, items)
            w.On.ModelImport.Clicked()
            assert(#operations == 0)
            w.On.ModelClose.Clicked()
        end
        assert(center.open(context))
        assert(#operations == 0)
    ''')


def test_cancelled_download_returns_to_center_with_error(center):
    center('''
        cancel_task = true
        interaction = function(w, items, step)
            if step == 1 then
                items.ModelSource.CurrentIndex = 1
                w.On.ModelDownload.Clicked()
            else
                assert(items.ModelMessage.Text:find('已取消'))
                assert(items.ModelDownload.Enabled)
                w.On.SubFixModelCenter.Close()
            end
        end
        assert(center.open(context))
        assert(#operations == 1 and operations[1].source == 'modelscope')
    ''')


def test_main_and_core_compile_in_resolve(run_lua):
    for path in (ROOT / "SubFix.lua", ROOT / ".subfix_support/subfix_generate_selection_core.lua", UI_PATH):
        run_lua("assert(loadfile(" + json.dumps(str(path)) + "))")
