"""shikar 计算管线：把来源记录入库并计算温度 / 阶段等派生指标。

实现来源：``docs/03-PRD.md`` 第 6 节「指标与算法定义」，本单为「计算管线」：

* :func:`ingest` —— 把来源产出的 :class:`~shikar.sources.base.MetricRecord`
  写入库（小区主数据 + 周度指标），随后对受影响小区调用 :func:`recompute`；
* :func:`recompute` —— 对指定（或全部）小区按周升序重算 ``mom4`` /
  ``absorb`` / ``temperature`` / ``stage``，并回写。

设计原则：

* **仅用 Python 标准库**：只依赖 ``sqlite3`` / ``typing`` / ``math`` 等，不引入
  任何第三方包；
* **连接即用**：所有函数接收外部传入的 ``sqlite3.Connection``，不持有全局状态；
* **幂等**：写入走 ``storage.upsert_xiaoqu`` / ``storage.upsert_metric``
  （``ON CONFLICT ... DO UPDATE``），``recompute`` 只做 UPDATE，重复执行不产生
  重复行。

归一化说明（PRD §6.2）：百分位归一化思想遵循 §6.2，但 **MVP 阶段以「小区自身
历史」为样本**（每个小区各指标取截至当周最近 ``window`` 条已有数值），城市内跨
小区方案待数据充足再切换。这一点同样体现在 :func:`_normalize_series` 与
:func:`recompute` 的 docstring 中。
"""

from __future__ import annotations

import math
import sqlite3
from typing import Iterable, Mapping, Optional, Sequence

from shikar import scoring, storage  # noqa: F401  (storage 供直接调用)
from shikar.sources.base import MetricRecord

__all__ = ["ingest", "recompute"]

# 分项名称（与 storage 列名、scoring.compute_temperature 的入参键一致）。
_SUBSCORES: tuple[str, ...] = ("mom4", "absorb", "bargain", "heat")


def _make_xiaoqu_id(city: str, district: str, xiaoqu_name: str) -> str:
    """由城市 / 区县 / 小区名拼接稳定 id。

    格式固定为 ``f"{city}|{district}|{xiaoqu_name}"``，保证同一输入永远得到
    同一 id（幂等的前提）。

    :param city: 城市名。
    :param district: 区县名。
    :param xiaoqu_name: 小区名称。
    :return: 稳定的小区 id。
    """
    return f"{city}|{district}|{xiaoqu_name}"


def _safe_float(value: object) -> Optional[float]:
    """把任意对象转为 ``float``，``None`` 或不可转换时返回 ``None``。"""
    if value is None:
        return None
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def ingest(
    conn: sqlite3.Connection,
    records: Iterable[MetricRecord],
    weights: Optional[Mapping[str, float]] = None,
) -> dict:
    """把一批来源记录写入库，并重算受影响小区的派生指标。

    对每条 :class:`~shikar.sources.base.MetricRecord`：

    1. ``xiaoqu_id`` 为空（或仅空白）时，用
       ``f"{city}|{district}|{xiaoqu_name}"`` 生成稳定 id；
    2. ``storage.upsert_xiaoqu`` 写入 / 更新小区主数据（``plate``、
       ``name=xiaoqu_name`` 等）；
    3. ``storage.upsert_metric`` 写入 / 更新周度指标（``price`` / ``supply`` /
       ``vol30`` / ``bargain`` / ``heat``），``mom4`` / ``mom13`` / ``absorb`` /
       ``temperature`` / ``stage`` 先留空（由 :func:`recompute` 计算）。

    写入完成后，对本次涉及的每个 ``xiaoqu_id`` 调用 :func:`recompute` 计算派生
    指标。重复调用同一批记录是幂等的（行数不增）。

    :param conn: 已打开的 SQLite 连接（需先 ``storage.init_db``）。
    :param records: 待写入的指标记录可迭代对象。
    :param weights: 可选温度权重覆盖，透传给 :func:`recompute`。
    :return: 形如 ``{"xiaoqu": 小区数, "metrics": 指标行数}`` 的统计字典；
        其中 ``xiaoqu`` 为影响的去重小区数，``metrics`` 为写入的记录条数。
    """
    affected: dict[str, None] = {}
    metric_rows = 0

    for rec in records:
        xid = (rec.xiaoqu_id or "").strip()
        if not xid:
            xid = _make_xiaoqu_id(rec.city, rec.district, rec.xiaoqu_name)

        storage.upsert_xiaoqu(
            conn,
            {
                "id": xid,
                "city": rec.city,
                "district": rec.district,
                "plate": rec.plate,
                "name": rec.xiaoqu_name,
            },
        )

        storage.upsert_metric(
            conn,
            {
                "xiaoqu_id": xid,
                "week": rec.week,
                "price": rec.price,
                "supply": rec.supply,
                "vol30": rec.vol30,
                "bargain": rec.bargain,
                "heat": rec.heat,
                # 派生指标先留空，交给 recompute 计算。
                "mom4": None,
                "mom13": None,
                "absorb": None,
                "temperature": None,
                "stage": None,
            },
        )

        affected[xid] = None
        metric_rows += 1

    if affected:
        recompute(conn, xiaoqu_ids=list(affected.keys()), weights=weights)

    return {"xiaoqu": len(affected), "metrics": metric_rows}


def _percentile_series(values: Sequence[Optional[float]], window: int, min_history: int) -> list[float]:
    """对单个小区单个分项做「自身历史」百分位归一化。

    对第 ``i`` 个值，取其**截至当周（含）最近 ``window`` 条**的已有（非缺失）
    数值作为样本，调用 :func:`scoring.percentile_normalize`。当 ``values[i]``
    缺失或样本数 ``< min_history`` 时返回中性值 ``50.0``。

    :param values: 按周升序的分项原始值序列（可含 ``None``）。
    :param window: 采样窗口（最近多少条）。
    :param min_history: 最少样本数，不足则回退 ``50.0``。
    :return: 与 ``values`` 等长的 0–100 归一化值列表。
    """
    out: list[float] = []
    for i, value in enumerate(values):
        if value is None:
            out.append(50.0)
            continue
        start = max(0, i - window + 1)
        samples = [v for v in values[start : i + 1] if v is not None]
        if len(samples) < min_history:
            out.append(50.0)
            continue
        out.append(scoring.percentile_normalize(value, samples))
    return out


def recompute(
    conn: sqlite3.Connection,
    xiaoqu_ids: Optional[Iterable[str]] = None,
    weights: Optional[Mapping[str, float]] = None,
    window: int = 52,
    min_history: int = 4,
) -> int:
    """重算指定（或全部）小区的派生指标并回写数据库。

    对每个小区，按 ``week`` 升序取其全部历史行，逐行计算：

    * ``mom4`` = ``price_t / price_{t-4周} - 1``（取该小区前第 4 条记录的
      ``price``；不足 4 条则为 ``None``）；
    * ``absorb`` = ``vol30 / supply``（``supply`` 为空或 0 则为 ``None``）；
    * 对 ``mom4 / absorb / bargain / heat`` 四个分项各自归一化：以该小区
      **截至当周（含）最近 ``window`` 条**的已有数值为样本，调用
      :func:`scoring.percentile_normalize`；``value`` 缺失或样本数
      ``< min_history`` 时取 ``50.0``；
    * ``temperature = scoring.compute_temperature({mom4, absorb, bargain, heat})``
      （分项均为上一步的归一化值）；
    * ``stage = scoring.classify_stage(temperature, mom4 if mom4 is not None
      else 0.0)``；
    * ``UPDATE xiaoqu_metric_weekly SET mom4=?, absorb=?, temperature=?, stage=?
      WHERE xiaoqu_id=? AND week=?``。

    归一化遵循 PRD §6.2 的百分位思想；**MVP 先以「小区自身历史」为样本**（城市
    内跨小区方案待数据充足再切换）。

    :param conn: 已打开的 SQLite 连接。
    :param xiaoqu_ids: 待重算的小区 id 可迭代对象；``None`` 表示全部小区。
    :param weights: 可选温度权重覆盖，透传给
        :func:`scoring.compute_temperature`。
    :param window: 归一化采样窗口（最近多少条），默认 52。
    :param min_history: 归一化最少样本数，默认 4。
    :return: 实际更新（UPDATE）的行数。
    """
    if xiaoqu_ids is None:
        ids = [r["id"] for r in storage.list_xiaoqu(conn)]
    else:
        ids = list(dict.fromkeys(xiaoqu_ids))

    updated = 0
    for xid in ids:
        series = storage.get_metric_series(conn, xid)
        n = len(series)
        if n == 0:
            continue

        prices = [_safe_float(r.get("price")) for r in series]
        supplies = [_safe_float(r.get("supply")) for r in series]
        vols = [_safe_float(r.get("vol30")) for r in series]
        bargains = [_safe_float(r.get("bargain")) for r in series]
        heats = [_safe_float(r.get("heat")) for r in series]

        # mom4：price_t / price_{t-4} - 1（前第 4 条记录，即索引 i-4）。
        mom4s: list[Optional[float]] = []
        for i in range(n):
            if i >= 4 and prices[i] is not None and prices[i - 4] not in (None, 0.0):
                mom4s.append(prices[i] / prices[i - 4] - 1.0)  # type: ignore[operator]
            else:
                mom4s.append(None)

        # absorb：vol30 / supply（supply 空或 0 时为 None）。
        absorbs: list[Optional[float]] = []
        for i in range(n):
            v = vols[i]
            s = supplies[i]
            if v is None or s is None or s == 0.0:
                absorbs.append(None)
            else:
                absorbs.append(v / s)

        # 四个分项各自「截至当周最近 window 条」百分位归一化。
        n_mom4 = _percentile_series(mom4s, window, min_history)
        n_absorb = _percentile_series(absorbs, window, min_history)
        n_bargain = _percentile_series(bargains, window, min_history)
        n_heat = _percentile_series(heats, window, min_history)

        for i, row in enumerate(series):
            temperature = scoring.compute_temperature(
                {
                    "mom4": n_mom4[i],
                    "absorb": n_absorb[i],
                    "bargain": n_bargain[i],
                    "heat": n_heat[i],
                },
                weights,
            )
            mom4_val = mom4s[i]
            stage = scoring.classify_stage(
                temperature, mom4_val if mom4_val is not None else 0.0
            )

            cur = conn.execute(
                "UPDATE xiaoqu_metric_weekly "
                "SET mom4 = ?, absorb = ?, temperature = ?, stage = ? "
                "WHERE xiaoqu_id = ? AND week = ?",
                (mom4_val, absorbs[i], temperature, stage, xid, row["week"]),
            )
            updated += cur.rowcount

    conn.commit()
    return updated
