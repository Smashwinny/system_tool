# 电脑卡顿助手 system_tool

面向普通用户的 Linux 卡顿诊断与安全清理工具。零第三方 Python 依赖，默认中文菜单，所有清理先预览，不会删除项目、rosbag、书签、密码或登录资料。

## 一键安装

```bash
cd /home/hulk/system_tool
./install.sh
```

安装后可以在终端输入 `system-tool`，也可以在 Ubuntu 应用列表搜索“电脑卡顿助手”。卸载运行 `./uninstall.sh`，安装文件会优先移入回收站。

## 傻瓜菜单

直接运行 `system-tool`，菜单提供：

1. 看一次当前用量
2. 动态监控
3. 简单清理
4. 深度清理
5. 只预览可清理内容

按 `Ctrl-C` 退出动态监控。

## 直接命令

```bash
system-tool status                 # 查询一次CPU、内存、Swap、GPU和卡点
system-tool watch                  # 动态监控
system-tool guard-enable           # 启用后台自动止血
system-tool guard-status           # 查看守卫状态
system-tool incidents              # 查看自动处置报告
system-tool guard-disable          # 停止并禁用守卫
system-tool clean                  # 简单清理，逐项确认
system-tool deep-clean             # 深度清理，逐项确认
system-tool deep-clean --dry-run   # 只计算，不删除
system-tool clean --yes            # 非交互执行所有简单清理项
system-tool status --json          # 给脚本使用的JSON
```

旧命令 `./system_tool.py --once` 和 `--json` 继续兼容。

## 清理边界

简单清理只处理缩略图缓存、30天前的 Chrome/Cursor 崩溃报告、本工具字节码，并可关闭 Snap Store。深度清理还会逐项询问 Chrome/Cursor 缓存、pip/npm 下载缓存和 Mesa 着色器缓存，并列出 Cursor、Chrome、飞书、微信、回放/RViz 的 RAM 与 Swap；只有用户明确选择后才会正常退出对应应用。

深度清理默认不自动勾选。运行中的 Chrome/Cursor 缓存会自动跳过；`--yes` 只确认可重建缓存和 Snap Store，不会批量关闭用户应用。工具绝不运行 `swapoff`、不清空 Linux 页缓存、不碰回收站、项目或 rosbag；“回放/RViz”只匹配明确的回放辅助命令，不匹配普通 ROS 生产节点。

## 卡顿判断与低负担采样

工具综合 CPU、内存、Swap-in/out、PSI、I/O `iowait`、`D` 状态进程、GPU 和内核告警，能区分“Swap 历史占用很大”和“正在从 Swap 回读导致桌面卡死”。

- 每1秒：CPU、内存、load、PSI、换页速率。
- 每3秒：进程 CPU/RAM/Swap、任务组和 D 状态。
- 每15秒：`nvidia-smi`。
- 每60秒：近期内核告警。
- 不读取昂贵的 `smaps`，不执行全盘 `du`、SMART 或全量 `lsof`。

## 开发与测试

```bash
python3 -m unittest discover -s tests -v
./system_tool.py status --no-gpu --no-logs
./system_tool.py deep-clean --dry-run
```

支持 Linux cgroup v2。无 NVIDIA GPU或无日志权限时会自动降级。

## 后台自动止血

运行 `system-tool guard-enable` 后，用户级 systemd 服务会在后台低频采样。只有同时满足
“可用内存极低”和“PSI或Swap-out持续处于临界状态”45秒，才会按当前RSS选择最大的
用户程序组。历史Swap占用不会被当作选择杀除对象的主要依据。
它先发送 `SIGTERM`，等待10秒；只有压力仍未恢复时才发送 `SIGKILL`。

桌面、终端、systemd、SSH、Codex/ChatGPT和守卫自身受到保护。每次动作都会显示桌面
通知，并把进程、原因、动作及前后内存写入
`~/.local/state/system-tool/incidents/`。同类自动动作至少间隔10分钟。GPU或内核日志
风暴只告警和留证，不会在无法可靠归因时随意杀进程。

守卫不会执行 `swapoff`、清空页缓存或自动重启电脑。卸载工具时故障报告默认保留。

同一种内核错误在10分钟内只生成一份报告和一次桌面通知，持续错误仍会在采样历史中
累计。随工具安装的双屏布局脚本会先检查GNOME或logind锁屏状态；锁屏或无法可靠判断
状态时不会调用 `xrandr`，解锁状态下才保留原有布局修复行为。

NVIDIA 的 `invalid head number` 在锁屏期间归类为显示切换事件：保留在采样历史中，
但不生成事故报告或桌面警告。解锁后错误停止则自动消退；只有在解锁状态持续产生
60秒才升级告警。`NVRM Xid`、OOM和其他内核错误不受此静默规则影响。

双屏排版不再每5秒调用 `xrandr`。`system-tool-monitor-hotplug.service` 常驻等待 DRM
热插拔或屏幕解锁事件；普通DRM事件只有在连接器状态确实变化时才检查布局，避免显示
查询自身再次产生DRM事件的反馈循环。启动和解锁时也检查一次，因此仍会保持外屏
在左、内屏在右且内屏为主屏。守卫同时检查 GNOME Shell 的 AppIndicators 递归和
一分钟内异常内存增长；命中时只临时停用托盘扩展并保存报告，不结束桌面会话。

当 NVIDIA `invalid head` 持续产生，或 GNOME Shell stage-view错误形成风暴，或图形
任务在高I/O压力下持续处于D状态时，守卫写入10分钟显示检查熔断标记。熔断只阻止新
的自动布局查询，不改变当前排版、不关闭应用；到期后自动恢复热插拔处理。

临时显示熔断和纯留证事件都静默执行，不弹窗；只有停用异常扩展或结束失控应用这类
会明显影响使用的动作才通知一次，并明确说明影响范围和报告路径。内存止血不再分别
弹“开始”和“完成”两次通知。

安装程序会将原有双屏脚本备份到
`~/.local/share/system-tool/backups/fix-monitor-layout.sh.pre-1.2.1`；运行卸载脚本时会先
恢复该版本，避免留下不可回退的显示配置变化。
