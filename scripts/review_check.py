#!/usr/bin/env python3
"""审核报告门禁：审核范围报告四栏填实、范围清单覆盖 diff、结论三选一。

用法：

    python scripts/review_check.py --base origin/main      # CI / 本地：校验本次 diff 带上的报告
    python scripts/review_check.py --report docs/review/pr-57.md --base origin/main
    python scripts/review_check.py --self-test             # 门禁自检：能过，也能拦

校验四件事（缺任一即退出 1）：
  1. 本次 diff 里必须有 docs/review/pr-*.md；
  2. 四栏（范围 / 影响面依据 / 跑了哪些测试 / 结论）都填了实质内容，
     空栏、「无」「同上」、只剩 <待填…> 占位符都算没填；
  3. 范围栏的文件清单必须覆盖本次 diff 的每个文件（报告自身除外），少一个就红；
  4. 结论只能是 PASS / PASS+NOTES / FAIL 之一。

退出码：0 通过；1 未通过；2 用法/读取错误（CI 里同样算红）。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

try:  # Windows 控制台默认不是 UTF-8，中文输出会花；CI 无影响
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORT_GLOB = re.compile(r"^docs/review/pr-.*\.md$")
VERDICTS = ("PASS", "PASS+NOTES", "FAIL")
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


def git(*args: str) -> str:
    p = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if p.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{p.stderr.strip()}")
    return p.stdout


def diff_files(base: str) -> list[str]:
    raw = git("diff", "--name-only", "--diff-filter=ACMR", f"{base}...HEAD")
    return [p for p in raw.splitlines() if p.strip()]


def strip_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


def substantive(cell: str) -> bool:
    """去掉 <占位符>、反引号、markdown 修饰后，还剩不剩实质内容。"""
    s = re.sub(r"<[^<>]*>", "", cell)
    s = s.replace("`", "").replace("*", "")
    core = s.strip(" \t|。．.,，、:：")
    return core.lower() not in EMPTY_WORDS and len(core) > 0


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


def validate(text: str, diff: list[str], report_path: str) -> list[str]:
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
                if not substantive(reason):
                    fails.append(f"范围清单里 `{fname}` 的「为什么在范围内」是空的")

    # 跑了哪些测试：至少一个非占位的代码块（命令 + 输出）
    if "跑了哪些测试" in bodies:
        blocks = re.findall(r"```[^\n]*\n(.*?)```", bodies["跑了哪些测试"], re.S)
        if not any(substantive(b) for b in blocks):
            fails.append("「跑了哪些测试」没有贴命令/输出代码块")

    # 结论三选一
    if "结论" in bodies:
        found = re.findall(
            r"(?m)^\s*(?:[-*]\s*)?(?:\*\*)?结论(?:\*\*)?\s*[:：]\s*(.+?)\s*$",
            bodies["结论"],
        )
        if not found:
            fails.append("「结论」里没有 `结论：<PASS | PASS+NOTES | FAIL>` 一行")
        else:
            value = found[-1].replace("*", "").replace("`", "").strip()
            if value not in VERDICTS:
                fails.append(f"结论只能是 PASS / PASS+NOTES / FAIL 之一（当前：{value}）")

    return fails


def run_self_test() -> int:
    fixtures = os.path.join(ROOT, "scripts", "review", "fixtures")
    cases = [
        ("report-valid.md", True),
        ("report-empty-section.md", False),
        ("report-missing-file.md", False),
        ("report-bad-verdict.md", False),
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
        fails = validate(text, SELF_TEST_DIFF, f"docs/review/{name}")
        passed = not fails
        if passed == should_pass:
            print(f"PASS  自检 {name} → {'通过' if passed else '拦下'}（预期）")
        else:
            bad += 1
            print(f"FAIL  自检 {name} → {'通过' if passed else '拦下'}，与预期不符")
            for f_ in fails:
                print(f"      {f_}")
    if bad:
        print(f"门禁自检 FAIL（{bad} 项）")
        return 1
    print("门禁自检 PASS（4/4：能过，也能拦）")
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
            all_fails.append(f"报告不存在：{rel}")
            continue
        with open(path, encoding="utf-8") as f:
            text = f.read()
        fails = validate(text, diff, rel)
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
