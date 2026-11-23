#!/usr/bin/env bash
# shikar 一键验证（本地/CI 同一入口）
#
# 目的：让「我验证过了」这句话可复跑、可贴证据。对标 pstack 的 create-verification-skill。
#
# 用法：
#   bash scripts/verify.sh            # 全量：语法 + JS + 测试
#   bash scripts/verify.sh --fast     # 跳过 pytest（只做语法/静态检查，秒级）
#   bash scripts/verify.sh --quiet    # 只输出结论行
#
# 退出码：0 全过；1 有失败项。CI 直接用它的退出码做门禁。
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1

FAST=0
QUIET=0
for arg in "$@"; do
  case "$arg" in
    --fast) FAST=1 ;;
    --quiet) QUIET=1 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "未知参数: $arg（--fast / --quiet）" >&2; exit 2 ;;
  esac
done

FAIL=0
SKIP=0
FAILED_ITEMS=()
SKIPPED_ITEMS=()

say() { [ "$QUIET" -eq 1 ] || printf '%s\n' "$*"; }
step() { say ""; say "== $* =="; }
pass() { say "PASS  $*"; }
fail() { say "FAIL  $*"; FAIL=1; FAILED_ITEMS+=("$*"); }
skip() { say "SKIP  $*"; SKIP=$((SKIP + 1)); SKIPPED_ITEMS+=("$*"); }

have() { command -v "$1" >/dev/null 2>&1; }

# 解释器必须真的能跑：Windows 上 `python3` 常是应用商店空壳（无输出、无声退出），
# 只看 command -v 会误判，所以要求能打印版本号。
pick_python() {
  local cand out
  for cand in "${PYTHON:-}" python3 python py; do
    [ -n "$cand" ] || continue
    have "$cand" || continue
    out=$("$cand" -c 'import sys;print(sys.version.split()[0])' 2>/dev/null)
    case "$out" in
      [0-9]*.[0-9]*) printf '%s' "$cand"; return 0 ;;
    esac
  done
  return 1
}

PY=$(pick_python) || { echo "找不到可用的 python3/python，无法验证" >&2; exit 2; }

say "shikar verify · $(date '+%F %T') · $(git rev-parse --short HEAD 2>/dev/null || echo 'no-git')"

# 1) Python 语法：整仓编译，不执行
step "Python 语法 (compileall)"
if OUT=$("$PY" -m compileall -q backend scripts 2>&1); then
  pass "backend/ + scripts/ 编译通过"
else
  fail "Python 语法错误"
  say "$OUT"
fi

# 2) Shell 语法：发布脚本自己先别炸
step "Shell 语法 (bash -n)"
sh_bad=0
for f in deploy/*.sh scripts/verify.sh; do
  [ -f "$f" ] || continue
  if ! OUT=$(bash -n "$f" 2>&1); then
    sh_bad=1
    fail "bash -n $f"
    say "$OUT"
  fi
done
[ "$sh_bad" -eq 0 ] && pass "deploy/*.sh + scripts/verify.sh 语法通过"

# 3) 前端静态资源：web/ 是纯静态，无构建步骤，门禁=语法+引用存在
step "前端静态资源"
if have node; then
  js_bad=0
  for f in web/*.js; do
    [ -f "$f" ] || continue
    if ! OUT=$(node --check "$f" 2>&1); then
      js_bad=1
      fail "node --check $f"
      say "$OUT"
    fi
  done
  [ "$js_bad" -eq 0 ] && pass "web/*.js 语法通过"
else
  skip "node 未安装，跳过 JS 语法检查"
fi

# index.html 引用的本地资源必须存在（防「改了文件名忘了改引用」）
REF_BAD=0
while read -r ref; do
  [ -e "web/$ref" ] || { REF_BAD=1; fail "web/index.html 引用了不存在的资源: $ref"; }
done < <(grep -oE '(src|href)="[^"#:?]+"' web/index.html 2>/dev/null \
         | sed -E 's/^(src|href)="//; s/"$//' | grep -vE '^(https?:|//|data:|mailto:)' | sort -u)
[ "$REF_BAD" -eq 0 ] && pass "web/index.html 本地资源引用齐全"

# 4) 测试：全量 pytest（--fast 跳过）
if [ "$FAST" -eq 1 ]; then
  step "pytest"
  skip "--fast 指定跳过 pytest"
else
  step "pytest"
  # 不要在这里再加 -q：backend/pytest.ini 的 addopts 已经是 -q，再加变 -qq 会吞掉「N passed」汇总行
  PYTEST_ARGS=()
  if "$PY" -c "import osmium" >/dev/null 2>&1; then
    :
  else
    # 不静默：明确说清跳过哪个文件、为什么
    PYTEST_ARGS+=(--ignore=tests/test_osm_poi.py)
    say "警告：未安装 pyosmium(osmium)，跳过 tests/test_osm_poi.py（安装：pip install -r backend/requirements-dev.txt）"
  fi
  if OUT=$(cd backend && "$PY" -m pytest "${PYTEST_ARGS[@]}" 2>&1); then
    pass "$(printf '%s\n' "$OUT" | grep -E 'passed|no tests ran' | tail -1)"
    [ "$QUIET" -eq 1 ] || printf '%s\n' "$OUT" | tail -3
  else
    fail "pytest 未通过"
    printf '%s\n' "$OUT" | tail -40
  fi
fi

# 结论
say ""
if [ "$FAIL" -eq 0 ]; then
  echo "VERIFY PASS${SKIP:+（跳过 $SKIP 项）}"
  if [ "$SKIP" -gt 0 ]; then
    for s in "${SKIPPED_ITEMS[@]}"; do echo "  跳过: $s"; done
  fi
  exit 0
else
  echo "VERIFY FAIL（$FAIL 项）"
  for f in "${FAILED_ITEMS[@]}"; do echo "  失败: $f"; done
  exit 1
fi
