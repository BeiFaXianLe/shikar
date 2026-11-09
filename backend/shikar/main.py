"""shikar 启动入口：打开数据库、可选播种 Demo 数据、同时提供 API 与静态网页。

本模块是项目「一键本地启动」的入口，仅使用 Python 标准库
（``argparse`` / ``sqlite3`` / ``math`` 等），不引入任何第三方依赖。

包含两部分：

* :func:`seed_demo` —— **确定性**生成 Demo 数据（北京 / 天津共 6 个小区、每小区
  10 周），经由 :func:`shikar.pipeline.ingest` 写入库中，使 ``/api/overview`` 有
  内容。价格走势有升有降，温度 / 阶段呈现差异。
* :func:`run` —— 解析命令行参数，打开 ``sqlite3`` 连接并 ``init_db``，按需播种，
  再以 :func:`shikar.api.create_server` 起服务并 ``serve_forever()``。

用法::

    python -m shikar.main --db shikar.db --web-root web --seed
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from typing import Optional

from shikar import pipeline, storage, subway
from shikar.api import create_server
from shikar.sources.base import MetricRecord

__all__ = ["seed_demo", "ensure_subway", "repo_root", "run"]

#: 仓库根目录：``<repo>/backend/shikar/main.py`` 的上三级目录。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
#: 默认地铁 GeoJSON（相对仓库根，避免 ``cd backend`` 启动时按当前目录找不到）。
_DEFAULT_SUBWAY_GEOJSON = os.path.join(_REPO_ROOT, "data", "subway", "shikar-subway.json")


def repo_root() -> str:
    """返回仓库根目录的绝对路径（与当前工作目录无关）。"""
    return _REPO_ROOT

#: Demo 每小区周数。
_DEMO_WEEKS = 10

#: Demo 小区定义：(city, district, plate, name, base_price, weekly_step)；
#: ``weekly_step`` 为每周期价格相对基价的线性增量（正=涨、负=跌），走势有升有降。
_DEMO_SPECS: tuple[tuple[str, str, str, str, float, float], ...] = (
    ("北京", "朝阳", "望京", "望京A区", 62000.0, 650.0),
    ("北京", "海淀", "中关村", "中关村B区", 88000.0, 900.0),
    ("北京", "昌平", "回龙观", "回龙观C区", 42000.0, -500.0),
    ("天津", "和平", "南京路", "和平D区", 38000.0, 320.0),
    ("天津", "河西", "梅江", "梅江E区", 30000.0, -260.0),
    ("天津", "南开", "鼓楼", "鼓楼F区", 26000.0, 120.0),
)


def seed_demo(conn: sqlite3.Connection) -> dict:
    """确定性地生成 Demo 数据并写入库。

    对 :data:`_DEMO_SPECS` 中每个小区，按周生成 ``_DEMO_WEEKS`` 条记录：价格为
    ``base_price + weekly_step * i``（线性，有升有降），``supply`` / ``vol30`` /
    ``bargain`` / ``heat`` 由小区序号与周序号经简单公式派生，全部为确定性数值
    （不依赖随机数），随后通过 :func:`shikar.pipeline.ingest` 统一入库并重算派生
    指标。

    由于 :func:`pipeline.ingest` 写入走 ``upsert``、重算只做 ``UPDATE``，重复调用
    本函数不会产生重复行（幂等）。

    :param conn: 已打开的 SQLite 连接（须先 ``storage.init_db``）。
    :return: 形如 ``{"xiaoqu": 小区数, "metrics": 指标行数}`` 的统计字典。
    """
    records: list[MetricRecord] = []
    for idx, (city, district, plate, name, base_price, step) in enumerate(_DEMO_SPECS):
        for i in range(_DEMO_WEEKS):
            # 由小区序号 idx 与周序号 i 派生的确定性指标，保证可复现。
            supply = 80 + idx * 15 + i * 2
            vol30 = 10 + idx * 3 + (i % 5)
            bargain = round(0.05 + 0.01 * (idx % 3) + 0.002 * i, 4)
            heat = round(40.0 + idx * 8 + i * 1.5, 2)
            records.append(
                MetricRecord(
                    city=city,
                    district=district,
                    plate=plate,
                    xiaoqu_id="",
                    xiaoqu_name=name,
                    week=f"2025-W{i + 1:02d}",
                    price=round(base_price + step * i, 2),
                    supply=supply,
                    vol30=vol30,
                    bargain=bargain,
                    heat=heat,
                )
            )
    result = pipeline.ingest(conn, records)
    return {"xiaoqu": result["xiaoqu"], "metrics": result["metrics"]}


def _is_empty(conn: sqlite3.Connection) -> bool:
    """判断库中是否尚无任何小区主数据（用于 ``--seed`` 的「库为空才播种」）。

    :param conn: 已打开的 SQLite 连接（须已建表）。
    :return: 无任何小区记录时返回 ``True``。
    """
    cur = conn.execute("SELECT COUNT(*) AS n FROM xiaoqu")
    row = cur.fetchone()
    return bool(row[0] == 0)


def ensure_subway(
    conn: sqlite3.Connection,
    geojson_path: str,
    community_crs: str = "wgs84",
    *,
    strict: bool = False,
) -> Optional[dict]:
    """若尚未导入地铁关系表且 GeoJSON 存在，则在启动时离线构建一次。

    仓储型数据库不提交体积巨大的地铁关系表（1.7 万小区 × 站点的预计算距离可达
    上百 MB），改由本函数在本地启动时按需重建，保持「clone 后可重建」约定。

    :param conn: 已打开的 SQLite 连接（读写）。
    :param geojson_path: 适配套路后的地铁 GeoJSON 路径。
    :param community_crs: 小区坐标的坐标系（须先核实，默认 ``wgs84``）。
    :param strict: 为真时，若需构建但文件缺失则抛错（显式指定路径场景）；否则打印
        明确告警后跳过（默认路径缺失时服务仍可启动，仅地铁筛选为空态）。
    :return: 导入统计字典；跳过（已有表）、或文件缺失且非 strict 时返回 ``None``。
    :raises FileNotFoundError: ``strict`` 为真且文件缺失。
    """
    if subway.available(conn):
        return None
    if not geojson_path or not os.path.isfile(geojson_path):
        message = (
            f"未找到地铁 GeoJSON：{geojson_path}。库中尚无地铁关系表，"
            "地铁筛选将为空态。请运行 scripts/build_subway.py 或指定 --subway-geojson。"
        )
        if strict:
            raise FileNotFoundError(message)
        print(f"[警告] {message}")
        return None
    with open(geojson_path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    return subway.import_geojson(conn, payload, community_crs=community_crs)


def run(argv: Optional[list[str]] = None) -> None:
    """启动本地服务：解析参数、打开数据库、按需播种、提供 API 与静态网页。

    :param argv: 命令行参数列表；``None`` 时从 ``sys.argv`` 读取（便于测试注入）。
    """
    parser = argparse.ArgumentParser(
        prog="shikar.main", description="shikar 本地服务启动入口（零依赖）"
    )
    parser.add_argument(
        "--db",
        default=os.path.join(_REPO_ROOT, "data", "shikar.db"),
        help="SQLite 数据库文件路径（默认仓库 data/shikar.db，与当前目录无关）",
    )
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址（默认仅回环）")
    parser.add_argument("--port", type=int, default=8080, help="监听端口")
    parser.add_argument(
        "--web-root",
        default=os.path.join(_REPO_ROOT, "web"),
        help="静态文件根目录（默认仓库 web/，与当前目录无关）",
    )
    parser.add_argument("--seed", action="store_true", help="库为空时播种 Demo 数据")
    parser.add_argument(
        "--subway-geojson",
        default=_DEFAULT_SUBWAY_GEOJSON,
        help="地铁 GeoJSON 路径（默认仓库 data/subway/shikar-subway.json）；"
        "库中尚无地铁表时启动自动导入",
    )
    parser.add_argument(
        "--community-crs",
        default="wgs84",
        choices=["wgs84", "gcj02", "bd09"],
        help="小区坐标的坐标系（须先核实；默认 wgs84）",
    )
    parser.add_argument(
        "--require-subway",
        action="store_true",
        help="要求启动时必须具备地铁数据；缺失 GeoJSON 且无地铁表时报错退出",
    )
    args = parser.parse_args(argv)

    conn = sqlite3.connect(args.db, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    storage.init_db(conn)

    if args.seed and _is_empty(conn):
        stats = seed_demo(conn)
        print(f"已播种 Demo 数据：小区 {stats['xiaoqu']} 个，指标 {stats['metrics']} 行")

    report = ensure_subway(
        conn, args.subway_geojson, community_crs=args.community_crs,
        strict=args.require_subway,
    )
    if report:
        print(
            "已导入地铁关系表：线路 {lines} 条、站点 {stations} 个、"
            "有坐标小区 {communities_with_coordinates}（缺坐标 {communities_missing_coordinates}）".format(**report)
        )

    server = create_server(conn, host=args.host, port=args.port, web_root=args.web_root)
    host, port = server.server_address[0], server.server_address[1]
    print(f"shikar 服务已启动： http://{host}:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n收到中断，正在退出……")
    finally:
        server.server_close()
        conn.close()


if __name__ == "__main__":  # pragma: no cover  （脚本入口）
    run()
