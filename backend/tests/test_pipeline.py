"""shikar.pipeline 计算管线单元测试。

覆盖本单验收边界：

* ``ingest`` 后行数正确、``mom4`` 计算正确（线性价格可验证）；
* ``temperature`` ∈ ``[0, 100]``、``stage`` 属于合法集合；
* 历史不足（前 1~2 周）时 ``temperature`` 合理且 ``stage`` 合法；
* 重复 ``ingest`` 幂等（行数不增）；
* 两个小区数据互不影响。

全部使用 ``:memory:`` 库，无任何第三方依赖。
"""

from __future__ import annotations

import pytest

from shikar import storage
from shikar.pipeline import ingest, recompute
from shikar.sources.base import MetricRecord

VALID_STAGES = {"冰点", "回暖", "温和", "升温", "过热", "降温", "下行"}


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
    xiaoqu_id: str = "",
) -> list[MetricRecord]:
    """构造 ``weeks`` 周的线性价格记录，用于验证 mom4 与温度范围。"""
    records: list[MetricRecord] = []
    for i in range(weeks):
        records.append(
            MetricRecord(
                city=city,
                district=district,
                plate="板块",
                xiaoqu_id=xiaoqu_id,
                xiaoqu_name=name,
                week=f"2024-W{i + 1:02d}",
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
    """线性价格下 mom4 应等于 price_t / price_{t-4} - 1。"""
    base, step = 50000.0, 1000.0
    records = _make_records("北京", "朝阳", "望京A", weeks=8, base_price=base, step=step)
    ingest(conn, records)

    series = storage.get_metric_series(conn, "北京|朝阳|望京A")
    # 前 4 条记录（索引 0..3）不足 4 条历史，mom4 应为 None。
    for i in range(4):
        assert series[i]["mom4"] is None

    for i in range(4, 8):
        expected = (base + step * i) / (base + step * (i - 4)) - 1.0
        assert series[i]["mom4"] == pytest.approx(expected)


def test_temperature_range_and_stage(conn):
    """temperature ∈ [0,100]，stage 属于合法集合。"""
    records = _make_records("北京", "朝阳", "望京A", weeks=12)
    ingest(conn, records)

    series = storage.get_metric_series(conn, "北京|朝阳|望京A")
    for row in series:
        assert row["temperature"] is not None
        assert 0.0 <= row["temperature"] <= 100.0
        assert row["stage"] in VALID_STAGES


def test_short_history(conn):
    """历史不足（1~2 周）时 temperature 合理且 stage 合法。"""
    records = _make_records("北京", "海淀", "中关村B", weeks=2)
    ingest(conn, records)

    series = storage.get_metric_series(conn, "北京|海淀|中关村B")
    assert len(series) == 2
    for row in series:
        assert row["temperature"] is not None
        assert 0.0 <= row["temperature"] <= 100.0
        assert row["stage"] in VALID_STAGES
        # 不足 4 条，mom4 应为 None。
        assert row["mom4"] is None


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
