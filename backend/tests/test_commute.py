"""通勤（``shikar.commute`` 与 ``/api/commute``）回归测试。

覆盖 Codex 审核 P1：
- P1-2 方向：以小区为起点，小区→A/B（非对称下 t_a/t_b 指向正确）且旧方向缓存失效；
- P1-4 锁隔离：慢通勤不阻塞 ``/api/health``；
- P1-5 错误分类：参数/上游/超时/预算返回不同状态与部分成功统计；
- P1-6 配额与候选顺序：先过滤有效坐标再取有限候选，报告候选数/截断；
- P1-7 缓存 TTL 与 key 维度（供应商/方向/模式/策略）。

全部使用假的上游函数（monkeypatch），不触网。
"""
from __future__ import annotations

import http.client
import json
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
from typing import Iterator

import pytest

from shikar import catalog, commute
from shikar.api import create_server
from shikar.storage import init_db, upsert_xiaoqu


def _count_sources(url: str) -> int:
    """从 OSRM table URL 里数 sources=RANGE 覆盖的点数。"""
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    spec = (q.get("sources") or ["0"])[0]
    total = 0
    for part in spec.split(";"):
        if "-" in part:
            lo, hi = part.split("-")
            total += int(hi) - int(lo) + 1
        else:
            total += 1
    return total


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    init_db(c)
    return c


def _seed(conn: sqlite3.Connection) -> None:
    rows = [
        ("C1", "北京", "朝阳", "望京", "望京A区", 116.48, 39.99),
        ("C2", "北京", "朝阳", "朝阳门", "朝阳门B区", 116.44, 39.92),
        # 无坐标：必须被候选选取先过滤掉，不能占用名额。
        ("C3", "北京", "海淀", "中关村", "中关村C区", None, None),
    ]
    for xid, city, district, plate, name, lng, lat in rows:
        upsert_xiaoqu(conn, {
            "id": xid, "city": city, "district": district, "plate": plate,
            "name": name, "alias": None, "lng": lng, "lat": lat,
            "build_year": None, "households": None,
        })


# --------------------------------------------------------------------------- #
# P1-2 方向：小区为起点 → A/B
# --------------------------------------------------------------------------- #
def test_direction_community_to_ab(monkeypatch) -> None:
    """OSRM table 请求的 sources 应是小区、destinations 是 A/B；t_a/t_b 指向正确端点。"""
    captured = {}

    def fake_get_json(url, timeout=30):
        captured["url"] = url
        n_src = _count_sources(url)
        # 每行 [到A, 到B]，两行值不同以验证 t_a/t_b 未互换。
        base = [[600.0, 1200.0], [700.0, 1300.0]]
        return {"code": "Ok", "durations": [base[i % 2] for i in range(n_src)]}

    monkeypatch.setattr(commute, "_get_json", fake_get_json)
    conn = _conn()
    a = {"name": "A", "lat": 39.90, "lng": 116.40}
    b = {"name": "B", "lat": 39.95, "lng": 116.50}
    cands = [
        {"id": "C1", "lat": 39.99, "lng": 116.48},
        {"id": "C2", "lat": 39.92, "lng": 116.44},
    ]
    cached, key, stats = commute.commute_for(conn, a, b, cands, "driving")
    # 请求里 sources 应为空/起点集合、destinations 为 A/B（sources 数=2, destinations=2）。
    assert "sources=0;1" in captured["url"]
    assert "destinations=2;3" in captured["url"]
    # t_a 取 dst0（到 A），t_b 取 dst1（到 B）。
    assert cached["C1"] == (600.0, 1200.0)
    assert cached["C2"] == (700.0, 1300.0)


def test_old_direction_cache_invalidated(monkeypatch) -> None:
    """旧方向缓存（v1 key）不应被新实现命中；策略版本变化即失效。"""
    conn = _conn()
    a = {"lat": 39.90, "lng": 116.40}
    b = {"lat": 39.95, "lng": 116.50}
    # 伪装一条旧方向、旧策略版本的缓存行（不存在于当前 key）。
    conn.execute(
        "INSERT INTO commute_cache(key,community_id,t_a,t_b,updated_at) VALUES (?,?,?,?,?)",
        ("v1-old|driving|a=39.9,116.4|b=39.95,116.5", "C1", 1.0, 2.0,
         commute._cache_ts(time.time())),
    )
    conn.commit()

    calls = {"n": 0}

    def fake_get_json(url, timeout=30):
        calls["n"] += 1
        return {"code": "Ok", "durations": [[600.0, 1200.0]]}

    monkeypatch.setattr(commute, "_get_json", fake_get_json)
    cached, key, _ = commute.commute_for(conn, a, b, [{"id": "C1", "lat": 1, "lng": 1}], "driving")
    # 必须真正请求上游（未被旧缓存命中），且结果来自新计算。
    assert calls["n"] == 1
    assert cached["C1"] == (600.0, 1200.0)
    assert commute.COMMUTE_CACHE_VERSION in key


# --------------------------------------------------------------------------- #
# P1-7 缓存 TTL 与 key 维度
# --------------------------------------------------------------------------- #
def test_cache_ttl_expiry(monkeypatch) -> None:
    """过期缓存不命中，重新计算。"""
    conn = _conn()
    a = {"lat": 39.90, "lng": 116.40}
    b = {"lat": 39.95, "lng": 116.50}
    key = commute._cache_key(a, b, "driving")
    old = commute._cache_ts(time.time() - commute.CACHE_TTL_OK - 3600)
    conn.execute(
        "INSERT INTO commute_cache(key,community_id,t_a,t_b,updated_at) VALUES (?,?,?,?,?)",
        (key, "C1", 1.0, 2.0, old),
    )
    conn.commit()
    calls = {"n": 0}

    def fake_get_json(url, timeout=30):
        calls["n"] += 1
        return {"code": "Ok", "durations": [[600.0, 1200.0]]}

    monkeypatch.setattr(commute, "_get_json", fake_get_json)
    cached, _, _ = commute.commute_for(conn, a, b, [{"id": "C1", "lat": 1, "lng": 1}], "driving")
    assert calls["n"] == 1
    assert cached["C1"] == (600.0, 1200.0)


def test_cache_key_dimensions() -> None:
    """key 需含供应商/方向/模式/策略；不同 mode/端点产生不同 key。"""
    a = {"lat": 39.90, "lng": 116.40}
    b = {"lat": 39.95, "lng": 116.50}
    k_drive = commute._cache_key(a, b, "driving")
    k_transit = commute._cache_key(a, b, "transit")
    k_swap = commute._cache_key(b, a, "driving")
    assert k_drive != k_transit  # 供应商/模式不同
    assert k_drive != k_swap     # 方向/端点不同
    assert "osrm" in k_drive and "baidu" in k_transit
    assert commute.COMMUTE_CACHE_VERSION in k_drive


# --------------------------------------------------------------------------- #
# P1-5 错误分类
# --------------------------------------------------------------------------- #
def test_upstream_error_classified(monkeypatch) -> None:
    def fake_urlopen(req, timeout=30):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(commute.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(commute.CommuteError) as ei:
        commute._get_json("http://x")
    assert ei.value.kind == "upstream"


def test_timeout_classified(monkeypatch) -> None:
    def fake_urlopen(req, timeout=30):
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(commute.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(commute.CommuteError) as ei:
        commute._get_json("http://x")
    assert ei.value.kind == "timeout"


def test_budget_error_has_partial(monkeypatch) -> None:
    """驾车批数超预算 → budget 错误并携带已计算部分。"""
    calls = {"n": 0}

    def fake_get_json(url, timeout=30):
        calls["n"] += 1
        # 依据请求中的 sources 数量返回对应行数。
        n_src = _count_sources(url)
        return {"code": "Ok", "durations": [[600.0, 1200.0] for _ in range(n_src)]}

    monkeypatch.setattr(commute, "_get_json", fake_get_json)
    conn = _conn()
    a = {"lat": 1.0, "lng": 1.0}
    b = {"lat": 2.0, "lng": 2.0}
    cands = [{"id": f"C{i}", "lat": 3.0, "lng": 3.0} for i in range(commute.BATCH + 1)]
    with pytest.raises(commute.CommuteError) as ei:
        commute.commute_for(conn, a, b, cands, "driving", max_requests=1)
    assert ei.value.kind == "budget"
    assert ei.value.partial["computed"] == commute.BATCH
    assert ei.value.partial["truncated"] is True


def test_invalid_mode_classified() -> None:
    conn = _conn()
    with pytest.raises(commute.CommuteError) as ei:
        commute.commute_for(conn, {"lat": 1, "lng": 1}, {"lat": 2, "lng": 2}, [], "walk")
    assert ei.value.kind == "params"


# --------------------------------------------------------------------------- #
# P1-3 / P1-6 候选顺序：先过滤无坐标，再取有限候选
# --------------------------------------------------------------------------- #
def test_candidates_filter_coords_before_limit() -> None:
    """无坐标小区不占候选名额；有效候选按空间规则排序并按上限截断。"""
    conn = _conn()
    _seed(conn)
    a = {"name": "A", "lat": 39.93, "lng": 116.45}
    b = {"name": "B", "lat": 39.93, "lng": 116.45}
    picked = catalog.select_commute_candidates(conn, a, b, city="北京")
    ids = [it["id"] for it in picked["items"]]
    assert "C3" not in ids  # 无坐标被排除
    assert set(ids) == {"C1", "C2"}
    assert picked["with_coords"] == 2
    assert picked["truncated"] is False
    # 上限截断
    picked1 = catalog.select_commute_candidates(conn, a, b, city="北京", max_candidates=1)
    assert len(picked1["items"]) == 1
    assert picked1["truncated"] is True
    assert picked1["with_coords"] == 2


# --------------------------------------------------------------------------- #
# API 层：错误映射与锁隔离
# --------------------------------------------------------------------------- #
@pytest.fixture()
def live_server(monkeypatch, tmp_path) -> Iterator[dict]:
    # 用文件库：create_server 才能为 /api/commute 派生请求级独立连接（与生产一致）。
    db_path = str(tmp_path / "live.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    init_db(conn)
    _seed(conn)
    server = create_server(conn, host="127.0.0.1", port=0)
    host, port = server.server_address[0], server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"host": host, "port": port, "conn": conn}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        conn.close()


def _get(server: dict, path: str) -> tuple[int, object]:
    # 对路径里的非 ASCII（如中文城市名）做百分号编码。
    path = urllib.parse.quote(path, safe="/?=&;")
    client = http.client.HTTPConnection(server["host"], server["port"], timeout=5)
    try:
        client.request("GET", path)
        resp = client.getresponse()
        return resp.status, json.loads(resp.read().decode("utf-8"))
    finally:
        client.close()


def _fake_places(monkeypatch):
    monkeypatch.setattr(commute, "resolve_place", lambda conn, text, city=None: {
        "name": text, "lat": 39.93, "lng": 116.45,
    })


def test_api_commute_budget_returns_429_with_partial(monkeypatch, live_server) -> None:
    _fake_places(monkeypatch)

    def fake_commute_for(conn, a, b, cand, mode, **kw):
        exc = commute.CommuteError("budget", "超预算", partial={
            "total": len(cand), "computed": 1, "cached": 0, "failed": 0, "truncated": True,
        })
        exc._cached = {cand[0]["id"]: (600.0, 1200.0)} if cand else {}
        raise exc

    monkeypatch.setattr(commute, "commute_for", fake_commute_for)
    status, body = _get(live_server, "/api/commute?fromA=A&fromB=B&mode=driving&city=北京")
    assert status == 429
    assert body["error_type"] == "budget"
    assert body["stats"]["truncated"] is True
    assert "items" in body  # 返回部分成功


def test_api_commute_upstream_returns_502(monkeypatch, live_server) -> None:
    def fake_resolve(conn, text, city=None):
        raise commute.CommuteError("upstream", "百度挂了")

    monkeypatch.setattr(commute, "resolve_place", fake_resolve)
    status, body = _get(live_server, "/api/commute?fromA=A&fromB=B")
    assert status == 502
    assert body["error_type"] == "upstream"


def test_api_commute_reports_candidate_counts(monkeypatch, live_server) -> None:
    _fake_places(monkeypatch)
    monkeypatch.setattr(commute, "commute_for", lambda conn, a, b, cand, mode, **kw: (
        {c["id"]: (600.0, 1200.0) for c in cand},
        "k",
        {"total": len(cand), "computed": len(cand), "cached": 0, "failed": 0, "truncated": False},
    ))
    status, body = _get(live_server, "/api/commute?fromA=A&fromB=B&mode=driving&city=北京")
    assert status == 200
    assert body["with_coords"] == 2
    assert body["candidates"] == 2
    assert body["truncated"] is False


def test_slow_commute_does_not_block_health(monkeypatch, live_server) -> None:
    """P1-4：慢通勤不应阻塞 /api/health（锁隔离）。"""
    _fake_places(monkeypatch)

    def slow_commute_for(conn, a, b, cand, mode, **kw):
        time.sleep(1.2)
        return {c["id"]: (600.0, 1200.0) for c in cand}, "k", {
            "total": len(cand), "computed": len(cand), "cached": 0, "failed": 0, "truncated": False,
        }

    monkeypatch.setattr(commute, "commute_for", slow_commute_for)

    results = {}

    def call_commute():
        results["commute"] = _get(live_server, "/api/commute?fromA=A&fromB=B&mode=driving&city=北京")

    t = threading.Thread(target=call_commute)
    t.start()
    time.sleep(0.2)  # 让慢通勤先进入处理
    t0 = time.time()
    status, _ = _get(live_server, "/api/health")
    elapsed = time.time() - t0
    t.join(timeout=5)
    assert status == 200
    # health 必须在慢通勤（~1.2s）结束前快速返回。
    assert elapsed < 0.6, f"health 被阻塞 {elapsed:.2f}s"
    assert results["commute"][0] == 200


def _brute_suggestion(text, region=None):
    """同名不同文本也返回同一 BD09 点，避免测试依赖百度。"""
    return [{"name": text, "lng": 116.40, "lat": 39.90}]


def test_concurrent_commute_distinct_coords_no_500(monkeypatch, live_server) -> None:
    """P1-1 回归：并发不同坐标对的驾车通勤不得因共用连接交叉执行而 500。"""
    monkeypatch.setattr(commute.baidu, "suggestion", _brute_suggestion)

    def slow_json(url, timeout=30):
        time.sleep(0.25)  # 拉长窗口，让并发请求必然重叠
        return {"code": "Ok", "durations": [[600.0, 1200.0] for _ in range(_count_sources(url))]}

    monkeypatch.setattr(commute, "_get_json", slow_json)

    results: dict[int, tuple] = {}

    def call(i: int) -> None:
        results[i] = _get(
            live_server,
            f"/api/commute?fromA=A{i}&fromB=B{i}&mode=driving&city=北京",
        )

    threads = [threading.Thread(target=call, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    codes = sorted(code for code, _ in results.values())
    errors = [body.get("error") for code, body in results.values() if code != 200]
    assert codes == [200] * 6, f"并发返回码 {codes}，错误 {errors}"
    assert live_server["conn"].execute("SELECT COUNT(*) FROM commute_cache").fetchone()[0] > 0


def test_concurrent_transit_commute_no_500(monkeypatch, live_server) -> None:
    """P1-1 回归：并发公交通勤同样不得出现共享连接交叉（曾报 tuple index out of range）。"""
    monkeypatch.setattr(commute.baidu, "suggestion", _brute_suggestion)

    def slow_transit(src, dst):
        time.sleep(0.05)
        return {"duration_s": 600.0}

    monkeypatch.setattr(commute.baidu, "transit", slow_transit)

    results: dict[int, tuple] = {}

    def call(i: int) -> None:
        results[i] = _get(
            live_server,
            f"/api/commute?fromA=A{i}&fromB=B{i}&mode=transit&city=北京",
        )

    threads = [threading.Thread(target=call, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    codes = sorted(code for code, _ in results.values())
    errors = [body.get("error") for code, body in results.values() if code != 200]
    assert codes == [200] * 6, f"并发返回码 {codes}，错误 {errors}"
