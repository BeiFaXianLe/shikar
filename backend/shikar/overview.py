"""shikar 首页总览聚合：把一个连接上的小区最新指标聚合成总览结构。

本模块在既有数据层 ``shikar.storage`` 之上提供两个函数，供 ``api`` 层调用：

* :func:`compute_overview`：聚合首页总览（可由 ``GET /api/overview`` 透传
  可选的 ``city`` / ``district`` 过滤）；
* :func:`list_districts`：列出可筛选的「城市—区」对（供 ``GET /api/districts``）。

设计原则：

* **仅用 Python 标准库**：只依赖 ``typing``，不引入任何第三方包；
* **只读、无副作用**：只通过 ``storage.list_xiaoqu`` /
  ``storage.get_metric_series`` 读取，不写库、不持有全局状态；
* **纯函数风格**：输入连接，输出普通可 JSON 序列化的字典，便于单独测试。

聚合规则：

* 「最新行」= 某小区按 ``week`` 升序、``temperature`` 非空的最后一条；无则跳过；
* 全局 ``week`` = 所有最新行里最大的 ``week``（字符串比较），无则 ``None``；
* 按 ``city`` 分组，组内输出：
  * ``temperature``：该城所有最新行 ``temperature`` 的均值，保留 1 位小数，
    无可用行时为 ``None``；
  * ``count``：参与计算的小区数；
  * ``top_hot`` / ``top_cold``：分别按 ``temperature`` 降序 / 升序，各至多 5 条；
  * ``top_rising`` / ``top_falling``：分别按 ``mom4`` 降序 / 升序，各至多 5 条，
    且**只包含 ``mom4`` 非空的行**；
* ``cities`` 按 city 名称升序排列。

可选过滤（向后兼容）：

* ``city`` 非 ``None`` 时只统计 ``xiaoqu.city`` 等于该值的小区；
* ``district`` 非 ``None`` 时只统计 ``xiaoqu.district`` 等于该值的小区
  （在 ``city`` 基础上再收窄）；
* 不传任何过滤参数时行为与旧版完全一致。
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from . import storage

__all__ = ["compute_overview", "list_districts"]

#: 每个榜单最多返回的条数。
_BOARD_SIZE = 5


def _latest_row(series: list[dict]) -> Optional[dict]:
    """取某小区指标序列中「按 week 升序、temperature 非空」的最后一条。

    ``series`` 由 :func:`shikar.storage.get_metric_series` 返回，已按 ``week``
    升序排序，因此从尾部向前找到第一条 ``temperature`` 非空的行即为最新行。

    :param series: 某小区的周度指标序列（``week`` 升序）。
    :return: 最新行字典；若整段序列都没有非空 ``temperature``，返回 ``None``。
    """
    for row in reversed(series):
        if row.get("temperature") is not None:
            return row
    return None


def _summary_item(xiaoqu: dict, row: dict) -> dict:
    """把一个小区的「最新行」与主数据合成榜单条目。

    :param xiaoqu: 小区主数据字典（含 ``id`` / ``name`` / ``district`` 等）。
    :param row: 该小区的最新指标行。
    :return: 形如 ``{xiaoqu_id, name, district, temperature, stage, mom4}`` 的字典。
    """
    return {
        "xiaoqu_id": xiaoqu.get("id"),
        "name": xiaoqu.get("name"),
        "district": xiaoqu.get("district"),
        "temperature": row.get("temperature"),
        "stage": row.get("stage"),
        "mom4": row.get("mom4"),
    }


def _top(rows: list[dict], key: str, reverse: bool) -> list[dict]:
    """按 ``key`` 排序取前 ``_BOARD_SIZE`` 条（不修改入参）。

    :param rows: 待排序条目列表。
    :param key: 排序依据的字段名。
    :param reverse: ``True`` 为降序，``False`` 为升序。
    :return: 排序后的至多 5 条新列表。
    """
    return sorted(rows, key=lambda item: item[key], reverse=reverse)[:_BOARD_SIZE]


def compute_overview(
    conn: sqlite3.Connection,
    city: Optional[str] = None,
    district: Optional[str] = None,
) -> dict:
    """聚合小区的最新指标，生成首页总览结构（可选取城市 / 区过滤）。

    对每个小区（``storage.list_xiaoqu``）取其指标序列
    （``storage.get_metric_series``），挑出「按 ``week`` 升序、``temperature``
    非空」的最后一条作为最新行；无最新行的小区直接跳过。随后：

    * 全局 ``week`` 取所有最新行 ``week`` 的字符串最大值（无则 ``None``）；
    * 按 ``city`` 分组，逐城计算均温、小区数与四类榜单。

    过滤在读取小区列表时下推给 :func:`shikar.storage.list_xiaoqu`：``city``
    只保留该城小区，``district`` 在 ``city`` 基础上再收窄到该区。其余聚合
    规则（均值、榜单、排序、返回结构）保持不变。

    :param conn: 已打开的 SQLite 连接（只读使用）。
    :param city: 只统计该城市的小区；``None`` 表示不限城市。
    :param district: 只统计该区的小区（在 ``city`` 基础上再过滤）；``None``
        表示不限区。
    :return: 形如 ``{"week": <str|None>, "cities": [...]}`` 的字典；空库或过滤
        后无可用小区时返回 ``{"week": None, "cities": []}``。
    """
    xiaoqu_list = storage.list_xiaoqu(conn, city=city, district=district)

    # city -> 该城所有最新行对应的榜单条目列表。
    grouped: dict[str, list[dict]] = {}
    weeks: list[str] = []

    for xiaoqu in xiaoqu_list:
        series = storage.get_metric_series(conn, xiaoqu.get("id"))
        if not series:
            continue
        row = _latest_row(series)
        if row is None:
            continue

        city = xiaoqu.get("city")
        # city 为空的记录无法归组，跳过以避免产出无名城市。
        if city is None:
            continue

        grouped.setdefault(city, []).append(_summary_item(xiaoqu, row))
        week = row.get("week")
        if week is not None:
            weeks.append(week)

    cities_out: list[dict] = []
    for city in sorted(grouped.keys()):
        items = grouped[city]

        temps = [item["temperature"] for item in items if item["temperature"] is not None]
        mean_temp: Optional[float] = (
            round(sum(temps) / len(temps), 1) if temps else None
        )

        rising_source = [item for item in items if item["mom4"] is not None]

        cities_out.append(
            {
                "city": city,
                "temperature": mean_temp,
                "count": len(items),
                "top_hot": _top(items, "temperature", reverse=True),
                "top_cold": _top(items, "temperature", reverse=False),
                "top_rising": _top(rising_source, "mom4", reverse=True),
                "top_falling": _top(rising_source, "mom4", reverse=False),
            }
        )

    return {"week": max(weeks) if weeks else None, "cities": cities_out}


def list_districts(
    conn: sqlite3.Connection, city: Optional[str] = None
) -> list[dict]:
    """列出可筛选的「城市—区」对，供首页区下拉使用。

    直接读取 ``xiaoqu`` 表，取去重后的 ``(city, district)`` 组合，按 ``city``、
    ``district`` 升序排列。``district`` 为 ``NULL`` 的记录（无法作为筛选项）
    会被跳过。给定 ``city`` 时只返回该城市下的区。

    :param conn: 已打开的 SQLite 连接（只读使用）。
    :param city: 只列出该城市的区；``None`` 表示列出全部城市。
    :return: ``[{"city": "北京", "district": "朝阳"}, ...]``，按 city、district
        升序且去重；无数据时返回空列表。
    """
    sql = [
        "SELECT DISTINCT city, district FROM xiaoqu",
        "WHERE city IS NOT NULL AND district IS NOT NULL",
    ]
    params: list = []
    if city is not None:
        sql.append("AND city = ?")
        params.append(city)
    sql.append("ORDER BY city ASC, district ASC")
    cur = conn.execute(" ".join(sql), params)
    return [{"city": r["city"], "district": r["district"]} for r in cur.fetchall()]
