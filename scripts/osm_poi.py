# -*- coding: utf-8 -*-
"""从 OSM .osm.pbf 抽取 学校/医院/铁路车站，并写入 poi / community_poi（全程免费、WGS84）。

用法：
  python scripts/osm_poi.py --city 北京 --pbf D:/osmdata/beijing.osm.pbf --dry-run
  python scripts/osm_poi.py --city 北京 --pbf D:/osmdata/beijing.osm.pbf
  python scripts/osm_poi.py --city 天津 --pbf D:/osmdata/tianjin.osm.pbf
"""
from __future__ import annotations

import argparse
import math
import os
import sqlite3
import sys
from collections import defaultdict

import osmium

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
from shikar import storage  # noqa: E402

OSM_ROW_LIMIT = 300  # 每个 POI 最多保留最近 N 个小区距离


def school_subtype(tags) -> str:
    isced = (tags.get("isced:level") or "").strip()
    name = tags.get("name") or ""
    if isced.startswith("1") or "小学" in name:
        return "primary"
    if isced.startswith("2") or ("初中" in name or "中学" in name):
        return "middle"
    if isced.startswith("3") or "高中" in name:
        return "high"
    return "unknown"


def is_subway(tags) -> bool:
    return tags.get("station") == "subway" or tags.get("subway") == "yes"


class Extract(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.pois = []  # dict

    def _add(self, osm_type, oid, tags, lon, lat):
        if lon is None or lat is None:
            return
        name = tags.get("name")
        if not name:
            return
        if tags.get("amenity") == "school":
            self.pois.append({"id": f"osm:{osm_type}{oid}", "type": "school",
                              "subtype": school_subtype(tags), "name": name,
                              "grade": "普通", "lat": lat, "lng": lon,
                              "district": tags.get("addr:district")})
        elif tags.get("amenity") == "hospital":
            self.pois.append({"id": f"osm:{osm_type}{oid}", "type": "hospital",
                              "subtype": None, "name": name, "grade": "未知",
                              "lat": lat, "lng": lon, "district": tags.get("addr:district")})
        elif tags.get("railway") == "station" and not is_subway(tags):
            # 只保留真正的铁路客运站（名字以“站”结尾，排除地铁/线路/轻轨等）
            if not name.endswith("站") or any(k in name for k in ("地铁", "号线", "轻轨")):
                return
            self.pois.append({"id": f"osm:{osm_type}{oid}", "type": "hsr",
                              "subtype": None, "name": name, "grade": None,
                              "lat": lat, "lng": lon, "district": tags.get("addr:district")})

    def node(self, n):
        self._add("n", n.id, n.tags, n.location.lon if n.location.valid() else None,
                  n.location.lat if n.location.valid() else None)

    def way(self, w):
        t = w.tags
        if not (t.get("amenity") in ("school", "hospital") or (t.get("railway") == "station" and not is_subway(t))):
            return
        xs = []; ys = []
        try:
            for nd in w.nodes:
                if nd.location.valid():
                    xs.append(nd.location.lon); ys.append(nd.location.lat)
        except Exception:
            pass
        if not xs:
            return
        self._add("w", w.id, t, sum(xs) / len(xs), sum(ys) / len(ys))


def haversine_km(lng1, lat1, lng2, lat2):
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1); dl = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", required=True)
    ap.add_argument("--pbf", required=True)
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "shikar.db"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-dist-km", type=float, default=3.0)
    a = ap.parse_args()

    h = Extract()
    h.apply_file(a.pbf, locations=True)
    pois = h.pois
    by_type = defaultdict(int)
    for p in pois:
        by_type[p["type"]] += 1
    print(f"{a.city}: extracted {len(pois)} pois {dict(by_type)}", flush=True)
    for p in pois[:5]:
        print("  sample", p["type"], p["name"], round(p["lng"], 5), round(p["lat"], 5), p["subtype"], flush=True)
    if a.dry_run:
        return

    conn = sqlite3.connect(a.db, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    storage.init_db(conn)

    # 迁移百度已标注的「三甲/二甲/重点」到同名 OSM POI（按包含关系近似匹配）
    grade_rows = conn.execute(
        "SELECT name,grade FROM poi WHERE source='baidu' AND grade IN ('三甲','二甲','重点')"
    ).fetchall()
    grade_map = [(r["name"].strip(), r["grade"]) for r in grade_rows]

    def match_grade(name):
        nm = name.strip()
        for bn, g in grade_map:
            if len(bn) >= 3 and (bn in nm or nm in bn):
                return g
        return None

    # 清掉该城此前 OSM 导入，避免重复
    conn.execute("DELETE FROM community_poi WHERE poi_id LIKE 'osm:%' AND poi_id IN (SELECT id FROM poi WHERE city=?)", (a.city,))
    conn.execute("DELETE FROM poi WHERE city=? AND id LIKE 'osm:%'", (a.city,))
    for p in pois:
        g = match_grade(p["name"])
        if g:
            p = {**p, "grade": g}
        storage.upsert_poi(conn, {**p, "city": a.city, "address": None, "source": "osm", "version": "2026-11"})
    conn.commit()
    # 删除百度旧 POI（避免与 OSM 重复）
    conn.execute("DELETE FROM community_poi WHERE poi_id IN (SELECT id FROM poi WHERE source='baidu')")
    conn.execute("DELETE FROM poi WHERE source='baidu'")
    conn.commit()

    comms = conn.execute("SELECT id,lng,lat FROM xiaoqu WHERE city=? AND lng IS NOT NULL AND lat IS NOT NULL", (a.city,)).fetchall()
    total_pairs = 0
    for p in pois:
        near = []
        for c in comms:
            if abs(c["lat"] - p["lat"]) > 0.15 or abs(c["lng"] - p["lng"]) > 0.20:
                continue
            d = haversine_km(p["lng"], p["lat"], c["lng"], c["lat"])
            if d <= a.max_dist_km:
                near.append((d, c["id"]))
        near.sort()
        for d, cid in near[:OSM_ROW_LIMIT]:
            storage.upsert_community_poi(conn, cid, p["id"], round(d, 3))
            total_pairs += 1
    conn.commit()
    print(f"community_poi upserted={total_pairs}", flush=True)
    for row in conn.execute("SELECT type,COUNT(*) FROM poi WHERE city=? GROUP BY type", (a.city,)):
        print("  poi", tuple(row))
    conn.close()


if __name__ == "__main__":
    main()
