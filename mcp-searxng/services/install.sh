#!/usr/bin/env bash
# 安装 services/ 下的 systemd 单元到 /etc/systemd/system 并启用 timers
# 用法: sudo ./services/install.sh
set -euo pipefail

SERVICES_DIR="$(cd "$(dirname "$0")" && pwd)"
SYS_DIR=/etc/systemd/system

install -m 644 "$SERVICES_DIR"/*.service "$SERVICES_DIR"/*.timer "$SYS_DIR"/
systemctl daemon-reload

systemctl enable --now \
    mcp-searxng-pixiv-rank.timer \
    mcp-searxng-pixiv-rank-weekly.timer \
    mcp-searxng-pixiv-rank-monthly.timer \
    mcp-searxng-pixiv-rank-r18daily.timer \
    mcp-searxng-pixiv-rank-r18weekly.timer \
    mcp-searxng-pixiv-desc.timer

echo "systemd 单元已安装，timers 已启用"
systemctl list-timers 'mcp-searxng*' --no-pager
