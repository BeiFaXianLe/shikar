#!/usr/bin/env python3
"""审核范围报告生成器：一条命令出「范围清单 + 调用方 + 建议测试 + 报告骨架」。

用法（入口是 scripts/review.sh）：

    bash scripts/review.sh                          # 基线默认 origin/main
    bash scripts/review.sh --base origin/main
    bash scripts/review.sh --pr 57                  # 报告名用 PR 编号
    bash scripts/review.sh --run                    # 顺带跑建议测试，把输出写进报告

做四件事：
  1. 列本次 diff 的文件（A/M/R，D 不算范围）；
  2. 列改动到的函数/类（Python 用 ast，其他用正则兜底）；
  3. 列这些符号的一层调用方（仓库内检索，标 file:line）；
  4. 按同名与 import 关系推荐要跑的测试，生成 docs/review/pr-<编号或分支>.md 骨架。

命中高扇出（鉴权 / 事务 / 序列化 / DB schema / 公共模块，或单符号调用方达阈值）时，
报告里加一行「已升级为全量范围，理由：…」，建议测试扩到全量。

只读：不写业务代码，不改 git 状态，除报告文件外不落盘。
"""
from __future__ import annotations

import argparse
import ast
import datetime
import os
import re
import subprocess
import sys

try:  # Windows 控制台默认不是 UTF-8，中文输出会花；CI 无影响
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 调用方达到这个数就算高扇出：改一处要看的地方太多，按全量范围审。
FANOUT_CALLER_THRESHOLD = 8

# 鉴权 / 事务 / 序列化 / DB schema：命中即升级全量。
HIGH_RISK_PATHS = [
    (r"(?:^|/)(?:auth|login|session|token|acl|permission)[^/]*$", "鉴权 / 会话"),
    (r"(?:^|/)(?:storage|db|database|schema|migrations?|transaction)[^/]*$", "事务 / DB schema"),
    (r"(?:^|/)(?:serial|json|encod|decod|parse)[^/]*\.py$", "序列化"),
]

# 公共模块：一处改动即被多个功能共享。
SHARED_MODULES = {
    "backend/shikar/api.py": "公共 API 层",
    "backend/shikar/storage.py": "公共存取层",
    "backend/shikar/main.py": "服务入口",
    "web/app.js": "前端单页入口",
}

SKIP_DIRS = {".git", "data", "__pycache__", "node_modules", "evidence", "prototype", "docs"}
CODE_EXTS = {".py", ".js", ".sh", ".html"}

JS_SYMBOL_PATTERNS = [
    r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)",
    r"^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)",
    r"^\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\(|function|[A-Za-z_$][\w$]*\s*=>)",
]


def git(*args: str, check: bool = True) -> str:
    p = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and p.returncode != 0:
        sys.exit(f"git {' '.join(args)} 失败：{p.stderr.strip()}")
    return p.stdout


def read_text(path: str) -> str:
    try:
        with open(os.path.join(ROOT, path), encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def parse_diff(base: str) -> tuple[list[tuple[str, str]], int, int]:
    """返回 ([(状态, 路径)], 增加行数, 删除行数)。"""
    raw = git("diff", "--name-status", "-z", "-M", f"{base}...HEAD")
    parts = [p for p in raw.split("\0") if p]
    entries: list[tuple[str, str]] = []
    i = 0
    while i < len(parts):
        status = parts[i]
        i += 1
        if status[:1] in ("R", "C"):
            i += 2  # 老路径 + 新路径，取新路径
            entries.append((status[0], parts[i - 1]))
        else:
            entries.append((status[0], parts[i]))
            i += 1

    added = removed = 0
    for line in git("diff", "--numstat", f"{base}...HEAD").splitlines():
        cols = line.split("\t")
        if len(cols) < 3:
            continue
        try:
            added += int(cols[0])
            removed += int(cols[1])
        except ValueError:
            pass
    return entries, added, removed


def py_symbols(path: str) -> list[tuple[str, int]]:
    src = read_text(path)
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out: list[tuple[str, int]] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + child.name
                out.append((name, child.lineno))
                walk(child, name + ".")
            else:
                walk(child, prefix)

    walk(tree, "")
    return out


def grep_symbols(path: str) -> list[tuple[str, int]]:
    src = read_text(path)
    out: list[tuple[str, int]] = []
    for lineno, line in enumerate(src.splitlines(), 1):
        for pat in JS_SYMBOL_PATTERNS:
            m = re.search(pat, line)
            if m:
                out.append((m.group(1), lineno))
                break
    return out


def symbols_of(path: str) -> list[tuple[str, int]]:
    if path.endswith(".py"):
        return py_symbols(path)
    if path.endswith((".js", ".html", ".sh")):
        return grep_symbols(path)
    return []


def is_definition(line: str, name: str) -> bool:
    n = re.escape(name)
    return bool(
        re.match(rf"^\s*(?:async\s+)?def\s+{n}\b", line)
        or re.match(rf"^\s*class\s+{n}\b", line)
        or re.match(rf"^\s*(?:export\s+)?(?:async\s+)?function\s+{n}\b", line)
        or re.match(rf"^\s*(?:const|let|var)\s+{n}\s*=", line)
    )


def build_index() -> dict[str, list[str]]:
    """仓库内可检索的文本文件与行（跳过 data/、docs/、原型与缓存）。"""
    index: dict[str, list[str]] = {}
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            ext = os.path.splitext(fn)[1]
            if ext not in CODE_EXTS:
                continue
            full = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(full) > 1_000_000:
                    continue
                with open(full, encoding="utf-8", errors="replace") as f:
                    index[os.path.relpath(full, ROOT).replace(os.sep, "/")] = f.read().splitlines()
            except OSError:
                continue
    return index


def find_callers(index: dict[str, list[str]], name: str, limit: int = 25) -> tuple[list[str], int]:
    """一层调用方：仓库内出现的该符号引用（去掉定义那一行）。"""
    bare = name.rsplit(".", 1)[-1]
    pat = re.compile(rf"(?<![\w$]){re.escape(bare)}(?![\w$])")
    hits: list[str] = []
    total = 0
    for path, lines in index.items():
        for lineno, line in enumerate(lines, 1):
            if not pat.search(line) or is_definition(line, bare):
                continue
            total += 1
            if len(hits) < limit:
                hits.append(f"{path}:{lineno}")
    return hits, total


def test_files() -> list[str]:
    tests_dir = os.path.join(ROOT, "backend", "tests")
    if not os.path.isdir(tests_dir):
        return []
    return sorted(
        f"backend/tests/{f}"
        for f in os.listdir(tests_dir)
        if f.startswith("test_") and f.endswith(".py")
    )


def suggested_tests(changed: list[str], all_tests: list[str]) -> dict[str, list[str]]:
    """按同名文件与 import/引用关系推荐测试；返回值只含命中的文件。"""
    contents = {t: read_text(t) for t in all_tests}
    result: dict[str, list[str]] = {}
    for path in changed:
        hits: list[str] = []
        stem = os.path.splitext(os.path.basename(path))[0]
        same_name = f"backend/tests/test_{stem}.py"
        if path.startswith("backend/tests/test_"):
            hits.append(path)
        elif same_name in all_tests:
            hits.append(same_name)
        for test in all_tests:
            if test in hits:
                continue
            body = contents.get(test, "")
            if path.startswith("backend/shikar/"):
                if re.search(rf"shikar(?:\.{stem}| import[^\n]*\b{stem}\b)", body):
                    hits.append(test)
            elif path.startswith(("web/", "deploy/", "scripts/")):
                if os.path.basename(path) in body:
                    hits.append(test)
        result[path] = sorted(set(hits))
    return result


def escalation_reasons(
    changed: list[str], symbols: dict[str, list[tuple[str, int]]], callers: dict[str, int]
) -> list[str]:
    reasons: list[str] = []
    for path in changed:
        for pattern, label in HIGH_RISK_PATHS:
            if re.search(pattern, path):
                reasons.append(f"{path} 命中{label}路径")
                break
        if path in SHARED_MODULES:
            reasons.append(f"{path} 是{SHARED_MODULES[path]}")
    hot = [name for name, n in callers.items() if n >= FANOUT_CALLER_THRESHOLD]
    if hot:
        shown = "、".join(hot[:5])
        reasons.append(f"单符号调用方 ≥ {FANOUT_CALLER_THRESHOLD}：{shown}（共 {len(hot)} 个）")
    return reasons


def why_in_scope(
    path: str,
    symbols: list[tuple[str, int]],
    caller_total: int,
    tests: list[str],
) -> str:
    if path.startswith("docs/"):
        return "文档改动，无运行时代码路径；不需要单测"
    if path.startswith(".github/"):
        return "CI 配置改动，影响门禁本身；无对应单测（由 CI 自检覆盖）"
    if path.startswith("backend/tests/"):
        return "测试改动：自身即验证"
    parts: list[str] = []
    if symbols:
        shown = "、".join(n for n, _ in symbols[:5])
        more = f" 等 {len(symbols)} 个" if len(symbols) > 5 else ""
        parts.append(f"改到符号：{shown}{more}")
    else:
        parts.append("无函数/类级改动")
    parts.append(f"仓库内引用 {caller_total} 处" if caller_total else "仓库内无引用")
    parts.append("建议测试 " + "、".join(os.path.basename(t) for t in tests) if tests else "无对应测试")
    return "；".join(parts)


def write_report(
    label: str,
    base: str,
    base_sha: str,
    branch: str,
    changed: list[str],
    symbols: dict[str, list[tuple[str, int]]],
    callers: dict[str, tuple[list[str], int]],
    tests: dict[str, list[str]],
    esc: list[str],
    added: int,
    removed: int,
    run_output: str | None,
) -> str:
    rel = f"docs/review/{label}.md"
    rows = []
    for path in changed:
        syms = symbols.get(path, [])
        total = callers.get(path, ([], 0))[1]
        rows.append(f"| `{path}` | {why_in_scope(path, syms, total, tests.get(path, []))} |")

    sym_rows = [
        f"| `{p}` | " + ("、".join(f"`{n}`（L{ln}）" for n, ln in syms[:10]) or "—") + " |"
        for p, syms in symbols.items()
        if syms
    ]
    caller_rows = []
    for name, (hits, total) in callers.items():
        if total == 0:
            continue
        shown = "、".join(f"`{h}`" for h in hits[:10])
        if total > len(hits[:10]):
            shown += f"（+{total - len(hits[:10])} 处）"
        caller_rows.append(f"| `{name}` | {shown} | {total} |")

    suggested = sorted({t for ts in tests.values() for t in ts})
    command = (
        "cd backend && python -m pytest " + " ".join(f"tests/{os.path.basename(t)}" for t in suggested)
        if suggested
        else "<待填：本次改动无建议测试，写清为什么可以不跑>"
    )
    if esc:
        command = "cd backend && python -m pytest   # 已升级为全量范围"

    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# 审核范围报告 · {label}",
        "",
        f"<!-- 生成：bash scripts/review.sh --base {base} · {now} -->",
        "<!-- review-gate（scripts/review_check.py）校验：四栏必须填实、范围清单必须覆盖本次 diff 的每个文件、结论三选一。 -->",
        "",
        f"- 基线：`{base}` @ `{base_sha}`",
        f"- 分支：`{branch}`",
        f"- 改动：{len(changed)} 个文件（+{added} / -{removed}）",
        "",
        "> 已升级为全量范围，理由：" + "；".join(esc) if esc else "> 范围按 diff 聚焦（未命中高扇出）",
        "",
        "## 范围",
        "",
        "<!-- 第一列就是 diff 的文件清单，review-gate 逐个核对，少一个就红。",
        "     第二列是「为什么在范围内」：机读证据已预填，审核人可补充判断。 -->",
        "",
        "| 文件 | 为什么在范围内 |",
        "|---|---|",
        *rows,
        "",
        "## 影响面依据",
        "",
        "<!-- 改动符号 → 一层调用方 → 判断结论。前两段由脚本填，判断结论由审核人写。 -->",
        "",
        "### 改到的符号",
        "",
        "| 文件 | 符号（定义行） |",
        "|---|---|",
        *(sym_rows or ["| — | 无函数/类级改动 |"]),
        "",
        "### 一层调用方",
        "",
        "| 符号 | 调用方（file:line） | 处数 |",
        "|---|---|---|",
        *(caller_rows or ["| — | 仓库内无引用 | 0 |"]),
        "",
        "### 判断结论",
        "",
        "<待填：范围为什么是这几个文件；哪些调用方决定了它；没有调用方也要写「无，理由：…」>",
        "",
        "## 跑了哪些测试",
        "",
        "<!-- 只跑建议测试；高扇出升级时跑全量。命令与实际输出都要贴，没跑就是没验证。 -->",
        "",
        "建议测试：",
        "",
        "```",
        command,
        "```",
        "",
        "实际输出：",
        "",
        "```",
        run_output.strip() if run_output else "<待填：粘贴真实输出>",
        "```",
        "",
        "## 结论",
        "",
        "<!-- 三选一：只写 PASS / PASS+NOTES / FAIL，不要写「通过」「没问题」。 -->",
        "",
        "结论：<PASS | PASS+NOTES | FAIL>",
        "",
        "理由：",
        "",
        "<待填：一两句>",
        "",
    ]
    path = os.path.join(ROOT, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    return rel


def run_tests(suggested: list[str], full: bool, escalated: bool) -> str:
    cmd = [sys.executable, "-m", "pytest"]
    if escalated or full:
        pass
    else:
        cmd += [f"tests/{os.path.basename(t)}" for t in suggested]
    p = subprocess.run(
        cmd,
        cwd=os.path.join(ROOT, "backend"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
    )
    return "$ " + " ".join(cmd) + "\n" + (p.stdout + p.stderr).strip()


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--pr", default=None, help="报告名用 PR 编号（默认用分支名）")
    ap.add_argument("--run", action="store_true", help="跑建议测试并把输出写进报告")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的报告")
    args = ap.parse_args()

    start = time.time()
    base_sha = git("rev-parse", "--short", args.base, check=False).strip()
    if not base_sha:
        sys.exit(f"找不到基线 {args.base}；先 git fetch origin main")

    entries, added, removed = parse_diff(args.base)
    changed = [p for st, p in entries if st != "D"]
    branch = git("rev-parse", "--abbrev-ref", "HEAD").strip()

    symbols: dict[str, list[tuple[str, int]]] = {}
    callers: dict[str, tuple[list[str], int]] = {}
    index = build_index()
    for path in changed:
        syms = symbols_of(path)
        if not syms:
            continue
        symbols[path] = syms
        for name, _ in syms:
            hits, total = find_callers(index, name)
            callers[name] = (hits, total)

    all_tests = test_files()
    tests = suggested_tests(changed, all_tests)
    esc = escalation_reasons(changed, symbols, {n: t for n, (_, t) in callers.items()})

    print(f"审核范围 · 基线 {args.base} ({base_sha}) · 分支 {branch}")
    print(f"改动 {len(changed)} 个文件（+{added} / -{removed}）")
    print()
    print("[1/4] 改动符号")
    if symbols:
        for path, syms in symbols.items():
            print(f"  {path}: " + "、".join(f"{n}(L{ln})" for n, ln in syms[:8]))
    else:
        print("  无函数/类级改动")
    print()
    print("[2/4] 一层调用方")
    hot = [(n, t) for n, (_, t) in callers.items() if t]
    if hot:
        for name, total in sorted(hot, key=lambda x: -x[1])[:12]:
            hits, _ = callers[name]
            print(f"  {name} ← {total} 处（{', '.join(hits[:4])}{' …' if total > 4 else ''}）")
    else:
        print("  无仓库内引用")
    print()
    print("[3/4] 建议测试")
    suggested = sorted({t for ts in tests.values() for t in ts})
    if esc:
        print("  已升级为全量范围，理由：" + "；".join(esc))
        print("  cd backend && python -m pytest")
    elif suggested:
        print("  cd backend && python -m pytest " + " ".join(os.path.basename(t) for t in suggested))
    else:
        print("  无（本次改动没有命中测试）")
    uncovered = [p for p in changed if p.endswith(".py") and not tests.get(p) and not p.startswith(("backend/tests/", "docs/"))]
    if uncovered:
        print("  未覆盖：" + "、".join(uncovered))
    print()

    label = f"pr-{args.pr}" if args.pr else "pr-" + re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-")
    rel = f"docs/review/{label}.md"
    if os.path.exists(os.path.join(ROOT, rel)) and not args.force:
        print(f"[4/4] 报告已存在，未覆盖：{rel}（要重生成加 --force）")
        return 0

    run_output = None
    if args.run:
        print("跑测试…")
        run_output = run_tests(suggested, full=True, escalated=bool(esc))

    rel = write_report(
        label=label,
        base=args.base,
        base_sha=base_sha,
        branch=branch,
        changed=changed,
        symbols=symbols,
        callers=callers,
        tests=tests,
        esc=esc,
        added=added,
        removed=removed,
        run_output=run_output,
    )
    print(f"[4/4] 报告骨架 → {rel}")
    print()
    print(f"下一步：填四栏（判断结论 / 实际输出 / 结论），再跑 python scripts/review_check.py --base {args.base}")
    print(f"用时 {time.time() - start:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
