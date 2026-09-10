# 死机预防与故障留证设计

状态：守卫核心和事件驱动显示热插拔已实现并启用；驱动更新尚未执行。

## 1. 已确认事实与定位边界

- 2026-08-31 的上一轮启动没有正常关机记录，日志在 22:29:51 中断。
- 当天 NVIDIA 内核驱动持续报告 `NV_ERR_NO_MEMORY`。
- 22:00 至日志中断前，`fix-monitor-layout.service` 启动 70 次。定时器配置周期为
  5 秒，但受 systemd 定时器精度和服务状态影响，实际触发并非严格每 5 秒一次。
- 1184 条
  `dispcmnCtrlCmdSystemGetVblankCounter_IMPL: invalid head number`。
- 这些错误全部集中在 22:28 一分钟内，形成明显的 NVIDIA 显示引擎错误风暴；
  当天最后一次 `NV_ERR_NO_MEMORY` 在 22:03，共 4 条。
- 同一窗口存在 GNOME Shell 无法更新 stage view 的错误。
- 没有发现 kernel panic、硬锁、NVMe/EXT4 错误、温度过高或正常关机记录。

因此当前最合理的定位是：长期内存压力降低了 NVIDIA 图形栈的稳定余量，外接显示器
路径在 22:28 发生驱动错误风暴，随后图形界面或整机失去响应。周期性 `xrandr`
显示枚举与故障处于同一路径，是应消除的放大因素；但现有证据不能证明每一条驱动错误
都是该脚本直接产生的。

现有日志无法区分“内核彻底死机”“图形界面死机后人工强制关机”和“意外断电”。
防护方案必须同时做到预防和跨重启留证，不能把推断写成确定结论。

后续归因采用分支验证：

- 停用周期查询后错误消失：显示查询/布局切换是主要触发条件。
- 停用周期查询后，在内存健康时仍出现显示错误：优先检查 NVIDIA 驱动、BIOS、
  HDMI 线材和接口。
- 仅在 Memory PSI、Swap-out 上升后出现：资源压力是主要前置条件。
- 出现 Xid：按 Xid 类型进入显卡驱动或硬件专项诊断。

## 2. 选定的总体方案

采用三层防护：消除显示轮询、低负担守卫与留证、可选任务资源隔离。

原则：普通异常只告警和留证；内存压力达到持续临界条件时允许自动结束当前RSS最大的
非保护用户程序组。不自动重启，不执行 `swapoff`。

### 2.0 驱动处理决策

当前核对结果：

- GPU 为 NVIDIA GeForce RTX 5070 Laptop，PCI ID `10de:2d58`。
- 当前内核为 `6.8.0-124-generic`。
- 已加载模块为 `595.71.05-open`，模块 `vermagic` 与当前内核一致。
- Ubuntu 仓库中同分支候选版本为 `595.84-open`。
- `ubuntu-drivers` 当前推荐 `nvidia-driver-595` 非 open 包，但没有证据证明切换模块
  类型可以解决本次问题。
- 上一轮启动未发现 `NVRM: Xid`。

因此不把“立即切换驱动分支”作为第一步。顺序选定为：先部署留证、消除周期显示
查询；下次用户计划重启时，更新同一 open 分支到仓库候选小版本。只有在内存健康且
无周期查询时仍复发，才单独比较 open 与 proprietary 分支。驱动更新不在用户正在
工作的会话中执行。

### 2.1 显示器热插拔改为事件驱动（已实施）

停用配置为每 5 秒周期运行的 `fix-monitor-layout.timer`，改为用户级常驻服务：

1. 等待 DRM hotplug 事件，不主动轮询 `xrandr`。
2. 收到事件后防抖 2 秒；10 秒窗口内多个事件只处理一次。
3. 只执行一次 `xrandr --query`。
4. 将当前布局规范化后与目标布局比较。
5. 仅在 HDMI 已连接且布局不同时运行一次 `xrandr` 修复命令。
6. 保存事件时间、修改前状态、命令返回码和修改后状态。

实际实现额外记录 DRM connector 的 `connected/disconnected` 签名。非解锁事件只有签名
变化才调用布局脚本，从源头阻断 `xrandr` → DRM change → 再次 `xrandr` 的反馈循环。
图形错误风暴触发后，布局检查熔断10分钟；当前排版不改变，服务仍继续监听事件。

建议文件：

- `monitor_hotplug.py`：监听 `udevadm monitor --kernel --subsystem-match=drm`。
- `systemd/system-tool-monitor-hotplug.service`：用户级常驻单元。
- `monitor-layout.conf`：目标输出、分辨率、刷新率和相对位置。

回退方式：停止新服务并重新启用原定时器；不改 Xorg 全局配置。

### 2.2 增加 `system-tool guard`

新增命令：

- `system-tool guard`：前台守卫，供 systemd 用户服务调用。
- `system-tool incidents`：列出最近事件。
- `system-tool incident --last`：显示最近一次完整证据窗口。
- `system-tool doctor`：检查 journald、通知、NVIDIA 查询和存储权限。

采样频率：

| 数据 | 周期 | 原因 |
| --- | ---: | --- |
| `/proc/meminfo`、`vmstat`、PSI、CPU | 5 秒 | 读取成本低，足以捕获换页趋势 |
| D 状态和主要进程组 | 15 秒 | 扫描 `/proc/<pid>` 成本较高 |
| `nvidia-smi` | 30 秒 | 外部命令比 `/proc` 查询重 |
| 内核告警 | 持续事件流 | 避免反复读取最近两分钟日志 |
| 健康心跳 | 60 秒 | 为突然断电或硬死机保留时间边界 |

### 2.3 阈值与状态机

单个瞬时值不触发高级告警，使用连续窗口：

- 内存压力：`MemAvailable < 2 GiB` 且 Memory PSI some ≥ 5%，持续 30 秒。
- 主动换出：Swap-out ≥ 20 MiB/s，持续 30 秒。
- Swap 回读卡顿：Swap-in ≥ 4 MiB/s，并且 IO PSI full ≥ 2% 或存在 D 状态进程。
- CPU 饱和：CPU PSI some ≥ 20%，持续 60 秒；仅高 CPU 使用率不判定死机风险。
- NVIDIA 告警：任意 Xid 立即记录；其他 NVRM ≥ 10 条/分钟时告警。
- 日志风暴：相同内核消息 ≥ 100 条/分钟时告警并按指纹聚合。
- GPU 查询失败：连续 3 次失败后告警，单次失败只记录。

状态分为 `normal`、`warning`、`critical`、`recovering`。恢复后仍保留前后各 5 分钟
证据，不因指标恢复而删除事件。

### 2.4 落盘和跨重启诊断

运行数据写到：

```text
~/.local/state/system-tool/
  heartbeat.json
  samples.jsonl
  incidents/<时间>-<类型>.json
```

- 每条样本先写临时文件并原子替换心跳，避免半条 JSON。
- `samples.jsonl` 滚动保存，默认上限 20 MiB，最多两个历史文件。
- 事件文件保存触发前后各 5 分钟的降采样数据和去重日志。
- 正常退出写入 shutdown marker。
- 下次启动发现上一 boot ID 的心跳晚于 shutdown marker 时标记为非正常结束，但不
  擅自声称是内核死机。该判断已实施。

### 2.4.1 GNOME Shell 专项止血（已实施）

- 日志流同时匹配内核异常和 GNOME Shell 的 AppIndicators 递归错误。
- 每15秒按单个 `gnome-shell` 进程RSS计算增长，不使用整个 Desktop 组的RSS总和。
- 一分钟增长至少768 MiB且达到2 GiB，或日志明确命中 AppIndicators 递归时，临时
  停用 `ubuntu-appindicators@ubuntu.com`。
- 不结束 GNOME Shell、不注销、不重启；处置结果写入 incident，30分钟内不重复动作。

### 2.5 通知和自动动作边界

默认动作：

- 临时显示熔断和纯留证事件静默执行；只有明显影响使用的动作才通知一次。
- 保存证据。
- 通知明确显示已执行动作、影响范围和报告路径。

禁止：

- 自动关机或重启。
- 自动清空 Swap。
- 在未达到下述持续临界条件时自动关闭任何用户程序。

自动止血例外：仅当低可用内存与 PSI/Swap-out 临界条件同时持续45秒，且候选程序组
当前RSS不少于2 GiB时，先发送 SIGTERM；等待10秒后压力仍未恢复才发送 SIGKILL。
桌面、终端、systemd、SSH、Codex/ChatGPT和守卫自身不可作为候选。Chrome、Cursor、
ROS或编译任务只有在成为最大合格候选时才会被处理，并生成报告与桌面通知。

后续可选“保护桌面模式”：仅对通过 `system-tool run` 启动且明确登记的任务组执行
限速或暂停，绝不根据进程名模糊匹配。

## 3. 可选任务隔离

建议接口：

```bash
system-tool run --profile build -- colcon build ...
system-tool run --profile replay -- ros2 launch ...
```

内部使用独立 systemd user scope。第一版只设置 `MemoryHigh` 和编译并行建议，不设置
会直接触发杀进程的 `MemoryMax`。是否增加硬上限，应在观察实际峰值后单独决定。

## 4. 实施顺序和验收

### 阶段 A：先部署守卫与留证

- 单元测试采样差值、连续窗口、日志聚合、滚动文件和异常退出判断。
- 注入 NVRM、Xid、Swap-in 和 PSI 样本，验证状态转换。
- 运行 30 分钟测量 CPU、内存、磁盘写入和日志量。
- 手工停止守卫并重新启动，验证不会误报为整机死机。

先部署守卫可以为后面的显示和驱动调整保留基线与回归证据。

### 阶段 B：显示轮询替换（已完成）

- 用录制的 DRM 事件测试防抖和布局比较。
- 手工插拔 HDMI，验证只处理一次且布局正确。
- 观察 30 分钟，确认无周期性 `xrandr` 查询。
- 验证停止新服务后可以恢复原定时器。

### 阶段 C：计划重启时更新同分支驱动

- 更新前保存包版本、内核版本、NVRM 基线和回退包信息。
- 只更新 `595-open` 同分支候选版本，不同时切换显示会话或其他图形配置。
- 在用户安排的维护窗口重启；启动后检查模块、显示输出、休眠唤醒和 HDMI 插拔。
- 若更新后异常，使用预先记录的包版本回退。

### 阶段 D：任务隔离

- 先只提供 profile 和资源报告。
- 根据真实编译/回放峰值选定 `MemoryHigh`。
- 单独确认后才考虑 `MemoryMax` 或自动暂停。

## 5. 已选定的策略

1. HDMI 目标布局是否固定为：外屏左侧 `2560x1440@59.95`，内屏右侧
   `1920x1200@60` 且内屏为主屏。
2. 严重告警使用桌面通知，不主动播放声音。
3. 证据按约 40 MiB 总空间上限滚动。
4. 第一版允许在持续临界内存压力下自动 TERM/KILL 合格的用户程序组；其他异常不
   自动处置。

## 6. 代码级修改清单

为了保持现有单文件工具容易安装，同时避免把守卫逻辑继续堆入一个大类，建议拆成
以下文件：

```text
system_tool/
  system_tool.py                 # 保留现有菜单、status/watch/clean
  system_guard.py                # 守卫状态机、采样循环、事件文件
  journal_stream.py              # journalctl事件流、指纹和频率聚合
  incident_store.py              # 原子心跳、JSONL滚动、事件查询
  monitor_hotplug.py             # DRM事件、防抖、布局比较
  systemd/
    system-tool-guard.service
    system-tool-monitor-hotplug.service
  tests/
    test_system_tool.py          # 现有测试
    test_system_guard.py
    test_journal_stream.py
    test_incident_store.py
    test_monitor_hotplug.py
```

### 6.1 `system_tool.py`

- 将命令选择扩展为 `guard`、`incidents`、`incident`、`doctor`。
- 现有 `Monitor` 和 `ProcessSampler` 保持兼容，抽出一个不渲染终端的采样接口供
  `system_guard.py` 复用。
- `status/watch` 继续使用原有 1/3/15/60 秒频率；后台 `guard` 使用更低的
  5/15/30 秒频率，二者不能意外互相改变。
- 修正进程分组：不能因为编译命令路径含 `mowmow` 就把 `cc1plus` 归为 ROS/RViz；
  编译器应单独归入 Build/Compiler。

### 6.2 `system_guard.py`

- 接收时钟、采样器、通知器和存储器依赖，测试使用假时钟，不真实等待30秒。
- 每个风险规则保存连续命中次数、首次命中时间和恢复时间。
- 只有状态变化才发送通知，普通样本只进入滚动历史。
- SIGTERM/SIGINT 写入正常退出标记；进程崩溃不写标记。
- 不导入或调用任何清理、结束进程相关函数。

### 6.3 `journal_stream.py`

- 启动 `journalctl -k -f -n 0 -o json`，按游标连续读取，避免每分钟重扫两分钟日志。
- 日志指纹移除时间、PID、地址等波动字段，再按60秒窗口计数。
- 保存原始消息最多180字符、指纹、首末时间和计数，不重复写入1184条相同文本。
- `journalctl` 退出时指数退避重启，守卫主循环不能随之退出。

### 6.4 `incident_store.py`

- 所有写入只允许位于 `XDG_STATE_HOME/system-tool`，解析后验证路径仍在该目录内。
- 心跳采用同目录临时文件、`fsync`、`os.replace`，不使用系统 `/tmp`。
- JSONL达到20 MiB后轮换为 `.1`，删除更老的一份；总上限约40 MiB。
- `incidents` 查询只读，不因展示历史而修改文件。
- 正常停止守卫与正常关机必须区分：停止服务不能在下次启动时被报告为整机异常。
  需同时保存 boot ID、服务退出原因和最后心跳。

### 6.5 `monitor_hotplug.py`

- `udevadm monitor` 只负责提供事件，不允许把事件文本拼入shell命令。
- 布局命令使用参数数组调用 `xrandr`，禁止 `shell=True`。
- 第一次读取输出失败时只记录，不重试风暴；下一次DRM事件再处理。
- 配置缺少目标输出时不修改任何显示状态。
- 提供 `--dry-run`，输出将执行的变化而不调用 `xrandr`。

### 6.6 安装和卸载

`install.sh` 需要：

- 安装所有 Python 模块和 systemd 用户单元到用户目录。
- 默认安装但不自动启用守卫，由用户显式运行 `system-tool guard-enable` 后启用。
- 启用热插拔服务前，先检测旧 `fix-monitor-layout.timer`；必须向用户展示替换关系，
  不可静默停用现有服务。
- 安装失败时不留下“旧定时器已停、新服务未启”的半完成状态。

`uninstall.sh` 需要：

- 先停止并禁用本工具自己的两个用户服务，再将安装文件移入回收站。
- 不自动重新启用旧定时器；提示可执行的恢复命令。
- 默认保留 `~/.local/state/system-tool` 证据，单独提供明确确认的历史清理命令。

### 6.7 建议提交拆分

1. `guard core and incident storage`：纯逻辑、测试和命令，不安装服务。
2. `user service installation`：systemd单元、安装/卸载对称和doctor检查。
3. `event-driven monitor layout`：热插拔监听、dry-run和旧定时器迁移提示。
4. `docs and field validation`：README、故障演练、资源开销与回退结果。

每个提交都可以独立测试和回退，不把驱动包更新混入代码提交。
