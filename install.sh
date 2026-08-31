#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_LIB="${XDG_DATA_HOME:-$HOME/.local/share}/system-tool"
INSTALL_BIN="$HOME/.local/bin"
APPLICATION_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"

mkdir -p "$INSTALL_LIB" "$INSTALL_BIN" "$APPLICATION_DIR"
install -m 0755 "$SOURCE_ROOT/system_tool.py" "$INSTALL_LIB/system_tool.py"
ln -sfn "$INSTALL_LIB/system_tool.py" "$INSTALL_BIN/system-tool"
sed "s|@EXEC@|$INSTALL_BIN/system-tool|g" "$SOURCE_ROOT/system-tool.desktop.in" > "$APPLICATION_DIR/system-tool.desktop"
chmod 0644 "$APPLICATION_DIR/system-tool.desktop"

echo "安装完成。"
echo "终端启动：system-tool"
echo "如果当前终端找不到命令，请运行：$INSTALL_BIN/system-tool"
echo "桌面应用列表中可搜索：电脑卡顿助手"
