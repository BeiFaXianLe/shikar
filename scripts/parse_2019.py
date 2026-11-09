# -*- coding: utf-8 -*-
"""从公开数据集（链家 2019 全量小区）解析小区主数据，**保留经纬度**。
输出 data/history2019.csv：city,district,plate,xiaoqu_id,xiaoqu_name,price,week,source,lng,lat
"""
import csv, os, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "history2019.csv")
PROXY = os.environ.get("FANG_PROXY", "http://127.0.0.1:7897")
URLS = {
    "北京": "https://raw.githubusercontent.com/shuijingoj/LianJiaCommunityData/master/community_data_2019.02/bj/baseinfo.csv",
    "天津": "https://raw.githubusercontent.com/shuijingoj/LianJiaCommunityData/master/community_data_2019.02/tj/baseinfo.csv",
}
COLS = ["city", "district", "plate", "xiaoqu_id", "xiaoqu_name", "price", "week", "source", "lng", "lat"]

_op = urllib.request.build_opener(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))


def main():
    total = 0
    valid = 0
    with open(OUT, "w", encoding="utf-8-sig", newline="") as fo:
        w = csv.writer(fo)
        w.writerow(COLS)
        for city, url in URLS.items():
            with _op.open(url, timeout=90) as f:
                raw = f.read().decode("utf-8-sig", "replace")
            lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            for r in csv.DictReader(lines, delimiter=";"):
                name = (r.get("community_name") or "").strip()
                cid = (r.get("community_id") or "").strip()
                if not name or not cid:
                    continue
                hier3 = (r.get("hier3") or "").strip()
                hier4 = (r.get("hier4") or "").strip()
                district = hier3[:-2] if hier3.endswith("小区") else hier3
                plate = hier4[:-2] if hier4.endswith("小区") else hier4
                lng = (r.get("longitude") or "").strip()
                lat = (r.get("latitude") or "").strip()
                ok = False
                try:
                    ok = 73.0 <= float(lng) <= 136.0 and 3.0 <= float(lat) <= 54.0
                except Exception:
                    ok = False
                if ok:
                    valid += 1
                else:
                    lng = lat = ""
                w.writerow([city, district, plate, cid, name, (r.get("unit_price") or "").strip(),
                            "2019-02", "lianjia2019", lng, lat])
                total += 1
    print(f"rows={total} with_coords={valid} -> {OUT}")


if __name__ == "__main__":
    main()
