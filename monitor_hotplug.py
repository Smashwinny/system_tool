#!/usr/bin/env python3
"""Run the layout repair only at startup, DRM hotplug, or screen unlock."""

from __future__ import annotations

import os
import selectors
import subprocess
import time
from pathlib import Path


LAYOUT_SCRIPT = Path(os.environ.get(
    "SYSTEM_TOOL_LAYOUT_SCRIPT", Path.home() / ".local" / "bin" / "fix-monitor-layout.sh"))


def start(command: list[str]) -> subprocess.Popen[str] | None:
    try:
        return subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, bufsize=1)
    except OSError:
        return None


def run_layout() -> None:
    try:
        subprocess.run([str(LAYOUT_SCRIPT)], timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass


def event_kind(source: str, line: str) -> str | None:
    lower = line.lower()
    if source == "drm" and ("/drm/" in lower or "subsystem=drm" in lower):
        return "drm"
    if source == "lock" and "activechanged" in lower and "false" in lower:
        return "unlock"
    return None


def main() -> int:
    os.environ.setdefault("DISPLAY", ":1")
    os.environ.setdefault("XAUTHORITY", f"/run/user/{os.getuid()}/gdm/Xauthority")
    processes = {
        "drm": start(["udevadm", "monitor", "--kernel", "--subsystem-match=drm"]),
        "lock": start(["gdbus", "monitor", "--session", "--dest", "org.gnome.ScreenSaver",
                       "--object-path", "/org/gnome/ScreenSaver"]),
    }
    selector = selectors.DefaultSelector()
    for source, process in processes.items():
        if process and process.stdout:
            selector.register(process.stdout, selectors.EVENT_READ, source)
    run_layout()
    pending_at: float | None = None
    try:
        while selector.get_map():
            timeout = max(0.0, pending_at - time.monotonic()) if pending_at is not None else None
            for key, _ in selector.select(timeout):
                line = key.fileobj.readline()
                if not line:
                    selector.unregister(key.fileobj)
                    continue
                if event_kind(str(key.data), line):
                    pending_at = time.monotonic() + 2.0
            if pending_at is not None and time.monotonic() >= pending_at:
                run_layout()
                pending_at = None
    finally:
        for process in processes.values():
            if process and process.poll() is None:
                process.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
