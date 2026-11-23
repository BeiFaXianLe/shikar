# SNAPSHOT_LOG — 当期快照记录

每周快照任务（shikar 数据维护）留痕。数据源：`data/fang_2026.csv`（房天下公开聚合页，低频限量抓取）。

| 日期 | 北京小区 | 天津小区 | 当期周次 | 指标期数分布 | 库内小区(北京/天津) | 限流 | 备注 |
|---|---|---|---|---|---|---|---|
| 2026-11-16 | 980 | 975 | 2026-W46 | 2026-W46×1955 / 2019-02×16622（合计 18577） | 10554 / 6885（合计 17439） | 否 | 本轮每城 96 页全部抓取成功，新增 0；CSV 周次仍停留 W46 |
| 2026-11-23 | 940 | 935 | 2026-W48 | 2026-W48×1875 / 2026-W46×1955 / 月粒度×762（合计 21214） | 10554 / 6885（合计 17439） | 否 | 每城 96 页全部抓取成功，新增 1875（京 940 / 津 935）；**周次首次前进**（W46→W48），得益于 PR#38 抓取键含 week |

> 「北京/天津小区」= 当期 CSV `fang_2026.csv` 的小区数；「库内小区」= 重建后 `shikar.db` 中 `xiaoqu` 表的总数（含 2019 历史匹配）。

## 维护备注

- **2026-11-23**：本周首次出现「周次前进」。此前 CSV 永远停在 `2026-W46`，原因是
  `fetch_fang.py` / `build_db.py` 的去重键不含 `week`（同小区后续周次被当重复丢弃），
  PR#38 已修复为「同城 + 同小区 + 同周」才算重复。本次抓取因此新增 1875 行
  （京 940 / 津 935），CSV 达 3830 行（W46 1955 + W48 1875）。

  本地 `data/fang_preview_all.csv`（逐周累积的历史成交，**不可再生**）此前缺失，
  已从私有备份仓库 `BeiFaXianLe/shikar-data-backup` 的 `critical-20261123_045022.tar.gz`
  取回（1014 行 / 406 小区）并用 `scripts/ingest_history.py` 入库（762 个「小区×月」行），
  再 `scripts/recompute_metrics.py` 回填派生指标。

  `temperature` 仅 27 行非空（6 个小区），**符合预期**：PR#36 落地了稀疏数据门槛
  （期数 ≥3、跨度 ≥365 天、相邻缺口 ≤365 天），不足的小区一律置空并计入
  `insufficient` 计数，而不是给出编造的温度。`/api/overview` 因此显示
  `insufficient_ratio ≈ 1.0`——这是口径正确的诚实呈现，不是回归。
  温度要真正铺开，需要持续累积周度/月份度数据（每周 `sample_fang_preview.py`）。

- **2026-11-23（环境）**：本机 DNS（223.5.5.5）把 `github.com` 解析到黑洞 IP
  `57.155.223.240`，导致 `git fetch`/`push` 全部失败（`gh api` 因走 API 域名不受影响）。
  已在**仓库本地** `git config`（非全局）设置 `http.curloptResolve=github.com:443:140.82.114.4`
  并清空失效的 `http.proxy=127.0.0.1:7897`，git 传输恢复正常。
  若代理恢复或 DNS 变化，可执行 `git config --local --unset http.curloptResolve` 还原。

- **2026-11-19**：修复「首页空白 / 数据不一致」根因。`build_db.py` 仅对极少数（2026-W46 且历史充足）
  小区计算 temperature，`ingest_history.py` 明确不计算温度/阶段（写 NULL），导致
  `xiaoqu_metric_weekly.temperature` 大面积 NULL（实测 22096 行中 0 行有值），
  首页 `/api/overview` 聚合因此为空、页面空白。
  处置：新增 `scripts/recompute_metrics.py`（封装 `shikar.pipeline.recompute`，全量回填
  mom4/absorb/temperature/stage，不改动 POI/地铁表，幂等可重跑），并将它**加入 `deploy/refresh.sh`
  标准流程**（`ingest_history.py` 之后、重启之前）。本次已对生产库执行一次，`temperature` 22096/22096 全部回填。
  **后续每周刷新/重建库后必须跑 recompute，否则首页会再次空白。**
  说明：当前数据源缺 vol30/bargain/heat，温度分项多为中性 50，导致温度区分度偏低
  （min/max/avg≈50/66/50）；这是数据源限制，补数需抓取且受限流影响，暂不处理。

- **2026-11-16**：`scripts/build_db.py` 会先删除 `data/shikar.db` 再重建，但只重建 `xiaoqu` / `xiaoqu_metric_weekly`，
  不重建 POI 与地铁关系表（POI 需重新调用百度 Places API，配额有限）。本周重建前已备份 `shikar.db`，
  重建后按原 schema 还原 `poi` / `community_poi` / `subway_*` / `community_subway*` / `commute_cache`，
  逐表内容哈希与备份一致，`integrity_check=ok`、外键校验 0 违规。
  **后续周次重建数据库时需沿用「先备份 → 重建 → 还原 enrichment」流程，否则会丢 POI/地铁数据。**
- 后端服务在重建前需停服（持有 DB 文件锁），重建后按原命令行恢复。
