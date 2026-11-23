# data/ — 持久化数据集

本目录的 CSV 与地铁 GeoJSON 随仓库提交，**换台电脑 clone 后无需重新抓数**；运行一次 `scripts/build_db.py` 即可生成数据库。

| 文件 | 说明 | 规模 |
|---|---|---|
| `history2019.csv` | 链家 2019 全量小区快照（公开数据集） | 北京 10101 / 天津 6523 |
| `fang_2026.csv` | 房天下 2026 当期小区快照（公开聚合页抓取，按「同城+小区+周」去重） | 3830（W46 1955 + W48 1875） |
| `SNAPSHOT_LOG.md` | 每周快照留痕（日期 / 京津小区数 / 指标期数分布 / 是否限流） | 见文件 |
| `shikar.db` | **构建产物（不随仓库提交，已加入 .gitignore）**：由上面两份 CSV 运行 `scripts/build_db.py` 生成 | 17439 小区 / 21214 指标 |
| `subway/` | 北京/天津真实地铁线路与站点（WGS84）与导入脚本所需 GeoJSON | 见 `subway/README.md` |

> `data/fang_preview_all.csv`（逐周累积的历史成交样本）**不入公开仓库**，只存私有备份仓库
> `BeiFaXianLe/shikar-data-backup`（见 `scripts/push_backup.py`）。它是不可再生数据，
> 丢了某一周的样本就永久缺失——本地缺失时可从该仓库的 `latest/critical-*.tar.gz` 取回。

字段：city,district,plate,xiaoqu_id,xiaoqu_name,week,price,supply,built_year,mom1_pct,source

## 重建数据库
```powershell
python scripts/build_db.py          # -> data/shikar.db（构建产物，不提交）
```
> `data/shikar.db` 是构建产物、不纳入版本库；clone 后先运行本命令生成基础库。地铁关系表在首次启动时自动导入（见下），也可显式预构建。`subway_line.updated_at` 取源数据版本，重复构建结果可复现（逐字节一致）。
>
> ⚠️ **重建后必做两步，否则首页会空 / 温度全丢**（与生产 `deploy/refresh.sh` 同流程）：
> 1. `python scripts/ingest_history.py --csv data/fang_preview_all.csv --db data\shikar.db`
>    ——把逐周累积的历史成交灌进库（该 CSV 不在公开仓库，需先从私有备份仓库取回）。
> 2. `python scripts/recompute_metrics.py --db data\shikar.db`
>    ——`build_db.py` / `ingest_history.py` 都不算温度，必须全量回填派生指标，
>    否则 `xiaoqu_metric_weekly.temperature` 大面积 NULL，首页 `/api/overview` 聚合为空。
>
> 另注：`build_db.py` 已自带 POI 备份回灌（`poi` / `community_poi` 不会丢），
> 但**地铁关系表不在其回灌名单内**，重建后需让服务启动时按 `data/subway/shikar-subway.json`
> 自动重建（`backend/shikar/main.py` 的 `ensure_subway`），或事先备份整库后自行还原。
> 服务运行时会持有该文件锁，重建前需先停服。详见 `SNAPSHOT_LOG.md` 的维护备注。

## 重建地铁关系表（P1）
```powershell
python scripts/prepare_subway_geojson.py --stations data/subway/beijing-station.json,data/subway/tianjin-station.json --lines data/subway/beijing-line.json,data/subway/tianjin-line.json --city-map beijing=北京,tianjin=天津 --out data/subway/shikar-subway.json
python scripts/build_subway.py --geojson data/subway/shikar-subway.json --community-crs wgs84
```
> 地铁数据为公开数据集（WGS84，见 `subway/README.md`）；只重建地铁关系表，不触碰行情主表。

## 运行应用（读取本地库）
```powershell
cd backend
python -m shikar.main --db ..\data\shikar.db --port 8080 --web-root ..\web
# 打开 http://127.0.0.1:8080
```
> 首次启动时若库中尚无地铁关系表、且存在 `data/subway/shikar-subway.json`，服务会自动导入
> 一次地铁关系（约 40 秒，之后跳过）。也可显式运行 `scripts/build_subway.py` 预构建。

## 更新当期数据（低频、限量，个人自用）
```powershell
python scripts/fetch_fang.py --city bj --pages 96 --delay 2.0
python scripts/fetch_fang.py --city tj --pages 96 --delay 2.0
python scripts/build_db.py
python scripts/ingest_history.py --csv data/fang_preview_all.csv --db data\shikar.db
python scripts/recompute_metrics.py --db data\shikar.db
```

> 去重键为「同城 + 小区 + 周」，因此**每周重跑会新增当期周的行**，而不是覆盖上一周。
> 数据仅公开聚合指标（均价/在售/建成年/环比），不含房源明细、图片与个人信息；仅供参考，非投资建议。
