# -*- coding: utf-8 -*-
"""随机采样房天下「小区详情页」的最新历史成交（公开页，无需登录），并合并追加到 CSV。

每个小区详情页 m.fang.com/xiaoqu/{bj|tj}/{id}.html 展示最新约 4 条历史成交
（签约时间、总价万、单价元/㎡）。本脚本：随机抽样 + 随机间隔 + 少量并发，
遇异常即跳过；仅访问公开页、不使用任何账号。

**合并模式**：若 --out 已存在，则把新记录并入（按 小区+月+价格+总价 去重），
不会覆盖以往采样的数据 → 支持每周多次运行持续累积。

用法：
  python scripts/sample_fang_preview.py --city bj --n 300
输出：data/fang_preview_sample.csv（默认）
"""
import argparse, concurrent.futures as cf, csv, os, random, re, threading, time, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FANG_CSV = os.path.join(ROOT, "data", "fang_2026.csv")
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1")
TAG = re.compile(r"<[^>]+>")
REC = re.compile(r"签约时间：(\d{4}-\d{2}-\d{2})\s*([\d.]+)\s*万\s*(\d[\d,]*)\s*元/㎡")
FIELDS = ["city", "district", "plate", "xiaoqu_id", "xiaoqu_name", "week", "price", "total_wan", "source"]

_lock = threading.Lock()
_counter = {"i": 0, "ok": 0}


def load_communities(city_code):
    city = {"bj": "北京", "tj": "天津"}[city_code]
    rows = []
    with open(FANG_CSV, "r", encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r.get("city") == city and r.get("xiaoqu_id"):
                rows.append(r)
    return rows


def load_existing(out):
    d = {}
    if os.path.exists(out):
        with open(out, "r", encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                k = (r.get("xiaoqu_id"), r.get("week"), str(r.get("price")), str(r.get("total_wan")))
                d[k] = r
    return d


def fetch(city_code, cid, tries=2):
    url = f"https://m.fang.com/xiaoqu/{city_code}/{cid}.html"
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:
            if i == tries - 1:
                return ""
            time.sleep(2 + random.random() * 2)
    return ""


def parse(html):
    text = re.sub(r"\s+", " ", TAG.sub(" ", html or ""))
    out = []
    for m in REC.finditer(text):
        try:
            out.append((m.group(1), float(m.group(2)), int(float(m.group(3).replace(",", "")))))
        except ValueError:
            continue
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", choices=["bj", "tj"], default="bj")
    ap.add_argument("--n", type=int, default=25)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--dmin", type=float, default=0.5)
    ap.add_argument("--dmax", type=float, default=1.6)
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "fang_preview_sample.csv"))
    a = ap.parse_args()
    if a.seed is not None:
        random.seed(a.seed)
    rows = load_communities(a.city)
    random.shuffle(rows)
    sample = rows[: a.n]
    city_cn = {"bj": "北京", "tj": "天津"}[a.city]
    existing = load_existing(a.out)
    before = len(existing)
    print(f"随机采样 {len(sample)}/{len(rows)} 小区（{city_cn}），并发 {a.workers}，已有 {before} 行", flush=True)

    def work(r):
        time.sleep(a.dmin + random.random() * (a.dmax - a.dmin))
        return r, parse(fetch(a.city, r["xiaoqu_id"]))

    new_rows = []
    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = [ex.submit(work, r) for r in sample]
        for fut in cf.as_completed(futs):
            r, recs = fut.result()
            with _lock:
                _counter["i"] += 1
                if recs:
                    _counter["ok"] += 1
                for d, total, unit in recs:
                    new_rows.append({"city": city_cn, "district": r["district"], "plate": r["plate"],
                                     "xiaoqu_id": f"{city_cn}|{r['xiaoqu_id']}", "xiaoqu_name": r["xiaoqu_name"],
                                     "week": d[:7], "price": unit, "total_wan": total, "source": "fang_preview"})
                if _counter["i"] % 50 == 0:
                    print(f"  ...{_counter['i']}/{len(sample)}", flush=True)

    for r in new_rows:
        k = (r["xiaoqu_id"], r["week"], str(r["price"]), str(r["total_wan"]))
        existing[k] = r
    merged = sorted(existing.values(), key=lambda r: (r["xiaoqu_id"], r["week"]))
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS); w.writeheader()
        for r in merged:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    added = len(existing) - before
    print(f"DONE 小区有成交 {_counter['ok']}/{len(sample)}，新增 {added} 行，累计 {len(existing)} 行 -> {a.out}", flush=True)


if __name__ == "__main__":
    main()
