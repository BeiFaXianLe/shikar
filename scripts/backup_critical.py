#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""备份 / 恢复 shikar 的「不可再生」数据。

## 为什么需要这个脚本

shikar 有两类数据，价值与可重建性完全不同：

**A. 可重建（不必单独备份，有源文件即可）**
   - xiaoqu / xiaoqu_metric_weekly  ← data/fang_2026.csv + data/history2019.csv
   - subway_*                       ← data/subway/*.geojson
   这些丢了重跑 build_db.py 就回来。

**B. 不可再生（必须备份）**
   - data/fang_preview_all.csv      ← **每周追加 500 条历史成交，丢一周就永久缺失**
   - data/fang_2026.csv             ← 当期快照（每周覆盖，但历史版本有追溯价值）
   - data/history2019.csv           ← 静态公开数据集（可重下，但成本高）
   - poi / community_poi 表         ← 由百度 POI 采集（需 API 配额）
   - signal_event / source_snapshot ← 运行期产生

本脚本同时备份 B 类的 **CSV 文件** 和 **数据库表**。
CSV 才是根本：数据库里的历史成交是由 fang_preview_all.csv 经 ingest_history.py 灌进去的，
所以只要 CSV 在，库随时能重建；反之 CSV 丢了，库里的数据也只是这一份副本。

## 用法

  python3 scripts/backup_critical.py backup  [--out-dir data/backups] [--keep 12]
  python3 scripts/backup_critical.py restore --file data/backups/critical-YYYYmmdd_HHMMSS.tar.gz
  python3 scripts/backup_critical.py list    [--out-dir data/backups]

备份产物是 tar.gz，内含：
  files/   ← 原始 CSV（原样保存，可直接用）
  db.sql   ← 不可再生表的 INSERT 语句
"""
import argparse
import glob
import gzip
import io
import os
import sqlite3
import sys
import tarfile
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(ROOT, "data", "shikar.db")
DEFAULT_OUT = os.path.join(ROOT, "data", "backups")

# 不可从其他来源重建、必须原样保存的文件（相对 ROOT）
CRITICAL_FILES = (
    "data/fang_preview_all.csv",   # 每周追加的历史成交样本，最珍贵
    "data/fang_2026.csv",          # 当期快照，历史版本有追溯价值
    "data/history2019.csv",        # 静态公开数据集
)

# 不可从 CSV 重建的表；顺序即恢复顺序（poi 先于 community_poi）
CRITICAL_TABLES = ("poi", "community_poi", "signal_event", "source_snapshot")


def _sql_literal(v):
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v).replace("'", "''")
    return "'" + s + "'"


def dump_table_sql(conn, table, out):
    """把一张表导出成 DELETE + INSERT 语句写入 out（StringIO，按批 flush 避免占内存）。

    :return: 导出的行数；**表不存在时返回 ``None``**（供调用方当作「不可再生数据缺失」，
        与「表存在但 0 行」区分开）。
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
    if not cols:
        out.write("-- 表 %s 不存在，跳过\n" % table)
        return None
    total = conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
    out.write("-- %s: %d 行\n" % (table, total))
    out.write("DELETE FROM %s;\n" % table)
    if total == 0:
        return 0

    col_list = ",".join(cols)
    cur = conn.execute("SELECT %s FROM %s" % (col_list, table))
    n = 0
    buf = []
    while True:
        batch = cur.fetchmany(5000)
        if not batch:
            break
        for row in batch:
            buf.append(
                "INSERT INTO %s(%s) VALUES(%s);\n"
                % (table, col_list, ",".join(_sql_literal(v) for v in row))
            )
            n += 1
        if len(buf) >= 5000:
            out.write("".join(buf))
            buf = []
    if buf:
        out.write("".join(buf))
    return n


def cmd_backup(args) -> int:
    """生成一份不可再生数据备份；关键文件缺失时返回非零（不当作成功）。"""
    db = args.db
    out_dir = args.out_dir
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    # 先写成 partial- 前缀，确认关键文件齐全后才改名为 critical-：
    # push_backup 只挑 critical-*.tar.gz，不完整备份永远不会被选中推送。
    # （推送是 force push，用不完整副本覆盖异地唯一备份 = 直接丢数据。）
    path = os.path.join(out_dir, "partial-%s.tar.gz" % ts)

    file_stats = {}
    table_counts = {}
    sql_buf = io.StringIO()
    sql_buf.write("-- shikar 不可再生表备份 %s\n" % ts)
    sql_buf.write("-- 表: %s\n\n" % ", ".join(CRITICAL_TABLES))

    if os.path.exists(db):
        conn = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
        try:
            for t in CRITICAL_TABLES:
                table_counts[t] = dump_table_sql(conn, t, sql_buf)
        finally:
            conn.close()
        db_exists = True
    else:
        sql_buf.write("-- 数据库不存在，跳过表备份\n")
        db_exists = False

    with tarfile.open(path, "w:gz") as tar:
        # 1. 原始文件（最珍贵：CSV 丢了无法再生）
        for rel in CRITICAL_FILES:
            full = os.path.join(ROOT, rel)
            if not os.path.exists(full):
                file_stats[rel] = None
                continue
            tar.add(full, arcname="files/" + os.path.basename(rel))
            file_stats[rel] = os.path.getsize(full)
        # 2. 不可再生表的 SQL
        data = sql_buf.getvalue().encode("utf-8")
        info = tarfile.TarInfo("db.sql")
        info.size = len(data)
        info.mtime = int(datetime.now().timestamp())
        tar.addfile(info, io.BytesIO(data))

    size = os.path.getsize(path)

    # 完整性门：**文件 + 库 + 四张不可再生表**都要在（docs/45 P3-2）。
    # 09:13 那份备份就是缺 fang_preview_all.csv 却“成功”的；
    # 只查 CSV 同样会放行「库/表丢了」的备份，而 push_backup 是 force push，
    # 推上去就会用空的 db.sql 覆盖异地唯一那份 POI/信号表。
    missing = [rel for rel, sz in file_stats.items() if sz is None]
    missing_tables = [t for t, n in table_counts.items() if n is None]
    incomplete = bool(missing) or not db_exists or bool(missing_tables)

    complete_path = os.path.join(out_dir, "critical-%s.tar.gz" % ts)
    if not incomplete:
        os.replace(path, complete_path)
        path = complete_path

    print("备份完成: %s (%.2f MB)" % (path, size / 1e6))
    print("  文件:")
    for rel, sz in file_stats.items():
        print("    %-32s %s" % (os.path.basename(rel), "缺失" if sz is None else "%.1f KB" % (sz / 1024)))
    print("  表:")
    if not db_exists:
        print("    %-32s %s" % ("(数据库)", "缺失"))
    for t, n in table_counts.items():
        print("    %-32s %s" % (t, "缺失" if n is None else "%d 行" % n))

    if incomplete:
        if missing:
            print("❌ 关键文件缺失：%s" % ", ".join(os.path.basename(m) for m in missing))
        if not db_exists:
            print("❌ 数据库不存在：%s（表备份为空）" % db)
        if missing_tables:
            print("❌ 不可再生表缺失：%s" % ", ".join(missing_tables))
        print("   已保存为 %s（不会被 push_backup 选中推送）。" % os.path.basename(path))
        print("   缺失的不可再生数据请先从私有备份仓库取回 / 先重建库，再重跑本命令；")
        print("   本命令返回非零，deploy/refresh.sh 据此跳过异地推送。")

    # 轮转：只保留最近 keep 份（仅统计完整的 critical-*）
    files = sorted(glob.glob(os.path.join(out_dir, "critical-*.tar.gz")))
    if args.keep > 0 and len(files) > args.keep:
        for old in files[: len(files) - args.keep]:
            os.remove(old)
            print("  删除旧备份: %s" % os.path.basename(old))
    return 1 if incomplete else 0


def cmd_restore(args):
    db = args.db
    path = args.file
    if not os.path.exists(path):
        sys.exit("备份文件不存在：%s" % path)

    restored_files = []
    sql = None
    with tarfile.open(path, "r:gz") as tar:
        for m in tar.getmembers():
            if not m.isfile():
                continue
            f = tar.extractfile(m)
            if f is None:
                continue
            if m.name.startswith("files/"):
                base = os.path.basename(m.name)
                target = os.path.join(ROOT, "data", base)
                # 覆盖前把旧文件留一份 .pre-restore，避免误覆盖无法回退
                if os.path.exists(target):
                    os.replace(target, target + ".pre-restore")
                with open(target, "wb") as o:
                    o.write(f.read())
                restored_files.append(base)
            elif m.name == "db.sql":
                sql = f.read().decode("utf-8")

    print("已恢复文件: %s" % (", ".join(restored_files) if restored_files else "无"))

    if sql and os.path.exists(db):
        conn = sqlite3.connect(db)
        try:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.executescript(sql)
            conn.commit()
            bad = conn.execute("PRAGMA foreign_key_check").fetchall()
            if bad:
                print("⚠ 恢复后发现 %d 条外键违规（备份可能来自不同版本的小区数据）" % len(bad))
            for t in CRITICAL_TABLES:
                try:
                    n = conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                    print("  %-18s %d 行" % (t, n))
                except sqlite3.Error:
                    pass
        finally:
            conn.close()
    elif sql:
        print("⚠ 数据库不存在（%s），只恢复了文件。先跑 build_db.py 再恢复表。" % db)

    print("恢复完成")


def cmd_list(args):
    files = sorted(glob.glob(os.path.join(args.out_dir, "critical-*.tar.gz")))
    if not files:
        print("无备份文件（目录 %s）" % args.out_dir)
        return
    print("备份目录: %s" % args.out_dir)
    total = 0
    for f in files:
        sz = os.path.getsize(f)
        total += sz
        print("  %s  %.2f MB" % (os.path.basename(f), sz / 1e6))
    print("合计 %.2f MB（%d 份）" % (total / 1e6, len(files)))


def main(argv=None):
    ap = argparse.ArgumentParser(description="shikar 关键数据备份/恢复")
    ap.add_argument("--db", default=DEFAULT_DB, help="数据库路径")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backup", help="备份不可重建的表")
    b.add_argument("--out-dir", default=DEFAULT_OUT)
    b.add_argument("--keep", type=int, default=8, help="保留最近 N 份，0 表示不轮转")
    b.set_defaults(func=cmd_backup)

    r = sub.add_parser("restore", help="从备份恢复")
    r.add_argument("--file", required=True)
    r.set_defaults(func=cmd_restore)

    ls = sub.add_parser("list", help="列出备份")
    ls.add_argument("--out-dir", default=DEFAULT_OUT)
    ls.set_defaults(func=cmd_list)

    args = ap.parse_args(argv)
    rc = args.func(args)
    if rc:
        raise SystemExit(rc)


if __name__ == "__main__":
    main()
