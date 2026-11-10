"""把原始地铁线路/站点 JSON 适配成 shikar 可导入的 GeoJSON 数据集。

**问题背景**：上游公开数据集的「站点文件」在导出时按坐标把每个物理站点去重为
一条 Point，且只保留一个 ``pshort``（线路简称），**丢失了换乘站与多条线路的对应
关系**（如西直门只剩 2 号线、国贸只剩 1 号线、天津站只剩 2 号线），直接导入会导致
「按线路筛选」漏查。见 PR #24 审核 P1-01。

**本适配器的解决方式**：站点与线路都来自同一份上游几何——地铁站坐标本就落在其
所属线路的折线上。适配器对每个站点，在**同城市所有线路折线**中找出距离 ≤ 阈值的
线路，作为该站点所属线路集合；因此换乘站会自然关联到其经过的多条线路。用 50m 阈值
（真实换乘站到所属折线距离 ≤20m，非所属线路通常 >200m，分离清晰；见测试
``test_prepare_subway.py::test_transfer_reconstruction_real`` 依据的实测分布）。

输入为两套 GeoJSON ``FeatureCollection``：

* 站点集合：每个 ``Point`` 的 ``properties`` 至少含 ``name``；``pshort`` 可选（仅作
  兜底，若几何匹配不到任何线路时才使用）。同一站点若已在数据中出现多次（不同坐标），
  会先按坐标四舍五入合并。
* 线路集合：每个 ``LineString`` / ``MultiLineString`` 的 ``properties`` 含
  ``name`` 与 ``short``（线路简称）。

输出统一为 shikar ``subway.import_geojson`` 需要的单一 FeatureCollection：

* 线路 feature：``properties`` 含 ``id``(城市+线路简称稳定), ``city``, ``name``；
* 站点 feature：``properties`` 含 ``id``(城市+坐标稳定), ``city``, ``name``,
  ``line_ids``（该站点所属线路 id 列表，含全部换乘线路）。

原始数据坐标系为 **WGS84**（上游工具已用 gcoord 从高德 GCJ02 纠偏到 WGS84）；本脚本
在输出里显式写出 ``coord_system: wgs84``，导入时按显式坐标系计算距离。

用法::

    python scripts/prepare_subway_geojson.py \
        --stations data/subway/beijing-station.json,data/subway/tianjin-station.json \
        --lines data/subway/beijing-line.json,data/subway/tianjin-line.json \
        --city-map beijing=北京,tianjin=天津 \
        --out data/subway/shikar-subway.json
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

#: 站点到线路折线的归属阈值（km）。真实换乘站到所属线路 ≤~20m；非所属线路通常
#: 远大于 200m。取 50m 兼顾精度与召回，且不会被并行/邻近线路误配。
STATION_LINE_TOLERANCE_KM = 0.05

#: 地球平均半径（km），与 ``shikar.subway.EARTH_KM`` 一致，避免导入脚本额外依赖。
_EARTH_KM = 6371.0088


def _point_segment_km(point, a, b) -> float:
    """点到线段的近似距离（局部等距投影，km）。"""
    scale = _EARTH_KM * math.pi / 180.0
    cosine = math.cos(math.radians(point[1]))
    ax, ay = (a[0] - point[0]) * scale * cosine, (a[1] - point[1]) * scale
    bx, by = (b[0] - point[0]) * scale * cosine, (b[1] - point[1]) * scale
    dx, dy = bx - ax, by - ay
    norm = dx * dx + dy * dy
    t = max(0.0, min(1.0, -(ax * dx + ay * dy) / norm)) if norm else 0.0
    return math.hypot(ax + t * dx, ay + t * dy)


def _polyline_km(point, segments) -> float:
    """点到多段折线最短距离（km），段间不连通。"""
    best = math.inf
    for segment in segments:
        for a, b in zip(segment, segment[1:]):
            best = min(best, _point_segment_km(point, a, b))
    return best


def _city_of(path: str, city_map: dict[str, str]) -> str:
    """按文件名前缀（如 ``beijing``）映射到中文城市名。"""
    stem = Path(path).name
    for key, city in city_map.items():
        if stem.startswith(key):
            return city
    raise SystemExit(f"无法从文件名推断城市：{path}（请补充 --city-map）")


def prepare(
    station_paths: list[str],
    line_paths: list[str],
    city_map: dict[str, str],
    source: str,
    version: str,
    tolerance_km: float = STATION_LINE_TOLERANCE_KM,
) -> dict:
    """把站点/线路原始 JSON 适配为单一 GeoJSON FeatureCollection（含换乘关系）。

    :param station_paths: 站点原始 GeoJSON 文件路径列表。
    :param line_paths: 线路原始 GeoJSON 文件路径列表。
    :param city_map: 文件名前缀到城市名的映射。
    :param source: 数据来源标识。
    :param version: 数据版本标识。
    :param tolerance_km: 站点归属线路的折线距离阈值（km）。
    :return: shikar 导入用的 FeatureCollection 字典。
    """
    features: list[dict] = []
    line_id_by_short: dict[tuple[str, str], str] = {}
    #: 城市 -> [(线路 id, 折线段列表)]
    city_lines: dict[str, list[tuple[str, list[list[tuple[float, float]]]]]] = {}

    for path in line_paths:
        city = _city_of(path, city_map)
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for feat in data["features"]:
            props = feat["properties"]
            short = props.get("short") or props.get("pshort") or props["name"]
            lid = f"{city}:{short}"
            line_id_by_short[(city, short)] = lid
            geometry = feat["geometry"]
            coords = geometry["coordinates"]
            segments = coords if geometry["type"] == "MultiLineString" else [coords]
            city_lines.setdefault(city, []).append(
                (lid, [[tuple(p) for p in seg] for seg in segments])
            )
            features.append(
                {
                    "type": "Feature",
                    "geometry": geometry,
                    "properties": {"id": lid, "city": city, "name": props["name"]},
                }
            )

    # 站点：先按 (城市, 经纬度四舍五入) 合并换乘站，再几何匹配所属线路。
    merged: dict[tuple[str, float, float], dict] = {}
    for path in station_paths:
        city = _city_of(path, city_map)
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for feat in data["features"]:
            props = feat["properties"]
            lng, lat = feat["geometry"]["coordinates"]
            key = (city, round(lng, 6), round(lat, 6))
            entry = merged.setdefault(
                key, {"name": props["name"], "city": city, "lng": lng, "lat": lat,
                      "fallback_short": props.get("pshort") or props.get("short")}
            )
            entry["fallback_short"] = entry.get("fallback_short") or (
                props.get("pshort") or props.get("short")
            )

    for (city, lng, lat), entry in merged.items():
        point = (entry["lng"], entry["lat"])
        line_ids: list[str] = []
        for lid, segments in city_lines.get(city, []):
            if _polyline_km(point, segments) <= tolerance_km:
                line_ids.append(lid)
        if not line_ids and entry.get("fallback_short"):
            # 几何未命中（罕见：站点坐标偏离折线）时退回原始 pshort 单线路，避免丢站。
            lid = line_id_by_short.get((city, entry["fallback_short"]))
            if lid:
                line_ids.append(lid)
        if not line_ids:
            continue  # 完全无法归属的站点跳过（不伪造线路）。
        sid = f"{city}:{lng:.6f},{lat:.6f}"
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [entry["lng"], entry["lat"]]},
                "properties": {
                    "id": sid,
                    "city": city,
                    "name": entry["name"],
                    "line_ids": sorted(line_ids),
                },
            }
        )

    return {
        "type": "FeatureCollection",
        "source": source,
        "version": version,
        "coord_system": "wgs84",
        "features": features,
    }


def main() -> None:
    """命令行入口：读取原始文件、适配并写出单一 GeoJSON。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stations", required=True, help="逗号分隔的站点原始 JSON 路径")
    parser.add_argument("--lines", required=True, help="逗号分隔的线路原始 JSON 路径")
    parser.add_argument(
        "--city-map", required=True, help="文件名前缀到城市，如 beijing=北京,tianjin=天津"
    )
    parser.add_argument("--source", default="generate-china-subways-geojson(WGS84)")
    parser.add_argument("--version", default="2026-11")
    parser.add_argument("--tolerance-km", type=float, default=STATION_LINE_TOLERANCE_KM)
    parser.add_argument("--out", required=True, help="输出 GeoJSON 路径")
    args = parser.parse_args()

    city_map: dict[str, str] = {}
    for pair in args.city_map.split(","):
        key, _, city = pair.partition("=")
        city_map[key.strip()] = city.strip()

    payload = prepare(
        args.stations.split(","),
        args.lines.split(","),
        city_map,
        source=args.source,
        version=args.version,
        tolerance_km=args.tolerance_km,
    )
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    line_count = sum(1 for f in payload["features"] if f["geometry"]["type"] != "Point")
    points = [f for f in payload["features"] if f["geometry"]["type"] == "Point"]
    transfer = sum(1 for p in points if len(p["properties"]["line_ids"]) > 1)
    print(
        json.dumps(
            {
                "out": args.out,
                "lines": line_count,
                "stations": len(points),
                "transfer_stations": transfer,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
