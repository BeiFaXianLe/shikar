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
* ``GET /api/overview?city=&district=``                → 首页总览聚合（可选过滤）
* ``GET /api/districts?city=``                         → 「城市—区」列表（供筛选）
* ``GET /api/catalog?city=&district=&q=&page=&page_size=`` → 小区目录（分页 + 搜索）
* ``GET /api/xiaoqu?city=&district=``                  → 小区列表
* ``GET /api/xiaoqu/{id}``                             → 单条小区 / 404
* ``GET /api/xiaoqu/{id}/series?start_week=&end_week=``→ 周度指标（week 升序）
* ``GET /api/xiaoqu/{id}/signals``                     → 信号事件列表
* 未知路径                                             → 404
* 非 GET                                               → 405

静态文件服务（可选）：

当 ``create_server`` 传入 ``web_root`` 时，非 ``/api/`` 前缀的 GET 请求将作为
静态文件处理：``/`` 映射到 ``index.html``，按扩展名给出 ``Content-Type``，并做
**路径穿越防护**（规范化后必须仍位于 ``web_root`` 内，越界返回 404）。
``web_root`` 为 ``None`` 时非 ``/api`` 路径仍走原有 404 JSON 逻辑（保持旧测试）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from . import catalog, overview, storage, subway

__all__ = ["create_server", "make_handler"]

#: 静态文件扩展名到 ``Content-Type`` 的映射；未命中时用 ``application/octet-stream``。
_CONTENT_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css",
    ".js": "application/javascript",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


def _content_type(path: str) -> str:
    """按文件扩展名返回静态文件 ``Content-Type``。

    :param path: 静态文件路径（或 URL 路径）。
    :return: 命中的 MIME 类型；未知扩展名返回 ``application/octet-stream``。
    """
    ext = os.path.splitext(path)[1].lower()
    return _CONTENT_TYPES.get(ext, "application/octet-stream")


def _resolve_static(web_root: str, url_path: str) -> Optional[str]:
    """把 URL 路径安全地解析为 ``web_root`` 内的绝对文件路径。

    做路径穿越防护：将 URL 路径解码、拼接后 ``realpath`` 规范化，若结果不在
    ``web_root`` 的真实路径之内则返回 ``None``（由调用方返回 404）。

    :param web_root: 静态根目录。
    :param url_path: 已 ``urlsplit`` 出的 URL 路径（如 ``/``、``/style.css``）。
    :return: 位于 ``web_root`` 内的绝对路径；越界或非法时返回 ``None``。
    """
    rel = unquote(url_path).lstrip("/")
    if rel == "" or url_path.endswith("/"):
        rel = (rel + "index.html") if rel else "index.html"
    root_abs = os.path.realpath(web_root)
    candidate = os.path.realpath(os.path.join(root_abs, rel))
    # 规范化后必须仍位于 web_root 内（含 root 自身边界，避免前缀误判）。
    if candidate != root_abs and not candidate.startswith(root_abs + os.sep):
        return None
    return candidate


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


def _first_int(qs: dict[str, list[str]], key: str) -> Optional[int]:
    """取查询字符串中某个键的首个值并转为整数；缺失/空串/非法时返回 ``None``。

    非法的整数字面量（如 ``abc``）按「未提供」处理，交由下游使用默认值，
    以避免把明显的坏参数放大成 500。

    :param qs: ``parse_qs`` 解析出的映射。
    :param key: 查询参数名。
    :return: 解析出的整数，或 ``None``。
    """
    raw = _first(qs, key)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _first_float(qs: dict[str, list[str]], key: str) -> Optional[float]:
    """取查询字符串中某个键的首个值并转为浮点数；缺失/空串/非法时返回 ``None``。

    非法的浮点字面量（如 ``abc``）按「未提供」处理，交由下游使用默认值，避免
    把明显的坏参数放大成 500。

    :param qs: ``parse_qs`` 解析出的映射。
    :param key: 查询参数名。
    :return: 解析出的浮点数，或 ``None``。
    """
    raw = _first(qs, key)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _list(qs: dict[str, list[str]], key: str) -> Optional[list[str]]:
    """取查询字符串中某键的全部非空值（用于多选线路等）；全空时返回 ``None``。

    :param qs: ``parse_qs`` 解析出的映射。
    :param key: 查询参数名。
    :return: 非空字符串值列表，或 ``None``。
    """
    values = [v for v in (qs.get(key) or []) if v != ""]
    return values or None


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

    # /api/overview  （首页总览聚合，可选 city / district 过滤）
    if path == "/api/overview":
        city = _first(qs, "city")
        district = _first(qs, "district")
        return 200, overview.compute_overview(conn, city=city, district=district)

    # /api/districts  （「城市—区」列表，供首页区下拉）
    if path == "/api/districts":
        city = _first(qs, "city")
        return 200, overview.list_districts(conn, city=city)

    # /api/subway/lines  （地铁线路与站点，供筛选下拉）
    if path == "/api/subway/lines":
        return 200, subway.list_lines(conn, city=_first(qs, "city"))

    # /api/xiaoqu  （列表 + 可选过滤）
    if path == "/api/xiaoqu":
        city = _first(qs, "city")
        district = _first(qs, "district")
        return 200, storage.list_xiaoqu(conn, city=city, district=district)

    # /api/catalog  （小区目录：城市 / 区 / 关键词 / 价格 / 地铁 + 分页 + 排序）
    if path == "/api/catalog":
        city = _first(qs, "city")
        district = _first(qs, "district")
        q = _first(qs, "q")
        page = _first_int(qs, "page")
        page_size = _first_int(qs, "page_size")
        try:
            return 200, catalog.query_catalog(
                conn,
                city=city,
                district=district,
                q=q,
                page=page if page is not None else 1,
                page_size=page_size if page_size is not None else 50,
                price_min=_first_float(qs, "price_min"),
                price_max=_first_float(qs, "price_max"),
                lines=_list(qs, "line"),
                station=_first(qs, "station"),
                subway_mode=_first(qs, "subway_mode") or "station",
                subway_max=_first_float(qs, "subway_max"),
                sort=_first(qs, "sort") or "default",
            )
        except ValueError as exc:
            # 参数非法（含未导入地铁却请求地铁筛选）→ 400，附明确中文原因。
            return 400, {"error": str(exc)}

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


def make_handler(
    conn: sqlite3.Connection, web_root: Optional[str] = None
) -> type[BaseHTTPRequestHandler]:
    """构造绑定到给定连接（及可选静态根目录）的处理器子类。

    以工厂方式而非全局状态把连接交给处理器，使多个 ``create_server`` 实例
    互不干扰。

    ``ThreadingHTTPServer`` 为每个请求派发到独立线程，因此传入的 ``conn`` 必须
    是线程安全的（例如以 ``check_same_thread=False`` 创建）。为避免并发请求在同
    一连接上交叉执行，这里另加一把进程内的互斥锁串行化所有数据库访问。

    :param conn: 已打开的 SQLite 连接（须为线程安全连接）。
    :param web_root: 可选静态文件根目录；``None`` 时不提供静态服务。
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

        def _send_bytes(
            self, status: int, content_type: str, body: bytes
        ) -> None:
            """写入一条完整的任意类型响应。

            :param status: HTTP 状态码。
            :param content_type: 响应 ``Content-Type``。
            :param body: 原始响应体字节。
            """
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_static(self, url_path: str) -> None:
            """按 ``web_root`` 提供静态文件；越界或不存在时返回 404 JSON。

            :param url_path: 已 ``urlsplit`` 出的 URL 路径。
            """
            if web_root is None:
                self._send_json(404, {"error": f"未知路径: {url_path}"})
                return
            target = _resolve_static(web_root, url_path)
            if target is None or not os.path.isfile(target):
                self._send_json(404, {"error": f"文件不存在: {url_path}"})
                return
            try:
                with open(target, "rb") as fh:
                    body = fh.read()
            except OSError as exc:
                self._send_json(404, {"error": f"文件不存在: {url_path} ({exc})"})
                return
            # 以实际的已解析文件路径（而非 URL）决定扩展名/MIME。
            self._send_bytes(200, _content_type(target), body)

        def do_GET(self) -> None:  # noqa: N802  （http.server 约定方法名）
            """处理 GET：``/api/`` 走路由输出 JSON，其余按静态文件服务。"""
            path = urlsplit(self.path).path
            if not path.startswith("/api/"):
                self._serve_static(path)
                return
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
    web_root: Optional[str] = None,
) -> ThreadingHTTPServer:
    """创建只读 HTTP 服务实例（未启动，需调用方 ``serve_forever`` / ``serve``）。

    由于返回的 ``ThreadingHTTPServer`` 会用独立线程处理每个请求，``conn`` 必须是
    线程安全的连接（例如通过 ``sqlite3.connect(path, check_same_thread=False)``
    创建，之后再 ``init_db`` 建表）。处理器内部另加互斥锁串行化数据库访问，因此
    即便多个请求并发也不会在同一连接上交叉执行。

    :param conn: 已打开的 SQLite 连接（须为线程安全连接），处理请求时只读使用。
    :param host: 绑定地址，默认 ``"127.0.0.1"``（仅回环，不回 ``0.0.0.0``）。
    :param port: 端口，默认 ``0`` 表示由系统分配空闲端口（测试友好）。
    :param web_root: 可选静态文件根目录；给出时提供静态文件服务，``None`` 时
        非 ``/api`` 路径返回 404 JSON（保持旧行为）。
    :return: 配置好处理器的 ``ThreadingHTTPServer``，其 ``server_address`` 反映
        实际绑定地址与端口。
    """
    handler_cls: Callable[..., BaseHTTPRequestHandler] = make_handler(conn, web_root)
    server = ThreadingHTTPServer((host, port), handler_cls)
    return server
