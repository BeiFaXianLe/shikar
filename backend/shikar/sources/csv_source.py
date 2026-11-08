"""手动导出的 CSV 数据来源。

用于读取用户**手动导出 / 公开数据集**生成的 CSV 文件，按表头映射为
:class:`~shikar.sources.base.MetricRecord`。本模块**只读本地文件**，不发起任何
网络请求，属于合规的「非爬取」来源。

表头与字段约定：

* 必填列：``city`` / ``district`` / ``xiaoqu_id`` / ``xiaoqu_name`` / ``week``；
* 可选列：``plate`` / ``price`` / ``supply`` / ``vol30`` / ``bargain`` / ``heat``；
* 未知的额外列会被忽略；
* 数值列（``price`` / ``supply`` / ``vol30`` / ``bargain`` / ``heat``）缺失或为空串
  时按 ``None`` 处理，否则按数值解析（``supply`` / ``vol30`` 解析为整数，其余为
  浮点数）。

错误处理：

* 缺任一必填列 → 抛 ``ValueError``（信息包含缺失列名）；
* 数值解析失败 → 抛 ``ValueError``（信息包含行号与列名）；
* 空文件（仅表头或完全为空）→ 返回 ``[]``。
"""

from __future__ import annotations

import csv
from typing import Callable, Optional

from .base import MetricRecord

__all__ = ["CsvSource"]

# 必填列，缺一即报错。
_REQUIRED_COLUMNS = ("city", "district", "xiaoqu_id", "xiaoqu_name", "week")

# 数值列及其解析函数；缺失或空串时视为 None。
_NUMERIC_COLUMNS: dict[str, Callable[[str], object]] = {
    "price": float,
    "supply": int,
    "vol30": int,
    "bargain": float,
    "heat": float,
}

# 文本可选列；缺失或空串时视为 None。
_OPTIONAL_TEXT_COLUMNS = ("plate",)


class CsvSource:
    """从本地 UTF-8 CSV 文件读取指标记录。

    满足 :class:`~shikar.sources.base.Source` 契约。每次 ``fetch`` 都重新读取
    文件，因此文件更新后再次 ``fetch`` 即可反映最新内容。
    """

    name = "csv"

    def __init__(self, path: str) -> None:
        """创建 CSV 来源。

        :param path: CSV 文件路径（UTF-8 编码）。
        """
        self.path = path

    def fetch(self) -> list[MetricRecord]:
        """读取并解析 CSV，返回记录列表。

        :return: :class:`MetricRecord` 列表；仅表头或空文件时返回 ``[]``。
        :raises ValueError: 缺少必填列，或某行数值列解析失败。
        """
        with open(self.path, "r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)

            # 完全为空的文件连表头都没有，按「空文件」返回 []。
            fieldnames = reader.fieldnames
            if not fieldnames:
                return []

            missing = [c for c in _REQUIRED_COLUMNS if c not in fieldnames]
            if missing:
                raise ValueError(
                    f"CSV 缺少必填列: {', '.join(missing)} "
                    f"(文件: {self.path})"
                )

            records: list[MetricRecord] = []
            # 行号从数据区第一行起算为 1，便于用户定位。
            # DictReader 的 line_num 含表头，这里用数据行序号更直观。
            for row_no, row in enumerate(reader, start=1):
                records.append(self._row_to_record(row, row_no))
            return records

    def _row_to_record(self, row: dict[str, Optional[str]], row_no: int) -> MetricRecord:
        """把一行 CSV 字典转换为 :class:`MetricRecord`。

        :param row: ``csv.DictReader`` 产出的单行字典。
        :param row_no: 数据行序号（从 1 起），用于错误信息。
        :return: 解析后的记录。
        :raises ValueError: 数值列解析失败。
        """
        values: dict[str, object] = {}
        for col in _REQUIRED_COLUMNS:
            values[col] = (row.get(col) or "").strip()

        for col in _OPTIONAL_TEXT_COLUMNS:
            raw = (row.get(col) or "").strip()
            values[col] = raw if raw != "" else None

        for col, parser in _NUMERIC_COLUMNS.items():
            raw = (row.get(col) or "").strip()
            if raw == "":
                values[col] = None
                continue
            try:
                values[col] = parser(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"CSV 第 {row_no} 行第 '{col}' 列数值解析失败: {raw!r}"
                ) from exc

        return MetricRecord(**values)  # type: ignore[arg-type]
