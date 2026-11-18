#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""备份 / 恢复「不可重建」数据（poi、community_poi、signal_event、source_snapshot）。

为什么单独做这个脚本：
  整库 shikar.db 有 248MB，但其中 xiaoqu / xiaoqu_metric_weekly / subway_* 都能从
  CSV / GeoJSON 重建。真正不可再生的只有 POI 关系与运行期事件，压缩后约 4MB。
  按整库备份既浪费磁盘（服务器只剩 14G）又慢，按「不可重建表」备份才是正确粒度。

用法：
  python3 scripts/backup_critical.py backup  [--out-dir data/backups] [--keep 8]
  python3 scripts/backup_critical.py restore --file data/backups/critical-YYYYmmdd.sql.gz
  python3 scripts/backup_critical.py list    [--out-dir data/backups]

备份文件是可读的 SQL（gzip 压缩），用 sqlite3 或本脚本都能恢复。
"""
import argparse
import glob
import gzip
import os
import sqlite3
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(ROOT, "data", "shikar.db")
DEFAULT_OUT = os.path.join(ROOT, "data", "backups")

# 不可从 CSV / GeoJSON 重建的表；顺序即恢复顺序（poi 先于 community_poi）
CRITICAL_TABLES = ("poi", "community_poi", "signal_event", "source_snapshot")


def _sql_literal(v):
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(v)
    s = str(v).replace("'", "''")
    return "'" + s + "'"


def dump_table(conn, table, out):
    """把一张表导出成 DELETE + INSERT 语句，逐批写入 out（避免大表占满内存）。

    out 是 gzip 文件对象，只接受 bytes，所以每段都要 encode。
    """
    def w(s):
        out.write(s.encode("utf-8"))

    cols = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
    if not cols:
        w("-- 表 %s 不存在，跳过\n" % table)
        return 0
    total = conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
    w("-- %s: %d 行\n" % (table, total))
    w("DELETE FROM %s;\n" % table)
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
            w("".join(buf))
            buf = []
    if buf:
        w("".join(buf))
    return n


def cmd_backup(args):
    db = args.db
    if not os.path.exists(db):
        sys.exit("数据库不存在：%s" % db)
    out_dir = args.out_dir
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(out_dir, "critical-%s.sql.gz" % ts)

    conn = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    try:
        with gzip.open(path, "wb", compresslevel=6) as gz:
            gz.write(("-- shikar 关键数据备份 %s\n" % ts).encode("utf-8"))
            gz.write(("-- 表: %s\n\n" % ", ".join(CRITICAL_TABLES)).encode("utf-8"))
            counts = {}
            for t in CRITICAL_TABLES:
                counts[t] = dump_table(conn, t, gz)
    finally:
        conn.close()

    size = os.path.getsize(path)
    print("备份完成: %s (%.2f MB)" % (path, size / 1e6))
    for t, n in counts.items():
        print("  %-18s %d 行" % (t, n))

    # 轮转：只保留最近 keep 份
    files = sorted(glob.glob(os.path.join(out_dir, "critical-*.sql.gz")))
    if args.keep > 0 and len(files) > args.keep:
        for old in files[: len(files) - args.keep]:
            os.remove(old)
            print("  删除旧备份: %s" % os.path.basename(old))
    return path


def cmd_restore(args):
    db = args.db
    path = args.file
    if not os.path.exists(path):
        sys.exit("备份文件不存在：%s" % path)
    if not os.path.exists(db):
        sys.exit("目标数据库不存在：%s（请先跑 build_db.py）" % db)

    with gzip.open(path, "rb") as f:
        sql = f.read().decode("utf-8")

    conn = sqlite3.connect(db)
    try:
        # community_poi 有外键指向 xiaoqu / poi，恢复期间先关掉外键检查，
        # 恢复完再统一校验，避免顺序问题导致整体失败。
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.executescript(sql)
        conn.commit()
        bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            print("⚠ 恢复后发现 %d 条外键违规（可能是备份来自不同版本的小区数据）" % len(bad))
        for t in CRITICAL_TABLES:
            try:
                n = conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                print("  %-18s %d 行" % (t, n))
            except sqlite3.Error:
                pass
        print("恢复完成: %s" % db)
    finally:
        conn.close()


def cmd_list(args):
    files = sorted(glob.glob(os.path.join(args.out_dir, "critical-*.sql.gz")))
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
    args.func(args)


if __name__ == "__main__":
    main()
