# -*- coding: utf-8 -*-
"""按页抓取房天下公开小区聚合数据，合并写入 data/fang_2026.csv（可续跑、去重）。
仅公开聚合指标，低频、限量，不绕过验证。个人自用。

用法示例：
  python scripts/fetch_fang.py --city bj --pages 100 --delay 1.5
  python scripts/fetch_fang.py --city tj --pages 100 --delay 1.5
"""
import argparse, csv, datetime, os, re, sys, time, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "fang_2026.csv")
PROXY = os.environ.get("FANG_PROXY", "").strip()
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1")
CITIES = {"bj": "北京", "tj": "天津"}
ITEM_RE = re.compile(r'<li dataflag="bg" data-bg="\{&quot;newcode&quot;:&quot;(\d+)&quot;\}">(.*?)</li>', re.S)
FIELDS = ["city","district","plate","xiaoqu_id","xiaoqu_name","week","price","supply","built_year","mom1_pct","source"]


def opener():
    if PROXY:
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    return urllib.request.build_opener()


def _iso_week(d=None):
    """返回 ISO 8601 周次 "YYYY-Www"（ISO 年 + ISO 周号，跨年周归属 ISO 年）。"""
    iso = (d or datetime.date.today()).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def get(op, url, ref):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": ref})
    with op.open(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def parse(html):
    out = []
    for m in ITEM_RE.finditer(html):
        newcode, body = m.group(1), m.group(2)
        name = re.search(r'<h3>(.*?)</h3>', body, re.S)
        dp = re.search(r'<span class="">(.*?)</span>', body, re.S)
        year = re.search(r'(\d{4})年建造', body)
        supply = re.search(r'二手房(\d+)套', body)
        price = re.search(r'<em>(\d+)</em>元/平', body)
        chg = re.search(r'jt-(up|dn)-n">([\d.]+)%', body)
        if not name or not price:
            continue
        dpt = dp.group(1).strip() if dp else ""
        _, _, plate = dpt.partition("-")
        change = ""
        if chg:
            v = float(chg.group(2)); change = v if chg.group(1) == "up" else -v
        out.append({"xiaoqu_id": newcode, "xiaoqu_name": re.sub(r"<.*?>", "", name.group(1)).strip(),
                    "plate": plate.strip(), "built_year": year.group(1) if year else "",
                    "supply": supply.group(1) if supply else "", "price": price.group(1), "mom1_pct": change})
    return out


def load_existing():
    rows = {}
    if os.path.exists(OUT):
        with open(OUT, "r", encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                # 键必须含 week：否则同一小区后续周次的抓取会被当成重复而丢弃，
                # CSV 永远停在第 1 期，周更数据从未真正累积。
                rows[(r["city"], r["xiaoqu_id"], r.get("week", ""))] = r
    return rows


def merge_rows(rows, items, cname, week):
    """把一批抓取结果并入 ``rows``（键含 city/xiaoqu_id/week），返回新增行数。

    同一周重跑幂等（同键跳过）；跨周自动追加新行。
    """
    added = 0
    for it in items:
        key = (cname, it["xiaoqu_id"], week)
        if key in rows:
            continue
        it.update({"city": cname, "week": week, "source": "fang.com"})
        rows[key] = {k: it.get(k, "") for k in FIELDS}
        added += 1
    return added


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", choices=["bj", "tj"], required=True)
    ap.add_argument("--pages", type=int, default=100)
    ap.add_argument("--delay", type=float, default=1.5)
    a = ap.parse_args()
    cname = CITIES[a.city]; week = _iso_week()
    rows = load_existing(); before = len(rows); added = 0
    op = opener()
    for page in range(1, a.pages + 1):
        try:
            html = get(op, f"https://m.fang.com/fangjia/{a.city}_list_ajax/?page={page}",
                       f"https://m.fang.com/fangjia/{a.city}_list_pinggu/")
        except Exception as e:
            print(f"page {page} ERR {e}", flush=True); break
        items = parse(html)
        if not items:
            print(f"page {page} empty/blocked, stop", flush=True); break
        added += merge_rows(rows, items, cname, week)
        time.sleep(a.delay)
        if page % 20 == 0:
            print(f"{cname} page {page} added={added} total={len(rows)}", flush=True)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS); w.writeheader()
        for r in rows.values(): w.writerow({k: r.get(k, "") for k in FIELDS})
    print(f"before={before} added={added} total={len(rows)} -> {OUT}", flush=True)


if __name__ == "__main__":
    main()
