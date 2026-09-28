#!/usr/bin/env python3
"""Repair layout after real DRM or X11 RandR changes, without polling."""

from __future__ import annotations

import os
import json
import selectors
import subprocess
import time
from pathlib import Path


LAYOUT_SCRIPT = Path(os.environ.get(
    "SYSTEM_TOOL_LAYOUT_SCRIPT", Path.home() / ".local" / "bin" / "fix-monitor-layout.sh"))
STATE_ROOT = Path(os.environ.get(
    "XDG_STATE_HOME", Path.home() / ".local" / "state")) / "system-tool"
LAYOUT_MIN_INTERVAL = 30.0


def start(command: list[str]) -> subprocess.Popen[str] | None:
    try:
        return subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, bufsize=1)
    except OSError:
        return None


def run_layout() -> None:
    if display_suppressed():
        return
    try:
        subprocess.run([str(LAYOUT_SCRIPT)], timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass


def connector_signature(root: Path = Path("/sys/class/drm")) -> tuple[tuple[str, str], ...]:
    rows = []
    for status in root.glob("card*-*/status"):
        try:
            rows.append((status.parent.name, status.read_text(encoding="utf-8").strip()))
        except OSError:
            continue
    return tuple(sorted(rows))


def display_suppressed(now: float | None = None, state_root: Path = STATE_ROOT) -> bool:
    try:
        payload = json.loads((state_root / "display-cooldown.json").read_text(encoding="utf-8"))
        return float(payload.get("until_epoch", 0)) > (time.time() if now is None else now)
    except (OSError, ValueError, TypeError):
        return False


def should_run_layout(kind: str, previous: tuple, current: tuple, suppressed: bool) -> bool:
    if suppressed:
        return False
    if kind == "startup":
        return True
    if kind == "randr":
        return True
    return kind == "drm" and current != previous


def event_kind(source: str, line: str) -> str | None:
    lower = line.lower()
    if source == "drm" and ("/drm/" in lower or "subsystem=drm" in lower):
        return "drm"
    if source == "randr" and "notify event" in lower:
        return "randr"
    if source == "lock" and "activechanged" in lower:
        if "true" in lower:
            return "lock"
        if "false" in lower:
            return "unlock"
    return None


def event_delay(kind: str) -> float:
    # Give the NVIDIA/GNOME transition time to settle and the guard time to
    # install its display cooldown if the event becomes an error storm.
    return 10.0 if kind == "randr" else 2.0


def main() -> int:
    os.environ.setdefault("DISPLAY", ":1")
    os.environ.setdefault("XAUTHORITY", f"/run/user/{os.getuid()}/gdm/Xauthority")
    processes = {
        "drm": start(["udevadm", "monitor", "--kernel", "--subsystem-match=drm"]),
        "randr": start(["stdbuf", "-oL", "xev", "-root", "-event", "randr"]),
        "lock": start(["gdbus", "monitor", "--session", "--dest", "org.gnome.ScreenSaver",
                       "--object-path", "/org/gnome/ScreenSaver"]),
    }
    selector = selectors.DefaultSelector()
    for source, process in processes.items():
        if process and process.stdout:
            selector.register(process.stdout, selectors.EVENT_READ, source)
    signature = connector_signature()
    if should_run_layout("startup", signature, signature, display_suppressed()):
        run_layout()
    last_layout_at = time.monotonic()
    pending_at: float | None = None
    pending_kind: str | None = None
    screen_locked = False
    dirty_while_locked = False
    try:
        while selector.get_map():
            timeout = max(0.0, pending_at - time.monotonic()) if pending_at is not None else None
            for key, _ in selector.select(timeout):
                line = key.fileobj.readline()
                if not line:
                    selector.unregister(key.fileobj)
                    continue
                kind = event_kind(str(key.data), line)
                if kind == "lock":
                    screen_locked = True
                elif kind == "unlock":
                    screen_locked = False
                    if dirty_while_locked:
                        pending_at = time.monotonic() + 15.0
                        pending_kind = "randr"
                        dirty_while_locked = False
                elif kind == "randr" and screen_locked:
                    dirty_while_locked = True
                elif kind:
                    pending_at = time.monotonic() + event_delay(kind)
                    pending_kind = "randr" if kind == "randr" else (pending_kind or "drm")
            if pending_at is not None and time.monotonic() >= pending_at:
                current = connector_signature()
                interval_ok = time.monotonic() - last_layout_at >= LAYOUT_MIN_INTERVAL
                if (interval_ok and
                        should_run_layout(pending_kind or "drm", signature, current,
                                          display_suppressed())):
                    run_layout()
                    last_layout_at = time.monotonic()
                signature = current
                pending_at = None
                pending_kind = None
    finally:
        for process in processes.values():
            if process and process.poll() is None:
                process.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
