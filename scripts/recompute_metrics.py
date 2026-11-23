# -*- coding: utf-8 -*-
"""全量重算并回写派生指标（mom4 / absorb / temperature / stage）。

背景：``build_db.py`` 只写原始数据，``mom4`` / ``absorb`` / ``temperature`` /
``stage`` 一律留空（口径只有一份实现，避免两个写入方各算一套）。本脚本是
派生指标的唯一计算入口：不做这一步，``xiaoqu_metric_weekly.temperature``
会全部为 NULL，导致首页 ``/api/overview`` 聚合为空、页面空白。

本脚本调用 ``shikar.pipeline.recompute`` 对全部（或指定）小区重算并回写，
不重建库、不删除/不改动 POI 与地铁表，可安全重复执行（幂等）。

注意：运行前建议先停服（服务持有 SQLite 文件锁）。可用 ``--stop-service``
让脚本自动停服→重算→重启（需 sudo 权限）。

用法：
  python3 scripts/recompute_metrics.py --db data/shikar.db
  sudo python3 scripts/recompute_metrics.py --db data/shikar.db --stop-service
"""
import argparse
import os
import sqlite3
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
from shikar import pipeline  # noqa: E402


def _ctl(action):
    for cmd in (["sudo", "-n", "systemctl", action, "shikar"],
                ["systemctl", action, "shikar"]):
        try:
            if subprocess.call(cmd) == 0:
                return True
        except Exception:
            pass
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "shikar.db"))
    ap.add_argument("--stop-service", action="store_true",
                    help="重算前后自动 stop/start shikar.service（需 sudo）")
    a = ap.parse_args()

    if not os.path.exists(a.db):
        sys.exit("db 不存在: %s" % a.db)

    stopped = False
    if a.stop_service:
        stopped = _ctl("stop")
        print("stop shikar:", "ok" if stopped else "SKIP(无权限/未运行)")
        time.sleep(1)

    try:
        conn = sqlite3.connect(a.db, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        t0 = time.time()
        n = pipeline.recompute(conn)
        conn.commit()
        nn = conn.execute(
            "SELECT COUNT(*) FROM xiaoqu_metric_weekly WHERE temperature IS NOT NULL"
        ).fetchone()[0]
        tot = conn.execute(
            "SELECT COUNT(*) FROM xiaoqu_metric_weekly"
        ).fetchone()[0]
        print("recompute updated: %d rows, %.1fs" % (n, time.time() - t0))
        print("temperature not null: %d / %d" % (nn, tot))
        conn.close()
    finally:
        if stopped:
            ok = _ctl("start")
            print("start shikar:", "ok" if ok else "FAIL")


if __name__ == "__main__":
    main()
