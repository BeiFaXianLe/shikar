"""shikar 小区目录查询：为 ``GET /api/catalog`` 提供分页 + 过滤的纯函数。

本模块在既有数据层 ``shikar.storage`` 与空间层 ``shikar.subway`` 之上提供唯一
一个函数 :func:`query_catalog`，供 ``api`` 层做参数解析后调用。它把「浏览 1.7 万
小区」所需的筛选、搜索、分页与轻量聚合收拢在一处，避免 ``api`` 层拼 SQL。

设计原则（与 ``overview`` 一致）：

* **仅用 Python 标准库**：只依赖 ``sqlite3`` / ``math`` / ``typing`` 与同项目的
  ``subway`` 模块，不引入第三方包；
* **只读、无副作用**：只读 ``xiaoqu``、``xiaoqu_metric_weekly`` 与地铁关系表，
  不写库、不持有全局状态；
* **纯函数风格**：输入连接与参数，输出可 JSON 序列化的普通字典，便于测试。

过滤与分页规则：

* ``city`` / ``district`` 精确匹配；``q`` 名称包含匹配（大小写不敏感）；
* ``price_min`` / ``price_max``：按**最新一期**（``week`` 最大）的 ``price``
  过滤；``None`` 表示不限；区间左闭右闭；最小大于最大抛错；
* ``lines``（线路 id 列表，多线取 OR）/ ``station``（指定站点）/ ``subway_max``
  （阈值 km）/ ``subway_mode``（``station`` 距站 或 ``line`` 距线路走向）：由
  ``subway.filter_sql`` 生成子查询，取小区交集；未导入地铁数据却要求地铁筛选
  时抛出明确错误；
* ``sort``：``default`` / ``price_asc`` / ``price_desc`` / ``subway_distance``；
  一切排序都以 ``city,district,name,id`` 作为稳定次级顺序。

每个条目额外携带 ``features.subway``：命中条件下**距离最近的一条**线路信息
（线路名、站名、距离、口径），未命中或无地铁数据时为 ``None``。
"""

from __future__ import annotations

import math
import sqlite3
from typing import Optional

from . import subway

__all__ = ["query_catalog"]

#: ``page_size`` 默认值。
_DEFAULT_PAGE_SIZE = 50

#: ``page_size`` 上限。
_MAX_PAGE_SIZE = 200

#: 地铁查询阈值上限（km），与 ``subway.MAX_QUERY_RADIUS_KM`` 保持一致且更宽松
#: 地留出「距线路」场景；超出视为非法参数。
_MAX_SUBWAY_KM = 5.0


def _clamp_page(page: Optional[int]) -> int:
    """把 ``page`` 规整为 >= 1 的整数；``None`` 视为 1。"""
    if page is None:
        return 1
    return page if page >= 1 else 1


def _clamp_page_size(page_size: Optional[int]) -> int:
    """把 ``page_size`` 规整到 ``[1, 200]`` 区间；``None`` 取默认 50。"""
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
    """拼出基础 ``WHERE`` 子句与参数（不含前导 ``WHERE``，条件以 ``AND`` 连接）。"""
    clauses: list[str] = []
    params: list = []
    if city is not None:
        clauses.append("AND x.city = ?")
        params.append(city)
    if district is not None:
        clauses.append("AND x.district = ?")
        params.append(district)
    if q is not None and q != "":
        clauses.append("AND lower(x.name) LIKE lower(?)")
        params.append("%" + q + "%")
    return " ".join(clauses), params


def query_catalog(
    conn: sqlite3.Connection,
    city: Optional[str] = None,
    district: Optional[str] = None,
    q: Optional[str] = None,
    page: Optional[int] = 1,
    page_size: Optional[int] = _DEFAULT_PAGE_SIZE,
    *,
    price_min: Optional[float] = None,
    price_max: Optional[float] = None,
    price_week: Optional[str] = None,
    lines: Optional[list[str]] = None,
    station: Optional[str] = None,
    subway_mode: str = "station",
    subway_max: Optional[float] = None,
    sort: str = "default",
) -> dict:
    """按城市 / 区 / 关键词 / 价格 / 地铁条件过滤并分页查询小区目录。

    返回结构 ``{"total", "page", "page_size", "price_periods", "items": [ ... ]}``；
    每个 ``items`` 元素含 ``id`` / ``city`` / ``district`` / ``plate`` / ``name`` /
    ``data_points`` / ``latest_price`` / ``latest_week`` / ``latest_mom`` /
    ``features``（``{"subway": {...}|None}``）。

    价格期次口径（P1-03）：价格一律取各小区**最新一期**的 ``price``，且每条结果都带
    ``latest_week``（期次）；``price_is_current`` 标记该期次是否为全库最新一期。
    ``price_week`` 非空时，只统计/返回「最新一期恰为该期次」的小区，使价格过滤与排序
    在同一期次内比较，避免 2019 与 2026 混比。``price_periods`` 返回库中出现的期次
    及各自小区数，供前端展示「历史价」分组与期次选择。

    :param conn: 已打开的 SQLite 连接（只读使用）。
    :param city: 城市精确过滤；``None`` 不限。
    :param district: 区精确过滤；``None`` 不限。
    :param q: 小区名包含匹配（不区分大小写）；``None`` 或空串不限。
    :param page: 页码，从 1 开始；``None`` 或 < 1 视为 1。
    :param page_size: 每页条数，默认 50、上限 200；< 1 视为 1。
    :param price_min: 最新均价下限（元/㎡，含）；``None`` 不限。
    :param price_max: 最新均价上限（元/㎡，含）；``None`` 不限。
    :param price_week: 限定价格期次（如 ``2026-W46``）；``None`` 不限（跨期，结果标注期次）。
    :param lines: 选中地铁线路 id 列表（多线取 OR）；``None``/空 不限。
    :param station: 指定站点 id；给出时仅按「距站」口径。
    :param subway_mode: ``"station"``（距站）或 ``"line"``（距线路走向）。
    :param subway_max: 地铁距离阈值（km）；给出即启用地铁筛选。
    :param sort: 排序方式，见模块文档。
    :return: 分页结果字典。
    :raises ValueError: 价格/地铁阈值非法、口径或排序不支持、需要地铁筛选但
        未导入地铁数据、线路/站点非法。
    """
    for label, value in (
        ("最低均价", price_min), ("最高均价", price_max), ("地铁距离", subway_max)
    ):
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ValueError(f"{label}必须是有限非负数")
    if price_min is not None and price_max is not None and price_min > price_max:
        raise ValueError("最低均价不能超过最高均价")
    if subway_max is not None and not 0 < subway_max <= _MAX_SUBWAY_KM:
        raise ValueError(f"地铁距离必须大于 0 且不超过 {_MAX_SUBWAY_KM:g} km")
    if subway_mode not in ("station", "line"):
        raise ValueError("距离口径只能为 station/line")
    if sort not in ("default", "price_asc", "price_desc", "subway_distance"):
        raise ValueError("排序参数不支持")

    page_n = _clamp_page(page)
    size_n = _clamp_page_size(page_size)
    where_sql, params = _build_where(city, district, q)

    # 价格与期次：metrics 关联哪一期。
    # - 指定 price_week：直接关联「该期次」指标（是这期就参与，不因有更新一期而被漏），
    #   价格过滤/排序/来源均基于该期。
    # - 未指定：关联各小区「最新一期」作为当前参考价。
    # 该 JOIN 由 total 与分页共用；绝不把缺失期次填成别的期价格。
    has_price_week = price_week is not None and price_week != ""
    if has_price_week:
        from_sql = (
            " FROM xiaoqu x JOIN xiaoqu_metric_weekly m "
            "ON m.xiaoqu_id=x.id AND m.week=? "
        )
        # 期次参数在 total/分页两处都要在 WHERE 之前绑定（JOIN 里用），单列管理。
        metric_join_params = [price_week]
    else:
        from_sql = (
            " FROM xiaoqu x LEFT JOIN xiaoqu_metric_weekly m "
            "ON m.xiaoqu_id=x.id AND m.week=("
            "SELECT MAX(mm.week) FROM xiaoqu_metric_weekly mm WHERE mm.xiaoqu_id=x.id) "
        )
        metric_join_params = []

    if price_min is not None:
        where_sql += " AND m.price >= ?"
        params.append(price_min)
    if price_max is not None:
        where_sql += " AND m.price <= ?"
        params.append(price_max)

    # 地铁筛选：任一条件触发即启用；阈值缺省取默认 1 km（与前端默认一致）。
    active_subway = bool(lines or station or subway_max is not None)
    radius = subway_max if subway_max is not None else 1.0
    if active_subway:
        sub_sql, sub_params = subway.filter_sql(
            conn, city=city, lines=lines, station=station,
            mode=subway_mode, radius=radius,
        )
        where_sql += f" AND x.id IN ({sub_sql})"
        params += sub_params

    # 排序距离（用于 subway_distance 排序与次行展示）；无地铁表时置 NULL。
    has_subway = subway.available(conn)
    distance_sql = "NULL"
    distance_params: list = []
    if has_subway:
        if station:
            distance_sql = (
                "(SELECT ds.distance_km FROM community_subway_station ds "
                "WHERE ds.community_id=x.id AND ds.station_id=?)"
            )
            distance_params = [station]
        else:
            field = "station_distance_km" if subway_mode == "station" else "line_distance_km"
            distance_sql = f"(SELECT MIN(cs.{field}) FROM community_subway cs WHERE cs.community_id=x.id"
            if lines:
                distance_sql += f" AND cs.line_id IN ({','.join('?' for _ in lines)})"
                distance_params = list(lines)
            distance_sql += ")"

    stable = "x.city ASC, x.district ASC, x.name ASC, x.id ASC"
    if sort == "price_asc":
        order = "m.price IS NULL ASC, m.price ASC, " + stable
    elif sort == "price_desc":
        order = "m.price IS NULL ASC, m.price DESC, " + stable
    elif sort == "subway_distance":
        order = "sort_distance IS NULL ASC, sort_distance ASC, " + stable
    else:
        order = stable

    # 参数顺序：JOIN 参数 → WHERE 参数（total 与分页一致）。
    total_row = conn.execute(
        f"SELECT COUNT(*) AS n {from_sql} WHERE 1 = 1 {where_sql}",
        metric_join_params + params,
    ).fetchone()
    total = int(total_row["n"]) if total_row is not None else 0

    offset = (page_n - 1) * size_n
    rows = conn.execute(
        f"SELECT x.id,x.city,x.district,x.plate,x.name,m.week,m.price,m.mom4,"
        f"{distance_sql} AS sort_distance {from_sql} "
        f"WHERE 1 = 1 {where_sql} ORDER BY {order} LIMIT ? OFFSET ?",
        metric_join_params + distance_params + params + [size_n, offset],
    ).fetchall()

    row_ids = [r["id"] for r in rows]
    features = subway.matching_features(
        conn, row_ids, lines=lines, station=station, mode=subway_mode,
        radius=radius if active_subway else None,
    )
    # 全库最新期次（用于标记「当前价」vs「历史价」）。
    max_week_row = conn.execute("SELECT MAX(week) AS w FROM xiaoqu_metric_weekly").fetchone()
    max_week = max_week_row["w"] if max_week_row is not None else None
    # 库中期次分布（期次 → 该期指标行数），供前端展示/选择期次。
    period_rows = conn.execute(
        "SELECT week, COUNT(*) AS n FROM xiaoqu_metric_weekly GROUP BY week ORDER BY week DESC"
    ).fetchall()
    price_periods = [{"week": r["week"], "communities": int(r["n"])} for r in period_rows]
    items: list[dict] = []
    for row in rows:
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
                "latest_price": row["price"],
                "latest_week": row["week"],
                "latest_mom": row["mom4"],
                "price_is_current": row["week"] == max_week,
                # 价格来源：本行价格取自哪一期指标（逐行显示，明确口径）。
                "price_source_week": row["week"],
                "features": {"subway": features.get(row["id"])},
            }
        )

    return {
        "total": total,
        "page": page_n,
        "page_size": size_n,
        "price_periods": price_periods,
        "price_week": price_week if has_price_week else None,
        "items": items,
    }
