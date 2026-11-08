"""shikar.api 只读 HTTP API 层单元测试。

全部仅用标准库：``threading`` 在后台启动 ``create_server(conn, port=0)``，
``http.client`` 发起真实请求。覆盖：

* ``/api/health`` = 200 且 ``{"status": "ok"}``；
* ``/api/xiaoqu`` 列表与 ``city`` / ``district`` 过滤；
* ``/api/xiaoqu/{id}`` 命中与未知 id → 404；
* ``/api/xiaoqu/{id}/series`` 的 week 升序与 ``start_week`` / ``end_week`` 区间；
* ``/api/xiaoqu/{id}/signals`` 返回；
* 未知路径 → 404；``POST`` → 405；
* 响应均为合法 JSON，中文不转义（``ensure_ascii=False``）。
"""

from __future__ import annotations

import http.client
import json
import sqlite3
import threading
from typing import Iterator

import pytest

from shikar.api import create_server
from shikar.storage import (
    add_signal,
    init_db,
    upsert_metric,
    upsert_xiaoqu,
)


def _threadsafe_conn() -> sqlite3.Connection:
    """构造一个 ``:memory:`` 连接，与 ``storage.connect`` 等价但线程安全。

    ``ThreadingHTTPServer`` 会用独立线程处理每个请求，标准库要求连接以
    ``check_same_thread=False`` 创建；该标志无法在既有连接上事后修改，故此处
    显式创建（不复用 ``storage.connect``，避免改动数据层模块）。
    """
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def _seed(conn) -> None:
    """播种 3 个小区（两个北京、一个天津）及若干周度指标与一条信号。"""
    upsert_xiaoqu(
        conn,
        {
            "id": "X1", "city": "北京", "district": "朝阳", "plate": "望京",
            "name": "望京A区", "alias": None, "lng": 116.48, "lat": 39.99,
            "build_year": 2005, "households": 1200,
        },
    )
    upsert_xiaoqu(
        conn,
        {
            "id": "X2", "city": "北京", "district": "海淀", "plate": "中关村",
            "name": "中关村B区", "alias": None, "lng": 116.31, "lat": 39.98,
            "build_year": 2010, "households": 800,
        },
    )
    upsert_xiaoqu(
        conn,
        {
            "id": "X3", "city": "天津", "district": "和平", "plate": "南京路",
            "name": "和平C区", "alias": None, "lng": 117.20, "lat": 39.12,
            "build_year": 1999, "households": 500,
        },
    )

    # X1 的周度指标，故意乱序写入以验证读取按 week 升序。
    for week, price in (("2026-W03", 5.30), ("2026-W01", 5.10), ("2026-W02", 5.20)):
        upsert_metric(
            conn,
            {
                "xiaoqu_id": "X1", "week": week, "price": price,
                "mom4": 0.1, "mom13": 0.2, "supply": 10, "vol30": 5,
                "absorb": 0.0, "bargain": 0.0, "heat": 50.0,
                "temperature": 60.0, "stage": "回暖",
            },
        )

    add_signal(conn, "X1", "2026-01-10", "回暖", {"note": "价格回升"})


@pytest.fixture()
def live_server() -> Iterator[dict]:
    """启动后台只读服务，返回含实际端口与路径前缀的字典，测试结束关闭。"""
    conn = _threadsafe_conn()
    init_db(conn)
    _seed(conn)
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
        conn.close()


# --------------------------------------------------------------------------- #
# 请求辅助
# --------------------------------------------------------------------------- #
def _request(
    server: dict, method: str, path: str
) -> tuple[int, dict[str, str], bytes]:
    """发一次 HTTP 请求，返回 ``(状态码, 响应头, 原始响应体字节)``。"""
    client = http.client.HTTPConnection(server["host"], server["port"], timeout=5)
    try:
        client.request(method, path)
        resp = client.getresponse()
        body = resp.read()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, headers, body
    finally:
        client.close()


def _json(server: dict, method: str, path: str) -> tuple[int, dict[str, str], object]:
    """请求并解析 JSON 响应，返回 ``(状态码, 响应头, 解析后的对象)``。"""
    status, headers, body = _request(server, method, path)
    return status, headers, json.loads(body.decode("utf-8"))


# --------------------------------------------------------------------------- #
# 测试用例
# --------------------------------------------------------------------------- #
def test_health(live_server: dict) -> None:
    """``/api/health`` 返回 200 与 ``{"status": "ok"}``，且 Content-Type 正确。"""
    status, headers, payload = _json(live_server, "GET", "/api/health")
    assert status == 200
    assert payload == {"status": "ok"}
    assert headers["content-type"] == "application/json; charset=utf-8"


def test_list_xiaoqu_all(live_server: dict) -> None:
    """``/api/xiaoqu`` 返回全部 3 个小区，按 id 升序。"""
    status, _headers, payload = _json(live_server, "GET", "/api/xiaoqu")
    assert status == 200
    assert [row["id"] for row in payload] == ["X1", "X2", "X3"]


def test_list_xiaoqu_filter_city(live_server: dict) -> None:
    """``city=北京`` 只返回两个北京小区。"""
    status, _headers, payload = _json(
        live_server, "GET", "/api/xiaoqu?city=%E5%8C%97%E4%BA%AC"
    )
    assert status == 200
    assert {row["id"] for row in payload} == {"X1", "X2"}


def test_list_xiaoqu_filter_city_district(live_server: dict) -> None:
    """``city=北京&district=朝阳`` 只返回 X1。"""
    status, _headers, payload = _json(
        live_server,
        "GET",
        "/api/xiaoqu?city=%E5%8C%97%E4%BA%AC&district=%E6%9C%9D%E9%98%B3",
    )
    assert status == 200
    assert [row["id"] for row in payload] == ["X1"]


def test_get_xiaoqu_found(live_server: dict) -> None:
    """``/api/xiaoqu/X1`` 返回该小区，中文名不转义。"""
    status, _headers, payload = _json(live_server, "GET", "/api/xiaoqu/X1")
    assert status == 200
    assert payload["id"] == "X1"
    assert payload["name"] == "望京A区"
    assert payload["city"] == "北京"


def test_get_xiaoqu_not_found(live_server: dict) -> None:
    """未知 id 返回 404 且响应体为含 ``error`` 的 JSON。"""
    status, headers, payload = _json(live_server, "GET", "/api/xiaoqu/NOPE")
    assert status == 404
    assert "error" in payload
    assert headers["content-type"] == "application/json; charset=utf-8"


def test_series_ascending(live_server: dict) -> None:
    """``/series`` 返回全部周度指标且按 week 升序。"""
    status, _headers, payload = _json(live_server, "GET", "/api/xiaoqu/X1/series")
    assert status == 200
    weeks = [row["week"] for row in payload]
    assert weeks == ["2026-W01", "2026-W02", "2026-W03"]


def test_series_range(live_server: dict) -> None:
    """``start_week`` / ``end_week`` 区间（含端点）过滤正确。"""
    status, _headers, payload = _json(
        live_server,
        "GET",
        "/api/xiaoqu/X1/series?start_week=2026-W02&end_week=2026-W03",
    )
    assert status == 200
    assert [row["week"] for row in payload] == ["2026-W02", "2026-W03"]


def test_signals(live_server: dict) -> None:
    """``/signals`` 返回播种的信号，``reason`` 已解析为对象，中文正常。"""
    status, _headers, payload = _json(live_server, "GET", "/api/xiaoqu/X1/signals")
    assert status == 200
    assert len(payload) == 1
    assert payload[0]["type"] == "回暖"
    assert payload[0]["reason"] == {"note": "价格回升"}


def test_unknown_path(live_server: dict) -> None:
    """未知路径返回 404 且为 JSON。"""
    status, _headers, payload = _json(live_server, "GET", "/api/does-not-exist")
    assert status == 404
    assert "error" in payload


def test_post_method_not_allowed(live_server: dict) -> None:
    """非 GET 方法返回 405 且为 JSON。"""
    status, _headers, payload = _json(live_server, "POST", "/api/health")
    assert status == 405
    assert "error" in payload


def test_chinese_not_escaped_raw(live_server: dict) -> None:
    """原始响应体中的中文为真正 UTF-8 字符，而非 ``\\uXXXX`` 转义。"""
    _status, _headers, body = _request(live_server, "GET", "/api/xiaoqu/X1")
    text = body.decode("utf-8")
    assert "望京A区" in text
    assert "\\u" not in text


def test_default_host_is_loopback() -> None:
    """``create_server`` 默认只绑定回环地址 ``127.0.0.1``。"""
    from shikar.api import create_server as _create

    conn = _threadsafe_conn()
    init_db(conn)
    try:
        server = _create(conn)
        try:
            assert server.server_address[0] == "127.0.0.1"
        finally:
            server.server_close()
    finally:
        conn.close()
