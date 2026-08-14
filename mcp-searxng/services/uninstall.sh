#!/usr/bin/env bash
# 卸载 services/ 对应的 systemd 单元（保留 mcp-searxng.service 需另行处理）
# 用法: sudo ./services/uninstall.sh
set -euo pipefail

systemctl disable --now \
    mcp-searxng-pixiv-rank.timer \
    mcp-searxng-pixiv-rank-weekly.timer \
    mcp-searxng-pixiv-rank-monthly.timer \
    mcp-searxng-pixiv-rank-r18daily.timer \
    mcp-searxng-pixiv-rank-r18weekly.timer \
    mcp-searxng-pixiv-desc.timer

systemctl disable mcp-searxng-pixiv-rank.service \
    'mcp-searxng-pixiv-rank@weekly.service' \
    'mcp-searxng-pixiv-rank@monthly.service' \
    'mcp-searxng-pixiv-rank@daily_r18.service' \
    'mcp-searxng-pixiv-rank@weekly_r18.service' \
    mcp-searxng-pixiv-desc.service

rm -f /etc/systemd/system/mcp-searxng-pixiv-rank*.{service,timer} \
    /etc/systemd/system/mcp-searxng-pixiv-desc.{service,timer}
systemctl daemon-reload
echo "pixiv-rank / pixiv-desc 相关单元已卸载（mcp-searxng.service 未动）"
