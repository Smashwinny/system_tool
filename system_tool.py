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
from typing import Any, Iterable


PROC = Path("/proc")
CGROUP = Path("/sys/fs/cgroup")
ANSI_CLEAR = "\033[2J\033[H"
ANSI_HIDE = "\033[?25l"
ANSI_SHOW = "\033[?25h"
VERSION = "1.3.0"


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


def parse_proc_stat(text: str) -> tuple[int, int, int]:
    first = text.splitlines()[0].split()
    values = [int(value) for value in first[1:]]
    idle = values[3]
    iowait = values[4] if len(values) > 4 else 0
    return sum(values), idle, iowait


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
        ("Build/Compiler", ("cc1plus", "cc1", "clang", "cmake --build", "ninja", "colcon build")),
        ("ROS/RViz", ("rviz", "ros2 bag", "eskf_", "rosbag", "ros2 launch", "progress_player")),
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
        self.last_cpu: tuple[int, int, int] | None = None
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
        total, idle, iowait_ticks = parse_proc_stat(read_text(PROC / "stat"))
        cpu = iowait = 0.0
        if self.last_cpu:
            delta_total = max(1, total - self.last_cpu[0])
            cpu = 100.0 * (1 - (idle - self.last_cpu[1] + iowait_ticks - self.last_cpu[2]) / delta_total)
            iowait = 100.0 * (iowait_ticks - self.last_cpu[2]) / delta_total
        self.last_cpu = (total, idle, iowait_ticks)

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
            "iowait_percent": max(0.0, min(100.0, iowait)),
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
            "blocked": [asdict(row) for row in self.processes if row.state == "D"][:12],
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
    blocked = snapshot.get("blocked", [])
    swap_in = mem.get("swap_in_per_sec", 0)
    if swap_in >= 4 * 1024 * 1024 and (io_psi >= 2 or blocked):
        names = ", ".join(str(row["command"]).split()[0].split("/")[-1] for row in blocked[:3])
        suffix = f"；等待进程：{names}" if names else ""
        findings.append((110, "Swap回读导致卡顿", f"正在回读 {human_bytes(swap_in)}/s{suffix}"))
    elif io_psi >= 5 or snapshot.get("iowait_percent", 0) >= 10:
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
    lines.append(f"I/O  PSI some/full {snapshot['io_psi'].get('some_avg10', 0):.2f}%/{snapshot['io_psi'].get('full_avg10', 0):.2f}%   iowait {snapshot.get('iowait_percent', 0):.1f}%   D进程 {len(snapshot.get('blocked', []))}")
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


@dataclass
class CleanupTarget:
    name: str
    description: str
    paths: list[Path]
    max_age_days: int = 0
    running_markers: tuple[str, ...] = ()


@dataclass
class AppCleanupTarget:
    name: str
    description: str
    markers: tuple[str, ...]


def user_home() -> Path:
    return Path.home().resolve()


def cache_targets(deep: bool = False) -> list[CleanupTarget]:
    home = user_home()
    targets = [
        CleanupTarget("缩略图缓存", "图片预览缓存，可自动重建", [home / ".cache/thumbnails"]),
        CleanupTarget("崩溃报告", "30天前的应用崩溃上传文件", [home / ".config/google-chrome/Crash Reports", home / ".config/Cursor/Crashpad"], 30),
        CleanupTarget("工具临时文件", "system_tool产生的Python字节码", [Path(__file__).resolve().parent / "__pycache__", Path(__file__).resolve().parent / "tests/__pycache__"]),
    ]
    if deep:
        targets.extend([
            CleanupTarget("Chrome缓存", "网页缓存，不删除书签、密码、历史和登录状态", [home / ".cache/google-chrome"], running_markers=("/opt/google/chrome/chrome",)),
            CleanupTarget("Cursor缓存", "编辑器缓存，不删除项目、设置和扩展", [home / ".cache/Cursor"], running_markers=("/usr/share/cursor/cursor",)),
            CleanupTarget("pip下载缓存", "Python安装包缓存，需要时会重新下载", [home / ".cache/pip"]),
            CleanupTarget("npm下载缓存", "npm内容寻址缓存，需要时会重新下载", [home / ".npm/_cacache"]),
            CleanupTarget("Mesa着色器缓存", "图形着色器缓存，会自动重建", [home / ".cache/mesa_shader_cache", home / ".cache/mesa_shader_cache_db"]),
        ])
    return targets


def path_is_allowed(path: Path) -> bool:
    resolved = path.resolve()
    home = user_home()
    tool_root = Path(__file__).resolve().parent
    allowed = (home / ".cache", home / ".config/google-chrome/Crash Reports",
               home / ".config/Cursor/Crashpad", home / ".npm/_cacache", tool_root)
    return any(resolved == root.resolve() or root.resolve() in resolved.parents for root in allowed)


def iter_cleanup_files(target: CleanupTarget) -> Iterable[Path]:
    cutoff = time.time() - target.max_age_days * 86400 if target.max_age_days else None
    for base in target.paths:
        if not base.exists() or not path_is_allowed(base):
            continue
        if base.is_file() or base.is_symlink():
            candidates = [base]
        else:
            candidates = (Path(root) / name for root, _, files in os.walk(base, followlinks=False) for name in files)
        for path in candidates:
            try:
                if cutoff is None or path.lstat().st_mtime < cutoff:
                    yield path
            except OSError:
                continue


def target_size(target: CleanupTarget) -> tuple[int, int]:
    size = count = 0
    for path in iter_cleanup_files(target):
        try:
            size += path.lstat().st_size
            count += 1
        except OSError:
            pass
    return size, count


def clean_target(target: CleanupTarget) -> tuple[int, int, list[str]]:
    removed = freed = 0
    errors: list[str] = []
    bases = [path for path in target.paths if path.exists() and path_is_allowed(path)]
    for path in list(iter_cleanup_files(target)):
        try:
            size = path.lstat().st_size
            path.unlink()
            removed += 1
            freed += size
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    for base in bases:
        if base.is_dir():
            for root, dirs, _ in os.walk(base, topdown=False, followlinks=False):
                for name in dirs:
                    try:
                        (Path(root) / name).rmdir()
                    except OSError:
                        pass
    return freed, removed, errors


def find_safe_helpers() -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmd = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if "/snap-store" in cmd or "ubuntu-software" in cmd:
            found.append((int(entry.name), "Snap Store"))
    return found


def markers_running(markers: tuple[str, ...]) -> bool:
    if not markers:
        return False
    for entry in PROC.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmd = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if any(marker in cmd for marker in markers):
            return True
    return False


def app_cleanup_targets() -> list[AppCleanupTarget]:
    return [
        AppCleanupTarget("Cursor", "关闭编辑器；请先保存未保存文件", ("/usr/share/cursor/cursor",)),
        AppCleanupTarget("Chrome", "关闭所有Chrome窗口；标签通常可恢复", ("/opt/google/chrome/chrome",)),
        AppCleanupTarget("飞书", "关闭飞书桌面端", ("/bytedance-feishu", "/opt/bytedance/feishu/")),
        AppCleanupTarget("微信", "关闭微信桌面端和小程序", ("/usr/bin/wechat", "/opt/wechat/")),
        AppCleanupTarget("回放/RViz", "只关闭rosbag回放、RViz和回放辅助工具，不删除数据", (
            "rosbag_progress_player.py", "cached_replay_adapter.py", "ordinal_replay_reader.py",
            "cached_process_inspector.py", "launch_eskf_multi_replay.sh",
            "rviz2 -d /home/hulk/ros2bag/progress_player/",
            "process_log_path:=/home/hulk/ros2bag/progress_player/runs/",
        )),
    ]


def scan_app_target(target: AppCleanupTarget) -> tuple[list[int], int, int]:
    pids: list[int] = []
    rss = swap = 0
    own_pid = os.getpid()
    for entry in PROC.iterdir():
        if not entry.name.isdigit() or int(entry.name) == own_pid:
            continue
        try:
            cmd = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            if not any(marker in cmd for marker in target.markers):
                continue
            status = (entry / "status").read_text()
        except OSError:
            continue
        pids.append(int(entry.name))
        rss_match = re.search(r"^VmRSS:\s+(\d+)", status, re.MULTILINE)
        swap_match = re.search(r"^VmSwap:\s+(\d+)", status, re.MULTILINE)
        rss += int(rss_match.group(1)) * 1024 if rss_match else 0
        swap += int(swap_match.group(1)) * 1024 if swap_match else 0
    return pids, rss, swap


def stop_app_targets(selected: list[tuple[AppCleanupTarget, list[int]]]) -> list[str]:
    results: list[str] = []
    for target, pids in selected:
        requested = 0
        for pid in sorted(set(pids), reverse=True):
            try:
                os.kill(pid, signal.SIGTERM)
                requested += 1
            except ProcessLookupError:
                pass
            except PermissionError:
                results.append(f"{target.name}: PID {pid}权限不足")
        if requested:
            results.append(f"{target.name}: 已请求退出{requested}个进程")
    return results


def stop_helpers(helpers: list[tuple[int, str]]) -> list[str]:
    stopped: list[str] = []
    for pid, name in helpers:
        try:
            os.kill(pid, signal.SIGTERM)
            stopped.append(f"{name} (PID {pid})")
        except ProcessLookupError:
            pass
        except PermissionError:
            stopped.append(f"{name} (PID {pid}) 权限不足")
    return stopped


def ask_yes_no(prompt: str, default: bool = False) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{prompt} {suffix} ").strip().lower()
    except EOFError:
        return False
    if not answer:
        return default
    return answer in {"y", "yes", "是", "好"}


def run_cleanup(deep: bool, assume_yes: bool = False, dry_run: bool = False) -> int:
    title = "深度清理" if deep else "简单清理"
    print(f"\n{title}预览（不会清理项目、rosbag、书签、密码或登录状态）")
    print("-" * 68)
    selected: list[CleanupTarget] = []
    total = 0
    for target in cache_targets(deep):
        size, count = target_size(target)
        if not count:
            continue
        print(f"{target.name:<16} {human_bytes(size):>10}  {count:>7}个文件  {target.description}")
        if markers_running(target.running_markers):
            print("  跳过：对应应用仍在运行，请关闭应用后再清理。")
            continue
        choose = assume_yes or (not dry_run and ask_yes_no(f"清理“{target.name}”？", default=not deep))
        if choose:
            selected.append(target)
            total += size
    helpers = find_safe_helpers()
    if helpers:
        print(f"Snap Store后台进程：{len(helpers)}个（关闭后可再次启动）")
    stop_selected = bool(helpers) and (assume_yes or (not dry_run and ask_yes_no("关闭Snap Store后台进程？", True)))
    selected_apps: list[tuple[AppCleanupTarget, list[int]]] = []
    if deep:
        print("\n运行内存清理候选（默认不关闭，可能中断未保存工作）")
        for app in app_cleanup_targets():
            pids, rss, swap = scan_app_target(app)
            if not pids:
                continue
            print(f"{app.name:<16} RAM {human_bytes(rss):>9}  Swap {human_bytes(swap):>9}  {len(pids):>4}个进程  {app.description}")
            if not dry_run and not assume_yes and ask_yes_no(f"正常退出“{app.name}”？"):
                selected_apps.append((app, pids))
    if dry_run:
        print("\n这是预览，没有删除文件或关闭程序。")
        return 0
    if not selected and not stop_selected and not selected_apps:
        print("没有选择任何清理项。")
        return 0
    if not assume_yes and not ask_yes_no(f"确认执行，预计最多释放 {human_bytes(total)}？"):
        print("已取消。")
        return 0
    freed = removed = 0
    all_errors: list[str] = []
    for target in selected:
        item_freed, item_removed, errors = clean_target(target)
        freed += item_freed
        removed += item_removed
        all_errors.extend(errors)
    stopped = stop_helpers(helpers) if stop_selected else []
    stopped.extend(stop_app_targets(selected_apps))
    print(f"\n完成：删除 {removed} 个可重建缓存文件，释放 {human_bytes(freed)}。")
    if stopped:
        print("已请求关闭：" + "、".join(stopped))
    if all_errors:
        print(f"有 {len(all_errors)} 个文件未能清理；首条：{all_errors[0]}")
        return 1
    return 0


def print_quick_status(enable_gpu: bool = True, enable_logs: bool = True) -> None:
    monitor = Monitor(enable_gpu=enable_gpu, enable_logs=enable_logs)
    monitor.sample(force_slow=True)
    time.sleep(0.25)
    print(render(monitor.sample(force_slow=True)))


def interactive_menu() -> int:
    while True:
        print("\n" + "=" * 52)
        print("  电脑卡顿助手 system_tool")
        print("=" * 52)
        print("1. 看一次当前用量（推荐）")
        print("2. 动态监控")
        print("3. 简单清理（缩略图、旧崩溃报告、Snap Store）")
        print("4. 深度清理（逐项预览和确认）")
        print("5. 只预览可清理内容")
        print("0. 退出")
        try:
            choice = input("请选择 [0-5]：").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if choice == "1":
            print_quick_status()
            input("\n按回车返回菜单...")
        elif choice == "2":
            run_watch(argparse.Namespace(interval=1.0, process_interval=3.0, gpu_interval=15.0,
                                         log_interval=60.0, no_gpu=False, no_logs=False))
        elif choice == "3":
            run_cleanup(False)
        elif choice == "4":
            run_cleanup(True)
        elif choice == "5":
            run_cleanup(True, dry_run=True)
        elif choice == "0":
            return 0
        else:
            print("请输入0到5。")


def run_watch(args: argparse.Namespace) -> int:
    monitor = Monitor(args.process_interval, args.gpu_interval, args.log_interval,
                      not args.no_gpu, not args.no_logs)
    if not sys.stdout.isatty():
        print("动态模式需要终端；可使用 system-tool status", file=sys.stderr)
        return 2
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    print(ANSI_HIDE, end="", flush=True)
    try:
        while True:
            print(ANSI_CLEAR + render(monitor.sample()), end="", flush=True)
            time.sleep(max(0.2, args.interval))
    except KeyboardInterrupt:
        return 0
    finally:
        print(ANSI_SHOW, end="\n", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="低开销 Linux 卡顿归因工具")
    parser.add_argument("--version", action="version", version=f"system-tool {VERSION}")
    parser.add_argument("command", nargs="?", choices=("menu", "watch", "status", "clean", "deep-clean",
                                                   "guard", "guard-enable", "guard-disable", "guard-status",
                                                   "incidents"),
                        help="不填则打开傻瓜菜单")
    parser.add_argument("--interval", type=float, default=1.0, help="轻量指标刷新秒数，默认 1")
    parser.add_argument("--process-interval", type=float, default=3.0, help="进程扫描间隔，默认 3")
    parser.add_argument("--gpu-interval", type=float, default=15.0, help="GPU 查询间隔，默认 15")
    parser.add_argument("--log-interval", type=float, default=60.0, help="日志查询间隔，默认 60")
    parser.add_argument("--no-gpu", action="store_true", help="禁用 nvidia-smi")
    parser.add_argument("--no-logs", action="store_true", help="禁用 journalctl")
    parser.add_argument("--once", action="store_true", help="采样一次后退出")
    parser.add_argument("--json", action="store_true", help="输出 JSON（隐含 --once）")
    parser.add_argument("--yes", action="store_true", help="清理时接受全部项目（脚本模式）")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不执行清理")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command in (None, "menu") and not (args.once or args.json):
        return interactive_menu()
    if args.command == "clean":
        return run_cleanup(False, args.yes, args.dry_run)
    if args.command == "deep-clean":
        return run_cleanup(True, args.yes, args.dry_run)
    if args.command == "watch":
        return run_watch(args)
    if args.command in ("guard", "guard-enable", "guard-disable", "guard-status", "incidents"):
        from system_guard import run_guard_command
        return run_guard_command(args.command)
    monitor = Monitor(args.process_interval, args.gpu_interval, args.log_interval,
                      not args.no_gpu, not args.no_logs)
    if args.command == "status" or args.once or args.json:
        monitor.sample(force_slow=True)
        time.sleep(min(0.25, max(0.05, args.interval)))
        snapshot = monitor.sample(force_slow=True)
        print(json.dumps(snapshot, ensure_ascii=False, indent=2) if args.json else render(snapshot))
        return 0
    return run_watch(args)


if __name__ == "__main__":
    raise SystemExit(main())
