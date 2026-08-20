#!/bin/bash
# 每日世界备份：暂停自动存盘 -> 强制落盘 -> tar -> 恢复自动存盘，保留最近 7 份。
set -euo pipefail

BASE=/opt/dyyjs-minecraft
STAMP=$(date +%Y%m%d-%H%M%S)
OUT="$BASE/backups/world-$STAMP.tar.gz"
RCON="python3 $BASE/rcon.py"

if systemctl is-active --quiet dyyjs-minecraft; then
    $RCON save-off > /dev/null
    $RCON "save-all flush" > /dev/null
    sleep 3
    trap '$RCON save-on > /dev/null || true' EXIT
fi

tar -czf "$OUT" -C "$BASE/server" world

# 只保留最新 7 份
ls -1t "$BASE"/backups/world-*.tar.gz | tail -n +8 | xargs -r rm --

echo "备份完成: $OUT ($(du -sh "$OUT" | cut -f1))"
