#!/usr/bin/env bash
# shikar 流程门禁：PR 正文里的「审核三段式」与「独立验证三态」填齐了才允许合并
#
# 依据：docs/19-pstack落地（shikar版）.md §5 三段式审核、docs/08-协作流程与角色.md
#       「验证权独立于开发权」（写代码的人不给自己签字）。
# 为什么要有它：模板里留了栏位，但空着也能合并，CI 绿被当成验证结论 —— 见 2026-11-23 复盘。
#
# 校验两项（缺任一即退出 1，并打印缺哪一项）：
#   1) 「审核（三段式）」三张表（必改 / 建议改 / 可接受风险）都填了实质内容，不是表头或占位符；
#   2) 「独立验证」的验证者字段非空、且不是 PR 作者本人。同一人判定做过身份归一：
#      去括号内容、去空白、小写后取核心名，再比；并拒收以作者名开头的验证者
#      （防「宝拉（代签）」这类后缀绕过）；三态 PASS / PASS+NOTES / FAIL 恰好勾一个，
#      且必须是通过类结论——勾 FAIL 即判不通过（验证说不可合并就不许合并）。
#
# 用法：
#   bash scripts/check_pr_review.sh --pr 47                 # 读 GitHub 上的真实 PR（gh api）
#   bash scripts/check_pr_review.sh --body-file body.md --author BeiFaXianLe   # 本地自测
#   bash scripts/check_pr_review.sh --body-file body.md --author BeiFaXianLe --quiet
#
# 选项：--repo OWNER/REPO（默认 BeiFaXianLe/shikar）· --quiet 只打结论行
# 退出码：0 通过；1 未通过；2 用法/读取错误（同样视为不通过，CI 里就是红）
set -uo pipefail

REPO="BeiFaXianLe/shikar"
PR_NUM=""
BODY_FILE=""
AUTHOR=""
QUIET=0

usage() { sed -n '2,22p' "$0"; }

die() { echo "流程门禁：$*" >&2; exit 2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --pr|--body-file|--author|--repo)
      [ $# -ge 2 ] || die "参数 $1 缺值"
      case "$1" in
        --pr)        PR_NUM="$2" ;;
        --body-file) BODY_FILE="$2" ;;
        --author)    AUTHOR="$2" ;;
        --repo)      REPO="$2" ;;
      esac
      shift 2
      ;;
    --quiet) QUIET=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数: $1（--pr / --body-file / --author / --repo / --quiet）" ;;
  esac
done

TMP_FILES=()
cleanup() { [ ${#TMP_FILES[@]} -eq 0 ] || rm -f "${TMP_FILES[@]}"; }
trap cleanup EXIT

tmp() { local f; f=$(mktemp) || die "无法创建临时文件"; TMP_FILES+=("$f"); printf '%s' "$f"; }

# ---------- 取正文与作者 ----------
if [ -n "$PR_NUM" ]; then
  command -v gh >/dev/null 2>&1 || die "读取 GitHub PR 需要 gh CLI"
  RAW=$(tmp)
  gh api "repos/$REPO/pulls/$PR_NUM" --jq '.body // ""' >"$RAW" \
    || die "读取 PR #$PR_NUM 正文失败（gh api）"
  [ -n "$AUTHOR" ] || AUTHOR=$(gh api "repos/$REPO/pulls/$PR_NUM" --jq '.user.login // ""') \
    || die "读取 PR #$PR_NUM 作者失败（gh api）"
elif [ -n "$BODY_FILE" ]; then
  [ -f "$BODY_FILE" ] || die "找不到正文文件: $BODY_FILE"
  RAW="$BODY_FILE"
else
  usage >&2
  die "需要 --pr 或 --body-file"
fi

BODY=$(tmp)
tr -d '\r' <"$RAW" >"$BODY" || die "读取正文失败"

SEC_REVIEW=$(tmp)
SEC_VERIFY=$(tmp)
# 只保留目标小节的内容（到下一个 `## ` 为止）
awk '/^##[ \t]/{ in_sec = ($0 ~ /审核/); next } in_sec { print }' "$BODY" >"$SEC_REVIEW"
awk '/^##[ \t]/{ in_sec = ($0 ~ /独立验证/); next } in_sec { print }' "$BODY" >"$SEC_VERIFY"

# ---------- 工具函数 ----------
# 数一张表里有几行是「实质内容」（跳过表头与分隔行、跳过纯占位符单元格）
count_rows() {
  awk '
    function is_sep(s) { return (s ~ /^[ \t]*\|[ \t:|-]*-[ \t:|-]*$/) }
    function cell_ok(c,   t) {
      t = c
      gsub(/^[ \t]+/, "", t); gsub(/[ \t]+$/, "", t)
      if (t == "") return 0
      if (t ~ /^<[^>]*>$/) return 0            # <名字> <待填> 这类模板占位
      if (tolower(t) == "tbd" || tolower(t) == "todo") return 0
      if (t == "待填" || t == "待定" || t == "...") return 0
      if (t ~ /^[-—–.。…、 ]+$/) return 0        # - / — / … 这类空位
      return 1
    }
    { if ($0 !~ /^[ \t]*\|/) next
      if (is_sep($0)) { if (n > 0) n--; next }   # 分隔行前那一行是表头，一起丢掉
      rows[++n] = $0 }
    END { cnt = 0
      for (i = 1; i <= n; i++) {
        s = rows[i]
        sub(/^[ \t]*\|/, "", s); sub(/\|[ \t]*$/, "", s)
        m = split(s, cells, "|")
        for (j = 1; j <= m; j++) if (cell_ok(cells[j])) { cnt++; break }
      }
      print cnt + 0 }
  '
}

# 取某个 `### ` 小节的内容
subsection() {
  awk -v pat="$1" '/^#{2,3}[ \t]/{ in_sub = ($0 ~ pat); next } in_sub { print }' "$SEC_REVIEW"
}

FAILS=()
fails() { FAILS+=("$1"); }
say() { [ "$QUIET" -eq 1 ] || printf '%s\n' "$*"; }

[ "$QUIET" -eq 1 ] || {
  label=""
  [ -n "$PR_NUM" ] && label="PR #$PR_NUM（$REPO） · "
  printf '流程门禁 · %s作者 %s\n' "$label" "${AUTHOR:-<未知>}"
}

# ---------- 校验 1：审核三段式 ----------
if [ ! -s "$SEC_REVIEW" ]; then
  say "FAIL  审核三段式：PR 正文里没有「## 审核（三段式，见 docs/19 §5）」小节"
  fails "审核三段式小节缺失"
else
  for pair in '必改（P0/P1）|必改' '建议改（P2）|建议改' '可接受风险（P3 或明示豁免）|可接受风险'; do
    title=${pair%%|*}
    pat=${pair##*|}
    n=$(subsection "$pat" | count_rows)
    if [ "${n:-0}" -ge 1 ]; then
      say "PASS  审核三段式·$title（$n 行实质内容）"
    else
      say "FAIL  审核三段式·$title：表格是空的或只有占位符（写「无」也要写）"
      fails "审核三段式·$title 未填"
    fi
  done
fi

# ---------- 校验 2：独立验证 ----------
# 去掉成对括号（全角/半角）及其内容：只留核心名，防「宝拉（代签）」这类后缀绕过。
# 用 bash 的 ${s%%（*} / ${s#*）} 按字面量切，不用 sed 的字符集——多字节字符进字符集会被按字节拆。
strip_parens() {
  local s="$1" head rest
  while [[ $s == *"（"* ]]; do
    head="${s%%（*}"; rest="${s#*（}"
    if [[ $rest == *"）"* ]]; then s="${head}${rest#*）}"; else s="$head"; fi   # 括号没闭合：从「（」起砍掉
  done
  while [[ $s == *"("* ]]; do
    head="${s%%(*}"; rest="${s#*(}"
    if [[ $rest == *")"* ]]; then s="${head}${rest#*)}"; else s="$head"; fi
  done
  printf '%s' "$s"
}

# 身份归一：去括号内容 → 去空白（含全角空格）→ 小写
normalize_name() {
  local s
  s=$(strip_parens "$1")
  s=${s//$(printf '\u3000')/}
  s=$(printf '%s' "$s" | tr -d '[:space:]')
  printf '%s' "$s" | tr '[:upper:]' '[:lower:]'
}

# $1 是否以 $2 开头（$2 为空时不算）
starts_with() { [ -n "$2" ] && [ "${1#"$2"}" != "$1" ]; }

if [ ! -s "$SEC_VERIFY" ]; then
  say "FAIL  独立验证：PR 正文里没有「## 独立验证」小节"
  fails "独立验证小节缺失"
else
  # 2a 验证者字段（用 sed 逐个剥字符，避开多字节字符放进 bash 字符集时的按字节切分）
  verifier=$(grep -m1 -E '^[ \t]*验证者' "$SEC_VERIFY" || true)
  verifier=${verifier#*验证者}
  verifier=$(printf '%s' "$verifier" | sed -e 's/^[[:space:]]*//' -e 's/^://' -e 's/^：//' \
      -e 's/·.*$//' -e 's/|.*$//' -e 's/[[:space:]]*$//')
  case "$verifier" in
    ""|"<名字>"|"待填"|"待定"|"-"|"—"|"...")
      say "FAIL  独立验证·验证者：字段为空或仍是占位符（$verifier）"
      fails "独立验证·验证者为空或占位符"
      ;;
    *)
      v_norm=$(normalize_name "$verifier")
      a_norm=$(normalize_name "$AUTHOR")
      if [ -z "$v_norm" ]; then
        say "FAIL  独立验证·验证者：去掉括号/空白后是空的（$verifier）"
        fails "独立验证·验证者为空或占位符"
      elif [ -n "$a_norm" ] && { [ "$v_norm" = "$a_norm" ] || starts_with "$v_norm" "$a_norm"; }; then
        if [ "$verifier" != "$AUTHOR" ]; then
          say "FAIL  独立验证·验证者：$verifier 与作者 $AUTHOR 是同一个人（归一后「$v_norm」），作者自己签不算"
        else
          say "FAIL  独立验证·验证者：$verifier 就是 PR 作者，作者自己签不算"
        fi
        fails "独立验证·验证者与作者相同（$verifier）"
      else
        say "PASS  独立验证·验证者：$verifier（作者 $AUTHOR，非同一人）"
      fi
      ;;
  esac

  # 2b 三态：恰好勾一个，且必须是「通过类」结论（PASS / PASS+NOTES）；勾 FAIL 即拦下
  STATES=$(tmp)
  awk '{ s = $0; gsub(/`/, "", s)
         if (match(s, /\[[xX]\][ \t]*(PASS\+NOTES|PASS|FAIL)/)) {
           t = substr(s, RSTART, RLENGTH); sub(/^\[[xX]\][ \t]*/, "", t)
           printf "%s\t%s\n", t, s } }' "$SEC_VERIFY" >"$STATES"
  n_state=$(grep -c . "$STATES" || true)
  if [ "${n_state:-0}" -eq 0 ]; then
    say "FAIL  独立验证·三态：PASS / PASS+NOTES / FAIL 一个都没勾"
    fails "独立验证·三态未勾选"
  elif [ "${n_state:-0}" -gt 1 ]; then
    say "FAIL  独立验证·三态：勾了 $n_state 个（$(cut -f1 "$STATES" | tr '\n' ' ')），只允许一个"
    fails "独立验证·三态勾了多个"
  else
    state=$(cut -f1 "$STATES")
    rawline=$(cut -f2- "$STATES")
    case "$state" in
      PASS|PASS+NOTES)
        say "PASS  独立验证·三态：$state"
        ;;
      FAIL)
        # 勾 FAIL 就是把「不许合并」写进正文：门禁必须跟着红，并把阻断项带出来
        block=""
        if [[ $rawline == *阻断项* ]]; then
          # 多字节字符不放进 sed 的字符集（会被按字节拆成乱码），只用字面量分两次剥
          block=$(printf '%s' "$rawline" | sed -e 's/.*阻断项：//' -e 's/.*阻断项://' \
              -e 's/）.*$//' -e 's/).*$//' -e 's/[[:space:]]*$//')
        fi
        say "FAIL  独立验证·三态：FAIL —— 验证结论为 FAIL，不得合并${block:+（阻断项：$block）}"
        fails "独立验证·结论为 FAIL，不得合并"
        ;;
      *)
        say "FAIL  独立验证·三态：识别不出结论（$state）"
        fails "独立验证·三态识别失败"
        ;;
    esac
  fi
fi

# ---------- 结论 ----------
say ""
if [ ${#FAILS[@]} -eq 0 ]; then
  echo "流程门禁 PASS"
  exit 0
fi
echo "流程门禁 FAIL（${#FAILS[@]} 项）"
for f in "${FAILS[@]}"; do echo "  缺: $f"; done
echo "  依据: docs/19 §5（三段式审核）+ docs/08「验证权独立于开发权」"
echo "  填完 PR 正文后本检查会自动重跑（pull_request: edited）"
exit 1
