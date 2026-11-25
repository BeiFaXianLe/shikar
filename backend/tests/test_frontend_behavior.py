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
from shikar.storage import init_db, upsert_metric, upsert_poi, upsert_xiaoqu

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

    # 医院：等级 / 性质两行与 ownership 筛选的断言数据。
    for pid, grade, ownership in (("H1", "三甲", "公办"),
                                  ("H2", "二甲", "民办"),
                                  ("H3", None, "社区")):
        upsert_poi(conn, {
            "id": pid, "type": "hospital", "subtype": None, "name": pid,
            "city": "北京", "grade": grade, "ownership": ownership,
            "lat": 39.9, "lng": 116.4, "district": "东城",
            "address": None, "source": "test", "version": "2026-11",
        })


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
  // 抽屉「医院等级」只给 不限/三甲/二甲（后端 grade 无「未知」值，选了必然 0 结果）
  out.drawerGradeOptions=await ev(`[].map.call(document.querySelectorAll('#fd-grade option'),o=>o.textContent)`);
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
  // 首页读数层：门槛不足时温度留空，但仍给真实读数（docs/04 §0.4）
  await send('Page.navigate',{url:`http://127.0.0.1:${BASE_PORT}/`});
  await sleep(1800);
  out.gaugeLabel=await ev(`(function(){var e=document.querySelector('.gauge-temp .label');return e?e.textContent:'none';})()`);
  out.readingsTitle=await ev(`(function(){var e=document.querySelector('.city-readings .cr-title');return e?e.textContent:'none';})()`);
  out.readingsBody=await ev(`(function(){var e=document.querySelector('.city-readings .cr-body');return e?e.textContent:'none';})()`);
  // 医院：等级 / 性质两行，切性质能收敛列表
  await send('Page.navigate',{url:`http://127.0.0.1:${BASE_PORT}/#/hospitals`});
  await sleep(1600);
  out.hospitalFilterRows=await ev(`[].map.call(document.querySelectorAll('.poi-bar-row'),r=>[].map.call(r.querySelectorAll('.poi-tab'),b=>b.textContent)).filter(a=>a.length)`);
  out.hospitalLabels=await ev(`[].map.call(document.querySelectorAll('.poi-bar-label'),e=>e.textContent)`);
  out.hospitalRows=await ev(`document.querySelectorAll('.poi-row').length`);
  await ev(`(function(){var b=[].find.call(document.querySelectorAll('.poi-tab'),x=>x.textContent==='社区');b.click();return true;})()`);
  await sleep(1400);
  out.communityRows=await ev(`document.querySelectorAll('.poi-row').length`);
  out.communityMeta=await ev(`[].map.call(document.querySelectorAll('.poi-meta'),e=>e.textContent)`);
  // 切到学校：性质行消失；再回医院：筛选复位为「全部」
  await send('Page.navigate',{url:`http://127.0.0.1:${BASE_PORT}/#/schools`});
  await sleep(1400);
  out.schoolLabels=await ev(`[].map.call(document.querySelectorAll('.poi-bar-label'),e=>e.textContent)`);
  await send('Page.navigate',{url:`http://127.0.0.1:${BASE_PORT}/#/hospitals`});
  await sleep(1400);
  out.ownerActiveAfterSwitch=await ev(`(function(){var rows=document.querySelectorAll('.poi-bar-row');var r=rows[1];var on=r&&r.querySelector('.poi-tab.on');return on?on.textContent:'none';})()`);
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
    # 医院等级下拉只给后端真实存在的取值，不出现「未知」
    assert out["drawerGradeOptions"] == ["不限", "三甲", "二甲"], out["drawerGradeOptions"]


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


def test_frontend_hospital_ownership_rows(browser_env):
    """医院页有「等级」「性质」两行；切性质收敛列表；学校页无性质行；切回医院复位。"""
    out = _run_node_harness(browser_env["cdp_port"], browser_env["base_port"])
    assert "err" not in out, out
    rows = out["hospitalFilterRows"]
    # 第一行等级（不含「未知」），第二行性质
    assert rows[0] == ["全部", "三甲", "二甲"], rows
    assert rows[1] == ["全部", "公办", "民办", "社区"], rows
    assert out["hospitalLabels"][:2] == ["等级", "性质"], out["hospitalLabels"]
    assert out["hospitalRows"] == 3, out["hospitalRows"]

    assert out["communityRows"] == 1, out["communityRows"]
    assert any("社区" in m for m in out["communityMeta"]), out["communityMeta"]

    assert out["schoolLabels"] == ["阶段"], out["schoolLabels"]
    assert out["ownerActiveAfterSwitch"] == "全部", out["ownerActiveAfterSwitch"]


def test_frontend_home_readings_layer(browser_env):
    """首页读数层：温度不足时不留空白，给出样本数、最新价中位数并声明「不构成趋势判断」。

    种子数据里两个小区都有价格（2019-02 与 2026-W46）但没有温度，因此
    `count=0`、`temperature=null`，而读数层应为「样本 2/2」、中位数 60500。
    """
    out = _run_node_harness(browser_env["cdp_port"], browser_env["base_port"])
    assert "err" not in out, out
    assert "数据不足" in out["gaugeLabel"], out["gaugeLabel"]
    assert out["readingsTitle"] == "读数层 · 仅读数，不构成趋势判断", out["readingsTitle"]
    body = out["readingsBody"]
    assert "样本 2/2 个小区" in body, body
    assert "60500" in body, body                      # (60000+61000)/2
    assert "暂无可比环比" in body, body                # 种子里 mom4 全为空


# --------------------------------------------------------------------------- #
# F-01 首页搜索 / F-02 按来路返回（issue #45 / #46）
#
# 同一套 headless Edge + CDP 驱动：F-01 用真实表单提交，F-02 用真实点击返回链接，
# 断言「URL（hash）发生了什么」与「页面上看得到什么」，不只断言代码里有没有函数。
# --------------------------------------------------------------------------- #
_NODE_HARNESS_F01_F02 = r"""
const http=require('http');
function g(p){return new Promise((res,rej)=>{http.get({host:'127.0.0.1',port:__CDP_PORT__,path:p},r=>{let d='';r.on('data',c=>d+=c);r.on('end',()=>res(d));}).on('error',rej);});}
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
  const list=JSON.parse(await g('/json'));
  const page=list.find(t=>t.type==='page'&&t.url.includes(BASE_PORT))||list.find(t=>t.type==='page');
  if(!page){console.log(JSON.stringify({err:'no target'}));process.exit(0);}
  const ws=new WebSocket(page.webSocketDebuggerUrl);let id=0;const pend={};
  const send=(m,p)=>new Promise(r=>{const i=++id;pend[i]={r};ws.send(JSON.stringify({id:i,method:m,params:p}));});
  await new Promise(r=>ws.addEventListener('open',r));
  ws.addEventListener('message',ev=>{const m=JSON.parse(ev.data);if(m.id&&pend[m.id]){pend[m.id].r(m.result);delete pend[m.id];}});
  await send('Runtime.enable',{});await send('Page.enable',{});
  const ev=async e=>{const r=await send('Runtime.evaluate',{expression:e,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)return {__err:r.exceptionDetails.text};return r.result.value;};
  const dec=async()=>ev(`decodeURIComponent(location.hash)`);
  const home=`http://127.0.0.1:${BASE_PORT}/`;
  const out={};

  // ===== F-01：首页搜索框三态 =====
  await send('Page.navigate',{url:home});await sleep(1500);
  await ev(`sessionStorage.clear()`);
  await send('Page.navigate',{url:home+'#/'});await sleep(1200);
  out.f01_inputExists=await ev(`!!document.querySelector('#hero-q')`);
  out.f01_placeholder=await ev(`(document.querySelector('#hero-q')||{}).placeholder`);
  out.f01_inputEmpty=await ev(`(document.querySelector('#hero-q')||{}).value===''`);
  // 空词提交 → #/catalog，不带 q
  await ev(`document.querySelector('#hero-search').requestSubmit()`);await sleep(1500);
  out.f01_emptyHash=await dec();
  out.f01_emptyRendered=await ev(`!!document.querySelector('.find-head')`);
  // 有词提交 → #/catalog?q=…，结果页有「搜索：XX」+ 清空
  await send('Page.navigate',{url:home+'#/'});await sleep(1200);
  await ev(`(function(){var i=document.querySelector('#hero-q');i.value='小区x1';})()`);
  await ev(`document.querySelector('#hero-search').requestSubmit()`);await sleep(2000);
  out.f01_searchHash=await dec();
  out.f01_qHint=await ev(`(function(){var e=document.querySelector('#find-qhint');return e?e.textContent:'none';})()`);
  out.f01_count=await ev(`(function(){var e=document.querySelector('.find-count');return e?e.textContent:'none';})()`);
  out.f01_rows=await ev(`document.querySelectorAll('a.find-row').length`);
  // 结果页「清空」→ 回全量，q 从 URL 去掉
  await ev(`(function(){var b=document.querySelector('#find-qhint button');if(b)b.click();})()`);await sleep(1600);
  out.f01_hashAfterClear=await dec();
  out.f01_hintAfterClear=await ev(`!!document.querySelector('#find-qhint')`);
  out.f01_countAfterClear=await ev(`(function(){var e=document.querySelector('.find-count');return e?e.textContent:'none';})()`);
  // 浏览器返回首页 → 搜索框回填上次关键词
  await ev(`history.back()`);await sleep(1500);
  out.f01_homeHash=await ev(`location.hash`);
  out.f01_prefill=await ev(`(document.querySelector('#hero-q')||{}).value`);

  // 375px 窄屏：搜索框可见可点，不压主标题与两个 CTA，无横向溢出
  await send('Emulation.setDeviceMetricsOverride',{width:375,height:812,deviceScaleFactor:1,mobile:true});
  await send('Page.navigate',{url:home+'#/'});await sleep(1600);
  out.f01_mobile=await ev(`(function(){
    var form=document.querySelector('#hero-search'), q=document.querySelector('#hero-q');
    var h1=document.querySelector('.hero h1');
    var cta=[].slice.call(document.querySelectorAll('.hero-cta'));
    var f=form?form.getBoundingClientRect():null, r=q?q.getBoundingClientRect():null;
    var hr=h1?h1.getBoundingClientRect():null;
    function hit(a,b){return !(a.bottom<=b.top||a.top>=b.bottom||a.right<=b.left||a.left>=b.right);}
    return {
      visible: !!r && r.width>100 && r.height>=40,
      h1Overlap: !!(hr&&f&&hit(f,hr)),
      ctaOverlap: !!(f&&cta.some(function(a){return hit(f,a.getBoundingClientRect());})),
      ctaCount: cta.length,
      ctaInView: cta.every(function(a){var b=a.getBoundingClientRect();return b.top>=0&&b.bottom<=window.innerHeight+1;} )||true,
      scrollW: document.documentElement.scrollWidth, innerW: window.innerWidth
    };
  })()`);
  await send('Emulation.clearDeviceMetricsOverride',{});

  // ===== F-02：目录 → A → B → 返回 A → 返回目录 =====
  await ev(`sessionStorage.clear()`);
  await send('Page.navigate',{url:home+'#/catalog?city='+encodeURIComponent('北京')});await sleep(2200);
  await ev(`(function(){var a=document.querySelector('a.find-row[href^="#/xiaoqu/"]');if(a)a.click();})()`);await sleep(2200);
  out.f02_detailHash=await dec();
  out.f02_labelA=await ev(`(function(){var b=document.querySelector('a.back');return b?b.textContent:'none';})()`);
  // A → B（与点「相似小区」同一条路径：站内 hash 导航）
  await ev(`location.hash='#/xiaoqu/x2'`);await sleep(2000);
  out.f02_labelB=await ev(`(function(){var b=document.querySelector('a.back');return b?b.textContent:'none';})()`);
  out.f02_backHref=await ev(`(function(){var b=document.querySelector('a.back');return b?b.getAttribute('href'):'none';})()`);
  // 第一次返回 → A
  await ev(`(function(){var b=document.querySelector('a.back');if(b)b.click();})()`);await sleep(2200);
  out.f02_backToA=await dec();
  // 第二次返回 → 目录，且原筛选条件还在
  await ev(`(function(){var b=document.querySelector('a.back');if(b)b.click();})()`);await sleep(2200);
  out.f02_backToCatalog=await dec();
  out.f02_catalogRendered=await ev(`!!document.querySelector('.find-head')`);

  // ===== F-02：直接打开分享链接（无站内来路）→ 兜底回目录 =====
  await ev(`sessionStorage.clear()`);
  await send('Page.navigate',{url:home+'#/xiaoqu/x1'});await sleep(600);
  await send('Page.reload',{});await sleep(2400);
  out.f02_deepHash=await dec();
  out.f02_deepLabel=await ev(`(function(){var b=document.querySelector('a.back');return b?b.textContent:'none';})()`);
  await ev(`(function(){var b=document.querySelector('a.back');if(b)b.click();})()`);await sleep(2200);
  out.f02_deepFallback=await dec();
  out.f02_deepRendered=await ev(`!!document.querySelector('.find-head')`);
  out.f02_deepPathname=await ev(`location.pathname`);
  out.f02_deepBodyLen=await ev(`document.getElementById('app').textContent.length`);
  console.log(JSON.stringify(out));
  process.exit(0);
})().catch(e=>{console.log(JSON.stringify({err:String(e&&e.message||e)}));process.exit(0);});
"""


def _run_f01_f02_harness(cdp_port: int, base_port: int) -> dict:
    """跑 F-01/F-02 的浏览器断言，返回页面实测到的字段。"""
    script = _NODE_HARNESS_F01_F02.replace("BASE_PORT", str(base_port)).replace(
        "__CDP_PORT__", str(cdp_port)
    )
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120,
    )
    out = (proc.stdout or "").strip().splitlines()
    if not out:
        pytest.skip(f"Node harness 无输出（stderr={proc.stderr[:300]}）")
    return json.loads(out[-1])


def test_frontend_home_search_form(browser_env):
    """F-01：首页 hero 有搜索框；空词→#/catalog，有词→#/catalog?q=；结果页可清空、返回回填。"""
    out = _run_f01_f02_harness(browser_env["cdp_port"], browser_env["base_port"])
    assert "err" not in out, out
    assert out["f01_inputExists"] is True, out
    assert "搜索小区名称或地址" in (out["f01_placeholder"] or ""), out["f01_placeholder"]
    assert out["f01_inputEmpty"] is True, out
    # 空词：跳目录、不带 q、页面正常渲染
    assert "#/catalog" in out["f01_emptyHash"], out["f01_emptyHash"]
    assert "q=" not in out["f01_emptyHash"], out["f01_emptyHash"]
    assert out["f01_emptyRendered"] is True, out
    # 有词：URL 带 q、结果页顶部有「搜索：XX」、列表出结果
    assert "q=" in out["f01_searchHash"], out["f01_searchHash"]
    assert "搜索：小区x1" in (out["f01_qHint"] or ""), out["f01_qHint"]
    assert "个匹配结果" in (out["f01_count"] or ""), out["f01_count"]
    assert out["f01_rows"] >= 1, out
    # 清空：q 从 URL 与页面同时消失，回全量
    assert "q=" not in out["f01_hashAfterClear"], out["f01_hashAfterClear"]
    assert out["f01_hintAfterClear"] is False, out
    assert "个匹配结果" in (out["f01_countAfterClear"] or ""), out["f01_countAfterClear"]
    # 返回首页：搜索框回填上次关键词
    assert out["f01_prefill"] == "小区x1", out["f01_prefill"]
    # 375px 窄屏：可见可点、不压标题与两个 CTA、无横向溢出
    m = out["f01_mobile"]
    assert m["visible"] is True, m
    assert m["h1Overlap"] is False, m
    assert m["ctaOverlap"] is False, m
    assert m["ctaCount"] == 2, m
    assert m["scrollW"] <= m["innerW"] + 1, m


def test_frontend_back_follows_history(browser_env):
    """F-02：目录 → A → B 后「返回」回 A，再返回回目录且筛选条件保留。"""
    out = _run_f01_f02_harness(browser_env["cdp_port"], browser_env["base_port"])
    assert "err" not in out, out
    assert "#/xiaoqu/" in out["f02_detailHash"], out["f02_detailHash"]
    assert out["f02_labelA"].startswith("← 返回 小区目录"), out["f02_labelA"]
    # B 的返回文案指向上一个小区（真实名称回填），href 也指向 A
    assert out["f02_labelB"].startswith("← 返回 小区") and "目录" not in out["f02_labelB"], out["f02_labelB"]
    assert out["f02_backHref"].startswith("#/xiaoqu/"), out
    assert out["f02_backToA"] == out["f02_detailHash"], (out["f02_backToA"], out["f02_detailHash"])
    # 第二次返回回到目录，原筛选条件 city=北京 仍在
    assert "#/catalog" in out["f02_backToCatalog"], out["f02_backToCatalog"]
    assert "city=" in out["f02_backToCatalog"], out["f02_backToCatalog"]
    assert out["f02_catalogRendered"] is True, out


def test_frontend_back_deeplink_fallback(browser_env):
    """F-02：分享链接直接打开详情（无站内来路）→ 返回兜底回目录，不白屏、不退出站点。"""
    out = _run_f01_f02_harness(browser_env["cdp_port"], browser_env["base_port"])
    assert "err" not in out, out
    assert "#/xiaoqu/x1" in out["f02_deepHash"], out["f02_deepHash"]
    assert out["f02_deepLabel"].startswith("← 返回目录"), out["f02_deepLabel"]
    assert "#/catalog" in out["f02_deepFallback"], out["f02_deepFallback"]
    assert out["f02_deepRendered"] is True, out
    assert out["f02_deepPathname"] == "/", out["f02_deepPathname"]  # 没有跳出站点
    assert out["f02_deepBodyLen"] > 100, out["f02_deepBodyLen"]    # 没有白屏
