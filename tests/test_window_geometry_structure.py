from pathlib import Path
import json
import re
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "SubFix.lua"
GENERATE = ROOT / ".subfix_support" / "subfix_generate_selection_core.lua"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_main_script_uses_resolve_display_geometry_for_every_window():
    source = read(MAIN)

    assert "SUBFIX_WINDOW_GEOMETRY = SUBFIX_WINDOW_GEOMETRY or {}" in source
    assert "function SUBFIX_WINDOW_GEOMETRY.resolve_screen_bounds()" in source
    assert "CGWindowListCopyWindowInfo" in source
    assert "console.log" not in geometry_jxa(source)
    assert "function SUBFIX_WINDOW_GEOMETRY.centered_geometry(fallback_geometry)" in source
    assert "return fallback_geometry" in source
    assert source.count("Geometry = SUBFIX_WINDOW_GEOMETRY.centered_geometry(") >= 20
    assert "win:SetAttrs({Geometry = SUBFIX_WINDOW_GEOMETRY.centered_geometry({500, 120, 500, 700})})" in source


def test_generate_core_uses_resolve_display_geometry_for_every_window():
    source = read(GENERATE)

    assert "SUBFIX_WINDOW_GEOMETRY = SUBFIX_WINDOW_GEOMETRY or {}" in source
    assert "function SUBFIX_WINDOW_GEOMETRY.resolve_screen_bounds()" in source
    assert geometry_jxa(source) == geometry_jxa(read(MAIN))
    assert "function SUBFIX_WINDOW_GEOMETRY.centered_geometry(fallback_geometry)" in source
    assert "return fallback_geometry" in source
    assert source.count("Geometry = SUBFIX_WINDOW_GEOMETRY.centered_geometry(") >= 9
    assert "progress_window:SetAttrs({Geometry = SUBFIX_WINDOW_GEOMETRY.centered_geometry(GENERATE_WRITEBACK_OVERLAY_GEOMETRY)})" in source


def geometry_jxa(source):
    return re.search(r"local jxa = \[\[(.*?)\]\]", source, re.S).group(1)


def frame(x, y, width, height):
    return {"origin": {"x": x, "y": y}, "size": {"width": width, "height": height}}


def window(x, y, width, height):
    return {"kCGWindowOwnerName": "DaVinci Resolve", "kCGWindowLayer": 0,
            "kCGWindowBounds": {"X": x, "Y": y, "Width": width, "Height": height}}


@pytest.mark.skipif(sys.platform != "darwin", reason="JXA requires macOS")
@pytest.mark.parametrize("secondary,main_window,expected", [
    (frame(2560, 360, 1920, 1080), window(2560, 30, 1920, 1050), "2560,0,1920,1080"),
    (frame(-1920, 0, 1920, 1080), window(-1920, 400, 1920, 1000), "-1920,360,1920,1080"),
    (frame(0, 1440, 1920, 1080), window(0, -1080, 1920, 1050), "0,-1080,1920,1080"),
    (frame(2560, 360, 1920, 1080), None, "0,30,2560,1410"),
])
def test_geometry_selects_resolve_main_window_screen(secondary, main_window, expected):
    screens = [{"frame": frame(0, 0, 2560, 1440), "visibleFrame": frame(0, 0, 2560, 1410)},
               {"frame": secondary, "visibleFrame": secondary}]
    # A small plugin window on another display must not override the main window.
    windows = [window(500, 300, 360, 100), main_window] if main_window else []
    script = geometry_jxa(read(MAIN)).replace("ObjC.", "mockObjC.").replace("$.", "mockDollar.")
    mock = '''
        const mockScreens = SCREENS;
        const mockWindows = WINDOWS;
        const mockObjC = {import: function() {}, deepUnwrap: x => x, castRefToObject: x => x};
        const mockDollar = {NSApplication: {}, NSScreen: {screens: {
            count: mockScreens.length, objectAtIndex: i => mockScreens[i]}},
            CGWindowListCopyWindowInfo: () => mockWindows};
        eval(SCRIPT);
    '''.replace("SCREENS", json.dumps(screens)).replace("WINDOWS", json.dumps(windows)).replace("SCRIPT", json.dumps(script))
    result = subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", "-e", mock], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


@pytest.mark.skipif(sys.platform != "darwin", reason="JXA requires macOS")
def test_geometry_probe_returns_coordinates_on_stdout():
    result = subprocess.run(["/usr/bin/osascript", "-l", "JavaScript", "-e", geometry_jxa(read(MAIN))], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert re.fullmatch(r"-?[\d.]+,-?[\d.]+,[\d.]+,[\d.]+", result.stdout.strip())
