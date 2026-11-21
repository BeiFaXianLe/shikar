"""``scripts/osm_poi.py`` 导入逻辑回归测试（P1-1）。

核心回归：导入**单个城市**的 OSM 数据，**不得删除其它城市**或**未被 OSM 对应**的
百度 POI；仅替换当前城市中已确认对应（同名/近名同类型）的百度设施，且替换在同一
事务内完成。

不实际解析 .osm.pbf：monkeypatch 掉 ``Extract`` 与 ``apply_file``，直接喂入 POI 列表。
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys

import pytest

from shikar import storage

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_module():
    path = os.path.join(_REPO_ROOT, "scripts", "osm_poi.py")
    spec = importlib.util.spec_from_file_location("osm_poi_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _seed_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    storage.init_db(conn)
    # 北京：有 OSM 对应（同名学校）→ 应被替换删除。
    storage.upsert_poi(conn, {
        "id": "baidu:bj_school", "type": "school", "subtype": "primary",
        "name": "北京实验小学", "city": "北京", "grade": "重点", "lat": 39.9,
        "lng": 116.4, "district": "朝阳", "address": None, "source": "baidu",
        "version": "x",
    })
    # 天津：与本次导入无关的百度 POI → 必须保留。
    storage.upsert_poi(conn, {
        "id": "baidu:tj_school", "type": "school", "subtype": "primary",
        "name": "天津实验小学", "city": "天津", "grade": "重点", "lat": 39.1,
        "lng": 117.2, "district": "和平", "address": None, "source": "baidu",
        "version": "x",
    })
    # 北京：未被 OSM 覆盖的百度医院（无对应）→ 必须保留。
    storage.upsert_poi(conn, {
        "id": "baidu:bj_hosp_only", "type": "hospital", "subtype": None,
        "name": "北京某三甲医院", "city": "北京", "grade": "三甲", "lat": 39.95,
        "lng": 116.45, "district": "朝阳", "address": None, "source": "baidu",
        "version": "x",
    })
    conn.commit()
    conn.close()


class _FakeExtract:
    """替身：跳过 pbf 解析，直接给出待导入 POI（北京）。"""

    def __init__(self):
        self.pois = [
            {"id": "osm:n1", "type": "school", "subtype": "primary",
             "name": "北京实验小学", "grade": "普通", "lat": 39.9, "lng": 116.4,
             "district": "朝阳"},
        ]

    def apply_file(self, pbf, locations=True):
        return None


def _run_main(monkeypatch, module, db_path: str, city: str = "北京") -> None:
    monkeypatch.setattr(module, "Extract", _FakeExtract)
    monkeypatch.setattr(sys, "argv", [
        "osm_poi.py", "--city", city, "--pbf", "fake.pbf", "--db", db_path,
    ])
    module.main()


def test_import_does_not_delete_other_city_or_uncovered(tmp_path, monkeypatch) -> None:
    db = str(tmp_path / "t.db")
    _seed_db(db)
    module = _load_module()
    _run_main(monkeypatch, module, db)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    ids = {r["id"] for r in conn.execute("SELECT id FROM poi").fetchall()}
    # 天津百度 POI 必须保留。
    assert "baidu:tj_school" in ids
    # 北京未被 OSM 覆盖的百度医院必须保留。
    assert "baidu:bj_hosp_only" in ids
    # 北京已确认对应的百度学校被替换删除。
    assert "baidu:bj_school" not in ids
    # OSM 新数据已写入。
    assert "osm:n1" in ids
    conn.close()


def test_import_does_not_touch_baidu_when_no_match(tmp_path, monkeypatch) -> None:
    """导入的 OSM POI 与本地百度无对应时，绝不删除任何百度 POI。"""
    db = str(tmp_path / "t2.db")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    storage.init_db(conn)
    storage.upsert_poi(conn, {
        "id": "baidu:bj_x", "type": "school", "subtype": "primary",
        "name": "完全不同的学校", "city": "北京", "grade": "重点", "lat": 39.9,
        "lng": 116.4, "district": "朝阳", "address": None, "source": "baidu",
        "version": "x",
    })
    conn.commit()
    conn.close()

    module = _load_module()

    class _OnlyDifferent(_FakeExtract):
        def __init__(self):
            self.pois = [
                {"id": "osm:z9", "type": "school", "subtype": "primary",
                 "name": "某某命名中学", "grade": "普通", "lat": 39.8, "lng": 116.3,
                 "district": "海淀"},
            ]

    monkeypatch.setattr(module, "Extract", _OnlyDifferent)
    monkeypatch.setattr(sys, "argv", [
        "osm_poi.py", "--city", "北京", "--pbf", "fake.pbf", "--db", db,
    ])
    module.main()

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    ids = {r["id"] for r in conn.execute("SELECT id FROM poi").fetchall()}
    assert "baidu:bj_x" in ids  # 名称不匹配 → 不删
    assert "osm:z9" in ids
    conn.close()
