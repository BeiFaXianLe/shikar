# -*- coding: utf-8 -*-
"""从仓库内 data/*.csv 构建 shikar SQLite 数据库（可复现、可移植）。

数据来源（随仓库持久化）：
- data/history2019.csv  链家 2019 全量小区快照（公开数据集）
- data/fang_2026.csv    房天下 2026 当期小区快照（公开聚合页抓取）

输出：data/shikar.db
用法：python scripts/build_db.py

本脚本只写**原始数据**（xiaoqu / xiaoqu_metric_weekly 的价格与供应）。
``mom4`` / ``absorb`` / ``temperature`` / ``stage`` 等派生列一律留空，由
``scripts/recompute_metrics.py``（``shikar.pipeline.recompute``）统一计算。
这样口径只有一份实现：旧实现把平台给的同月环比 ``mom1_pct`` 直接当 ``mom4``
写库，随后又被 recompute 用另一个错口径覆盖（docs/45 报告 P0-2 / docs/04 §5）。
"""
import csv, os, sqlite3, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
from shikar import storage  # noqa: E402

DATA = os.path.join(ROOT, "data")

# 没有 CSV 来源、重建时必须原样保留的表。
# poi / community_poi 由 build_poi.py / osm_poi.py / fetch_hsr.py 采集；
# signal_event / source_snapshot 由运行期写入。这些表在 build_db.py 重建时若不显式
# 备份回灌，就会被清空（曾经导致线上学校/医院/高铁站三页全空）。
PRESERVED_TABLES = ("signal_event", "source_snapshot")



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
    """删除旧库前，把 poi / community_poi / signal_event / source_snapshot 的全部行读进内存。

    返回 (poi_rows, rel_rows, extra_rows, err)。extra_rows 是 {表名: (列名列表, 行列表)}，
    存放同样没有 CSV 来源、重建时必须原样保留的表（signal_event / source_snapshot）。

    旧库不存在、或旧库没有 poi 表（首次部署）时返回空列表，构建照常进行；
    旧库读失败则把错误放在 err 里，由 main 中止构建。
    """
    if not os.path.exists(db):
        return [], [], {}, None
    try:
        # 只读打开：绝不能因为备份反而改动旧库
        old = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
        try:
            tables = {r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "poi" not in tables:
                return [], [], {}, None
            poi_rows = old.execute(
                "SELECT id,type,subtype,name,city,grade,lat,lng,district,address,source,version FROM poi"
            ).fetchall()
            rel_rows = []
            if "community_poi" in tables:
                rel_rows = old.execute(
                    "SELECT community_id,poi_id,distance_km FROM community_poi"
                ).fetchall()
            extra_rows = {}
            for t in PRESERVED_TABLES:
                if t in tables:
                    cols = [r[1] for r in old.execute("PRAGMA table_info(%s)" % t)]
                    rows = old.execute("SELECT %s FROM %s" % (",".join(cols), t)).fetchall()
                    if rows:
                        extra_rows[t] = (cols, rows)
            return poi_rows, rel_rows, extra_rows, None
        finally:
            old.close()
    except sqlite3.Error as e:
        return [], [], {}, str(e)


def restore_poi(conn, poi_rows, rel_rows, extra_rows=None):
    """把 load_poi 备份的数据写回新库，返回 (poi 行数, 关联行数, 其他表行数字典)。

    - poi 整行按原样写回（含手动打的 grade / version 标签）。
    - community_poi 只写新库里 xiaoqu 仍存在、且 poi 已回灌的行，跳过孤儿关联，
      避免外键/主键冲突让整个 refresh 失败。
    - signal_event / source_snapshot 按原主键（AUTOINCREMENT 的 id）整行写回，
      保住历史事件与快照的 id 不变；xiaoqu 已不存在的行同样跳过，避免外键问题。
    """
    n_poi = 0
    if poi_rows:
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

    extra_counts = {}
    for t, (cols, rows) in (extra_rows or {}).items():
        new_xq = {r[0] for r in conn.execute("SELECT id FROM xiaoqu")}
        # xiaoqu_id 可为 NULL（source_snapshot 允许），只丢弃指向已消失小区的行
        idx = cols.index("xiaoqu_id") if "xiaoqu_id" in cols else None
        if idx is None:
            alive = rows
        else:
            alive = [r for r in rows if r[idx] is None or r[idx] in new_xq]
        del new_xq
        conn.executemany(
            "INSERT OR REPLACE INTO %s (%s) VALUES (%s)"
            % (t, ",".join(cols), ",".join("?" * len(cols))),
            alive,
        )
        extra_counts[t] = len(alive)
        if len(alive) != len(rows):
            print(f"  {t} 跳过 {len(rows) - len(alive)} 行（关联小区已不在库中）")
    return n_poi, n_rel, extra_counts

def _current_week(rows):
    """当期周：rows（2026 CSV 行）中出现次数最多的 week；并列取字典序最大；无数据返回 None。"""
    counts = {}
    for r in rows:
        w = (r.get("week") or "").strip()
        if not w:
            continue
        counts[w] = counts.get(w, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda w: (counts[w], w))


def dedupe_2026(rows):
    """2026 CSV 去重：**同城 + 同小区 + 同周**才算重复。

    早期实现只用 ``(city, xiaoqu_id)``，会把同一小区后续周次全部当成重复丢掉，
    导致 CSV 永远只剩第一周（与 fetch_fang 的同型缺陷）。
    """
    seen = set()
    out = []
    for r in rows:
        key = (r.get("city"), r.get("xiaoqu_id"), (r.get("week") or "").strip())
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def main():
    rows2026 = read_csv("fang_2026.csv")
    rows2019 = read_csv("history2019.csv")

    # 2026 去重（同 city + xiaoqu_id + week）
    cur = dedupe_2026(rows2026)
    current_week = _current_week(cur)
    # 2019 按「城市 + 小区名」配对到 2026 小区；多周时优先用当期周那行做档案。
    cur_by_key = {}
    for r in cur:
        k = (r["city"], r["xiaoqu_name"].strip())
        if k not in cur_by_key or (r.get("week") or "").strip() == current_week:
            cur_by_key[k] = r

    xiaoqu = {}   # id -> meta
    metrics = []  # (xid, week, price, supply)

    def add_xq(xid, city, district, plate, name, build_year=None, lng=None, lat=None):
        if xid not in xiaoqu:
            xiaoqu[xid] = {"id": xid, "city": city, "district": district, "plate": plate,
                           "name": name, "build_year": build_year, "lng": lng, "lat": lat}

    # 2026 当期
    for r in cur:
        xid = f'{r["city"]}|{r["xiaoqu_id"]}'
        add_xq(xid, r["city"], r["district"], r.get("plate") or None, r["xiaoqu_name"], to_int(r.get("built_year")))
        metrics.append((xid, (r["week"] or "").strip(), to_float(r["price"]), to_int(r.get("supply"))))

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
        metrics.append((xid, (r["week"] or "").strip(), to_float(r["price"]), None))

    # 派生指标（mom4 / absorb / temperature / stage）不在本脚本计算，一律交给
    # scripts/recompute_metrics.py（shikar.pipeline.recompute）：门槛与口径只有
    # 一份实现，避免 build_db 与 recompute 两条写入路径互相覆盖（docs/45 P0-2）。

    db = os.path.join(DATA, "shikar.db")
    # POI 数据没有外部 CSV 来源（由 build_poi.py / osm_poi.py / fetch_hsr.py 采集入库），
    # 重建前先备份，重建后回灌，否则每周 refresh 会把 poi / community_poi 清空。
    saved_poi, saved_rel, saved_extra, poi_err = load_poi(db)
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

        n_metric = 0
        for xid, week, price, supply in metrics:
            # 派生列留空：统一由 recompute_metrics.py 计算（口径只有一份）。
            storage.upsert_metric(conn, {
                "xiaoqu_id": xid, "week": week, "price": price, "supply": supply,
                "vol30": None, "bargain": None, "heat": None,
                "mom4": None, "mom13": None, "absorb": None,
                "temperature": None, "stage": None,
            })
            n_metric += 1

        n_poi, n_rel, n_extra = restore_poi(conn, saved_poi, saved_rel, saved_extra)
        conn.commit()

        # 交付前校验：备份了几行就必须写回几行（孤儿行会被显式跳过并打印），
        # 数量不符视为构建失败，保留旧库不动。
        if len(saved_poi) != n_poi:
            sys.exit(f"POI 回灌数量不符：备份 {len(saved_poi)} 行，写入 {n_poi} 行；已保留旧库不动")
        for t, (cols, rows) in saved_extra.items():
            if n_extra.get(t, 0) > len(rows):
                sys.exit(f"{t} 回灌数量异常：备份 {len(rows)} 行，写入 {n_extra[t]} 行；已保留旧库不动")

        extra_desc = "".join(f" {t}={n_extra[t]}" for t in sorted(n_extra))
        print(f"xiaoqu={len(xiaoqu)} metrics={n_metric} matched2019={matched} "
              f"poi={n_poi} community_poi={n_rel}{extra_desc}")
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
    try:
        if os.path.exists(db):
            os.replace(db, prev_db)
        os.replace(tmp_db, db)
    except OSError as e:
        # Windows 上若有进程仍持有 db 连接，rename 会失败（PermissionError/WinError 32）。
        # 此时旧库仍完好、新库在 .building 里，只需停掉占用进程后重跑。
        sys.exit(
            "替换数据库失败（旧库未动，新库保留在 %s）：%s\n"
            "  常见原因：有进程仍持有 %s 的连接（如本地调试服务未退出）。\n"
            "  处理：停掉占用进程后重跑 build_db.py。" % (tmp_db, e, db)
        )
    print("db ->", db)


if __name__ == "__main__":
    main()

