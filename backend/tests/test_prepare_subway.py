"""``scripts/prepare_subway_geojson.py`` 适配层测试（不联网）。

验证：把「站点/线路原始 GeoJSON」适配为 shikar 导入格式——线路 feature 带
``id``/``city``/``name``，站点按**到线路折线的距离**归属线路（含换乘站的多线路），
输出显式 ``coord_system: wgs84``，且可被 ``subway.import_geojson`` 直接导入。
含真实换乘站回归（P1-01）与启动导入测试（P1-02）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3

import pytest

from shikar import storage, subway

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_module():
    """按文件路径加载 ``scripts/prepare_subway_geojson.py``。"""
    path = os.path.join(_REPO_ROOT, "scripts", "prepare_subway_geojson.py")
    spec = importlib.util.spec_from_file_location("prepare_subway_geojson", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_STATIONS = {
    "type": "FeatureCollection",
    "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [116.0, 40.0]},
         "properties": {"name": "换乘站", "pshort": "1号线"}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [116.0, 40.0]},
         "properties": {"name": "换乘站", "pshort": "2号线"}},
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [116.02, 40.0]},
         "properties": {"name": "终点站", "pshort": "1号线"}},
    ],
}
_LINES = {
    "type": "FeatureCollection",
    "features": [
        {"type": "Feature", "geometry": {"type": "LineString",
         "coordinates": [[116.0, 40.0], [116.02, 40.0]]},
         "properties": {"name": "地铁1号线", "short": "1号线"}},
        {"type": "Feature", "geometry": {"type": "LineString",
         "coordinates": [[116.0, 40.0], [116.0, 40.02]]},
         "properties": {"name": "地铁2号线", "short": "2号线"}},
    ],
}


def test_prepare_merges_transfer_and_imports(tmp_path):
    mod = _load_module()
    # 写出临时原始文件（文件名前缀用于城市推断）。
    station_path = tmp_path / "beijing-station.json"
    line_path = tmp_path / "beijing-line.json"
    station_path.write_text(json.dumps(_STATIONS, ensure_ascii=False), encoding="utf-8")
    line_path.write_text(json.dumps(_LINES, ensure_ascii=False), encoding="utf-8")

    payload = mod.prepare(
        [str(station_path)], [str(line_path)], {"beijing": "北京"}, source="t", version="v1"
    )
    assert payload["coord_system"] == "wgs84"
    points = [f for f in payload["features"] if f["geometry"]["type"] == "Point"]
    lines = [f for f in payload["features"] if f["geometry"]["type"] != "Point"]
    # 换乘站(同坐标)合并为一个点，两个线路各一个线 feature。
    assert len(lines) == 2
    assert len(points) == 2
    transfer = next(p for p in points if p["properties"]["name"] == "换乘站")
    assert set(transfer["properties"]["line_ids"]) == {"北京:1号线", "北京:2号线"}

    # 可被 import_geojson 直接导入。
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    storage.init_db(c)
    storage.upsert_xiaoqu(c, {"id": "x", "name": "x", "city": "北京", "district": "d",
                              "lng": 116.0, "lat": 40.0})
    report = subway.import_geojson(c, payload, community_crs="wgs84")
    assert report["lines"] == 2 and report["stations"] == 2
    c.close()


def test_ensure_subway_builds_once(tmp_path):
    """``main.ensure_subway`` 缺地图表时构建一次，已有表时跳过。"""
    import json as _json

    from shikar import main as main_mod

    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    storage.init_db(c)
    storage.upsert_xiaoqu(c, {"id": "x", "name": "x", "city": "北京", "district": "d",
                              "lng": 116.0, "lat": 40.0})
    mod = _load_module()
    payload = mod.prepare(
        [str(_write(tmp_path / "beijing-station.json", _STATIONS))],
        [str(_write(tmp_path / "beijing-line.json", _LINES))],
        {"beijing": "北京"}, source="t", version="v1",
    )
    geo = tmp_path / "shikar-subway.json"
    geo.write_text(_json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    r1 = main_mod.ensure_subway(c, str(geo), community_crs="wgs84")
    assert r1 and r1["lines"] == 2
    # 第二次已有表 → 跳过（返回 None）。
    assert main_mod.ensure_subway(c, str(geo), community_crs="wgs84") is None
    c.close()


def _write(path, obj):
    """把对象写入 UTF-8 JSON 文件并返回路径。"""
    import json as _json
    path.write_text(_json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# P1-01 真实换乘站回归：提交的 data/subway/shikar-subway.json 必须保留换乘关系
# --------------------------------------------------------------------------- #
def test_real_transfer_relationships_preserved():
    """真实数据里西直门=4/13/2、国贸=10/1、望京=14/15、天津站=2/3/9 等换乘要保留。"""
    path = os.path.join(_REPO_ROOT, "data", "subway", "shikar-subway.json")
    if not os.path.isfile(path):
        import pytest
        pytest.skip("未提交 shikar-subway.json")
    data = json.loads(open(path, encoding="utf-8").read())
    by_name = {}
    for f in data["features"]:
        if f["geometry"]["type"] == "Point":
            by_name.setdefault(f["properties"]["name"], set()).update(f["properties"]["line_ids"])

    def line_shorts(name, city):
        return {lid.split(":", 1)[1] for lid in by_name.get(name, set()) if lid.startswith(city + ":")}

    # 北京换乘
    assert {"4号线大兴线", "13号线", "2号线"}.issubset(line_shorts("西直门", "北京"))
    assert {"10号线", "1号线"}.issubset(line_shorts("国贸", "北京"))
    assert {"14号线东段", "15号线"}.issubset(line_shorts("望京", "北京"))
    # 天津换乘（审核点名 天津站缺 3/9）
    assert {"2号线", "3号线", "9号线"}.issubset(line_shorts("天津站", "天津"))
    # 换乘站总量应显著大于 0（此前缺陷为 0）。
    transfer = [n for n, ls in by_name.items() if len(ls) > 1]
    assert len(transfer) > 50


def test_prepare_tolerance_rejects_far_line(tmp_path):
    """离折线 > 阈值的线路不得被误配到站点（避免邻近线路假换乘）。"""
    mod = _load_module()
    # 站点在 A 线端点(116,40)，B 线在纬度 40.01 处（约 1.1km），阈值 50m 不应配 B。
    stations = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [116.0, 40.0]},
         "properties": {"name": "甲"}},
    ]}
    lines = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[116.0, 40.0], [116.02, 40.0]]},
         "properties": {"name": "A线", "short": "A"}},
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[116.0, 40.01], [116.02, 40.01]]},
         "properties": {"name": "B线", "short": "B"}},
    ]}
    payload = mod.prepare(
        [str(_write(tmp_path / "beijing-station.json", stations))],
        [str(_write(tmp_path / "beijing-line.json", lines))],
        {"beijing": "北京"}, source="t", version="v1", tolerance_km=0.05,
    )
    pt = next(f for f in payload["features"] if f["geometry"]["type"] == "Point")
    assert pt["properties"]["line_ids"] == ["北京:A"]


# --------------------------------------------------------------------------- #
# P1-02 启动导入：默认路径按仓库解析；缺文件严格模式报错
# --------------------------------------------------------------------------- #
def test_main_ensure_subway_strict_missing_raises(tmp_path):
    """strict=True 且文件缺失时抛 FileNotFoundError（缺文件明确报错）。"""
    from shikar import main as main_mod
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    storage.init_db(c)
    try:
        with pytest.raises(FileNotFoundError):
            main_mod.ensure_subway(c, str(tmp_path / "nope.json"), strict=True)
    finally:
        c.close()


def test_main_default_paths_are_absolute_repo_paths():
    """默认 db/geojson 路径为绝对路径且指向仓库内（与 cwd 无关，修 P1-02）。"""
    from shikar import main as main_mod
    root = main_mod.repo_root()
    assert os.path.isabs(root)
    assert os.path.isdir(os.path.join(root, "backend"))
    # 默认 GeoJSON 指向仓库 data/subway 下。
    default_geo = main_mod._DEFAULT_SUBWAY_GEOJSON
    assert os.path.isabs(default_geo)
    assert default_geo.replace("\\", "/").endswith("data/subway/shikar-subway.json")


def test_main_missing_subway_file_exits_nonzero(tmp_path):
    """P1-2：无地铁表且地铁 GeoJSON 缺失时，默认启动应报错退出（非静默成功）。"""
    import subprocess
    import sys

    backend = os.path.join(_REPO_ROOT, "backend")
    db = tmp_path / "empty.db"
    missing = tmp_path / "no-such-subway.json"
    proc = subprocess.run(
        [sys.executable, "-m", "shikar.main",
         "--db", str(db), "--subway-geojson", str(missing), "--port", "0"],
        cwd=backend, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert "未找到地铁 GeoJSON" in (proc.stderr + proc.stdout)


def test_main_allow_no_subway_starts_without_geojson(tmp_path):
    """--allow-no-subway 时缺文件不报错退出（允许无地铁状态启动）。"""
    import subprocess
    import sys

    backend = os.path.join(_REPO_ROOT, "backend")
    db = tmp_path / "empty2.db"
    missing = tmp_path / "no-such-subway2.json"
    # 用 --help 之外的方式：加 --allow-no-subway 后 begin serve；为不阻塞，给非法端口立即失败？
    # 改为直接调用 run 的解析部分不可行（会 serve_forever）。故用超时启动后判定其进入了 serve。
    proc = subprocess.Popen(
        [sys.executable, "-m", "shikar.main",
         "--db", str(db), "--subway-geojson", str(missing), "--port", "0", "--allow-no-subway"],
        cwd=backend, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        # 启动成功会打印"服务已启动"；读取一行判断未因缺文件退出。
        import time
        time.sleep(4)
        alive = proc.poll() is None
        assert alive, ("进程提前退出", proc.returncode)
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=10)
        except Exception:
            proc.kill()


def test_cd_backend_finds_default_geojson():
    """在 backend 目录下，默认 GeoJSON 解析到存在的仓库文件（cd backend 启动可命中）。"""
    from shikar import main as main_mod
    # 模拟 cwd=backend：默认路径是绝对路径，不依赖 cwd。
    assert os.path.isfile(main_mod._DEFAULT_SUBWAY_GEOJSON)
    # 相对路径也应被按仓库根解析（run 内逻辑）：此处直接验证目标文件在仓库内存在。
    rel = os.path.join("data", "subway", "shikar-subway.json")
    assert os.path.isfile(os.path.join(main_mod.repo_root(), rel))
