#!/usr/bin/env bash
set -euo pipefail

INSTALL_LIB="${XDG_DATA_HOME:-$HOME/.local/share}/system-tool"
INSTALL_BIN="$HOME/.local/bin/system-tool"
APPLICATION_FILE="${XDG_DATA_HOME:-$HOME/.local/share}/applications/system-tool.desktop"
USER_UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/system-tool-guard.service"

if command -v systemctl >/dev/null 2>&1; then
    systemctl --user disable --now system-tool-guard.service >/dev/null 2>&1 || true
fi

if command -v gio >/dev/null 2>&1; then
    [ ! -e "$APPLICATION_FILE" ] || gio trash "$APPLICATION_FILE"
    if [ -e "$INSTALL_BIN" ] || [ -L "$INSTALL_BIN" ]; then
        gio trash "$INSTALL_BIN"
    fi
    [ ! -e "$INSTALL_LIB" ] || gio trash "$INSTALL_LIB"
    [ ! -e "$USER_UNIT" ] || gio trash "$USER_UNIT"
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --user daemon-reload >/dev/null 2>&1 || true
    fi
    echo "已移到回收站，可以恢复。"
    echo "故障报告仍保留在：${XDG_STATE_HOME:-$HOME/.local/state}/system-tool"
else
    echo "未找到gio。请手动删除以下安装文件："
    echo "$INSTALL_LIB"
    echo "$INSTALL_BIN"
    echo "$APPLICATION_FILE"
    echo "$USER_UNIT"
    exit 1
fi
