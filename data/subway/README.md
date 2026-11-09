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

### 同对象坐标样本与误差（P2-06）

取最近小区与已知 WGS84 地铁站的球面距离，对比「小区按 WGS84 直接计算」与「若小区实为 GCJ02
先转 WGS84 再算」两种假设：

| 车站(WGS84) | 最近小区 | 距离(按 WGS84) km | 距离(若小区实为 GCJ02) km |
|---|---|---|---|
| 中关村 | 海淀南路31号院 | 0.074 | 0.544 |
| 西直门 | 车公庄中里 | 0.123 | 0.656 |
| 望京 | 望京西路53号 | 0.025 | 0.522 |
| 国贸 | 双花园南里一区 | 0.222 | 0.646 |

左列均为 0.02–0.22 km，符合「小区本就在站点附近」的常识；若小区实为 GCJ02，转算后会系统性
多加约 0.5 km（右列），与「最近小区」事实不符。因此判定两图层同为 WGS84。

### 上游版本与采集（P2-06）

- 数据仓库：`github.com/luxueyan/generate-china-subways-geojson`，提交
  `5e9c60e74f4681375e319f1ca9c3039e1e978de8`（提交时间 2020-12-07），ISC 许可。
- 该工具用 `gcoord` 将高德 GCJ02 纠偏为 WGS84 后导出；本仓库据此适配，坐标系显式声明、不猜测。

### 换乘关系重建（P1-01）

上游「站点文件」按坐标去重、每站只保留一个线路简称，**丢失换乘关系**。本仓库适配脚本
`prepare_subway_geojson.py` 改为按「站点到每条线路折线的距离 ≤ 50m」重建站点归属：由于站点坐标
本就落在其线路折线上（实测站点到所属折线 0–20m，非所属线路通常 >200m），换乘站会自然关联到
经过它的多条线路。实测重建后：西直门=4/13/2 号线、国贸=10/1 号线、望京=14 东/15 号线、天津站
=2/3/9 号线，共 77 个换乘站（`shikar-subway.json` 中 `line_ids` 含多条线路的站点数）。
