# -*- coding: utf-8 -*-
"""医院性质（poi.ownership）相关回归测试。

覆盖任务书 docs/49 的四个必测点：

* 旧库升级：``init_db`` 对已有 poi 表补 ownership 列，可重复执行、原行不丢；
* 周重建回灌：``build_db.load_poi`` / ``restore_poi`` 的列清单包含 ownership，
  旧库无该列时以 NULL 占位，回灌后列值仍在；
* 查询过滤：``list_pois`` 的 ownership 单条件、与 grade 叠加、空值不过滤；
* 人工标注脚本：非法值/未知 id 被跳过、幂等、``--dry-run`` 不写库。
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3

import pytest

from shikar.storage import connect, init_db, list_pois, upsert_poi

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_script(name: str):
    path = os.path.join(ROOT, "scripts", name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _poi(pid, *, type_="hospital", name=None, city="北京", district="东城",
         grade=None, ownership=None):
    return {
        "id": pid, "type": type_, "subtype": None, "name": name or ("医院" + pid),
        "city": city, "grade": grade, "ownership": ownership,
        "lat": 39.9, "lng": 116.4, "district": district,
        "address": None, "source": "test", "version": "2026-11",
    }


# --------------------------------------------------------------------------- #
# 迁移
# --------------------------------------------------------------------------- #
_LEGACY_POI_DDL = """
CREATE TABLE poi (
    id       TEXT PRIMARY KEY,
    type     TEXT NOT NULL,
    subtype  TEXT,
    name     TEXT NOT NULL,
    city     TEXT,
    grade    TEXT,
    lat      REAL,
    lng      REAL,
    district TEXT,
    address  TEXT,
    source   TEXT,
    version  TEXT
)
"""


def test_init_db_adds_ownership_to_legacy_poi_and_keeps_rows():
    conn = sqlite3.connect(":memory:")
    conn.execute(_LEGACY_POI_DDL)
    conn.execute(
        "INSERT INTO poi (id,type,name,city,grade) VALUES ('old1','hospital','老医院','北京','三甲')"
    )
    conn.commit()

    init_db(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(poi)")]
    assert "ownership" in cols
    assert conn.execute("SELECT COUNT(*) FROM poi").fetchone()[0] == 1

    # 幂等：第二次执行不报错、列不重复、行不变
    init_db(conn)
    assert [r[1] for r in conn.execute("PRAGMA table_info(poi)")] == cols
    assert conn.execute("SELECT COUNT(*) FROM poi").fetchone()[0] == 1
    conn.close()


# --------------------------------------------------------------------------- #
# upsert / list_pois
# --------------------------------------------------------------------------- #
@pytest.fixture()
def conn():
    c = connect()
    init_db(c)
    try:
        yield c
    finally:
        c.close()


def test_upsert_poi_roundtrip_ownership(conn):
    upsert_poi(conn, _poi("h1", grade="三甲", ownership="公办"))
    row = conn.execute("SELECT grade,ownership FROM poi WHERE id='h1'").fetchone()
    assert tuple(row) == ("三甲", "公办")

    # 再次 upsert 允许覆盖（采集链路以新值为准；人工值由脚本单独 UPDATE 保住）
    upsert_poi(conn, _poi("h1", grade="三甲", ownership="民办"))
    assert conn.execute("SELECT ownership FROM poi WHERE id='h1'").fetchone()[0] == "民办"


def test_list_pois_filters_ownership_alone_and_with_grade(conn):
    upsert_poi(conn, _poi("h1", grade="三甲", ownership="公办"))
    upsert_poi(conn, _poi("h2", grade="二甲", ownership="民办"))
    upsert_poi(conn, _poi("h3", grade=None, ownership="社区"))
    upsert_poi(conn, _poi("h4", grade="三甲", ownership=None, city="天津"))

    assert [r["id"] for r in list_pois(conn, "hospital", ownership="社区")] == ["h3"]
    assert [r["id"] for r in list_pois(conn, "hospital", grade="三甲", ownership="公办")] == ["h1"]
    assert [r["id"] for r in list_pois(conn, "hospital", grade="三甲", ownership="民办")] == []
    # 不传 ownership = 不过滤（向后兼容）
    assert {r["id"] for r in list_pois(conn, "hospital")} == {"h1", "h2", "h3", "h4"}
    # 空串按「未指定」处理
    assert {r["id"] for r in list_pois(conn, "hospital", ownership="")} == {"h1", "h2", "h3", "h4"}


# --------------------------------------------------------------------------- #
# 周重建回灌
# --------------------------------------------------------------------------- #
def _build_db_module():
    return _load_script("build_db")


def test_restore_poi_keeps_ownership(conn):
    mod = _build_db_module()
    rows = [
        ("h1", "hospital", None, "协和", "北京", "三甲", "公办",
         39.9, 116.4, "东城", None, "baidu", "2026-11"),
    ]
    n_poi, n_rel, _ = mod.restore_poi(conn, rows, [])
    assert (n_poi, n_rel) == (1, 0)
    row = conn.execute("SELECT grade,ownership FROM poi WHERE id='h1'").fetchone()
    assert tuple(row) == ("三甲", "公办")


def test_load_poi_reads_ownership_and_tolerates_legacy_schema(tmp_path):
    mod = _build_db_module()

    # 新库：ownership 有值
    new_db = str(tmp_path / "new.db")
    c = sqlite3.connect(new_db)
    c.execute(_LEGACY_POI_DDL.replace("grade    TEXT,", "grade    TEXT,\n    ownership TEXT,"))
    c.execute("INSERT INTO poi (id,type,name,city,grade,ownership) "
              "VALUES ('n1','hospital','新医院','北京','三甲','公办')")
    c.commit(); c.close()
    rows, _rel, _extra, err = mod.load_poi(new_db)
    assert err is None
    assert rows[0][6] == "公办"

    # 旧库：没有 ownership 列 → SELECT 里以 NULL 占位，列数仍是 13
    old_db = str(tmp_path / "old.db")
    c = sqlite3.connect(old_db)
    c.execute(_LEGACY_POI_DDL)
    c.execute("INSERT INTO poi (id,type,name,city,grade) "
              "VALUES ('o1','hospital','旧医院','北京','二甲')")
    c.commit(); c.close()
    rows, _rel, _extra, err = mod.load_poi(old_db)
    assert err is None
    assert len(rows[0]) == 13
    assert rows[0][6] is None
    assert rows[0][0] == "o1"


# --------------------------------------------------------------------------- #
# 人工标注脚本
# --------------------------------------------------------------------------- #
def test_tag_script_skips_invalid_and_is_idempotent(conn, capsys):
    mod = _load_script("tag_poi_ownership")
    upsert_poi(conn, _poi("h1", grade="三甲"))
    upsert_poi(conn, _poi("h2", grade="二甲"))

    stat = mod.apply_rows(conn, [
        {"poi_id": "h1", "name": "h1", "city": "北京", "grade": "三甲", "ownership": "公办", "note": ""},
        {"poi_id": "h2", "name": "h2", "city": "北京", "grade": "二甲", "ownership": "未知", "note": ""},
        {"poi_id": "missing", "name": "无", "city": "北京", "grade": "", "ownership": "民办", "note": ""},
    ])
    assert (stat["changed"], stat["skipped"], stat["missing"]) == (1, 1, 1)
    assert conn.execute("SELECT ownership FROM poi WHERE id='h1'").fetchone()[0] == "公办"
    assert conn.execute("SELECT ownership FROM poi WHERE id='h2'").fetchone()[0] is None

    # 同一份数据再跑一次：0 变更（幂等）
    stat2 = mod.apply_rows(conn, [
        {"poi_id": "h1", "ownership": "公办"},
    ])
    assert (stat2["changed"], stat2["unchanged"]) == (0, 1)
    assert "不在" in capsys.readouterr().err


def test_tag_script_dry_run_does_not_write(conn):
    mod = _load_script("tag_poi_ownership")
    upsert_poi(conn, _poi("h1"))
    stat = mod.apply_rows(conn, [{"poi_id": "h1", "ownership": "社区"}], dry_run=True)
    assert stat["changed"] == 1
    assert conn.execute("SELECT ownership FROM poi WHERE id='h1'").fetchone()[0] is None


def test_tag_script_rejects_bad_header(tmp_path):
    mod = _load_script("tag_poi_ownership")
    p = tmp_path / "bad.csv"
    p.write_text("poi_id,name\nh1,医院\n", encoding="utf-8")
    with pytest.raises(ValueError):
        mod.load_rows(str(p))
