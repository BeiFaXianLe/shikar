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


def load_poi(db):
    """删除旧库前，把 poi / community_poi 的全部行读进内存。

    返回 (poi_rows, rel_rows, err)。旧库不存在、或旧库没有 poi 表（首次部署）时
    返回空列表，构建照常进行；旧库读失败则把错误放在 err 里，由 main 中止构建。
    """
    if not os.path.exists(db):
        return [], [], None
    try:
        # 只读打开：绝不能因为备份反而改动旧库
        old = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
        try:
            tables = {r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "poi" not in tables:
                return [], [], None
            poi_rows = old.execute(
                "SELECT id,type,subtype,name,city,grade,lat,lng,district,address,source,version FROM poi"
            ).fetchall()
            rel_rows = []
            if "community_poi" in tables:
                rel_rows = old.execute(
                    "SELECT community_id,poi_id,distance_km FROM community_poi"
                ).fetchall()
            return poi_rows, rel_rows, None
        finally:
            old.close()
    except sqlite3.Error as e:
        return [], [], str(e)


def restore_poi(conn, poi_rows, rel_rows):
    """把 load_poi 备份的 POI 行写回新库，返回 (poi 行数, 关联行数)。

    - poi 整行按原样写回（含手动打的 grade / version 标签）。
    - community_poi 只写新库里 xiaoqu 仍存在、且 poi 已回灌的行，跳过孤儿关联，
      避免外键/主键冲突让整个 refresh 失败。
    """
    if not poi_rows:
        return 0, 0
    cols = "id,type,subtype,name,city,grade,lat,lng,district,address,source,version"
    conn.executemany(f"INSERT OR REPLACE INTO poi ({cols}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", poi_rows)
    n_poi = len(poi_rows)

    n_rel = 0
    if rel_rows:
        new_xq = {r[0] for r in conn.execute("SELECT id FROM xiaoqu")}
        new_poi = {r[0] for r in conn.execute("SELECT id FROM poi")}
        alive = [r for r in rel_rows if r[0] in new_xq and r[1] in new_poi]
        del new_xq, new_poi  # 生产环境 community_poi 可达数百万行，尽早释放集合
        conn.executemany(
            "INSERT OR REPLACE INTO community_poi (community_id,poi_id,distance_km) VALUES (?,?,?)",
            alive,
        )
        n_rel = len(alive)
        if len(alive) != len(rel_rows):
            print(f"  community_poi 跳过孤儿关联 {len(rel_rows) - len(alive)} 行（小区已不在库中）")
    return n_poi, n_rel


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

    def add_xq(xid, city, district, plate, name, build_year=None, lng=None, lat=None):
        if xid not in xiaoqu:
            xiaoqu[xid] = {"id": xid, "city": city, "district": district, "plate": plate,
                           "name": name, "build_year": build_year, "lng": lng, "lat": lat}

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
        lng = to_float(r.get("lng")); lat = to_float(r.get("lat"))
        key = (city, name)
        if key in cur_by_key:
            xid = f'{cur_by_key[key]["city"]}|{cur_by_key[key]["xiaoqu_id"]}'
            matched += 1
            # 用 2019 数据回填坐标（2026 快照无坐标）
            if lng is not None and lat is not None:
                xiaoqu[xid]["lng"] = lng
                xiaoqu[xid]["lat"] = lat
        else:
            xid = f'{city}|2019|{r["xiaoqu_id"]}'
            add_xq(xid, city, r["district"], r.get("plate") or None, name, lng=lng, lat=lat)
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
    # POI 数据没有外部 CSV 来源（由 build_poi.py / osm_poi.py / fetch_hsr.py 采集入库），
    # 重建前先备份，重建后回灌，否则每周 refresh 会把 poi / community_poi 清空。
    saved_poi, saved_rel, poi_err = load_poi(db)
    if poi_err:
        # 旧库读不出来时，宁可先不删库，等人工处理，也不要静默丢数据
        sys.exit(f"读取旧库 poi 失败，已中止以免丢失 POI：{poi_err}")

    # 建到临时库，校验通过后再原子替换。
    # 这样旧库在整个构建过程中始终完好：任何一步失败都只是丢掉临时文件，
    # 不会出现「新库已就位但没有 POI」的中间态（旧实现先 os.remove 旧库，无退路）。
    tmp_db = db + ".building"
    if os.path.exists(tmp_db):
        os.remove(tmp_db)

    conn = sqlite3.connect(tmp_db, check_same_thread=False)
    try:
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

        n_poi, n_rel = restore_poi(conn, saved_poi, saved_rel)
        conn.commit()

        # 交付前校验：POI 数量必须与备份一致，否则视为构建失败（不替换旧库）。
        if len(saved_poi) != n_poi:
            sys.exit(f"POI 回灌数量不符：备份 {len(saved_poi)} 行，写入 {n_poi} 行；已保留旧库不动")

        print(f"xiaoqu={len(xiaoqu)} metrics={n_metric} matched2019={matched} temp_rows={n_temp} "
              f"poi={n_poi} community_poi={n_rel}")
        for row in conn.execute("SELECT city, COUNT(*) c FROM xiaoqu GROUP BY city"):
            print(f"  {row['city']}: {row['c']}")
    except BaseException:
        conn.close()
        if os.path.exists(tmp_db):
            os.remove(tmp_db)
        raise
    else:
        conn.close()

    # 原子替换：旧库先改名为 .prev（保底），临时库再改名到位。
    # 任一步失败都能靠 .prev 恢复，且 .prev 不会被下次刷新无条件覆盖。
    prev_db = db + ".prev"
    if os.path.exists(prev_db):
        os.remove(prev_db)
    if os.path.exists(db):
        os.replace(db, prev_db)
    os.replace(tmp_db, db)
    print("db ->", db)


if __name__ == "__main__":
    main()
