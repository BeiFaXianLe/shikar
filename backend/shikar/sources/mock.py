"""内存 Mock 数据来源。

提供两种用法：

* :class:`MockSource` —— 用调用方给定的记录列表构造，``fetch`` 原样返回；
* :func:`sample_records` —— 返回一份**确定性**的京津样例数据，便于本地开发
  与测试在无真实数据时跑通全链路。

本模块**无 IO、无网络**，仅做内存数据装配。
"""

from __future__ import annotations

from .base import MetricRecord

__all__ = ["MockSource", "sample_records"]


class MockSource:
    """把一组内存记录当作数据来源。

    满足 :class:`~shikar.sources.base.Source` 契约：具备 ``name`` 属性与
    ``fetch`` 方法。``fetch`` 原样返回构造时传入的列表（返回新列表对象，
    避免调用方意外修改内部引用）。
    """

    name = "mock"

    def __init__(self, records: list[MetricRecord]) -> None:
        """创建内存 Mock 来源。

        :param records: 要提供的记录列表。
        """
        self._records = list(records)

    def fetch(self) -> list[MetricRecord]:
        """返回构造时传入的记录列表的副本。

        :return: :class:`MetricRecord` 列表（原样内容）。
        """
        return list(self._records)


def sample_records() -> list[MetricRecord]:
    """返回一份确定性的京津小区样例数据。

    覆盖北京、天津两座城市及多个区县，共 8 条记录；每次调用返回内容一致的
    新列表，可用于测试断言与本地演示。

    :return: 固定的 :class:`MetricRecord` 列表（长度 >= 6）。
    """
    return [
        MetricRecord(
            city="北京",
            district="朝阳",
            plate="望京",
            xiaoqu_id="BJ-CY-001",
            xiaoqu_name="望京新城",
            week="2024-W01",
            price=78000.0,
            supply=42,
            vol30=15,
            bargain=0.08,
            heat=72.0,
        ),
        MetricRecord(
            city="北京",
            district="朝阳",
            plate="望京",
            xiaoqu_id="BJ-CY-002",
            xiaoqu_name="花家地南里",
            week="2024-W01",
            price=65000.0,
            supply=30,
            vol30=9,
            bargain=0.12,
            heat=55.0,
        ),
        MetricRecord(
            city="北京",
            district="海淀",
            plate="中关村",
            xiaoqu_id="BJ-HD-001",
            xiaoqu_name="知春里",
            week="2024-W01",
            price=112000.0,
            supply=18,
            vol30=6,
            bargain=0.05,
            heat=88.0,
        ),
        MetricRecord(
            city="北京",
            district="海淀",
            plate="五道口",
            xiaoqu_id="BJ-HD-002",
            xiaoqu_name="华清嘉园",
            week="2024-W02",
            price=108000.0,
            supply=22,
            vol30=8,
            bargain=0.06,
            heat=81.0,
        ),
        MetricRecord(
            city="天津",
            district="和平",
            plate="小白楼",
            xiaoqu_id="TJ-HP-001",
            xiaoqu_name="小白楼公寓",
            week="2024-W01",
            price=42000.0,
            supply=36,
            vol30=11,
            bargain=0.10,
            heat=48.0,
        ),
        MetricRecord(
            city="天津",
            district="南开",
            plate="鼓楼",
            xiaoqu_id="TJ-NK-001",
            xiaoqu_name="鼓楼里",
            week="2024-W01",
            price=38000.0,
            supply=28,
            vol30=10,
            bargain=0.14,
            heat=44.0,
        ),
        MetricRecord(
            city="天津",
            district="河西",
            plate="梅江",
            xiaoqu_id="TJ-HX-001",
            xiaoqu_name="梅江云水园",
            week="2024-W02",
            price=35000.0,
            supply=45,
            vol30=13,
            bargain=0.11,
            heat=52.0,
        ),
        MetricRecord(
            city="北京",
            district="西城",
            plate="金融街",
            xiaoqu_id="BJ-XC-001",
            xiaoqu_name="丰汇园",
            week="2024-W01",
            price=145000.0,
            supply=12,
            vol30=4,
            bargain=0.04,
            heat=90.0,
        ),
    ]
