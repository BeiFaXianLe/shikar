"""把已核实地铁 GeoJSON 导入 ``data/shikar.db``（离线预计算，不重建行情数据）。

只做两件事：读取 GeoJSON → 调用 ``shikar.subway.import_geojson`` 事务化写入
地铁空间表与两张预计算距离关系表。不触碰 ``xiaoqu`` / 价格指标 / 其他业务表。

坐标系必须显式传入 ``--community-crs``（小区坐标的坐标系）。站点与线路在
GeoJSON 内自带 ``coord_system``，同样为显式声明，二者都不猜测。

用法::

    python scripts/build_subway.py --geojson data/subway/shikar-subway.json \
        --community-crs wgs84
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from shikar.subway import import_geojson  # noqa: E402


def main() -> None:
    """命令行入口：读取 GeoJSON、导入并打印统计。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--geojson", type=Path, required=True, help="已核实地铁 GeoJSON")
    parser.add_argument("--db", type=Path, default=ROOT / "data/shikar.db")
    parser.add_argument(
        "--community-crs",
        choices=["wgs84", "gcj02", "bd09"],
        required=True,
        help="小区坐标的坐标系（须先核实）",
    )
    args = parser.parse_args()

    payload = json.loads(args.geojson.read_text(encoding="utf-8"))
    # mode=rw 防止路径拼错时静默创建空库。
    with sqlite3.connect(args.db.resolve().as_uri() + "?mode=rw", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        report = import_geojson(conn, payload, community_crs=args.community_crs)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
