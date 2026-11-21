"""形态 A 相似小区前端规则（docs/32）的 Node 可执行测试（A-TC2~7）。

``web/similar.js`` 是纯函数模块（无 DOM / 无网络 / 无依赖），直接用 Node 的 ``vm`` 加载后断言。
Node 不可用时整体跳过，不制造环境相关失败。
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SIMILAR = os.path.join(_REPO_ROOT, "web", "similar.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node 不可用")

_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const code = fs.readFileSync(process.argv[1], "utf8");
const sandbox = {};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
const S = sandbox.ShikarSimilar;
if (!S) throw new Error("ShikarSimilar 未导出");

function assert(cond, msg) { if (!cond) throw new Error(msg); }

const series = [
  { week: "2026-W01", price: 48000, temperature: null, stage: null },
  { week: "2026-W10", price: 49000, temperature: 40.0, stage: "温和" },
  { week: "2026-W20", price: 49500, temperature: 46.0, stage: "温和" },
  { week: "2026-W30", price: 50000, temperature: 50.0, stage: "温和" },
];
const detail = { id: "X", name: "本小区", city: "北京", district: "朝阳", plate: "望京" };
const ctx = S.buildContext(detail, series);
assert(ctx.city === "北京", "TC2 ctx.city");
assert(ctx.price === 50000, "ctx.price");
assert(ctx.temperature === 50.0, "ctx.temperature");
assert(ctx.sufficient === true, "ctx.sufficient");

const items = [
  { id: "A", name: "A", city: "北京", district: "朝阳", data_points: 4, temperature: 51.0, latest_price: 50500, latest_week: "2026-W30", stage: "温和" },
  { id: "B", name: "B", city: "天津", district: "和平", data_points: 4, temperature: 50.0, latest_price: 50000, latest_week: "2026-W30" },
  { id: "C", name: "C", city: "北京", district: "朝阳", data_points: 1, temperature: 50.0, latest_price: 50000, latest_week: "2026-W30" },
  { id: "D", name: "D", city: "北京", district: "朝阳", data_points: 4, temperature: 50.0, latest_price: 70000, latest_week: "2026-W30" },
  { id: "E", name: "E", city: "北京", district: "朝阳", data_points: 4, temperature: 50.0, latest_price: 50000, latest_week: "2026-W30" },
  { id: "X", name: "本小区", city: "北京", district: "朝阳", data_points: 4, temperature: 50.0, latest_price: 50000, latest_week: "2026-W30" },
  { id: "F", name: "F", city: "北京", district: "朝阳", data_points: 4, temperature: 40.0, latest_price: 50000, latest_week: "2026-W30", stage: "冰点" },
];
const kept = S.recommend(items, ctx, { band: S.DEFAULT_BAND });
const ids = kept.map(i => i.id);
assert(ids.indexOf("B") < 0, "TC2 跨城候选未排除");
assert(ids.indexOf("C") < 0, "TC4 data_points<2 未排除");
assert(ids.indexOf("D") < 0, "TC3 价带外候选未排除");
assert(ids.indexOf("X") < 0, "TC5 未排除自身");
assert(ids.indexOf("A") >= 0 && ids.indexOf("F") >= 0, "TC3/4 合格候选被误杀");
assert(ids.indexOf("E") < ids.indexOf("A"), "TC6 温度差为 0 的候选应排最前");
assert(ids.indexOf("A") < ids.indexOf("F"), "TC6 温度差排序不正确");
assert(kept.length <= S.MAX_RESULTS, "推荐数量超过上限");

const r = S.reason(kept.filter(i => i.id === "A")[0], ctx);
assert(r.indexOf("51.0") >= 0, "TC7 理由缺温度数值");
assert(r.indexOf("本小区") >= 0, "TC7 理由缺当前小区名");
assert(r.indexOf("期次 2026-W30") >= 0, "TC7 理由缺期次");
assert(r.indexOf("同处「温和」阶段") >= 0, "TC7 理由缺阶段");

const sparseCtx = S.buildContext(detail, [{ week: "2026-W30", price: 50000, temperature: 50.0 }]);
assert(sparseCtx.sufficient === false, "dp<2 应降级");
const sr = S.reason({ id: "A", latest_price: 48000, latest_week: "2026-W30", temperature: 50.0 }, sparseCtx);
assert(sr.indexOf("温度") < 0, "降级时理由不得出现温度话术");
assert(sr.indexOf("低 4.0%") >= 0, "降级时理由应给出真实价格差");

const q = S.queryFor(ctx, {});
assert(q.city === "北京" && q.price_week === "2026-W30", "queryFor 缺少 city/price_week");
assert(q.stage === "温和", "充分时应带 stage");
const sparseQ = S.queryFor(sparseCtx, {});
assert(sparseQ.stage === undefined && sparseQ.district === "朝阳", "降级时应按同区且不带 stage");

console.log(JSON.stringify({ ok: true, kept: ids }));
"""


def _run_harness() -> dict:
    proc = subprocess.run(
        ["node", "-e", _HARNESS, _SIMILAR],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert proc.returncode == 0, f"node 断言失败：{proc.stderr or proc.stdout}"
    return proc.stdout


def test_similar_rules_executable() -> None:
    """A-TC2~7 与降级口径：Node 直接执行 similar.js 的断言。"""
    _run_harness()


def test_similar_frontend_wiring() -> None:
    """A-TC1/A-TC8：详情页挂载相似区块、候选链接指向详情路由、脚本按序加载。"""
    with open(os.path.join(_REPO_ROOT, "web", "app.js"), encoding="utf-8") as fh:
        app_js = fh.read()
    with open(os.path.join(_REPO_ROOT, "web", "index.html"), encoding="utf-8") as fh:
        html = fh.read()
    assert "renderSimilarSection(detail, series)" in app_js
    assert 'text: "相似小区"' in app_js
    assert '"#/xiaoqu/"' in app_js or "'#/xiaoqu/'" in app_js
    assert html.index("/similar.js") < html.index("/app.js")


def test_similar_has_no_network_or_deps() -> None:
    """A-TC9：零 API key / 零网络调用 / 零新增依赖。"""
    with open(_SIMILAR, encoding="utf-8") as fh:
        js = fh.read()
    for banned in ("fetch(", "XMLHttpRequest", "http://", "https://", "apiKey", "api_key"):
        assert banned not in js, f"similar.js 不得包含 {banned}"
