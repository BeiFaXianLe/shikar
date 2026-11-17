#!/usr/bin/env bash
# shikar 每周维护：更新当期快照 → 随机采样历史成交(合并累积) → 重建库 → 回灌历史 → 重启
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
echo "refresh start $(date '+%F %T %Z')"

# 1) 更新当期快照（房天下公开聚合）
python3 scripts/fetch_fang.py --city bj --pages 100 --delay 1.5 || echo "fetch bj failed (continue)"
python3 scripts/fetch_fang.py --city tj --pages 100 --delay 1.5 || echo "fetch tj failed (continue)"

# 2) 每周随机采样历史成交（合并累积；bj 300 + tj 200 = 500，随机间隔+少量并发，公开页无需登录）
python3 scripts/sample_fang_preview.py --city bj --n 300 --workers 4 --out data/fang_preview_all.csv || echo "sample bj failed (continue)"
python3 scripts/sample_fang_preview.py --city tj --n 200 --workers 4 --out data/fang_preview_all.csv || echo "sample tj failed (continue)"

# 3) 备份 + 重建库
# 说明：build_db.py 自己会「建到临时库 → 校验 → 原子替换」，并把替换前的旧库留在
# data/shikar.db.prev。所以这里不再用固定名的 .bak（那会被每周覆盖，一次坏构建就把
# 唯一好备份冲掉）。另存一份带日期的备份，便于回溯任意历史版本。
TS=$(date '+%Y%m%d')
if [ -f data/shikar.db ]; then
  cp -f data/shikar.db "data/shikar.db.bak-$TS" 2>/dev/null || true
  # 只保留最近 4 份日备份，避免占满磁盘
  ls -1t data/shikar.db.bak-* 2>/dev/null | tail -n +5 | xargs -r rm -f
fi

if python3 scripts/build_db.py; then
  echo "build_db ok; re-ingesting history sample"
  python3 scripts/ingest_history.py --csv data/fang_preview_all.csv --db data/shikar.db || echo "ingest history skipped/failed"
  echo "restarting shikar"
  sudo -n systemctl restart shikar && echo "shikar restarted"
else
  echo "build_db FAILED; db 未被替换（旧库仍在 data/shikar.db），no restart"
  ls -lh data/shikar.db data/shikar.db.prev 2>/dev/null || true
fi
echo "refresh done $(date '+%F %T %Z')"
