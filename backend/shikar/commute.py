"""双点通勤：地点解析（百度 suggestion）+ 驾车时长矩阵（OSRM 公共实例，免费）+ 本地缓存。"""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.parse
import urllib.request

from . import baidu

OSRM = "https://router.project-osrm.org"
BATCH = 80  # OSRM table 单次坐标上限内，分批


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "shikar-commute/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.load(r)
    if data.get("code") not in (0, "Ok", None) and "durations" not in data:
        raise RuntimeError(f"OSRM 错误: {data.get('code')} {data.get('message')}")
    return data


def resolve_place(text: str, city: str | None = None) -> dict | None:
    """把地点名解析成 WGS84 坐标（百度 suggestion）。"""
    res = baidu.suggestion(text, region=city or "全国")
    if not res:
        return None
    r = res[0]
    lng, lat = baidu.bd09_to_wgs84(r["lng"], r["lat"])
    return {"name": r.get("name") or text, "lat": lat, "lng": lng}


def _table(a: tuple[float, float], b: tuple[float, float], dests: list[tuple[float, float]]):
    pts = [a, b] + dests
    coords = ";".join(f"{lng},{lat}" for (lat, lng) in pts)
    url = f"{OSRM}/table/v1/driving/{coords}?sources=0;1&annotations=duration"
    data = _get_json(url)
    d = data["durations"]
    return d[0][2:], d[1][2:]


def commute_for(conn: sqlite3.Connection, a: dict, b: dict, candidates: list[dict], mode: str = "driving"):
    """返回 {community_id: (t_a, t_b)}，带 SQLite 缓存。mode: driving(OSRM免费) / transit(百度)。"""
    if mode not in ("driving", "transit"):
        raise ValueError("mode 仅支持 driving / transit")
    key = f"{a['lat']:.5f},{a['lng']:.5f}|{b['lat']:.5f},{b['lng']:.5f}|{mode}"
    cached: dict[str, tuple] = {}
    for row in conn.execute("SELECT community_id,t_a,t_b FROM commute_cache WHERE key=?", (key,)).fetchall():
        cached[row["community_id"]] = (row["t_a"], row["t_b"])
    missing = [c for c in candidates if c["id"] not in cached]
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ")

    def save(cid, x, y):
        conn.execute(
            "INSERT INTO commute_cache(key,community_id,t_a,t_b,updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(key,community_id) DO UPDATE SET t_a=excluded.t_a,t_b=excluded.t_b,updated_at=excluded.updated_at",
            (key, cid, x, y, now),
        )
        cached[cid] = (x, y)

    if mode == "driving":
        for i in range(0, len(missing), BATCH):
            chunk = missing[i:i + BATCH]
            ta, tb = _table((a["lat"], a["lng"]), (b["lat"], b["lng"]),
                            [(c["lat"], c["lng"]) for c in chunk])
            for c, x, y in zip(chunk, ta, tb):
                save(c["id"], x, y)
    else:  # transit：百度公交（按次计费），逐个 OD 计算
        def bd(p):
            lng, lat = baidu.wgs84_to_bd09(p["lng"], p["lat"])
            return (lat, lng)
        pa, pb = bd(a), bd(b)
        for c in missing:
            pc = bd(c)
            try:
                ta = baidu.transit(pa, pc)["duration_s"]
                tb = baidu.transit(pb, pc)["duration_s"]
            except Exception:
                continue
            save(c["id"], ta, tb)
    conn.commit()
    return cached, key
