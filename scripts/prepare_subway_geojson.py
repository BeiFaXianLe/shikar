"""把原始地铁线路/站点 JSON 适配成 shikar 可导入的 GeoJSON 数据集。

输入为两套（或合并的）GeoJSON ``FeatureCollection``：

* 站点集合：每个 ``Point`` 的 ``properties`` 至少含 ``name`` 与 ``pshort``
  （线路简称）；同一站点可能因换乘在数据中出现多次（同坐标、多线路），
  这里按坐标合并，汇总其所属线路。
* 线路集合：每个 ``LineString`` / ``MultiLineString`` 的 ``properties`` 含
  ``name`` 与 ``short``（线路简称）。

输出统一为 shikar ``subway.import_geojson`` 需要的单一 FeatureCollection：

* 线路 feature：``properties`` 含 ``id``(城市+线路简称稳定), ``city``, ``name``；
* 站点 feature：``properties`` 含 ``id``(城市+坐标稳定), ``city``, ``name``,
  ``line_ids``（该站点所属线路 id 列表）。

原始数据坐标系为 **WGS84**（上游工具已用 gcoord 从高德 GCJ02 纠偏到 WGS84）；
本脚本在输出里显式写出 ``coord_system: wgs84``，导入时按显式坐标系计算距离。

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
from pathlib import Path


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
) -> dict:
    """把站点/线路原始 JSON 适配为单一 GeoJSON FeatureCollection。

    :param station_paths: 站点原始 GeoJSON 文件路径列表。
    :param line_paths: 线路原始 GeoJSON 文件路径列表。
    :param city_map: 文件名前缀到城市名的映射。
    :param source: 数据来源标识。
    :param version: 数据版本标识。
    :return: shikar 导入用的 FeatureCollection 字典。
    """
    # 线路：先建立「城市 + 线路简称 -> 线路 id」。
    features: list[dict] = []
    line_id_by_short: dict[tuple[str, str], str] = {}
    for path in line_paths:
        city = _city_of(path, city_map)
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for feat in data["features"]:
            props = feat["properties"]
            short = props.get("short") or props.get("pshort") or props["name"]
            lid = f"{city}:{short}"
            line_id_by_short[(city, short)] = lid
            geometry = feat["geometry"]
            # 统一为 MultiLineString 语义，保留原始坐标（WGS84）。
            features.append(
                {
                    "type": "Feature",
                    "geometry": geometry,
                    "properties": {
                        "id": lid,
                        "city": city,
                        "name": props["name"],
                    },
                }
            )

    # 站点：按 (城市, 经纬度四舍五入) 合并换乘站，汇总线路 id。
    merged: dict[tuple[str, float, float], dict] = {}
    for path in station_paths:
        city = _city_of(path, city_map)
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for feat in data["features"]:
            props = feat["properties"]
            short = props.get("pshort") or props.get("short")
            lng, lat = feat["geometry"]["coordinates"]
            key = (city, round(lng, 6), round(lat, 6))
            lid = line_id_by_short.get((city, short))
            entry = merged.setdefault(
                key,
                {
                    "name": props["name"],
                    "lng": lng,
                    "lat": lat,
                    "line_ids": [],
                },
            )
            if lid and lid not in entry["line_ids"]:
                entry["line_ids"].append(lid)

    for (city, lng, lat), entry in merged.items():
        if not entry["line_ids"]:
            # 站点不属于任何已知线路：跳过，避免导入期「站点缺 line_ids」。
            continue
        sid = f"{city}:{lng:.6f},{lat:.6f}"
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [entry["lng"], entry["lat"]]},
                "properties": {
                    "id": sid,
                    "city": city,
                    "name": entry["name"],
                    "line_ids": entry["line_ids"],
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
    )
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    lines = sum(1 for f in payload["features"] if f["geometry"]["type"] != "Point")
    stations = sum(1 for f in payload["features"] if f["geometry"]["type"] == "Point")
    print(
        json.dumps(
            {"out": args.out, "lines": lines, "stations": stations}, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
