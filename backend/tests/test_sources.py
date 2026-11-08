"""shikar.sources 采集适配层单元测试。

覆盖本单定义的边界：

* ``MockSource`` 原样返回、且与入参列表解耦；
* ``sample_records`` 确定性（两次调用相等）且条数 >= 6，覆盖京津两城多区；
* ``CsvSource`` 正常读取（``tmp_path`` 造文件）、缺必填列抛 ``ValueError``、
  空文件返回 ``[]``、额外列忽略、可选/数值列空串按 ``None``、数值非法抛
  ``ValueError``（含行号与列名）；
* 两个实现都满足 ``Source`` 契约（``name`` + ``fetch``）。

全部只依赖标准库，无任何网络调用。
"""

from __future__ import annotations

import pytest

from shikar.sources import (
    CsvSource,
    MetricRecord,
    MockSource,
    Source,
    sample_records,
)


# --------------------------------------------------------------------------- #
# MetricRecord
# --------------------------------------------------------------------------- #
class TestMetricRecord:
    def test_defaults_are_none(self):
        rec = MetricRecord(city="北京", district="朝阳")
        assert rec.plate is None
        assert rec.price is None
        assert rec.supply is None
        assert rec.vol30 is None
        assert rec.bargain is None
        assert rec.heat is None

    def test_full_construction(self):
        rec = MetricRecord(
            city="北京",
            district="朝阳",
            plate="望京",
            xiaoqu_id="X1",
            xiaoqu_name="某小区",
            week="2024-W01",
            price=50000.0,
            supply=10,
            vol30=3,
            bargain=0.1,
            heat=60.0,
        )
        assert rec.xiaoqu_id == "X1"
        assert rec.price == 50000.0
        assert rec.supply == 10


# --------------------------------------------------------------------------- #
# MockSource
# --------------------------------------------------------------------------- #
class TestMockSource:
    def test_returns_records_as_is(self):
        recs = sample_records()[:3]
        src = MockSource(recs)
        assert src.fetch() == recs

    def test_fetch_returns_new_list(self):
        recs = sample_records()[:2]
        src = MockSource(recs)
        out = src.fetch()
        out.append(MetricRecord(city="x", district="y"))
        # 追加不应污染来源内部或原始入参。
        assert len(src.fetch()) == 2
        assert len(recs) == 2

    def test_init_copies_input_list(self):
        recs = sample_records()[:2]
        src = MockSource(recs)
        recs.append(MetricRecord(city="x", district="y"))
        assert len(src.fetch()) == 2

    def test_empty_records(self):
        assert MockSource([]).fetch() == []


# --------------------------------------------------------------------------- #
# sample_records
# --------------------------------------------------------------------------- #
class TestSampleRecords:
    def test_at_least_six(self):
        assert len(sample_records()) >= 6

    def test_deterministic(self):
        assert sample_records() == sample_records()

    def test_covers_two_cities_multiple_districts(self):
        recs = sample_records()
        cities = {r.city for r in recs}
        districts = {r.district for r in recs}
        assert {"北京", "天津"} <= cities
        assert len(districts) >= 4

    def test_records_have_required_fields(self):
        for r in sample_records():
            assert r.city and r.district
            assert r.xiaoqu_id and r.xiaoqu_name and r.week


# --------------------------------------------------------------------------- #
# CsvSource
# --------------------------------------------------------------------------- #
_HEADER = "city,district,plate,xiaoqu_id,xiaoqu_name,week,price,supply,vol30,bargain,heat\n"


def _write_csv(tmp_path, body: str, name: str = "data.csv") -> str:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return str(p)


class TestCsvSourceBasic:
    def test_reads_records(self, tmp_path):
        body = (
            _HEADER
            + "北京,朝阳,望京,X1,望京新城,2024-W01,78000,42,15,0.08,72\n"
            + "天津,和平,小白楼,X2,小白楼公寓,2024-W01,42000,36,11,0.10,48\n"
        )
        path = _write_csv(tmp_path, body)
        recs = CsvSource(path).fetch()
        assert len(recs) == 2
        assert recs[0] == MetricRecord(
            city="北京",
            district="朝阳",
            plate="望京",
            xiaoqu_id="X1",
            xiaoqu_name="望京新城",
            week="2024-W01",
            price=78000.0,
            supply=42,
            vol30=15,
            bargain=0.08,
            heat=72.0,
        )
        assert recs[1].city == "天津"
        assert recs[1].supply == 36

    def test_numeric_types(self, tmp_path):
        body = _HEADER + "北京,朝阳,望京,X1,某小区,2024-W01,78000,42,15,0.08,72\n"
        rec = CsvSource(_write_csv(tmp_path, body)).fetch()[0]
        assert isinstance(rec.price, float)
        assert isinstance(rec.supply, int)
        assert isinstance(rec.vol30, int)
        assert isinstance(rec.bargain, float)
        assert isinstance(rec.heat, float)


class TestCsvSourceEmptyAndOptional:
    def test_empty_file_returns_empty(self, tmp_path):
        assert CsvSource(_write_csv(tmp_path, "")).fetch() == []

    def test_only_header_returns_empty(self, tmp_path):
        assert CsvSource(_write_csv(tmp_path, _HEADER)).fetch() == []

    def test_optional_numeric_empty_is_none(self, tmp_path):
        # supply / vol30 / bargain / heat / price 全部留空。
        body = "city,district,xiaoqu_id,xiaoqu_name,week,price,supply,vol30,bargain,heat\n" + (
            "北京,朝阳,X1,某小区,2024-W01,,,,,\n"
        )
        rec = CsvSource(_write_csv(tmp_path, body)).fetch()[0]
        assert rec.price is None
        assert rec.supply is None
        assert rec.vol30 is None
        assert rec.bargain is None
        assert rec.heat is None

    def test_missing_optional_column_is_none(self, tmp_path):
        # 完全不提供 plate 及所有数值列（非必填）。
        body = "city,district,xiaoqu_id,xiaoqu_name,week\n北京,朝阳,X1,某小区,2024-W01\n"
        rec = CsvSource(_write_csv(tmp_path, body)).fetch()[0]
        assert rec.plate is None
        assert rec.price is None
        assert rec.city == "北京"


class TestCsvSourceErrors:
    def test_missing_required_column_raises_with_name(self, tmp_path):
        # 缺 xiaoqu_name。
        body = "city,district,xiaoqu_id,week\n北京,朝阳,X1,2024-W01\n"
        path = _write_csv(tmp_path, body)
        with pytest.raises(ValueError) as exc:
            CsvSource(path).fetch()
        assert "xiaoqu_name" in str(exc.value)

    def test_missing_multiple_required_columns(self, tmp_path):
        body = "city,xiaoqu_id,week\n北京,X1,2024-W01\n"
        with pytest.raises(ValueError) as exc:
            CsvSource(_write_csv(tmp_path, body)).fetch()
        msg = str(exc.value)
        assert "district" in msg and "xiaoqu_name" in msg

    def test_invalid_numeric_raises_with_row_and_column(self, tmp_path):
        body = _HEADER + "北京,朝阳,望京,X1,某小区,2024-W01,notanumber,42,15,0.08,72\n"
        path = _write_csv(tmp_path, body)
        with pytest.raises(ValueError) as exc:
            CsvSource(path).fetch()
        msg = str(exc.value)
        assert "1" in msg          # 数据行序号
        assert "price" in msg      # 出错的列名

    def test_invalid_int_column_raises(self, tmp_path):
        body = _HEADER + "北京,朝阳,望京,X1,某小区,2024-W01,78000,abc,15,0.08,72\n"
        with pytest.raises(ValueError) as exc:
            CsvSource(_write_csv(tmp_path, body)).fetch()
        assert "supply" in str(exc.value)

    def test_error_on_second_data_row_reports_row_2(self, tmp_path):
        body = (
            _HEADER
            + "北京,朝阳,望京,X1,某小区,2024-W01,78000,42,15,0.08,72\n"
            + "天津,和平,小白楼,X2,某小区,2024-W01,bad,36,11,0.10,48\n"
        )
        with pytest.raises(ValueError) as exc:
            CsvSource(_write_csv(tmp_path, body)).fetch()
        assert "2" in str(exc.value)
        assert "price" in str(exc.value)


class TestCsvSourceExtraColumns:
    def test_extra_columns_ignored(self, tmp_path):
        body = (
            "city,district,plate,xiaoqu_id,xiaoqu_name,week,price,supply,vol30,bargain,heat,extra_col\n"
            + "北京,朝阳,望京,X1,某小区,2024-W01,78000,42,15,0.08,72,ignoreme\n"
        )
        rec = CsvSource(_write_csv(tmp_path, body)).fetch()[0]
        assert rec.xiaoqu_id == "X1"
        assert rec.price == 78000.0


# --------------------------------------------------------------------------- #
# Source 契约
# --------------------------------------------------------------------------- #
class TestSourceContract:
    def test_mock_is_source(self):
        assert isinstance(MockSource(sample_records()), Source)

    def test_csv_is_source(self, tmp_path):
        path = _write_csv(tmp_path, _HEADER)
        assert isinstance(CsvSource(path), Source)

    def test_both_expose_name_and_fetch(self):
        for src in (MockSource([]), CsvSource("whatever.csv")):
            assert isinstance(src.name, str) and src.name
            assert callable(src.fetch)
