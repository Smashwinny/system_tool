#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_LIB="${XDG_DATA_HOME:-$HOME/.local/share}/system-tool"
INSTALL_BIN="$HOME/.local/bin"
APPLICATION_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
BACKUP_DIR="$INSTALL_LIB/backups"
LAYOUT_SCRIPT="$INSTALL_BIN/fix-monitor-layout.sh"
LAYOUT_BACKUP="$BACKUP_DIR/fix-monitor-layout.sh.pre-1.2.1"
LAYOUT_STAGED="$INSTALL_BIN/.fix-monitor-layout.sh.new"

mkdir -p "$INSTALL_LIB" "$INSTALL_BIN" "$APPLICATION_DIR" "$USER_UNIT_DIR" "$BACKUP_DIR"
if [ -f "$LAYOUT_SCRIPT" ] && [ ! -e "$LAYOUT_BACKUP" ]; then
    install -m 0755 "$LAYOUT_SCRIPT" "$LAYOUT_BACKUP"
fi
install -m 0755 "$SOURCE_ROOT/system_tool.py" "$INSTALL_LIB/system_tool.py"
install -m 0644 "$SOURCE_ROOT/system_guard.py" "$INSTALL_LIB/system_guard.py"
install -m 0644 "$SOURCE_ROOT/incident_store.py" "$INSTALL_LIB/incident_store.py"
install -m 0644 "$SOURCE_ROOT/journal_stream.py" "$INSTALL_LIB/journal_stream.py"
install -m 0755 "$SOURCE_ROOT/monitor_hotplug.py" "$INSTALL_LIB/monitor_hotplug.py"
install -m 0755 "$SOURCE_ROOT/fix-monitor-layout.sh" "$LAYOUT_STAGED"
mv -f "$LAYOUT_STAGED" "$LAYOUT_SCRIPT"
mkdir -p "$INSTALL_LIB/docs"
install -m 0644 "$SOURCE_ROOT/docs/freeze-prevention-design.md" "$INSTALL_LIB/docs/freeze-prevention-design.md"
install -m 0644 "$SOURCE_ROOT/systemd/system-tool-guard.service" "$USER_UNIT_DIR/system-tool-guard.service"
install -m 0644 "$SOURCE_ROOT/systemd/system-tool-monitor-hotplug.service" "$USER_UNIT_DIR/system-tool-monitor-hotplug.service"
ln -sfn "$INSTALL_LIB/system_tool.py" "$INSTALL_BIN/system-tool"
sed "s|@EXEC@|$INSTALL_BIN/system-tool|g" "$SOURCE_ROOT/system-tool.desktop.in" > "$APPLICATION_DIR/system-tool.desktop"
chmod 0644 "$APPLICATION_DIR/system-tool.desktop"
if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload || true
    systemctl --user disable --now fix-monitor-layout.timer 2>/dev/null || true
    systemctl --user enable --now system-tool-monitor-hotplug.service || true
    systemctl --user try-restart system-tool-guard.service || true
fi

echo "安装完成。"
echo "终端启动：system-tool"
echo "如果当前终端找不到命令，请运行：$INSTALL_BIN/system-tool"
echo "桌面应用列表中可搜索：电脑卡顿助手"
echo "启用后台自动止血：system-tool guard-enable"
echo "查看后台状态：system-tool guard-status"
