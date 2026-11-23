"""shikar 温度 / 阶段 / 信号 纯函数算法模块。

实现来源：``docs/03-PRD.md`` 第 6 节「指标与算法定义」。本模块严格保持为
**纯函数**：无 IO、无网络、无全局可变状态，输入到输出可确定、可回测。
不包含任何硬编码的城市 / 小区数据。

关键公式引用：

* §6.1 基础指标定义（price / mom4 / supply / vol30 / absorb / bargain / heat）。
* §6.2 归一化：每个指标在城市范围内做 52 周滚动百分位，映射到 0–100。
* §6.3 温度：``0.40*n(mom4) + 0.25*n(absorb) + 0.20*(100-n(bargain)) + 0.15*n(heat)``；
  分项缺失时按**可用分项**重新归一化权重（见 :func:`compute_temperature`）。
* §6.4 趋势阶段（温度水位 × 动量方向）。
* §6.5 右侧信号（买入 / 风险）。
"""

from __future__ import annotations

from typing import Mapping, Sequence

__all__ = [
    "percentile_normalize",
    "compute_temperature",
    "classify_stage",
    "detect_signals",
]

# §6.3 默认温度权重（可在配置中覆盖）。
DEFAULT_WEIGHTS: dict[str, float] = {
    "mom4": 0.40,
    "absorb": 0.25,
    "bargain": 0.20,
    "heat": 0.15,
}

# §6.2 历史样本量下限：低于该值时无法估计分位，返回中性值 50.0。
MIN_HISTORY_SAMPLES = 4


def percentile_normalize(value: float, history: Sequence[float]) -> float:
    """把 ``value`` 在 ``history`` 历史样本中的分位映射到 0–100。

    采用「中位秩」（mid-rank）百分位，避免对并列值产生偏置：

    * ``below`` 为严格小于 ``value`` 的样本数；
    * ``equal`` 为与 ``value`` 相等的样本数；
    * 返回 ``(below + 0.5 * equal) / len(history) * 100``。

    因此当 ``value`` 小于全部历史样本时返回 ``0.0``，大于全部样本时返回
    ``100.0``，处于样本中位数附近时约返回 ``50.0``。

    历史样本不足（``len(history) < MIN_HISTORY_SAMPLES``，即 < 4）时无法可靠
    估计分位，按 PRD §6.2 的「样本不足即中性」精神返回 ``50.0``。

    :param value: 待归一化的指标值。
    :param history: 该指标的历史样本序列（例如最近 52 周），可为空。
    :return: 0–100 的分位值。
    """
    n = len(history)
    if n < MIN_HISTORY_SAMPLES:
        return 50.0

    below = 0
    equal = 0
    for sample in history:
        if sample < value:
            below += 1
        elif sample == value:
            equal += 1

    return (below + 0.5 * equal) / n * 100.0


def compute_temperature(
    metrics: Mapping[str, float | None],
    weights: Mapping[str, float] | None = None,
) -> float:
    """按 PRD §6.3 加权求和计算 0–100 的温度值（缺失分项按可用权重归一化）。

    公式（权重来源：PRD §6.3，可在配置中覆盖）::

        temperature = Σ(w_i * s_i) / Σ(w_i)      i 取「可用分项」

    其中 ``s_i`` 是分项 ``i`` 的 0–100 已归一化值，``bargain`` 取反向
    ``100 - n(bargain)``（议价空间越大温度越低）。

    设计约定（纯函数、无隐式状态）：``metrics`` 为指标名到数值的映射，其四项
    分项 ``n(mom4)`` / ``n(absorb)`` / ``n(bargain)`` / ``n(heat)`` 由调用方先经
    :func:`percentile_normalize` 计算后以 0–100 的 **已归一化值** 填入键
    ``"mom4"`` / ``"absorb"`` / ``"bargain"`` / ``"heat"``。

    **缺失分项不参与加权，也不记中性 50 分**：键不存在或值为 ``None`` 的分项
    直接从分子分母中剔除，权重在剩余分项上重新归一化。旧口径把缺失分项当
    50 分，而 ``absorb`` / ``bargain`` / ``heat`` 三项在现网数据里全库为空，
    导致 75% 权重永久中性、温度实际恒为 50.0（见 docs/45 报告 P0-1）。

    四个分项全部可用时，本函数与旧口径逐值等价（``Σw = 1``）。全部分项都
    不可用时返回中性 ``50.0``；调用方应把这种情形视为「无温度」（写 ``None``），
    需要区分时请先判断 ``metrics`` 里是否有可用分项。

    :param metrics: 已归一化的分项值（0–100）映射；缺失分项可省略或传 ``None``。
    :param weights: 可选权重覆盖，缺省用 :data:`DEFAULT_WEIGHTS`。
    :return: 裁剪到 0–100 的温度。
    """
    w = dict(DEFAULT_WEIGHTS)
    if weights is not None:
        w.update(weights)

    total_weight = 0.0
    weighted_sum = 0.0
    for name, weight in w.items():
        value = metrics.get(name)
        if value is None or weight == 0.0:
            continue
        score = 100.0 - float(value) if name == "bargain" else float(value)
        weighted_sum += weight * score
        total_weight += weight

    if total_weight == 0.0:
        # 没有任何可用分项：保持中性回退，由调用方决定是否当成「无温度」。
        return 50.0

    return max(0.0, min(100.0, weighted_sum / total_weight))


def classify_stage(temperature: float, mom4: float) -> str:
    """按 PRD §6.4 依据温度水位与动量方向判定趋势阶段。

    阶段规则（边界 25 / 55 / 80，条件取自 PRD §6.4）::

        | 阶段 | 条件                                   |
        |------|----------------------------------------|
        | 冰点 | 温度 < 25 且 mom4 <= 0                 |
        | 回暖 | 温度 < 25 且 mom4 > 0                  |
        | 温和 | 25 <= 温度 < 55 且 mom4 >= 0           |
        | 下行 | 25 <= 温度 < 55 且 mom4 < 0            |
        | 升温 | 55 <= 温度 < 80 且 mom4 > 0            |
        | 降温 | 55 <= 温度 < 80 且 mom4 < 0            |
        | 过热 | 温度 >= 80                             |

    ``mom4 == 0`` 视为「无正动量」：低温区归 冰点，中温区归 温和，55–80 区间
    既不升温也不降温，按「温和」处理（缺乏上行 momentum 时不标记升温）；温度
    >= 80 时无论动量方向均为 过热。

    :param temperature: 温度值（0–100）。
    :param mom4: 4 周价格动量。
    :return: 阶段中文名。
    """
    if temperature >= 80.0:
        return "过热"

    if temperature >= 55.0:
        # 55 <= 温度 < 80
        if mom4 < 0:
            return "降温"
        if mom4 > 0:
            return "升温"
        return "温和"

    if temperature >= 25.0:
        # 25 <= 温度 < 55
        if mom4 < 0:
            return "下行"
        return "温和"

    # 温度 < 25
    if mom4 > 0:
        return "回暖"
    return "冰点"


def _mean(values: Sequence[float]) -> float:
    """返回 ``values`` 的算术平均；空序列返回 0.0。"""
    if not values:
        return 0.0
    return sum(values) / len(values)


def detect_signals(series: Sequence[Mapping[str, float]]) -> list[dict]:
    """按 PRD §6.5 检测「买入 / 风险」右侧信号事件。

    输入 ``series`` 是按时间升序排列的每周指标快照序列，每项至少含：
    ``price`` / ``vol30`` / ``bargain`` / ``supply`` / ``absorb``。

    规则（PRD §6.5）::

        * 买入信号：price 站上 13 周均线 且 vol30 > 近 13 周 vol30 均值 * 1.2
                   且 bargain 连续 2 周收窄。
        * 风险信号：price 跌破 13 周均线 且 supply 环比 +15%
                   且 absorb 连续 2 周下降。

    实现说明：

    * 「13 周均线」取含当周在内的最近 13 个 ``price`` 的均值（不足 13 周时用
      现有全部样本，样本不足 2 周则不判定）；
    * 「bargain 连续 2 周收窄」指 ``bargain[t] < bargain[t-1] < bargain[t-2]``，
      即连续两周环比下降（议价空间收窄）；
    * 「absorb 连续 2 周下降」指 ``absorb[t] < absorb[t-1] < absorb[t-2]``；
    * 「supply 环比 +15%」指 ``supply[t] >= supply[t-1] * 1.15``；
    * 每个事件记录触发时的指标快照（price / vol30 / bargain / supply / absorb），
      保证可解释。返回值按时间升序，每条形如
      ``{"index": int, "type": "买入"|"风险", "reason": {...快照...}}``。

    :param series: 按时间升序的每周指标快照序列。
    :return: 触发的事件列表（可能为空）。
    """
    events: list[dict] = []
    n = len(series)
    if n < 3:
        return events

    prices = [float(row.get("price", 0.0)) for row in series]
    vols = [float(row.get("vol30", 0.0)) for row in series]
    bargains = [float(row.get("bargain", 0.0)) for row in series]
    supplies = [float(row.get("supply", 0.0)) for row in series]
    absorbs = [float(row.get("absorb", 0.0)) for row in series]

    for i in range(2, n):
        window_start = max(0, i - 12)  # 含当周的最近 13 周
        price_ma13 = _mean(prices[window_start : i + 1])
        vol_ma13 = _mean(vols[window_start : i + 1])

        snapshot = {
            "price": prices[i],
            "vol30": vols[i],
            "bargain": bargains[i],
            "supply": supplies[i],
            "absorb": absorbs[i],
        }

        # 买入信号：站上均线 且 放量 且 bargain 连续 2 周收窄。
        bargain_narrowing = bargains[i] < bargains[i - 1] < bargains[i - 2]
        if (
            prices[i] > price_ma13
            and vol_ma13 > 0.0
            and vols[i] > vol_ma13 * 1.2
            and bargain_narrowing
        ):
            events.append(
                {
                    "index": i,
                    "type": "买入",
                    "reason": dict(snapshot),
                }
            )

        # 风险信号：跌破均线 且 supply 环比 +15% 且 absorb 连续 2 周下降。
        supply_jump = supplies[i - 1] > 0.0 and supplies[i] >= supplies[i - 1] * 1.15
        absorb_falling = absorbs[i] < absorbs[i - 1] < absorbs[i - 2]
        if prices[i] < price_ma13 and supply_jump and absorb_falling:
            events.append(
                {
                    "index": i,
                    "type": "风险",
                    "reason": dict(snapshot),
                }
            )

    return events
