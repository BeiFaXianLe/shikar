# 审核范围报告 · pr-自检样本

- 基线：`origin/main` @ `3cdf8c1`
- 分支：`chore/review-range-gate`
- 改动：4 个文件（+180 / -0）

## 范围

| 文件 | 为什么在范围内 |
|---|---|
| `scripts/review.sh` | 新增 bash 入口，只挑解释器并转发 |
| `scripts/review/report.py` | 新增范围生成器 |
| `scripts/review_check.py` | 新增门禁校验器 |
| `docs/review/README.md` | 文档：说明用法与门禁口径 |

## 影响面依据

### 判断结论

<待填：范围为什么是这几个文件>

## 跑了哪些测试

```
$ python scripts/review_check.py --self-test
门禁自检 PASS（4/4）
```

## 结论

结论：PASS
