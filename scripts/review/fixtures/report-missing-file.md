# 审核范围报告 · pr-自检样本

- 基线：`origin/main` @ `3cdf8c1`
- 分支：`chore/review-range-gate`
- 改动：4 个文件（+180 / -0）

> 范围按 diff 聚焦（未命中高扇出）

## 范围

| 文件 | 为什么在范围内 |
|---|---|
| `scripts/review.sh` | 新增 bash 入口，只挑解释器并转发；无仓库内引用 |
| `scripts/review/report.py` | 新增范围生成器；被 scripts/review.sh 调用 |
| `scripts/review_check-typo.py` | 新增门禁校验器；被 CI 的 review-gate 调用 |
| `docs/review/README.md` | 文档：说明用法与门禁口径；不需要单测 |

## 影响面依据

### 改到的符号

| 文件 | 符号（定义行） |
|---|---|
| `scripts/review/report.py` | `main`（L420） |

### 一层调用方

| 符号 | 调用方（file:line） | 处数 |
|---|---|---|
| `main` | `scripts/review.sh:35` | 1 |

### 判断结论

三个文件都是新增的审核工具，只被入口与 CI 调用；不触及业务代码，范围就是这 4 个文件。

## 跑了哪些测试

建议测试：门禁自检（无业务单测）

```
$ python scripts/review_check.py --self-test
门禁自检 PASS（4/4：能过，也能拦）
```

实际输出：

```
$ bash scripts/review.sh --base origin/main
审核范围 · 基线 origin/main (3cdf8c1) · 分支 chore/review-range-gate
用时 1.2s
```

## 结论

结论：PASS

理由：只新增审核工具与文档，不改业务代码；自检通过。
