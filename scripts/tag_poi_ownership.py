# -*- coding: utf-8 -*-
"""把人工核对的医院性质（公办 / 民办 / 社区）写进 poi.ownership。

输入 CSV 表头固定：``poi_id,name,city,grade,ownership,note``

* 只接受 ``ownership ∈ {公办, 民办, 社区}``，其余行跳过并打印警告；
* 只按 ``poi_id`` 做 ``UPDATE``，**不走 upsert_poi**，避免采集脚本把值写回 NULL；
* 幂等：重复跑只统计真正发生变化的行；
* ``--dry-run`` 打印将要改的行与前后对比，不改库。

为什么人工核对：公开渠道（国家医保服务平台等）只给「类型」和「等级」，
没有「公办 / 民办」这个字段。核对依据优先用医院官网「医院简介」里的举办单位，
或北京卫健委 ``bjggcx.wjw.beijing.gov.cn/Unit/Index`` 逐条查证。

用法：
  python scripts/tag_poi_ownership.py --csv data/poi_ownership.csv --dry-run
  python scripts/tag_poi_ownership.py --csv data/poi_ownership.csv
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
from shikar import storage  # noqa: E402

ALLOWED = ("公办", "民办", "社区")
FIELDS = ("poi_id", "name", "city", "grade", "ownership", "note")


def load_rows(path: str) -> list[dict]:
    """读 CSV；表头缺失或不对直接报错，避免静默空跑。"""
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in FIELDS if c not in (reader.fieldnames or [])]
        if missing:
            raise ValueError("CSV 缺少表头列: %s（需要 %s）" % (",".join(missing), ",".join(FIELDS)))
        return list(reader)


def apply_rows(conn: sqlite3.Connection, rows: list[dict], dry_run: bool = False) -> dict:
    """把核对结果写入库，返回统计字典（changed / skipped / missing / unchanged）。

    只更新 ``ownership``；不碰 grade、不碰其他列，也不插入新行。
    """
    stat = {"rows": len(rows), "changed": 0, "unchanged": 0, "skipped": 0,
            "missing": 0, "changes": []}
    for r in rows:
        pid = (r.get("poi_id") or "").strip()
        own = (r.get("ownership") or "").strip()
        if not pid:
            stat["skipped"] += 1
            print("跳过：poi_id 为空（name=%r）" % (r.get("name"),), file=sys.stderr)
            continue
        if own not in ALLOWED:
            stat["skipped"] += 1
            print("跳过：ownership=%r 不在 %s（poi_id=%s name=%r）"
                  % (own, "/".join(ALLOWED), pid, r.get("name")), file=sys.stderr)
            continue
        cur = conn.execute("SELECT ownership FROM poi WHERE id=?", (pid,)).fetchone()
        if cur is None:
            stat["missing"] += 1
            print("跳过：库中无此 poi_id（%s %r）" % (pid, r.get("name")), file=sys.stderr)
            continue
        old = cur[0]
        if old == own:
            stat["unchanged"] += 1
            continue
        stat["changed"] += 1
        stat["changes"].append((pid, old, own, r.get("name")))
        if dry_run:
            print("DRY  %s  %r  %s -> %s" % (pid, r.get("name"), old, own))
        else:
            conn.execute("UPDATE poi SET ownership=? WHERE id=?", (own, pid))
    if not dry_run:
        conn.commit()
    return stat


def print_summary(conn: sqlite3.Connection) -> None:
    """打印分城市/性质统计与覆盖率，便于在 PR 里写清数据口径。"""
    total = conn.execute("SELECT COUNT(*) FROM poi WHERE type='hospital'").fetchone()[0]
    graded = conn.execute(
        "SELECT COUNT(*) FROM poi WHERE type='hospital' AND grade IN ('三甲','二甲')"
    ).fetchone()[0]
    owned = conn.execute(
        "SELECT COUNT(*) FROM poi WHERE type='hospital' AND ownership IS NOT NULL"
    ).fetchone()[0]
    print("\n医院总数=%d 有等级(三甲/二甲)=%d 有性质=%d" % (total, graded, owned))
    for row in conn.execute(
        "SELECT city, ownership, COUNT(*) FROM poi "
        "WHERE type='hospital' AND ownership IS NOT NULL "
        "GROUP BY city, ownership ORDER BY city, ownership"
    ):
        print("  %s  %s  %d" % (row[0] or "(城市空)", row[1], row[2]))


def main() -> None:
    ap = argparse.ArgumentParser(description="人工核对医院性质（公办/民办/社区）入库")
    ap.add_argument("--csv", default=os.path.join(ROOT, "data", "poi_ownership.csv"))
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "shikar.db"))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(a.csv):
        sys.exit("CSV 不存在：%s" % a.csv)
    rows = load_rows(a.csv)

    conn = sqlite3.connect(a.db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    storage.init_db(conn)
    try:
        stat = apply_rows(conn, rows, dry_run=a.dry_run)
        print("CSV 行数=%d 变更=%d 未变=%d 跳过=%d 库中无此 id=%d"
              % (stat["rows"], stat["changed"], stat["unchanged"], stat["skipped"], stat["missing"]))
        if a.dry_run:
            print("（--dry-run：未写库）")
        print_summary(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
