# system_tool

一个零第三方依赖、低开销的 Linux 卡顿归因终端工具。它不只展示资源数字，还会根据 CPU、内存换页、PSI、I/O、GPU 和内核告警判断“当前主要卡点”。

## 使用

```bash
cd /home/hulk/system_tool
./system_tool.py
```

按 `Ctrl-C` 退出。工具默认只读，不会结束进程或修改系统配置。

一次性报告：

```bash
./system_tool.py --once
./system_tool.py --json
```

关闭较慢的可选采样：

```bash
./system_tool.py --no-gpu --no-logs
```

## 刷新策略

- 每 1 秒：`/proc/stat`、内存、load、PSI、换页速率。
- 每 3 秒：扫描进程，统计 CPU、RAM、Swap，并聚合为 Chrome、ROS/RViz、终端开发工具等任务组。
- 每 15 秒：调用一次 `nvidia-smi`。
- 每 60 秒：检查最近两分钟的 OOM、NVIDIA、温度和 I/O 内核告警。
- 不读取昂贵的 `smaps`，不执行 `du`、SMART、全量 `lsof` 或历史日志扫描。

刷新间隔均可调整：

```bash
./system_tool.py --interval 1 --process-interval 5 --gpu-interval 30 --log-interval 120
```

## 判定原则

- Swap 已用量本身不等于卡顿；优先看实时 swap-in/out 和 Memory PSI。
- 高 load 本身不等于 CPU 饱和；同时参考 CPU 利用率和 CPU PSI。
- 单个 RViz 使用 100% 表示占满一个核心，不代表整机 CPU 已用尽。
- 进程榜使用采样间 CPU 增量，不使用从启动至今的累计平均值。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

支持 Linux cgroup v2；无 NVIDIA GPU 或无日志权限时会降级运行。
