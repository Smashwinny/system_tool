#!/usr/bin/env bash
set -euo pipefail

INSTALL_LIB="${XDG_DATA_HOME:-$HOME/.local/share}/system-tool"
INSTALL_BIN="$HOME/.local/bin/system-tool"
APPLICATION_FILE="${XDG_DATA_HOME:-$HOME/.local/share}/applications/system-tool.desktop"

if command -v gio >/dev/null 2>&1; then
    [ ! -e "$APPLICATION_FILE" ] || gio trash "$APPLICATION_FILE"
    if [ -e "$INSTALL_BIN" ] || [ -L "$INSTALL_BIN" ]; then
        gio trash "$INSTALL_BIN"
    fi
    [ ! -e "$INSTALL_LIB" ] || gio trash "$INSTALL_LIB"
    echo "已移到回收站，可以恢复。"
else
    echo "未找到gio。请手动删除以下安装文件："
    echo "$INSTALL_LIB"
    echo "$INSTALL_BIN"
    echo "$APPLICATION_FILE"
    exit 1
fi
