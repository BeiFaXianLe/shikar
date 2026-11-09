"""``scripts/prepare_subway_geojson.py`` 适配层测试（不联网，用临时小样本）。

验证：把「站点/线路原始 GeoJSON」适配为 shikar 导入格式——线路 feature 带
``id``/``city``/``name``，站点 feature 按坐标合并换乘站并汇总 ``line_ids``，
输出显式 ``coord_system: wgs84``，且可被 ``subway.import_geojson`` 直接导入。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3

from shikar import storage, subway

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_module():
    """按文件路径加载 ``scripts/prepare_subway_geojson.py``。"""
    path = os.path.join(_REPO_ROOT, "scripts", "prepare_subway_geojson.py")
    spec = importlib.util.spec_from_file_location("prepare_subway_geojson", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_STATIONS = {
    "type": "FeatureCollection",
    "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [116.0, 40.0]},
         "properties": {"name": "换乘站", "pshort": "1号线"}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [116.0, 40.0]},
         "properties": {"name": "换乘站", "pshort": "2号线"}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [116.02, 40.0]},
         "properties": {"name": "终点站", "pshort": "1号线"}},
    ],
}
_LINES = {
    "type": "FeatureCollection",
    "features": [
        {"type": "Feature", "geometry": {"type": "LineString",
         "coordinates": [[116.0, 40.0], [116.02, 40.0]]},
         "properties": {"name": "地铁1号线", "short": "1号线"}},
        {"type": "Feature", "geometry": {"type": "LineString",
         "coordinates": [[116.0, 40.0], [116.0, 40.02]]},
         "properties": {"name": "地铁2号线", "short": "2号线"}},
    ],
}


def test_prepare_merges_transfer_and_imports(tmp_path):
    mod = _load_module()
    # 写出临时原始文件（文件名前缀用于城市推断）。
    station_path = tmp_path / "beijing-station.json"
    line_path = tmp_path / "beijing-line.json"
    station_path.write_text(json.dumps(_STATIONS, ensure_ascii=False), encoding="utf-8")
    line_path.write_text(json.dumps(_LINES, ensure_ascii=False), encoding="utf-8")

    payload = mod.prepare(
        [str(station_path)], [str(line_path)], {"beijing": "北京"}, source="t", version="v1"
    )
    assert payload["coord_system"] == "wgs84"
    points = [f for f in payload["features"] if f["geometry"]["type"] == "Point"]
    lines = [f for f in payload["features"] if f["geometry"]["type"] != "Point"]
    # 换乘站(同坐标)合并为一个点，两个线路各一个线 feature。
    assert len(lines) == 2
    assert len(points) == 2
    transfer = next(p for p in points if p["properties"]["name"] == "换乘站")
    assert set(transfer["properties"]["line_ids"]) == {"北京:1号线", "北京:2号线"}

    # 可被 import_geojson 直接导入。
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    storage.init_db(c)
    storage.upsert_xiaoqu(c, {"id": "x", "name": "x", "city": "北京", "district": "d",
                              "lng": 116.0, "lat": 40.0})
    report = subway.import_geojson(c, payload, community_crs="wgs84")
    assert report["lines"] == 2 and report["stations"] == 2
    c.close()


def test_ensure_subway_builds_once(tmp_path):
    """``main.ensure_subway`` 缺地图表时构建一次，已有表时跳过。"""
    import json as _json

    from shikar import main as main_mod

    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    storage.init_db(c)
    storage.upsert_xiaoqu(c, {"id": "x", "name": "x", "city": "北京", "district": "d",
                              "lng": 116.0, "lat": 40.0})
    mod = _load_module()
    payload = mod.prepare(
        [str(_write(tmp_path / "beijing-station.json", _STATIONS))],
        [str(_write(tmp_path / "beijing-line.json", _LINES))],
        {"beijing": "北京"}, source="t", version="v1",
    )
    geo = tmp_path / "shikar-subway.json"
    geo.write_text(_json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    r1 = main_mod.ensure_subway(c, str(geo), community_crs="wgs84")
    assert r1 and r1["lines"] == 2
    # 第二次已有表 → 跳过（返回 None）。
    assert main_mod.ensure_subway(c, str(geo), community_crs="wgs84") is None
    c.close()


def _write(path, obj):
    """把对象写入 UTF-8 JSON 文件并返回路径。"""
    import json as _json
    path.write_text(_json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return path
