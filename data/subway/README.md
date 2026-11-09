# data/subway/ — 地铁线路与站点（真实数据，WGS84）

本目录存放 P1「地铁沿线找小区」使用的真实地铁 GeoJSON 数据与来源说明。

## 文件

| 文件 | 说明 |
|---|---|
| `beijing-station.json` / `beijing-line.json` | 北京地铁站点/线路（原始，来自公开数据集） |
| `tianjin-station.json` / `tianjin-line.json` | 天津地铁站点/线路（原始，来自公开数据集） |
| `shikar-subway.json` | 由 `scripts/prepare_subway_geojson.py` 适配、供 `scripts/build_subway.py` 导入的单一 FeatureCollection |

## 来源与许可

- 数据来源：[`luxueyan/generate-china-subways-geojson`](https://github.com/luxueyan/generate-china-subways-geojson)（ISC 许可）。
  该工具从高德地图公开接口抓取线路/站点，并用 `gcoord` 做 **GCJ02 → WGS84** 纠偏后输出。
- 因此本目录所有坐标均为 **WGS84**。GeoJSON 内 `coord_system: "wgs84"` 为显式声明，导入时按此计算距离，不做任何猜测。
- 覆盖：北京 24 条线路 / 340 站；天津 6 条线路 / 143 站（截至原始数据集版本）。
- **注意**：这是第三方整理的公开数据，线路走向与站点坐标精度未逐站核验；仅作位置参考，不代表官方数据。生产使用前应再次核对来源许可与更新时效。

## 重建地铁关系表

```powershell
# 1) 适配为 shikar 导入格式
python scripts/prepare_subway_geojson.py `
  --stations data/subway/beijing-station.json,data/subway/tianjin-station.json `
  --lines data/subway/beijing-line.json,data/subway/tianjin-line.json `
  --city-map beijing=北京,tianjin=天津 `
  --out data/subway/shikar-subway.json

# 2) 导入到 data/shikar.db（显式声明小区坐标系为 wgs84）
python scripts/build_subway.py --geojson data/subway/shikar-subway.json --community-crs wgs84
```

## 坐标系核实说明

小区经纬度来自 `history2019.csv`（链家 2019 快照），原始未标注坐标系。经用上述 **已知 WGS84** 的
地铁站点做锚点比对（中关村、望京、西直门、国贸等站与最近小区偏差多为 0.02–0.2 km，且无
GCJ02 应有的约 +0.0055° 经度系统偏移），判定小区坐标与 WGS84 兼容，故按 `wgs84` 计算。
若后续发现小区坐标实为 GCJ02，应在导入时改用 `--community-crs gcj02`，无需改动代码。
