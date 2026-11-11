# -*- coding: utf-8 -*-
"""把采集到的历史成交样本（CSV）按「小区×月」聚合后写入 shikar SQLite。

- 仅新增/更新指标行，**不删除**任何已有数据（不动 2019/2026 基线）。
- 同一小区同月多条成交取单价中位数作为该月 price，成交条数作为 supply。
- 不计算温度/阶段（保持诚实口径；曲线仍可展示）。

用法：
  python scripts/ingest_history.py --csv data/fang_preview_all.csv --db data/shikar.db
"""
import argparse, csv, os, sqlite3, statistics, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
from shikar import storage  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=os.path.join(ROOT, "data", "fang_preview_all.csv"))
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "shikar.db"))
    a = ap.parse_args()
    if not os.path.exists(a.csv):
        print("CSV 不存在:", a.csv); return

    rows = list(csv.DictReader(open(a.csv, "r", encoding="utf-8-sig", newline="")))
    print(f"读取 {len(rows)} 行")

    # 聚合： (xiaoqu_id, week) -> {price:[], meta}
    agg = {}
    meta = {}
    for r in rows:
        xid = (r.get("xiaoqu_id") or "").strip()
        wk = (r.get("week") or "").strip()
        try:
            price = float(r.get("price"))
        except (TypeError, ValueError):
            continue
        if not xid or not wk:
            continue
        agg.setdefault((xid, wk), []).append(price)
        meta[xid] = (r.get("city"), r.get("district"), r.get("plate"), r.get("xiaoqu_name"))

    conn = sqlite3.connect(a.db, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    storage.init_db(conn)

    n_comm = 0; n_cell = 0
    seen_comm = set()
    for (xid, wk), prices in agg.items():
        if xid not in seen_comm:
            # 若小区不存在则补建主数据
            cur = conn.execute("SELECT 1 FROM xiaoqu WHERE id=?", (xid,)).fetchone()
            if not cur:
                city, district, plate, name = meta[xid]
                storage.upsert_xiaoqu(conn, {"id": xid, "city": city, "district": district,
                                             "plate": plate, "name": name, "build_year": None})
            seen_comm.add(xid)
        price = round(statistics.median(prices), 0)
        storage.upsert_metric(conn, {
            "xiaoqu_id": xid, "week": wk, "price": price, "supply": len(prices),
            "vol30": None, "bargain": None, "heat": None,
            "mom4": None, "mom13": None, "absorb": None,
            "temperature": None, "stage": None,
        })
        n_cell += 1
    conn.commit()
    n_comm = len(seen_comm)
    print(f"完成：小区 {n_comm} 个，写入(小区×月) {n_cell} 行 -> {a.db}")
    for row in conn.execute("SELECT city, COUNT(*) c FROM xiaoqu GROUP BY city"):
        print(f"  {row['city']}: {row['c']} 小区")
    conn.close()


if __name__ == "__main__":
    main()
