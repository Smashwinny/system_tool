#!/usr/bin/env python3
"""Conservatively bring fully off-screen X11 application windows back."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass


WORKAREA_RE = re.compile(r"\bWA:\s*(-?\d+),(-?\d+)\s+(\d+)x(\d+)")
DESKTOP_ORIGIN_RE = re.compile(r"^@!(-?\d+),(-?\d+);")
SKIP_CLASSES = ("gjs.Gjs", "gnome-shell.Gnome-shell")


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class Window:
    window_id: str
    desktop: int
    rect: Rect
    wm_class: str
    title: str


def command_output(command: list[str], timeout: float = 2.0) -> str | None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                                check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def parse_workarea(output: str) -> Rect | None:
    for line in output.splitlines():
        if " * " not in line:
            continue
        match = WORKAREA_RE.search(line)
        if match:
            return Rect(*(int(value) for value in match.groups()))
    return None


def parse_windows(output: str) -> list[Window]:
    windows: list[Window] = []
    for line in output.splitlines():
        fields = line.split(None, 8)
        if len(fields) < 8:
            continue
        try:
            window_id, desktop = fields[0], int(fields[1])
            rect = Rect(*(int(value) for value in fields[2:6]))
        except ValueError:
            continue
        wm_class = fields[6]
        title = fields[8] if len(fields) == 9 else ""
        windows.append(Window(window_id, desktop, rect, wm_class, title))
    return windows


def has_useful_overlap(window: Rect, workarea: Rect) -> bool:
    overlap_width = min(window.x + window.width, workarea.x + workarea.width) - max(window.x, workarea.x)
    overlap_height = min(window.y + window.height, workarea.y + workarea.height) - max(window.y, workarea.y)
    # A reachable title-bar-sized patch is enough; partially off-screen windows are left alone.
    return overlap_width >= 80 and overlap_height >= 32


def safe_position(window: Rect, workarea: Rect, offset: int = 0) -> tuple[int, int]:
    margin = 24
    max_x = workarea.x + max(0, workarea.width - min(window.width, workarea.width) - margin)
    max_y = workarea.y + max(0, workarea.height - min(window.height, workarea.height) - margin)
    x = min(max(window.x, workarea.x + margin), max_x)
    y = min(max(window.y, workarea.y + margin), max_y)
    # Avoid placing every recovered window on exactly the same pixel.
    return max(workarea.x, x - offset), max(workarea.y, y - offset)


def coordinate_scale(windows: list[Window]) -> float:
    """Infer Mutter's move-coordinate scale from DING desktop markers."""
    candidates: list[float] = []
    for window in windows:
        if window.wm_class != "gjs.Gjs":
            continue
        match = DESKTOP_ORIGIN_RE.match(window.title)
        if not match:
            continue
        expected_x, expected_y = (int(value) for value in match.groups())
        if expected_x and window.rect.x:
            candidates.append(abs(window.rect.x / expected_x))
        if expected_y and window.rect.y:
            candidates.append(abs(window.rect.y / expected_y))
    plausible = [value for value in candidates if 0.5 <= value <= 4.0]
    return sorted(plausible)[len(plausible) // 2] if plausible else 1.0


def repair_offscreen_windows(dry_run: bool = False) -> list[tuple[Window, int, int]]:
    desktop_output = command_output(["wmctrl", "-d"])
    window_output = command_output(["wmctrl", "-lGx"])
    if desktop_output is None or window_output is None:
        return []
    workarea = parse_workarea(desktop_output)
    if workarea is None or workarea.width <= 0 or workarea.height <= 0:
        return []

    windows = parse_windows(window_output)
    move_scale = coordinate_scale(windows)
    repairs: list[tuple[Window, int, int]] = []
    for window in windows:
        if window.desktop < -1 or window.rect.width <= 0 or window.rect.height <= 0:
            continue
        if any(window.wm_class.endswith(name) for name in SKIP_CLASSES):
            continue
        if has_useful_overlap(window.rect, workarea):
            continue
        offset = (len(repairs) % 8) * 24
        new_x, new_y = safe_position(window.rect, workarea, offset)
        repairs.append((window, new_x, new_y))
        if not dry_run:
            try:
                command_x = round(new_x / move_scale)
                command_y = round(new_y / move_scale)
                subprocess.run(["wmctrl", "-ir", window.window_id, "-e",
                                f"0,{command_x},{command_y},-1,-1"], timeout=2, check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.TimeoutExpired):
                continue
    return repairs


def main() -> int:
    os.environ.setdefault("DISPLAY", ":1")
    os.environ.setdefault("XAUTHORITY", f"/run/user/{os.getuid()}/gdm/Xauthority")
    repairs = repair_offscreen_windows(dry_run="--dry-run" in os.sys.argv[1:])
    for window, x, y in repairs:
        print(f"{window.window_id} {window.wm_class} -> {x},{y} {window.title}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
