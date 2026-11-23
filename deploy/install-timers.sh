#!/usr/bin/env bash
# shikar 定时任务单元：安装 / 卸载 / 查看（幂等）
#
# 跑在服务器上（ubuntu@192.144.183.46，部署目录 /opt/shikar）。不在 systemd 主机上会直接退出。
#
# 管两套单元，源文件在 deploy/systemd/（那里是唯一事实来源）：
#   shikar-refresh.timer + shikar-refresh.service   每周一 04:30 抓数 + 重建库
#   shikar-release.timer + shikar-release.service   每天 05:20 把代码对齐到 origin/main
#
# 用法：
#   bash deploy/install-timers.sh status            # 只读：是否安装、与仓库是否一致、下次触发、最近日志
#   bash deploy/install-timers.sh diff              # 只读：/etc 与仓库单元的逐行差异
#   bash deploy/install-timers.sh install --dry-run # 只打印将做什么，不改任何文件
#   bash deploy/install-timers.sh install           # 安装/更新（内容一致则跳过写入，仅校验启用状态）
#   bash deploy/install-timers.sh install --force   # /etc 里有人手改过时，强制用仓库版覆盖（会留 .bak）
#   bash deploy/install-timers.sh uninstall         # 停用并删除 4 个单元（保留已有 .bak-*，不动数据）
#
# 写死的安全约束（别改）：
#   1. 只写 $UNIT_DIR 下这 4 个单元文件；**永不** rm -rf，**永不**碰
#      $APP_DIR/data（不可再生数据）、$APP_DIR/.secrets（token）、$APP_DIR/.releases、
#      $APP_DIR/data/backups（回滚与容灾都要用）。
#   2. **永不** `systemctl start shikar-refresh.service`：手动采集会向不可再生的
#      data/fang_preview_all.csv 追加数据，属不可逆动作，要人点头
#      （docs/发布与回滚.md §5）。本脚本只装/卸单元，不代跑任务。
#   3. /etc 里的单元与仓库不同且没给 --force：直接退出，不静默覆盖人工改动。
#   4. 覆盖前把原文件留成 <name>.bak-YYYYmmdd-HHMMSS。
#
# 退出码：0 成功；1 执行失败；2 参数或前置条件问题
set -uo pipefail

APP_DIR=${APP_DIR:-/opt/shikar}
UNIT_DIR=${UNIT_DIR:-/etc/systemd/system}
SRC_DIR=$APP_DIR/deploy/systemd

UNITS=(shikar-refresh.timer shikar-refresh.service shikar-release.timer shikar-release.service)
TIMERS=(shikar-refresh.timer shikar-release.timer)

TS=$(date '+%Y%m%d-%H%M%S')
DRY=0
FORCE=0
MODE=""

log()  { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }
warn() { printf '[%s] WARN: %s\n' "$(date '+%H:%M:%S')" "$*" >&2; }
die()  { printf '[%s] ERROR: %s\n' "$(date '+%H:%M:%S')" "$1" >&2; exit "${2:-2}"; }
run()  { if [ "$DRY" -eq 1 ]; then printf '  [dry-run] %s\n' "$*"; else "$@"; fi; }

while [ $# -gt 0 ]; do
  case "$1" in
    status|diff|install|uninstall) MODE=$1; shift ;;
    --dry-run) DRY=1; shift ;;
    --force)   FORCE=1; shift ;;
    -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
    *)         die "未知参数: $1（--help 看用法）" ;;
  esac
done

[ -n "$MODE" ] || { sed -n '2,28p' "$0"; exit 2; }
[ "$FORCE" -eq 1 ] && [ "$MODE" != install ] && die "--force 只用于 install"

# ---------- 前置检查 ----------
need_systemd() {
  command -v systemctl >/dev/null 2>&1 || die "找不到 systemctl —— 本脚本只用于 systemd 主机（部署目录 $APP_DIR）"
  [ -d /run/systemd/system ] || die "这不是 systemd 正在运行的主机 —— 安装/卸载必须在服务器上做"
}

need_local() {
  [ -d "$SRC_DIR" ] || die "找不到单元源目录 $SRC_DIR —— 本脚本要在已部署的副本里跑"
  local u
  for u in "${UNITS[@]}"; do
    [ -f "$SRC_DIR/$u" ] || die "缺少单元源文件：$SRC_DIR/$u"
  done
  [ -x "$APP_DIR/deploy/refresh.sh" ] || die "$APP_DIR/deploy/refresh.sh 不存在或不可执行（单元会起不来）"
  [ -x "$APP_DIR/deploy/release.sh" ] || die "$APP_DIR/deploy/release.sh 不存在或不可执行（每日对齐会失败）"
}

need_sudo() {
  sudo -n true 2>/dev/null || die "sudo -n 不可用（需要 NOPASSWD）—— 安装/卸载无法进行"
}

# ---------- status（只读） ----------
print_status() {
  log "部署目录 $APP_DIR · 单元目录 $UNIT_DIR"
  local tz
  tz=$(timedatectl show -p Timezone --value 2>/dev/null || true)
  [ -n "$tz" ] || tz=$(cat /etc/timezone 2>/dev/null || echo unknown)
  if [ "$tz" = "Asia/Shanghai" ]; then
    log "时区 $tz（OnCalendar 按此时区解释）"
  else
    warn "时区是 $tz，不是 Asia/Shanghai —— OnCalendar 会按该时区解释，抓数/对齐时间整体偏移"
  fi

  local u
  for u in "${UNITS[@]}"; do
    if [ ! -f "$UNIT_DIR/$u" ]; then
      printf '  %-26s 未安装\n' "$u"
    elif diff -q "$SRC_DIR/$u" "$UNIT_DIR/$u" >/dev/null 2>&1; then
      printf '  %-26s 已安装 · 与仓库一致\n' "$u"
    else
      printf '  %-26s 已安装 · 与仓库不同（diff 看清后用 install --force 覆盖）\n' "$u"
    fi
  done

  local t
  for t in "${TIMERS[@]}"; do
    log "  $t: $(systemctl is-enabled "$t" 2>&1) / $(systemctl is-active "$t" 2>&1)"
  done
  log "下次触发（NEXT 含随机延迟，别当配置值；抓数配置值是 Mon 04:30，对齐是每天 05:20）："
  systemctl list-timers --all --no-pager 2>/dev/null \
    | awk 'NR==1 || /shikar-(refresh|release)\.timer/' | sed 's/^/  /'

  log "最近一次抓数（journal 尾部 15 行）："
  journalctl -u shikar-refresh.service -n 15 --no-pager 2>/dev/null | sed 's/^/  /' || warn "读 journal 失败"
  log "最近一次对齐（journal 尾部 8 行）："
  journalctl -u shikar-release.service -n 8 --no-pager 2>/dev/null | sed 's/^/  /' || warn "读 journal 失败"

  log "数据新鲜度自查（只读，两条任选）："
  log "  sqlite3 $APP_DIR/data/shikar.db \"select max(week), count(*) from xiaoqu_metric_weekly where week like '20%';\""
  log "  期望 max(week) 不早于本周（周一抓数后前进一期）"
}

# ---------- diff（只读） ----------
print_diff() {
  local u
  for u in "${UNITS[@]}"; do
    if [ ! -f "$UNIT_DIR/$u" ]; then
      printf '=== %s：未安装 ===\n' "$u"
    elif diff -q "$SRC_DIR/$u" "$UNIT_DIR/$u" >/dev/null 2>&1; then
      printf '=== %s：一致 ===\n' "$u"
    else
      printf '=== %s：不同（左=/etc 现状，右=仓库） ===\n' "$u"
      diff -u "$UNIT_DIR/$u" "$SRC_DIR/$u" 2>&1 \
        | sed -e "s|^--- .*|--- $UNIT_DIR|" -e 's|^+++ .*|+++ deploy/systemd|'
    fi
  done
}

# ---------- install ----------
do_install() {
  need_sudo
  local changed=0 u src dst
  for u in "${UNITS[@]}"; do
    src=$SRC_DIR/$u
    dst=$UNIT_DIR/$u
    if [ -f "$dst" ] && diff -q "$src" "$dst" >/dev/null 2>&1; then
      log "$u 已是仓库版本，跳过写入"
      continue
    fi
    if [ -f "$dst" ] && [ "$FORCE" -eq 0 ]; then
      die "$dst 与仓库不同（可能有人手改过）。先跑 diff 看清差异，确认要覆盖再加 --force（会留 .bak-$TS）"
    fi
    if [ -f "$dst" ]; then
      log "备份 $dst -> $dst.bak-$TS"
      run sudo cp -p "$dst" "$dst.bak-$TS" || die "备份失败，中止（什么都没改）"
    fi
    log "写入 $dst"
    run sudo install -m 0644 -o root -g root "$src" "$dst" || die "写入失败：$dst"
    changed=1
  done

  if [ "$changed" -eq 1 ]; then
    log "systemctl daemon-reload"
    run sudo systemctl daemon-reload || die "daemon-reload 失败"
  else
    log "4 个单元均与仓库一致，不重写（仍校验启用状态）"
  fi

  local t
  for t in "${TIMERS[@]}"; do
    run sudo systemctl enable --now "$t" || die "enable --now $t 失败"
  done

  if [ "$DRY" -eq 1 ]; then
    log "dry-run 结束：未改动任何文件"
    return 0
  fi

  for t in "${TIMERS[@]}"; do
    [ "$(systemctl is-enabled "$t" 2>/dev/null)" = enabled ] || die "$t 未能 enabled"
    [ "$(systemctl is-active "$t" 2>/dev/null)" = active ] || die "$t 未能 active"
  done
  log "安装完成，下次触发："
  systemctl list-timers --all --no-pager | awk '/shikar-(refresh|release)\.timer/' | sed 's/^/  /'
  log "抓数的 NEXT 是 04:30 + 随机延迟；配置值见 deploy/systemd/shikar-refresh.timer"
}

# ---------- uninstall ----------
do_uninstall() {
  need_sudo
  local t u
  for t in "${TIMERS[@]}"; do
    log "停用 $t"
    run sudo systemctl disable --now "$t" || warn "disable --now $t 失败（可能本就没装）"
  done
  for u in "${UNITS[@]}"; do
    if [ -f "$UNIT_DIR/$u" ]; then
      log "删除 $UNIT_DIR/$u（保留已有的 .bak-*）"
      run sudo rm -f "$UNIT_DIR/$u" || die "删除失败：$UNIT_DIR/$u"
    else
      log "$u 未安装"
    fi
  done
  run sudo systemctl daemon-reload || warn "daemon-reload 失败，需人工执行"
  if [ "$DRY" -eq 1 ]; then
    log "dry-run 结束：未改动任何文件"
    return 0
  fi
  log "已卸载。以下内容**没有**被改动："
  log "  $APP_DIR/data        （不可再生数据：fang_preview_all.csv / shikar.db / backups）"
  log "  $APP_DIR/.secrets    （gh-token / baidu_ak）"
  log "  $APP_DIR/.releases   （发布点，回滚还要用）"
  log "重装：bash $APP_DIR/deploy/install-timers.sh install"
}

# ---------- 分发 ----------
need_systemd
need_local

case "$MODE" in
  status)    print_status ;;
  diff)      print_diff ;;
  install)   do_install ;;
  uninstall) do_uninstall ;;
  *)         die "未知子命令: $MODE" ;;
esac
exit 0
