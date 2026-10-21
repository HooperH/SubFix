"""Exercise close events before and after the full window is initialized."""

from pathlib import Path

import pytest

from test_normalize_length_options import run_lua  # noqa: F401


SOURCE = (Path(__file__).resolve().parents[1] / "SubFix.lua").read_text(encoding="utf-8")


def section(start, end):
    return SOURCE[SOURCE.index(start):SOURCE.index(end)]


@pytest.mark.parametrize("full_initialized", [False, True])
def test_close_event_hides_windows_and_exits(run_lua, full_initialized):
    run_lua("full_initialized = " + str(full_initialized).lower())
    run_lua('''
        print = function() end
        local handle_main_window_close
        local mini_hidden, full_hidden, review_hidden, exits, cancels = 0, 0, 0, 0, 0
        local mini_win = {On = {mini = {}}, Hide = function() mini_hidden = mini_hidden + 1 end}
        local WINDOW_META = {mini_window_id = "mini"}
        local dispatcher = {ExitLoop = function() exits = exits + 1 end}
        local win, pre_delivery_review_window
        function kill_normalize_background_process() cancels = cancels + 1 end
        if full_initialized then
            win = {Hide = function() full_hidden = full_hidden + 1 end}
            pre_delivery_review_window = {Hide = function() review_hidden = review_hidden + 1 end}
    ''' + section("function close_pre_delivery_review_window()", "function show_pre_delivery_review_window(") + '''
        end
    ''' + section("mini_win.On[WINDOW_META.mini_window_id].Close", "function mini_win.On.MiniTrackSpin.TextChanged")
        + section("handle_main_window_close = function()", "-- 完整版关闭与强制退出事件") + '''
        mini_win.On.mini.Close({})
        assert(mini_hidden == 1, "mini window remained visible")
        assert(exits > 0, "event loop did not exit")
        assert(AI_CANCEL_REQUESTED and NORMALIZE_CANCEL_REQUESTED and cancels == 1)
        assert(full_hidden == (full_initialized and 1 or 0))
        assert(review_hidden == (full_initialized and 1 or 0))
        assert(pre_delivery_review_window == nil)
    ''')
