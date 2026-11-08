"""shikar 只读 HTTP API 层（纯 Python 标准库，零第三方依赖）。

本模块在既有数据层 ``shikar.storage`` 之上暴露一组只读 JSON 接口，供本地
网页前端调用。实现严格只使用标准库：``http.server`` / ``json`` /
``urllib.parse``，不引入 fastapi / flask / uvicorn 等第三方框架。

设计要点：

* **只读、只接 GET**：任何非 ``GET`` 请求返回 ``405`` 且响应体为 JSON。
* **注入连接**：``create_server`` 接收外部传入的 ``sqlite3.Connection``，本模块
  不持有全局连接，便于测试用 ``:memory:`` 库。
* **默认仅绑定回环地址**：默认 ``127.0.0.1``，不绑 ``0.0.0.0``；``port=0`` 时由
  系统分配空闲端口，便于测试。
* **统一 JSON**：所有成功与错误响应都是 JSON，``Content-Type`` 为
  ``application/json; charset=utf-8``，中文以 ``ensure_ascii=False`` 输出，
  不乱码、不转义成 ``\\uXXXX``。

路由：

* ``GET /api/health``                                  → ``{"status": "ok"}``
* ``GET /api/xiaoqu?city=&district=``                  → 小区列表
* ``GET /api/xiaoqu/{id}``                             → 单条小区 / 404
* ``GET /api/xiaoqu/{id}/series?start_week=&end_week=``→ 周度指标（week 升序）
* ``GET /api/xiaoqu/{id}/signals``                     → 信号事件列表
* 未知路径                                             → 404
* 非 GET                                               → 405
"""

from __future__ import annotations

import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from . import storage

__all__ = ["create_server", "make_handler"]


# --------------------------------------------------------------------------- #
# 响应辅助
# --------------------------------------------------------------------------- #
def _json_payload(obj: object) -> bytes:
    """把任意可序列化对象编码为 UTF-8 JSON 字节串（中文不转义）。

    :param obj: 待编码对象。
    :return: ``ensure_ascii=False`` 编码后的 UTF-8 字节串。
    """
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _first(qs: dict[str, list[str]], key: str) -> Optional[str]:
    """取查询字符串中某个键的首个值；缺失或空串时返回 ``None``。

    :param qs: ``parse_qs`` 解析出的映射。
    :param key: 查询参数名。
    :return: 首个非空字符串值，或 ``None``。
    """
    values = qs.get(key)
    if not values:
        return None
    value = values[0]
    return value if value != "" else None


# --------------------------------------------------------------------------- #
# 路由处理
# --------------------------------------------------------------------------- #
def _route(
    conn: sqlite3.Connection, raw_path: str
) -> tuple[int, object]:
    """根据请求路径与查询串返回 ``(状态码, 响应对象)``。

    纯函数式的路由核心：不触碰 socket，便于单独推理与测试。

    :param conn: 已打开的 SQLite 连接（只读使用）。
    :param raw_path: 含查询串的原始请求路径（如 ``/api/xiaoqu?city=北京``）。
    :return: 二元组 ``(HTTP 状态码, 将被 JSON 序列化的响应对象)``。
    """
    parts = urlsplit(raw_path)
    path = parts.path
    qs = parse_qs(parts.query, keep_blank_values=True)

    # /api/health
    if path == "/api/health":
        return 200, {"status": "ok"}

    # /api/xiaoqu  （列表 + 可选过滤）
    if path == "/api/xiaoqu":
        city = _first(qs, "city")
        district = _first(qs, "district")
        return 200, storage.list_xiaoqu(conn, city=city, district=district)

    # /api/xiaoqu/{id} 及其子资源
    prefix = "/api/xiaoqu/"
    if path.startswith(prefix):
        rest = path[len(prefix):]
        # 去掉尾部斜杠，避免 /api/xiaoqu/{id}/ 被误判
        rest = rest.rstrip("/")
        segments = rest.split("/") if rest else []

        if len(segments) == 1 and segments[0]:
            xiaoqu_id = unquote(segments[0])
            rows = storage.list_xiaoqu(conn)
            for row in rows:
                if row.get("id") == xiaoqu_id:
                    return 200, row
            return 404, {"error": f"xiaoqu 不存在: {xiaoqu_id}"}

        if len(segments) == 2 and segments[0] and segments[1] == "series":
            xiaoqu_id = unquote(segments[0])
            start_week = _first(qs, "start_week")
            end_week = _first(qs, "end_week")
            return 200, storage.get_metric_series(
                conn, xiaoqu_id, start_week=start_week, end_week=end_week
            )

        if len(segments) == 2 and segments[0] and segments[1] == "signals":
            xiaoqu_id = unquote(segments[0])
            return 200, storage.list_signals(conn, xiaoqu_id)

    return 404, {"error": f"未知路径: {path}"}


def make_handler(conn: sqlite3.Connection) -> type[BaseHTTPRequestHandler]:
    """构造绑定到给定连接的 ``BaseHTTPRequestHandler`` 子类。

    以工厂方式而非全局状态把连接交给处理器，使多个 ``create_server`` 实例
    互不干扰。

    ``ThreadingHTTPServer`` 为每个请求派发到独立线程，因此传入的 ``conn`` 必须
    是线程安全的（例如以 ``check_same_thread=False`` 创建）。为避免并发请求在同
    一连接上交叉执行，这里另加一把进程内的互斥锁串行化所有数据库访问。

    :param conn: 已打开的 SQLite 连接（须为线程安全连接）。
    :return: 可作为 ``RequestHandlerClass`` 使用的处理器类。
    """
    db_lock = threading.Lock()

    class _Handler(BaseHTTPRequestHandler):
        """只读 JSON 请求处理器。"""

        #: 在访问日志中隐藏每个请求的默认输出噪声。
        def log_message(self, fmt: str, *args: object) -> None:  # noqa: D401
            """静默默认 stderr 访问日志，避免测试与本地运行时刷屏。"""
            return

        def _send_json(self, status: int, obj: object) -> None:
            """写入一条完整 JSON 响应。

            :param status: HTTP 状态码。
            :param obj: 将被序列化的响应对象。
            """
            body = _json_payload(obj)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802  （http.server 约定方法名）
            """处理 GET：路由后统一输出 JSON。"""
            try:
                with db_lock:
                    status, obj = _route(conn, self.path)
            except Exception as exc:  # 防御：任何内部错误都返回 JSON 500
                self._send_json(500, {"error": f"内部错误: {exc}"})
                return
            self._send_json(status, obj)

        def _method_not_allowed(self) -> None:
            """对所有非 GET 方法统一返回 405 JSON。"""
            self._send_json(405, {"error": f"方法不允许: {self.command}"})

        # 显式覆盖常见写方法，均返回 405（而非 http.server 的 501 HTML）。
        do_POST = _method_not_allowed  # type: ignore[assignment]
        do_PUT = _method_not_allowed  # type: ignore[assignment]
        do_PATCH = _method_not_allowed  # type: ignore[assignment]
        do_DELETE = _method_not_allowed  # type: ignore[assignment]
        do_HEAD = _method_not_allowed  # type: ignore[assignment]
        do_OPTIONS = _method_not_allowed  # type: ignore[assignment]

    return _Handler


def create_server(
    conn: sqlite3.Connection,
    host: str = "127.0.0.1",
    port: int = 0,
) -> ThreadingHTTPServer:
    """创建只读 HTTP 服务实例（未启动，需调用方 ``serve_forever`` / ``serve``）。

    由于返回的 ``ThreadingHTTPServer`` 会用独立线程处理每个请求，``conn`` 必须是
    线程安全的连接（例如通过 ``sqlite3.connect(path, check_same_thread=False)``
    创建，之后再 ``init_db`` 建表）。处理器内部另加互斥锁串行化数据库访问，因此
    即便多个请求并发也不会在同一连接上交叉执行。

    :param conn: 已打开的 SQLite 连接（须为线程安全连接），处理请求时只读使用。
    :param host: 绑定地址，默认 ``"127.0.0.1"``（仅回环，不回 ``0.0.0.0``）。
    :param port: 端口，默认 ``0`` 表示由系统分配空闲端口（测试友好）。
    :return: 配置好处理器的 ``ThreadingHTTPServer``，其 ``server_address`` 反映
        实际绑定地址与端口。
    """
    handler_cls: Callable[..., BaseHTTPRequestHandler] = make_handler(conn)
    server = ThreadingHTTPServer((host, port), handler_cls)
    return server
