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
cp -f data/shikar.db data/shikar.db.bak 2>/dev/null || true
if python3 scripts/build_db.py; then
  echo "build_db ok; re-ingesting history sample"
  python3 scripts/ingest_history.py --csv data/fang_preview_all.csv --db data/shikar.db || echo "ingest history skipped/failed"
  echo "restarting shikar"
  sudo -n systemctl restart shikar && echo "shikar restarted"
else
  echo "build_db FAILED; restoring old db, no restart"
  cp -f data/shikar.db.bak data/shikar.db 2>/dev/null || true
fi
echo "refresh done $(date '+%F %T %Z')"
