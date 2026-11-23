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
  * ``readings``：**读数层**。门槛不达标的小区不给温度与趋势，但它们的真实价格
    不该跟着一起丢掉（docs/04 §0.4「不足则只给读数」），因此单独统计：
    ``with_price``（有价格读数的小区数）、``total``（同上级）、
    ``median_price``（各小区最新一期价格的中位数）、``with_mom``（可算环比的小区数）、
    ``median_mom``（环比中位数，与 ``mom4`` 同口径：分数，-0.12 即 -12%）；
* ``cities`` 按 city 名称升序排列。

可选过滤（向后兼容）：

* ``city`` 非 ``None`` 时只统计 ``xiaoqu.city`` 等于该值的小区；
* ``district`` 非 ``None`` 时只统计 ``xiaoqu.district`` 等于该值的小区
  （在 ``city`` 基础上再收窄）；
* 不传任何过滤参数时行为与旧版完全一致。
"""

from __future__ import annotations

import sqlite3
import statistics
from typing import Optional

from . import storage

__all__ = ["compute_overview", "list_districts"]

#: 每个榜单最多返回的条数。
_BOARD_SIZE = 5
#: 计算「较上期变化」时允许的最大观测序号差；超过视为「上期太旧」，与无上期同样计。
MAX_DELTA_GAP = 4


def _last_row(series: list[dict]) -> Optional[dict]:
    """取某小区指标序列中按 ``week`` 升序的最后一条（不要求 ``temperature`` 非空）。

    用于区分「没有数据」与「有数据但温度不足」：后者不计入均温/榜单，
    但要在总览里以 ``insufficient`` 计数出现（docs/04 §3.2）。

    :param series: 按 ``week`` 升序的指标行列表。
    :return: 最后一条带 ``week`` 的行；没有则 ``None``。
    """
    for row in reversed(series):
        if row.get("week") is not None:
            return row
    return None


def _latest_index(series: list[dict]) -> Optional[int]:
    """返回 ``temperature`` 非空的最后一条的下标；没有则 ``None``。"""
    for i in range(len(series) - 1, -1, -1):
        if series[i].get("temperature") is not None:
            return i
    return None


def _latest_priced_row(series: list[dict]) -> Optional[dict]:
    """取某小区指标序列中最后一条 ``price`` 非空的行。

    用途：首页「读数层」。门槛不足的小区不给温度与趋势，但仍然有真实成交/挂牌
    价格——这部分读数不该跟着一起丢掉（docs/04 §0.4「不足则只给读数」）。

    :param series: 按 ``week`` 升序的指标行列表。
    :return: 最后一条有价格的行；全序列无价格则 ``None``。
    """
    for row in reversed(series):
        if row.get("price") is not None:
            return row
    return None


def _median_or_none(values: list[float], digits: int = 1) -> Optional[float]:
    """中位数，空列表返回 ``None``（保留 ``digits`` 位小数）。"""
    if not values:
        return None
    return round(statistics.median(values), digits)


def _prev_row(series: list[dict], latest_index: int) -> tuple[Optional[dict], Optional[int]]:
    """取最新行之前、最近一条 ``temperature`` 非空的行与其**观测序号差**。

    序号差 = ``latest_index - prev_index``（按序列下标，不解析周字符串）。
    与 ``mom4`` 的「前第 4 条记录」口径一致：比较的是观测，不是日历周。

    :return: ``(prev_row, gap)``；没有上期时返回 ``(None, None)``。
    """
    for j in range(latest_index - 1, -1, -1):
        if series[j].get("temperature") is not None:
            return series[j], latest_index - j
    return None, None


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
    （``storage.get_metric_series``）：

    * 「合格小区」= 存在 ``temperature`` 非空的历史行，取其最后一条作为最新行；
    * 「数据不足小区」= 有数据但所有行 ``temperature`` 均为空（docs/04 §2.2 门槛
      未满足），**不计入均温与四类榜单**，只计入 ``insufficient`` 计数。

    随后：

    * 全局 ``week`` 取所有小区最后一条有数据行的 ``week`` 最大值（无数据才 ``None``）；
    * 按 ``city`` 分组，逐城计算均温、``count``（合格数）、``total``（有数据数）、
      ``insufficient`` 与 ``insufficient_ratio``，以及四类榜单（只含合格小区）。

    过滤在读取小区列表时下推给 :func:`shikar.storage.list_xiaoqu`：``city``
    只保留该城小区，``district`` 在 ``city`` 基础上再收窄到该区。其余聚合
    规则（均值、榜单、排序、返回结构）保持不变。

    :param conn: 已打开的 SQLite 连接（只读使用）。
    :param city: 只统计该城市的小区；``None`` 表示不限城市。
    :param district: 只统计该区的小区（在 ``city`` 基础上再过滤）；``None``
        表示不限区。
    :return: 形如 ``{"week": <str|None>, "cities": [...]}`` 的字典；城市项含
        ``temperature`` / ``count`` / ``total`` / ``insufficient`` /
        ``insufficient_ratio`` 与四类榜单；另有 ``readings`` 读数层
        （``with_price`` / ``total`` / ``median_price`` / ``with_mom`` / ``median_mom``）。
        空库或过滤后无数据时返回 ``{"week": None, "cities": []}``。
    """
    xiaoqu_list = storage.list_xiaoqu(conn, city=city, district=district)

    # city -> 合格（temperature 非空）小区的榜单条目；另有总数与不足数。
    grouped: dict[str, list[dict]] = {}
    totals: dict[str, int] = {}
    insufficient: dict[str, int] = {}
    # city -> district -> 聚合累计（温度、合格数、总数、不足数）。
    district_stats: dict[str, dict[str, dict]] = {}
    # city -> 周变化累计：可用配对的和 + 无可用上期的个数。
    delta_stats: dict[str, dict] = {}
    # city -> 读数层累计：最新价列表 + 可算环比的 mom4 列表（docs/04 §0.4）。
    reading_stats: dict[str, dict] = {}
    weeks: list[str] = []

    for xiaoqu in xiaoqu_list:
        series = storage.get_metric_series(conn, xiaoqu.get("id"))
        if not series:
            continue
        last = _last_row(series)
        if last is None:
            continue

        city_name = xiaoqu.get("city")
        # 城市为空的记录无法归组，跳过以避免产出无名城市。
        if city_name is None:
            continue

        totals[city_name] = totals.get(city_name, 0) + 1
        district_name = xiaoqu.get("district") or "未分区"
        dstat = (
            district_stats.setdefault(city_name, {})
            .setdefault(district_name, {"temps": [], "count": 0, "total": 0, "insufficient": 0})
        )
        dstat["total"] += 1
        week = last.get("week")
        if week is not None:
            weeks.append(week)

        # 读数层：无论温度是否达标，只要有价格就读数（docs/04 §0.4）。
        priced = _latest_priced_row(series)
        if priced is not None:
            rstat = reading_stats.setdefault(city_name, {"prices": [], "moms": []})
            rstat["prices"].append(priced["price"])
            if priced.get("mom4") is not None:
                rstat["moms"].append(priced["mom4"])

        idx = _latest_index(series)
        if idx is None:
            # 有数据但温度不足：不参与均温与榜单，只计数（docs/04 §3.2）。
            insufficient[city_name] = insufficient.get(city_name, 0) + 1
            dstat["insufficient"] += 1
            continue
        row = series[idx]
        grouped.setdefault(city_name, []).append(_summary_item(xiaoqu, row))
        dstat["count"] += 1
        if row.get("temperature") is not None:
            dstat["temps"].append(row["temperature"])

        # 较上期（上一期有温度的观测）；序号差 > MAX_DELTA_GAP 视为「上期太旧」，
        # 与「无上期」合并计入 delta_insufficient。
        dst = delta_stats.setdefault(city_name, {"sums": [], "missing": 0})
        prev, gap = _prev_row(series, idx)
        if prev is not None and gap is not None and gap <= MAX_DELTA_GAP:
            dst["sums"].append(row["temperature"] - prev["temperature"])
        else:
            dst["missing"] += 1

    cities_out: list[dict] = []
    for city_name in sorted(totals.keys()):
        items = grouped.get(city_name, [])

        temps = [item["temperature"] for item in items if item["temperature"] is not None]
        mean_temp: Optional[float] = (
            round(sum(temps) / len(temps), 1) if temps else None
        )

        rising_source = [item for item in items if item["mom4"] is not None]

        total = totals[city_name]
        n_insufficient = insufficient.get(city_name, 0)
        dst = delta_stats.get(city_name, {"sums": [], "missing": 0})
        delta_sums = dst["sums"]
        delta_missing = dst["missing"]
        # 读数层：不足小区也给读数，因此与「合格小区」分开统计。
        rst = reading_stats.get(city_name, {"prices": [], "moms": []})
        readings_out = {
            "with_price": len(rst["prices"]),
            "total": total,
            "median_price": _median_or_none(rst["prices"]),
            "with_mom": len(rst["moms"]),
            # mom4 与 api 其余字段同样口径：分数（-0.12 即 -12%），前端乘 100 展示
            "median_mom": _median_or_none(rst["moms"], digits=4),
        }
        districts_out = []
        for district_name, dstat in district_stats.get(city_name, {}).items():
            dtemps = dstat["temps"]
            dtotal = dstat["total"]
            districts_out.append({
                "district": district_name,
                "temperature": round(sum(dtemps) / len(dtemps), 1) if dtemps else None,
                "count": dstat["count"],
                "total": dtotal,
                "insufficient": dstat["insufficient"],
                # 保留 4 位：3 位时 10550/10554 会被四舍五入成 1.0，把「还有几个
                # 合格小区」这一事实抹平（docs/45 P1-2）。
                "insufficient_ratio": (
                    round(dstat["insufficient"] / dtotal, 4) if dtotal else 0.0
                ),
            })
        # 温度降序，null 排最后；同值按区名升序。
        districts_out.sort(
            key=lambda d: (d["temperature"] is None, -(d["temperature"] or 0.0), d["district"])
        )
        cities_out.append(
            {
                "city": city_name,
                "temperature": mean_temp,
                "count": len(items),
                "total": total,
                "insufficient": n_insufficient,
                # 保留 4 位：3 位时 10550/10554 会被四舍五入成 1.0，把「还有几个
                # 合格小区」这一事实抹平（docs/45 P1-2）。
                "insufficient_ratio": (
                    round(n_insufficient / total, 4) if total else 0.0
                ),
                "temperature_delta": (
                    round(sum(delta_sums) / len(delta_sums), 1) if delta_sums else None
                ),
                "delta_count": len(delta_sums),
                "delta_insufficient": delta_missing,
                "delta_insufficient_ratio": (
                    round(delta_missing / len(items), 3) if items else 0.0
                ),
                "districts": districts_out,
                "readings": readings_out,
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
