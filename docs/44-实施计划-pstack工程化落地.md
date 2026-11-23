# 实施计划 · pstack 工程化补齐（shikar）

> 来源：宝拉 2026-11-23 11:22 在 shikar项目群说「按照建议全部开始实施」，采纳 `docs/19-pstack落地（shikar版）.md` 与同日对标 pstack 的差距分析（群记录 #24）。
> 本文是**先方案后编码**里那份方案：先写清做哪些、怎么验、不做什么，再落文件。
> 状态：仓库本地改动已完成并通过验证；**推送 / 开 PR / 改 GitHub 分支保护 / 首次上线仍等人点头**（见 §6）。

---

## 1. 目标

把「能跑」换成「能证明」：PR 合并前有可复跑门禁，合并后有可回滚的发布脚本，重复错误固化成测试/门禁而不是再写一条规范。

**完成条件**

| # | 断言 | 怎么验 |
|---|---|---|
| 1 | 本地一键验证能跑，全绿 | `bash scripts/verify.sh` → `VERIFY PASS`（本次实测 `302 passed in 70.9s`） |
| 2 | CI 在 PR 与 push main 时自动跑同一套门禁 | `.github/workflows/ci.yml` 的 `verify` job 调用 `scripts/verify.sh` |
| 3 | 新 PR 自动带目标/完成条件/证据/三段式审核/三态结论 | 开一个 PR，正文即 `.github/pull_request_template.md` |
| 4 | 发布脚本幂等、可干跑、失败自动回滚、不碰 `data/` | 服务器上 `bash deploy/release.sh --dry-run` 见 §5 证据；dry-run 后服务与文件均未变 |
| 5 | 发布/回滚有人能照着做，不可逆动作列清 | `docs/发布与回滚.md` |
| 6 | 协作流程写明 open PR→babysit→shipping 与「合并由人点头」 | `docs/08-协作流程与角色.md` 新增章节 |

**不做**

- 不改任何测试代码（现有 302 个用例就是基线）。
- 不替换现有审核/开发助手的分工，`docs/08` 只补交付三段，不推翻原表。
- 不引入新运行时依赖（运行时仍是纯标准库；只补一份**测试用** `backend/requirements-dev.txt`）。
- 不自动合并、不自动改仓库保护设置、不自动连服务器上线。

---

## 2. 现状核查（实测，不是转述）

| 事实 | 证据 |
|---|---|
| 仓库无 `.github/workflows/`、无 PR 模板 | `ls .github/workflows` → 不存在；`find .github -iname '*pull_request*'` → 空 |
| 测试基线 302 passed | `cd backend && python -m pytest` → `302 passed in 70.85s` |
| 运行时零第三方依赖 | `backend/`+`scripts/` 的 import 只有标准库 + `osmium`（仅 OSM 导入脚本用） |
| `deploy/` 只有数据维护，没有代码发布 | `deploy/refresh.sh`（每周抓数重建库）、`deploy/set-gh-token.sh` |
| web 是纯静态，没有构建步骤 | `web/` 只有 `app.js/index.html/styles.css/similar.js`，无 `package.json` |
| 服务器服务定义 | `systemctl cat shikar`：`WorkingDirectory=/opt/shikar/backend`，`--port 8080`，`--web-root /opt/shikar/web` |
| 服务器 git 元数据脏（内容=main） | 9 个源码文件按 LF 归一后 blob 哈希与本地 main 逐一相同；`HEAD` 仍停 `1195b46`。详见 `docs/发布与回滚.md` §1.1 |
| 服务器缺 `pyosmium`，2 个 OSM 用例会失败 | 服务器上 `pytest` → `scripts/osm_poi.py:18: ModuleNotFoundError`（test_osm_poi 2 例） |
| 磁盘只剩 ~8.9G，库 259MB | `df -h /`、`ls -lh data/shikar.db` |

---

## 3. 交付物清单

| # | 文件 | 作用 | 对应建议清单 |
|---|---|---|---|
| 1 | `.github/workflows/ci.yml` | PR/push main 跑 `scripts/verify.sh`（pytest + 语法 + 静态资源）、shellcheck、定时依赖体检 | ① 必做 |
| 2 | `.github/pull_request_template.md` | 把 `docs/19` §2 §3 §5 落成模板：目标/完成条件/不做/证据/P0–P3 三段式/独立验证三态/人工合并 | ③ 必做 |
| 3 | `deploy/release.sh` | 幂等发布：取提交→备份→同步代码→服务端测试→重启→健康+冒烟→失败自动回滚；带 `--dry-run/--rollback/--list` | ② 必做 |
| 4 | `docs/发布与回滚.md` | 服务器账号/路径/服务名/端口、谁能执行、步骤、回滚、不可逆动作、与 refresh.sh 的边界 | ② 必做 |
| 5 | `scripts/verify.sh` | 一键验证（本地与 CI 同一入口），可复跑、可贴证据 | ⑤ 建议 |
| 6 | `docs/08-协作流程与角色.md` | 追加 open PR→babysit→shipping 三段；fresh agent 独立验证；PASS/PASS+NOTES/FAIL；合并由人点头 | ⑦ 建议 |
| 7 | `backend/requirements-dev.txt` | 测试依赖（pytest、osmium），CI 用；说明运行时零依赖 | 支撑 ① |
| 8 | `docs/44-…`（本文） | 方案与验收记录 | — |

可选项 ⑥（安全扫描）：已并入 CI 的 `deps-audit` job（仅定时/手动，`continue-on-error`，不阻断合并）。
当时报告说「涉及权限、资金、删数据时升为必须」——本轮尚未升为必须，留待出现此类改动时收紧。

---

## 4. 关键设计决定（为什么这么做）

1. **门禁只有一个入口**：CI 调 `scripts/verify.sh`，人本地也调它。避免「本地过了 CI 挂」两套口径。
2. **发布不依赖服务器工作区**：因为服务器 git 是脏的（§2），用 `git archive <commit>` 取代码再 rsync 覆盖代码目录。
   这样既不会 `reset --hard` 误伤 `data/`，也不要求服务器先修好 git。
3. **发布只碰代码目录**：`backend web scripts deploy` 四个目录，`data/` 与 `.secrets/` 明确排除。`data/` 里是不可再生数据。
4. **幂等靠标记文件，不靠 git HEAD**：`.deployed-release` 记录已发布提交。重复跑不搬文件，只校验健康。
5. **冒烟只断言结构**：`/api/overview` 必须有 `week` 且有城市数据；不断言温度数值——数值依赖数据质量，卡在发布门禁上会天天误报。
6. **缺 `pyosmium` 时显式跳过而非静默**：服务器没装就打印「跳过 test_osm_poi 及其原因」，其余用例照跑。
7. **不自动删旧发布点**：磁盘只剩 ~8.9G，删备份是人的决定；脚本只告警。同理不自动改 GitHub 设置。

---

## 5. 验证证据（本次实测）

```
$ bash scripts/verify.sh
shikar verify · 2026-11-23 13:14 · cefae80
== Python 语法 (compileall) ==   PASS  backend/ + scripts/ 编译通过
== Shell 语法 (bash -n) ==       PASS  deploy/*.sh + scripts/verify.sh 语法通过
== 前端静态资源 ==               PASS  web/*.js 语法通过
                                 PASS  web/index.html 本地资源引用齐全
== pytest ==                     PASS  302 passed in 70.92s (0:01:10)
VERIFY PASS（跳过 0 项）
```

```
$ bash deploy/release.sh --dry-run --yes        # 在服务器 /opt/shikar 上
[13:22:19] 当前运行副本 git HEAD=1195b46（历史发布用「拷文件」做过，HEAD 可能落后）
[13:22:19] dry-run：不 fetch，origin/* 可能不是最新
[13:22:19] 同步代码目录：backend web scripts deploy（data/ 与 .secrets/ 不动）
[13:22:19] WARN: 服务器未装 pyosmium(osmium)，跳过 tests/test_osm_poi.py（其余照跑，不静默）
[13:22:19] 健康检查通过（第 1s）：http://127.0.0.1:8080/api/health
[13:22:19] 冒烟检查通过：/api/overview 有 week 且有城市数据
```

dry-run 后核对：`.releases/`、`.deployed-release` 均未生成，`systemctl is-active shikar` 仍 `active`，`HEAD` 仍 `1195b46` —— 确认干跑零改动。

---

## 6. 需要宝拉点头的事（脚本已就绪，只差人一句话）

| # | 事项 | 说明 | 我的建议 |
|---|---|---|---|
| 1 | 推送分支 + 开 PR | 本轮改动提交在本地分支，未 push（缺 Git 登录态）。push 与开 PR 后 CI 会真跑一次 | 先推分支开 PR，让 CI 实跑一遍门禁，再合并 |
| 2 | 开启 main 分支保护 | 需要在 GitHub 仓库设置里勾「Require status checks: verify」，助手无权改仓库设置 | 由你或给权限的账号开；或给我一次带 Git 登录态的回合，我执行 `gh api` |
| 3 | 首次真实发布 | `deploy/release.sh` 迄今只跑过 dry-run | 建议先用 `--ref <当前 main>` 发布一次（内容与线上一致，等于练手），确认幂等与回执无误 |
| 4 | 是否修服务器 git 脏状态 | `git reset --hard origin/main` 能让元数据归位，但要先确认不牵连 `data/` | 可缓；发布脚本已不依赖它。修之前单独确认一次 |

---

## 7. 遗留风险

| 风险 | 影响 | 处置 |
|---|---|---|
| 服务器 git 脏，人工 `git pull` 会搞混 | 可能覆盖 `data/` 周更数据 | 已写入 `docs/发布与回滚.md` §1.1 与 §5；发布脚本绕开工作区 |
| 磁盘 ~8.9G，库 259MB | 多发几次发布包 + 整库备份容易打满 | 脚本不自动删，超阈值告警；`fullbak` 只留 1 份 |
| `pyosmium` 未装 | 2 个 OSM 用例在服务器上无法跑 | 显式跳过 + 告警；要全跑就 `pip install -r backend/requirements-dev.txt`（需人决定是否装） |
| CI 用 Python 3.14 | 与生产一致，但 GitHub runner 的 3.14 若不可用会红 | 若 `setup-python` 拉不到 3.14，降 3.13 并在此记录（生产是 3.14，需另议） |
