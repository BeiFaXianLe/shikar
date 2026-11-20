"""地点联想 /api/suggest 与 /api/commute 坐标入参的单元测试。

不启动 HTTP server（Windows 下易受端口/权限影响），直接调用纯函数 ``_route``，
并对百度外部调用做 monkeypatch，保证离线可跑、结果确定。
"""
from __future__ import annotations

import sqlite3

import pytest

from shikar import api


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def test_suggest_returns_display_name_city(monkeypatch):
    """联想结果应带 display='地点-城市'，并保留原始 name/city/district/坐标。"""
    def fake_suggestion(query, region="全国"):
        assert query == "中关村"
        assert region == "北京"
        return [
            {"name": "中关村-地铁站", "city": "北京市", "district": "海淀区",
             "lat": 39.989956, "lng": 116.323066, "address": "", "url": None},
            {"name": "中关村壹号", "city": "北京市", "district": "海淀区",
             "lat": 40.08206, "lng": 116.253836, "address": "", "url": None},
        ]

    monkeypatch.setattr(api.baidu, "suggestion", fake_suggestion)
    code, body = api._route(_conn(), "/api/suggest?q=中关村&city=北京&limit=8")
    assert code == 200
    items = body["items"]
    assert len(items) == 2
    assert items[0]["display"] == "中关村-地铁站-北京市"
    assert items[0]["name"] == "中关村-地铁站"
    assert items[0]["city"] == "北京市"
    assert items[0]["lat"] == pytest.approx(39.989956)
    assert items[0]["lng"] == pytest.approx(116.323066)


def test_suggest_empty_query_returns_empty(monkeypatch):
    """空查询直接返回空列表，不打百度接口。"""
    def boom(*a, **k):  # 不应被调用
        raise AssertionError("空查询不应调用百度")

    monkeypatch.setattr(api.baidu, "suggestion", boom)
    code, body = api._route(_conn(), "/api/suggest?q=&city=北京")
    assert code == 200
    assert body["items"] == []


def test_suggest_degrades_when_baidu_fails(monkeypatch):
    """百度不可用时不 500，返回空 items + error 提示，避免打挂下拉。"""
    def fail(*a, **k):
        raise RuntimeError("未配置百度 AK")

    monkeypatch.setattr(api.baidu, "suggestion", fail)
    code, body = api._route(_conn(), "/api/suggest?q=国贸&city=北京")
    assert code == 200
    assert body["items"] == []
    assert "error" in body


def test_commute_accepts_selected_coords_skipping_suggestion(monkeypatch):
    """前端选中下拉后传 BD09 坐标，commute 应直接转 WGS84，不再调用 suggestion。"""
    def no_suggest(*a, **k):
        raise AssertionError("传入坐标时不应再调用 suggestion 解析")

    monkeypatch.setattr(api.baidu, "suggestion", no_suggest)
    # 记录捕获到的 origin，验证坐标已被转成 WGS84（不再是 BD09 原值）
    captured = {}

    def fake_commute_for(conn, a, b, candidates, mode="driving"):
        captured["a"] = a
        captured["b"] = b
        return {}, "key"

    monkeypatch.setattr(api.commute, "commute_for", fake_commute_for)
    # 候选小区查询也需绕过（内存库无 xiaoqu 表），返回空候选即可验证解析阶段
    monkeypatch.setattr(api.catalog, "query_catalog",
                        lambda *a, **k: {"items": []})
    # 没有候选小区时也应在解析阶段就走通
    q = ("/api/commute?fromA=%E4%B8%AD%E5%85%B3%E6%9D%91&fromB=%E5%9B%BD%E8%B4%B8"
         "&latA=39.989956&lngA=116.323066&latB=39.91&lngB=116.46&mode=driving&w=0.5")
    code, body = api._route(_conn(), q)
    assert code == 200
    # BD09 -> WGS84 后经度应明显偏移（BD09 相对 WGS84 有 ~0.006 的固定偏移）
    assert abs(captured["a"]["lng"] - 116.323066) > 0.001
    assert abs(captured["a"]["lat"] - 39.989956) > 0.0001


def test_commute_requires_both_sides():
    """缺地点且缺坐标时应 400。"""
    code, body = api._route(_conn(), "/api/commute?fromA=x&mode=driving")
    assert code == 400
