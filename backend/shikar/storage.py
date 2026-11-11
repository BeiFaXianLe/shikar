"""shikar SQLite 数据层骨架：表结构 + 仓储函数。

实现来源：``docs/03-PRD.md`` 第 6.6 节「数据字典（核心表）」。

设计原则：

* **仅用 Python 标准库**：只依赖 ``sqlite3``，不引入任何第三方包；
* **连接即用**：所有函数都接收外部传入的 ``sqlite3.Connection``，本模块不持有
  全局连接状态，便于测试用 ``:memory:`` 库；
* **外键强制**：``connect`` 打开 ``PRAGMA foreign_keys=ON``，并由仓储函数在
  需要时再次确保，避免被测连接漏开；
* **幂等**：``init_db`` 使用 ``CREATE TABLE IF NOT EXISTS``，``upsert_*`` 使用
  ``INSERT ... ON CONFLICT ... DO UPDATE``，重复调用不产生重复行。

对外接口（全部带类型标注与 docstring）：

* ``connect`` / ``init_db``
* ``upsert_xiaoqu`` / ``upsert_metric``
* ``get_metric_series`` / ``list_xiaoqu``
* ``add_signal`` / ``list_signals``
"""

from __future__ import annotations

import json
import sqlite3
from typing import Mapping, Optional

__all__ = [
    "connect",
    "init_db",
    "upsert_xiaoqu",
    "upsert_metric",
    "get_metric_series",
    "list_xiaoqu",
    "add_signal",
    "list_signals",
]


# --------------------------------------------------------------------------- #
# 连接与建表
# --------------------------------------------------------------------------- #
def connect(path: str = ":memory:") -> sqlite3.Connection:
    """打开并返回一个 SQLite 连接。

    :param path: 数据库文件路径；默认 ``":memory:"`` 表示进程内内存库。
    :return: 已设置 ``row_factory=sqlite3.Row`` 且开启外键约束的连接。
    """
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """建表与索引（幂等）。

    执行 PRD §6.6 定义的四张核心表以及两个二级索引，全部使用
    ``CREATE ... IF NOT EXISTS``，因此可安全地重复调用。

    :param conn: 已打开的 SQLite 连接。
    """
    # 防御性开启外键（用户可能传入手工构造的连接）。
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS xiaoqu (
            id         TEXT PRIMARY KEY,
            city       TEXT,
            district   TEXT,
            plate      TEXT,
            name       TEXT,
            alias      TEXT,
            lng        REAL,
            lat        REAL,
            build_year INTEGER,
            households INTEGER
        );

        CREATE TABLE IF NOT EXISTS xiaoqu_metric_weekly (
            xiaoqu_id   TEXT    NOT NULL,
            week        TEXT    NOT NULL,
            price       REAL,
            mom4        REAL,
            mom13       REAL,
            supply      INTEGER,
            vol30       INTEGER,
            absorb      REAL,
            bargain     REAL,
            heat        REAL,
            temperature REAL,
            stage       TEXT,
            PRIMARY KEY (xiaoqu_id, week),
            FOREIGN KEY (xiaoqu_id) REFERENCES xiaoqu (id)
        );

        CREATE TABLE IF NOT EXISTS signal_event (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            xiaoqu_id   TEXT NOT NULL,
            date        TEXT NOT NULL,
            type        TEXT NOT NULL,
            reason_json TEXT
        );

        CREATE TABLE IF NOT EXISTS source_snapshot (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            source     TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            xiaoqu_id  TEXT,
            raw_json   TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_xiaoqu_city_district
            ON xiaoqu (city, district);
        CREATE INDEX IF NOT EXISTS idx_metric_weekly_xiaoqu_week
            ON xiaoqu_metric_weekly (xiaoqu_id, week);
        CREATE INDEX IF NOT EXISTS idx_xiaoqu_lat_lng
            ON xiaoqu (lat, lng);

        CREATE TABLE IF NOT EXISTS poi (
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
        );
        CREATE INDEX IF NOT EXISTS idx_poi_type ON poi (type, subtype);
        CREATE INDEX IF NOT EXISTS idx_poi_lat_lng ON poi (lat, lng);

        CREATE TABLE IF NOT EXISTS community_poi (
            community_id TEXT NOT NULL,
            poi_id       TEXT NOT NULL,
            distance_km  REAL NOT NULL,
            PRIMARY KEY (community_id, poi_id),
            FOREIGN KEY (community_id) REFERENCES xiaoqu (id),
            FOREIGN KEY (poi_id) REFERENCES poi (id)
        );
        CREATE INDEX IF NOT EXISTS idx_community_poi_poi
            ON community_poi (poi_id, distance_km, community_id);
        """
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# 小区主数据
# --------------------------------------------------------------------------- #
_XIAOQU_COLUMNS = (
    "id",
    "city",
    "district",
    "plate",
    "name",
    "alias",
    "lng",
    "lat",
    "build_year",
    "households",
)


def upsert_xiaoqu(conn: sqlite3.Connection, row: Mapping) -> None:
    """按 ``id`` 插入或更新一条小区主数据。

    已存在同 ``id`` 时更新其余列；缺失的可选列以 ``None`` 写入。未在
    ``row`` 中提供的键不会覆盖已有值之外的行为——本函数按整行语义写入，
    传入完整映射是调用方责任。

    :param conn: 已打开的 SQLite 连接。
    :param row: 至少包含 ``id`` 的小区字段映射。
    """
    cols = ", ".join(_XIAOQU_COLUMNS)
    placeholders = ", ".join(f":{c}" for c in _XIAOQU_COLUMNS)
    updates = ", ".join(f"{c} = excluded.{c}" for c in _XIAOQU_COLUMNS if c != "id")
    sql = (
        f"INSERT INTO xiaoqu ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT (id) DO UPDATE SET {updates}"
    )
    params = {c: row.get(c) for c in _XIAOQU_COLUMNS}
    conn.execute(sql, params)
    conn.commit()


# --------------------------------------------------------------------------- #
# 周度指标
# --------------------------------------------------------------------------- #
_METRIC_COLUMNS = (
    "xiaoqu_id",
    "week",
    "price",
    "mom4",
    "mom13",
    "supply",
    "vol30",
    "absorb",
    "bargain",
    "heat",
    "temperature",
    "stage",
)


def upsert_metric(conn: sqlite3.Connection, row: Mapping) -> None:
    """按 ``(xiaoqu_id, week)`` 插入或更新一条周度指标。

    :param conn: 已打开的 SQLite 连接。
    :param row: 至少包含 ``xiaoqu_id`` 与 ``week`` 的指标字段映射。
    """
    cols = ", ".join(_METRIC_COLUMNS)
    placeholders = ", ".join(f":{c}" for c in _METRIC_COLUMNS)
    updates = ", ".join(
        f"{c} = excluded.{c}" for c in _METRIC_COLUMNS if c not in ("xiaoqu_id", "week")
    )
    sql = (
        f"INSERT INTO xiaoqu_metric_weekly ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT (xiaoqu_id, week) DO UPDATE SET {updates}"
    )
    params = {c: row.get(c) for c in _METRIC_COLUMNS}
    conn.execute(sql, params)
    conn.commit()


def get_metric_series(
    conn: sqlite3.Connection,
    xiaoqu_id: str,
    start_week: Optional[str] = None,
    end_week: Optional[str] = None,
) -> list[dict]:
    """查询某小区的周度指标序列，按 ``week`` 升序。

    :param conn: 已打开的 SQLite 连接。
    :param xiaoqu_id: 小区 id。
    :param start_week: 起始周（含），``None`` 表示不设下界。
    :param end_week: 结束周（含），``None`` 表示不设上界。
    :return: 指标字典列表，``week`` 升序；无匹配时返回空列表。
    """
    sql = [
        "SELECT xiaoqu_id, week, price, mom4, mom13, supply, vol30, absorb,",
        "       bargain, heat, temperature, stage",
        "FROM xiaoqu_metric_weekly WHERE xiaoqu_id = ?",
    ]
    params: list = [xiaoqu_id]
    if start_week is not None:
        sql.append("AND week >= ?")
        params.append(start_week)
    if end_week is not None:
        sql.append("AND week <= ?")
        params.append(end_week)
    sql.append("ORDER BY week ASC")
    cur = conn.execute(" ".join(sql), params)
    return [dict(r) for r in cur.fetchall()]


# --------------------------------------------------------------------------- #
# 小区列表查询
# --------------------------------------------------------------------------- #
def list_xiaoqu(
    conn: sqlite3.Connection,
    city: Optional[str] = None,
    district: Optional[str] = None,
) -> list[dict]:
    """按城市 / 区域过滤列出小区主数据。

    :param conn: 已打开的 SQLite 连接。
    :param city: 城市过滤条件，``None`` 表示不限。
    :param district: 区域过滤条件，``None`` 表示不限。
    :return: 小区字典列表；无匹配时返回空列表。
    """
    cols = ", ".join(_XIAOQU_COLUMNS)
    sql = [f"SELECT {cols} FROM xiaoqu WHERE 1 = 1"]
    params: list = []
    if city is not None:
        sql.append("AND city = ?")
        params.append(city)
    if district is not None:
        sql.append("AND district = ?")
        params.append(district)
    sql.append("ORDER BY id ASC")
    cur = conn.execute(" ".join(sql), params)
    return [dict(r) for r in cur.fetchall()]


# --------------------------------------------------------------------------- #
# 信号事件
# --------------------------------------------------------------------------- #
def add_signal(
    conn: sqlite3.Connection,
    xiaoqu_id: str,
    date: str,
    type: str,
    reason: Mapping,
) -> int:
    """写入一条信号事件，``reason`` 以 JSON 字符串持久化。

    :param conn: 已打开的 SQLite 连接。
    :param xiaoqu_id: 小区 id。
    :param date: 信号日期（字符串，ISO 日期建议格式）。
    :param type: 信号类型。
    :param reason: 信号原因映射，序列化为 JSON 文本存储。
    :return: 新插入行的自增主键 ``id``。
    """
    reason_json = json.dumps(reason, ensure_ascii=False)
    cur = conn.execute(
        "INSERT INTO signal_event (xiaoqu_id, date, type, reason_json) "
        "VALUES (?, ?, ?, ?)",
        (xiaoqu_id, date, type, reason_json),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_signals(conn: sqlite3.Connection, xiaoqu_id: str) -> list[dict]:
    """列出某小区的全部信号事件，``reason_json`` 解析为 ``reason`` 字典。

    :param conn: 已打开的 SQLite 连接。
    :param xiaoqu_id: 小区 id。
    :return: 信号字典列表（含 ``id`` / ``xiaoqu_id`` / ``date`` / ``type`` /
        ``reason``），按 ``id`` 升序；``reason_json`` 为空时 ``reason`` 为
        ``None``。
    """
    cur = conn.execute(
        "SELECT id, xiaoqu_id, date, type, reason_json "
        "FROM signal_event WHERE xiaoqu_id = ? ORDER BY id ASC",
        (xiaoqu_id,),
    )
    result: list[dict] = []
    for r in cur.fetchall():
        item = dict(r)
        raw = item.pop("reason_json")
        item["reason"] = json.loads(raw) if raw else None
        result.append(item)
    return result


# --------------------------------------------------------------------------- #
# POI（学校/医院/高铁）与「小区—POI 距离」
# --------------------------------------------------------------------------- #
def upsert_poi(conn: sqlite3.Connection, row: Mapping) -> None:
    """按 id upsert 一条 POI。row 需含 id,type,name。"""
    conn.execute(
        "INSERT INTO poi (id,type,subtype,name,city,grade,lat,lng,district,address,source,version) "
        "VALUES (:id,:type,:subtype,:name,:city,:grade,:lat,:lng,:district,:address,:source,:version) "
        "ON CONFLICT (id) DO UPDATE SET type=excluded.type,subtype=excluded.subtype,name=excluded.name,"
        "city=excluded.city,grade=excluded.grade,lat=excluded.lat,lng=excluded.lng,district=excluded.district,"
        "address=excluded.address,source=excluded.source,version=excluded.version",
        {"id": row.get("id"), "type": row.get("type"), "subtype": row.get("subtype"),
         "name": row.get("name"), "city": row.get("city"), "grade": row.get("grade"), "lat": row.get("lat"),
         "lng": row.get("lng"), "district": row.get("district"), "address": row.get("address"),
         "source": row.get("source"), "version": row.get("version")},
    )


def upsert_community_poi(conn: sqlite3.Connection, community_id: str, poi_id: str, distance_km: float) -> None:
    conn.execute(
        "INSERT INTO community_poi (community_id,poi_id,distance_km) VALUES (?,?,?) "
        "ON CONFLICT (community_id,poi_id) DO UPDATE SET distance_km=excluded.distance_km",
        (community_id, poi_id, distance_km),
    )


def list_pois(conn: sqlite3.Connection, type_: str, subtype: str | None = None,
              grade: str | None = None, district: str | None = None,
              city: str | None = None) -> list[dict]:
    sql = ("SELECT p.*, "
           "(SELECT COUNT(*) FROM community_poi cp WHERE cp.poi_id = p.id) AS community_count "
           "FROM poi p WHERE p.type = ?")
    params: list = [type_]
    if subtype:
        sql += " AND p.subtype = ?"; params.append(subtype)
    if grade:
        sql += " AND p.grade = ?"; params.append(grade)
    if city:
        sql += " AND p.city = ?"; params.append(city)
    if district:
        sql += " AND p.district LIKE ?"; params.append(district + "%")
    sql += " ORDER BY p.name ASC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def top_communities_for_poi(conn: sqlite3.Connection, poi_id: str, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT cp.community_id AS id, x.name, x.city, x.district, x.plate, cp.distance_km, "
        "  (SELECT price FROM xiaoqu_metric_weekly m WHERE m.xiaoqu_id=x.id ORDER BY week DESC LIMIT 1) AS latest_price, "
        "  (SELECT week FROM xiaoqu_metric_weekly m WHERE m.xiaoqu_id=x.id ORDER BY week DESC LIMIT 1) AS latest_week "
        "FROM community_poi cp JOIN xiaoqu x ON x.id = cp.community_id "
        "WHERE cp.poi_id = ? ORDER BY cp.distance_km ASC, x.id ASC LIMIT ?",
        (poi_id, int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]

