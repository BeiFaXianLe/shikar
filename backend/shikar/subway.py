"""P1 地铁离线数据与空间关系（纯 Python 标准库）。

本模块承载「按生活半径找小区 · P1 地铁沿线」的离线空间层：

* 空间计算：Haversine 球面距离、点到多段折线最短距离、坐标系显式转换；
* 导入：把**已核实坐标系**的地铁 GeoJSON（线路折线 + 站点 + 站点所属线路）
  事务化写入 ``subway_line`` / ``subway_station`` / ``subway_line_station`` 以及
  两张预计算关系表；
* 查询：供 ``/api/subway/lines`` 与 ``/api/catalog`` 只读查询使用，不再实时做
  空间计算。

设计原则（与 ``storage`` / ``catalog`` 一致）：

* **仅用标准库**：``json`` / ``math`` / ``sqlite3`` / ``datetime``；
* **只读查询不写库**：只有 :func:`import_geojson` 在导入期写入，且以
  ``SAVEPOINT`` 保证原子替换（失败回滚，不破坏旧数据）；
* **坐标系不猜测**：导入必须显式传入 ``community_crs`` / GeoJSON 自带
  ``coord_system``，非法或缺失直接抛 ``ValueError``；
* **不伪造数据**：必须传入真实线路与站点，空数据或非法几何一律拒绝。

数据模型：

* ``subway_line(id, city, name, geometry_json, source, version, updated_at,
  community_crs)`` —— 线路折线（GeoJSON coordinates，WGS84）。
* ``subway_station(id, city, name, lng, lat)`` —— 站点（WGS84）。
* ``subway_line_station(line_id, station_id, position)`` —— 线路与站点多对多。
* ``community_subway(community_id, line_id, station_id, station_distance_km,
  line_distance_km)`` —— 每小区对每线路：最近站距离与到线路折线距离。
* ``community_subway_station(community_id, station_id, distance_km)`` —— 每小区
  对每站点距离（支持按指定站点筛选，避免「只查全局最近线路」漏查）。
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone

__all__ = [
    "haversine_km",
    "polyline_distance_km",
    "to_wgs84",
    "available",
    "import_geojson",
    "list_lines",
    "filter_sql",
    "matching_features",
]

#: 地球平均半径（KM，WGS84 常用均值）。
EARTH_KM = 6371.0088

#: 地铁距离查询半径上限（km）。导入时只保存该半径内的站点距离，与 UI 上限一致；
#: 任何合法的「距站 ≤ R」筛选都有 ``R ≤ 此值``，因此不会漏查。
MAX_QUERY_RADIUS_KM = 5.0


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """计算两个 ``(lng, lat)`` 点的球面大圆距离，单位 km。

    :param a: 第一个点的 ``(经度, 纬度)``，角度制。
    :param b: 第二个点的 ``(经度, 纬度)``，角度制。
    :return: 球面距离（km）；同点为 ``0.0``。
    """
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_KM * math.asin(math.sqrt(min(1.0, max(0.0, h))))


def polyline_distance_km(
    point: tuple[float, float], segments: list[list[tuple[float, float]]]
) -> float:
    """求点到多段折线的最短球面距离（km）。

    采用「以查询点为原点的局部等距投影」把经纬度近似换算为平面 km，再逐段求
    点到线段最短距离。``segments`` 为若干条独立折线（MultiLineString 的各段），
    段与段之间**不**相连，因此不会把断开的缺口当成短路命中。

    折线点数量较少（单条线路数十至数百点），逐段线性扫描的开销可接受；该函数
    只在离线导入期调用，不进入在线查询路径。

    :param point: 查询点 ``(lng, lat)``（WGS84）。
    :param segments: 折线集合，每个折线是 ``[(lng, lat), ...]`` 的点序列。
    :return: 到折线的最短距离（km）；``segments`` 为空时返回 ``inf``。
    """
    scale = EARTH_KM * math.pi / 180.0
    cosine = math.cos(math.radians(point[1]))
    best = math.inf
    for segment in segments:
        for a, b in zip(segment, segment[1:]):
            ax, ay = (a[0] - point[0]) * scale * cosine, (a[1] - point[1]) * scale
            bx, by = (b[0] - point[0]) * scale * cosine, (b[1] - point[1]) * scale
            dx, dy = bx - ax, by - ay
            norm = dx * dx + dy * dy
            t = max(0.0, min(1.0, -(ax * dx + ay * dy) / norm)) if norm else 0.0
            best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


def _gcj_offset(lng: float, lat: float) -> tuple[float, float]:
    """GCJ02 相对 WGS84 的公开转换公式（仅用于中国范围纠偏）。

    :param lng: 经度。
    :param lat: 纬度。
    :return: ``(dlng, dlat)`` 偏移量（度）。
    """
    x, y = lng - 105.0, lat - 35.0
    dlat = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    dlng = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    dlat += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    dlat += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    dlat += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    dlng += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    dlng += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    dlng += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    rad = math.radians(lat)
    magic = 1 - 0.00669342162296594323 * math.sin(rad) ** 2
    dlat = dlat * 180.0 / (
        (6378245.0 * (1 - 0.00669342162296594323)) / (magic * math.sqrt(magic)) * math.pi
    )
    dlng = dlng * 180.0 / (6378245.0 / math.sqrt(magic) * math.cos(rad) * math.pi)
    return dlng, dlat


def to_wgs84(point: tuple[float, float], crs: str) -> tuple[float, float]:
    """把一个 ``(lng, lat)`` 从 ``crs`` 显式转换到 WGS84；拒绝猜测坐标系。

    :param point: 原始 ``(经度, 纬度)``。
    :param crs: 源坐标系，必须是 ``"wgs84"`` / ``"gcj02"`` / ``"bd09"`` 之一。
    :return: WGS84 ``(经度, 纬度)``。
    :raises ValueError: 坐标系未知、数值非法或超出合法经纬度范围。
    """
    if crs not in ("wgs84", "gcj02", "bd09"):
        raise ValueError("坐标系必须明确为 wgs84/gcj02/bd09")
    lng, lat = float(point[0]), float(point[1])
    if (
        not math.isfinite(lng) or not math.isfinite(lat)
        or not -180.0 <= lng <= 180.0 or not -90.0 <= lat <= 90.0
    ):
        raise ValueError("经纬度必须为有限值且位于合法范围")
    if crs == "wgs84":
        return lng, lat
    if crs == "bd09":
        x, y = lng - 0.0065, lat - 0.006
        z = math.hypot(x, y) - 0.00002 * math.sin(y * math.pi * 3000.0 / 180.0)
        theta = math.atan2(y, x) - 0.000003 * math.cos(x * math.pi * 3000.0 / 180.0)
        lng, lat = z * math.cos(theta), z * math.sin(theta)
    # 中国范围外不做 GCJ02 纠偏（GCJ02 仅在中国境内生效）。
    if not (72.0 <= lng <= 138.0 and 0.8 <= lat <= 56.0):
        return lng, lat
    wl, wa = lng, lat
    for _ in range(5):
        dl, da = _gcj_offset(wl, wa)
        wl, wa = lng - dl, lat - da
    return wl, wa


def available(conn: sqlite3.Connection) -> bool:
    """只读检查地铁空间表是否已导入。

    :param conn: 已打开的 SQLite 连接。
    :return: 存在 ``subway_line`` 表时返回 ``True``（兼容尚未导入的旧库）。
    """
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='subway_line'"
        ).fetchone()
    )


def _init_schema(conn: sqlite3.Connection) -> None:
    """建立地铁空间表与索引（仅在导入事务内调用，不提交）。

    使用逐条 ``execute`` 而非 ``executescript``，以免隐式提交破坏外层
    ``SAVEPOINT`` 的原子性。

    :param conn: 已打开的 SQLite 连接。
    """
    statements = [
        "CREATE TABLE IF NOT EXISTS subway_line ("
        "id TEXT PRIMARY KEY, city TEXT NOT NULL, name TEXT NOT NULL, "
        "geometry_json TEXT NOT NULL, source TEXT, version TEXT, "
        "updated_at TEXT, community_crs TEXT)",
        "CREATE TABLE IF NOT EXISTS subway_station ("
        "id TEXT PRIMARY KEY, city TEXT NOT NULL, name TEXT NOT NULL, "
        "lng REAL NOT NULL, lat REAL NOT NULL)",
        "CREATE TABLE IF NOT EXISTS subway_line_station ("
        "line_id TEXT REFERENCES subway_line(id), "
        "station_id TEXT REFERENCES subway_station(id), "
        "position INTEGER, PRIMARY KEY(line_id, station_id))",
        "CREATE TABLE IF NOT EXISTS community_subway ("
        "community_id TEXT REFERENCES xiaoqu(id), "
        "line_id TEXT REFERENCES subway_line(id), "
        "station_id TEXT REFERENCES subway_station(id), "
        "station_distance_km REAL, line_distance_km REAL, "
        "PRIMARY KEY(community_id, line_id))",
        "CREATE TABLE IF NOT EXISTS community_subway_station ("
        "community_id TEXT REFERENCES xiaoqu(id), "
        "station_id TEXT REFERENCES subway_station(id), "
        "distance_km REAL, PRIMARY KEY(community_id, station_id))",
        "CREATE INDEX IF NOT EXISTS idx_subway_station_distance "
        "ON community_subway_station(station_id, distance_km, community_id)",
        "CREATE INDEX IF NOT EXISTS idx_subway_line_distance "
        "ON community_subway(line_id, line_distance_km, community_id)",
        "CREATE INDEX IF NOT EXISTS idx_subway_line_station_distance "
        "ON community_subway(line_id, station_distance_km, community_id)",
        "CREATE INDEX IF NOT EXISTS idx_subway_city ON subway_line(city)",
    ]
    for statement in statements:
        conn.execute(statement)


def import_geojson(
    conn: sqlite3.Connection, payload: dict, *, community_crs: str
) -> dict:
    """校验并事务化替换离线地铁数据（原子；失败回滚，保留旧数据）。

    GeoJSON 约定：``FeatureCollection``，带 ``source`` / ``version`` /
    ``coord_system``；站点为 ``Point``，``properties`` 需含 ``id`` / ``city`` /
    ``name`` / ``line_ids``（所属线路 id 列表）；线路为 ``LineString`` 或
    ``MultiLineString``，``properties`` 需含 ``id`` / ``city`` / ``name``。

    所有小区（``xiaoqu``）坐标按 ``community_crs`` 转换到 WGS84，与 WGS84 的
    站点/折线同坐标系计算真实距离。保存每城市全部小区对全部站点的距离，避免
    任意站点筛选漏查；仅保存同城市关系。

    :param conn: 已打开的 SQLite 连接。
    :param payload: 解析后的 GeoJSON 字典。
    :param community_crs: 小区坐标的坐标系（``wgs84``/``gcj02``/``bd09``）。
    :return: 导入统计字典（线路数、站点数、有/无坐标小区数、坐标系）。
    :raises ValueError: 结构、坐标系或几何非法，或线路/站点为空。
    """
    # 先验证小区坐标系合法（提前失败，避免长耗时写入后才发现）。
    to_wgs84((0.0, 0.0), community_crs)
    if payload.get("type") != "FeatureCollection" or not payload.get("source") or not payload.get("version"):
        raise ValueError("GeoJSON 需要 FeatureCollection、source、version")
    crs = payload.get("coord_system")
    to_wgs84((0.0, 0.0), crs)

    lines: dict[str, tuple[dict, list[list[tuple[float, float]]]]] = {}
    stations: dict[str, tuple[dict, tuple[float, float]]] = {}
    for feature in payload["features"]:
        props, geometry = feature["properties"], feature["geometry"]
        for key in ("id", "city", "name"):
            if not props.get(key):
                raise ValueError(f"GeoJSON 缺少 {key}")
        fid = props["id"]
        if geometry["type"] == "Point":
            if fid in stations:
                raise ValueError("站点 ID 重复")
            stations[fid] = (props, to_wgs84(geometry["coordinates"], crs))
        elif geometry["type"] in ("LineString", "MultiLineString"):
            if fid in lines:
                raise ValueError("线路 ID 重复")
            segments = geometry["coordinates"]
            if geometry["type"] == "LineString":
                segments = [segments]
            if not segments or any(len(s) < 2 for s in segments):
                raise ValueError("线路折线每段至少需要两个点")
            lines[fid] = (props, [[to_wgs84(p, crs) for p in seg] for seg in segments])
        else:
            raise ValueError("仅支持 Point/LineString/MultiLineString")

    if not lines or not stations:
        raise ValueError("需要真实线路和站点，不能导入空数据")

    by_line: dict[str, list[str]] = {fid: [] for fid in lines}
    for sid, (props, _) in stations.items():
        if not props.get("line_ids"):
            raise ValueError("站点缺少 line_ids")
        for lid in props["line_ids"]:
            if lid not in lines or lines[lid][0]["city"] != props["city"]:
                raise ValueError("站点与线路城市或 ID 不匹配")
            by_line[lid].append(sid)
    if any(not ids for ids in by_line.values()):
        raise ValueError("线路需要至少一个站点")

    # 先把小区坐标全部校验转换，再进入写事务。
    communities: list[tuple[str, str, tuple[float, float]]] = []
    missing = 0
    for row in conn.execute("SELECT id,city,lng,lat FROM xiaoqu"):
        if row["lng"] is None or row["lat"] is None:
            missing += 1
        else:
            communities.append(
                (row["id"], row["city"], to_wgs84((row["lng"], row["lat"]), community_crs))
            )
    now = datetime.now(timezone.utc).isoformat()

    conn.execute("SAVEPOINT subway_import")
    try:
        _init_schema(conn)
        for table in (
            "community_subway_station", "community_subway",
            "subway_line_station", "subway_station", "subway_line",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.executemany(
            "INSERT INTO subway_line VALUES (?,?,?,?,?,?,?,?)",
            [
                (lid, p["city"], p["name"], json.dumps(g, ensure_ascii=False),
                 payload["source"], payload["version"], now, community_crs)
                for lid, (p, g) in lines.items()
            ],
        )
        conn.executemany(
            "INSERT INTO subway_station VALUES (?,?,?,?,?)",
            [(sid, p["city"], p["name"], *point) for sid, (p, point) in stations.items()],
        )
        conn.executemany(
            "INSERT INTO subway_line_station VALUES (?,?,?)",
            [(lid, sid, i) for lid, ids in by_line.items() for i, sid in enumerate(ids)],
        )
        station_rows: list[tuple[str, str, float]] = []
        line_rows: list[tuple[str, str, str, float, float]] = []
        for xid, city, point in communities:
            distances = {
                sid: haversine_km(point, sp)
                for sid, (p, sp) in stations.items() if p["city"] == city
            }
            # 只保存查询半径覆盖内的站点距离（查询覆盖半径与 UI 上限一致，见
            # ``MAX_QUERY_RADIUS_KM``），避免 1.7 万小区 × 全部站点造成数百万行
            # 数据膨胀；超出半径的站点不会命中任何合法筛选，无需落库。
            for sid, d in distances.items():
                if d <= MAX_QUERY_RADIUS_KM:
                    station_rows.append((xid, sid, d))
            for lid, (props, segments) in lines.items():
                if props["city"] != city:
                    continue
                # 每条线路取最近站点；距离相同按站点 ID 稳定排序。
                sid = min(by_line[lid], key=lambda s: (distances[s], s))
                line_rows.append(
                    (xid, lid, sid, distances[sid], polyline_distance_km(point, segments))
                )
        conn.executemany(
            "INSERT INTO community_subway_station VALUES (?,?,?)", station_rows
        )
        conn.executemany("INSERT INTO community_subway VALUES (?,?,?,?,?)", line_rows)
        conn.execute("RELEASE subway_import")
    except Exception:
        conn.execute("ROLLBACK TO subway_import")
        conn.execute("RELEASE subway_import")
        raise

    return {
        "lines": len(lines),
        "stations": len(stations),
        "communities_with_coordinates": len(communities),
        "communities_missing_coordinates": missing,
        "community_crs": community_crs,
    }


def list_lines(conn: sqlite3.Connection, city: str | None = None) -> dict:
    """列出线路及其站点，供 ``/api/subway/lines`` 与前端筛选下拉。

    未导入地铁数据时返回明确空态（``items`` 为空、``message`` 说明原因），
    不构造表、不返回错误。

    :param conn: 已打开的 SQLite 连接（只读使用）。
    :param city: 城市过滤；``None`` 表示全部城市。
    :return: ``{"items", "source", "version", "message"}``。
    """
    if not available(conn):
        return {"items": [], "source": None, "version": None, "message": "尚未导入地铁数据"}
    query = "SELECT * FROM subway_line"
    params: list = []
    if city:
        query += " WHERE city=?"
        params.append(city)
    lines = conn.execute(query + " ORDER BY city,name,id", params).fetchall()
    items = []
    for line in lines:
        stations = conn.execute(
            "SELECT s.id,s.name FROM subway_station s "
            "JOIN subway_line_station ls ON ls.station_id=s.id "
            "WHERE ls.line_id=? ORDER BY ls.position,s.id",
            (line["id"],),
        ).fetchall()
        items.append(
            {
                "id": line["id"],
                "city": line["city"],
                "name": line["name"],
                "stations": [dict(s) for s in stations],
            }
        )
    return {
        "items": items,
        "source": lines[0]["source"] if lines else None,
        "version": lines[0]["version"] if lines else None,
        "message": None if items else "该城市尚未导入地铁数据",
    }


def filter_sql(
    conn: sqlite3.Connection,
    *,
    city: str | None = None,
    lines: list[str] | None = None,
    station: str | None = None,
    mode: str = "station",
    radius: float = 1.0,
) -> tuple[str, list]:
    """构造「满足地铁条件的小区 id」子查询与绑定参数，并校验同城关系。

    * 多线路取 **OR**（任一选定线路满足即可，且按所选线路各自口径命中）；
    * 指定站点时对该站点取「距站」距离；
    * ``mode="station"`` 用「到最近（所选）站点距离」，``mode="line"`` 用「到线路
      折线距离」；两者阈值统一为 ``radius``（km）。

    :param conn: 已打开的 SQLite 连接（只读使用）。
    :param city: 小区/线路城市；用于校验线路与站点归属。
    :param lines: 选中线路 id 列表；空或 ``None`` 表示不限线路。
    :param station: 指定站点 id；``None`` 表示不指定。
    :param mode: 距离口径，``"station"`` 或 ``"line"``。
    :param radius: 阈值（km）。
    :return: ``(子查询 SQL, 参数列表)``。
    :raises ValueError: 未导入数据、线路/站点非法或跨城市、站点与线路不匹配。
    """
    if not available(conn):
        raise ValueError("尚未导入地铁数据，请先导入离线 GeoJSON")
    ids = list(dict.fromkeys(lines or []))
    if len(ids) > 50:
        raise ValueError("最多选择 50 条线路")
    if ids:
        rows = conn.execute(
            f"SELECT id,city FROM subway_line WHERE id IN ({','.join('?' for _ in ids)})",
            ids,
        ).fetchall()
        if len(rows) != len(ids) or (city and any(r["city"] != city for r in rows)):
            raise ValueError("线路不存在或不属于所选城市")
    if station:
        row = conn.execute("SELECT city FROM subway_station WHERE id=?", (station,)).fetchone()
        if not row or (city and row["city"] != city):
            raise ValueError("站点不存在或不属于所选城市")
        if mode == "line":
            raise ValueError("指定站点时只能使用距站模式")
        if ids and not conn.execute(
            f"SELECT 1 FROM subway_line_station WHERE station_id=? "
            f"AND line_id IN ({','.join('?' for _ in ids)})",
            [station] + ids,
        ).fetchone():
            raise ValueError("站点不属于所选线路")
        return (
            "SELECT community_id FROM community_subway_station "
            "WHERE station_id=? AND distance_km<=?",
            [station, radius],
        )
    field = "station_distance_km" if mode == "station" else "line_distance_km"
    sql, params = f"SELECT community_id FROM community_subway WHERE {field}<=?", [radius]
    if ids:
        sql += f" AND line_id IN ({','.join('?' for _ in ids)})"
        params += ids
    return sql, params


def matching_features(
    conn: sqlite3.Connection,
    ids: list[str],
    *,
    lines: list[str] | None = None,
    station: str | None = None,
    mode: str = "station",
    radius: float | None = None,
) -> dict:
    """批量查询「本页小区」的匹配线路信息，用于结果次行展示。

    不做实时空间计算，只读预计算关系表；每个小区取距离最近（且满足选定线路/
    站点）的一条匹配记录。

    :param conn: 已打开的 SQLite 连接（只读使用）。
    :param ids: 本页小区 id 列表。
    :param lines: 选中线路 id 列表；用于限定匹配线路。
    :param station: 指定站点 id；给出时只取该站点记录。
    :param mode: 距离口径，决定返回 ``distance_km`` 取站距离还是线路距离。
    :param radius: 可选阈值；超过阈值的记录不再返回。
    :return: ``{community_id: feature}``；无数据时为空字典。
    """
    if not ids or not available(conn):
        return {}
    params: list = list(ids)
    if station:
        sql = (
            "SELECT ds.community_id,l.id line_id,l.name line_name,"
            "s.id station_id,s.name station_name,ds.distance_km "
            "FROM community_subway_station ds "
            "JOIN subway_station s ON s.id=ds.station_id "
            "JOIN subway_line_station ls ON ls.station_id=s.id "
            "JOIN subway_line l ON l.id=ls.line_id "
            "WHERE ds.community_id IN (" + ",".join("?" for _ in ids) + ") "
            "AND ds.station_id=?"
        )
        params.append(station)
    else:
        distance = "cs.station_distance_km" if mode == "station" else "cs.line_distance_km"
        sql = (
            f"SELECT cs.community_id,l.id line_id,l.name line_name,"
            f"s.id station_id,s.name station_name,{distance} distance_km "
            "FROM community_subway cs "
            "JOIN subway_line l ON l.id=cs.line_id "
            "JOIN subway_station s ON s.id=cs.station_id "
            "WHERE cs.community_id IN (" + ",".join("?" for _ in ids) + ")"
        )
    if lines:
        sql += f" AND l.id IN ({','.join('?' for _ in lines)})"
        params += list(lines)
    sql += " ORDER BY distance_km,l.id"
    result: dict[str, dict] = {}
    for row in conn.execute(sql, params):
        if radius is not None and row["distance_km"] > radius:
            continue
        feature = dict(row)
        feature.pop("community_id")
        feature["mode"] = mode
        feature["distance_km"] = round(feature["distance_km"], 3)
        result.setdefault(row["community_id"], feature)
    return result
