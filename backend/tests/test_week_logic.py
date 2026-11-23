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

from shikar import overview

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
    """端到端：多周 CSV 全部入库，当期周（W47）有温度、榜单非空。

    这是 testWorker 复验发现的缺口：旧去重键 (city, xiaoqu_id) 把 12 行截成 3 行
    （只剩首周），当期周没有温度、overview 必空。
    """
    data = tmp_path / "data"
    data.mkdir()
    header = "city,district,plate,xiaoqu_id,xiaoqu_name,week,price,supply,built_year,mom1_pct,source"
    (data / "history2019.csv").write_text(header + "\n", encoding="utf-8-sig")

    # 跨度 ≥365 天、相邻缺口均 ≤365 天，且当期周为 2026-W47（满足 #36 门槛）
    weeks = ["2025-W05", "2025-W22", "2026-W05", "2026-W47"]
    lines = [header]
    for i, name in enumerate(["A小区", "B小区", "C小区"]):
        for j, w in enumerate(weeks):
            lines.append(
                f"北京,朝阳,望京,X{i},{name},{w},{50000 + i * 1000 + j * 500},10,2005,1.0,fang.com"
            )
    (data / "fang_2026.csv").write_text("\n".join(lines) + "\n", encoding="utf-8-sig")

    monkeypatch.setattr(build_db, "DATA", str(data))
    build_db.main()

    conn = sqlite3.connect(str(data / "shikar.db"))
    conn.row_factory = sqlite3.Row
    try:
        n = conn.execute("SELECT COUNT(*) FROM xiaoqu_metric_weekly").fetchone()[0]
        assert n == 12, f"多周行被丢弃，只剩 {n} 行"
        rows = conn.execute(
            "SELECT temperature FROM xiaoqu_metric_weekly WHERE week = '2026-W47'"
        ).fetchall()
        assert len(rows) == 3
        assert all(r["temperature"] is not None for r in rows), "当期周温度为空"
    finally:
        conn.close()

    ov_conn = sqlite3.connect(str(data / "shikar.db"))
    ov_conn.row_factory = sqlite3.Row
    try:
        ov = overview.compute_overview(ov_conn)
    finally:
        ov_conn.close()
    assert ov["week"] == "2026-W47"
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


# —— 调用点集成：温度只能落在推导出的当期周上 ——
#
# 上面几例只测 _current_week 本身。若 main() 里把入参写错（例如传 rows2019
# 而非 cur），那些用例仍会全绿，但温度会被算到错误期次上。本用例跑真实的
# main()，从产物库反查哪一期拿到了 temperature。

def _rows2026(city="北京", xid="1", name="测试小区", week="2026-W46"):
    return [{
        "city": city, "district": "朝阳", "plate": "望京", "xiaoqu_id": xid,
        "xiaoqu_name": name, "week": week, "price": "60000", "supply": "100",
        "built_year": "2010", "mom1_pct": "-1.5", "source": "fang.com",
    }]


def _rows2019(city="北京", xid="9", name="测试小区", weeks=("2025-06", "2025-12", "2026-06")):
    """同一小区多期历史。

    期次需同时满足 sufficient() 的三条：≥3 期、跨度≥365 天、相邻缺口≤365 天。
    2025-06 → 2026-11(W46) 跨度 533 天，各相邻缺口均 <365。
    """
    return [{
        "city": city, "district": "朝阳", "plate": "望京", "xiaoqu_id": xid,
        "xiaoqu_name": name, "week": w, "price": "50000", "source": "lianjia",
        "lng": "116.48", "lat": "39.99",
    } for w in weeks]


def _run_main(monkeypatch, tmp_path, rows2026, rows2019):
    """在临时目录跑一次 main()，返回 {week: temperature} 映射。"""
    import sqlite3

    monkeypatch.setattr(build_db, "DATA", str(tmp_path))
    monkeypatch.setattr(
        build_db, "read_csv",
        lambda name: rows2026 if name == "fang_2026.csv" else rows2019,
    )
    build_db.main()

    conn = sqlite3.connect(str(tmp_path / "shikar.db"))
    try:
        return dict(conn.execute(
            "SELECT week, temperature FROM xiaoqu_metric_weekly"
        ).fetchall())
    finally:
        conn.close()


def test_temperature_lands_only_on_derived_current_week(monkeypatch, tmp_path):
    temps = _run_main(
        monkeypatch, tmp_path,
        _rows2026(week="2026-W46"), _rows2019(),
    )
    # 当期周由 2026 CSV 推导为 2026-W46，只有这一期允许有温度。
    assert temps["2026-W46"] is not None
    assert temps["2025-06"] is None
    assert temps["2025-12"] is None
    assert temps["2026-06"] is None


def test_temperature_follows_csv_week_instead_of_hardcoded_one(monkeypatch, tmp_path):
    """CSV 换成 W47 后，温度必须落在 W47——这条会在硬编码复发时失败。"""
    temps = _run_main(
        monkeypatch, tmp_path,
        _rows2026(week="2026-W47"), _rows2019(),
    )
    assert temps["2026-W47"] is not None
    assert temps["2025-06"] is None
    assert temps["2026-06"] is None


def test_whitespace_in_week_does_not_block_temperature(monkeypatch, tmp_path):
    """week 带首尾空白时，推导值与入库值必须一致，否则温度静默丢失。"""
    temps = _run_main(
        monkeypatch, tmp_path,
        _rows2026(week=" 2026-W46 "), _rows2019(),
    )
    assert temps["2026-W46"] is not None


def test_build_db_uses_pipeline_gate():
    """门槛只有一份实现：build_db 必须引用 pipeline.is_sufficient，不得自带副本。"""
    from shikar import pipeline

    assert build_db.pipeline is pipeline
    assert build_db.pipeline.is_sufficient is pipeline.is_sufficient
    with open(build_db.__file__, "r", encoding="utf-8") as fh:
        src = fh.read()
    assert "def parse_week(" not in src, "build_db 又复制了一份周次解析"
    assert "def sufficient(" not in src, "build_db 又复制了一份充分性门槛"
    assert "pipeline.is_sufficient(" in src


def test_build_db_and_recompute_agree_on_same_data(monkeypatch, tmp_path):
    """交叉对照：同一份数据，build_db 与 pipeline.recompute 的合格判定必须一致。

    只要有人把门槛拆成两份、或改了一处，这里就会红：先由 build_db 建库，
    再用 recompute 在同一库上重算，判定（是否有温度）必须完全相同。
    """
    import sqlite3

    from shikar import pipeline

    def row(xid, name, week):
        return {
            "city": "北京", "district": "朝阳", "plate": "望京", "xiaoqu_id": xid,
            "xiaoqu_name": name, "week": week, "price": "60000", "supply": "100",
            "built_year": "2010", "mom1_pct": "1.0", "source": "fang.com",
        }

    rows2026 = [row("S1", "合格小区", w)
                for w in ("2025-W05", "2025-W22", "2026-W05", "2026-W47")]
    rows2026 += [row("S2", "不足小区", w) for w in ("2026-W05", "2026-W47")]

    monkeypatch.setattr(build_db, "DATA", str(tmp_path))
    monkeypatch.setattr(
        build_db, "read_csv",
        lambda name: rows2026 if name == "fang_2026.csv" else [],
    )
    build_db.main()

    conn = sqlite3.connect(str(tmp_path / "shikar.db"))
    conn.row_factory = sqlite3.Row
    try:
        def verdicts():
            per: dict[str, list] = {}
            for r in conn.execute(
                "SELECT xiaoqu_id, temperature FROM xiaoqu_metric_weekly"
            ):
                per.setdefault(r["xiaoqu_id"], []).append(r["temperature"])
            return {k: any(t is not None for t in v) for k, v in per.items()}

        before = verdicts()
        assert before == {"北京|S1": True, "北京|S2": False}, before

        pipeline.recompute(conn)
        after = verdicts()
        assert after == before, f"recompute 与 build_db 判定不一致：build_db={before} recompute={after}"
    finally:
        conn.close()
