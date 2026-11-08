"""采集数据来源适配层的公共契约。

本模块定义两个核心类型，供各类``Source``实现共用：

* :class:`MetricRecord` —— 单条小区周度指标记录的数据载体（``dataclass``）；
* :class:`Source` —— 数据来源的抽象``Protocol``（结构性子类型，无需继承）。

设计原则：

* **仅用标准库**：只依赖 ``dataclasses`` / ``typing``，不引入任何第三方包；
* **零 IO、零网络**：本模块纯粹描述数据结构与接口，不进行任何读写；
* **数值可缺省**：除定位记录所必需的文本字段外，所有指标值均为
  ``Optional`` 且默认 ``None``，便于表达「该周该项数据缺失」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

__all__ = ["MetricRecord", "Source"]


@dataclass
class MetricRecord:
    """单条小区周度指标记录。

    一条记录唯一定位到「某小区 × 某一周」，其余字段为该周的各项指标。
    指标类字段全部可选（默认 ``None``），缺失时表示该来源未提供该指标。

    :ivar city: 城市名，例如 ``"北京"``。
    :ivar district: 区县名，例如 ``"朝阳"``。
    :ivar plate: 板块名，例如 ``"望京"``；可选。
    :ivar xiaoqu_id: 小区唯一 id（稳定标识）。
    :ivar xiaoqu_name: 小区名称。
    :ivar week: 所属周（如 ``"2024-W01"`` 或 ``"2024-01-08"``，按来源约定）。
    :ivar price: 均价（元/平米）。
    :ivar supply: 在售房源量。
    :ivar vol30: 近 30 天成交量。
    :ivar bargain: 议价空间 / 砍价幅度。
    :ivar heat: 热度值。
    """

    city: str
    district: str
    plate: Optional[str] = None
    xiaoqu_id: str = ""
    xiaoqu_name: str = ""
    week: str = ""
    price: Optional[float] = None
    supply: Optional[int] = None
    vol30: Optional[int] = None
    bargain: Optional[float] = None
    heat: Optional[float] = None


@runtime_checkable
class Source(Protocol):
    """数据来源的抽象接口。

    任何实现了 ``name`` 属性与 ``fetch`` 方法的对象都满足本契约（结构化
    子类型），无需显式继承。``runtime_checkable`` 使其可用于
    ``isinstance(obj, Source)`` 的结构性检查。

    ``fetch`` 应当：

    * 返回一个 :class:`MetricRecord` 列表，且不抛异常（读取失败等应由实现
      自行转换为 ``ValueError`` 等明确异常）；
    * 不发起任何网络请求。
    """

    name: str

    def fetch(self) -> list[MetricRecord]:
        """拉取该来源的全部指标记录。

        :return: :class:`MetricRecord` 列表；无数据时返回空列表。
        """
        ...
