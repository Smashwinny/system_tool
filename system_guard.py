#!/usr/bin/env python3
"""Background pressure guard with conservative automatic process termination."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from incident_store import IncidentStore
from journal_stream import EventAggregator, JournalWatcher
from system_tool import Monitor, ProcessRow, human_bytes, parse_meminfo, parse_psi, read_text


MIB = 1024 * 1024
GIB = 1024 * MIB
PROTECTED_GROUPS = {"Desktop", "Codex/ChatGPT"}
AGGREGATED_APP_GROUPS = {
    "Build/Compiler", "ROS/RViz", "Chrome", "Cursor", "Feishu", "WeChat", "Java/Gradle",
}
PROTECTED_TOKENS = (
    "system-tool", "system_guard.py", "systemd", "init", "gnome-shell", "xorg", "gdm",
    "sshd", "gnome-terminal", "ptyxis", "dbus-daemon", "pipewire", "wireplumber",
)
INVALID_HEAD_TEXT = "dispcmnctrlcmdsystemgetvblankcounter_impl: invalid head number"
APPINDICATOR_RECURSION_TEXT = "js error: too much recursion"


@dataclass
class GuardConfig:
    sample_interval: float = 5.0
    process_interval: float = 15.0
    gpu_interval: float = 30.0
    critical_available_bytes: int = 768 * MIB
    critical_available_ratio: float = 0.05
    critical_memory_psi: float = 20.0
    critical_full_psi: float = 8.0
    critical_swap_out_per_sec: float = 100 * MIB
    sustain_seconds: float = 45.0
    offender_min_bytes: int = 2 * GIB
    term_grace_seconds: float = 10.0
    action_cooldown_seconds: float = 600.0
    nvrm_burst_per_minute: int = 10
    log_storm_per_minute: int = 100
    kernel_notification_cooldown_seconds: float = 600.0
    display_error_unlocked_sustain_seconds: float = 60.0
    desktop_growth_window_seconds: float = 60.0
    desktop_growth_bytes: int = 768 * MIB
    desktop_growth_floor_bytes: int = 2 * GIB
    desktop_action_cooldown_seconds: float = 1800.0


def current_boot_id() -> str:
    return read_text(Path("/proc/sys/kernel/random/boot_id")).strip()


def compact_sample(snapshot: dict[str, Any], state: str, log_summary: list[dict[str, Any]]) -> dict[str, Any]:
    memory = snapshot["memory"]
    groups = snapshot.get("groups", {})
    top_groups = dict(sorted(
        groups.items(),
        key=lambda item: float(item[1].get("rss", 0)) + float(item[1].get("swap", 0)),
        reverse=True,
    )[:12])
    blocked = []
    for row in snapshot.get("blocked", [])[:5]:
        compact_row = dict(row)
        compact_row["command"] = str(compact_row.get("command", ""))[:240]
        blocked.append(compact_row)
    return {
        "timestamp": snapshot["timestamp"],
        "epoch": time.time(),
        "boot_id": current_boot_id(),
        "state": state,
        "cpu_percent": snapshot["cpu_percent"],
        "iowait_percent": snapshot["iowait_percent"],
        "load": snapshot["load"],
        "memory": memory,
        "memory_psi": snapshot["memory_psi"],
        "io_psi": snapshot["io_psi"],
        "cpu_psi": snapshot["cpu_psi"],
        "blocked": blocked,
        "groups": top_groups,
        "gpu": snapshot["gpu"],
        "kernel_alerts": log_summary[:5],
    }


def memory_is_critical(snapshot: dict[str, Any], config: GuardConfig) -> tuple[bool, str]:
    memory = snapshot["memory"]
    total = max(1, int(memory["total"]))
    available = int(memory["available"])
    low_limit = max(config.critical_available_bytes, int(total * config.critical_available_ratio))
    some = float(snapshot["memory_psi"].get("some_avg10", 0))
    full = float(snapshot["memory_psi"].get("full_avg10", 0))
    swap_out = float(memory.get("swap_out_per_sec", 0))
    pressure = some >= config.critical_memory_psi or full >= config.critical_full_psi or swap_out >= config.critical_swap_out_per_sec
    detail = f"available={human_bytes(available)}, PSI some/full={some:.1f}/{full:.1f}%, swap-out={human_bytes(swap_out)}/s"
    return available <= low_limit and pressure, detail


def process_uid(pid: int) -> int | None:
    status = read_text(Path("/proc") / str(pid) / "status")
    match = next((line for line in status.splitlines() if line.startswith("Uid:")), "")
    try:
        return int(match.split()[1])
    except (IndexError, ValueError):
        return None


def protected_process(row: ProcessRow, ancestry: set[int]) -> bool:
    command = row.command.lower()
    return (row.pid in ancestry or row.group in PROTECTED_GROUPS or
            any(token in command for token in PROTECTED_TOKENS))


def select_offender(processes: list[ProcessRow], minimum: int, uid: int | None = None,
                    ancestry: set[int] | None = None) -> tuple[str, list[ProcessRow], int] | None:
    owner = os.getuid() if uid is None else uid
    protected = ancestry or set()
    groups: dict[str, list[ProcessRow]] = {}
    for row in processes:
        if process_uid(row.pid) != owner or protected_process(row, protected):
            continue
        key = row.group if row.group in AGGREGATED_APP_GROUPS else f"{row.group} (PID {row.pid})"
        groups.setdefault(key, []).append(row)
    ranked = sorted(
        ((name, rows, sum(row.rss for row in rows)) for name, rows in groups.items()),
        key=lambda item: item[2], reverse=True,
    )
    return ranked[0] if ranked and ranked[0][2] >= minimum else None


def process_ancestry(pid: int) -> set[int]:
    result: set[int] = set()
    current = pid
    while current > 1 and current not in result:
        result.add(current)
        stat = read_text(Path("/proc") / str(current) / "stat").split()
        try:
            current = int(stat[3])
        except (IndexError, ValueError):
            break
    return result


def descendants(targets: set[int]) -> set[int]:
    parents: dict[int, int] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        stat = read_text(entry / "stat").split()
        try:
            parents[int(entry.name)] = int(stat[3])
        except (IndexError, ValueError):
            continue
    expanded = set(targets)
    changed = True
    while changed:
        changed = False
        for pid, parent in parents.items():
            if parent in expanded and pid not in expanded:
                expanded.add(pid)
                changed = True
    return expanded


def signal_targets(pids: set[int], sig: int, sender: Callable[[int, int], None] = os.kill) -> list[int]:
    sent: list[int] = []
    for pid in sorted(pids, reverse=True):
        try:
            if process_uid(pid) != os.getuid():
                continue
            sender(pid, sig)
            sent.append(pid)
        except (ProcessLookupError, PermissionError):
            continue
    return sent


def notify(title: str, body: str) -> None:
    if not shutil_which("notify-send"):
        return
    try:
        subprocess.run(["notify-send", "--urgency=critical", title, body], timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired):
        pass


def shutil_which(command: str) -> str | None:
    from shutil import which
    return which(command)


def screen_lock_state() -> str:
    if shutil_which("gdbus"):
        try:
            result = subprocess.run(
                ["gdbus", "call", "--session", "--dest", "org.gnome.ScreenSaver",
                 "--object-path", "/org/gnome/ScreenSaver", "--method", "org.gnome.ScreenSaver.GetActive"],
                capture_output=True, text=True, timeout=2, check=False,
            )
            if result.returncode == 0:
                if "true" in result.stdout.lower():
                    return "locked"
                if "false" in result.stdout.lower():
                    return "unlocked"
        except (OSError, subprocess.TimeoutExpired):
            pass
    return "unknown"


def gnome_shell_rss(processes: list[ProcessRow]) -> int:
    return sum(row.rss for row in processes
               if row.command.split(" ", 1)[0].endswith("/gnome-shell") or row.command == "gnome-shell")


def previous_boot_was_unclean(store: IncidentStore, boot_id: str) -> dict[str, Any] | None:
    heartbeat = store.read_marker("heartbeat.json")
    clean = store.read_marker("clean-exit.json")
    if not heartbeat or heartbeat.get("boot_id") == boot_id:
        return None
    heartbeat_epoch = float(heartbeat.get("epoch", 0))
    clean_epoch = float(clean.get("epoch", 0)) if clean else 0.0
    if clean_epoch >= heartbeat_epoch:
        return None
    return {
        "kind": "unclean-reboot",
        "timestamp": time.strftime("%F %T"),
        "previous_heartbeat": heartbeat,
        "previous_clean_exit": clean,
        "automatic_action": "none",
    }


class Guardian:
    def __init__(self, config: GuardConfig | None = None, store: IncidentStore | None = None,
                 monitor: Monitor | None = None, watcher: JournalWatcher | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleeper: Callable[[float], None] = time.sleep) -> None:
        self.config = config or GuardConfig()
        self.store = store or IncidentStore()
        self.monitor = monitor or Monitor(self.config.process_interval, self.config.gpu_interval,
                                          86400, enable_gpu=True, enable_logs=False)
        self.watcher = watcher or JournalWatcher()
        self.aggregator = EventAggregator()
        self.clock = clock
        self.sleeper = sleeper
        self.stop_requested = False
        self.critical_since: float | None = None
        self.last_action = -self.config.action_cooldown_seconds
        self.kernel_last_notified: dict[str, float] = {}
        self.display_error_since: dict[str, float] = {}
        self.desktop_history: deque[tuple[float, int]] = deque()
        self.last_desktop_action = -self.config.desktop_action_cooldown_seconds
        self.ancestry = process_ancestry(os.getpid())
        unclean = previous_boot_was_unclean(self.store, current_boot_id())
        if unclean:
            path = self.store.write_incident(unclean)
            notify("system-tool：检测到非正常重启", f"已保留上次心跳，报告：{path}")

    def request_stop(self, *_: object) -> None:
        self.stop_requested = True

    def _kernel_summary(self) -> list[dict[str, Any]]:
        for event in self.watcher.drain():
            self.aggregator.add(event)
        return self.aggregator.summary(time.time())

    def _kernel_incident(self, summary: list[dict[str, Any]]) -> None:
        now = time.time()
        for top in summary:
            message = str(top["message"])
            count = int(top["count"])
            key = str(top["fingerprint"])
            is_xid = "xid" in message.lower()
            is_nvrm = "nvrm" in message.lower()
            is_invalid_head = INVALID_HEAD_TEXT in message.lower()
            if is_invalid_head:
                lock_state = screen_lock_state()
                last_event = float(top.get("last_time", now))
                event_is_fresh = now - last_event <= max(15.0, self.config.sample_interval * 2)
                if lock_state == "locked" or not event_is_fresh:
                    self.display_error_since.pop(key, None)
                    continue
                started = self.display_error_since.setdefault(key, now)
                if now - started < self.config.display_error_unlocked_sustain_seconds:
                    continue
            qualifies = (is_xid or (is_nvrm and count >= self.config.nvrm_burst_per_minute) or
                         count >= self.config.log_storm_per_minute)
            if not qualifies:
                continue
            last_notified = self.kernel_last_notified.get(key, float("-inf"))
            if now - last_notified < self.config.kernel_notification_cooldown_seconds:
                continue
            payload = {"kind": "kernel-alert", "timestamp": time.strftime("%F %T"),
                       "fingerprint": key, "count_per_minute": count, "message": message,
                       "notification_cooldown_seconds": self.config.kernel_notification_cooldown_seconds,
                       "automatic_action": "none"}
            path = self.store.write_incident(payload)
            notify("system-tool：内核异常", f"{message[-120:]}\n报告：{path}")
            self.kernel_last_notified[key] = now

    def _disable_appindicator(self, reason: str, snapshot: dict[str, Any]) -> dict[str, Any] | None:
        now = self.clock()
        if now - self.last_desktop_action < self.config.desktop_action_cooldown_seconds:
            return None
        command = ["gnome-extensions", "disable", "ubuntu-appindicators@ubuntu.com"]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=8, check=False)
            success = result.returncode == 0
            error = result.stderr[-500:]
        except (OSError, subprocess.TimeoutExpired) as exc:
            success, error = False, str(exc)
        payload = {
            "kind": "desktop-recursion-relief", "timestamp": time.strftime("%F %T"),
            "reason": reason, "gnome_shell_rss": gnome_shell_rss(self.monitor.processes),
            "available_before": snapshot["memory"]["available"],
            "automatic_action": "disable ubuntu-appindicators extension",
            "action_succeeded": success, "error": error,
        }
        path = self.store.write_incident(payload)
        self.last_desktop_action = now
        title = "system-tool：已阻止桌面递归" if success else "system-tool：桌面递归止血失败"
        notify(title, f"{'已临时停用托盘扩展' if success else error}\n报告：{path}")
        return payload

    def _desktop_risk(self, snapshot: dict[str, Any], summary: list[dict[str, Any]]) -> str | None:
        for event in summary:
            message = str(event.get("message", "")).lower()
            if APPINDICATOR_RECURSION_TEXT in message and "ubuntu-appindicators" in message:
                return "GNOME Shell AppIndicators reported recursive menu creation"
        now = self.clock()
        rss = gnome_shell_rss(self.monitor.processes)
        self.desktop_history.append((now, rss))
        cutoff = now - self.config.desktop_growth_window_seconds
        while len(self.desktop_history) > 1 and self.desktop_history[1][0] <= cutoff:
            self.desktop_history.popleft()
        oldest_rss = self.desktop_history[0][1]
        if rss >= self.config.desktop_growth_floor_bytes and rss - oldest_rss >= self.config.desktop_growth_bytes:
            return (f"gnome-shell RSS grew {human_bytes(rss - oldest_rss)} in "
                    f"{now - self.desktop_history[0][0]:.0f}s (now {human_bytes(rss)})")
        return None

    def _act(self, snapshot: dict[str, Any], reason: str) -> dict[str, Any] | None:
        selected = select_offender(self.monitor.processes, self.config.offender_min_bytes,
                                   ancestry=self.ancestry)
        if not selected:
            return None
        group, rows, total_rss = selected
        total_swap = sum(row.swap for row in rows)
        initial = {row.pid for row in rows}
        targets = descendants(initial) - self.ancestry
        descriptions = [{"pid": row.pid, "command": row.command[:300], "rss": row.rss, "swap": row.swap}
                        for row in sorted(rows, key=lambda item: item.rss + item.swap, reverse=True)[:20]]
        term_sent = signal_targets(targets, signal.SIGTERM)
        notify("system-tool：正在自动止血", f"{group} 当前RAM {human_bytes(total_rss)}，已请求退出。")
        self.sleeper(self.config.term_grace_seconds)
        remaining = {pid for pid in targets if Path("/proc", str(pid)).exists()}
        current_mem = parse_meminfo(read_text(Path("/proc/meminfo")))
        current_psi = parse_psi(read_text(Path("/proc/pressure/memory")))
        still_low = current_mem.get("MemAvailable", 0) <= max(
            self.config.critical_available_bytes,
            int(current_mem.get("MemTotal", 1) * self.config.critical_available_ratio),
        )
        still_pressure = (current_psi.get("some_avg10", 0) >= self.config.critical_memory_psi or
                          current_psi.get("full_avg10", 0) >= self.config.critical_full_psi)
        kill_sent = signal_targets(remaining, signal.SIGKILL) if remaining and still_low and still_pressure else []
        payload = {
            "kind": "automatic-relief", "timestamp": time.strftime("%F %T"), "reason": reason,
            "offender_group": group, "estimated_rss": total_rss, "estimated_swap": total_swap,
            "processes": descriptions,
            "sigterm_pids": term_sent, "sigkill_pids": kill_sent,
            "available_before": snapshot["memory"]["available"],
            "available_after_grace": current_mem.get("MemAvailable", 0),
            "policy": "sustained critical memory pressure; user-owned non-protected group",
        }
        path = self.store.write_incident(payload)
        action = "强制结束" if kill_sent else "正常退出"
        notify("system-tool：自动止血完成", f"{group} 已{action}。报告：{path}")
        return payload

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        self.watcher.start()
        self.monitor.sample(force_slow=True)
        try:
            while not self.stop_requested:
                started = self.clock()
                snapshot = self.monitor.sample()
                summary = self._kernel_summary()
                critical, detail = memory_is_critical(snapshot, self.config)
                if critical:
                    self.critical_since = self.critical_since or started
                    state = "critical" if started - self.critical_since >= self.config.sustain_seconds else "warning"
                else:
                    self.critical_since = None
                    state = "normal"
                sample = compact_sample(snapshot, state, summary)
                self.store.append_sample(sample)
                self.store.heartbeat({"timestamp": time.strftime("%F %T"), "epoch": time.time(),
                                      "boot_id": current_boot_id(), "state": state, "pid": os.getpid()})
                self._kernel_incident(summary)
                desktop_reason = self._desktop_risk(snapshot, summary)
                if desktop_reason:
                    self._disable_appindicator(desktop_reason, snapshot)
                if (state == "critical" and started - self.last_action >= self.config.action_cooldown_seconds):
                    if self._act(snapshot, detail):
                        self.last_action = self.clock()
                        self.critical_since = None
                elapsed = self.clock() - started
                self.sleeper(max(0.2, self.config.sample_interval - elapsed))
        finally:
            self.watcher.stop()
            self.store.mark_clean_exit({"timestamp": time.strftime("%F %T"), "epoch": time.time(),
                                        "boot_id": current_boot_id(), "pid": os.getpid()})
        return 0


def systemctl_user(*arguments: str) -> int:
    try:
        return subprocess.run(["systemctl", "--user", *arguments], check=False).returncode
    except OSError:
        return 1


def print_incidents() -> int:
    rows = IncidentStore().recent_incidents()
    if not rows:
        print("暂无故障处置报告。")
        return 0
    for row in rows:
        print(f"{row.get('timestamp', '?')}  {row.get('kind', '?')}  {row.get('offender_group', row.get('message', ''))}")
        print(f"  {row['report_path']}")
    return 0


def run_guard_command(command: str) -> int:
    if command == "guard":
        return Guardian().run()
    if command == "guard-enable":
        return systemctl_user("enable", "--now", "system-tool-guard.service")
    if command == "guard-disable":
        return systemctl_user("disable", "--now", "system-tool-guard.service")
    if command == "guard-status":
        return systemctl_user("status", "system-tool-guard.service", "--no-pager")
    if command == "incidents":
        return print_incidents()
    return 2
