"""shikar.overview 总览聚合函数与 ``GET /api/overview`` 接口测试。

全部仅用标准库：

* 用 ``storage.connect()`` 内存库 + ``pipeline.ingest`` 造 2 个城市、每城 ≥2
  小区、≥6 周数据（价格线性以便 ``mom4`` 在充足历史后非空）；
* 直接断言 :func:`shikar.overview.compute_overview` 的数值 / 排序 / 字段；
* 再通过 ``api.create_server`` 起服务请求 ``/api/overview``，断言 200 与结构正确；
* 空库返回 ``{"week": None, "cities": []}``。
"""

from __future__ import annotations

import http.client
import json
import sqlite3
import threading
from datetime import date, timedelta
from typing import Iterator

import pytest

from shikar import storage
from shikar.api import create_server
from shikar.overview import compute_overview, list_districts
from shikar.pipeline import ingest
from shikar.sources.base import MetricRecord

WEEKS = 14  # 每小区 14 期×30 天：跨度 390 天过门槛，mom4 从第 2 期起可算、第 5 期起有分位。


def _week_at(i: int, start: date = date(2024, 1, 1), step_days: int = 30) -> str:
    """第 ``i`` 期（默认 2024-01-01 起每 30 天一期，落在 mom4 的 28±7 天窗口内）。"""
    d = start + timedelta(days=step_days * i)
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


WEEKS_LIST = [_week_at(i) for i in range(WEEKS)]
LAST_WEEK = WEEKS_LIST[-1]


# --------------------------------------------------------------------------- #
# 造数辅助
# --------------------------------------------------------------------------- #
def _linear_records(
    city: str,
    district: str,
    name: str,
    base_price: float,
    step: float,
    weeks: int = WEEKS,
) -> list[MetricRecord]:
    """构造某小区 ``weeks`` 周、价格线性递增的指标记录。

    线性价格使 ``mom4 = price_t / price_{t-4} - 1`` 在第 5 周起恒为非空常量，
    便于精确断言榜单排序。
    """
    records: list[MetricRecord] = []
    for i in range(weeks):
        records.append(
            MetricRecord(
                city=city,
                district=district,
                plate="板块",
                xiaoqu_id="",
                xiaoqu_name=name,
                week=_week_at(i),
                price=base_price + step * i,
                supply=100 + i,
                vol30=10 + i,
                bargain=0.10,
                heat=50.0,
            )
        )
    return records


#: 两个城市、每城 2 个小区。北京（望京A / 中关村B）步长不同，温度/涨幅有区分；
#: 天津（和平C / 河西D）同理。
_SPECS = [
    # (city, district, name, base_price, step)
    ("北京", "朝阳", "望京A", 50000.0, 1000.0),
    ("北京", "海淀", "中关村B", 60000.0, 2000.0),
    ("天津", "和平", "和平C", 30000.0, 300.0),
    ("天津", "河西", "河西D", 25000.0, 500.0),
]


def _seed(conn: sqlite3.Connection) -> None:
    """用 ``pipeline.ingest`` 播种 2 城 4 小区、每小区 6 周的数据。"""
    records: list[MetricRecord] = []
    for city, district, name, base, step in _SPECS:
        records.extend(_linear_records(city, district, name, base, step))
    ingest(conn, records)


@pytest.fixture()
def conn() -> Iterator[sqlite3.Connection]:
    """已建表并播种的 ``:memory:`` 连接，测试结束自动关闭。"""
    c = storage.connect()
    storage.init_db(c)
    _seed(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture()
def empty_conn() -> Iterator[sqlite3.Connection]:
    """已建表但无任何数据的 ``:memory:`` 连接。"""
    c = storage.connect()
    storage.init_db(c)
    try:
        yield c
    finally:
        c.close()


# --------------------------------------------------------------------------- #
# compute_overview 直接测试
# --------------------------------------------------------------------------- #
def test_empty_db(empty_conn: sqlite3.Connection) -> None:
    """空库返回 ``{"week": None, "cities": []}``。"""
    assert compute_overview(empty_conn) == {"week": None, "cities": []}


def test_week_is_global_max(conn: sqlite3.Connection) -> None:
    """全局 ``week`` 取所有最新行的最大 week（此处为最后一周）。"""
    result = compute_overview(conn)
    assert result["week"] == LAST_WEEK


def test_cities_sorted_ascending(conn: sqlite3.Connection) -> None:
    """``cities`` 按 city 名称升序排列（北京在天津之前）。"""
    result = compute_overview(conn)
    assert [c["city"] for c in result["cities"]] == ["北京", "天津"]


def test_city_fields_and_count(conn: sqlite3.Connection) -> None:
    """每城 ``count`` 为参与小区数、``temperature`` 为最新行均温（1 位小数）。"""
    result = compute_overview(conn)
    by_city = {c["city"]: c for c in result["cities"]}

    # 两城各 2 个小区。
    assert by_city["北京"]["count"] == 2
    assert by_city["天津"]["count"] == 2

    # 逐小区取最新行 temperature，独立算均值（1 位小数）。
    for city in ("北京", "天津"):
        temps = []
        for xiaoqu in storage.list_xiaoqu(conn, city=city):
            series = storage.get_metric_series(conn, xiaoqu["id"])
            latest = [r for r in series if r["temperature"] is not None][-1]
            temps.append(latest["temperature"])
        expected = round(sum(temps) / len(temps), 1)
        assert by_city[city]["temperature"] == expected


def test_top_hot_and_cold_sorted(conn: sqlite3.Connection) -> None:
    """``top_hot`` 按 temperature 降序、``top_cold`` 升序，且条目字段完整。"""
    result = compute_overview(conn)
    for city in result["cities"]:
        hot = [item["temperature"] for item in city["top_hot"]]
        cold = [item["temperature"] for item in city["top_cold"]]
        assert hot == sorted(hot, reverse=True)
        assert cold == sorted(cold)
        # 至多 5 条。
        assert len(city["top_hot"]) <= 5
        assert len(city["top_cold"]) <= 5
        # 字段完整。
        for item in city["top_hot"]:
            assert set(item) == {
                "xiaoqu_id", "name", "district", "temperature", "stage", "mom4"
            }


def test_top_rising_and_falling_only_non_null_mom4(conn: sqlite3.Connection) -> None:
    """``top_rising`` / ``top_falling`` 只含 mom4 非空行，且分别降序 / 升序。"""
    result = compute_overview(conn)
    for city in result["cities"]:
        # 所有条目 mom4 非空。
        assert all(item["mom4"] is not None for item in city["top_rising"])
        assert all(item["mom4"] is not None for item in city["top_falling"])
        rising = [item["mom4"] for item in city["top_rising"]]
        falling = [item["mom4"] for item in city["top_falling"]]
        assert rising == sorted(rising, reverse=True)
        assert falling == sorted(falling)
        assert len(city["top_rising"]) <= 5
        assert len(city["top_falling"]) <= 5


def test_skips_xiaoqu_without_temperature(conn: sqlite3.Connection) -> None:
    """temperature 全空的小区被跳过、不计入 count。"""
    # 直接写入一个 temperature 全为 NULL 的小区：compute_overview 应整体跳过它。
    storage.upsert_xiaoqu(
        conn,
        {"id": "E|东城|单周E", "city": "北京", "district": "东城", "name": "单周E"},
    )
    for week in WEEKS_LIST[:2]:
        storage.upsert_metric(
            conn,
            {"xiaoqu_id": "E|东城|单周E", "week": week, "price": 40000.0,
             "temperature": None},
        )
    result = compute_overview(conn)
    beijing = next(c for c in result["cities"] if c["city"] == "北京")
    # 单周 E 无 temperature，不参与统计。
    assert beijing["count"] == 2
    # 但它是「有数据、数据不足」，要计入 total 与 insufficient（docs/04 §3.2）。
    assert beijing["total"] == 3
    assert beijing["insufficient"] == 1
    # 精度为 4 位（docs/45 P1-2）：3 位会把 10550/10554 抹成 1.0。
    assert beijing["insufficient_ratio"] == round(1 / 3, 4)
    names = {item["name"] for item in beijing["top_hot"]}
    assert "单周E" not in names


def test_overview_excludes_insufficient_from_boards(conn: sqlite3.Connection) -> None:
    """数据不足小区不进任何榜单，也不计入均温。"""
    ingest(conn, _linear_records("北京", "东城", "单期F", 50000.0, 1000.0, weeks=1))
    result = compute_overview(conn, city="北京")
    beijing = result["cities"][0]
    assert beijing["count"] == 2
    assert beijing["total"] == 3
    assert beijing["insufficient"] == 1
    listed = {i["name"] for i in (
        beijing["top_hot"] + beijing["top_cold"]
        + beijing["top_rising"] + beijing["top_falling"]
    )}
    assert "单期F" not in listed
    # 均温只按两个合格小区计算。
    temps = []
    for xq in storage.list_xiaoqu(conn, city="北京"):
        if xq["name"] == "单期F":
            continue
        latest = [r for r in storage.get_metric_series(conn, xq["id"])
                  if r["temperature"] is not None][-1]
        temps.append(latest["temperature"])
    assert beijing["temperature"] == round(sum(temps) / len(temps), 1)


def test_overview_all_insufficient_has_null_temperature(empty_conn: sqlite3.Connection) -> None:
    """全城只有不足小区时：temperature 为 None、榜单为空，但 week 仍给数据周。"""
    ingest(empty_conn, _linear_records("北京", "东城", "单期G", 50000.0, 1000.0, weeks=1))
    result = compute_overview(empty_conn)
    city = result["cities"][0]
    assert city["temperature"] is None
    assert city["count"] == 0 and city["total"] == 1 and city["insufficient"] == 1
    assert city["insufficient_ratio"] == 1.0
    assert city["top_hot"] == [] and city["top_rising"] == []
    assert result["week"] == WEEKS_LIST[0]


# --------------------------------------------------------------------------- #
# 较上期变化（delta）与分区温度
# --------------------------------------------------------------------------- #
def _add_xq(conn, xid: str, district: str, rows: list[tuple[str, float | None]]) -> None:
    """直接写入带 temperature 的指标行（绕过门槛，便于精确构造）。"""
    storage.upsert_xiaoqu(conn, {
        "id": xid, "city": "北京", "district": district, "plate": "望京", "name": xid,
    })
    for week, temp in rows:
        storage.upsert_metric(conn, {
            "xiaoqu_id": xid, "week": week, "price": 50000.0, "temperature": temp,
        })


# --------------------------------------------------------------------------- #
# 读数层（readings）：不足小区也给读数，但不得影响温度、榜单与不足计数
# --------------------------------------------------------------------------- #
def _add_metric_xq(conn, xid: str, district: str,
                   rows: list[tuple[str, float | None, float | None]]) -> None:
    """写入带 ``price`` / ``mom4`` 的指标行（绕过门槛，便于精确构造读数层用例）。

    :param rows: ``(week, price, mom4)`` 三元组列表。
    """
    storage.upsert_xiaoqu(conn, {
        "id": xid, "city": "北京", "district": district, "plate": "望京", "name": xid,
    })
    for week, price, mom4 in rows:
        storage.upsert_metric(conn, {
            "xiaoqu_id": xid, "week": week, "price": price, "mom4": mom4,
        })


def test_overview_readings_include_insufficient(empty_conn: sqlite3.Connection) -> None:
    """门槛不足的小区没有温度，但它的价格读数仍应计入读数层。

    场景：北京有两小区——一个 6 期（合格）、一个 1 期（不足）。
    期望 ``count==1``（只有合格者）、``insufficient==1``，而
    ``readings.with_price==2``（两者都有价格）。这正是首页「温度留空但读数仍在」
    的依据（docs/04 §0.4）。
    """
    ingest(empty_conn, _linear_records("北京", "朝阳", "六期合格", 50000.0, 1000.0))
    ingest(empty_conn, _linear_records("北京", "海淀", "单期不足", 70000.0, 0.0, weeks=1))

    city = compute_overview(empty_conn)["cities"][0]
    assert city["count"] == 1
    assert city["insufficient"] == 1
    assert city["readings"]["with_price"] == 2
    assert city["readings"]["total"] == 2
    # 最新价：合格小区 50000+1000×(WEEKS-1) 与不足小区 70000 的中位数。
    # rebase 到首页读数层后 WEEKS 由 6 增至 14（新 mom4 需 28±7 天的日历跨度），
    # 故这里按 WEEKS 推算，不再写死 55000。
    latest_qualified = 50000.0 + 1000.0 * (WEEKS - 1)
    assert city["readings"]["median_price"] == (latest_qualified + 70000.0) / 2


def test_overview_readings_mom4_fraction_and_median(empty_conn: sqlite3.Connection) -> None:
    """``median_mom`` 保持与 ``mom4`` 同口径（分数，非百分数），且只统计非空值。"""
    _add_metric_xq(empty_conn, "M1", "朝阳", [("2024-W01", 50000.0, None),
                                               ("2024-W05", 51000.0, 0.02)])
    _add_metric_xq(empty_conn, "M2", "海淀", [("2024-W01", 60000.0, None),
                                               ("2024-W05", 63000.0, 0.06)])
    # 第三个小区只有一期、无 mom4，不应拉低分母之外的东西
    _add_metric_xq(empty_conn, "M3", "东城", [("2024-W05", 40000.0, None)])

    readings = compute_overview(empty_conn)["cities"][0]["readings"]
    assert readings["with_price"] == 3
    assert readings["with_mom"] == 2
    # 0.02 与 0.06 的中位数 = 0.04（分数），不是 2.0（百分数）
    assert readings["median_mom"] == 0.04
    # 最新价 51000 / 63000 / 40000 的中位数
    assert readings["median_price"] == 51000.0


def test_overview_readings_empty_when_no_price(empty_conn: sqlite3.Connection) -> None:
    """全部小区都没有价格时：读数层各项为 0/None，不留空字段缺失。"""
    storage.upsert_xiaoqu(empty_conn, {
        "id": "N1", "city": "北京", "district": "朝阳", "plate": "望京", "name": "N1",
    })
    storage.upsert_metric(empty_conn, {
        "xiaoqu_id": "N1", "week": "2024-W05", "price": None, "temperature": None,
    })
    city = compute_overview(empty_conn)["cities"][0]
    assert city["readings"] == {
        "with_price": 0, "total": 1, "median_price": None,
        "with_mom": 0, "median_mom": None,
    }


def test_overview_delta_from_prev_observation(empty_conn: sqlite3.Connection) -> None:
    """两期观测 40 → 44：较上期 +4.0，参与数 1，无不足。"""
    _add_xq(empty_conn, "D1", "朝阳", [("2024-W01", 40.0), ("2024-W14", 44.0)])
    city = compute_overview(empty_conn)["cities"][0]
    assert city["temperature_delta"] == 4.0
    assert city["delta_count"] == 1
    assert city["delta_insufficient"] == 0
    assert city["delta_insufficient_ratio"] == 0.0


def test_overview_delta_null_when_no_prev(empty_conn: sqlite3.Connection) -> None:
    """只有一期：delta 为 None，并计入「无可用上期」。"""
    _add_xq(empty_conn, "D2", "朝阳", [("2024-W01", 50.0)])
    city = compute_overview(empty_conn)["cities"][0]
    assert city["temperature_delta"] is None
    assert city["delta_count"] == 0
    assert city["delta_insufficient"] == 1
    assert city["delta_insufficient_ratio"] == 1.0


def test_overview_delta_skips_too_old_prev(empty_conn: sqlite3.Connection) -> None:
    """上期观测序号差 >4（太旧）：不算 delta，计入 insufficient。"""
    rows: list[tuple[str, float | None]] = [("2024-W01", 40.0)]
    rows += [(f"2024-W{2 + i:02d}", None) for i in range(4)]  # 中间 4 条无温度
    rows.append(("2024-W06", 50.0))                            # 序号差 = 5
    _add_xq(empty_conn, "D3", "朝阳", rows)
    city = compute_overview(empty_conn)["cities"][0]
    assert city["temperature_delta"] is None
    assert city["delta_count"] == 0
    assert city["delta_insufficient"] == 1


def test_overview_districts_aggregate_and_sort(empty_conn: sqlite3.Connection) -> None:
    """分区温度按降序、字段齐全、不足比例正确。"""
    _add_xq(empty_conn, "E1", "朝阳", [("2024-W01", 50.0)])
    _add_xq(empty_conn, "E2", "海淀", [("2024-W01", 40.0)])
    _add_xq(empty_conn, "E3", "海淀", [("2024-W01", None)])  # 不足
    city = compute_overview(empty_conn)["cities"][0]
    ds = city["districts"]
    assert [d["district"] for d in ds] == ["朝阳", "海淀"]
    assert ds[0]["temperature"] == 50.0 and ds[0]["insufficient_ratio"] == 0.0
    assert ds[1]["temperature"] == 40.0
    assert ds[1]["total"] == 2 and ds[1]["count"] == 1 and ds[1]["insufficient"] == 1
    assert ds[1]["insufficient_ratio"] == 0.5


# --------------------------------------------------------------------------- #
# HTTP 接口测试
# --------------------------------------------------------------------------- #
def _threadsafe_conn() -> sqlite3.Connection:
    """线程安全 ``:memory:`` 连接（供 ``ThreadingHTTPServer`` 使用）。"""
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


@pytest.fixture()
def live_server() -> Iterator[dict]:
    """启动后台只读服务，测试结束关闭。"""
    c = _threadsafe_conn()
    storage.init_db(c)
    _seed(c)
    server = create_server(c, host="127.0.0.1", port=0)
    host, port = server.server_address[0], server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"host": host, "port": port}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        c.close()


def _get_json(server: dict, path: str) -> tuple[int, dict, object]:
    """发起 GET 请求并解析 JSON，返回 ``(状态码, 响应头, 对象)``。"""
    client = http.client.HTTPConnection(server["host"], server["port"], timeout=5)
    try:
        client.request("GET", path)
        resp = client.getresponse()
        body = resp.read()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, headers, json.loads(body.decode("utf-8"))
    finally:
        client.close()


def test_api_overview(live_server: dict) -> None:
    """``GET /api/overview`` 返回 200、JSON 结构正确、中文不转义。"""
    status, headers, payload = _get_json(live_server, "/api/overview")
    assert status == 200
    assert headers["content-type"] == "application/json; charset=utf-8"

    assert payload["week"] == LAST_WEEK
    assert [c["city"] for c in payload["cities"]] == ["北京", "天津"]
    for city in payload["cities"]:
        assert set(city) == {
            "city", "temperature", "count",
                "total", "insufficient", "insufficient_ratio",
                "temperature_delta", "delta_count", "delta_insufficient",
                "delta_insufficient_ratio", "districts", "readings",
            "top_hot", "top_cold", "top_rising", "top_falling",
        }
        assert isinstance(city["temperature"], float)
        assert city["count"] == 2
        # 读数层：不判趋势，只给读数（docs/04 §0.4）。
        assert set(city["readings"]) == {
            "with_price", "total", "median_price", "with_mom", "median_mom",
        }
        assert city["readings"]["with_price"] == 2
        assert city["readings"]["total"] == 2


# --------------------------------------------------------------------------- #
# compute_overview 过滤（city / district）
# --------------------------------------------------------------------------- #
def test_compute_overview_city_filter(conn: sqlite3.Connection) -> None:
    """只传 city 时仅统计该城小区，榜单只含该城数据。"""
    result = compute_overview(conn, city="北京")
    assert [c["city"] for c in result["cities"]] == ["北京"]
    beijing = result["cities"][0]
    assert beijing["count"] == 2
    names = {item["name"] for item in beijing["top_hot"]}
    assert names == {"望京A", "中关村B"}


def test_compute_overview_district_filter(conn: sqlite3.Connection) -> None:
    """传 city + district 时只统计该区，count/均值/榜单均只含该区小区。"""
    result = compute_overview(conn, city="北京", district="朝阳")
    assert [c["city"] for c in result["cities"]] == ["北京"]
    chaoyang = result["cities"][0]
    assert chaoyang["count"] == 1
    # 唯一小区的 name 出现在各榜单（mom4 非空的榜单也应有它）。
    hot_names = [item["name"] for item in chaoyang["top_hot"]]
    cold_names = [item["name"] for item in chaoyang["top_cold"]]
    assert hot_names == ["望京A"]
    assert cold_names == ["望京A"]
    # 均值即该小区最新行 temperature（1 位小数）。
    xiaoqu = storage.list_xiaoqu(conn, city="北京", district="朝阳")[0]
    series = storage.get_metric_series(conn, xiaoqu["id"])
    latest = [r for r in series if r["temperature"] is not None][-1]
    assert chaoyang["temperature"] == round(latest["temperature"], 1)


def test_compute_overview_district_only(conn: sqlite3.Connection) -> None:
    """只传 district（不传 city）也能跨城收窄到该区。"""
    result = compute_overview(conn, district="和平")
    # 仅天津有「和平」区。
    assert [c["city"] for c in result["cities"]] == ["天津"]
    assert result["cities"][0]["count"] == 1


def test_compute_overview_filter_no_match(conn: sqlite3.Connection) -> None:
    """过滤到不存在的小区时返回空结构（week 为 None）。"""
    result = compute_overview(conn, city="上海")
    assert result == {"week": None, "cities": []}


def test_compute_overview_no_filter_unchanged(conn: sqlite3.Connection) -> None:
    """不传过滤参数时与显式传 None 的结构/数值一致（向后兼容）。"""
    assert compute_overview(conn) == compute_overview(conn, city=None, district=None)


# --------------------------------------------------------------------------- #
# list_districts
# --------------------------------------------------------------------------- #
def test_list_districts_all(conn: sqlite3.Connection) -> None:
    """返回去重、按 city、district 升序的「城市—区」对。"""
    rows = list_districts(conn)
    assert rows == [
        {"city": "北京", "district": "朝阳"},
        {"city": "北京", "district": "海淀"},
        {"city": "天津", "district": "和平"},
        {"city": "天津", "district": "河西"},
    ]

    # 每个 (city, district) 唯一，且整体有序。
    pairs = [(r["city"], r["district"]) for r in rows]
    assert pairs == sorted(pairs)
    assert len(pairs) == len(set(pairs))


def test_list_districts_by_city(conn: sqlite3.Connection) -> None:
    """按 city 过滤只返回该城市的区。"""
    rows = list_districts(conn, city="北京")
    assert rows == [
        {"city": "北京", "district": "朝阳"},
        {"city": "北京", "district": "海淀"},
    ]


def test_list_districts_empty(empty_conn: sqlite3.Connection) -> None:
    """空库返回空列表。"""
    assert list_districts(empty_conn) == []
    assert list_districts(empty_conn, city="北京") == []


# --------------------------------------------------------------------------- #
# HTTP 接口：/api/overview 过滤 + /api/districts
# --------------------------------------------------------------------------- #
def test_api_overview_filtered(live_server: dict) -> None:
    """``GET /api/overview?city=&district=`` 返回 200 且只含该区数据。"""
    status, headers, payload = _get_json(
        live_server, "/api/overview?city=%E5%8C%97%E4%BA%AC&district=%E6%9C%9D%E9%98%B3"
    )
    assert status == 200
    assert headers["content-type"] == "application/json; charset=utf-8"
    assert [c["city"] for c in payload["cities"]] == ["北京"]
    assert payload["cities"][0]["count"] == 1
    assert {item["name"] for item in payload["cities"][0]["top_hot"]} == {"望京A"}


def test_api_districts(live_server: dict) -> None:
    """``GET /api/districts`` 返回全部「城市—区」对，``?city=`` 可过滤。"""
    status, headers, payload = _get_json(live_server, "/api/districts")
    assert status == 200
    assert headers["content-type"] == "application/json; charset=utf-8"
    assert payload == [
        {"city": "北京", "district": "朝阳"},
        {"city": "北京", "district": "海淀"},
        {"city": "天津", "district": "和平"},
        {"city": "天津", "district": "河西"},
    ]

    status2, _h2, payload2 = _get_json(live_server, "/api/districts?city=%E5%A4%A9%E6%B4%A5")
    assert status2 == 200
    assert payload2 == [
        {"city": "天津", "district": "和平"},
        {"city": "天津", "district": "河西"},
    ]

