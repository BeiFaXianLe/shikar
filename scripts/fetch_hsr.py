# -*- coding: utf-8 -*-
"""用百度 POI 补充「高铁站/主要火车站」（少量请求），替换 OSM 中质量差的车站。"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
from shikar import baidu, storage  # noqa: E402

CITIES = {"bj": "北京", "tj": "天津"}


def main():
    db = os.path.join(ROOT, "data", "shikar.db")
    conn = sqlite3.connect(db, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    storage.init_db(conn)
    for code, cname in CITIES.items():
        conn.execute("DELETE FROM community_poi WHERE poi_id IN (SELECT id FROM poi WHERE city=? AND type='hsr')", (cname,))
        conn.execute("DELETE FROM poi WHERE city=? AND type='hsr'", (cname,))
        items = baidu.place_search("高铁站", cname, max_pages=3)
        kept = []
        for it in items:
            name = (it.get("name") or "").strip()
            if not name.endswith("站") or any(k in name for k in ("地铁", "号线", "轻轨")):
                continue
            storage.upsert_poi(conn, {
                "id": "baidu:" + it["uid"], "type": "hsr", "subtype": None,
                "name": name, "city": cname, "grade": None,
                "lat": it["lat"], "lng": it["lng"], "district": it.get("district"),
                "address": it.get("address"), "source": "baidu", "version": "2026-11",
            })
            kept.append(it)
        conn.commit()
        comms = conn.execute("SELECT id,lng,lat FROM xiaoqu WHERE city=? AND lng IS NOT NULL AND lat IS NOT NULL", (cname,)).fetchall()
        cnt = 0
        for it in kept:
            lat, lng = it["lat"], it["lng"]
            wlng, wlat = baidu.bd09_to_wgs84(lng, lat)
            near = []
            for c in comms:
                if abs(c["lat"] - wlat) > 0.4 or abs(c["lng"] - wlng) > 0.5:
                    continue
                d = baidu.haversine_km(wlng, wlat, c["lng"], c["lat"])
                if d <= 10:
                    near.append((d, c["id"]))
            near.sort()
            for d, cid in near[:300]:
                storage.upsert_community_poi(conn, cid, "baidu:" + it["uid"], round(d, 3))
                cnt += 1
        conn.commit()
        print(f"{cname}: hsr saved={len(kept)} pairs={cnt} names={[k['name'] for k in kept][:12]}", flush=True)
    conn.close()


if __name__ == "__main__":
    main()
