# -*- coding: utf-8 -*-
"""用百度地图 POI 检索采集 学校/医院/高铁站，并计算与小区距离，写入 poi / community_poi。

用法：
  python scripts/build_poi.py --city bj --districts 海淀区,朝阳区 --kinds school,hospital
  python scripts/build_poi.py --city bj --kinds school,hospital,hsr        # 全部区
AK 从 BAIDU_AK 或 .secrets/baidu_ak.txt 读取（不入库）。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
from shikar import baidu, storage  # noqa: E402

DISTRICTS = {
    "bj": ("北京", ["东城区", "西城区", "朝阳区", "海淀区", "丰台区", "石景山区", "昌平区", "大兴区",
                   "通州区", "顺义区", "房山区", "门头沟区", "怀柔区", "平谷区", "密云区", "延庆区"]),
    "tj": ("天津", ["和平区", "河东区", "河西区", "南开区", "河北区", "红桥区", "东丽区", "西青区",
                   "津南区", "北辰区", "武清区", "宝坻区", "滨海新区", "宁河区", "静海区", "蓟州区"]),
}
SCHOOL_QUERIES = [("小学", "primary"), ("初中", "middle"), ("高中", "high")]


def poi_id(uid: str) -> str:
    return "baidu:" + str(uid)


def save_pois(conn, items, type_, subtype=None, grade=None, tag_by_uid=None, city=None):
    n = 0
    for it in items:
        if not it.get("uid") or it.get("lng") is None or it.get("lat") is None:
            continue
        g = grade
        if tag_by_uid is not None:
            g = "三甲" if it["uid"] in tag_by_uid else "未知"
        storage.upsert_poi(conn, {
            "id": poi_id(it["uid"]), "type": type_, "subtype": subtype, "name": it["name"],
            "grade": g, "city": city, "lat": it["lat"], "lng": it["lng"], "district": it.get("district"),
            "address": it.get("address"), "source": "baidu", "version": time.strftime("%Y-%m-%d"),
        })
        n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", choices=["bj", "tj"], required=True)
    ap.add_argument("--districts", default="")  # 空=全部
    ap.add_argument("--kinds", default="school,hospital")
    ap.add_argument("--max-pages", type=int, default=10)
    ap.add_argument("--max-dist-km", type=float, default=5.0)
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "shikar.db"))
    a = ap.parse_args()

    cname, all_districts = DISTRICTS[a.city]
    districts = [d.strip() for d in a.districts.split(",") if d.strip()] or all_districts
    kinds = set(k.strip() for k in a.kinds.split(",") if k.strip())

    conn = sqlite3.connect(a.db, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    storage.init_db(conn)

    print(f"[{cname}] districts={len(districts)} kinds={kinds}", flush=True)
    ns = nh = nz = 0
    for d in districts:
        if "school" in kinds:
            for q, sub in SCHOOL_QUERIES:
                try:
                    items = baidu.place_search(q, d, max_pages=a.max_pages)
                except Exception as e:
                    print(f"  school {d} {q} ERR {e}", flush=True); continue
                n = save_pois(conn, items, "school", sub, city=cname)
                ns += n
                print(f"  school {d} {q}: {len(items)} -> saved {n}", flush=True)
                time.sleep(0.25)
        if "hospital" in kinds:
            try:
                items = baidu.place_search("医院", d, max_pages=a.max_pages)
                sanjia = baidu.place_search("三甲医院", d, max_pages=a.max_pages)
                tagset = {x["uid"] for x in sanjia if x.get("uid")}
                n = save_pois(conn, items, "hospital", None, tag_by_uid=tagset, city=cname)
                nh += n
                print(f"  hospital {d}: {len(items)} -> saved {n} (三甲标记 {len(tagset)})", flush=True)
            except Exception as e:
                print(f"  hospital {d} ERR {e}", flush=True)
            time.sleep(0.25)
    if "hsr" in kinds:
        try:
            items = baidu.place_search("高铁站", cname, max_pages=a.max_pages)
            nz = save_pois(conn, items, "hsr", city=cname)
            print(f"  hsr {cname}: {len(items)} -> saved {nz}", flush=True)
        except Exception as e:
            print(f"  hsr ERR {e}", flush=True)
    conn.commit()
    print(f"POI saved: school={ns} hospital={nh} hsr={nz}", flush=True)

    # 计算距离（POI 为 BD09 -> WGS84；小区为 WGS84）
    pois = conn.execute("SELECT id,lat,lng FROM poi").fetchall()
    comms = conn.execute(
        "SELECT id,lng,lat FROM xiaoqu WHERE city=? AND lng IS NOT NULL AND lat IS NOT NULL", (cname,)
    ).fetchall()
    print(f"distance calc: pois={len(pois)} communities={len(comms)}", flush=True)
    cnt = 0
    for p in pois:
        wlng, wlat = baidu.bd09_to_wgs84(p["lng"], p["lat"])
        for c in comms:
            if abs(c["lat"] - wlat) > 0.30 or abs(c["lng"] - wlng) > 0.40:
                continue
            dkm = baidu.haversine_km(wlng, wlat, c["lng"], c["lat"])
            if dkm <= a.max_dist_km:
                storage.upsert_community_poi(conn, c["id"], p["id"], round(dkm, 3))
                cnt += 1
    conn.commit()
    print(f"community_poi rows upserted={cnt}", flush=True)
    for row in conn.execute("SELECT type, COUNT(*) n FROM poi GROUP BY type"):
        print(f"  poi {row['type']}: {row['n']}")
    conn.close()


if __name__ == "__main__":
    main()
