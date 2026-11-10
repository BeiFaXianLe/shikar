"""百度地图 Web 服务客户端（纯标准库）：POI 检索 / 地点联想 / 驾车 / 公交，及坐标转换。
AK 从环境变量 ``BAIDU_AK`` 或仓库根 ``.secrets/baidu_ak.txt`` 读取（不入库）。
"""
from __future__ import annotations

import json
import math
import os
import urllib.parse
import urllib.request

BASE = "https://api.map.baidu.com"
_PI = math.pi
_A = 6378245.0
_EE = 0.00669342162296594323
_X_PI = _PI * 3000.0 / 180.0


def repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def read_ak() -> str:
    ak = os.environ.get("BAIDU_AK", "").strip()
    if ak:
        return ak
    p = os.path.join(repo_root(), ".secrets", "baidu_ak.txt")
    if os.path.isfile(p):
        return open(p, encoding="utf-8").read().strip()
    raise RuntimeError("未配置百度 AK：请设置环境变量 BAIDU_AK 或写入 .secrets/baidu_ak.txt")


def _get(path: str, params: dict) -> dict:
    params = dict(params)
    params["ak"] = read_ak()
    params.setdefault("output", "json")
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=20) as r:
        data = json.load(r)
    if int(data.get("status", -1)) != 0:
        raise RuntimeError(f"百度接口错误 status={data.get('status')} msg={data.get('message')}")
    return data


# ------------------------------ 坐标转换 ------------------------------
def _out_of_china(lng: float, lat: float) -> bool:
    return not (73.66 < lng < 135.05 and 3.86 < lat < 53.55)


def _trans_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * _PI) + 20.0 * math.sin(2.0 * x * _PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * _PI) + 40.0 * math.sin(y / 3.0 * _PI)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * _PI) + 320 * math.sin(y * _PI / 30.0)) * 2.0 / 3.0
    return ret


def _trans_lng(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * _PI) + 20.0 * math.sin(2.0 * x * _PI)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * _PI) + 40.0 * math.sin(x / 3.0 * _PI)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * _PI) + 300.0 * math.sin(x / 30.0 * _PI)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lng: float, lat: float) -> tuple[float, float]:
    if _out_of_china(lng, lat):
        return lng, lat
    dlat = _trans_lat(lng - 105.0, lat - 35.0)
    dlng = _trans_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * _PI
    magic = math.sin(radlat)
    magic = 1 - _EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * _PI)
    dlng = (dlng * 180.0) / (_A / sqrtmagic * math.cos(radlat) * _PI)
    return lng + dlng, lat + dlat


def gcj02_to_bd09(lng: float, lat: float) -> tuple[float, float]:
    z = math.sqrt(lng * lng + lat * lat) + 0.00002 * math.sin(lat * _X_PI)
    theta = math.atan2(lat, lng) + 0.000003 * math.cos(lng * _X_PI)
    return z * math.cos(theta) + 0.0065, z * math.sin(theta) + 0.006


def bd09_to_gcj02(lng: float, lat: float) -> tuple[float, float]:
    x = lng - 0.0065
    y = lat - 0.006
    z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * _X_PI)
    theta = math.atan2(y, x) - 0.000003 * math.cos(x * _X_PI)
    return z * math.cos(theta), z * math.sin(theta)


def gcj02_to_wgs84(lng: float, lat: float) -> tuple[float, float]:
    if _out_of_china(lng, lat):
        return lng, lat
    dlat = _trans_lat(lng - 105.0, lat - 35.0)
    dlng = _trans_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * _PI
    magic = math.sin(radlat)
    magic = 1 - _EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * _PI)
    dlng = (dlng * 180.0) / (_A / sqrtmagic * math.cos(radlat) * _PI)
    return lng - dlng, lat - dlat


def bd09_to_wgs84(lng: float, lat: float) -> tuple[float, float]:
    g = bd09_to_gcj02(lng, lat)
    return gcj02_to_wgs84(g[0], g[1])


def wgs84_to_bd09(lng: float, lat: float) -> tuple[float, float]:
    g = wgs84_to_gcj02(lng, lat)
    return gcj02_to_bd09(g[0], g[1])


def haversine_km(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ------------------------------ API ------------------------------
def place_search(query: str, region: str, tag: str | None = None,
                 page_size: int = 20, max_pages: int = 10) -> list[dict]:
    """地点检索（scope=2 详情版）。返回扁平 POI 列表（BD09 坐标）。"""
    out: list[dict] = []
    total = None
    for page in range(max_pages):
        params = {"query": query, "region": region, "scope": 2,
                  "page_size": page_size, "page_num": page}
        if tag:
            params["tag"] = tag
        data = _get("/place/v2/search", params)
        if total is None:
            total = int(data.get("total", 0))
        results = data.get("results") or []
        if not results:
            break
        for r in results:
            loc = r.get("location") or {}
            di = r.get("detail_info") or {}
            out.append({
                "uid": r.get("uid"),
                "name": r.get("name"),
                "lat": loc.get("lat"), "lng": loc.get("lng"),
                "address": r.get("address"),
                "district": r.get("area"),
                "tag": di.get("tag") or di.get("classified_poi_tag"),
                "type": di.get("type"),
            })
        if total is not None and len(out) >= total:
            break
    return out


def suggestion(query: str, region: str = "全国") -> list[dict]:
    data = _get("/place/v2/suggestion", {"query": query, "region": region})
    out = []
    for r in data.get("result") or []:
        loc = r.get("location") or {}
        out.append({"name": r.get("name"), "lat": loc.get("lat"), "lng": loc.get("lng"),
                    "address": r.get("address"), "district": r.get("district")})
    return out


def driving(origin: tuple[float, float], dest: tuple[float, float]) -> dict:
    """origin/dest = (lat, lng)。返回 {duration_s, distance_m}。"""
    data = _get("/directionlite/v1/driving", {"origin": f"{origin[0]},{origin[1]}", "destination": f"{dest[0]},{dest[1]}"})
    route = data["result"]["routes"][0]
    return {"duration_s": route.get("duration"), "distance_m": route.get("distance")}


def transit(origin: tuple[float, float], dest: tuple[float, float]) -> dict:
    data = _get("/directionlite/v1/transit", {"origin": f"{origin[0]},{origin[1]}", "destination": f"{dest[0]},{dest[1]}"})
    route = data["result"]["routes"][0]
    return {"duration_s": route.get("duration"), "distance_m": route.get("distance"), "price": route.get("price")}
