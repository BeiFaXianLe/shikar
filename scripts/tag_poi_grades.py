# -*- coding: utf-8 -*-
"""用百度 POI 分类标签给已有 OSM 学校/医院补标等级（三甲/二甲、重点/普通），免费/低配额。

用法：python scripts/tag_poi_grades.py
"""
from __future__ import annotations

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
from shikar import baidu, storage  # noqa: E402

CITIES = ["北京", "天津"]
HOSP_TAGS = [("三甲", "医疗;三级甲等医院"), ("二甲", "医疗;二级甲等医院")]
SCHOOL_TAGS = [("primary", "教育培训;重点小学"), ("middle", "教育培训;重点中学"), ("high", "教育培训;重点高中")]


def norm(name: str) -> str:
    n = (name or "").strip()
    for p in ("北京市", "天津市", "北京", "天津"):
        if n.startswith(p):
            n = n[len(p):]
    return n.replace(" ", "").replace("（", "(").replace("）", ")").strip()


def collect(q, tag, region):
    try:
        return [it["name"] for it in baidu.place_search(q, region, tag=tag, max_pages=6) if it.get("name")]
    except Exception as e:
        print("  tag ERR", tag, e, flush=True)
        return []


def main():
    db = os.path.join(ROOT, "data", "shikar.db")
    conn = sqlite3.connect(db, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    storage.init_db(conn)

    for city in CITIES:
        print(f"[{city}]", flush=True)
        hosp_map = {}
        for grade, tag in HOSP_TAGS:
            names = collect("医院", tag, city)
            for nm in names:
                hosp_map.setdefault(norm(nm), grade)
            print(f"  hospital tag {grade}: {len(names)} names", flush=True)
        sch_maps = {}
        for sub, tag in SCHOOL_TAGS:
            q = {"primary": "小学", "middle": "初中", "high": "高中"}[sub]
            names = collect(q, tag, city)
            sch_maps[sub] = {norm(nm) for nm in names}
            print(f"  school tag {sub}: {len(names)} names", flush=True)

        pois = conn.execute("SELECT id,type,subtype,name,grade FROM poi WHERE city=?", (city,)).fetchall()
        upd_h = upd_s = 0
        for p in pois:
            nm = norm(p["name"])
            if p["type"] == "hospital":
                g = hosp_map.get(nm)
                if not g:
                    for k, v in hosp_map.items():
                        if len(k) >= 4 and (k in nm or nm in k):
                            g = v; break
                if g and g != p["grade"]:
                    conn.execute("UPDATE poi SET grade=? WHERE id=?", (g, p["id"]))
                    upd_h += 1
            elif p["type"] == "school":
                key = p["subtype"] if p["subtype"] in sch_maps else None
                target = sch_maps.get(key) if key else None
                hit = False
                if target:
                    if nm in target:
                        hit = True
                    else:
                        for k in target:
                            if len(k) >= 4 and (k in nm or nm in k):
                                hit = True; break
                if hit and p["grade"] != "重点":
                    conn.execute("UPDATE poi SET grade='重点' WHERE id=?", (p["id"],))
                    upd_s += 1
        conn.commit()
        n_school = conn.execute("SELECT COUNT(*) FROM poi WHERE city=? AND type='school'", (city,)).fetchone()[0]
        n_key = conn.execute("SELECT COUNT(*) FROM poi WHERE city=? AND type='school' AND grade='重点'", (city,)).fetchone()[0]
        n_sj = conn.execute("SELECT COUNT(*) FROM poi WHERE city=? AND type='hospital' AND grade='三甲'", (city,)).fetchone()[0]
        n_ej = conn.execute("SELECT COUNT(*) FROM poi WHERE city=? AND type='hospital' AND grade='二甲'", (city,)).fetchone()[0]
        print(f"  updated hospital={upd_h} school_total={n_school} 重点={n_key}", flush=True)
        print(f"  hospital 三甲={n_sj} 二甲={n_ej}", flush=True)
    conn.close()


if __name__ == "__main__":
    main()
