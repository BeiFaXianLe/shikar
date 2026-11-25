#!/usr/bin/env python3
"""审核报告门禁：审核范围报告四栏填实、范围清单覆盖 diff、结论三选一。

用法：

    python scripts/review_check.py --base origin/main      # CI / 本地：校验本次 diff 带上的报告
    python scripts/review_check.py --report docs/review/pr-57.md --base origin/main
    python scripts/review_check.py --self-test             # 门禁自检：能过，也能拦

校验四件事（缺任一即退出 1）：
  1. 本次 diff 里必须有 docs/review/pr-*.md；
  2. 四栏（范围 / 影响面依据 / 跑了哪些测试 / 结论）都填了实质内容。
     判定是正向的：范围理由至少 6 个文字/数字，影响面至少 40 字且写到调用方/引用，
     测试栏必须同时有命令行与真实结果（passed / failed / PASS / exit=…）——只有一条命令不算跑过；
  3. 范围栏的文件清单必须覆盖本次 diff 的每个文件（报告自身除外，**删除的文件也算**），少一个就红；
  4. 结论写 PASS / PASS+NOTES 才算绿；写 FAIL 同样判红——审核判为不可合并时不允许合并。

退出码：0 通过；1 未通过；2 用法/读取错误（CI 里同样算红）。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile

try:  # Windows 控制台默认不是 UTF-8，中文输出会花；CI 无影响
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT_GLOB = re.compile(r"^docs/review/pr-.*\.md$")
VERDICTS = ("PASS", "PASS+NOTES", "FAIL")
# 正向判定的下限：少于这些就不算「填实」。
MIN_REASON = 6  # 范围理由至少几个文字/数字
MIN_IMPACT = 40  # 影响面依据至少几个文字/数字

EMPTY_WORDS = {
    "",
    "-",
    "—",
    "–",
    "...",
    "…",
    "无",
    "同上",
    "n/a",
    "na",
    "tbd",
    "todo",
    "待填",
    "待定",
}
SELF_TEST_DIFF = [
    "scripts/review.sh",
    "scripts/review/report.py",
    "scripts/review_check.py",
    "docs/review/README.md",
]


def git(*args: str, cwd: str | None = None) -> str:
    p = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=cwd or ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if p.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{p.stderr.strip()}")
    return p.stdout


def diff_files(base: str, cwd: str | None = None) -> list[str]:
    # ACMRD：删除的文件同样要审（删掉鉴权/事务代码是最危险的改法），不能从范围里漏掉。
    raw = git("diff", "--name-only", "--diff-filter=ACMRD", f"{base}...HEAD", cwd=cwd)
    return [p for p in raw.splitlines() if p.strip()]


def strip_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


WORD = re.compile(r"[0-9A-Za-z_\u4e00-\u9fff]")


def word_len(s: str) -> int:
    """文字/数字的个数：纯符号、纯空格、标点都不算。"""
    return len(WORD.findall(s))


def clean_cell(cell: str) -> str:
    s = re.sub(r"<[^<>]*>", "", cell)
    return s.replace("`", "").replace("*", "").strip(" \t|。．.,，、:：")


def substantive(cell: str) -> bool:
    """去掉 <占位符>、反引号、markdown 修饰后，至少还剩 2 个文字/数字。"""
    core = clean_cell(cell)
    return core.lower() not in EMPTY_WORDS and word_len(core) >= 2


def reason_ok(cell: str) -> bool:
    """范围理由：至少 MIN_REASON 个文字/数字，「见上」「本次改动」这类不够。"""
    core = clean_cell(cell)
    return core.lower() not in EMPTY_WORDS and word_len(core) >= MIN_REASON


# 影响面依据要写到「谁被改到、谁在调」这一层，光说「按 diff 判断」不算。
IMPACT_MARKERS = re.compile(r"调用方|调用链|引用|file:line|符号|接缝|判断结论|:\d+|无残留")


def impact_ok(body: str) -> bool:
    text = "\n".join(substantive_lines(body))
    return word_len(text) >= MIN_IMPACT and bool(IMPACT_MARKERS.search(text))


CODE_BLOCK = re.compile(r"```[^\n]*\n(.*?)```", re.S)
CMD_LINE = re.compile(r"^\s*[$>]\s*\S|\b(?:pytest|python3?|bash|sh|npm|node|shellcheck|git|make)\b")
RESULT_LINE = re.compile(
    r"(?i)\bpassed\b|\bfailed\b|\berror\b|\bok\b|\bpass\b|\bfail\b|exit\s*[=:]\s*\d+|用时|耗时"
)


def tests_ok(body: str) -> bool:
    """测试栏：必须有一个代码块同时含命令行与结果行 —— 只有一条命令等于没跑。"""
    for block in CODE_BLOCK.findall(strip_comments(body)):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        has_cmd = any(CMD_LINE.search(ln) for ln in lines)
        has_out = any(RESULT_LINE.search(ln) for ln in lines if not CMD_LINE.search(ln))
        if has_cmd and has_out:
            return True
    return False


def substantive_lines(body: str) -> list[str]:
    """小节里真有内容的行：跳过标题、表格分隔行与全空的表格行。"""
    out = []
    for line in strip_comments(body).splitlines():
        s = line.strip()
        if not s or s.startswith("<!--") or s.startswith("-->"):
            continue
        if s.startswith("#"):
            continue
        if re.match(r"^\|[\s:|-]+\|?$", s):  # 表格分隔行
            continue
        if s.startswith("|"):
            cells = [c for c in s.strip("|").split("|")]
            if any(substantive(c) for c in cells):
                out.append(s)
            continue
        if substantive(s):
            out.append(s)
    return out


def split_sections(text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    current = None
    buf: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^##\s+(.*)$", line)
        if m:
            if current is not None:
                sections[current] = "\n".join(buf)
            current = m.group(1).strip()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf)
    return sections


def pick(sections: dict[str, str], keyword: str) -> str | None:
    for title, body in sections.items():
        if keyword in title:
            return body
    return None


def parse_range_rows(body: str) -> tuple[list[tuple[str, str]], str | None]:
    """解析范围表：(文件, 理由) 行 + 错误说明。"""
    lines = strip_comments(body).splitlines()
    table = [ln for ln in lines if ln.strip().startswith("|")]
    if not table:
        return [], "范围栏没有文件清单表格（需要「| 文件 | 为什么在范围内 |」两列表格）"
    header = [c.strip() for c in table[0].strip().strip("|").split("|")]
    if not any("文件" in c for c in header) or not any("为什么" in c or "理由" in c for c in header):
        return [], f"范围表头不对：需要同时有「文件」与「为什么/理由」两列（当前：{header}）"
    rows: list[tuple[str, str]] = []
    for line in table[1:]:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        if all(re.fullmatch(r"[\s:|-]*", c) for c in cells):  # 分隔行
            continue
        fname = cells[0].replace("`", "").strip().replace("\\", "/")
        rows.append((fname, cells[1]))
    return rows, None


def normalize(path: str) -> str:
    p = path.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p


def validate(text: str, diff: list[str]) -> list[str]:
    fails: list[str] = []

    if "<待填" in text:
        fails.append("报告里还有 <待填…> 占位符没写掉")

    sections = split_sections(strip_comments(text))
    required = {"范围": "范围", "影响面依据": "影响面依据", "跑了哪些测试": "跑了哪些测试", "结论": "结论"}
    bodies: dict[str, str] = {}
    for key, keyword in required.items():
        body = pick(sections, keyword)
        if body is None:
            fails.append(f"缺「## {key}」小节")
            continue
        bodies[key] = body
        if not substantive_lines(body):
            fails.append(f"「{key}」小节是空的或只有占位符（写「无」也要写清理由）")

    # 范围清单必须覆盖 diff（报告自身不算）
    needed = [normalize(p) for p in diff if not REPORT_GLOB.match(normalize(p))]
    if "范围" in bodies:
        rows, err = parse_range_rows(bodies["范围"])
        if err:
            fails.append(err)
        else:
            listed = {normalize(f) for f, _ in rows}
            missing = [p for p in needed if p not in listed]
            if missing:
                fails.append("范围清单漏了 diff 里的文件：" + "、".join(missing))
            for fname, reason in rows:
                if not reason_ok(reason):
                    fails.append(
                        f"范围清单里 `{fname}` 的「为什么在范围内」太短或是占位"
                        f"（至少 {MIN_REASON} 个文字/数字，「见上」「本次改动」不算）"
                    )

        # 影响面依据要写到调用方/引用这一层
        if "影响面依据" in bodies and not impact_ok(bodies["影响面依据"]):
            fails.append(
                f"「影响面依据」信息量不足：至少 {MIN_IMPACT} 个字，并写清改动符号与调用方/引用"
                "（没有调用方也要写「无引用，理由：…」）"
            )

    # 跑了哪些测试：代码块里要同时有命令行与真实结果
    if "跑了哪些测试" in bodies and not tests_ok(bodies["跑了哪些测试"]):
        fails.append(
            "「跑了哪些测试」只有命令没有结果：代码块里要同时贴命令行与真实输出"
            "（passed / failed / PASS / 用时 / exit=…），没贴输出等于没跑"
        )

    # 结论：只能是三态之一；写 FAIL 按约定判红（不可合并）
    if "结论" in bodies:
        found = re.findall(
            r"(?m)^\s*(?:[-*]\s*)?(?:\*\*)?结论(?:\*\*)?\s*[:：]\s*(.+?)\s*$",
            bodies["结论"],
        )
        if not found:
            fails.append("「结论」里没有 `结论：<PASS | PASS+NOTES | FAIL>` 一行")
        else:
            values = {re.sub(r"[。．.，,；;!！]+$", "", v.replace("*", "").replace("`", "").strip()) for v in found}
            value = re.sub(
                r"[。．.，,；;!！]+$", "", found[-1].replace("*", "").replace("`", "").strip()
            )
            if len(values) > 1:
                fails.append("「结论」出现了多个不同值：" + " / ".join(sorted(values)) + "（只允许一条）")
            elif value not in VERDICTS:
                fails.append(f"结论只能是 PASS / PASS+NOTES / FAIL 之一（当前：{value}）")
            elif value == "FAIL":
                fails.append("结论是 FAIL：审核判为不可合并，review-gate 按约定判红（修完重审，改成 PASS / PASS+NOTES 才绿）")

    return fails


def run_deletion_self_test() -> int:
    """真 git 层：删除的文件必须进范围（ACMR 会静默漏掉，那正是之前的假绿来源）。"""
    with tempfile.TemporaryDirectory() as tmp:
        try:
            git("init", "-q", cwd=tmp)
            git("config", "user.email", "self-test@example.com", cwd=tmp)
            git("config", "user.name", "self-test", cwd=tmp)
            for name in ("keep.py", "auth.py"):
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
                    f.write("def handler():\n    return 1\n")
            git("add", "-A", cwd=tmp)
            git("commit", "-q", "-m", "base", cwd=tmp)
            git("rm", "-q", "auth.py", cwd=tmp)
            git("commit", "-q", "-m", "drop auth", cwd=tmp)
            listed = diff_files("HEAD~1", cwd=tmp)
        except (SystemExit, OSError) as e:
            print(f"FAIL  自检 删除文件进范围 → 跑不起来：{e}")
            return 1
    if "auth.py" not in listed:
        print("FAIL  自检 删除文件进范围 → diff_files 漏了被删的文件")
        return 1
    print("PASS  自检 删除文件进范围 → 已列入（真 git 仓库）")
    return 0


def run_self_test() -> int:
    fixtures = os.path.join(ROOT, "scripts", "review", "fixtures")
    cases = [
        ("report-valid.md", True),
        ("report-empty-section.md", False),
        ("report-missing-file.md", False),
        ("report-bad-verdict.md", False),
        ("report-thin.md", False),  # 空壳：理由「见上」、影响面一句、测试栏只有 $
        ("report-fail-verdict.md", False),  # 四栏都真，但结论是 FAIL → 不许合并
    ]
    bad = 0
    for name, should_pass in cases:
        path = os.path.join(fixtures, name)
        if not os.path.exists(path):
            print(f"FAIL  自检样本缺失：{name}")
            bad += 1
            continue
        with open(path, encoding="utf-8") as f:
            text = f.read()
        fails = validate(text, SELF_TEST_DIFF)
        passed = not fails
        if passed == should_pass:
            print(f"PASS  自检 {name} → {'通过' if passed else '拦下'}（预期）")
        else:
            bad += 1
            print(f"FAIL  自检 {name} → {'通过' if passed else '拦下'}，与预期不符")
            for f_ in fails:
                print(f"      {f_}")
    bad += run_deletion_self_test()
    total = len(cases) + 1
    if bad:
        print(f"门禁自检 FAIL（{bad} 项）")
        return 1
    print(f"门禁自检 PASS（{total}/{total}：能过，也能拦）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--report", default=None, help="直接校验指定报告（本地自测）")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return run_self_test()

    if args.report:
        reports = [normalize(args.report)]
        diff: list[str] = []
        try:
            diff = diff_files(args.base)
        except SystemExit:
            print("提示：拿不到 git diff，范围覆盖检查按空 diff 处理（只校验四栏）", file=sys.stderr)
    else:
        diff = diff_files(args.base)
        reports = [p for p in (normalize(x) for x in diff) if REPORT_GLOB.match(p)]
        if not reports:
            print("审核门禁 FAIL（1 项）")
            print(f"  缺: 本次 diff 没有 docs/review/pr-*.md（跑 bash scripts/review.sh --base {args.base} 生成）")
            return 1

    all_fails: list[str] = []
    for rel in reports:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            print(f"FAIL  {rel}（这个路径在 diff 里，但工作区没有这个文件）")
            all_fails.append(f"报告不存在：{rel}")
            continue
        with open(path, encoding="utf-8") as f:
            text = f.read()
        fails = validate(text, diff)
        if fails:
            print(f"FAIL  {rel}")
            for f_ in fails:
                print(f"      {f_}")
            all_fails.extend(fails)
        else:
            print(f"PASS  {rel}（四栏齐全 · 范围覆盖 {len([p for p in diff if not REPORT_GLOB.match(normalize(p))])} 个文件 · 结论有效）")

    if all_fails:
        print(f"审核门禁 FAIL（{len(all_fails)} 项）")
        print("  依据: docs/review/README.md（四栏必填、范围覆盖 diff、结论三选一）")
        return 1
    print("审核门禁 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
