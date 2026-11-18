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
# 备份策略（服务器磁盘仅剩 ~14G，整库 248M，不能堆多份）：
#   a. 不可再生数据备份（tar.gz，含 CSV + 关键表，约 0.6MB）：
#        - data/fang_preview_all.csv  ← 每周追加 500 条历史成交，**丢了永久缺失**
#        - data/fang_2026.csv / history2019.csv
#        - poi / community_poi / signal_event / source_snapshot 表
#      保留 12 份 ≈ 7MB。
#   b. 整库快照：只保留 1 份（最近一次），供整库回滚用。
#   c. build_db.py 自己还会留 shikar.db.prev（原子替换前的旧库）。
TS=$(date '+%Y%m%d')
mkdir -p data/backups

# a. 不可再生数据备份（失败不阻塞刷新，但要显式告警）
python3 scripts/backup_critical.py backup --keep 12 || echo "WARN: 关键数据备份失败（继续）"

# b. 整库快照，只留 1 份
if [ -f data/shikar.db ]; then
  cp -f data/shikar.db data/shikar.db.fullbak 2>/dev/null || true
  # 清理历史遗留的固定名/日期备份，避免继续占磁盘
  rm -f data/shikar.db.bak 2>/dev/null || true
  ls -1t data/shikar.db.bak-* 2>/dev/null | tail -n +2 | xargs -r rm -f
fi

if python3 scripts/build_db.py; then
  echo "build_db ok; re-ingesting history sample"
  # 这一步把 fang_preview_all.csv 的历史成交灌进库。CSV 已在上方备份，
  # 所以即使库被重建，历史数据也能从这个 CSV 完整恢复。
  python3 scripts/ingest_history.py --csv data/fang_preview_all.csv --db data/shikar.db || echo "ingest history skipped/failed"
  # 重建成功后刷一次备份，保证「备份 = 当前库 + 当前 CSV」
  python3 scripts/backup_critical.py backup --keep 12 || echo "WARN: 关键数据备份失败（继续）"
  echo "restarting shikar"
  sudo -n systemctl restart shikar && echo "shikar restarted"
else
  echo "build_db FAILED; db 未被替换（旧库仍在 data/shikar.db），no restart"
  ls -lh data/shikar.db data/shikar.db.prev 2>/dev/null || true
fi
echo "refresh done $(date '+%F %T %Z')"
