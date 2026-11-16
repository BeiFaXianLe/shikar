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
