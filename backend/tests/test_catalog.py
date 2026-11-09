"""shikar 小区目录（``GET /api/catalog`` 与 ``catalog.query_catalog``）测试。

全部仅用标准库：``threading`` 在后台启动 ``create_server(conn, port=0)``，
``http.client`` 发起真实请求。覆盖：

* 纯函数 ``query_catalog``：city / district / q 过滤、分页与 total、
  ``page_size`` 上限、空结果、最新一期字段；
* HTTP 层 ``/api/catalog``：查询参数透传、默认值、非整数参数降级；
* 回归：``/api/xiaoqu`` 原行为未被破坏（仍返回全部且不分页）。
"""

from __future__ import annotations

import http.client
import json
import sqlite3
import threading
from typing import Iterator

import pytest

from shikar.api import create_server
from shikar.catalog import query_catalog
from shikar.storage import init_db, upsert_metric, upsert_xiaoqu


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def _conn() -> sqlite3.Connection:
    """构造线程安全的 ``:memory:`` 连接并建表（供纯函数与 HTTP 复用）。"""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    init_db(conn)
    return conn


def _seed(conn: sqlite3.Connection) -> None:
    """播种小区：北京 3 个（朝阳 2 / 海淀 1）、天津 2 个（和平 1 / 河西 1）。

    北京小区名故意含可供 ``q`` 命中的共同片段（「望京」），并给其中两个写入
    两期指标，用于核对 ``data_points`` 与 ``latest_*``。
    """
    rows = [
        ("北京|1", "北京", "朝阳", "望京", "望京A区"),
        ("北京|2", "北京", "朝阳", "朝阳门", "望京B区"),
        ("北京|3", "北京", "海淀", "中关村", "中关村C区"),
        ("天津|4", "天津", "和平", "南京路", "和平D区"),
        ("天津|5", "天津", "河西", "梅江", "梅江E区"),
    ]
    for xid, city, district, plate, name in rows:
        upsert_xiaoqu(
            conn,
            {
                "id": xid, "city": city, "district": district, "plate": plate,
                "name": name, "alias": None, "lng": None, "lat": None,
                "build_year": None, "households": None,
            },
        )

    # 北京|1：两期（2019-02 与 2026-W46），最新一期 price=49480、mom4=-0.0134。
    upsert_metric(conn, {
        "xiaoqu_id": "北京|1", "week": "2019-02", "price": 60094.0,
        "mom4": None, "mom13": None, "supply": None, "vol30": None,
        "absorb": None, "bargain": None, "heat": None,
        "temperature": None, "stage": None,
    })
    upsert_metric(conn, {
        "xiaoqu_id": "北京|1", "week": "2026-W46", "price": 49480.0,
        "mom4": -0.0134, "mom13": None, "supply": 12, "vol30": 3,
        "absorb": None, "bargain": None, "heat": None,
        "temperature": None, "stage": None,
    })
    # 北京|2：单期。
    upsert_metric(conn, {
        "xiaoqu_id": "北京|2", "week": "2026-W46", "price": 70000.0,
        "mom4": 0.02, "mom13": None, "supply": 5, "vol30": 1,
        "absorb": None, "bargain": None, "heat": None,
        "temperature": None, "stage": None,
    })


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    """已建表并播种的内存连接。"""
    c = _conn()
    _seed(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture()
def live_server(conn: sqlite3.Connection) -> Iterator[dict]:
    """在后台启动只读服务，返回含 host/port 的字典，测试结束关闭。"""
    server = create_server(conn, host="127.0.0.1", port=0)
    host, port = server.server_address[0], server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"host": host, "port": port}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get_json(server: dict, path: str) -> tuple[int, object]:
    """GET 一次并解析 JSON，返回 ``(状态码, 解析后的对象)``。"""
    client = http.client.HTTPConnection(server["host"], server["port"], timeout=5)
    try:
        client.request("GET", path)
        resp = client.getresponse()
        body = resp.read()
        return resp.status, json.loads(body.decode("utf-8"))
    finally:
        client.close()


# --------------------------------------------------------------------------- #
# 纯函数 query_catalog
# --------------------------------------------------------------------------- #
def test_all_returns_total_and_items(conn: sqlite3.Connection) -> None:
    """不传过滤：total = 全量 5，items 长度与 total 一致（单页足够）。"""
    res = query_catalog(conn)
    assert res["total"] == 5
    assert res["page"] == 1
    assert res["page_size"] == 50
    assert len(res["items"]) == 5


def test_filter_by_city(conn: sqlite3.Connection) -> None:
    """``city=北京`` 只返回 3 个北京小区。"""
    res = query_catalog(conn, city="北京")
    assert res["total"] == 3
    assert {it["city"] for it in res["items"]} == {"北京"}


def test_filter_by_city_and_district(conn: sqlite3.Connection) -> None:
    """``city=北京&district=朝阳`` 只返回 2 个。"""
    res = query_catalog(conn, city="北京", district="朝阳")
    assert res["total"] == 2
    assert {it["id"] for it in res["items"]} == {"北京|1", "北京|2"}


def test_filter_by_q_substring(conn: sqlite3.Connection) -> None:
    """``q=望京`` 命中两个含「望京」名的小区。"""
    res = query_catalog(conn, q="望京")
    assert res["total"] == 2
    assert {it["name"] for it in res["items"]} == {"望京A区", "望京B区"}


def test_filter_by_q_case_insensitive(conn: sqlite3.Connection) -> None:
    """``q`` 对大小写不敏感：用小写片段也能命中大写名字。"""
    # 先塞一个含大写拉丁字母的名字。
    upsert_xiaoqu(
        conn,
        {"id": "北京|9", "city": "北京", "district": "朝阳", "plate": "X",
         "name": "CBD花园ABC", "alias": None, "lng": None, "lat": None,
         "build_year": None, "households": None},
    )
    res = query_catalog(conn, q="abc")
    assert res["total"] == 1
    assert res["items"][0]["id"] == "北京|9"


def test_pagination_total_and_slice(conn: sqlite3.Connection) -> None:
    """page_size=2：total 仍为全量，第 1、2 页各 2 条、第 3 页 1 条。"""
    p1 = query_catalog(conn, page=1, page_size=2)
    assert p1["total"] == 5
    assert len(p1["items"]) == 2
    p3 = query_catalog(conn, page=3, page_size=2)
    assert p3["total"] == 5
    assert len(p3["items"]) == 1
    # 各页 id 不重叠，拼起来覆盖全部。
    ids = set()
    for pg in (1, 2, 3):
        for it in query_catalog(conn, page=pg, page_size=2)["items"]:
            ids.add(it["id"])
    assert len(ids) == 5


def test_page_beyond_last_is_empty(conn: sqlite3.Connection) -> None:
    """超出末页：items 为空，但 total 正确。"""
    res = query_catalog(conn, page=999, page_size=2)
    assert res["total"] == 5
    assert res["items"] == []


def test_page_size_cap(conn: sqlite3.Connection) -> None:
    """page_size 超过 200 被截到 200；小于 1 视为 1。"""
    assert query_catalog(conn, page_size=1000)["page_size"] == 200
    assert query_catalog(conn, page_size=0)["page_size"] == 1
    assert query_catalog(conn, page_size=None)["page_size"] == 50


def test_empty_result(conn: sqlite3.Connection) -> None:
    """无匹配：total = 0，items = []。"""
    res = query_catalog(conn, q="不存在的名字")
    assert res["total"] == 0
    assert res["items"] == []


def test_latest_and_data_points(conn: sqlite3.Connection) -> None:
    """两期小区：data_points=2，latest 取 week 最大那一期。"""
    res = query_catalog(conn, city="北京", district="朝阳", q="望京A")
    assert res["total"] == 1
    it = res["items"][0]
    assert it["data_points"] == 2
    assert it["latest_week"] == "2026-W46"
    assert it["latest_price"] == 49480.0
    assert it["latest_mom"] == -0.0134


def test_item_shape(conn: sqlite3.Connection) -> None:
    """条目包含约定的全部字段。"""
    it = query_catalog(conn, q="中关村")["items"][0]
    for key in ("id", "city", "district", "plate", "name", "data_points",
                "latest_price", "latest_week", "latest_mom"):
        assert key in it


# --------------------------------------------------------------------------- #
# HTTP 层 /api/catalog
# --------------------------------------------------------------------------- #
def test_http_catalog_default(live_server: dict) -> None:
    """``/api/catalog`` 默认返回全量分页结构。"""
    status, payload = _get_json(live_server, "/api/catalog")
    assert status == 200
    assert payload["total"] == 5
    assert payload["page"] == 1
    assert payload["page_size"] == 50
    assert isinstance(payload["items"], list)


def test_http_catalog_query_params(live_server: dict) -> None:
    """查询参数 city / district / q / page_size 透传正确。"""
    status, payload = _get_json(
        live_server,
        "/api/catalog?city=%E5%8C%97%E4%BA%AC&q=%E6%9C%9B%E4%BA%AC&page_size=1",
    )
    assert status == 200
    assert payload["total"] == 2
    assert payload["page_size"] == 1
    assert len(payload["items"]) == 1


def test_http_catalog_bad_int_degrades(live_server: dict) -> None:
    """非整数 page / page_size 降级为默认值，不报 500。"""
    status, payload = _get_json(live_server, "/api/catalog?page=abc&page_size=xyz")
    assert status == 200
    assert payload["page"] == 1
    assert payload["page_size"] == 50


def test_http_catalog_empty(live_server: dict) -> None:
    """无匹配关键词：200 且 items 为空。"""
    status, payload = _get_json(live_server, "/api/catalog?q=zzz-no-such")
    assert status == 200
    assert payload["total"] == 0
    assert payload["items"] == []


# --------------------------------------------------------------------------- #
# 回归：/api/xiaoqu 原行为未变
# --------------------------------------------------------------------------- #
def test_xiaoqu_list_unchanged(live_server: dict) -> None:
    """``/api/xiaoqu`` 仍返回全量列表（数组），行为与分页目录互不影响。"""
    status, payload = _get_json(live_server, "/api/xiaoqu")
    assert status == 200
    assert isinstance(payload, list)
    assert len(payload) == 5
    assert [r["id"] for r in payload] == sorted(r["id"] for r in payload)
