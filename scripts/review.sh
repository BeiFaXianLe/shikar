#!/usr/bin/env bash
# 审核范围报告入口（本地一条命令，60 秒内出结果）
#
# 依据：docs/review/README.md、docs/57-工作流-pstack对照复审-2026-11-25.md §1.1
# 为什么要有它：审核方的范围原来靠「怕漏就全仓扫」决定，现在由 diff + 调用方 + 建议测试决定。
#
# 用法：
#   bash scripts/review.sh                    # 基线默认 origin/main
#   bash scripts/review.sh --base origin/main
#   bash scripts/review.sh --pr 57            # 报告名用 PR 编号：docs/review/pr-57.md
#   bash scripts/review.sh --run              # 顺带跑建议测试，把输出写进报告
#   bash scripts/review.sh --force            # 覆盖已存在的报告
#
# 只读 git 与工作区，除 docs/review/pr-*.md 外不落盘。
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1

# Windows 上 python3 可能是应用商店空壳（无输出、无声退出），所以要求能打印版本号。
pick_python() {
  local cand out
  for cand in "${PYTHON:-}" python3 python py; do
    [ -n "$cand" ] || continue
    command -v "$cand" >/dev/null 2>&1 || continue
    out=$("$cand" -c 'import sys;print(sys.version.split()[0])' 2>/dev/null)
    case "$out" in
      [0-9]*.[0-9]*) printf '%s' "$cand"; return 0 ;;
    esac
  done
  return 1
}

PY=$(pick_python) || { echo "找不到可用的 python3/python" >&2; exit 2; }

exec "$PY" scripts/review/report.py "$@"
