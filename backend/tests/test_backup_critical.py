"""``scripts/backup_critical.py`` 的回归测试（docs/45 报告 P3-2）。

覆盖四条断言：

* 关键文件齐全 + 库在 + 四张不可再生表都在 → 生成 ``critical-<ts>.tar.gz``，返回 0；
* 缺任一不可再生**文件** → 返回非零，产物只能叫 ``partial-<ts>.tar.gz``；
* **库不存在** → 同样非零 + ``partial-``（否则 force push 会用空 ``db.sql``
  覆盖异地唯一那份 POI / 信号表）；
* **某张不可再生表缺失** → 同样非零 + ``partial-``。

``push_backup.py`` 只挑 ``critical-*.tar.gz``，所以 ``partial-`` 永远不会被推上去。
全部在 ``tmp_path`` 里跑，不读写真实 ``data/``。
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import types

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_script(name):
    """按文件路径加载 ``scripts/<name>.py``（模块级无副作用）。"""
    path = os.path.join(_REPO_ROOT, "scripts", f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"_script_{name}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


backup = _load_script("backup_critical")


def _prepare(tmp_path, monkeypatch, present, *, with_db=True, tables=None):
    """搭一个最小仓库根：按 ``present`` 造 CRITICAL_FILES，按需建库与表。"""
    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    for rel in backup.CRITICAL_FILES:
        if rel in present:
            (root / rel).write_text("x", encoding="utf-8")

    db = root / "data" / "shikar.db"
    if with_db:
        conn = sqlite3.connect(str(db))
        try:
            for t in (backup.CRITICAL_TABLES if tables is None else tables):
                conn.execute("CREATE TABLE %s (id INTEGER)" % t)
            conn.commit()
        finally:
            conn.close()

    out = tmp_path / "backups"
    monkeypatch.setattr(backup, "ROOT", str(root))
    args = types.SimpleNamespace(db=str(db), out_dir=str(out), keep=0)
    return args, out


def _names(out):
    return os.listdir(out)


def test_missing_critical_file_fails_loudly(tmp_path, monkeypatch, capsys):
    """缺不可再生文件：非零退出 + 只留 partial-，绝不冒充 critical-。"""
    args, out = _prepare(tmp_path, monkeypatch, present=set())
    rc = backup.cmd_backup(args)

    assert rc == 1
    names = _names(out)
    assert any(n.startswith("partial-") for n in names)
    assert not any(n.startswith("critical-") for n in names), (
        "不完整备份不得命名为 critical-*.tar.gz：push_backup 会把它推上去覆盖异地备份"
    )
    assert "关键文件缺失" in capsys.readouterr().out


def test_missing_database_fails_loudly(tmp_path, monkeypatch, capsys):
    """库不存在：表备份为空，同样不能算成功（会被 force push 覆盖异地 db.sql）。"""
    args, out = _prepare(
        tmp_path, monkeypatch, present=set(backup.CRITICAL_FILES), with_db=False
    )
    rc = backup.cmd_backup(args)

    assert rc == 1
    assert not any(n.startswith("critical-") for n in _names(out))
    printed = capsys.readouterr().out
    assert "数据库不存在" in printed


def test_missing_critical_table_fails_loudly(tmp_path, monkeypatch, capsys):
    """某张不可再生表缺失：不能当作完整备份。"""
    kept = tuple(t for t in backup.CRITICAL_TABLES if t != "community_poi")
    args, out = _prepare(
        tmp_path, monkeypatch, present=set(backup.CRITICAL_FILES), tables=kept
    )
    rc = backup.cmd_backup(args)

    assert rc == 1
    assert not any(n.startswith("critical-") for n in _names(out))
    assert "不可再生表缺失" in capsys.readouterr().out


def test_complete_backup_returns_zero(tmp_path, monkeypatch):
    """文件 + 库 + 四张表齐全：生成 critical- 备份并返回 0。"""
    args, out = _prepare(tmp_path, monkeypatch, present=set(backup.CRITICAL_FILES))
    rc = backup.cmd_backup(args)

    assert rc == 0
    names = _names(out)
    assert any(n.startswith("critical-") and n.endswith(".tar.gz") for n in names)
    assert not any(n.startswith("partial-") for n in names)
