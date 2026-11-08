"""shikar.storage 数据层单元测试。

覆盖 PRD §6.6 数据字典对应的边界：

* 建表幂等（重复 ``init_db`` 不报错、表结构不变）；
* upsert 幂等（重复写不产生重复行、值被更新）；
* ``get_metric_series`` 的 week 升序与区间过滤；
* ``list_xiaoqu`` 按 city / district 过滤；
* signal 存取（reason 往返 JSON）；
* 外键约束（插入不存在的 ``xiaoqu_id`` 到 metric 应报错）。

全部使用 ``:memory:`` 库，无任何第三方依赖。
"""

from __future__ import annotations

import sqlite3

import pytest

from shikar.storage import (
    add_signal,
    connect,
    get_metric_series,
    init_db,
    list_signals,
    list_xiaoqu,
    upsert_metric,
    upsert_xiaoqu,
)


@pytest.fixture()
def conn():
    """返回一个已建表的 ``:memory:`` 连接，测试结束自动关闭。"""
    c = connect()
    init_db(c)
    try:
        yield c
    finally:
        c.close()


def _xiaoqu(xid="X1", city="北京", district="朝阳", **extra):
    row = {
        "id": xid,
        "city": city,
        "district": district,
        "plate": "望京",
        "name": f"小区{xid}",
        "alias": None,
        "lng": 116.48,
        "lat": 39.99,
        "build_year": 2005,
        "households": 1200,
    }
    row.update(extra)
    return row


def _metric(xid="X1", week="2024-W01", price=50000.0, **extra):
    row = {
        "xiaoqu_id": xid,
        "week": week,
        "price": price,
        "mom4": 0.01,
        "mom13": 0.02,
        "supply": 30,
        "vol30": 12,
        "absorb": 0.4,
        "bargain": 0.1,
        "heat": 60.0,
        "temperature": 55.0,
        "stage": "上涨",
    }
    row.update(extra)
    return row


# --------------------------------------------------------------------------- #
# 建表幂等
# --------------------------------------------------------------------------- #
class TestInitDb:
    def test_init_is_idempotent(self, conn):
        # 再次调用不应报错，也不改变表集合。
        init_db(conn)
        init_db(conn)
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"xiaoqu", "xiaoqu_metric_weekly", "signal_event", "source_snapshot"} <= names

    def test_indexes_created(self, conn):
        idx = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_xiaoqu_city_district" in idx
        assert "idx_metric_weekly_xiaoqu_week" in idx

    def test_foreign_keys_pragma_on(self, conn):
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


# --------------------------------------------------------------------------- #
# upsert 幂等
# --------------------------------------------------------------------------- #
class TestUpsertXiaoqu:
    def test_insert_then_update_no_duplicate(self, conn):
        upsert_xiaoqu(conn, _xiaoqu(households=1000))
        upsert_xiaoqu(conn, _xiaoqu(households=2222))
        rows = list_xiaoqu(conn)
        assert len(rows) == 1
        assert rows[0]["households"] == 2222

    def test_multiple_ids(self, conn):
        upsert_xiaoqu(conn, _xiaoqu("X1"))
        upsert_xiaoqu(conn, _xiaoqu("X2"))
        assert len(list_xiaoqu(conn)) == 2


class TestUpsertMetric:
    def test_insert_then_update_no_duplicate(self, conn):
        upsert_xiaoqu(conn, _xiaoqu())
        upsert_metric(conn, _metric(price=50000.0))
        upsert_metric(conn, _metric(price=60000.0))
        series = get_metric_series(conn, "X1")
        assert len(series) == 1
        assert series[0]["price"] == 60000.0

    def test_upsert_preserves_other_weeks(self, conn):
        upsert_xiaoqu(conn, _xiaoqu())
        upsert_metric(conn, _metric(week="2024-W01", price=1.0))
        upsert_metric(conn, _metric(week="2024-W02", price=2.0))
        upsert_metric(conn, _metric(week="2024-W01", price=9.0))
        series = get_metric_series(conn, "X1")
        assert [r["week"] for r in series] == ["2024-W01", "2024-W02"]
        assert series[0]["price"] == 9.0


# --------------------------------------------------------------------------- #
# series 排序与区间过滤
# --------------------------------------------------------------------------- #
class TestGetMetricSeries:
    def _seed(self, conn):
        upsert_xiaoqu(conn, _xiaoqu())
        for w in ("2024-W03", "2024-W01", "2024-W02"):
            upsert_metric(conn, _metric(week=w, price=float(w[-2:])))

    def test_ascending_order(self, conn):
        self._seed(conn)
        series = get_metric_series(conn, "X1")
        assert [r["week"] for r in series] == ["2024-W01", "2024-W02", "2024-W03"]

    def test_start_week_inclusive(self, conn):
        self._seed(conn)
        series = get_metric_series(conn, "X1", start_week="2024-W02")
        assert [r["week"] for r in series] == ["2024-W02", "2024-W03"]

    def test_end_week_inclusive(self, conn):
        self._seed(conn)
        series = get_metric_series(conn, "X1", end_week="2024-W02")
        assert [r["week"] for r in series] == ["2024-W01", "2024-W02"]

    def test_range_both_bounds(self, conn):
        self._seed(conn)
        series = get_metric_series(conn, "X1", "2024-W02", "2024-W02")
        assert [r["week"] for r in series] == ["2024-W02"]

    def test_empty_range_returns_empty(self, conn):
        self._seed(conn)
        assert get_metric_series(conn, "X1", "2099-W01", "2099-W02") == []

    def test_unknown_xiaoqu_returns_empty(self, conn):
        assert get_metric_series(conn, "NOPE") == []


# --------------------------------------------------------------------------- #
# list_xiaoqu 过滤
# --------------------------------------------------------------------------- #
class TestListXiaoqu:
    def _seed(self, conn):
        upsert_xiaoqu(conn, _xiaoqu("B1", city="北京", district="朝阳"))
        upsert_xiaoqu(conn, _xiaoqu("B2", city="北京", district="海淀"))
        upsert_xiaoqu(conn, _xiaoqu("T1", city="天津", district="和平"))

    def test_filter_by_city(self, conn):
        self._seed(conn)
        rows = list_xiaoqu(conn, city="北京")
        assert {r["id"] for r in rows} == {"B1", "B2"}

    def test_filter_by_city_and_district(self, conn):
        self._seed(conn)
        rows = list_xiaoqu(conn, city="北京", district="朝阳")
        assert [r["id"] for r in rows] == ["B1"]

    def test_filter_by_district_only(self, conn):
        self._seed(conn)
        rows = list_xiaoqu(conn, district="和平")
        assert [r["id"] for r in rows] == ["T1"]

    def test_no_filter_returns_all(self, conn):
        self._seed(conn)
        assert len(list_xiaoqu(conn)) == 3


# --------------------------------------------------------------------------- #
# 信号存取
# --------------------------------------------------------------------------- #
class TestSignals:
    def test_add_and_list_roundtrip(self, conn):
        upsert_xiaoqu(conn, _xiaoqu())
        sid = add_signal(conn, "X1", "2024-01-15", "buy", {"k": 1, "note": "放量"})
        assert isinstance(sid, int)
        rows = list_signals(conn, "X1")
        assert len(rows) == 1
        assert rows[0]["id"] == sid
        assert rows[0]["type"] == "buy"
        assert rows[0]["reason"] == {"k": 1, "note": "放量"}

    def test_signal_autoincrement_ids(self, conn):
        upsert_xiaoqu(conn, _xiaoqu())
        a = add_signal(conn, "X1", "2024-01-01", "buy", {})
        b = add_signal(conn, "X1", "2024-01-02", "risk", {})
        assert b > a
        rows = list_signals(conn, "X1")
        assert [r["id"] for r in rows] == sorted([a, b])

    def test_empty_reason_yields_none(self, conn):
        # 直接写 NULL reason_json 时，list 应得到 reason=None。
        upsert_xiaoqu(conn, _xiaoqu())
        conn.execute(
            "INSERT INTO signal_event (xiaoqu_id, date, type, reason_json) "
            "VALUES (?, ?, ?, NULL)",
            ("X1", "2024-01-01", "buy"),
        )
        conn.commit()
        rows = list_signals(conn, "X1")
        assert rows[0]["reason"] is None


# --------------------------------------------------------------------------- #
# 外键约束
# --------------------------------------------------------------------------- #
class TestForeignKey:
    def test_metric_with_unknown_xiaoqu_raises(self, conn):
        with pytest.raises(sqlite3.IntegrityError):
            upsert_metric(conn, _metric(xid="DOES-NOT-EXIST"))

    def test_metric_with_known_xiaoqu_ok(self, conn):
        upsert_xiaoqu(conn, _xiaoqu())
        upsert_metric(conn, _metric())  # 不应抛错
        assert len(get_metric_series(conn, "X1")) == 1
