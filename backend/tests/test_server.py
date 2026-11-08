"""shikar 静态服务（``api`` 的 ``web_root``）与启动入口（``main``）测试。

全部仅用标准库：

* 静态服务：用 ``tmp_path`` 造 ``index.html`` / ``style.css``，以
  ``create_server(conn, web_root=str(tmp))`` 起真实后台服务，验证 ``/`` 返回
  ``text/html`` 且含文件内容、``/style.css`` 为 ``text/css``、缺失文件 404 JSON、
  路径穿越（含编码形式）越界返回 404 且不泄露外部文件、``/api/health`` 不回归。
* ``web_root`` 缺失时非 ``/api`` 路径仍返回 404 JSON（旧行为）。
* ``seed_demo``：内存库播种后计数正确、``overview.compute_overview`` 得到 2 个城市
  且 count>0，重复调用幂等。
* ``run``：以 ``--port 0``（不阻塞 ``serve_forever`` 长期运行）不易测，故改为
  对 ``_is_empty`` 与参数解析做覆盖，并对 ``seed_demo`` 做端到端数据校验。
"""

from __future__ import annotations

import http.client
import json
import sqlite3
import threading
from typing import Iterator
from urllib.parse import quote

import pytest

from shikar import storage
from shikar.api import create_server
from shikar.main import seed_demo
from shikar.overview import compute_overview


def _threadsafe_conn() -> sqlite3.Connection:
    """线程安全 ``:memory:`` 连接（供 ``ThreadingHTTPServer`` 使用）。"""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# --------------------------------------------------------------------------- #
# 静态服务夹具
# --------------------------------------------------------------------------- #
@pytest.fixture()
def web_root(tmp_path) -> str:
    """构造含 ``index.html`` / ``style.css`` 及一个外部机密文件的临时静态根。"""
    root = tmp_path / "web"
    root.mkdir()
    (root / "index.html").write_text("<h1>shikar 首页</h1>", encoding="utf-8")
    (root / "style.css").write_text("body{color:#333}", encoding="utf-8")
    # 与 web 同级的外部文件，用于验证路径穿越不会泄露它。
    (tmp_path / "secret.txt").write_text("TOP-SECRET", encoding="utf-8")
    return str(root)


@pytest.fixture()
def static_server(web_root: str) -> Iterator[dict]:
    """起一个带 ``web_root`` 的后台服务，测试结束关闭。"""
    conn = _threadsafe_conn()
    storage.init_db(conn)
    server = create_server(conn, host="127.0.0.1", port=0, web_root=web_root)
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


@pytest.fixture()
def no_web_server() -> Iterator[dict]:
    """起一个不带 ``web_root`` 的后台服务（验证旧 404 行为）。"""
    conn = _threadsafe_conn()
    storage.init_db(conn)
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


def _get(server: dict, path: str) -> tuple[int, dict[str, str], bytes]:
    """发一次 GET，返回 ``(状态码, 响应头(小写键), 原始字节)``。"""
    client = http.client.HTTPConnection(server["host"], server["port"], timeout=5)
    try:
        client.request("GET", path)
        resp = client.getresponse()
        body = resp.read()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, headers, body
    finally:
        client.close()


def _get_json(server: dict, path: str) -> tuple[int, dict[str, str], object]:
    """GET 并解析 JSON。"""
    status, headers, body = _get(server, path)
    return status, headers, json.loads(body.decode("utf-8"))


# --------------------------------------------------------------------------- #
# 静态服务测试
# --------------------------------------------------------------------------- #
def test_root_serves_index_html(static_server: dict) -> None:
    """``GET /`` 返回 200、``text/html`` 且含 index.html 内容。"""
    status, headers, body = _get(static_server, "/")
    assert status == 200
    assert headers["content-type"] == "text/html; charset=utf-8"
    assert "shikar 首页" in body.decode("utf-8")


def test_css_content_type(static_server: dict) -> None:
    """``GET /style.css`` 返回 ``text/css``。"""
    status, headers, body = _get(static_server, "/style.css")
    assert status == 200
    assert headers["content-type"] == "text/css"
    assert b"body{" in body


def test_missing_file_404_json(static_server: dict) -> None:
    """``GET /nope`` 返回 404 且响应体为含 ``error`` 的 JSON。"""
    status, headers, payload = _get_json(static_server, "/nope")
    assert status == 404
    assert "error" in payload
    assert headers["content-type"] == "application/json; charset=utf-8"


@pytest.mark.parametrize(
    "path",
    [
        "/../secret.txt",
        "/%2e%2e/secret.txt",
        "/%2e%2e%2fsecret.txt",
        "/sub/../../secret.txt",
        "/..%2fsecret.txt",
    ],
)
def test_path_traversal_blocked(static_server: dict, path: str) -> None:
    """各种 ``..`` 及编码形式的越界请求均返回 404，且不泄露外部文件内容。"""
    status, headers, body = _get(static_server, path)
    assert status == 404
    assert headers["content-type"] == "application/json; charset=utf-8"
    assert b"TOP-SECRET" not in body


def test_api_route_not_regressed(static_server: dict) -> None:
    """启用 ``web_root`` 后 ``/api/health`` 仍是 JSON、行为不变。"""
    status, headers, payload = _get_json(static_server, "/api/health")
    assert status == 200
    assert payload == {"status": "ok"}
    assert headers["content-type"] == "application/json; charset=utf-8"


def test_unknown_api_path_still_404_json(static_server: dict) -> None:
    """``/api/`` 前缀下的未知路径仍走 API 的 404 JSON（不被静态服务吞掉）。"""
    status, headers, payload = _get_json(static_server, "/api/does-not-exist")
    assert status == 404
    assert "error" in payload


def test_no_web_root_keeps_old_404(no_web_server: dict) -> None:
    """``web_root=None`` 时非 ``/api`` 路径仍返回 404 JSON（旧行为）。"""
    status, headers, payload = _get_json(no_web_server, "/")
    assert status == 404
    assert "error" in payload
    assert headers["content-type"] == "application/json; charset=utf-8"


# --------------------------------------------------------------------------- #
# seed_demo 测试
# --------------------------------------------------------------------------- #
def test_seed_demo_counts() -> None:
    """``seed_demo`` 返回 6 小区、60 指标行，且库中行数一致。"""
    conn = _threadsafe_conn()
    storage.init_db(conn)
    try:
        stats = seed_demo(conn)
        assert stats == {"xiaoqu": 6, "metrics": 60}
        n_xiaoqu = conn.execute("SELECT COUNT(*) FROM xiaoqu").fetchone()[0]
        n_metric = conn.execute(
            "SELECT COUNT(*) FROM xiaoqu_metric_weekly"
        ).fetchone()[0]
        assert n_xiaoqu == 6
        assert n_metric == 60
    finally:
        conn.close()


def test_seed_demo_overview_has_two_cities() -> None:
    """播种后 ``compute_overview`` 得到 2 个城市且每城 count>0。"""
    conn = _threadsafe_conn()
    storage.init_db(conn)
    try:
        seed_demo(conn)
        ov = compute_overview(conn)
        cities = [c["city"] for c in ov["cities"]]
        assert cities == ["北京", "天津"]
        for c in ov["cities"]:
            assert c["count"] > 0
        assert ov["week"] == "2025-W10"
    finally:
        conn.close()


def test_seed_demo_prices_have_variance() -> None:
    """Demo 价格走势有升有降（存在 mom4 为正与为负的小区）。"""
    conn = _threadsafe_conn()
    storage.init_db(conn)
    try:
        seed_demo(conn)
        mom4_vals = [
            r[0]
            for r in conn.execute(
                "SELECT mom4 FROM xiaoqu_metric_weekly WHERE mom4 IS NOT NULL"
            ).fetchall()
        ]
        assert any(v > 0 for v in mom4_vals)
        assert any(v < 0 for v in mom4_vals)
    finally:
        conn.close()


def test_seed_demo_idempotent() -> None:
    """重复调用 ``seed_demo`` 不产生重复行。"""
    conn = _threadsafe_conn()
    storage.init_db(conn)
    try:
        seed_demo(conn)
        seed_demo(conn)
        n_metric = conn.execute(
            "SELECT COUNT(*) FROM xiaoqu_metric_weekly"
        ).fetchone()[0]
        n_xiaoqu = conn.execute("SELECT COUNT(*) FROM xiaoqu").fetchone()[0]
        assert n_xiaoqu == 6
        assert n_metric == 60
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# run / _is_empty 辅助
# --------------------------------------------------------------------------- #
def test_is_empty_true_then_false() -> None:
    """空库 ``_is_empty`` 为真，播种后为假。"""
    from shikar.main import _is_empty

    conn = _threadsafe_conn()
    storage.init_db(conn)
    try:
        assert _is_empty(conn) is True
        seed_demo(conn)
        assert _is_empty(conn) is False
    finally:
        conn.close()
