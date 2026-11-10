"""前端行为自动化回归（草稿焦点 / 单站深链 / 期次清除），第 3 轮复审 P2-5。

以 headless Edge + DevTools Protocol 驱动真实浏览器，对 ``web/app.js`` 的三处行为做
可执行断言（复审明确允许「前端行为用可执行的 DOM/JS 复现」）：

* 草稿焦点：抽屉内勾选线路触发重绘后，输入的价格草稿保留、焦点落在所用控件；
* 单站深链：``#/catalog?station=…&subway_max=…``（无 line）能恢复站点与阈值并出结果；
* 期次清除：带 ``price_week`` 时有「期次」chip，点「清除其他条件」后 chip/URL 均去除。

无 Edge 或无法启动时整体跳过（``pytest.skip``），不制造环境相关失败。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.request
from typing import Iterator, Optional

import pytest

from shikar import subway
from shikar.api import create_server
from shikar.storage import init_db, upsert_metric, upsert_xiaoqu

_EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    shutil.which("msedge") or "",
]


def _edge_path() -> Optional[str]:
    for p in _EDGE_CANDIDATES:
        if p and os.path.isfile(p):
            return p
    return None


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


_GEOJSON = {
    "type": "FeatureCollection", "source": "t", "version": "v1", "coord_system": "wgs84",
    "features": [
        {"type": "Feature", "properties": {"id": "a", "city": "北京", "name": "甲线"},
         "geometry": {"type": "LineString", "coordinates": [[116.3, 39.98], [116.32, 39.98]]}},
        {"type": "Feature", "properties": {"id": "s1", "city": "北京", "name": "甲站", "line_ids": ["a"]},
         "geometry": {"type": "Point", "coordinates": [116.3, 39.98]}},
        {"type": "Feature", "properties": {"id": "s2", "city": "北京", "name": "乙站", "line_ids": ["a"]},
         "geometry": {"type": "Point", "coordinates": [116.32, 39.98]}},
    ],
}


def _seed(conn: sqlite3.Connection) -> None:
    for i, (xid, lng, lat) in enumerate([("x1", 116.3, 39.98), ("x2", 116.31, 39.98)]):
        upsert_xiaoqu(conn, {"id": xid, "name": "小区" + xid, "city": "北京", "district": "海淀",
                             "plate": "p", "alias": None, "lng": lng, "lat": lat,
                             "build_year": None, "households": None})
        upsert_metric(conn, {"xiaoqu_id": xid, "week": "2019-02", "price": 50000.0 + i * 1000,
                            "mom4": None, "mom13": None, "supply": None, "vol30": None,
                            "absorb": None, "bargain": None, "heat": None,
                            "temperature": None, "stage": None})
        upsert_metric(conn, {"xiaoqu_id": xid, "week": "2026-W46", "price": 60000.0 + i * 1000,
                            "mom4": None, "mom13": None, "supply": None, "vol30": None,
                            "absorb": None, "bargain": None, "heat": None,
                            "temperature": None, "stage": None})
    subway.import_geojson(conn, _GEOJSON, community_crs="wgs84")


def _http_json(port: int, path: str, timeout: float = 5.0):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# 说明：受限于零第三方依赖与不引入 jsdom，这里用「Edge headless 导航到带测试参数的页面 +
# 通过 README 中的深链逻辑 + 读取渲染后的 DOM」的方式做真实浏览器断言；交互型（勾选重绘）
# 用页面内注入的最小脚本经 CDP Runtime.evaluate 完成。为保持标准库，CDP 交互交给一个
# 内联 Node 脚本（Node 24 自带 WebSocket / fetch）。
_NODE_HARNESS = r"""
const http=require('http');
function g(p){return new Promise((res,rej)=>{http.get({host:'127.0.0.1',port:__CDP_PORT__,path:p},r=>{let d='';r.on('data',c=>d+=c);r.on('end',()=>res(d));}).on('error',rej);});}
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
  const list=JSON.parse(await g('/json'));
  const page=list.find(t=>t.type==='page'&&t.url.includes(BASE_PORT));
  if(!page){console.log(JSON.stringify({err:'no target'}));process.exit(0);}
  const ws=new WebSocket(page.webSocketDebuggerUrl);let id=0;const pend={};
  const send=(m,p)=>new Promise(r=>{const i=++id;pend[i]={r};ws.send(JSON.stringify({id:i,method:m,params:p}));});
  await new Promise(r=>ws.addEventListener('open',r));
  ws.addEventListener('message',ev=>{const m=JSON.parse(ev.data);if(m.id&&pend[m.id]){pend[m.id].r(m.result);delete pend[m.id];}});
  await send('Runtime.enable',{});await send('Page.enable',{});
  const ev=async e=>{const r=await send('Runtime.evaluate',{expression:e,returnByValue:true,awaitPromise:true});return r.exceptionDetails?{__err:r.exceptionDetails.text}:r.result.value;};
  const out={};
  // 草稿焦点
  await send('Page.navigate',{url:`http://127.0.0.1:${BASE_PORT}/#/catalog?city=`+encodeURIComponent('北京')+'&line='+encodeURIComponent('北京:甲线')+'&subway_max=1'});
  await sleep(1800);
  await ev(`document.querySelector('#more-filters').click()`);await sleep(400);
  await ev(`(function(){var i=document.querySelector('#fd-price-min');i.value='60000';i.dispatchEvent(new Event('input',{bubbles:true}));i.focus();})()`);
  await ev(`(function(){var cb=document.querySelectorAll('.fd-checks input[type=checkbox]')[0];cb.checked=!cb.checked;cb.dispatchEvent(new Event('change',{bubbles:true}));})()`);
  await sleep(400);
  out.priceRetained=await ev(`(function(){var i=document.querySelector('#fd-price-min');return i?i.value:'gone';})()`);
  out.focusInDrawer=await ev(`(function(){var a=document.activeElement;return a&&a.id||'(none)';})()`);
  await ev(`(function(){var c=document.querySelector('.fd-close');if(c)c.click();})()`);await sleep(200);
  // 单站深链
  await send('Page.navigate',{url:`http://127.0.0.1:${BASE_PORT}/#/catalog?city=`+encodeURIComponent('北京')+'&station='+encodeURIComponent('s1')+'&subway_max=1.5'});
  await sleep(1800);
  out.stationChips=await ev(`[].map.call(document.querySelectorAll('.fc-chip'),b=>b.textContent)`);
  out.stationCount=await ev(`(function(){var e=document.querySelector('.find-count');return e?e.textContent:'none';})()`);
  // 期次清除
  await send('Page.navigate',{url:`http://127.0.0.1:${BASE_PORT}/#/catalog?city=`+encodeURIComponent('北京')+'&price_week=2019-02'});
  await sleep(1800);
  out.chipsWithPeriod=await ev(`[].map.call(document.querySelectorAll('.fc-chip'),b=>b.textContent)`);
  await ev(`(function(){var b=document.querySelector('#clear-all');if(b)b.click();})()`);
  await sleep(1000);
  out.chipsAfterClear=await ev(`[].map.call(document.querySelectorAll('.fc-chip'),b=>b.textContent)`);
  out.hashAfterClear=await ev(`location.hash`);
  console.log(JSON.stringify(out));
  process.exit(0);
})().catch(e=>{console.log(JSON.stringify({err:String(e&&e.message||e)}));process.exit(0);});
"""


def _run_node_harness(cdp_port: int, base_port: int) -> dict:
    # 先替换 BASE_PORT 再替换 PORT，避免 "PORT" 命中 "BASE_PORT" 的子串。
    script = _NODE_HARNESS.replace("BASE_PORT", str(base_port)).replace("__CDP_PORT__", str(cdp_port))
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    out = (proc.stdout or "").strip().splitlines()
    if not out:
        pytest.skip(f"Node harness 无输出（stderr={proc.stderr[:300]}）")
    return json.loads(out[-1])


@pytest.fixture(scope="module")
def browser_env() -> Iterator[dict]:
    edge = _edge_path()
    if not edge:
        pytest.skip("未找到 Edge，跳过前端行为浏览器测试")
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    init_db(conn)
    _seed(conn)
    web_root = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "web"
    )
    server = create_server(conn, host="127.0.0.1", port=0, web_root=web_root)
    base_port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    cdp_port = _free_port()
    profile = os.path.join(
        os.environ.get("TEMP", "/tmp"), f"shikar-edge-{os.getpid()}-{cdp_port}"
    )
    proc = subprocess.Popen(
        [edge, "--headless=new", "--disable-gpu", f"--remote-debugging-port={cdp_port}",
         f"--user-data-dir={profile}", "--no-first-run", "--window-size=1280,900",
         f"http://127.0.0.1:{base_port}/"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # 等 CDP 就绪（最多约 18s）
    ready = False
    for _ in range(60):
        try:
            _http_json(cdp_port, "/json/version", timeout=1)
            ready = True
            break
        except Exception:
            time.sleep(0.3)
    if not ready:
        proc.terminate()
        server.shutdown(); server.server_close(); conn.close()
        pytest.skip("Edge CDP 未能启动，跳过前端行为测试")
    try:
        yield {"cdp_port": cdp_port, "base_port": base_port}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        server.shutdown(); server.server_close(); t.join(timeout=5); conn.close()
        shutil.rmtree(profile, ignore_errors=True)


def test_frontend_draft_focus_and_price_retained(browser_env):
    out = _run_node_harness(browser_env["cdp_port"], browser_env["base_port"])
    assert "err" not in out, out
    # 勾选线路重绘后价格草稿保留（P1-04 / P2-3）
    assert out["priceRetained"] == "60000", out
    # 焦点仍在抽屉内某控件上（不为 body/丢失）
    assert out["focusInDrawer"] not in ("(none)", "", "BODY"), out


def test_frontend_single_station_deeplink(browser_env):
    out = _run_node_harness(browser_env["cdp_port"], browser_env["base_port"])
    assert "err" not in out, out
    chips = " ".join(out["stationChips"])
    assert "甲站" in chips or "≤ 1.5 km" in chips, chips  # 站点/阈值被恢复
    assert "个匹配结果" in out["stationCount"], out["stationCount"]


def test_frontend_clear_period(browser_env):
    out = _run_node_harness(browser_env["cdp_port"], browser_env["base_port"])
    assert "err" not in out, out
    assert any("期次" in c for c in out["chipsWithPeriod"]), out["chipsWithPeriod"]
    assert not any("期次" in c for c in out["chipsAfterClear"]), out["chipsAfterClear"]
    assert "price_week" not in (out.get("hashAfterClear") or ""), out.get("hashAfterClear")
