"""shikar 首页总览聚合：把一个连接上的小区最新指标聚合成总览结构。

本模块在既有数据层 ``shikar.storage`` 之上提供单个纯聚合函数
:func:`compute_overview`，供 ``api`` 层的 ``GET /api/overview`` 直接调用。

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
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from . import storage

__all__ = ["compute_overview"]

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


def compute_overview(conn: sqlite3.Connection) -> dict:
    """聚合全部小区的最新指标，生成首页总览结构。

    对每个小区（``storage.list_xiaoqu``）取其指标序列
    （``storage.get_metric_series``），挑出「按 ``week`` 升序、``temperature``
    非空」的最后一条作为最新行；无最新行的小区直接跳过。随后：

    * 全局 ``week`` 取所有最新行 ``week`` 的字符串最大值（无则 ``None``）；
    * 按 ``city`` 分组，逐城计算均温、小区数与四类榜单。

    :param conn: 已打开的 SQLite 连接（只读使用）。
    :return: 形如 ``{"week": <str|None>, "cities": [...]}`` 的字典；空库返回
        ``{"week": None, "cities": []}``。
    """
    xiaoqu_list = storage.list_xiaoqu(conn)

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
