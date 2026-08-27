#!/usr/bin/env python3
"""Low-overhead Linux bottleneck monitor (standard library only)."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


PROC = Path("/proc")
CGROUP = Path("/sys/fs/cgroup")
ANSI_CLEAR = "\033[2J\033[H"
ANSI_HIDE = "\033[?25l"
ANSI_SHOW = "\033[?25h"


def read_text(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except (OSError, PermissionError):
        return ""


def human_bytes(value: float) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    value = float(value)
    for unit in units:
        if abs(value) < 1024 or unit == units[-1]:
            return f"{value:.1f}{unit}" if value < 100 else f"{value:.0f}{unit}"
        value /= 1024
    return f"{value:.1f}TiB"


def parse_meminfo(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in text.splitlines():
        match = re.match(r"([^:]+):\s+(\d+)", line)
        if match:
            out[match.group(1)] = int(match.group(2)) * 1024
    return out


def parse_psi(text: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        prefix = parts[0]
        for part in parts[1:]:
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            if key.startswith("avg"):
                out[f"{prefix}_{key}"] = float(value)
    return out


def parse_proc_stat(text: str) -> tuple[int, int]:
    first = text.splitlines()[0].split()
    values = [int(value) for value in first[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def parse_vmstat(text: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            result[parts[0]] = int(parts[1])
    return result


def process_group(cmd: str, comm: str, cgroup: str) -> str:
    joined = f"{cmd} {comm} {cgroup}".lower()
    rules = (
        ("ROS/RViz", ("rviz", "ros2 bag", "eskf_", "rosbag", "mowmow")),
        ("Chrome", ("google-chrome", "/chrome", "app-gnome-google")),
        ("Cursor", ("/cursor", "app-org.chromium.chromium-350")),
        ("Codex/ChatGPT", ("codex", "chatgpt")),
        ("Feishu", ("feishu", "bytedance")),
        ("WeChat", ("wechat", "xwechat")),
        ("Java/Gradle", ("gradle", "/java")),
        ("Docker", ("docker", "containerd")),
        ("Desktop", ("gnome-shell", "xorg", "gnome-terminal")),
    )
    for label, needles in rules:
        if any(needle in joined for needle in needles):
            return label
    return comm or "other"


@dataclass
class ProcessRow:
    pid: int
    group: str
    command: str
    cpu: float
    rss: int
    swap: int
    state: str


class ProcessSampler:
    def __init__(self) -> None:
        self.previous: dict[int, int] = {}
        self.previous_total = 0
        self.cpu_count = os.cpu_count() or 1

    def sample(self, total_ticks: int) -> list[ProcessRow]:
        rows: list[ProcessRow] = []
        current: dict[int, int] = {}
        delta_total = max(1, total_ticks - self.previous_total)
        for entry in PROC.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                stat = (entry / "stat").read_text().split()
                ticks = int(stat[13]) + int(stat[14])
                state = stat[2]
                status = (entry / "status").read_text()
                cmd_raw = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
                comm = stat[1].strip("()")
                cmd = cmd_raw or comm
                cgroup = read_text(entry / "cgroup")
            except (OSError, ValueError, IndexError, PermissionError):
                continue
            current[pid] = ticks
            cpu = max(0.0, (ticks - self.previous.get(pid, ticks)) * 100.0 * self.cpu_count / delta_total)
            rss_match = re.search(r"^VmRSS:\s+(\d+)", status, re.MULTILINE)
            swap_match = re.search(r"^VmSwap:\s+(\d+)", status, re.MULTILINE)
            rows.append(ProcessRow(pid, process_group(cmd, comm, cgroup), cmd, cpu,
                                   int(rss_match.group(1)) * 1024 if rss_match else 0,
                                   int(swap_match.group(1)) * 1024 if swap_match else 0, state))
        self.previous = current
        self.previous_total = total_ticks
        return rows


class Monitor:
    def __init__(self, process_interval: float = 3, gpu_interval: float = 15,
                 log_interval: float = 60, enable_gpu: bool = True, enable_logs: bool = True) -> None:
        self.process_interval = process_interval
        self.gpu_interval = gpu_interval
        self.log_interval = log_interval
        self.enable_gpu = enable_gpu
        self.enable_logs = enable_logs
        self.last_cpu: tuple[int, int] | None = None
        self.last_vm: tuple[float, dict[str, int]] | None = None
        self.last_process = 0.0
        self.last_gpu = 0.0
        self.last_log = 0.0
        self.process_sampler = ProcessSampler()
        self.processes: list[ProcessRow] = []
        self.gpu: dict[str, Any] = {}
        self.alerts: list[str] = []

    def _gpu(self) -> dict[str, Any]:
        if not self.enable_gpu or not shutil.which("nvidia-smi"):
            return {}
        command = ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu", "--format=csv,noheader,nounits"]
        try:
            output = subprocess.run(command, capture_output=True, text=True, timeout=2, check=False)
            if output.returncode != 0:
                return {"error": output.stderr.strip() or "nvidia-smi failed"}
            values = [item.strip() for item in output.stdout.splitlines()[0].split(",")]
            return {"util": float(values[0]), "memory_used_mib": float(values[1]),
                    "memory_total_mib": float(values[2]), "temperature_c": float(values[3])}
        except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
            return {"error": "nvidia-smi unavailable"}

    def _logs(self) -> list[str]:
        if not self.enable_logs or not shutil.which("journalctl"):
            return []
        try:
            result = subprocess.run(
                ["journalctl", "-k", "--since", "2 minutes ago", "--no-pager", "-n", "120"],
                capture_output=True, text=True, timeout=3, check=False,
            )
            pattern = re.compile(r"oom|out of memory|killed process|nvrm|xid|thermal|I/O error", re.I)
            return [line[-180:] for line in result.stdout.splitlines() if pattern.search(line)][-5:]
        except (OSError, subprocess.TimeoutExpired):
            return []

    def sample(self, force_slow: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        total, idle = parse_proc_stat(read_text(PROC / "stat"))
        cpu = 0.0
        if self.last_cpu:
            delta_total = max(1, total - self.last_cpu[0])
            cpu = 100.0 * (1 - (idle - self.last_cpu[1]) / delta_total)
        self.last_cpu = (total, idle)

        mem = parse_meminfo(read_text(PROC / "meminfo"))
        vm = parse_vmstat(read_text(PROC / "vmstat"))
        swap_in = swap_out = 0.0
        if self.last_vm:
            elapsed = max(0.001, now - self.last_vm[0])
            page = os.sysconf("SC_PAGE_SIZE")
            swap_in = max(0, vm.get("pswpin", 0) - self.last_vm[1].get("pswpin", 0)) * page / elapsed
            swap_out = max(0, vm.get("pswpout", 0) - self.last_vm[1].get("pswpout", 0)) * page / elapsed
        self.last_vm = (now, vm)

        if force_slow or now - self.last_process >= self.process_interval:
            self.processes = self.process_sampler.sample(total)
            self.last_process = now
        if force_slow or now - self.last_gpu >= self.gpu_interval:
            self.gpu = self._gpu()
            self.last_gpu = now
        if force_slow or now - self.last_log >= self.log_interval:
            self.alerts = self._logs()
            self.last_log = now

        groups: dict[str, dict[str, float]] = defaultdict(lambda: {"cpu": 0.0, "rss": 0, "swap": 0, "count": 0})
        for row in self.processes:
            group = groups[row.group]
            group["cpu"] += row.cpu
            group["rss"] += row.rss
            group["swap"] += row.swap
            group["count"] += 1

        snapshot: dict[str, Any] = {
            "timestamp": time.strftime("%F %T"),
            "cpu_percent": max(0.0, min(100.0, cpu)),
            "load": [float(x) for x in read_text(PROC / "loadavg").split()[:3]],
            "cpu_psi": parse_psi(read_text(PROC / "pressure/cpu")),
            "memory_psi": parse_psi(read_text(PROC / "pressure/memory")),
            "io_psi": parse_psi(read_text(PROC / "pressure/io")),
            "memory": {
                "total": mem.get("MemTotal", 0), "available": mem.get("MemAvailable", 0),
                "swap_total": mem.get("SwapTotal", 0), "swap_free": mem.get("SwapFree", 0),
                "swap_in_per_sec": swap_in, "swap_out_per_sec": swap_out,
            },
            "gpu": self.gpu,
            "alerts": self.alerts,
            "groups": dict(groups),
            "top_cpu": [asdict(row) for row in sorted(self.processes, key=lambda x: x.cpu, reverse=True)[:8]],
            "top_memory": [asdict(row) for row in sorted(self.processes, key=lambda x: x.rss + x.swap, reverse=True)[:8]],
        }
        snapshot["diagnosis"] = diagnose(snapshot)
        return snapshot


def diagnose(snapshot: dict[str, Any]) -> list[dict[str, str]]:
    findings: list[tuple[int, str, str]] = []
    mem = snapshot["memory"]
    available_ratio = mem["available"] / max(1, mem["total"])
    mem_psi = snapshot["memory_psi"].get("some_avg10", 0)
    io_psi = snapshot["io_psi"].get("full_avg10", 0)
    cpu_psi = snapshot["cpu_psi"].get("some_avg10", 0)
    if mem_psi >= 5 or mem["swap_out_per_sec"] >= 20 * 1024 * 1024:
        findings.append((100, "内存换页", f"memory PSI {mem_psi:.1f}%, swap-out {human_bytes(mem['swap_out_per_sec'])}/s"))
    elif available_ratio < 0.1:
        findings.append((65, "可用内存偏低", f"仅剩 {human_bytes(mem['available'])}"))
    if io_psi >= 5:
        findings.append((90, "磁盘 I/O 等待", f"IO full PSI {io_psi:.1f}%"))
    if cpu_psi >= 10 or snapshot["cpu_percent"] >= 90:
        findings.append((85, "CPU 饱和", f"CPU {snapshot['cpu_percent']:.0f}%, PSI {cpu_psi:.1f}%"))
    if snapshot["top_cpu"] and snapshot["top_cpu"][0]["cpu"] >= 80:
        row = snapshot["top_cpu"][0]
        findings.append((70, f"单进程热点：{row['group']}", f"PID {row['pid']} {row['cpu']:.0f}% CPU"))
    if snapshot["gpu"].get("util", 0) >= 95:
        findings.append((80, "GPU 饱和", f"GPU {snapshot['gpu']['util']:.0f}%"))
    if snapshot["alerts"]:
        findings.append((95, "近期内核告警", snapshot["alerts"][-1][-100:]))
    if not findings:
        findings.append((0, "当前无明显系统级卡点", "观察单个应用响应和网络状态"))
    return [{"title": title, "detail": detail} for _, title, detail in sorted(findings, reverse=True)[:3]]


def bar(value: float, width: int = 18) -> str:
    filled = max(0, min(width, round(value * width / 100)))
    return "█" * filled + "░" * (width - filled)


def truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def render(snapshot: dict[str, Any]) -> str:
    width = max(80, shutil.get_terminal_size((120, 30)).columns)
    mem = snapshot["memory"]
    used_pct = 100 * (mem["total"] - mem["available"]) / max(1, mem["total"])
    swap_used = mem["swap_total"] - mem["swap_free"]
    swap_pct = 100 * swap_used / max(1, mem["swap_total"])
    lines = [f"SYSTEM LAG MONITOR  {snapshot['timestamp']}  Ctrl-C 退出", "=" * min(width, 120)]
    lines.append(f"CPU  {bar(snapshot['cpu_percent'])} {snapshot['cpu_percent']:5.1f}%   LOAD {' '.join(f'{x:.2f}' for x in snapshot['load'])}   PSI {snapshot['cpu_psi'].get('some_avg10', 0):.2f}%")
    lines.append(f"MEM  {bar(used_pct)} {used_pct:5.1f}%   可用 {human_bytes(mem['available'])}/{human_bytes(mem['total'])}   PSI {snapshot['memory_psi'].get('some_avg10', 0):.2f}%")
    lines.append(f"SWAP {bar(swap_pct)} {swap_pct:5.1f}%   已用 {human_bytes(swap_used)}   IN {human_bytes(mem['swap_in_per_sec'])}/s OUT {human_bytes(mem['swap_out_per_sec'])}/s")
    lines.append(f"I/O  PSI some/full {snapshot['io_psi'].get('some_avg10', 0):.2f}%/{snapshot['io_psi'].get('full_avg10', 0):.2f}%")
    gpu = snapshot["gpu"]
    if gpu and "error" not in gpu:
        lines.append(f"GPU  {gpu['util']:.0f}%  VRAM {gpu['memory_used_mib']:.0f}/{gpu['memory_total_mib']:.0f}MiB  TEMP {gpu['temperature_c']:.0f}C")
    elif gpu.get("error"):
        lines.append(f"GPU  {gpu['error']}")
    lines.extend(["", "当前判断"])
    for item in snapshot["diagnosis"]:
        lines.append(f"  • {item['title']} — {item['detail']}")

    lines.extend(["", "任务组（CPU / RAM / Swap / 进程数）"])
    groups = sorted(snapshot["groups"].items(), key=lambda x: (x[1]["cpu"] + (x[1]["rss"] + x[1]["swap"]) / 2**28), reverse=True)[:10]
    for name, data in groups:
        lines.append(f"  {name:<17} {data['cpu']:6.1f}%  {human_bytes(data['rss']):>8}  {human_bytes(data['swap']):>8}  {int(data['count']):5d}")

    lines.extend(["", "Top CPU"])
    for row in snapshot["top_cpu"][:6]:
        lines.append(f"  {row['pid']:>7} {row['cpu']:6.1f}% {human_bytes(row['rss']):>8} {row['state']}  {truncate(row['command'], width - 35)}")
    if snapshot["alerts"]:
        lines.extend(["", "最近告警"] + [f"  ! {truncate(line, width - 4)}" for line in snapshot["alerts"][-3:]])
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="低开销 Linux 卡顿归因工具")
    parser.add_argument("--interval", type=float, default=1.0, help="轻量指标刷新秒数，默认 1")
    parser.add_argument("--process-interval", type=float, default=3.0, help="进程扫描间隔，默认 3")
    parser.add_argument("--gpu-interval", type=float, default=15.0, help="GPU 查询间隔，默认 15")
    parser.add_argument("--log-interval", type=float, default=60.0, help="日志查询间隔，默认 60")
    parser.add_argument("--no-gpu", action="store_true", help="禁用 nvidia-smi")
    parser.add_argument("--no-logs", action="store_true", help="禁用 journalctl")
    parser.add_argument("--once", action="store_true", help="采样一次后退出")
    parser.add_argument("--json", action="store_true", help="输出 JSON（隐含 --once）")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    monitor = Monitor(args.process_interval, args.gpu_interval, args.log_interval,
                      not args.no_gpu, not args.no_logs)
    if args.once or args.json:
        monitor.sample(force_slow=True)
        time.sleep(min(0.25, max(0.05, args.interval)))
        snapshot = monitor.sample(force_slow=True)
        print(json.dumps(snapshot, ensure_ascii=False, indent=2) if args.json else render(snapshot))
        return 0
    if not sys.stdout.isatty():
        print("动态模式需要终端；可使用 --once 或 --json", file=sys.stderr)
        return 2
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    print(ANSI_HIDE, end="", flush=True)
    try:
        while True:
            snapshot = monitor.sample()
            print(ANSI_CLEAR + render(snapshot), end="", flush=True)
            time.sleep(max(0.2, args.interval))
    except KeyboardInterrupt:
        return 0
    finally:
        print(ANSI_SHOW, end="\n", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
