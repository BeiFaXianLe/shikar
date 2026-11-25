# 审核范围报告 · pr-chore-review-range-gate

<!-- 生成：bash scripts/review.sh --base origin/main · 2026-11-25 12:17 -->
<!-- review-gate（scripts/review_check.py）校验：四栏必须填实、范围清单必须覆盖本次 diff 的每个文件、结论三选一。 -->

- 基线：`origin/main` @ `3cdf8c1`
- 分支：`chore/review-range-gate`
- 改动：8 个文件（+1121 / -0）

> 已升级为全量范围，理由：单符号调用方 ≥ 8：git、main（共 2 个）

## 范围

<!-- 第一列就是 diff 的文件清单，review-gate 逐个核对，少一个就红。
     第二列是「为什么在范围内」：机读证据已预填，审核人可补充判断。 -->

| 文件 | 为什么在范围内 |
|---|---|
| `docs/review/README.md` | 文档改动，无运行时代码路径；不需要单测 |
| `scripts/review.sh` | 无函数/类级改动；仓库内无引用；无对应测试 |
| `scripts/review/fixtures/report-bad-verdict.md` | 无函数/类级改动；仓库内无引用；无对应测试 |
| `scripts/review/fixtures/report-empty-section.md` | 无函数/类级改动；仓库内无引用；无对应测试 |
| `scripts/review/fixtures/report-missing-file.md` | 无函数/类级改动；仓库内无引用；无对应测试 |
| `scripts/review/fixtures/report-valid.md` | 无函数/类级改动；仓库内无引用；无对应测试 |
| `scripts/review/report.py` | 改到符号：git、read_text、parse_diff、py_symbols、py_symbols.walk 等 17 个；仓库内无引用；无对应测试 |
| `scripts/review_check.py` | 改到符号：git、diff_files、strip_comments、substantive、substantive_lines 等 12 个；仓库内无引用；无对应测试 |

## 影响面依据

<!-- 改动符号 → 一层调用方 → 判断结论。前两段由脚本填，判断结论由审核人写。 -->

### 改到的符号

| 文件 | 符号（定义行） |
|---|---|
| `scripts/review/report.py` | `git`（L68）、`read_text`（L82）、`parse_diff`（L90）、`py_symbols`（L119）、`py_symbols.walk`（L127）、`grep_symbols`（L140）、`symbols_of`（L152）、`is_definition`（L160）、`build_index`（L170）、`find_callers`（L190） |
| `scripts/review_check.py` | `git`（L59）、`diff_files`（L73）、`strip_comments`（L78）、`substantive`（L82）、`substantive_lines`（L90）、`split_sections`（L111）、`pick`（L129）、`parse_range_rows`（L136）、`normalize`（L157）、`validate`（L164） |

### 一层调用方

| 符号 | 调用方（file:line） | 处数 |
|---|---|---|
| `git` | `backend/tests/test_deploy_units.py:142`、`deploy/release.sh:85`、`deploy/release.sh:86`、`deploy/release.sh:198`、`deploy/release.sh:199`、`deploy/release.sh:207`、`deploy/release.sh:213`、`deploy/release.sh:218`、`deploy/release.sh:229`、`deploy/release.sh:230`（+34 处） | 44 |
| `read_text` | `scripts/build_subway.py:42`、`scripts/prepare_subway_geojson.py:110`、`scripts/prepare_subway_geojson.py:134`、`scripts/review/report.py:120`、`scripts/review/report.py:141`、`scripts/review/report.py:219` | 6 |
| `parse_diff` | `scripts/review/report.py:436` | 1 |
| `py_symbols` | `scripts/review/report.py:154` | 1 |
| `py_symbols.walk` | `backend/tests/test_commute.py:213`、`scripts/review/report.py:132`、`scripts/review/report.py:134`、`scripts/review/report.py:136`、`scripts/review/report.py:173` | 5 |
| `grep_symbols` | `scripts/review/report.py:156` | 1 |
| `symbols_of` | `scripts/review/report.py:444` | 1 |
| `is_definition` | `scripts/review/report.py:198` | 1 |
| `build_index` | `scripts/review/report.py:442` | 1 |
| `find_callers` | `scripts/review/report.py:449` | 1 |
| `test_files` | `scripts/review/report.py:452` | 1 |
| `suggested_tests` | `scripts/review/report.py:453` | 1 |
| `escalation_reasons` | `scripts/review/report.py:454` | 1 |
| `why_in_scope` | `scripts/review/report.py:304` | 1 |
| `write_report` | `scripts/review/report.py:500` | 1 |
| `run_tests` | `scripts/review/report.py:498` | 1 |
| `main` | `backend/shikar/api.py:673`、`backend/shikar/main.py:16`、`backend/shikar/main.py:35`、`backend/shikar/main.py:174`、`backend/tests/test_osm_poi.py:80`、`backend/tests/test_osm_poi.py:132`、`backend/tests/test_prepare_subway.py:89`、`backend/tests/test_prepare_subway.py:92`、`backend/tests/test_prepare_subway.py:178`、`backend/tests/test_prepare_subway.py:191`（+50 处） | 60 |
| `diff_files` | `scripts/review_check.py:266`、`scripts/review_check.py:270` | 2 |
| `strip_comments` | `scripts/review_check.py:93`、`scripts/review_check.py:138`、`scripts/review_check.py:170` | 3 |
| `substantive` | `scripts/review_check.py:103`、`scripts/review_check.py:106`、`scripts/review_check.py:194`、`scripts/review_check.py:200` | 4 |
| `substantive_lines` | `scripts/review_check.py:179` | 1 |
| `split_sections` | `scripts/review_check.py:170` | 1 |
| `pick` | `scripts/review_check.py:174` | 1 |
| `parse_range_rows` | `scripts/review_check.py:185` | 1 |
| `normalize` | `scripts/review_check.py:183`、`scripts/review_check.py:189`、`scripts/review_check.py:263`、`scripts/review_check.py:271`、`scripts/review_check.py:292` | 5 |
| `validate` | `scripts/review_check.py:236`、`scripts/review_check.py:285` | 2 |
| `run_self_test` | `scripts/review_check.py:260` | 1 |

### 判断结论

<待填：范围为什么是这几个文件；哪些调用方决定了它；没有调用方也要写「无，理由：…」>

## 跑了哪些测试

<!-- 只跑建议测试；高扇出升级时跑全量。命令与实际输出都要贴，没跑就是没验证。 -->

建议测试：

```
cd backend && python -m pytest   # 已升级为全量范围
```

实际输出：

```
<待填：粘贴真实输出>
```

## 结论

<!-- 三选一：只写 PASS / PASS+NOTES / FAIL，不要写「通过」「没问题」。 -->

结论：<PASS | PASS+NOTES | FAIL>

理由：

<待填：一两句>
