#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 data/backups/ 里的最新备份推送到**私有** GitHub 仓库做异地容灾。

## 为什么单独一个私有仓库

shikar 主仓库是 PUBLIC（只放代码）。备份里含真实成交记录与 POI 数据，
推公开仓库等于把数据资产公开，且百度 POI 数据二次分发可能违反其条款。
所以备份推到独立的私有仓库 BeiFaXianLe/shikar-data-backup。

## 工作方式

用 git 的「孤儿快照」模式：每次推送只保留最新一份备份 + 一份索引，
不累积历史二进制（否则仓库会无限膨胀）。

    backup-repo/
      latest/critical-<时间戳>.tar.gz
      latest/INDEX.md            ← 备份清单（时间、体积、内容摘要）
      README.md

每次推送都是 `git commit --amend` 式的单提交覆盖（force push），
所以远程仓库体积 ≈ 一份备份（约 4.3MB）。

## 用法

  python3 scripts/push_backup.py                    # 推最新一份
  python3 scripts/push_backup.py --dry-run          # 只看会做什么
  python3 scripts/push_backup.py --keep-local 3     # 本地也保留最近 3 份

需要环境变量 GH_TOKEN（有 repo 权限），或本机 gh 已登录。
"""
import argparse
import glob
import os
import shutil
import subprocess
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKUP_DIR = os.path.join(ROOT, "data", "backups")
WORK_DIR = os.path.join(ROOT, ".backup-repo")   # 临时工作区，已在 .gitignore
REMOTE = "https://github.com/BeiFaXianLe/shikar-data-backup.git"
BRANCH = "main"


def run(cmd, cwd=None, check=True, capture=True):
    r = subprocess.run(
        cmd, cwd=cwd, shell=isinstance(cmd, str),
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        text=True,
    )
    if check and r.returncode != 0:
        print("命令失败: %s" % (cmd if isinstance(cmd, str) else " ".join(cmd)))
        print((r.stdout or "")[-2000:])
        sys.exit(1)
    return (r.stdout or "").strip()


def latest_backup():
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "critical-*.tar.gz")))
    if not files:
        return None
    return files[-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--keep-local", type=int, default=0,
                    help="本地备份保留份数，0 表示不动")
    ap.add_argument("--remote", default=REMOTE)
    a = ap.parse_args()

    src = latest_backup()
    if not src:
        sys.exit("没有可推送的备份（%s 下无 critical-*.tar.gz）" % BACKUP_DIR)
    size_mb = os.path.getsize(src) / 1e6
    print("待推送: %s (%.2f MB)" % (os.path.basename(src), size_mb))

    if a.dry_run:
        print("[dry-run] 将重建 %s 并 force push 到 %s" % (WORK_DIR, a.remote))
        return

    # 1. 准备干净的工作区
    if os.path.exists(WORK_DIR):
        shutil.rmtree(WORK_DIR)
    os.makedirs(WORK_DIR)
    latest_dir = os.path.join(WORK_DIR, "latest")
    os.makedirs(latest_dir)

    # 2. 只放最新一份备份
    shutil.copy2(src, os.path.join(latest_dir, os.path.basename(src)))

    # 3. 写索引
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    index = [
        "# shikar 数据备份索引",
        "",
        "- 推送时间：%s" % ts,
        "- 备份文件：`%s`" % os.path.basename(src),
        "- 体积：%.2f MB" % size_mb,
        "",
        "## 包含内容",
        "",
        "| 文件 | 说明 |",
        "|---|---|",
        "| `files/fang_preview_all.csv` | 逐周累积的历史成交样本（不可再生） |",
        "| `files/fang_2026.csv` | 2026 当期小区快照 |",
        "| `files/history2019.csv` | 2019 链家全量快照 |",
        "| `db.sql` | poi / community_poi / signal_event / source_snapshot 表 |",
        "",
        "## 恢复方式",
        "",
        "```bash",
        "# 1. 拉取备份",
        "git clone https://github.com/BeiFaXianLe/shikar-data-backup.git",
        "cd shikar-data-backup/latest",
        "",
        "# 2. 恢复到项目（CSV 会覆盖，旧文件留 .pre-restore）",
        "python3 scripts/backup_critical.py restore --file critical-*.tar.gz",
        "```",
        "",
        "> 本仓库为**私有**：含真实成交记录与 POI 数据，不要转为公开。",
    ]
    with open(os.path.join(latest_dir, "INDEX.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(index))

    with open(os.path.join(WORK_DIR, "README.md"), "w", encoding="utf-8") as f:
        f.write(
            "# shikar-data-backup\n\n"
            "shikar 数据资产的异地备份仓库（私有）。\n\n"
            "只保留**最新一份**备份，历史版本由本地 `data/backups/` 保留。\n\n"
            "内容与恢复方式见 `latest/INDEX.md`。\n\n"
            "> 含真实成交记录与百度 POI 数据，**不要转为公开**。\n"
        )

    # 4. 初始化并推送（单提交 + force，保证仓库体积恒定）
    run(["git", "init", "-q", "-b", BRANCH], cwd=WORK_DIR)
    run(["git", "config", "user.name", "shikar-backup-bot"], cwd=WORK_DIR)
    run(["git", "config", "user.email", "backup@local"], cwd=WORK_DIR)
    run(["git", "add", "-A"], cwd=WORK_DIR)
    run(["git", "commit", "-q", "-m", "backup: %s" % os.path.basename(src)], cwd=WORK_DIR)
    run(["git", "remote", "add", "origin", a.remote], cwd=WORK_DIR)

    print("推送中…")
    out = run(["git", "push", "-f", "origin", BRANCH], cwd=WORK_DIR, check=False)
    if "error" in out.lower() or "fatal" in out.lower() or "rejected" in out.lower():
        print(out[-1500:])
        print("\n推送失败。请确认 gh 已登录或设置了 GH_TOKEN。")
        sys.exit(1)
    print("推送完成")

    # 5. 可选：本地保留份数
    if a.keep_local > 0:
        files = sorted(glob.glob(os.path.join(BACKUP_DIR, "critical-*.tar.gz")))
        for old in files[: max(0, len(files) - a.keep_local)]:
            os.remove(old)
            print("  删除本地旧备份: %s" % os.path.basename(old))

    print("完成: https://github.com/BeiFaXianLe/shikar-data-backup")


if __name__ == "__main__":
    main()
