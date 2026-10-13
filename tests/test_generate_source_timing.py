"""Exercise source/timecode conversion in Resolve's Lua runtime."""

import json
import os
from pathlib import Path

import pytest

from test_generate_progress_lifecycle import run_lua  # noqa: F401


SOURCE_PATH = Path(os.environ.get("SUBFIX_TEST_GENERATE_CORE", str(
    Path(__file__).resolve().parents[1] / ".subfix_support/subfix_generate_selection_core.lua"
)))
SOURCE = SOURCE_PATH.read_text()
TIMING = SOURCE[SOURCE.index("local function get_audio_item_source_offset_frames("):
                SOURCE.index("local function build_audio_track_options_for_dialog(")]


@pytest.mark.parametrize("source_fps,timeline_fps,source_frame", [
    (59.94, 29.97, 492),
    (59.94, 29.97, 1261),
    (59.94, 29.97, 2021),
    (25, 25, 250),
    (24, 30, 240),
])
@pytest.mark.parametrize("linked", [False, True])
def test_source_audio_uses_source_fps_and_scope_uses_timeline_fps(
    run_lua, source_fps, timeline_fps, source_frame, linked
):
    run_lua(f"source_fps={source_fps}; timeline_fps={timeline_fps}; source_frame={source_frame}; linked={str(linked).lower()}")
    run_lua(HARNESS + TIMING + """
        -- Select seconds 2..5 within this timeline clip, not the full clip.
        local sources = assert(collect_audio_sources_for_scope(timeline,
            {mode='selection', start_frame=1000+2*timeline_fps, end_frame=1000+5*timeline_fps}, timeline_fps))
        assert(#sources == 1)
        local expected = source_frame/source_fps + 2 + (linked and 0.5 or 0)
        assert(math.abs(sources[1].source_start_seconds-expected)<0.000001,
            'wrong audio start: '..sources[1].source_start_seconds..' expected '..expected)
        assert(math.abs(sources[1].source_end_seconds-expected-3)<0.000001, 'wrong selected duration')
        assert(sources[1].file_path == (linked and '/linked.wav' or '/source.mov'))
        assert(sources[1].audio_channel_index == (linked and 1 or nil))
    """)


@pytest.mark.parametrize("failure", ["source_api", "fps_api", "missing_fps", "zero_fps", "negative_fps"])
def test_legacy_offset_keeps_timeline_frame_units(run_lua, failure):
    run_lua("source_fps=59.94; timeline_fps=29.97; source_frame=492; linked=false")
    run_lua(HARNESS + "failure=" + json.dumps(failure) + """
        if failure == 'source_api' then item.GetSourceStartFrame = nil
        else
            media.GetClipProperty = function(_, key)
                if key == 'File Path' then return '/source.mov' end
                if failure == 'fps_api' then error('unavailable') end
                if failure == 'zero_fps' then return 0 end
                if failure == 'negative_fps' then return -1 end
                return ''
            end
        end
    """ + TIMING + """
        local sources = assert(collect_audio_sources_for_scope(timeline,
            {mode='selection', start_frame=1000, end_frame=1000+3*timeline_fps}, timeline_fps))
        assert(math.abs(sources[1].source_start_seconds-246/timeline_fps)<0.000001,
            'fallback mixed source frames with timeline fps')
    """)


def test_complete_module_compiles(run_lua):
    run_lua("assert(loadfile(" + json.dumps(str(SOURCE_PATH)) + "))")


HARNESS = """
    WORK_SCOPE_MODE_SELECTION = 'selection'
    file_exists = function() return true end
    basename = function(path) return path end
    get_audio_track_display_name = function() return 'A1' end
    decode_json_text = function()
        return {embedded_audio_channels=2, track_mapping={['1']={channel_idx={3}}},
            linked_audio=linked and {['1']={path='/linked.wav',channels=2,offset=24000}} or {}}
    end
    media = {GetClipProperty=function(_, key)
        if key == 'FPS' then return source_fps end
        if key == 'Sample Rate' then return 48000 end
        if key == 'File Path' then return '/source.mov' end
    end}
    item = {
        GetStart=function() return 1000 end,
        GetEnd=function() return 1000+10*timeline_fps end,
        GetSourceStartFrame=function() return source_frame end,
        GetLeftOffset=function() return 246 end,
        GetMediaPoolItem=function() return media end,
        GetSourceAudioChannelMapping=function() return 'mapping' end,
        GetProperty=function() return false end,
    }
    timeline = {GetTrackCount=function() return 1 end,
        GetItemListInTrack=function() return {item} end}
"""
