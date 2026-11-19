# SNAPSHOT_LOG — 当期快照记录

每周快照任务（shikar 数据维护）留痕。数据源：`data/fang_2026.csv`（房天下公开聚合页，低频限量抓取）。

| 日期 | 北京小区 | 天津小区 | 当期周次 | 指标期数分布 | 库内小区(北京/天津) | 限流 | 备注 |
|---|---|---|---|---|---|---|---|
| 2026-11-16 | 980 | 975 | 2026-W46 | 2026-W46×1955 / 2019-02×16622（合计 18577） | 10554 / 6885（合计 17439） | 否 | 本轮每城 96 页全部抓取成功，新增 0；CSV 周次仍停留 W46 |

> 「北京/天津小区」= 当期 CSV `fang_2026.csv` 的小区数；「库内小区」= 重建后 `shikar.db` 中 `xiaoqu` 表的总数（含 2019 历史匹配）。

## 维护备注

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
