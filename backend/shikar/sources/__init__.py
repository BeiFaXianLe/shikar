"""shikar 采集数据来源适配层。

对外导出全部公共类型与两种非爬取实现：

* :class:`MetricRecord` / :class:`Source` —— 公共契约；
* :class:`MockSource` / :func:`sample_records` —— 内存 Mock 来源与确定性样例；
* :class:`CsvSource` —— 本地手动导出 CSV 来源。

本包纯本地、零第三方依赖、无任何网络调用。
"""

from .base import MetricRecord, Source
from .csv_source import CsvSource
from .mock import MockSource, sample_records

__all__ = [
    "MetricRecord",
    "Source",
    "MockSource",
    "sample_records",
    "CsvSource",
]
