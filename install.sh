#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_LIB="${XDG_DATA_HOME:-$HOME/.local/share}/system-tool"
INSTALL_BIN="$HOME/.local/bin"
APPLICATION_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
USER_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

mkdir -p "$INSTALL_LIB" "$INSTALL_BIN" "$APPLICATION_DIR" "$USER_UNIT_DIR"
install -m 0755 "$SOURCE_ROOT/system_tool.py" "$INSTALL_LIB/system_tool.py"
install -m 0644 "$SOURCE_ROOT/system_guard.py" "$INSTALL_LIB/system_guard.py"
install -m 0644 "$SOURCE_ROOT/incident_store.py" "$INSTALL_LIB/incident_store.py"
install -m 0644 "$SOURCE_ROOT/journal_stream.py" "$INSTALL_LIB/journal_stream.py"
mkdir -p "$INSTALL_LIB/docs"
install -m 0644 "$SOURCE_ROOT/docs/freeze-prevention-design.md" "$INSTALL_LIB/docs/freeze-prevention-design.md"
install -m 0644 "$SOURCE_ROOT/systemd/system-tool-guard.service" "$USER_UNIT_DIR/system-tool-guard.service"
ln -sfn "$INSTALL_LIB/system_tool.py" "$INSTALL_BIN/system-tool"
sed "s|@EXEC@|$INSTALL_BIN/system-tool|g" "$SOURCE_ROOT/system-tool.desktop.in" > "$APPLICATION_DIR/system-tool.desktop"
chmod 0644 "$APPLICATION_DIR/system-tool.desktop"
if command -v systemctl >/dev/null 2>&1; then
    systemctl --user daemon-reload || true
fi

echo "安装完成。"
echo "终端启动：system-tool"
echo "如果当前终端找不到命令，请运行：$INSTALL_BIN/system-tool"
echo "桌面应用列表中可搜索：电脑卡顿助手"
echo "启用后台自动止血：system-tool guard-enable"
echo "查看后台状态：system-tool guard-status"
