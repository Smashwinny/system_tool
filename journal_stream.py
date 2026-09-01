#!/usr/bin/env python3
"""Low-overhead kernel journal event stream and duplicate aggregation."""

from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
import time
from collections import defaultdict, deque
from typing import Any


ALERT_PATTERN = re.compile(r"oom|out of memory|killed process|nvrm|xid|thermal|i/o error|ext4-fs error", re.I)


def fingerprint(message: str) -> str:
    value = message.lower()
    value = re.sub(r"0x[0-9a-f]+", "<hex>", value)
    value = re.sub(r"\b\d{3,}\b", "<n>", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:240]


class JournalWatcher:
    def __init__(self) -> None:
        self.events: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=2000)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.process: subprocess.Popen[str] | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, name="system-tool-journal", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()
        if self.thread:
            self.thread.join(timeout=2)

    def _run(self) -> None:
        delay = 1.0
        while not self.stop_event.is_set():
            try:
                self.process = subprocess.Popen(
                    ["journalctl", "-k", "-f", "-n", "0", "-o", "json", "--no-pager"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                )
                assert self.process.stdout is not None
                delay = 1.0
                for line in self.process.stdout:
                    if self.stop_event.is_set():
                        break
                    try:
                        message = str(json.loads(line).get("MESSAGE", ""))
                    except (ValueError, TypeError):
                        continue
                    if not ALERT_PATTERN.search(message):
                        continue
                    event = {"time": time.time(), "message": message[-500:], "fingerprint": fingerprint(message)}
                    try:
                        self.events.put_nowait(event)
                    except queue.Full:
                        try:
                            self.events.get_nowait()
                            self.events.put_nowait(event)
                        except queue.Empty:
                            pass
            except OSError:
                pass
            if not self.stop_event.wait(delay):
                delay = min(30.0, delay * 2)

    def drain(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        while True:
            try:
                rows.append(self.events.get_nowait())
            except queue.Empty:
                return rows


class EventAggregator:
    def __init__(self, window_seconds: float = 60.0) -> None:
        self.window_seconds = window_seconds
        self.times: dict[str, deque[float]] = defaultdict(deque)
        self.latest: dict[str, str] = {}

    def add(self, event: dict[str, Any]) -> None:
        key = str(event["fingerprint"])
        timestamp = float(event["time"])
        self.times[key].append(timestamp)
        self.latest[key] = str(event["message"])

    def summary(self, now: float) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for key in list(self.times):
            values = self.times[key]
            while values and values[0] < now - self.window_seconds:
                values.popleft()
            if not values:
                del self.times[key]
                self.latest.pop(key, None)
                continue
            rows.append({"fingerprint": key, "count": len(values), "message": self.latest[key][-180:]})
            rows[-1]["first_time"] = values[0]
            rows[-1]["last_time"] = values[-1]
        return sorted(rows, key=lambda row: int(row["count"]), reverse=True)
