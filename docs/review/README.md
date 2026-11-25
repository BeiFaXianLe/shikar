# 审核范围报告（review-gate）

依据：`docs/57-工作流-pstack对照复审-2026-11-25.md` §1.1 / §1.3、`docs/19-pstack落地（shikar版）.md` §5。
目的：把「审核范围」从提示词里的原则（「默认聚焦、不过度审核」）变成一条可复跑的命令 + 一道 CI 闸门。

## 怎么用

```bash
# 1. 出范围清单与报告骨架（默认基线 origin/main，一条命令，60 秒内）
bash scripts/review.sh --base origin/main

# 2. 填四栏：判断结论 / 实际输出 / 结论（范围与调用方由脚本预填机读证据）
#    报告在 docs/review/pr-<PR编号或分支名>.md

# 3. 本地先验一遍门禁（CI 的 review-gate 会再跑一次）
python scripts/review_check.py --base origin/main
```

报告随 PR 一起提交。`--run` 可以顺带跑建议测试并写入输出（只跑建议测试，不跑全量）。

## 报告四栏（缺任一即红）

| 栏 | 内容 | 谁填 |
|---|---|---|
| `## 范围` | diff 的文件清单 + 每个文件「为什么在范围内」 | 脚本预填证据，审核人补充判断 |
| `## 影响面依据` | 改动符号 → 一层调用方 → **判断结论** | 前两段脚本填，判断结论审核人写 |
| `## 跑了哪些测试` | 建议测试的命令 + 实际输出 | 审核人跑、贴 |
| `## 结论` | `结论：PASS` / `结论：PASS+NOTES` / `结论：FAIL` 三选一 | 审核人 |

空栏、「无」「同上」、只剩 `<待填…>` 占位符，都算没填。

## 高扇出自动升级

命中以下任一，报告里会多一行「已升级为全量范围，理由：…」，建议测试扩到全量：

- 鉴权 / 会话（auth、login、session、token、acl、permission）
- 事务 / DB schema（storage、db、schema、migration、transaction）
- 序列化（serial、json、encod、decod、parse）
- 公共模块（`backend/shikar/api.py`、`storage.py`、`main.py`、`web/app.js`）
- 单个改动的符号调用方 ≥ 8 处

其余情况只跑建议测试；全量测试留给 CI 的 `verify`。

## CI 校验什么

`review-gate`（`.github/workflows/ci.yml`）跑 `scripts/review_check.py`：

1. 本次 diff 必须带 `docs/review/pr-*.md`；
2. 四栏都填实（空栏 / 「无」 / 「同上」 / 占位符算空）；
3. **范围清单必须覆盖 diff 的每个文件**（报告自身除外），少一个就红；
4. 结论只能是 `PASS` / `PASS+NOTES` / `FAIL` 之一。

门禁自检：`python scripts/review_check.py --self-test`（固定样本要能过、也要能拦）。

## 已知边界

- 调用方是文本检索，动态调用（`getattr`、字符串路由、前端 hash 路由）会漏——报告里「仓库内无引用」不等于真的没人用，审核人仍需按判断结论补一段。
- 只保证「已枚举的不漏」，不保证「没想到的」；后者靠人。
