# -*- coding: utf-8 -*-
"""从仓库内 data/*.csv 构建 shikar SQLite 数据库（可复现、可移植）。

数据来源（随仓库持久化）：
- data/history2019.csv  链家 2019 全量小区快照（公开数据集）
- data/fang_2026.csv    房天下 2026 当期小区快照（公开聚合页抓取）

输出：data/shikar.db
用法：python scripts/build_db.py
"""
import csv, os, sqlite3, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
from shikar import storage, scoring  # noqa: E402

DATA = os.path.join(ROOT, "data")


def read_csv(name):
    path = os.path.join(DATA, name)
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def to_float(v):
    v = (v or "").strip()
    if v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def to_int(v):
    v = (v or "").strip()
    if v == "":
        return None
    try:
        return int(float(v))
    except ValueError:
        return None


def main():
    rows2026 = read_csv("fang_2026.csv")
    rows2019 = read_csv("history2019.csv")

    # 2026 去重（同 city+xiaoqu_id）
    seen = set(); cur = []
    for r in rows2026:
        k = (r["city"], r["xiaoqu_id"])
        if k in seen:
            continue
        seen.add(k); cur.append(r)
    cur_by_key = {(r["city"], r["xiaoqu_name"].strip()): r for r in cur}

    xiaoqu = {}   # id -> meta
    metrics = []  # (xid, week, price, supply, mom4)

    def add_xq(xid, city, district, plate, name, build_year=None):
        if xid not in xiaoqu:
            xiaoqu[xid] = {"id": xid, "city": city, "district": district, "plate": plate,
                           "name": name, "build_year": build_year}

    # 2026 当期
    for r in cur:
        xid = f'{r["city"]}|{r["xiaoqu_id"]}'
        add_xq(xid, r["city"], r["district"], r.get("plate") or None, r["xiaoqu_name"], to_int(r.get("built_year")))
        mom = None
        pct = to_float(r.get("mom1_pct"))
        if pct is not None:
            mom = pct / 100.0
        metrics.append((xid, r["week"], to_float(r["price"]), to_int(r.get("supply")), mom))

    # 2019 历史
    matched = 0
    for r in rows2019:
        city = r["city"]; name = r["xiaoqu_name"].strip()
        key = (city, name)
        if key in cur_by_key:
            xid = f'{cur_by_key[key]["city"]}|{cur_by_key[key]["xiaoqu_id"]}'
            matched += 1
        else:
            xid = f'{city}|2019|{r["xiaoqu_id"]}'
            add_xq(xid, city, r["district"], r.get("plate") or None, name)
        metrics.append((xid, r["week"], to_float(r["price"]), None, None))

    # —— 数据充足度：≥3 期、跨度≥365天、相邻缺口≤365天，才允许计算温度/阶段 ——
    from datetime import date
    def parse_week(w):
        try:
            if "W" in w:
                y, ww = w.split("-W"); return date.fromisocalendar(int(y), int(ww), 1)
            if "-" in w:
                y, m = w.split("-")[:2]; return date(int(y), int(m), 1)
        except Exception:
            return None
        return None

    weeks_by = {}
    for xid, week, price, supply, mom in metrics:
        weeks_by.setdefault(xid, set()).add(week)

    def sufficient(xid):
        ws = sorted(d for d in (parse_week(w) for w in weeks_by.get(xid, set())) if d)
        if len(ws) < 3:
            return False
        if (ws[-1] - ws[0]).days < 365:
            return False
        gaps = [(ws[i + 1] - ws[i]).days for i in range(len(ws) - 1)]
        if gaps and max(gaps) > 365:
            return False
        return True

    city_mom = {}
    for xid, week, price, supply, mom in metrics:
        if sufficient(xid) and mom is not None:
            city_mom.setdefault(xiaoqu[xid]["city"], []).append(mom)

    db = os.path.join(DATA, "shikar.db")
    if os.path.exists(db):
        os.remove(db)
    conn = sqlite3.connect(db, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    storage.init_db(conn)

    for m in xiaoqu.values():
        storage.upsert_xiaoqu(conn, m)

    n_metric = 0; n_temp = 0
    for xid, week, price, supply, mom in metrics:
        temp = None; stage = None
        if sufficient(xid) and week == "2026-W46":
            n_mom = 50.0 if mom is None else scoring.percentile_normalize(mom, city_mom.get(xiaoqu[xid]["city"], []))
            temp = scoring.compute_temperature({"mom4": n_mom, "absorb": 50.0, "bargain": 50.0, "heat": 50.0})
            stage = scoring.classify_stage(temp, mom if mom is not None else 0.0)
            n_temp += 1
        storage.upsert_metric(conn, {
            "xiaoqu_id": xid, "week": week, "price": price, "supply": supply,
            "vol30": None, "bargain": None, "heat": None,
            "mom4": mom, "mom13": None, "absorb": None,
            "temperature": temp, "stage": stage,
        })
        n_metric += 1
    conn.commit()

    print(f"xiaoqu={len(xiaoqu)} metrics={n_metric} matched2019={matched} temp_rows={n_temp}")
    for row in conn.execute("SELECT city, COUNT(*) c FROM xiaoqu GROUP BY city"):
        print(f"  {row['city']}: {row['c']}")
    print("db ->", db)
    conn.close()


if __name__ == "__main__":
    main()
