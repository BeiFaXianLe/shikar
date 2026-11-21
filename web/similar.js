/* 形态 A：相似小区推荐（纯函数，无 DOM / 无网络 / 无依赖）
 *
 * 方案：Producter docs/32。全部规则只做「字段比较 + 模板拼接」：
 * - 候选来自 /api/catalog 的现有参数（city / district / price band / price_week / stage）；
 * - 排序用「温度差绝对值 → 价格差 → id」，不写相似度函数；
 * - 理由只引用真实字段值（温度 / 阶段 / 均价 / 期次），不生成数据里没有的结论。
 *
 * 暴露到 window.ShikarSimilar，供 app.js 使用、供 Node 单测直接加载。
 */
(function (root) {
  "use strict";

  var DEFAULT_BAND = 0.2;      // ±20%
  var RELAXED_BAND = 0.3;      // 放宽档 ±30%
  var MIN_RESULTS = 3;
  var MAX_RESULTS = 5;
  var MIN_DATA_POINTS = 2;     // 对齐详情页 dp<2 不给温度的口径

  function num(v) {
    if (v === null || v === undefined || v === "") return null;
    var n = Number(v);
    return isNaN(n) ? null : n;
  }

  /** 从 series 里取最后一条某字段非空的行。 */
  function lastWith(series, key) {
    for (var i = series.length - 1; i >= 0; i--) {
      if (series[i] && series[i][key] !== null && series[i][key] !== undefined) {
        return series[i];
      }
    }
    return null;
  }

  /**
   * 用详情 + 序列构造推荐上下文。
   * sufficient=false 表示当前小区自身数据不足：此时只按「同区 + 同价带」推荐、理由不提温度。
   */
  function buildContext(detail, series) {
    detail = detail || {};
    series = Array.isArray(series) ? series : [];
    var tempRow = lastWith(series, "temperature");
    var priceRow = lastWith(series, "price");
    var dataPoints = series.length;
    var temperature = tempRow ? num(tempRow.temperature) : null;
    return {
      id: detail.id || "",
      name: detail.name || detail.id || "",
      city: detail.city || "",
      district: detail.district || "",
      plate: detail.plate || "",
      price: priceRow ? num(priceRow.price) : null,
      priceWeek: priceRow ? priceRow.week || null : null,
      temperature: temperature,
      stage: tempRow ? tempRow.stage || null : null,
      dataPoints: dataPoints,
      sufficient: temperature !== null && dataPoints >= MIN_DATA_POINTS
    };
  }

  /** 第一档 / 放宽档的查询参数（直接喂给 /api/catalog）。 */
  function queryFor(ctx, opts) {
    opts = opts || {};
    var band = opts.band === undefined ? DEFAULT_BAND : opts.band;
    var q = { city: ctx.city };
    if (!ctx.sufficient && ctx.district) q.district = ctx.district;
    if (ctx.price !== null && ctx.price !== undefined) {
      q.price_min = Math.round(ctx.price * (1 - band));
      q.price_max = Math.round(ctx.price * (1 + band));
    }
    if (ctx.priceWeek) q.price_week = ctx.priceWeek;
    if (ctx.sufficient && !opts.dropStage && ctx.stage) q.stage = ctx.stage;
    return q;
  }

  /** 候选过滤：同城、排除自身、data_points>=2、价格在带内（默认 ±20%）。 */
  function filterCandidates(items, ctx, opts) {
    opts = opts || {};
    var band = opts.band === undefined ? DEFAULT_BAND : opts.band;
    var out = [];
    (items || []).forEach(function (it) {
      if (!it || !it.id || it.id === ctx.id) return;
      if (ctx.city && it.city && it.city !== ctx.city) return;
      if (opts.district && it.district && it.district !== opts.district) return;
      if (!(num(it.data_points) >= MIN_DATA_POINTS)) return;
      if (ctx.price === null || ctx.price === undefined) return;
      var price = num(it.latest_price);
      if (price === null) return;
      if (Math.abs(price - ctx.price) > ctx.price * band + 0.5) return;
      out.push(it);
    });
    return out;
  }

  /** 排序：温度差升序（无温度排最后）→ 价格差升序 → id 升序。 */
  function rank(items, ctx) {
    function tempDiff(it) {
      if (!ctx.sufficient || ctx.temperature === null) return Infinity;
      var t = num(it.temperature);
      return t === null ? Infinity : Math.abs(t - ctx.temperature);
    }
    function priceDiff(it) {
      var p = num(it.latest_price);
      if (p === null || ctx.price === null) return Infinity;
      return Math.abs(p - ctx.price);
    }
    return (items || []).slice().sort(function (a, b) {
      var da = tempDiff(a), db = tempDiff(b);
      if (da !== db) return da - db;
      var pa = priceDiff(a), pb = priceDiff(b);
      if (pa !== pb) return pa - pb;
      return String(a.id) < String(b.id) ? -1 : (String(a.id) > String(b.id) ? 1 : 0);
    });
  }

  /** 推荐理由：只拼真实字段 + 期次口径；当前小区数据不足时不出现任何温度话术。 */
  function reason(item, ctx) {
    var parts = [];
    if (ctx.sufficient) {
      var t = num(item.temperature);
      if (t !== null && ctx.temperature !== null) {
        parts.push(
          "温度 " + t.toFixed(1) + " 与 " + (ctx.name || "本小区") +
          "（" + ctx.temperature.toFixed(1) + "）相差 " +
          Math.abs(t - ctx.temperature).toFixed(1)
        );
        if (item.stage && ctx.stage && item.stage === ctx.stage) {
          parts.push("同处「" + item.stage + "」阶段");
        }
      }
    }
    var price = num(item.latest_price);
    if (price !== null && ctx.price) {
      var pct = ((price - ctx.price) / ctx.price) * 100;
      if (Math.abs(pct) >= 0.1) {
        parts.push("均价比本小区" + (pct < 0 ? "低 " : "高 ") + Math.abs(pct).toFixed(1) + "%");
      }
    }
    if (!parts.length) parts.push("同价带内可一并对比");
    parts.push("期次 " + (item.latest_week || "—"));
    return parts.join("；");
  }

  /** 推荐列表：过滤 + 排序 + 截断到 3–5 条。 */
  function recommend(items, ctx, opts) {
    opts = opts || {};
    var filtered = filterCandidates(items, ctx, opts);
    var ranked = rank(filtered, ctx);
    return ranked.slice(0, opts.max || MAX_RESULTS);
  }

  root.ShikarSimilar = {
    DEFAULT_BAND: DEFAULT_BAND,
    RELAXED_BAND: RELAXED_BAND,
    MIN_RESULTS: MIN_RESULTS,
    MAX_RESULTS: MAX_RESULTS,
    MIN_DATA_POINTS: MIN_DATA_POINTS,
    buildContext: buildContext,
    queryFor: queryFor,
    filterCandidates: filterCandidates,
    rank: rank,
    reason: reason,
    recommend: recommend
  };
})(typeof window !== "undefined" ? window : globalThis);
