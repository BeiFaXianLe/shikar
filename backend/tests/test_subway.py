"""P1 地铁空间层（``shikar.subway``）与「目录地铁/价格筛选」测试。

全部使用**人工构造的 WGS84 测试夹具**（非现实地铁数据），覆盖：

* 球面距离 Haversine 已知值；
* 点到多段折线距离（含 MultiLineString 断段不连通）；
* 坐标系显式转换与非法坐标系拒绝；
* GeoJSON 导入：校验、原子性（失败保留旧数据）、幂等、缺坐标计数；
* ``filter_sql``：单线路、多线路 OR、指定站点、距线路口径、跨城市/非法线路；
* ``matching_features``：只返回命中线路、半径过滤；
* ``query_catalog`` 地铁 + 价格扩展：结果集、稳定排序、次行 features、400 级错误。
"""

from __future__ import annotations

import copy
import sqlite3

import pytest

from shikar import catalog, storage, subway


def _geojson() -> dict:
    """两条交叉线路 + 三站点的 WGS84 夹具（坐标仅用于测试，不是现实地铁）。

    线路 a（横线）连接站点 s1(116,40) 与 s2(116.02,40)；
    线路 b（竖线）经过站点 s3(116.01,40)。
    """
    return {
        "type": "FeatureCollection",
        "source": "test-fixture",
        "version": "v1",
        "coord_system": "wgs84",
        "features": [
            {"type": "Feature", "properties": {"id": "a", "city": "北京", "name": "甲线"},
             "geometry": {"type": "MultiLineString", "coordinates": [[[116.0, 40.0], [116.02, 40.0]]]}},
            {"type": "Feature", "properties": {"id": "b", "city": "北京", "name": "乙线"},
             "geometry": {"type": "LineString", "coordinates": [[116.01, 39.99], [116.01, 40.01]]}},
            {"type": "Feature", "properties": {"id": "s1", "city": "北京", "name": "甲站", "line_ids": ["a"]},
             "geometry": {"type": "Point", "coordinates": [116.0, 40.0]}},
            {"type": "Feature", "properties": {"id": "s2", "city": "北京", "name": "乙站", "line_ids": ["a"]},
             "geometry": {"type": "Point", "coordinates": [116.02, 40.0]}},
            {"type": "Feature", "properties": {"id": "s3", "city": "北京", "name": "丙站", "line_ids": ["b"]},
             "geometry": {"type": "Point", "coordinates": [116.01, 40.0]}},
        ],
    }


def _conn() -> sqlite3.Connection:
    """构造内存连接、建表并写入带坐标/缺坐标的小区。"""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    storage.init_db(c)
    rows = [
        # x1 在 s1 与 s3 之间；x2 就在 s1（0 距离）；x3 缺坐标。
        ("x1", 116.01, 40.0),
        ("x2", 116.0, 40.0),
        ("x3", None, None),
    ]
    for xid, lng, lat in rows:
        storage.upsert_xiaoqu(c, {
            "id": xid, "name": xid, "city": "北京", "district": "海淀",
            "plate": "p", "alias": None, "lng": lng, "lat": lat,
            "build_year": None, "households": None,
        })
        if lng is not None:
            storage.upsert_metric(c, {
                "xiaoqu_id": xid, "week": "2026-W46", "price": 60000.0 if xid == "x1" else 90000.0,
                "mom4": None, "mom13": None, "supply": None, "vol30": None,
                "absorb": None, "bargain": None, "heat": None,
                "temperature": None, "stage": None,
            })
    return c


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = _conn()
    subway.import_geojson(c, _geojson(), community_crs="wgs84")
    try:
        yield c
    finally:
        c.close()


# --------------------------------------------------------------------------- #
# 纯几何
# --------------------------------------------------------------------------- #
def test_haversine_known_and_zero():
    assert subway.haversine_km((0, 0), (0, 0)) == 0.0
    assert subway.haversine_km((0, 0), (0, 1)) == pytest.approx(111.195, abs=0.01)


def test_polyline_on_segment_and_disconnected():
    # 点在线段中点 → 距离约 0。
    assert subway.polyline_distance_km((116.01, 40.0), [[[116.0, 40.0], [116.02, 40.0]]]) < 1e-6
    # MultiLineString 两段之间断开，中点不应命中（不可虚构连接段）。
    assert subway.polyline_distance_km(
        (116.01, 40.0),
        [[[116.0, 40.0], [116.001, 40.0]], [[116.019, 40.0], [116.02, 40.0]]],
    ) > 0.7


def test_to_wgs84_rejects_unknown_crs():
    with pytest.raises(ValueError, match="坐标系"):
        subway.to_wgs84((116.0, 40.0), "unknown")


def test_to_wgs84_rejects_nonfinite():
    with pytest.raises(ValueError):
        subway.to_wgs84((float("nan"), 40.0), "wgs84")


def test_to_wgs84_gcj_roundtrip_offset():
    # GCJ02 点转回 WGS84 后应产生可观的偏移（约数百米），且不改变量级。
    wl, wa = subway.to_wgs84((116.404, 39.915), "gcj02")
    assert abs(wl - 116.404) > 1e-4
    assert 39.9 < wa < 39.93


# --------------------------------------------------------------------------- #
# 导入
# --------------------------------------------------------------------------- #
def test_import_counts_and_missing_coordinates(conn):
    # x1、x2 有坐标参与；x3 缺坐标。
    # community_subway：每有坐标小区 × 线路（a,b）=2 → 2 小区 × 2 = 4。
    assert conn.execute("select count(*) from community_subway").fetchone()[0] == 4
    assert conn.execute("select count(*) from subway_station").fetchone()[0] == 3
    report = subway.import_geojson(conn, _geojson(), community_crs="wgs84")
    assert report["communities_with_coordinates"] == 2
    assert report["communities_missing_coordinates"] == 1


def test_import_idempotent(conn):
    before = conn.execute("select count(*) from community_subway").fetchone()[0]
    subway.import_geojson(conn, _geojson(), community_crs="wgs84")
    assert conn.execute("select count(*) from community_subway").fetchone()[0] == before


def test_import_atomic_on_invalid_geometry(conn):
    good_stations = conn.execute("select count(*) from subway_station").fetchone()[0]
    bad = copy.deepcopy(_geojson())
    bad["features"][-1]["geometry"]["coordinates"] = [float("nan"), 40.0]
    with pytest.raises(ValueError):
        subway.import_geojson(conn, bad, community_crs="wgs84")
    # 旧数据未被破坏。
    assert conn.execute("select count(*) from subway_station").fetchone()[0] == good_stations


def test_import_rejects_empty():
    c = _conn()
    try:
        payload = _geojson()
        payload["features"] = []
        with pytest.raises(ValueError, match="空数据"):
            subway.import_geojson(c, payload, community_crs="wgs84")
    finally:
        c.close()


def test_list_lines_and_no_data_state():
    c = _conn()
    try:
        empty = subway.list_lines(c, city="北京")
        assert empty["items"] == [] and "尚未导入" in empty["message"]
    finally:
        c.close()
    c2 = _conn()
    try:
        subway.import_geojson(c2, _geojson(), community_crs="wgs84")
        lines = subway.list_lines(c2, city="北京")
        assert {row["id"] for row in lines["items"]} == {"a", "b"}
        assert len(next(r for r in lines["items"] if r["id"] == "a")["stations"]) == 2
    finally:
        c2.close()


# --------------------------------------------------------------------------- #
# filter_sql / matching_features
# --------------------------------------------------------------------------- #
def test_filter_sql_single_line(conn):
    sql, params = subway.filter_sql(conn, city="北京", lines=["a"], radius=0.5)
    ids = {r[0] for r in conn.execute(sql, params)}
    # 线 a 距站阈值 0.5km：x2 在 s1(0km) 命中；x1 距 s1/s2 均约 0.85km，不命中。
    assert "x2" in ids and "x1" not in ids


def test_filter_sql_multi_line_or(conn):
    sql, params = subway.filter_sql(conn, city="北京", lines=["a", "b"], radius=0.5)
    ids = {r[0] for r in conn.execute(sql, params)}
    # 线 b 的 s3 恰在 x1 位置，故 x1 因线 b 命中；x2 因线 a 命中。
    assert {"x1", "x2"}.issubset(ids)


def test_filter_sql_station_and_line_mode(conn):
    sql, params = subway.filter_sql(conn, city="北京", station="s3", radius=0.5)
    ids = {r[0] for r in conn.execute(sql, params)}
    assert ids == {"x1"}  # x1 就在 s3
    with pytest.raises(ValueError, match="距站"):
        subway.filter_sql(conn, city="北京", station="s3", mode="line")


def test_filter_sql_rejects_foreign_line(conn):
    with pytest.raises(ValueError, match="线路不存在"):
        subway.filter_sql(conn, city="北京", lines=["nonexistent"], radius=1.0)


def test_matching_features_only_matched_line(conn):
    feats = subway.matching_features(conn, ["x1"], lines=["b"], mode="station", radius=1.0)
    assert "x1" in feats
    assert feats["x1"]["line_id"] == "b"
    assert feats["x1"]["mode"] == "station"


# --------------------------------------------------------------------------- #
# query_catalog：地铁 + 价格扩展
# --------------------------------------------------------------------------- #
def test_catalog_subway_filter(conn):
    res = catalog.query_catalog(conn, city="北京", lines=["b"], subway_max=0.5)
    # 仅 x1 命中线 b 的 s3；x2 不命中线 b（距 s3 约 1.7km）。
    assert res["total"] == 1
    it = res["items"][0]
    assert it["id"] == "x1"
    assert it["features"]["subway"]["line_id"] == "b"


def test_catalog_price_filter(conn):
    res = catalog.query_catalog(conn, city="北京", price_min=80000, price_max=100000)
    assert [it["id"] for it in res["items"]] == ["x2"]


def test_catalog_subway_distance_sort(conn):
    # 线 a 距站 ≤1km：x2(0km) 与 x1(约0.85km) 命中，按距离升序 x2 在前。
    res = catalog.query_catalog(conn, city="北京", lines=["a"], subway_max=1.0, sort="subway_distance")
    ids = [it["id"] for it in res["items"]]
    assert ids.index("x2") < ids.index("x1")


def test_catalog_subway_without_data_raises():
    c = _conn()  # 未导入地铁
    try:
        with pytest.raises(ValueError, match="尚未导入地铁"):
            catalog.query_catalog(c, city="北京", lines=["a"], subway_max=1.0)
    finally:
        c.close()


def test_catalog_price_bounds_validation(conn):
    with pytest.raises(ValueError, match="不能超过"):
        catalog.query_catalog(conn, price_min=100, price_max=50)
    with pytest.raises(ValueError, match="地铁距离"):
        catalog.query_catalog(conn, lines=["a"], subway_max=99)


# --------------------------------------------------------------------------- #
# P1-01 换乘：同一站点关联多线路后可被任一所含线路检索到
# --------------------------------------------------------------------------- #
def _transfer_geojson() -> dict:
    """站点 s3(116.01,40) 同时属于线 b(竖线) 与线 c(经过该点的横线)。"""
    return {
        "type": "FeatureCollection", "source": "t", "version": "v1", "coord_system": "wgs84",
        "features": [
            {"type": "Feature", "properties": {"id": "b", "city": "北京", "name": "乙线"},
             "geometry": {"type": "LineString", "coordinates": [[116.01, 39.99], [116.01, 40.01]]}},
            {"type": "Feature", "properties": {"id": "c", "city": "北京", "name": "丙线"},
             "geometry": {"type": "LineString", "coordinates": [[116.0, 40.0], [116.02, 40.0]]}},
            {"type": "Feature", "properties": {"id": "s3", "city": "北京", "name": "丙站",
                                               "line_ids": ["b", "c"]},
             "geometry": {"type": "Point", "coordinates": [116.01, 40.0]}},
        ],
    }


def test_transfer_station_matched_by_each_line():
    c = _conn()
    try:
        storage.upsert_xiaoqu(c, {"id": "xt", "name": "xt", "city": "北京", "district": "海淀",
                                  "plate": "p", "alias": None, "lng": 116.01, "lat": 40.0,
                                  "build_year": None, "households": None})
        subway.import_geojson(c, _transfer_geojson(), community_crs="wgs84")
        # 用线 b 或线 c 过滤都应命中 xt（换乘站两线都在 0 距离内）。
        for lid in ("b", "c"):
            sql, params = subway.filter_sql(c, city="北京", lines=[lid], radius=0.2)
            assert "xt" in {r[0] for r in c.execute(sql, params)}, f"line {lid} 漏查换乘站"
        # 指定站点 s3 也能命中。
        sql, params = subway.filter_sql(c, city="北京", station="s3", radius=0.2)
        assert "xt" in {r[0] for r in c.execute(sql, params)}
    finally:
        c.close()


# --------------------------------------------------------------------------- #
# P2-07 阈值边界：恰好等于阈值应命中（<=），略超则不命中
# --------------------------------------------------------------------------- #
def test_threshold_boundary_inclusive(conn):
    # x1 到线 b 的 s3 距离为 0；构造一个恰在阈值上的点难以精确，改用 station 模式：
    sql, params = subway.filter_sql(conn, city="北京", station="s3", radius=0.0)
    assert {r[0] for r in conn.execute(sql, params)} == {"x1"}  # 0 <= 0 命中
    sql2, params2 = subway.filter_sql(conn, city="北京", station="s2", radius=0.0)
    # x2 就在 s1 而不在 s2；x1 距 s2 约 1.7km -> 均不命中（<=0 严格）
    assert {r[0] for r in conn.execute(sql2, params2)} == set()


def test_threshold_nonzero_both_sides(conn):
    """P2-7：合法非零阈值的两侧边界（刚够 / 刚不够）。"""
    # x1 恰在 s3 上（0km）。s1-s3 间距约 0.86km。
    near = {r[0] for r in conn.execute(*subway.filter_sql(conn, city="北京", station="s1", radius=0.9))}
    far = {r[0] for r in conn.execute(*subway.filter_sql(conn, city="北京", station="s1", radius=0.5))}
    assert "x1" in near          # 0.86km <= 0.9 命中
    assert "x1" not in far       # 0.86km >  0.5 不命中（另一侧）


# --------------------------------------------------------------------------- #
# P2-07 组合筛选：地铁 + 价格 AND 同时生效
# --------------------------------------------------------------------------- #
def test_catalog_combined_subway_and_price(conn):
    # 线 a<=1km 命中 x1(60000)、x2(90000)；再加价格<=70000 -> 仅 x1。
    res = catalog.query_catalog(conn, city="北京", lines=["a"], subway_max=1.0,
                                price_min=0, price_max=70000)
    assert [it["id"] for it in res["items"]] == ["x1"]


# --------------------------------------------------------------------------- #
# P1 回归：price_week 与 line/station 组合时的参数绑定顺序（total>0 但 items=[]）
# --------------------------------------------------------------------------- #
def test_catalog_price_week_with_line_all_params_bound(conn):
    res = catalog.query_catalog(conn, city="北京", lines=["a"], subway_max=1.0, price_week="2026-W46")
    assert res["total"] > 0
    assert len(res["items"]) == res["total"], "不得出现 total>0 但 items=[]（参数绑定错位）"
    assert "x2" in [it["id"] for it in res["items"]]


def test_catalog_price_week_with_station_all_params_bound(conn):
    res = catalog.query_catalog(conn, city="北京", station="s3", price_week="2026-W46")
    assert res["total"] > 0
    assert len(res["items"]) == res["total"]
    assert "x1" in [it["id"] for it in res["items"]]


def test_catalog_price_week_nonexistent_returns_empty(conn):
    res = catalog.query_catalog(conn, city="北京", lines=["a"], subway_max=1.0, price_week="1999-01")
    assert res["total"] == 0 and res["items"] == []


def test_catalog_item_exposes_real_price_source(conn):
    res = catalog.query_catalog(conn, city="北京", q="x")
    it = next(i for i in res["items"] if i["id"] == "x1")
    assert it["price_source"] == "房天下"
