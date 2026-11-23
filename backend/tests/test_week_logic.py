"""脚本层周次口径回归测试（不联网、不读写 data/）。

- ``scripts/build_db.py:_current_week``：当期周由 2026 CSV 行数据推导，取出现次数最多的
  week（并列取字典序最大）；CSV 为空返回 None（不再硬编码具体周次）。
- ``scripts/fetch_fang.py:_iso_week``：用 ISO 8601 的 year+week 组合，跨年周不得出现
  "2021-W53" 这类自相矛盾的周次。
"""

from __future__ import annotations

import datetime
import importlib.util
import os
import sqlite3

from shikar import overview, pipeline

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_script(name):
    """按文件路径加载 ``scripts/<name>.py``（两个脚本模块级均无副作用）。"""
    path = os.path.join(_REPO_ROOT, "scripts", f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"_script_{name}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


build_db = _load_script("build_db")
fetch_fang = _load_script("fetch_fang")


# —— 缺陷 1：当期周不得硬编码 ——

def test_current_week_follows_data_w46():
    rows = [{"week": "2026-W45"}, {"week": "2026-W46"}, {"week": "2026-W46"}]
    assert build_db._current_week(rows) == "2026-W46"


def test_current_week_follows_data_w47():
    rows = [{"week": "2026-W46"}, {"week": "2026-W47"}, {"week": "2026-W47"}]
    assert build_db._current_week(rows) == "2026-W47"


# —— 缺陷 3：抓取去重键缺 week，导致周更数据从不累积 ——

def test_merge_rows_same_week_is_idempotent():
    """同一小区同一周跑两次只保留一行。"""
    rows = {}
    items = [{"xiaoqu_id": "X1", "xiaoqu_name": "小区一", "price": "50000"}]
    assert fetch_fang.merge_rows(rows, items, "北京", "2026-W46") == 1
    assert fetch_fang.merge_rows(rows, items, "北京", "2026-W46") == 0
    assert len(rows) == 1


def test_merge_rows_different_week_appends():
    """同一小区不同周跑两次 → 两行（周更数据真正累积）。"""
    rows = {}
    items = [{"xiaoqu_id": "X1", "xiaoqu_name": "小区一", "price": "50000"}]
    assert fetch_fang.merge_rows(rows, items, "北京", "2026-W46") == 1
    assert fetch_fang.merge_rows(rows, items, "北京", "2026-W47") == 1
    assert len(rows) == 2
    assert {k[2] for k in rows} == {"2026-W46", "2026-W47"}


def test_load_existing_key_includes_week(tmp_path, monkeypatch):
    """从 CSV 读入时键含 week，同小区两个周次不会被折叠成一行。"""
    out = tmp_path / "fang.csv"
    header = ",".join(fetch_fang.FIELDS)
    out.write_text(
        header + "\n"
        + "北京,朝阳,望京,X1,小区一,2026-W46,50000,10,2005,,fang.com\n"
        + "北京,朝阳,望京,X1,小区一,2026-W47,50500,10,2005,,fang.com\n",
        encoding="utf-8-sig",
    )
    monkeypatch.setattr(fetch_fang, "OUT", str(out))
    rows = fetch_fang.load_existing()
    assert len(rows) == 2
    assert {k[2] for k in rows} == {"2026-W46", "2026-W47"}


def test_build_db_has_no_hardcoded_current_week():
    """回归护栏：当期周必须来自数据，源码里不得再出现具体周次字面量。"""
    with open(build_db.__file__, "r", encoding="utf-8") as f:
        src = f.read()
    assert "2026-W46" not in src, "build_db.py 又写死了当期周"
    assert "current_week" in src, "build_db.py 未使用由数据推导的当期周"


def test_dedupe_2026_keeps_multiple_weeks():
    """去重键必须含 week：同小区 4 个周次不能被折叠成 1 行。"""
    rows = [
        {"city": "北京", "xiaoqu_id": "X1", "week": w}
        for w in ("2025-W05", "2025-W22", "2025-W40", "2026-W47")
    ]
    assert len(build_db.dedupe_2026(rows)) == 4
    # 完全重复的行才丢
    assert len(build_db.dedupe_2026(rows + rows[:1])) == 4


def test_build_db_end_to_end_multi_week_csv(tmp_path, monkeypatch):
    """端到端：多周 CSV 全部入库；派生指标交给 recompute 统一计算。

    这是 testWorker 复验发现的缺口：旧去重键 (city, xiaoqu_id) 把 12 行截成 3 行
    （只剩首周），当期周没有温度、overview 必空。
    另：build_db 只写**原始数据**，``mom4`` / ``temperature`` 一律交给
    ``scripts/recompute_metrics.py``（``shikar.pipeline.recompute``），
    避免「build_db 写一版、recompute 再覆盖一版」两套口径打架。
    """
    data = tmp_path / "data"
    data.mkdir()
    header = "city,district,plate,xiaoqu_id,xiaoqu_name,week,price,supply,built_year,mom1_pct,source"
    (data / "history2019.csv").write_text(header + "\n", encoding="utf-8-sig")

    # 15 期、每期相隔 28 天：跨度 392 天过门槛，相邻缺口 28 天在 mom4 的 28±7 窗口内。
    weeks = []
    d = datetime.date(2025, 9, 1)
    for _ in range(15):
        y, w, _ = d.isocalendar()
        weeks.append(f"{y}-W{w:02d}")
        d += datetime.timedelta(days=28)

    lines = [header]
    for i, name in enumerate(["A小区", "B小区", "C小区"]):
        for j, w in enumerate(weeks):
            price = 50000 + i * 1000 + (j % 3) * 500
            lines.append(
                f"北京,朝阳,望京,X{i},{name},{w},{price},10,2005,1.0,fang.com"
            )
    (data / "fang_2026.csv").write_text("\n".join(lines) + "\n", encoding="utf-8-sig")

    monkeypatch.setattr(build_db, "DATA", str(data))
    build_db.main()

    conn = sqlite3.connect(str(data / "shikar.db"))
    conn.row_factory = sqlite3.Row
    try:
        n = conn.execute("SELECT COUNT(*) FROM xiaoqu_metric_weekly").fetchone()[0]
        assert n == 45, f"多周行被丢弃，只剩 {n} 行"
        # build_db 只写原始数据：派生列必须留空（口径只有一份实现）。
        assert conn.execute(
            "SELECT COUNT(*) FROM xiaoqu_metric_weekly WHERE temperature IS NOT NULL"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM xiaoqu_metric_weekly WHERE mom4 IS NOT NULL"
        ).fetchone()[0] == 0

        # 生产顺序：build_db → recompute_metrics（refresh.sh 同流程）。
        pipeline.recompute(conn)
        rows = conn.execute(
            "SELECT temperature FROM xiaoqu_metric_weekly WHERE week = ?", (weeks[-1],)
        ).fetchall()
        assert len(rows) == 3
        assert all(r["temperature"] is not None for r in rows), "recompute 后当期周温度仍为空"
    finally:
        conn.close()

    ov_conn = sqlite3.connect(str(data / "shikar.db"))
    ov_conn.row_factory = sqlite3.Row
    try:
        ov = overview.compute_overview(ov_conn)
    finally:
        ov_conn.close()
    assert ov["week"] == weeks[-1]
    assert ov["cities"] and ov["cities"][0]["count"] == 3
    assert ov["cities"][0]["temperature"] is not None


def test_current_week_majority_wins_over_lexicographically_larger_minority():
    rows = [{"week": "2026-W46"}] * 3 + [{"week": "2026-W47"}]
    assert build_db._current_week(rows) == "2026-W46"


def test_current_week_tie_takes_lexicographic_max():
    rows = [{"week": "2026-W46"}, {"week": "2026-W47"}]
    assert build_db._current_week(rows) == "2026-W47"


def test_current_week_ignores_missing_week_and_empty_rows():
    assert build_db._current_week([]) is None
    assert build_db._current_week([{"week": ""}, {}]) is None


# —— 缺陷 2：ISO 周年 + 周号 ——

def test_iso_week_cross_year_uses_iso_year():
    # 2021-01-01 属于 ISO 2020 年第 53 周
    assert fetch_fang._iso_week(datetime.date(2021, 1, 1)) == "2020-W53"


def test_iso_week_matches_existing_library_format():
    assert fetch_fang._iso_week(datetime.date(2026, 11, 9)) == "2026-W46"
    assert fetch_fang._iso_week(datetime.date(2027, 1, 1)) == "2026-W53"


# —— 调用点集成：build_db 只写原始数据，派生指标交给 recompute ——
#
# 旧实现让 build_db 自己算一遍温度（只用当期周 + 平台近月环比），recompute 又用
# 另一套口径覆盖；两套口径必然漂移（docs/45 P0-2）。现在 build_db 只写原始行，
# 派生列一律由 shikar.pipeline.recompute 计算，下面的用例锁住这个契约。

def _weekly_rows(xid="1", name="测试小区", city="北京", n=15,
                 start=datetime.date(2025, 9, 1)):
    """构造 ``n`` 期、每期相隔 28 天的 2026 CSV 行（跨度够、缺口落在 4 周窗口内）。"""
    rows = []
    d = start
    for i in range(n):
        y, w, _ = d.isocalendar()
        rows.append({
            "city": city, "district": "朝阳", "plate": "望京", "xiaoqu_id": xid,
            "xiaoqu_name": name, "week": f"{y}-W{w:02d}",
            "price": str(60000 + i * 300 + (i % 3) * 500),
            "supply": "100", "built_year": "2010", "mom1_pct": "-1.5",
            "source": "fang.com",
        })
        d += datetime.timedelta(days=28)
    return rows


def _run_main(monkeypatch, tmp_path, rows2026, rows2019=()):
    """在临时目录跑一次 build_db.main()，返回连接（调用方负责 close）。"""
    import sqlite3

    monkeypatch.setattr(build_db, "DATA", str(tmp_path))
    monkeypatch.setattr(
        build_db, "read_csv",
        lambda name: list(rows2026) if name == "fang_2026.csv" else list(rows2019),
    )
    build_db.main()
    conn = sqlite3.connect(str(tmp_path / "shikar.db"))
    conn.row_factory = sqlite3.Row
    return conn


def test_build_db_writes_raw_only_then_recompute_fills_temperature(monkeypatch, tmp_path):
    rows = _weekly_rows()
    conn = _run_main(monkeypatch, tmp_path, rows)
    try:
        weeks = [r["week"] for r in conn.execute(
            "SELECT week FROM xiaoqu_metric_weekly ORDER BY week")]
        assert weeks == sorted(r["week"] for r in rows), "多周行被丢弃/改期"
        assert conn.execute(
            "SELECT COUNT(*) FROM xiaoqu_metric_weekly WHERE temperature IS NOT NULL"
        ).fetchone()[0] == 0, "build_db 不得自行计算温度"
        assert conn.execute(
            "SELECT COUNT(*) FROM xiaoqu_metric_weekly WHERE mom4 IS NOT NULL"
        ).fetchone()[0] == 0, "build_db 不得把 mom1_pct 当 mom4 写入"

        pipeline.recompute(conn)
        filled = conn.execute(
            "SELECT COUNT(*) FROM xiaoqu_metric_weekly WHERE temperature IS NOT NULL"
        ).fetchone()[0]
        # 不是「只有当期周有温度」，而是逐期可算的都算。
        assert filled > 1
        latest = conn.execute(
            "SELECT temperature FROM xiaoqu_metric_weekly WHERE week = ?",
            (max(r["week"] for r in rows),),
        ).fetchone()[0]
        assert latest is not None
    finally:
        conn.close()


def test_week_whitespace_normalized_before_storage(monkeypatch, tmp_path):
    """week 带首尾空白时入库必须是去空白后的同一期，否则会出现重复期次。"""
    rows = _weekly_rows(n=3)
    rows[0]["week"] = " " + rows[0]["week"] + " "
    conn = _run_main(monkeypatch, tmp_path, rows)
    try:
        weeks = sorted(r["week"] for r in conn.execute(
            "SELECT week FROM xiaoqu_metric_weekly"))
        assert weeks == sorted(r["week"].strip() for r in rows)
        assert all(w == w.strip() for w in weeks)
    finally:
        conn.close()


def test_build_db_does_not_reimplement_derived_metrics():
    """派生指标只有一份实现：build_db 不得自带门槛 / 温度 / 周次解析副本。"""
    with open(build_db.__file__, "r", encoding="utf-8") as fh:
        src = fh.read()
    for forbidden in ("def parse_week(", "def sufficient(",
                      "compute_temperature(", "is_sufficient("):
        assert forbidden not in src, f"build_db 又复制了一份派生逻辑：{forbidden}"


def test_build_db_then_recompute_matches_qualification(monkeypatch, tmp_path):
    """同一份数据：合格小区 recompute 后有温度，不足小区没有。"""
    rows2026 = _weekly_rows(xid="S1", name="合格小区")
    rows2026 += _weekly_rows(xid="S2", name="不足小区", n=3)
    conn = _run_main(monkeypatch, tmp_path, rows2026)
    try:
        def per_xiaoqu():
            per = {}
            for r in conn.execute(
                "SELECT xiaoqu_id, temperature FROM xiaoqu_metric_weekly"
            ):
                per.setdefault(r["xiaoqu_id"], []).append(r["temperature"])
            return {k: any(t is not None for t in v) for k, v in per.items()}

        assert per_xiaoqu() == {"北京|S1": False, "北京|S2": False}
        pipeline.recompute(conn)
        assert per_xiaoqu() == {"北京|S1": True, "北京|S2": False}
    finally:
        conn.close()


def test_recompute_threshold_is_the_single_gate(monkeypatch, tmp_path):
    """灵敏度：把 recompute 的门槛调严，温度必须消失（证明门槛确实生效）。"""
    conn = _run_main(monkeypatch, tmp_path, _weekly_rows())
    try:
        def has_temp():
            return conn.execute(
                "SELECT COUNT(*) FROM xiaoqu_metric_weekly WHERE temperature IS NOT NULL"
            ).fetchone()[0] > 0

        pipeline.recompute(conn)
        assert has_temp() is True
        pipeline.recompute(conn, min_periods=16)
        assert has_temp() is False
    finally:
        conn.close()
