"""P1 新增 HTTP 接口测试：``/api/subway/lines`` 与目录地铁/价格筛选参数。

用后台真实服务 + ``http.client``，与 ``test_catalog.py`` 相同的轻量启动方式；
使用人工 WGS84 夹具，不依赖现实地铁数据。
"""

from __future__ import annotations

import http.client
import json
import sqlite3
import threading
from typing import Iterator

import pytest

from shikar import subway
from shikar.api import create_server
from shikar.storage import init_db, upsert_metric, upsert_xiaoqu

_GEOJSON = {
    "type": "FeatureCollection", "source": "t", "version": "v1", "coord_system": "wgs84",
    "features": [
        {"type": "Feature", "properties": {"id": "a", "city": "北京", "name": "甲线"},
         "geometry": {"type": "LineString", "coordinates": [[116.0, 40.0], [116.02, 40.0]]}},
        {"type": "Feature", "properties": {"id": "s1", "city": "北京", "name": "甲站", "line_ids": ["a"]},
         "geometry": {"type": "Point", "coordinates": [116.0, 40.0]}},
        {"type": "Feature", "properties": {"id": "s2", "city": "北京", "name": "乙站", "line_ids": ["a"]},
         "geometry": {"type": "Point", "coordinates": [116.02, 40.0]}},
    ],
}


def _conn(with_subway: bool) -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    init_db(c)
    for xid, lng, lat, price in [("x1", 116.0, 40.0, 60000.0), ("x2", 116.02, 40.0, 90000.0)]:
        upsert_xiaoqu(c, {"id": xid, "name": xid, "city": "北京", "district": "海淀",
                          "plate": "p", "alias": None, "lng": lng, "lat": lat,
                          "build_year": None, "households": None})
        upsert_metric(c, {"xiaoqu_id": xid, "week": "2026-W46", "price": price,
                          "mom4": None, "mom13": None, "supply": None, "vol30": None,
                          "absorb": None, "bargain": None, "heat": None,
                          "temperature": None, "stage": None})
    if with_subway:
        subway.import_geojson(c, _GEOJSON, community_crs="wgs84")
    return c


@pytest.fixture()
def server_with_subway() -> Iterator[dict]:
    c = _conn(True)
    server = create_server(c, host="127.0.0.1", port=0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield {"host": server.server_address[0], "port": server.server_address[1]}
    finally:
        server.shutdown(); server.server_close(); t.join(timeout=5); c.close()


@pytest.fixture()
def server_no_subway() -> Iterator[dict]:
    c = _conn(False)
    server = create_server(c, host="127.0.0.1", port=0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield {"host": server.server_address[0], "port": server.server_address[1]}
    finally:
        server.shutdown(); server.server_close(); t.join(timeout=5); c.close()


def _get(server: dict, path: str):
    client = http.client.HTTPConnection(server["host"], server["port"], timeout=5)
    try:
        client.request("GET", path)
        resp = client.getresponse()
        return resp.status, json.loads(resp.read().decode("utf-8"))
    finally:
        client.close()


def test_subway_lines_endpoint(server_with_subway):
    status, payload = _get(server_with_subway, "/api/subway/lines?city=%E5%8C%97%E4%BA%AC")
    assert status == 200
    assert {row["id"] for row in payload["items"]} == {"a"}
    assert payload["items"][0]["stations"][0]["name"] in {"甲站", "乙站"}


def test_subway_lines_no_data(server_no_subway):
    status, payload = _get(server_no_subway, "/api/subway/lines?city=%E5%8C%97%E4%BA%AC")
    assert status == 200
    assert payload["items"] == []


def test_catalog_line_filter_http(server_with_subway):
    # x1 在甲站、x2 在乙站，均在线 a 且距离 0 → 两个都命中。
    status, payload = _get(server_with_subway, "/api/catalog?city=%E5%8C%97%E4%BA%AC&line=a&subway_max=0.5")
    assert status == 200
    assert payload["total"] == 2
    assert all(it["features"]["subway"]["line_id"] == "a" for it in payload["items"])


def test_catalog_price_filter_http(server_with_subway):
    status, payload = _get(server_with_subway, "/api/catalog?price_min=80000&price_max=100000")
    assert status == 200
    assert [it["id"] for it in payload["items"]] == ["x2"]


def test_catalog_bad_subway_param_returns_400(server_with_subway):
    status, payload = _get(server_with_subway, "/api/catalog?line=a&subway_max=99")
    assert status == 400
    assert "error" in payload


def test_catalog_subway_filter_without_data_returns_400(server_no_subway):
    status, payload = _get(server_no_subway, "/api/catalog?line=a&subway_max=1")
    assert status == 400
    assert "地铁" in payload["error"]


def test_catalog_regression_plain(server_with_subway):
    """不带地铁/价格参数时，目录仍返回全部小区（兼容旧行为）。"""
    status, payload = _get(server_with_subway, "/api/catalog")
    assert status == 200
    assert payload["total"] == 2
    assert all("features" in it for it in payload["items"])


# --------------------------------------------------------------------------- #
# P2-01：非法数字参数必须 400（不能静默忽略）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("param", ["price_min", "price_max", "subway_max"])
def test_catalog_invalid_number_returns_400(server_with_subway, param):
    status, payload = _get(server_with_subway, f"/api/catalog?{param}=abc")
    assert status == 400
    assert "error" in payload


# --------------------------------------------------------------------------- #
# P2-02：未支持的 POI 参数必须明确 400，不静默返回全部
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("param", ["stage=primary", "school_max=2", "grade=%E4%B8%89%E7%94%B2", "hospital_max=3", "hsr_max=5"])
def test_catalog_unsupported_poi_param_returns_400(server_with_subway, param):
    status, payload = _get(server_with_subway, f"/api/catalog?{param}")
    assert status == 400
    assert "不支持" in payload["error"]


# --------------------------------------------------------------------------- #
# P1-03：价格期次口径 —— 每条带期次与当前/历史标记，可按期次限定
# --------------------------------------------------------------------------- #
def _conn_periods() -> sqlite3.Connection:
    """两个小区：x_cur 最新为 2026-W46，x_old 最新为 2019-02。"""
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    init_db(c)
    for xid, price, week in [("x_cur", 90000.0, "2026-W46"), ("x_old", 30000.0, "2019-02")]:
        upsert_xiaoqu(c, {"id": xid, "name": xid, "city": "北京", "district": "海淀",
                          "plate": "p", "alias": None, "lng": None, "lat": None,
                          "build_year": None, "households": None})
        upsert_metric(c, {"xiaoqu_id": xid, "week": week, "price": price, "mom4": None,
                          "mom13": None, "supply": None, "vol30": None, "absorb": None,
                          "bargain": None, "heat": None, "temperature": None, "stage": None})
    return c


@pytest.fixture()
def server_periods() -> Iterator[dict]:
    c = _conn_periods()
    server = create_server(c, host="127.0.0.1", port=0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield {"host": server.server_address[0], "port": server.server_address[1]}
    finally:
        server.shutdown(); server.server_close(); t.join(timeout=5); c.close()


def test_catalog_price_period_flags(server_periods):
    status, payload = _get(server_periods, "/api/catalog")
    assert status == 200
    items = {it["id"]: it for it in payload["items"]}
    assert items["x_cur"]["price_is_current"] is True
    assert items["x_old"]["price_is_current"] is False
    assert items["x_old"]["latest_week"] == "2019-02"
    # 期次分布包含两期。
    weeks = {p["week"] for p in payload["price_periods"]}
    assert {"2026-W46", "2019-02"} <= weeks


def test_catalog_price_week_restricts_to_period(server_periods):
    # 限定 2026-W46：只返回当期小区，价格过滤不再与 2019 混比。
    status, payload = _get(server_periods, "/api/catalog?price_week=2026-W46")
    assert status == 200
    assert [it["id"] for it in payload["items"]] == ["x_cur"]
