"""shikar 小区目录查询：为 ``GET /api/catalog`` 提供分页 + 过滤的纯函数。

本模块在既有数据层 ``shikar.storage`` 之上提供唯一一个函数
:func:`query_catalog`，供 ``api`` 层做参数解析后调用。它把「浏览 1.7 万小区」
所需的筛选、搜索、分页与轻量聚合收拢在一处，避免 ``api`` 层拼 SQL。

设计原则（与 ``overview`` 一致）：

* **仅用 Python 标准库**：只依赖 ``sqlite3`` / ``typing``，不引入第三方包；
* **只读、无副作用**：只读 ``xiaoqu`` 与 ``xiaoqu_metric_weekly``，不写库、
  不持有全局状态；
* **纯函数风格**：输入连接与参数，输出可 JSON 序列化的普通字典，便于测试。

过滤与分页规则：

* ``city`` / ``district``：精确匹配（``=``）；``None`` 表示不限；
* ``q``：小区名包含匹配（SQL ``LIKE %q%``），且**不区分大小写**（``LIKE`` 在
  SQLite 中对 ASCII 默认大小写不敏感，此处再以 ``lower()`` 显式兜底）；
* ``page`` 从 1 开始；``page`` < 1 视为 1，超过末页时返回空 ``items`` 但
  仍给出正确 ``total``；
* ``page_size`` 默认 50、上限 200；``page_size`` < 1 视为 1。

每个条目给出：目录 id、城市、区、板块、小区名、数据期数、以及**最新一期**
（``week`` 最大）的 ``price`` / ``week`` / ``mom4``。最新一期取该小区
``week`` 字符串最大值的那一行（可能其 ``price`` 为空，此时 ``latest_price``
为 ``None``，如实反映）。
"""

from __future__ import annotations

import sqlite3
from typing import Optional

__all__ = ["query_catalog"]

#: ``page_size`` 默认值。
_DEFAULT_PAGE_SIZE = 50

#: ``page_size`` 上限。
_MAX_PAGE_SIZE = 200


def _clamp_page(page: Optional[int]) -> int:
    """把 ``page`` 规整为 >= 1 的整数；``None`` 视为 1。

    :param page: 原始页码（可能为 ``None``）。
    :return: 规整后的页码（最小 1）。
    """
    if page is None:
        return 1
    return page if page >= 1 else 1


def _clamp_page_size(page_size: Optional[int]) -> int:
    """把 ``page_size`` 规整到 ``[1, 200]`` 区间；``None`` 取默认 50。

    :param page_size: 原始每页条数（可能为 ``None``）。
    :return: 规整后的每页条数。
    """
    if page_size is None:
        return _DEFAULT_PAGE_SIZE
    if page_size < 1:
        return 1
    if page_size > _MAX_PAGE_SIZE:
        return _MAX_PAGE_SIZE
    return page_size


def _build_where(
    city: Optional[str], district: Optional[str], q: Optional[str]
) -> tuple[str, list]:
    """拼出 ``WHERE`` 子句与参数（不含前导 ``WHERE``，条件以 ``AND`` 连接）。

    :param city: 城市精确过滤；``None`` 不限。
    :param district: 区精确过滤；``None`` 不限。
    :param q: 小区名包含（不区分大小写）；``None`` 或空串不限。
    :return: ``(" AND a = ? AND b LIKE ?", [...])`` 形式的子句与参数列表。
    """
    clauses: list[str] = []
    params: list = []
    if city is not None:
        clauses.append("AND city = ?")
        params.append(city)
    if district is not None:
        clauses.append("AND district = ?")
        params.append(district)
    if q is not None and q != "":
        # 以 lower(name) LIKE lower(?) 显式不区分大小写；通配符按用户输入原样使用。
        clauses.append("AND lower(name) LIKE lower(?)")
        params.append("%" + q + "%")
    return " ".join(clauses), params


def query_catalog(
    conn: sqlite3.Connection,
    city: Optional[str] = None,
    district: Optional[str] = None,
    q: Optional[str] = None,
    page: Optional[int] = 1,
    page_size: Optional[int] = _DEFAULT_PAGE_SIZE,
) -> dict:
    """按城市 / 区 / 关键词过滤并分页查询小区目录。

    过滤条件全部可选；不传则返回全量（分页）。返回结构：

    ``{"total", "page", "page_size", "items": [ {...}, ... ]}``

    其中每个 ``items`` 元素含 ``id`` / ``city`` / ``district`` / ``plate`` /
    ``name`` / ``data_points`` / ``latest_price`` / ``latest_week`` /
    ``latest_mom``。

    ``data_points`` 为该小区在 ``xiaoqu_metric_weekly`` 的行数；``latest_*``
    取该小区 ``week`` 最大那一行的 ``price`` / ``week`` / ``mom4``（可能为
    ``None``）。

    :param conn: 已打开的 SQLite 连接（只读使用）。
    :param city: 城市精确过滤；``None`` 不限。
    :param district: 区精确过滤；``None`` 不限。
    :param q: 小区名包含匹配（不区分大小写）；``None`` 或空串不限。
    :param page: 页码，从 1 开始；``None`` 或 < 1 视为 1。
    :param page_size: 每页条数，默认 50、上限 200；< 1 视为 1。
    :return: 分页结果字典；空结果时 ``items`` 为 ``[]`` 且 ``total`` 正确。
    """
    page_n = _clamp_page(page)
    size_n = _clamp_page_size(page_size)
    where_sql, params = _build_where(city, district, q)

    total_row = conn.execute(
        f"SELECT COUNT(*) AS n FROM xiaoqu WHERE 1 = 1 {where_sql}",
        params,
    ).fetchone()
    total = int(total_row["n"]) if total_row is not None else 0

    offset = (page_n - 1) * size_n
    rows = conn.execute(
        "SELECT id, city, district, plate, name FROM xiaoqu "
        f"WHERE 1 = 1 {where_sql} "
        "ORDER BY city ASC, district ASC, name ASC, id ASC "
        "LIMIT ? OFFSET ?",
        params + [size_n, offset],
    ).fetchall()

    items: list[dict] = []
    for row in rows:
        latest = conn.execute(
            "SELECT week, price, mom4 FROM xiaoqu_metric_weekly "
            "WHERE xiaoqu_id = ? ORDER BY week DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        dp_row = conn.execute(
            "SELECT COUNT(*) AS n FROM xiaoqu_metric_weekly WHERE xiaoqu_id = ?",
            (row["id"],),
        ).fetchone()
        data_points = int(dp_row["n"]) if dp_row is not None else 0
        items.append(
            {
                "id": row["id"],
                "city": row["city"],
                "district": row["district"],
                "plate": row["plate"],
                "name": row["name"],
                "data_points": data_points,
                "latest_price": latest["price"] if latest is not None else None,
                "latest_week": latest["week"] if latest is not None else None,
                "latest_mom": latest["mom4"] if latest is not None else None,
            }
        )

    return {
        "total": total,
        "page": page_n,
        "page_size": size_n,
        "items": items,
    }
