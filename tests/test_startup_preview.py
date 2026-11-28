from pathlib import Path

import pytest

from test_normalize_length_options import run_lua


SOURCE = (Path(__file__).resolve().parents[1] / "SubFix.lua").read_text()


def test_mini_placement_uses_resolve_screen_and_respects_window_movement(run_lua):
    geometry = SOURCE[SOURCE.index("function SUBFIX_WINDOW_GEOMETRY.center_in_bounds"):SOURCE.index("-- ========== 全局状态")]
    run_lua("SUBFIX_WINDOW_GEOMETRY = {}\n" + geometry + '''
        local initial = {100, 50, 435, 382}
        local screen = {x=-1920, y=30, width=1920, height=1050}
        local window = {Geometry={unpack(initial)}}
        local place = SUBFIX_WINDOW_GEOMETRY.finish_mini_placement
        place(window, initial, nil)
        assert(window.Geometry[1] == 100)
        place(window, initial, screen)
        assert(window.Geometry[1] == -1178 and window.Geometry[2] == 364)
        window.Geometry = {210, 50, 435, 382}
        place(window, initial, screen)
        assert(window.Geometry[1] == 210)
        window.Geometry = {100, 50, 600, 382}
        place(window, initial, screen)
        assert(window.Geometry[1] == 100 and window.Geometry[3] == 600)
    ''')


@pytest.mark.parametrize("mode", ["bulk", "unavailable", "partial"])
def test_preview_keeps_order_selection_and_maps_after_insert(run_lua, mode):
    renderer = SOURCE[SOURCE.index("render_rows_to_window = function"):SOURCE.index("function compute_render_signature")]
    run_lua(f'mode = "{mode}"\n' + '''
        tree = {}
        function tree:Clear() self.items = {} end
        function tree:NewItem()
            return {Text={}, SetText=function(self, col, text) self.Text[col] = text end}
        end
        function tree:AddTopLevelItem(item) table.insert(self.items,item) end
        if mode ~= "unavailable" then
            function tree:AddTopLevelItems(items)
                if mode == "partial" then self.items = {items[1]} else self.items = items end
            end
        end
        function tree:TopLevelItemCount() return #self.items end
        function tree:SetSelectedNode(node) selected = node end
        function resolve_window(w) return w end
        function find_window_item() return tree end
        function apply_preview_tree_layout() end
        function with_tree_updates_suspended(t,w,fn) return pcall(fn) end
        function trim_text(s) return s end
        function set_preview_tree_maps_for_window(_, by_item, by_id) map, ids = by_item, by_id end
        function set_preview_tree_node_display_text(item,text) item.Text[1] = text end
    ''' + renderer + '''
        rows = {{id="a", display_text="甲"}, {id="b", display_text="乙"}}
        current_selected_row_id = "b"
        assert(render_rows_to_window({},rows)==2 and #tree.items==2)
        for i,row in ipairs(rows) do
            local item = tree.items[i]
            assert(item.Text[1]==row.display_text and map[item]==row and ids[row.id]==item)
        end
        assert(selected == tree.items[2])
    ''')


def test_startup_track_notification_does_not_trigger_second_refresh(run_lua):
    handler = SOURCE[SOURCE.index("function mini_win.On.MiniTrackSpin.TextChanged"):SOURCE.index("function mini_win.On.MiniTrackSpinUp.Clicked")]
    run_lua('''
        mini_win = {On={MiniTrackSpin={}}}
        current_track, refreshes = 1, 0
        function trim(s) return s end
        function set_target_track_value(track) assert(track==2) end
        function refresh_subtitles() refreshes=refreshes+1 end
    ''' + handler + '''
        mini_win.On.MiniTrackSpin.TextChanged({Text="1"})
        assert(refreshes==0)
        mini_win.On.MiniTrackSpin.TextChanged({Text="2"})
        mini_win.On.MiniTrackSpin.TextChanged({Text="2"})
        assert(current_track==2 and refreshes==1)
    ''')
