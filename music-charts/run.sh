#!/bin/sh
# 每日榜单更新入口：抓取各平台榜单 -> 匹配本地曲库 -> 写入 Navidrome 播放列表 -> 生成未匹配报表
set -u
BASE=/volume1/docker/marvis/music-charts
MIRROR="/volume1/共享音乐/_榜单报表"
STAMP=$(date +%F)

cd "$BASE" || exit 1
mkdir -p reports logs "$MIRROR"

echo "[$(date '+%F %T')] start"
python3 charts.py >> "logs/run-$STAMP.out" 2>&1
RC=$?
echo "[$(date '+%F %T')] exit=$RC"

# 报表镜像一份到共享音乐，方便在文件管理器/Navidrome 挂载目录里直接看
cp -f "reports/未匹配清单-$STAMP.csv" "$MIRROR/" 2>/dev/null
cp -f "reports/榜单更新-$STAMP.md" "$MIRROR/" 2>/dev/null

exit $RC
