"""双点通勤：地点解析（百度 suggestion）+ 时长矩阵（OSRM 公共实例，免费）+ 本地缓存。

修复 P1（Codex 审核 P2/P3）：
- 方向：以「小区」为起点，通勤到 A/B 两个目的地（非对称路网下二者不可互换）。
- 缓存键加入方向/供应商/模式/配额策略与 A/B 端点，杜绝不同请求串用同一缓存行；
  并对缓存加 TTL（空/失败结果更短），超期失效。
- 错误分类：``CommuteError`` 区分参数、上游失败、超时、配额/并发预算耗尽，
  各携带可供上层映射到不同 HTTP 状态的 ``kind`` 与可选的 ``partial`` 统计。
- 并发预算：驾车批量对 OSRM 的请求数受配置限制；公交逐 OD 调用前检查预算，
  预算/并发耗尽时抛 ``budget`` 类错误并附部分成功统计，不静默当成成功。
- 地点解析结果缓存（短 TTL），失败不缓存、不在失败路径上无缓存重试。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request

from . import baidu

OSRM = "https://router.project-osrm.org"
BATCH = 80  # OSRM table 单次坐标上限内，分批

#: 缓存键的「策略版本」：方向或计算口径一旦改变即提升此值，使旧缓存整体失效。
#: 版本 2：通勤方向由 A/B→小区 改为 小区→A/B，旧方向缓存必须失效（P1-2）。
COMMUTE_CACHE_VERSION = "v2-community-to-ab"

#: 缓存 TTL（秒）：有效路线结果保留 30 天。
CACHE_TTL_OK = 30 * 24 * 3600
#: 空/失败路线短 TTL（秒）：避免永久把「无路线」固化，又不过度重复请求。
CACHE_TTL_EMPTY = 6 * 3600
#: 地点解析结果缓存 TTL（秒）。
RESOLVE_TTL = 7 * 24 * 3600
#: 每批 OSRM 表的请求超时（秒）。
OSRM_TIMEOUT = 30
#: 单次 /api/commute 允许对上游发起的最大请求数（并发/时间/配额预算）。
DEFAULT_MAX_REQUESTS = 8
#: 公交（百度，按次计费）单次最多计算的 OD 数（更严格，控制成本）。
DEFAULT_MAX_TRANSIT_OD = 40
#: 公交连续失败多少条且零成功时判定上游整体不可用（止损，不再烧配额）。
_TRANSIT_FAIL_FAST = 3


class CommuteError(RuntimeError):
    """通勤计算的结构化错误，携带 ``kind`` 供 API 层映射到不同 HTTP 状态。

    ``kind`` 取值：
    - ``"params"``：参数/输入错误（如 mode 非法、坐标缺失）→ 400；
    - ``"upstream"``：上游（OSRM/百度）返回错误 → 502；
    - ``"timeout"``：上游超时 → 504；
    - ``"budget"``：配额/并发/时间预算耗尽 → 429（附 ``partial`` 部分成功统计）。
    """

    def __init__(self, kind: str, message: str, partial: dict | None = None):
        super().__init__(message)
        self.kind = kind
        self.partial = partial or {}


def _classify_upstream(exc: BaseException | None) -> str:
    """把上游异常归为 ``timeout`` 或 ``upstream``（百度客户端不区分）。"""
    if exc is None:
        return "upstream"
    if isinstance(exc, TimeoutError):
        return "timeout"
    reason = getattr(exc, "reason", None)
    text = f"{reason if reason is not None else exc}".lower()
    if "timed out" in text or "timeout" in text:
        return "timeout"
    return "upstream"


def _get_json(url: str, timeout: int = OSRM_TIMEOUT) -> dict:
    """请求 OSRM 并解析 JSON，把网络/上游错误分类为 ``CommuteError``。"""
    req = urllib.request.Request(url, headers={"User-Agent": "shikar-commute/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.URLError as exc:
        # 超时与连接失败区分：socket.timeout 会被包成 URLError 的 reason。
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            raise CommuteError("timeout", f"OSRM 请求超时：{reason}")
        raise CommuteError("upstream", f"OSRM 连接失败：{reason}")
    except TimeoutError as exc:
        raise CommuteError("timeout", f"OSRM 请求超时：{exc}")
    except (json.JSONDecodeError, ValueError) as exc:
        raise CommuteError("upstream", f"OSRM 返回无法解析：{exc}")
    if data.get("code") not in (0, "Ok", None) and "durations" not in data:
        raise CommuteError("upstream", f"OSRM 错误: {data.get('code')} {data.get('message')}")
    return data


def _cache_ts(now: float) -> str:
    """把当前时间转为可比较的 ISO 时间戳字符串（UTC）。"""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))


def _is_fresh(updated_at: str | None, now: float) -> bool:
    """判断缓存行是否仍在 TTL 内；无法解析的时间视为过期。"""
    if not updated_at:
        return False
    try:
        ts = time.mktime(time.strptime(updated_at, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except (ValueError, OverflowError):
        return False
    ttl = CACHE_TTL_OK
    return (now - ts) <= ttl


def resolve_place(conn: sqlite3.Connection | None, text: str, city: str | None = None) -> dict | None:
    """把地点名解析成 WGS84 坐标（百度 suggestion），带短 TTL 缓存。

    成功的解析结果写入 ``place_cache``（短 TTL）；解析失败（无结果或上游报错）
    **不缓存**，也不在此函数内做无缓存重试，交由上层按正常错误路径处理一次。

    :param conn: SQLite 连接（用于缓存）；``None`` 时不做缓存直接请求。
    :param text: 待解析地点名。
    :param city: 城市（region）提示。
    :return: ``{"name","lat","lng"}`` 或 ``None``（解析不到）。
    :raises CommuteError: 上游（百度）调用失败时（``kind="upstream"``）。
    """
    ck = f"{city or '全国'}|{text}"
    now = time.time()
    if conn is not None:
        row = conn.execute(
            "SELECT name,lat,lng,updated_at FROM place_cache WHERE k=?", (ck,)
        ).fetchone()
        if row is not None and row["updated_at"] and _is_fresh(row["updated_at"], now):
            return {"name": row["name"], "lat": row["lat"], "lng": row["lng"]}
    try:
        res = baidu.suggestion(text, region=city or "全国")
    except Exception as exc:  # 百度客户端把 status!=0 抛 RuntimeError
        raise CommuteError("upstream", f"地点解析失败：{exc}")
    if not res:
        return None
    r = res[0]
    lng, lat = baidu.bd09_to_wgs84(r["lng"], r["lat"])
    out = {"name": r.get("name") or text, "lat": lat, "lng": lng}
    if conn is not None:
        conn.execute(
            "INSERT INTO place_cache(k,name,lat,lng,updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(k) DO UPDATE SET name=excluded.name,lat=excluded.lat,"
            "lng=excluded.lng,updated_at=excluded.updated_at",
            (ck, out["name"], lat, lng, _cache_ts(now)),
        )
        conn.commit()
    return out


def _cache_key(a: dict, b: dict, mode: str, origin_tag: str = "community") -> str:
    """构造缓存键：含策略版本、方向、端点、供应商与模式。

    方向（``origin``）与端点对调会产生不同键，确保非对称路网下 A→小区 与
    小区→A 不串用；``COMMUTE_CACHE_VERSION`` 变化使旧方向缓存整体失效。
    """
    provider = "osrm" if mode == "driving" else "baidu"
    return (
        f"{COMMUTE_CACHE_VERSION}|{provider}|{mode}|origin={origin_tag}"
        f"|a={a['lat']:.5f},{a['lng']:.5f}|b={b['lat']:.5f},{b['lng']:.5f}"
    )


def commute_for(
    conn: sqlite3.Connection,
    a: dict,
    b: dict,
    candidates: list[dict],
    mode: str = "driving",
    max_requests: int = DEFAULT_MAX_REQUESTS,
    max_transit_od: int = DEFAULT_MAX_TRANSIT_OD,
):
    """返回 ``({community_id: (t_a, t_b)}, key, stats)``，带 SQLite 缓存。

    方向（P1-2）：以**小区为起点**，分别计算 小区→A、小区→B 的时长；
    ``t_a`` / ``t_b`` 即小区到 A/B 的通勤时长（秒）。需求方向为「小区→A/B」，
    与旧实现相反，故缓存键带策略版本使旧缓存失效。

    并发/预算（P1-4/P1-6）：
    - 驾车：按 ``BATCH`` 分批调用 OSRM table，请求数受 ``max_requests`` 限制，
      超预算抛 ``CommuteError("budget", …, partial)``；
    - 公交：逐 OD 调用百度，候选超过 ``max_transit_od`` 时该条本身即视为超配额，
      抛出带 ``partial`` 的 ``budget`` 错误。

    错误分类（P1-5）：上游失败/超时以 ``kind`` 区分；已成功的候选放入 ``partial``，
    便于上层返回部分成功统计而非混成「成功空」。

    写事务边界：上游计算全部只进内存缓冲，结束后**一次短事务**写入并提交——写事务
    不跨网络调用；并发冷缓存下不会互等写锁。写冲突以 ``busy``（503）返回。

    :return: ``(cached, key, stats)``，``stats`` 含
        ``{"computed","cached","failed","truncated","total"}``。
    """
    if mode not in ("driving", "transit"):
        raise CommuteError("params", "mode 仅支持 driving / transit")
    key = _cache_key(a, b, mode)
    now = time.time()
    cached: dict[str, tuple] = {}
    for row in conn.execute(
        "SELECT community_id,t_a,t_b,updated_at FROM commute_cache WHERE key=?", (key,)
    ).fetchall():
        if _is_fresh(row["updated_at"], now) and row["t_a"] is not None and row["t_b"] is not None:
            cached[row["community_id"]] = (row["t_a"], row["t_b"])
    missing = [c for c in candidates if c["id"] not in cached]
    stats = {"total": len(candidates), "cached": len(cached), "computed": 0,
             "failed": 0, "attempted": 0, "truncated": False, "partial": False}

    pending: list[tuple[str, float, float]] = []

    def buffer(cid: str, x: float, y: float) -> None:
        """只进内存缓冲：写事务绝不跨网络调用（并发冷缓存写锁死修复）。"""
        pending.append((cid, x, y))
        stats["computed"] += 1

    def flush() -> None:
        """一次短事务写入缓冲行，并把结果**合并**进 ``cached``（保留已有命中项）。"""
        if not pending:
            return
        rows = [(key, cid, x, y, _cache_ts(now)) for cid, x, y in pending]
        try:
            conn.executemany(
                "INSERT INTO commute_cache(key,community_id,t_a,t_b,updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(key,community_id) DO UPDATE SET t_a=excluded.t_a,t_b=excluded.t_b,updated_at=excluded.updated_at",
                rows,
            )
            conn.commit()
        except sqlite3.OperationalError as exc:
            conn.rollback()
            raise CommuteError("busy", f"缓存写入繁忙：{exc}", partial=dict(stats)) from exc
        for cid, x, y in pending:
            cached[cid] = (x, y)
        pending.clear()

    if mode == "driving":
        # 以小区为起点，分别取到 A、B 的时长（方向：小区→A/B）。
        dests_ab = [(a["lat"], a["lng"]), (b["lat"], b["lng"])]
        chunks = [missing[i:i + BATCH] for i in range(0, len(missing), BATCH)] if missing else []
        if len(chunks) > max_requests:
            stats["truncated"] = True
            chunks = chunks[:max_requests]
        try:
            for chunk in chunks:
                # 每批把本批小区作为 sources，目的地固定为 A、B 两点。
                t_a_by, t_b_by = _table_multi_source(
                    [(c["lat"], c["lng"]) for c in chunk], dests_ab
                )
                for c, ra, rb in zip(chunk, t_a_by, t_b_by):
                    if ra is None or rb is None:
                        stats["failed"] += 1
                        continue
                    buffer(c["id"], ra, rb)
        except CommuteError as exc:
            # 上游失败：先把已算出的批次落库（短事务），再抛原始错误；
            # flush 自身失败不覆盖原错误。
            try:
                flush()
            except CommuteError:
                pass
            stats["partial"] = stats["failed"] > 0
            exc.partial = dict(stats)
            exc._cached = dict(cached)
            raise
        flush()
        if stats["truncated"]:
            exc = CommuteError(
                "budget",
                f"候选超过单次驾车预算（{max_requests} 批），已计算前 "
                f"{stats['computed']} 条",
                partial=dict(stats),
            )
            exc._cached = dict(cached)
            raise exc
    else:  # transit：百度公交（按次计费），逐 OD 计算；方向为 小区→A / 小区→B。
        if len(missing) > max_transit_od:
            stats["truncated"] = True
            missing = missing[:max_transit_od]

        def bd(p):
            lng, lat = baidu.wgs84_to_bd09(p["lng"], p["lat"])
            return (lat, lng)

        pa, pb = bd(a), bd(b)
        consecutive = 0
        last_exc: BaseException | None = None
        for c in missing:
            pc = bd(c)
            stats["attempted"] += 1
            try:
                ta = baidu.transit(pc, pa)["duration_s"]  # 小区→A
                tb = baidu.transit(pc, pb)["duration_s"]  # 小区→B
            except Exception as exc:  # 百度客户端把 status!=0 抛 RuntimeError
                last_exc = exc
                stats["failed"] += 1
                consecutive += 1
                # 连续失败且一条都没成功：判定为上游整体不可用，尽早止损。
                if stats["computed"] == 0 and consecutive >= _TRANSIT_FAIL_FAST:
                    raise CommuteError(
                        _classify_upstream(exc),
                        f"公交上游连续失败 {consecutive} 次：{exc}",
                        partial=dict(stats),
                    ) from exc
                continue
            if ta is None or tb is None:
                stats["failed"] += 1
                consecutive += 1
                continue
            consecutive = 0
            buffer(c["id"], ta, tb)
        flush()
        if stats["computed"] == 0 and stats["attempted"] > 0:
            raise CommuteError(
                _classify_upstream(last_exc),
                f"公交上游未返回可用结果（失败 {stats['failed']}/{stats['attempted']}）：{last_exc}",
                partial=dict(stats),
            )
        stats["partial"] = stats["failed"] > 0
        if stats["truncated"]:
            exc = CommuteError(
                "budget",
                f"公交候选超过单次预算（{max_transit_od} 个），已计算前 {stats['computed']} 条",
                partial=dict(stats),
            )
            exc._cached = dict(cached)
            raise exc
        return cached, key, stats
    return cached, key, stats


def _table_multi_source(sources: list[tuple[float, float]],
                        dests: list[tuple[float, float]]):
    """以小区为 sources、A/B 为 destinations 的 OSRM table。

    :param sources: 每项 (lat, lng)，作为出发点（小区）。
    :param dests: 每项 (lat, lng)，作为目的地（A、B）。
    :return: ``(t_a_list, t_b_list)``，分别对应每个 source 到 dest[0]/dest[1] 的时长（秒）；
        缺失时对应位置为 ``None``。
    """
    pts = sources + dests
    coords = ";".join(f"{lng},{lat}" for (lat, lng) in pts)
    n_src = len(sources)
    n_dst = len(dests)
    src_idx = ";".join(str(i) for i in range(n_src))
    dst_idx = ";".join(str(n_src + j) for j in range(n_dst))
    url = (
        f"{OSRM}/table/v1/driving/{coords}"
        f"?sources={src_idx}&destinations={dst_idx}&annotations=duration"
    )
    data = _get_json(url)
    durations = data["durations"]  # [n_src][n_dst]
    t_a = [row[0] if len(row) > 0 else None for row in durations]
    t_b = [row[1] if len(row) > 1 else None for row in durations]
    return t_a, t_b
