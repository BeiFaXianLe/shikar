"""shikar.pipeline 计算管线单元测试。

覆盖本单验收边界：

* ``ingest`` 后行数正确、``mom4`` 计算正确（线性价格可验证）；
* ``temperature`` ∈ ``[0, 100]``、``stage`` 属于合法集合；
* 数据不足（<3 期 / 跨度 <12 月 / 缺口 >1 年）时 ``temperature`` 与
  ``stage`` 为 ``None``，``mom4`` 照常推导（docs/04 §2.2）；
* ``mom4`` 按**日历差**对齐（28±7 天），跨年 / 两周间隔不得连成「4 周动量」；
* 缺失分项不进温度加权，也不会编造中性 50（docs/45 P0-1）；
* 重复 ``ingest`` 幂等（行数不增）；
* 两个小区数据互不影响。

全部使用 ``:memory:`` 库，无任何第三方依赖。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from shikar import storage
from shikar.pipeline import ingest, recompute
from shikar.sources.base import MetricRecord

VALID_STAGES = {"冰点", "回暖", "温和", "升温", "过热", "降温", "下行"}


def _week_at(i: int, start: date = date(2024, 1, 1), step_days: int = 30) -> str:
    """第 ``i`` 期（默认从 2024-01-01 每 30 天一期，接进「4 周」窗口）。"""
    d = start + timedelta(days=step_days * i)
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


@pytest.fixture()
def conn():
    """返回一个已建表的 ``:memory:`` 连接，测试结束自动关闭。"""
    c = storage.connect()
    storage.init_db(c)
    try:
        yield c
    finally:
        c.close()


def _make_records(
    city: str,
    district: str,
    name: str,
    weeks: int = 8,
    base_price: float = 50000.0,
    step: float = 1000.0,
    step_days: int = 30,
    xiaoqu_id: str = "",
) -> list[MetricRecord]:
    """构造 ``weeks`` 期的线性价格记录（默认 30 天一期，落在 28±7 天窗口内）。

    周期 30 天既能让 ``mom4`` 对齐到上一期，又能用 ``weeks=14`` 凑出
    跨度 ≥365 天的合格序列；期次不足时按 docs/04 §2.2 只给读数、不给温度。
    """
    records: list[MetricRecord] = []
    for i in range(weeks):
        records.append(
            MetricRecord(
                city=city,
                district=district,
                plate="板块",
                xiaoqu_id=xiaoqu_id,
                xiaoqu_name=name,
                week=_week_at(i, step_days=step_days),
                price=base_price + step * i,
                supply=100 + i,
                vol30=10 + i,
                bargain=0.10 - 0.005 * i,
                heat=50.0 + i,
            )
        )
    return records


def test_ingest_counts(conn):
    """ingest 后小区数、指标行数正确。"""
    records = _make_records("北京", "朝阳", "望京A", weeks=8)
    result = ingest(conn, records)
    assert result == {"xiaoqu": 1, "metrics": 8}

    series = storage.get_metric_series(conn, "北京|朝阳|望京A")
    assert len(series) == 8
    # 小区主数据写入正确。
    xqs = storage.list_xiaoqu(conn)
    assert len(xqs) == 1
    assert xqs[0]["name"] == "望京A"
    assert xqs[0]["city"] == "北京"


def test_mom4_correct_linear_price(conn):
    """4 周动量按日历对齐：30 天一期时 mom4 = price_t / price_{t-1} - 1。"""
    base, step = 50000.0, 1000.0
    records = _make_records("北京", "朝阳", "望京A", weeks=8, base_price=base, step=step)
    ingest(conn, records)

    series = storage.get_metric_series(conn, "北京|朝阳|望京A")
    # 首期没有 ~28 天前的基准，mom4 为空；之后每期都对齐到上一期。
    assert series[0]["mom4"] is None
    for i in range(1, 8):
        expected = (base + step * i) / (base + step * (i - 1)) - 1.0
        assert series[i]["mom4"] == pytest.approx(expected)


def test_mom4_null_across_year_gap(conn):
    """2019-02 + 2026-W46：相隔 7 年，不得被当成「4 周动量」。

    docs/45 P0-2 的现场：262 行 mom4 里 0 行跨度 ≤ 35 天，170 行跨度 > 400 天。
    """
    storage.upsert_xiaoqu(conn, {"id": "北京|朝阳|跨年", "city": "北京",
                                 "district": "朝阳", "name": "跨年"})
    for week, price in (("2019-02", 60000.0), ("2026-W46", 49480.0)):
        storage.upsert_metric(conn, {
            "xiaoqu_id": "北京|朝阳|跨年", "week": week, "price": price,
        })
    recompute(conn, xiaoqu_ids=["北京|朝阳|跨年"])
    rows = storage.get_metric_series(conn, "北京|朝阳|跨年")
    assert [r["mom4"] for r in rows] == [None, None]


def test_mom4_null_when_only_two_weeks_apart(conn):
    """两次快照相隔 14 天：凑不出「4 周前」基准 → mom4 为空，不拿第 1 条硬算。"""
    storage.upsert_xiaoqu(conn, {"id": "北京|朝阳|两周", "city": "北京",
                                 "district": "朝阳", "name": "两周"})
    for week, price in (("2026-W46", 50000.0), ("2026-W48", 52000.0)):
        storage.upsert_metric(conn, {
            "xiaoqu_id": "北京|朝阳|两周", "week": week, "price": price,
        })
    recompute(conn, xiaoqu_ids=["北京|朝阳|两周"])
    rows = storage.get_metric_series(conn, "北京|朝阳|两周")
    assert [r["mom4"] for r in rows] == [None, None]


def test_mom4_available_with_four_weekly_snapshots(conn):
    """连续 4 周快照：第 4 周能对齐到 21 天前（窗口下限）→ mom4 非空。

    这是「已经跑了四周数据却算不出 mom4」的回归点：旧口径要求「前第 4 条
    记录」（5 期），4 周数据永远凑不够。
    """
    storage.upsert_xiaoqu(conn, {"id": "北京|朝阳|四周", "city": "北京",
                                 "district": "朝阳", "name": "四周"})
    weeks = ["2026-W45", "2026-W46", "2026-W47", "2026-W48"]
    prices = [50000.0, 51000.0, 52000.0, 53000.0]
    for week, price in zip(weeks, prices):
        storage.upsert_metric(conn, {
            "xiaoqu_id": "北京|朝阳|四周", "week": week, "price": price,
        })
    recompute(conn, xiaoqu_ids=["北京|朝阳|四周"])
    rows = storage.get_metric_series(conn, "北京|朝阳|四周")
    assert rows[0]["mom4"] is None
    assert rows[1]["mom4"] is None  # 7 天，太近
    assert rows[2]["mom4"] is None  # 14 天，太近
    assert rows[3]["mom4"] == pytest.approx(53000.0 / 50000.0 - 1.0)


def test_mom4_null_when_current_price_is_zero(conn):
    """当期价为 0 属缺失值：不能算出 mom4 = -100%（否则会把自己钉成「降温最快」）。"""
    storage.upsert_xiaoqu(conn, {"id": "北京|朝阳|零价", "city": "北京",
                                 "district": "朝阳", "name": "零价"})
    for week, price in (("2026-W45", 50000.0), ("2026-W48", 0.0)):
        storage.upsert_metric(conn, {
            "xiaoqu_id": "北京|朝阳|零价", "week": week, "price": price,
        })
    recompute(conn, xiaoqu_ids=["北京|朝阳|零价"])
    rows = storage.get_metric_series(conn, "北京|朝阳|零价")
    assert [r["mom4"] for r in rows] == [None, None]


def test_mom4_window_boundaries(conn):
    """窗口边界：35 天（28+7）内可算，42 天不可（三个小区各自独立）。"""
    cases = {
        "边界35": (("2026-W40", 50000.0), ("2026-W45", 51000.0)),  # 35 天
        "边界42": (("2026-W40", 50000.0), ("2026-W46", 51000.0)),  # 42 天
    }
    for name, rows_in in cases.items():
        xid = f"北京|朝阳|{name}"
        storage.upsert_xiaoqu(conn, {"id": xid, "city": "北京", "district": "朝阳", "name": name})
        for week, price in rows_in:
            storage.upsert_metric(conn, {"xiaoqu_id": xid, "week": week, "price": price})
        recompute(conn, xiaoqu_ids=[xid])

    within = storage.get_metric_series(conn, "北京|朝阳|边界35")
    outside = storage.get_metric_series(conn, "北京|朝阳|边界42")
    assert within[0]["mom4"] is None
    assert within[1]["mom4"] == pytest.approx(51000.0 / 50000.0 - 1.0)
    assert [r["mom4"] for r in outside] == [None, None]


def test_mom4_orders_mixed_month_and_week_rows_by_date(conn):
    """``"2026-12"`` 的字符串序在 ``"2026-W46"`` 之前，但日期在后：必须按日期对齐。"""
    xid = "北京|朝阳|混排"
    storage.upsert_xiaoqu(conn, {"id": xid, "city": "北京", "district": "朝阳", "name": "混排"})
    # 2026-W46 = 2026-11-09，2026-12 = 2026-12-01，相差 22 天（窗口内）。
    for week, price in (("2026-12", 52000.0), ("2026-W46", 51000.0)):
        storage.upsert_metric(conn, {"xiaoqu_id": xid, "week": week, "price": price})
    recompute(conn, xiaoqu_ids=[xid])
    by_week = {r["week"]: r["mom4"] for r in storage.get_metric_series(conn, xid)}
    assert by_week["2026-W46"] is None
    assert by_week["2026-12"] == pytest.approx(52000.0 / 51000.0 - 1.0)


def test_temperature_range_and_stage(conn):
    """temperature ∈ [0,100]，stage 属于合法集合；样本不足的早期期次不给温度。"""
    records = _make_records("北京", "朝阳", "望京A", weeks=14)
    ingest(conn, records)

    series = storage.get_metric_series(conn, "北京|朝阳|望京A")
    # 前 3 期各分项样本都不足 4 个 → 不给温度；之后才进入加权。
    for row in series[:3]:
        assert row["temperature"] is None and row["stage"] is None
    for row in series[3:]:
        assert row["temperature"] is not None
        assert 0.0 <= row["temperature"] <= 100.0
        assert row["stage"] in VALID_STAGES


def test_two_periods_keep_mom4_but_no_temperature(conn):
    """2 期（相隔 30 天）：mom4 是可算的读数，但门槛不足 → 不给温度。"""
    records = _make_records("北京", "海淀", "中关村B", weeks=2)
    ingest(conn, records)

    series = storage.get_metric_series(conn, "北京|海淀|中关村B")
    assert len(series) == 2
    assert series[0]["mom4"] is None
    assert series[1]["mom4"] is not None
    for row in series:
        assert row["temperature"] is None
        assert row["stage"] is None


def test_single_period_has_no_temperature(conn):
    """1 期：有读数，但没有任何结论字段。"""
    ingest(conn, _make_records("北京", "东城", "单期A", weeks=1))
    row = storage.get_metric_series(conn, "北京|东城|单期A")[0]
    assert row["temperature"] is None and row["stage"] is None
    assert row["price"] is not None


def test_two_periods_long_span_still_insufficient(conn):
    """2 期即使跨度 7 年，期数不够 → 仍不给温度。"""
    storage.upsert_xiaoqu(conn, {"id": "北京|西城|两期B", "city": "北京",
                                 "district": "西城", "name": "两期B"})
    weeks = ["2019-02", "2026-W46"]
    for w in weeks:
        storage.upsert_metric(conn, {
            "xiaoqu_id": "北京|西城|两期B", "week": w, "price": 50000.0,
        })
    recompute(conn, xiaoqu_ids=["北京|西城|两期B"])
    assert all(r["temperature"] is None for r in storage.get_metric_series(conn, "北京|西城|两期B"))


def test_three_periods_short_span_insufficient(conn):
    """3 期但跨度 <365 天 → 不足。"""
    storage.upsert_xiaoqu(conn, {"id": "北京|西城|短跨度C", "city": "北京",
                                 "district": "西城", "name": "短跨度C"})
    for i in range(3):
        storage.upsert_metric(conn, {
            "xiaoqu_id": "北京|西城|短跨度C", "week": _week_at(i, step_days=7),
            "price": 50000.0 + i * 100,
        })
    recompute(conn, xiaoqu_ids=["北京|西城|短跨度C"])
    assert all(r["temperature"] is None for r in storage.get_metric_series(conn, "北京|西城|短跨度C"))


def test_gap_larger_than_one_year_insufficient(conn):
    """3 期、首末跨度够，但中间缺口 >365 天 → 不足。"""
    storage.upsert_xiaoqu(conn, {"id": "北京|西城|大缺口D", "city": "北京",
                                 "district": "西城", "name": "大缺口D"})
    weeks = ["2023-W01", "2024-W30", "2026-W30"]
    for w in weeks:
        storage.upsert_metric(conn, {
            "xiaoqu_id": "北京|西城|大缺口D", "week": w, "price": 50000.0,
        })
    recompute(conn, xiaoqu_ids=["北京|西城|大缺口D"])
    assert all(r["temperature"] is None for r in storage.get_metric_series(conn, "北京|西城|大缺口D"))


def test_sufficient_but_no_usable_subscore_keeps_null_temperature(conn):
    """过门槛但没有任何可用分项（200 天一期、只有价格）→ 温度 NULL，不编造 50。

    docs/45 P0-1：旧口径把缺席的三个分项当 50 分，于是温度恒为 50.0；
    新口径只对可用分项重新归一化，一个都没有时直接不给温度。
    """
    storage.upsert_xiaoqu(conn, {"id": "北京|西城|合格E", "city": "北京",
                                 "district": "西城", "name": "合格E"})
    for i in range(3):
        storage.upsert_metric(conn, {
            "xiaoqu_id": "北京|西城|合格E", "week": _week_at(i, step_days=200),
            "price": 50000.0 + i * 500, "supply": 100.0, "vol30": 10.0,
        })
    recompute(conn, xiaoqu_ids=["北京|西城|合格E"])
    rows = storage.get_metric_series(conn, "北京|西城|合格E")
    assert all(r["temperature"] is None for r in rows)
    assert all(r["stage"] is None for r in rows)


def test_sufficient_monthly_series_gets_real_temperature(conn):
    """14 期、30 天一期（跨度 390 天）→ 过门槛且有**真实分布**的温度（非恒 50）。"""
    prices = [50000.0, 50500.0, 52000.0, 51500.0, 53000.0, 52800.0, 54000.0,
              53700.0, 55200.0, 54900.0, 56000.0, 55700.0, 57000.0, 56600.0]
    records = [
        MetricRecord(
            city="北京", district="西城", plate="板块", xiaoqu_id="",
            xiaoqu_name="月产合格", week=_week_at(i), price=p,
            supply=100.0 + i, vol30=None, bargain=None, heat=None,
        )
        for i, p in enumerate(prices)
    ]
    ingest(conn, records)

    series = storage.get_metric_series(conn, "北京|西城|月产合格")
    temps = [r["temperature"] for r in series if r["temperature"] is not None]
    assert temps, "合格且能算 mom4 的序列不应没有温度"
    assert all(0.0 <= t <= 100.0 for t in temps)
    # 价涨跌交替 → 百分位有差异，不再全部是 50.0。
    assert len({round(t, 4) for t in temps}) > 1
    assert any(abs(t - 50.0) > 0.01 for t in temps)


def test_recompute_after_ingest_history_keeps_null(conn):
    """生产路径回归：ingest_history 写入 2 期后，recompute 不得补出 50 分温度。

    事故形态：build_db 已按门槛置空，refresh.sh 随后跑 recompute_metrics，
    若 recompute 没有同一门槛，会把全库重新填成中性 50。
    """
    storage.upsert_xiaoqu(conn, {
        "id": "北京|朝阳|历史两期", "city": "北京", "district": "朝阳", "name": "历史两期",
    })
    for week, price in (("2019-02", 60000.0), ("2026-W46", 49480.0)):
        storage.upsert_metric(conn, {
            "xiaoqu_id": "北京|朝阳|历史两期", "week": week, "price": price,
            "temperature": None, "stage": None,
        })
    recompute(conn)
    recompute(conn)  # 幂等：再跑一次结果不变
    rows = storage.get_metric_series(conn, "北京|朝阳|历史两期")
    assert len(rows) == 2
    assert all(r["temperature"] is None for r in rows)
    assert all(r["stage"] is None for r in rows)


def test_recompute_idempotent_for_sufficient_series(conn):
    """合格序列：两次 recompute 结果一致且能算出温度。"""
    records = _make_records("北京", "西城", "合格F", weeks=14)
    ingest(conn, records)
    first = [(r["week"], r["temperature"], r["stage"]) for r in storage.get_metric_series(conn, "北京|西城|合格F")]
    recompute(conn)
    second = [(r["week"], r["temperature"], r["stage"]) for r in storage.get_metric_series(conn, "北京|西城|合格F")]
    assert first == second
    assert any(t is not None for _, t, _ in first)


def test_ingest_idempotent(conn):
    """重复 ingest 同一批记录行数不增。"""
    records = _make_records("北京", "朝阳", "望京A", weeks=8)
    first = ingest(conn, records)
    second = ingest(conn, records)
    assert first == second == {"xiaoqu": 1, "metrics": 8}

    series = storage.get_metric_series(conn, "北京|朝阳|望京A")
    assert len(series) == 8
    assert len(storage.list_xiaoqu(conn)) == 1


def test_two_xiaoqu_independent(conn):
    """两个小区数据互不影响（各自的 mom4 / 归一化独立）。"""
    # 小区 A：价格上升；小区 B：价格下降。
    a = _make_records("北京", "朝阳", "望京A", weeks=8, base_price=50000.0, step=1000.0)
    b = _make_records("天津", "和平", "和平C", weeks=8, base_price=30000.0, step=-500.0)
    ingest(conn, a + b)

    assert len(storage.list_xiaoqu(conn)) == 2
    sa = storage.get_metric_series(conn, "北京|朝阳|望京A")
    sb = storage.get_metric_series(conn, "天津|和平|和平C")
    assert len(sa) == 8
    assert len(sb) == 8

    # A 价格上涨 → mom4 > 0；B 价格下降 → mom4 < 0。
    assert sa[-1]["mom4"] > 0
    assert sb[-1]["mom4"] < 0

    # 归一化以各自小区自身历史为样本：A 的 mom4 归一化与 B 独立。
    # 只重算 A，B 的派生值不应改变。
    before_b = [(r["week"], r["mom4"], r["temperature"], r["stage"]) for r in sb]
    recompute(conn, xiaoqu_ids=["北京|朝阳|望京A"])
    after_b = [
        (r["week"], r["mom4"], r["temperature"], r["stage"])
        for r in storage.get_metric_series(conn, "天津|和平|和平C")
    ]
    assert before_b == after_b


def test_recompute_returns_rowcount(conn):
    """recompute 返回更新的行数。"""
    records = _make_records("北京", "朝阳", "望京A", weeks=8)
    ingest(conn, records)
    n = recompute(conn, xiaoqu_ids=["北京|朝阳|望京A"])
    assert n == 8

