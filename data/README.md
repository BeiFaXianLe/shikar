# data/ — 持久化数据集

本目录随仓库提交，**换台电脑 clone 下来即可用**，无需重新抓。

| 文件 | 说明 | 规模 |
|---|---|---|
| `history2019.csv` | 链家 2019 全量小区快照（公开数据集） | 北京 10101 / 天津 6523 |
| `fang_2026.csv` | 房天下 2026 当期小区快照（公开聚合页抓取，已去重） | 1955 |
| `shikar.db` | 由上述两者构建的 SQLite 库（可直接被应用读取） | 17439 小区 / 18579 指标 |
| `subway/` | 北京/天津真实地铁线路与站点（WGS84）与导入脚本所需 GeoJSON | 见 `subway/README.md` |

字段：city,district,plate,xiaoqu_id,xiaoqu_name,week,price,supply,built_year,mom1_pct,source

## 重建数据库
```powershell
python scripts/build_db.py          # -> data/shikar.db
```

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
python scripts/fetch_fang.py --city bj --pages 100 --delay 1.5
python scripts/fetch_fang.py --city tj --pages 100 --delay 1.5
python scripts/build_db.py
```

> 数据仅公开聚合指标（均价/在售/建成年/环比），不含房源明细、图片与个人信息；仅供参考，非投资建议。
