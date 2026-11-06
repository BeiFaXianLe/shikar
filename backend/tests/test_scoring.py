"""shikar.scoring 纯函数单元测试。

覆盖 PRD §6 的边界：温度 0 / 100、阶段临界值（25/55/80 两侧）、mom4=0、
历史样本不足、信号「触发」与「不触发」两类。
"""

from __future__ import annotations

import pytest

from shikar.scoring import (
    DEFAULT_WEIGHTS,
    MIN_HISTORY_SAMPLES,
    classify_stage,
    compute_temperature,
    detect_signals,
    percentile_normalize,
)


# --------------------------------------------------------------------------- #
# percentile_normalize
# --------------------------------------------------------------------------- #
class TestPercentileNormalize:
    def test_insufficient_history_returns_neutral(self):
        # 样本 < 4 一律 50.0（含空序列与刚好 3 个）。
        assert percentile_normalize(1.0, []) == 50.0
        assert percentile_normalize(1.0, [1.0]) == 50.0
        assert percentile_normalize(1.0, [1.0, 2.0]) == 50.0
        assert percentile_normalize(1.0, [1.0, 2.0, 3.0]) == 50.0

    def test_min_history_boundary_is_4(self):
        assert MIN_HISTORY_SAMPLES == 4
        # 正好 4 个样本即开始估计，不再是中性值。
        history = [10.0, 20.0, 30.0, 40.0]
        assert percentile_normalize(40.0, history) == 87.5

    def test_value_below_all_returns_zero(self):
        assert percentile_normalize(-5.0, [1.0, 2.0, 3.0, 4.0]) == 0.0

    def test_value_above_all_returns_hundred(self):
        assert percentile_normalize(100.0, [1.0, 2.0, 3.0, 4.0]) == 100.0

    def test_mid_rank_equal_values(self):
        # 全部相等时 mid-rank 给 50.0。
        assert percentile_normalize(5.0, [5.0, 5.0, 5.0, 5.0]) == 50.0

    def test_known_percentile(self):
        history = [1.0, 2.0, 3.0, 4.0]
        # value=3.0：below=2（1,2），equal=1 -> (2+0.5)/4*100 = 62.5
        assert percentile_normalize(3.0, history) == 62.5

    def test_tied_values_mid_rank(self):
        history = [1.0, 2.0, 2.0, 2.0, 5.0]
        # value=2.0：below=1，equal=3 -> (1+1.5)/5*100 = 50.0
        assert percentile_normalize(2.0, history) == pytest.approx(50.0)


# --------------------------------------------------------------------------- #
# compute_temperature
# --------------------------------------------------------------------------- #
class TestComputeTemperature:
    def test_all_zero_components_clip_to_zero(self):
        # n(mom4)=0,n(absorb)=0,n(heat)=0，n(bargain)=100 -> 0.20*(100-100)=0。
        metrics = {"mom4": 0.0, "absorb": 0.0, "bargain": 100.0, "heat": 0.0}
        assert compute_temperature(metrics) == 0.0

    def test_all_hundred_components_clip_to_hundred(self):
        # n(mom4)=100,n(absorb)=100,n(heat)=100，n(bargain)=0 -> 20 + 100 分项满。
        metrics = {"mom4": 100.0, "absorb": 100.0, "bargain": 0.0, "heat": 100.0}
        assert compute_temperature(metrics) == 100.0

    def test_weights_sum_to_one(self):
        assert sum(DEFAULT_WEIGHTS.values()) == pytest.approx(1.0)

    def test_default_weighted_sum(self):
        # 全部中性 50 时温度应为 50，且 (100-n(bargain))=50。
        metrics = {"mom4": 50.0, "absorb": 50.0, "bargain": 50.0, "heat": 50.0}
        assert compute_temperature(metrics) == pytest.approx(50.0)

    def test_known_weighted_value(self):
        # 0.40*80 + 0.25*60 + 0.20*(100-40) + 0.15*20
        # = 32 + 15 + 12 + 3 = 62
        metrics = {"mom4": 80.0, "absorb": 60.0, "bargain": 40.0, "heat": 20.0}
        assert compute_temperature(metrics) == pytest.approx(62.0)

    def test_bargain_inverts(self):
        # 提高 bargain 归一值应降低温度（100 - n(bargain)）。
        low_bargain = compute_temperature(
            {"mom4": 0.0, "absorb": 0.0, "bargain": 0.0, "heat": 0.0}
        )
        high_bargain = compute_temperature(
            {"mom4": 0.0, "absorb": 0.0, "bargain": 100.0, "heat": 0.0}
        )
        assert low_bargain > high_bargain

    def test_custom_weights_override(self):
        # 完全覆盖默认权重：只用 mom4，其余权重 0。
        custom = {"mom4": 1.0, "absorb": 0.0, "bargain": 0.0, "heat": 0.0}
        metrics = {"mom4": 90.0, "absorb": 0.0, "bargain": 100.0, "heat": 0.0}
        assert compute_temperature(metrics, weights=custom) == pytest.approx(90.0)

    def test_empty_metrics_neutral(self):
        assert compute_temperature({}) == 50.0

    def test_missing_component_defaults_neutral(self):
        # 仅给 mom4，其余按 50 -> 0.40*100 + 0.25*50 + 0.20*50 + 0.15*50
        # = 40 + 12.5 + 10 + 7.5 = 70
        assert compute_temperature({"mom4": 100.0}) == pytest.approx(70.0)


# --------------------------------------------------------------------------- #
# classify_stage —— 边界 25 / 55 / 80 两侧 + mom4 符号
# --------------------------------------------------------------------------- #
class TestClassifyStage:
    @pytest.mark.parametrize(
        "temp,mom4,expected",
        [
            # 温度 < 25
            (0.0, -0.1, "冰点"),
            (24.9, 0.0, "冰点"),   # mom4=0 视为无正动量 -> 冰点
            (24.9, 0.1, "回暖"),
            (10.0, 5.0, "回暖"),
            # 边界 25 两侧
            (24.9, -0.1, "冰点"),
            (25.0, -0.1, "下行"),
            (25.0, 0.0, "温和"),
            (25.0, 0.1, "温和"),
            # 中温区 25–55
            (40.0, -0.1, "下行"),
            (40.0, 0.0, "温和"),
            (40.0, 0.1, "温和"),
            (54.9, -0.1, "下行"),
            (54.9, 0.0, "温和"),
            # 边界 55 两侧
            (55.0, 0.1, "升温"),
            (55.0, -0.1, "降温"),
            (55.0, 0.0, "温和"),   # 55–80 且 mom4=0 不升温不降温
            # 升温 / 降温区间
            (60.0, 0.1, "升温"),
            (60.0, -0.1, "降温"),
            (79.9, 0.1, "升温"),
            (79.9, -0.1, "降温"),
            # 边界 80 两侧
            (79.9, 1.0, "升温"),
            (80.0, 0.0, "过热"),
            (80.0, -1.0, "过热"),   # 过热不受动量方向影响
            (100.0, -5.0, "过热"),
        ],
    )
    def test_stage(self, temp, mom4, expected):
        assert classify_stage(temp, mom4) == expected

    def test_mom4_zero_low_band_is_ice(self):
        assert classify_stage(20.0, 0.0) == "冰点"

    def test_mom4_zero_mid_band_is_mild(self):
        assert classify_stage(45.0, 0.0) == "温和"


# --------------------------------------------------------------------------- #
# detect_signals —— 触发 / 不触发
# --------------------------------------------------------------------------- #
def _flat_series(n, price=100.0, vol=10.0, bargain=0.05, supply=100.0, absorb=0.1):
    return [
        {
            "price": price,
            "vol30": vol,
            "bargain": bargain,
            "supply": supply,
            "absorb": absorb,
        }
        for _ in range(n)
    ]


class TestDetectSignals:
    def test_too_short_returns_empty(self):
        assert detect_signals([]) == []
        assert detect_signals(_flat_series(2)) == []

    def test_no_signal_on_flat_series(self):
        assert detect_signals(_flat_series(20)) == []

    def test_buy_signal_triggered(self):
        # 构造：前若干周价格 100、vol 10；最后一周价格上冲、
        # vol 远高于均值、bargain 连续收窄。
        series = _flat_series(15, price=100.0, vol=10.0, bargain=0.10)
        # 倒数最后两周 bargain 逐步收窄，最后一周放量上冲。
        series[-2]["bargain"] = 0.08
        series[-1]["bargain"] = 0.06
        series[-1]["price"] = 120.0   # 站上 13 周均线
        series[-1]["vol30"] = 40.0    # > 均值*1.2
        events = detect_signals(series)
        buy = [e for e in events if e["type"] == "买入"]
        assert len(buy) == 1
        assert buy[0]["index"] == len(series) - 1
        # 快照完整
        reason = buy[0]["reason"]
        for key in ("price", "vol30", "bargain", "supply", "absorb"):
            assert key in reason
        assert reason["price"] == 120.0

    def test_buy_not_triggered_without_volume(self):
        series = _flat_series(15, price=100.0, vol=10.0, bargain=0.10)
        series[-2]["bargain"] = 0.08
        series[-1]["bargain"] = 0.06
        series[-1]["price"] = 120.0
        # vol 未放量
        series[-1]["vol30"] = 10.0
        assert [e for e in detect_signals(series) if e["type"] == "买入"] == []

    def test_buy_not_triggered_without_bargain_narrowing(self):
        series = _flat_series(15, price=100.0, vol=10.0, bargain=0.10)
        series[-1]["price"] = 120.0
        series[-1]["vol30"] = 40.0
        # bargain 未连续收窄（保持平）
        assert [e for e in detect_signals(series) if e["type"] == "买入"] == []

    def test_buy_not_triggered_below_ma(self):
        # 价格持续下跌后最后一周仍低于 13 周均线。
        series = _flat_series(15, price=100.0, vol=10.0, bargain=0.10)
        series[-2]["bargain"] = 0.08
        series[-1]["bargain"] = 0.06
        series[-1]["price"] = 90.0    # 低于均线
        series[-1]["vol30"] = 40.0
        assert [e for e in detect_signals(series) if e["type"] == "买入"] == []

    def test_risk_signal_triggered(self):
        series = _flat_series(15, price=100.0, vol=10.0, bargain=0.05,
                              supply=100.0, absorb=0.20)
        # absorb 连续两周下降
        series[-2]["absorb"] = 0.15
        series[-1]["absorb"] = 0.10
        # supply 环比 +15% 以上
        series[-1]["supply"] = 120.0
        # 价格跌破均线
        series[-1]["price"] = 80.0
        events = detect_signals(series)
        risk = [e for e in events if e["type"] == "风险"]
        assert len(risk) == 1
        assert risk[0]["index"] == len(series) - 1
        assert risk[0]["reason"]["supply"] == 120.0

    def test_risk_not_triggered_without_supply_jump(self):
        series = _flat_series(15, price=100.0, vol=10.0, bargain=0.05,
                              supply=100.0, absorb=0.20)
        series[-2]["absorb"] = 0.15
        series[-1]["absorb"] = 0.10
        series[-1]["supply"] = 105.0   # 未达 +15%
        series[-1]["price"] = 80.0
        assert [e for e in detect_signals(series) if e["type"] == "风险"] == []

    def test_risk_not_triggered_without_absorb_fall(self):
        series = _flat_series(15, price=100.0, vol=10.0, bargain=0.05,
                              supply=100.0, absorb=0.20)
        series[-1]["supply"] = 120.0
        series[-1]["price"] = 80.0
        # absorb 未连续下降
        assert [e for e in detect_signals(series) if e["type"] == "风险"] == []

    def test_risk_not_triggered_price_above_ma(self):
        series = _flat_series(15, price=100.0, vol=10.0, bargain=0.05,
                              supply=100.0, absorb=0.20)
        series[-2]["absorb"] = 0.15
        series[-1]["absorb"] = 0.10
        series[-1]["supply"] = 120.0
        series[-1]["price"] = 110.0    # 高于均线
        assert [e for e in detect_signals(series) if e["type"] == "风险"] == []

    def test_multiple_events_ordered(self):
        # 构造买入后接风险两个事件，确认按时间升序。
        series = _flat_series(20, price=100.0, vol=10.0, bargain=0.10,
                              supply=100.0, absorb=0.20)
        # 索引 15：买入触发
        series[14]["bargain"] = 0.09
        series[15]["bargain"] = 0.08
        series[15]["price"] = 130.0
        series[15]["vol30"] = 40.0
        # 索引 19：风险触发
        series[18]["absorb"] = 0.15
        series[19]["absorb"] = 0.10
        series[19]["supply"] = 120.0
        series[19]["price"] = 80.0
        events = detect_signals(series)
        assert [e["index"] for e in events] == sorted(e["index"] for e in events)
        types = {e["type"] for e in events}
        assert "买入" in types and "风险" in types
