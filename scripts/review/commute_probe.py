"""通勤链路评审探针（只读、不触网、不写 data/）。

用途：复现 `docs/16-代码审核-PR32-三段式样板.md` 中的 P1-1 / P1-2 / P1-3 与
P2-1 / P2-2。每个场景独立建内存库，全部上游调用都是假的。

用法（默认探测当前仓库，可指向任意检出）：

    python scripts/review/commute_probe.py
    python scripts/review/commute_probe.py --repo ../shikar-review-32

输出每个场景的「期望 / 实际」，末尾给出汇总。退出码恒为 0（探针只报事实，
是否阻断由审核结论决定）。
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.parse

_DEFAULT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _parse_repo() -> str:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--repo", default=_DEFAULT_ROOT)
    ns, _ = ap.parse_known_args()
    return os.path.abspath(ns.repo)


ROOT = _parse_repo()
sys.path.insert(0, os.path.join(ROOT, "backend"))

from shikar import catalog, commute  # noqa: E402
from shikar.api import create_server  # noqa: E402
from shikar.storage import init_db, upsert_xiaoqu  # noqa: E402


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    init_db(c)
    return c


def _file_conn() -> tuple[sqlite3.Connection, str]:
    """文件库：/api/commute 只有在文件库上才会启用请求级独立连接（与生产一致）。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    c = sqlite3.connect(path, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    init_db(c)
    return c, path


def _seed(c: sqlite3.Connection, rows: list[tuple[str, str, float, float]]) -> None:
    for xid, name, lng, lat in rows:
        upsert_xiaoqu(c, {
            "id": xid, "city": "北京", "district": "朝阳", "plate": "P",
            "name": name, "alias": None, "lng": lng, "lat": lat,
            "build_year": None, "households": None,
        })


def _serve(c: sqlite3.Connection):
    srv = create_server(c, host="127.0.0.1", port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[0], srv.server_address[1]


def _get(host: str, port: int, path: str) -> tuple[int, dict]:
    cl = http.client.HTTPConnection(host, port, timeout=30)
    try:
        cl.request("GET", urllib.parse.quote(path, safe="/?=&;"))
        r = cl.getresponse()
        raw = r.read().decode("utf-8")
        try:
            return r.status, json.loads(raw)
        except ValueError:
            return r.status, {"raw": raw[:200]}
    finally:
        cl.close()


def _n_sources(url: str) -> int:
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    return len([p for p in (q.get("sources") or ["0"])[0].split(";") if p])


def _fake_place(conn, text, city=None):
    return {"name": text, "lat": 39.93, "lng": 116.45}


def sc_a_concurrency() -> None:
    """A. 6 个并发 /api/commute 是否全部 200（P1-1）。"""
    c, db_path = _file_conn()
    _seed(c, [(f"C{i}", f"小区{i}", 116.4 + i * 1e-3, 39.9 + i * 1e-3) for i in range(5)])
    commute.resolve_place = _fake_place

    def slow_json(url, timeout=30):
        time.sleep(0.25)
        return {"code": "Ok", "durations": [[600.0, 1200.0] for _ in range(_n_sources(url))]}

    commute._get_json = slow_json
    srv, host, port = _serve(c)
    res: dict[int, tuple] = {}

    def one(i):
        try:
            res[i] = _get(host, port, f"/api/commute?fromA=A{i}&fromB=B{i}&mode=driving&city=北京")
        except Exception as exc:  # noqa: BLE001
            res[i] = ("EXC", str(exc))

    threads = [threading.Thread(target=one, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    srv.shutdown()
    srv.server_close()
    c.close()
    os.unlink(db_path)
    codes = [v[0] for v in res.values()]
    errs = {v[1].get("error") for v in res.values() if isinstance(v[1], dict) and v[1].get("error")}
    verdict = "PASS" if codes.count(200) == 6 else "FAIL"
    print(f"[A] 并发 6 个 /api/commute（期望 6×200）→ {codes.count(200)}×200 / "
          f"{codes.count(500)}×500  {verdict}")
    if errs:
        print(f"    500 样本: {sorted(e for e in errs if e)[0][:80]}")


def sc_b_transit_outage() -> None:
    """B. 公交上游全挂时应给出非 200 或明确降级（P1-2）。"""
    c = _conn()
    _seed(c, [("T1", "甲", 116.40, 39.90), ("T2", "乙", 116.41, 39.91)])
    commute.resolve_place = _fake_place

    def boom(*_a, **_k):
        raise RuntimeError("百度挂了")

    commute.baidu.transit = boom
    srv, host, port = _serve(c)
    status, body = _get(host, port, "/api/commute?fromA=A&fromB=B&mode=transit&city=北京")
    srv.shutdown()
    srv.server_close()
    verdict = "PASS" if status >= 500 else "FAIL"
    print(f"[B] transit 上游全挂（期望 502/504）→ status={status} items={len(body.get('items', []))} "
          f"stats={body.get('stats')} error={body.get('error')}  {verdict}")


def sc_c_candidates() -> None:
    """C. 空间排序是否作用于全量命中（P1-3）。"""
    c = _conn()
    rows = [(f"A{i:03d}", f"A{i:03d}", 120.0 + i * 0.01, 45.0) for i in range(200)]
    rows += [(f"Z{i:03d}", f"Z{i:03d}", 116.45 + i * 1e-4, 39.93 + i * 1e-4) for i in range(50)]
    _seed(c, rows)
    a = {"name": "A", "lat": 39.93, "lng": 116.45}
    picked = catalog.select_commute_candidates(c, a, dict(a), city="北京", max_candidates=10)
    ids = [it["id"] for it in picked["items"]]
    verdict = "PASS" if any(i.startswith("Z") for i in ids) else "FAIL"
    print(f"[C] 250 命中、最近的 50 个排在名称序后（期望含 Z*）→ total={picked['total_matched']} "
          f"with_coords={picked['with_coords']} 前3={ids[:3]}  {verdict}")


def sc_d_place_ttl() -> None:
    """D. place_cache 是否按声明的 7 天过期（P2-2）。"""
    c = _conn()
    hits = {"n": 0}

    def suggestion(text, region=None):
        hits["n"] += 1
        return [{"name": text, "lng": 116.4, "lat": 39.9}]

    commute.baidu.suggestion = suggestion
    commute.baidu.bd09_to_wgs84 = lambda lng, lat: (lng, lat)
    c.execute(
        "INSERT INTO place_cache(k,name,lat,lng,updated_at) VALUES (?,?,?,?,?)",
        ("全国|老地点", "老地点", 39.9, 116.4, commute._cache_ts(time.time() - 8 * 86400)),
    )
    c.commit()
    out = commute.resolve_place(c, "老地点")
    verdict = "PASS" if hits["n"] == 1 else "FAIL"
    print(f"[D] 8 天前的地点缓存（期望过期重取，声明 TTL 7 天）→ 上游调用 {hits['n']} 次 "
          f"命中={out is not None}  {verdict}")


def sc_e_budget_reachable() -> None:
    """E. HTTP 层能否触达驾车/公交预算（P2-1）。"""
    from shikar import api

    driving_budget = commute.DEFAULT_MAX_REQUESTS * commute.BATCH
    driving_room = api._COMMUTE_MAX_DRIVING > driving_budget
    transit_room = api._COMMUTE_MAX_TRANSIT > commute.DEFAULT_MAX_TRANSIT_OD
    verdict = "PASS" if driving_room and transit_room else "FAIL"
    print(f"[E] 预算可达性（期望候选上限 > 预算）→ 驾车 {api._COMMUTE_MAX_DRIVING} vs "
          f"{driving_budget}；公交 {api._COMMUTE_MAX_TRANSIT} vs "
          f"{commute.DEFAULT_MAX_TRANSIT_OD}  {verdict}")


def main() -> None:
    print(f"repo: {ROOT}\n")
    for fn in (sc_a_concurrency, sc_b_transit_outage, sc_c_candidates,
               sc_d_place_ttl, sc_e_budget_reachable):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            print(f"[{fn.__name__}] EXCEPTION: {exc!r}")
        print()


if __name__ == "__main__":
    main()
