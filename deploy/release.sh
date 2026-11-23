#!/usr/bin/env bash
# shikar 代码发布 / 回滚（幂等）
#
# 跑在服务器上（ubuntu@192.144.183.46，仓库 /opt/shikar）。不在服务器上跑会直接退出。
#
# 用法：
#   bash deploy/release.sh                       # 发布 origin/main 最新提交
#   bash deploy/release.sh --ref <tag|sha>       # 发布指定 tag / commit
#   bash deploy/release.sh --dry-run             # 只打印将做什么，不改任何文件
#   bash deploy/release.sh --no-restart          # 同步代码 + 跑测试，不重启服务
#   bash deploy/release.sh --no-tests            # 跳过服务端 pytest（不推荐）
#   bash deploy/release.sh --yes                 # 跳过交互确认（无人值守）
#   bash deploy/release.sh --rollback latest     # 回滚到上一个发布点
#   bash deploy/release.sh --rollback <发布ID>   # 回滚到指定发布点（.releases/ 下的目录名）
#   bash deploy/release.sh --list                # 列出可回滚的发布点
#
# 写死的安全约束（别改）：
#   1. 只同步代码目录 backend/ web/ scripts/ deploy/，**绝不覆盖 data/ 与 .secrets/**；
#      data/ 里是不可再生数据（历史成交 CSV）和 259MB 的库。
#   2. 覆盖前把将被替换的代码目录打包进 .releases/<ID>/code.tar.gz，回滚只依赖它。
#   3. 重启后 /api/health 与 /api/overview 必须通过，否则自动回滚并以非 0 退出。
#   4. 不自动删旧备份（磁盘只剩 ~8.9G，删是人的决定，脚本只提醒）。
#
# 退出码：0 成功（含「已是最新」）；1 失败并已尝试回滚；2 参数/前置条件问题
set -uo pipefail

APP_DIR=${APP_DIR:-/opt/shikar}
SERVICE=${SERVICE:-shikar}
PY=${PYTHON:-python3}
PORT=${PORT:-8080}
HEALTH_URL=${HEALTH_URL:-http://127.0.0.1:${PORT}/api/health}
SMOKE_URL=${SMOKE_URL:-http://127.0.0.1:${PORT}/api/overview}
RELEASE_ROOT=${RELEASE_ROOT:-$APP_DIR/.releases}
KEEP_RELEASES=${KEEP_RELEASES:-5}
HEALTH_TRIES=${HEALTH_TRIES:-40}
CODE_DIRS=(backend web scripts deploy)
MARKER=$APP_DIR/.deployed-release

REF=main
DRY=0
DO_TESTS=1
DO_RESTART=1
ASSUME_YES=0
MODE=deploy
ROLLBACK_ID=""

log()  { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }
warn() { printf '[%s] WARN: %s\n' "$(date '+%H:%M:%S')" "$*" >&2; }
die()  { printf '[%s] ERROR: %s\n' "$(date '+%H:%M:%S')" "$1" >&2; exit "${2:-2}"; }
run()  { if [ "$DRY" -eq 1 ]; then printf '  [dry-run] %s\n' "$*"; else "$@"; fi; }

while [ $# -gt 0 ]; do
  case "$1" in
    --ref)        REF=${2:-}; shift 2 ;;
    --dry-run)    DRY=1; shift ;;
    --no-tests)   DO_TESTS=0; shift ;;
    --no-restart) DO_RESTART=0; shift ;;
    --yes|-y)     ASSUME_YES=1; shift ;;
    --rollback)   MODE=rollback; ROLLBACK_ID=${2:-latest}; shift 2 ;;
    --list)       MODE=list; shift ;;
    -h|--help)    sed -n '2,28p' "$0"; exit 0 ;;
    *)            die "未知参数: $1（--help 看用法）" ;;
  esac
done

# ---------- 前置检查 ----------
[ -d "$APP_DIR" ] || die "找不到 $APP_DIR —— 本脚本必须在服务器上、对已部署的副本执行"
[ -d "$APP_DIR/.git" ] || die "$APP_DIR 不是 git 仓库，无法按提交取代码"
for c in git tar rsync curl; do command -v "$c" >/dev/null 2>&1 || die "缺少命令: $c"; done
command -v "$PY" >/dev/null 2>&1 || die "缺少 $PY"

# --list 必须只读，不建目录；部署/回滚才需要 RELEASE_ROOT
if [ "$DRY" -eq 0 ] && [ "$MODE" != list ]; then
  mkdir -p "$RELEASE_ROOT" || die "无法创建 $RELEASE_ROOT"
fi

# ---------- --list ----------
if [ "$MODE" = list ]; then
  log "可回滚的发布点（$RELEASE_ROOT）："
  if compgen -G "$RELEASE_ROOT/*/code.tar.gz" >/dev/null; then
    while IFS= read -r d; do
      meta="$RELEASE_ROOT/$d/meta.env"
      commit="?"; ref="?"
      if [ -f "$meta" ]; then
        # 不 source：避免执行 .releases 里的内容，也避开 shellcheck SC1090
        commit=$(grep -m1 '^COMMIT=' "$meta" | cut -d= -f2-)
        ref=$(grep -m1 '^REF=' "$meta" | cut -d= -f2-)
        [ -n "$commit" ] || commit="?"
        [ -n "$ref" ] || ref="?"
      fi
      printf '  %s  commit=%s  ref=%s\n' "$d" "$commit" "$ref"
    done < <(ls -1 "$RELEASE_ROOT" | grep -E '^[0-9]{8}-[0-9]{6}$' | sort -r)
  else
    log "  （暂无发布点，第一次成功发布会生成）"
  fi
  exit 0
fi

# ---------- 回滚 ----------
restore_and_restart() {
  local tarball=$1
  log "回滚：还原代码…"
  if ! run tar -xzf "$tarball" -C "$APP_DIR"; then
    warn "回滚：还原代码失败"
    return 1
  fi
  log "回滚：代码已还原到发布前（data/ 未动）"
  [ "$DO_RESTART" -eq 1 ] || return 0
  if ! run sudo -n systemctl restart "$SERVICE"; then
    warn "回滚：代码已还原，但重启失败——需人工执行 sudo systemctl restart $SERVICE"
    return 2
  fi
  if ! wait_healthy; then
    warn "回滚：代码已还原并已重启，但健康检查未通过——需人工排查（journalctl -u $SERVICE -n 100）"
    return 3
  fi
  log "回滚完成：代码已还原，服务健康"
  return 0
}

# 回滚后统一报结论：代码到底还原了没有，服务到底是死是活，分开说清楚。
report_rollback() {
  local rc=$1 why=$2
  case "$rc" in
    0) die "$why；已回滚到发布前状态，服务健康" 1 ;;
    1) die "$why；且回滚还原代码失败，需立即人工介入" 1 ;;
    *) die "$why；代码已回滚到发布前，但服务需人工确认（systemctl status $SERVICE）" 1 ;;
  esac
}

wait_healthy() {
  local i
  for i in $(seq 1 "$HEALTH_TRIES"); do
    if curl -fsS --max-time 3 "$HEALTH_URL" 2>/dev/null | grep -q '"status"[[:space:]]*:[[:space:]]*"ok"'; then
      log "健康检查通过（第 ${i}s）：$HEALTH_URL"
      return 0
    fi
    sleep 1
  done
  warn "健康检查超时（${HEALTH_TRIES}s）：$HEALTH_URL"
  return 1
}

smoke_check() {
  local body
  body=$(curl -fsS --max-time 10 "$SMOKE_URL" 2>/dev/null) || { warn "冒烟失败：$SMOKE_URL 不可访问"; return 1; }
  # 只断言结构（有当期周、有城市数组），不断言具体数值——数值依赖数据，不该卡发布
  printf '%s' "$body" | grep -q '"week"'      || { warn "冒烟失败：/api/overview 无 week 字段"; return 1; }
  printf '%s' "$body" | grep -q '"cities"'    || { warn "冒烟失败：/api/overview 无 cities 字段"; return 1; }
  printf '%s' "$body" | grep -qE '"cities"[[:space:]]*:[[:space:]]*\[[[:space:]]*\{' \
    || { warn "冒烟失败：/api/overview cities 为空（库可能没数据或指标未重算）"; return 1; }
  log "冒烟检查通过：/api/overview 有 week 且有城市数据"
  return 0
}

if [ "$MODE" = rollback ]; then
  if [ "$ROLLBACK_ID" = latest ]; then
    ROLLBACK_ID=$(ls -1 "$RELEASE_ROOT" 2>/dev/null | grep -E '^[0-9]{8}-[0-9]{6}$' | sort -r | head -1)
    [ -n "$ROLLBACK_ID" ] || die "没有可回滚的发布点"
  fi
  TARBALL="$RELEASE_ROOT/$ROLLBACK_ID/code.tar.gz"
  [ -f "$TARBALL" ] || die "找不到回滚包：$TARBALL（--list 看可用发布点）"
  log "回滚到发布点 $ROLLBACK_ID（$TARBALL）"
  if [ "$ASSUME_YES" -eq 0 ] && [ "$DRY" -eq 0 ]; then
    printf '确认回滚 %s 的代码？[y/N] ' "$APP_DIR"
    read -r ans
    case "$ans" in y|Y) : ;; *) log "已取消（未改动任何文件）"; exit 2 ;; esac
  fi
  restore_and_restart "$TARBALL"
  rc=$?
  if [ "$rc" -eq 0 ] && [ "$DRY" -eq 0 ]; then
    printf 'ROLLBACK_TO=%s\n' "$ROLLBACK_ID" > "$MARKER"
  fi
  log "回滚结束：$ROLLBACK_ID"
  exit "$([ "$rc" -eq 0 ] && echo 0 || echo 1)"
fi

# ---------- 发布 ----------
log "发布：$APP_DIR  目标 ref=$REF  dry-run=$DRY"

PREV_HEAD=$(git -C "$APP_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)
log "当前运行副本 git HEAD=$PREV_HEAD（注意：历史发布用「拷文件」做过，HEAD 可能落后于实际内容，见 docs/发布与回滚.md）"

if [ "$DRY" -eq 0 ]; then
  log "拉取远端引用…"
  # 这台服务器到 GitHub 的连接不稳定：实测单次 fetch 会卡 90s 超时，紧接着重试 2s 就成功。
  # 所以重试 3 次；仍失败则不直接中止，改用本地已有引用（解析不到目标提交才报错）。
  FETCH_OK=0
  for attempt in 1 2 3; do
    if FETCH_OUT=$(timeout "${FETCH_TIMEOUT:-45}" git -C "$APP_DIR" \
                     -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=30 \
                     fetch --prune origin 2>&1); then
      FETCH_OK=1
      break
    fi
    warn "第 $attempt 次 git fetch 失败，重试…"
    [ -n "${FETCH_OUT:-}" ] && printf '%s\n' "$FETCH_OUT" | tail -3 >&2
    sleep 2
  done
  if [ "$FETCH_OK" -eq 0 ]; then
    warn "git fetch 连续失败，改试本地已有引用（解析不到目标提交则中止）"
    FETCH_FAILED=1
  fi
else
  log "dry-run：不 fetch，origin/* 可能不是最新"
fi

# 解析目标提交：优先 origin/<ref>。服务器本地分支常落后（历史拷贝式发布导致），
# 所以 --ref main 必须指「远端 main」，而不是服务器上那个陈旧的本地 main。
resolve_target() {
  local r=$1
  git -C "$APP_DIR" rev-parse --verify --quiet "origin/${r}^{commit}" && return 0
  git -C "$APP_DIR" rev-parse --verify --quiet "${r}^{commit}" && return 0
  return 1
}
TARGET=$(resolve_target "$REF")
[ -n "${TARGET:-}" ] || die "解析不到 ref：$REF（试过 origin/$REF 和 $REF${FETCH_FAILED:+；且上一步 git fetch 失败}）"
TARGET_SHORT=$(printf '%s' "$TARGET" | cut -c1-7)
TARGET_SUBJECT=$(git -C "$APP_DIR" log -1 --format='%s' "$TARGET" 2>/dev/null)
log "目标提交：$TARGET_SHORT  $TARGET_SUBJECT"

# 幂等：同一个提交已经发布过 → 不重复搬文件，只做健康检查
if [ -f "$MARKER" ] && grep -q "COMMIT=$TARGET" "$MARKER" 2>/dev/null; then
  log "代码已是 $TARGET_SHORT（幂等跳过同步）"
  if [ "$DO_RESTART" -eq 1 ]; then
    if wait_healthy && smoke_check; then log "服务健康，无需操作"; exit 0; fi
    warn "代码是最新的，但服务不健康 —— 尝试重启一次"
    run sudo -n systemctl restart "$SERVICE"
    if wait_healthy && smoke_check; then log "重启后恢复"; exit 0; fi
    die "服务仍不健康，需人工排查（journalctl -u $SERVICE -n 100）" 1
  fi
  exit 0
fi

STAGE=$(mktemp -d /tmp/shikar-release.XXXXXX) || die "无法创建临时目录"
trap 'rm -rf "$STAGE"' EXIT

log "导出目标提交到暂存区…"
if [ "$DRY" -eq 0 ]; then
  git -C "$APP_DIR" archive "$TARGET" | tar -x -C "$STAGE" || die "git archive 失败"
  [ -f "$STAGE/backend/shikar/main.py" ] || die "导出内容不完整（缺 backend/shikar/main.py）"
fi

RELEASE_ID=$(date '+%Y%m%d-%H%M%S')
REL_DIR=$RELEASE_ROOT/$RELEASE_ID

# 备份将被覆盖的代码目录（不含 data/、.secrets/）
log "备份现有代码到 $REL_DIR/code.tar.gz"
if [ "$DRY" -eq 0 ]; then
  mkdir -p "$REL_DIR" || die "无法创建 $REL_DIR"
  existing=()
  for d in "${CODE_DIRS[@]}"; do [ -d "$APP_DIR/$d" ] && existing+=("$d"); done
  tar -czf "$REL_DIR/code.tar.gz" -C "$APP_DIR" \
      --exclude='__pycache__' --exclude='*.pyc' "${existing[@]}" \
    || die "代码备份失败（中止，什么都没改）"
  {
    printf 'RELEASE_ID=%s\n' "$RELEASE_ID"
    printf 'COMMIT=%s\n' "$TARGET"
    printf 'REF=%s\n' "$REF"
    printf 'PREV_HEAD=%s\n' "$PREV_HEAD"
    printf 'AT=%s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')"
  } > "$REL_DIR/meta.env"
fi

if [ "$ASSUME_YES" -eq 0 ] && [ "$DRY" -eq 0 ]; then
  printf '确认把 %s 发布到 %s 并重启 %s？[y/N] ' "$TARGET_SHORT" "$APP_DIR" "$SERVICE"
  read -r ans
  case "$ans" in y|Y) : ;; *) log "已取消（未改动任何文件）"; exit 2 ;; esac
fi

log "同步代码目录：${CODE_DIRS[*]}（data/ 与 .secrets/ 不动）"
SYNC_FAIL=0
for d in "${CODE_DIRS[@]}"; do
  [ -d "$STAGE/$d" ] || continue
  run mkdir -p "$APP_DIR/$d"
  # --delete 清掉上游已删除的旧文件；排除运行时产物
  if ! run rsync -a --delete \
        --exclude='__pycache__/' --exclude='*.pyc' \
        "$STAGE/$d/" "$APP_DIR/$d/"; then
    SYNC_FAIL=1; break
  fi
done

if [ "$SYNC_FAIL" -eq 1 ]; then
  warn "同步失败，回滚代码"
  restore_and_restart "$REL_DIR/code.tar.gz"
  report_rollback $? "同步代码失败"
fi

# 服务端测试：发布前先证明这套代码在这台机器上跑得过
if [ "$DO_TESTS" -eq 1 ]; then
  log "服务端 pytest…"
  # backend/pytest.ini 的 addopts 已含 -q，这里别再传，否则变 -qq 吞掉汇总行
  PYTEST_ARGS=()
  if ! "$PY" -c "import osmium" >/dev/null 2>&1; then
    PYTEST_ARGS+=(--ignore=tests/test_osm_poi.py)
    warn "服务器未装 pyosmium(osmium)，跳过 tests/test_osm_poi.py（其余照跑，不静默）"
  fi
  if [ "$DRY" -eq 1 ]; then
    printf '  [dry-run] (cd %s/backend && %s -m pytest %s)\n' "$APP_DIR" "$PY" "${PYTEST_ARGS[*]}"
  elif ! ( cd "$APP_DIR/backend" && "$PY" -m pytest "${PYTEST_ARGS[@]}" >"$STAGE/pytest.log" 2>&1 ); then
    warn "pytest 未通过："
    tail -25 "$STAGE/pytest.log" >&2
    restore_and_restart "$REL_DIR/code.tar.gz"
    report_rollback $? "测试未通过（新代码未上线）"
  else
    log "pytest 通过：$(grep -E 'passed|failed|error' "$STAGE/pytest.log" | tail -1)"
  fi
fi

if [ "$DO_RESTART" -eq 0 ]; then
  log "--no-restart：代码已同步，服务未重启（新代码尚未生效）"
  exit 0
fi

log "重启服务 $SERVICE…"
if ! run sudo -n systemctl restart "$SERVICE"; then
  warn "重启命令失败，回滚"
  restore_and_restart "$REL_DIR/code.tar.gz"
  report_rollback $? "重启 $SERVICE 失败"
fi

if ! wait_healthy || ! smoke_check; then
  warn "上线后校验未通过，自动回滚到发布前代码"
  restore_and_restart "$REL_DIR/code.tar.gz"
  report_rollback $? "发布后健康检查/冒烟未通过（发布包留在 $REL_DIR 供排查）"
fi

if [ "$DRY" -eq 0 ]; then
  printf 'COMMIT=%s\nREF=%s\nRELEASE_ID=%s\nAT=%s\n' \
    "$TARGET" "$REF" "$RELEASE_ID" "$(date '+%Y-%m-%dT%H:%M:%S%z')" > "$MARKER"
  printf '%s  deploy  %s -> %s (%s)  ref=%s  by=%s\n' \
    "$(date '+%F %T')" "$PREV_HEAD" "$TARGET_SHORT" "$TARGET_SUBJECT" "$REF" "${SUDO_USER:-${USER:-?}}" \
    >> "$RELEASE_ROOT/history.log"
fi

# 只提醒，不自动删（磁盘只剩 ~8.9G，删备份是人的决定）
COUNT=$(ls -1 "$RELEASE_ROOT" 2>/dev/null | grep -cE '^[0-9]{8}-[0-9]{6}$' || true)
if [ "${COUNT:-0}" -gt "$KEEP_RELEASES" ]; then
  warn "发布点已有 $COUNT 个（阈值 $KEEP_RELEASES），磁盘可用 $(df -h "$APP_DIR" | awk 'NR==2{print $4}')。清理请人工确认后删 $RELEASE_ROOT 下的旧目录。"
fi

log "发布完成：$PREV_HEAD -> $TARGET_SHORT  发布点 $RELEASE_ID"
log "回滚命令：bash deploy/release.sh --rollback $RELEASE_ID"
exit 0
