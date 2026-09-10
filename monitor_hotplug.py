#!/usr/bin/env python3
"""Run the layout repair only at startup, DRM hotplug, or screen unlock."""

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
    if kind in {"startup", "unlock"}:
        return True
    return kind == "drm" and current != previous


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
    signature = connector_signature()
    if should_run_layout("startup", signature, signature, display_suppressed()):
        run_layout()
    pending_at: float | None = None
    pending_kind: str | None = None
    try:
        while selector.get_map():
            timeout = max(0.0, pending_at - time.monotonic()) if pending_at is not None else None
            for key, _ in selector.select(timeout):
                line = key.fileobj.readline()
                if not line:
                    selector.unregister(key.fileobj)
                    continue
                kind = event_kind(str(key.data), line)
                if kind:
                    pending_at = time.monotonic() + 2.0
                    pending_kind = "unlock" if kind == "unlock" else (pending_kind or "drm")
            if pending_at is not None and time.monotonic() >= pending_at:
                current = connector_signature()
                if should_run_layout(pending_kind or "drm", signature, current, display_suppressed()):
                    run_layout()
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
