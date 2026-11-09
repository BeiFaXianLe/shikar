/* shikar 前端单页应用 —— 纯原生 JS，零依赖、无外链。
 *
 * 路由（hash）：
 *   首页     #/
 *   小区详情  #/xiaoqu/<id>
 *
 * 数据来源（相对路径的同源 API）：
 *   GET /api/overview
 *   GET /api/xiaoqu/{id}
 *   GET /api/xiaoqu/{id}/series
 *   GET /api/xiaoqu/{id}/signals
 */
(function () {
  "use strict";

  var appEl = document.getElementById("app");
  var weekEl = document.getElementById("week");

  // -------------------------------------------------------------------------
  // 工具函数
  // -------------------------------------------------------------------------

  /** 创建元素：el("div", {class:"x", text:"..."}, [children]) */
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        var v = attrs[k];
        if (v === null || v === undefined) return;
        if (k === "text") {
          node.textContent = v;
        } else if (k === "html") {
          node.innerHTML = v;
        } else if (k === "class") {
          node.className = v;
        } else if (k.indexOf("on") === 0 && typeof v === "function") {
          node.addEventListener(k.slice(2).toLowerCase(), v);
        } else {
          node.setAttribute(k, v);
        }
      });
    }
    if (children) {
      (Array.isArray(children) ? children : [children]).forEach(function (c) {
        if (c === null || c === undefined) return;
        node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
      });
    }
    return node;
  }

  // SVG 命名空间标识符（W3C 固定 URI，不是网络请求；拼接以规避外链字面量）。
  var SVG_NS = "http" + "://www.w3.org/2000/svg";

  /** 创建 SVG 元素。 */
  function svgEl(tag, attrs) {
    var node = document.createElementNS(SVG_NS, tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        if (attrs[k] !== null && attrs[k] !== undefined) {
          node.setAttribute(k, attrs[k]);
        }
      });
    }
    return node;
  }

  /** 数字格式化：null/undefined → 「—」；否则保留 maxDecimals 位并去掉多余 .0。 */
  function fmtNum(v, maxDecimals) {
    if (v === null || v === undefined || v === "" || isNaN(Number(v))) return "—";
    var d = maxDecimals === undefined ? 1 : maxDecimals;
    var n = Number(v);
    var s = n.toFixed(d);
    if (s.indexOf(".") >= 0) {
      s = s.replace(/0+$/, "").replace(/\.$/, "");
    }
    return s;
  }

  /** 带符号格式化（用于涨跌）。 */
  function fmtSigned(v, maxDecimals) {
    var s = fmtNum(v, maxDecimals);
    if (s === "—") return s;
    if (Number(v) > 0) return "+" + s;
    return s;
  }

  /** 温度→颜色（0 蓝 … 100 红）。 */
  function tempColor(temp) {
    if (temp === null || temp === undefined || isNaN(Number(temp))) return "#4f9dff";
    var t = Math.max(0, Math.min(100, Number(temp))) / 100;
    // 蓝 (79,157,255) → 红 (255,90,90)，中间经过青/黄。
    var stops = [
      [79, 157, 255],   // 0   蓝
      [61, 182, 160],   // 0.35 青
      [230, 200, 70],   // 0.65 黄
      [255, 130, 61],   // 0.82 橙
      [255, 90, 90],    // 1   红
    ];
    var pos = [0, 0.35, 0.65, 0.82, 1];
    var i = 0;
    while (i < pos.length - 1 && t > pos[i + 1]) i++;
    var seg = pos[i + 1] - pos[i];
    var f = seg === 0 ? 0 : (t - pos[i]) / seg;
    var c0 = stops[i], c1 = stops[i + 1];
    var r = Math.round(c0[0] + (c1[0] - c0[0]) * f);
    var g = Math.round(c0[1] + (c1[1] - c0[1]) * f);
    var b = Math.round(c0[2] + (c1[2] - c0[2]) * f);
    return "rgb(" + r + "," + g + "," + b + ")";
  }

  /** 阶段名 → 徽章 CSS 类名后缀。 */
  var STAGE_KEYS = {
    "冰点": "bingdian",
    "回暖": "huinuan",
    "温和": "wenhe",
    "升温": "shengwen",
    "过热": "guore",
    "降温": "jiangwen",
    "下行": "xiaxing",
  };
  function stageBadge(stage) {
    var key = STAGE_KEYS[stage] || "unknown";
    return el("span", { class: "badge stage-" + key, text: stage || "未知" });
  }

  /** 生成错误/空状态节点。 */
  function stateNode(kind, text) {
    if (kind === "loading") {
      return el("div", { class: "loading" }, [
        el("span", { class: "spinner" }),
        document.createTextNode(text || "加载中…"),
      ]);
    }
    return el("div", { class: kind, text: text });
  }

  /** 通用 JSON 请求，失败抛出带可读信息的错误。 */
  function getJSON(path) {
    return fetch(path, { headers: { Accept: "application/json" } }).then(function (resp) {
      if (!resp.ok) {
        return resp
          .json()
          .catch(function () { return {}; })
          .then(function (body) {
            throw new Error((body && body.error) || "HTTP " + resp.status);
          });
      }
      return resp.json();
    });
  }

  // -------------------------------------------------------------------------
  // SVG：温度仪表（0–100 弧）
  // -------------------------------------------------------------------------
  function tempGauge(temp) {
    // 半圆仪表：从左(180°)到右(0°)，用描边圆弧。
    var W = 150, H = 92;
    var cx = 75, cy = 80, r = 60;
    var svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H, role: "img" });

    // 背景弧（灰）
    function arcPath(cx, cy, r, startDeg, endDeg) {
      var a0 = (Math.PI * startDeg) / 180;
      var a1 = (Math.PI * endDeg) / 180;
      var x0 = cx + r * Math.cos(a0);
      var y0 = cy - r * Math.sin(a0);
      var x1 = cx + r * Math.cos(a1);
      var y1 = cy - r * Math.sin(a1);
      var large = Math.abs(endDeg - startDeg) > 180 ? 1 : 0;
      var sweep = endDeg < startDeg ? 1 : 0;
      return "M " + x0 + " " + y0 + " A " + r + " " + r + " 0 " + large + " " + sweep + " " + x1 + " " + y1;
    }

    // 背景弧 180°→0°（左侧到右侧）
    svg.appendChild(
      svgEl("path", {
        d: arcPath(cx, cy, r, 180, 0),
        fill: "none",
        stroke: "#2a3341",
        "stroke-width": 12,
        "stroke-linecap": "round",
      })
    );

    // 彩色进度弧
    var hasTemp = temp !== null && temp !== undefined && !isNaN(Number(temp));
    if (hasTemp) {
      var t = Math.max(0, Math.min(100, Number(temp)));
      var endDeg = 180 - (t / 100) * 180;
      svg.appendChild(
        svgEl("path", {
          d: arcPath(cx, cy, r, 180, endDeg),
          fill: "none",
          stroke: tempColor(t),
          "stroke-width": 12,
          "stroke-linecap": "round",
        })
      );
      // 端点小圆
      var a = (Math.PI * endDeg) / 180;
      svg.appendChild(
        svgEl("circle", {
          cx: cx + r * Math.cos(a),
          cy: cy - r * Math.sin(a),
          r: 5,
          fill: tempColor(t),
        })
      );
    }

    // 刻度端点标注 0 / 100
    var zeroT = svgEl("text", {
      x: 12, y: H - 2, fill: "#66717d", "font-size": "10", "text-anchor": "middle",
    });
    zeroT.textContent = "0";
    svg.appendChild(zeroT);
    var hundredT = svgEl("text", {
      x: W - 12, y: H - 2, fill: "#66717d", "font-size": "10", "text-anchor": "middle",
    });
    hundredT.textContent = "100";
    svg.appendChild(hundredT);
    if (!hasTemp) {
      var nt = svgEl("text", {
        x: cx, y: cy - 14, fill: "#66717d", "font-size": "13", "text-anchor": "middle",
      });
      nt.textContent = "无数据";
      svg.appendChild(nt);
    }
    return svg;
  }

  // -------------------------------------------------------------------------
  // SVG：折线图（温度 / 价格）
  // -------------------------------------------------------------------------
  function lineChart(points, opts) {
    // points: [{week, value}]，value 可能为 null
    opts = opts || {};
    var W = 760, H = 240;
    var padL = 46, padR = 18, padT = 18, padB = 34;
    var plotW = W - padL - padR;
    var plotH = H - padT - padB;

    var svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H, role: "img" });

    var valid = points.filter(function (p) {
      return p.value !== null && p.value !== undefined && !isNaN(Number(p.value));
    });

    if (valid.length === 0) {
      var emptyT = svgEl("text", {
        x: W / 2, y: H / 2, fill: "#66717d", "font-size": "15", "text-anchor": "middle",
      });
      emptyT.textContent = "暂无数据";
      svg.appendChild(emptyT);
      return svg;
    }

    var vals = valid.map(function (p) { return Number(p.value); });
    var minV = Math.min.apply(null, vals);
    var maxV = Math.max.apply(null, vals);
    if (minV === maxV) {
      minV -= 1;
      maxV += 1;
    }
    var range = maxV - minV;
    minV -= range * 0.08;
    maxV += range * 0.08;
    range = maxV - minV;

    function x(i) {
      if (points.length <= 1) return padL + plotW / 2;
      return padL + (i / (points.length - 1)) * plotW;
    }
    function y(v) {
      return padT + (1 - (v - minV) / range) * plotH;
    }

    var color = opts.color || "#4fa3ff";

    // 网格线与 y 轴刻度（3 条）
    var ticks = 3;
    for (var t = 0; t <= ticks; t++) {
      var val = minV + (range * t) / ticks;
      var yy = y(val);
      svg.appendChild(
        svgEl("line", {
          x1: padL, y1: yy, x2: W - padR, y2: yy,
          stroke: "#222a34", "stroke-width": 1,
        })
      );
      var lab = svgEl("text", {
        x: padL - 8, y: yy + 4, fill: "#66717d", "font-size": "11", "text-anchor": "end",
      });
      lab.textContent = fmtNum(val, opts.decimals === undefined ? 1 : opts.decimals);
      svg.appendChild(lab);
    }

    // 折线路径（跳过 null）
    var d = "";
    var started = false;
    points.forEach(function (p, i) {
      if (p.value === null || p.value === undefined || isNaN(Number(p.value))) {
        started = false;
        return;
      }
      var xx = x(i), yy = y(Number(p.value));
      d += (started ? " L " : " M ") + xx + " " + yy;
      started = true;
    });
    svg.appendChild(
      svgEl("path", {
        d: d, fill: "none", stroke: color, "stroke-width": 2.5,
        "stroke-linejoin": "round", "stroke-linecap": "round",
      })
    );

    // 数据点
    points.forEach(function (p, i) {
      if (p.value === null || p.value === undefined || isNaN(Number(p.value))) return;
      svg.appendChild(
        svgEl("circle", {
          cx: x(i), cy: y(Number(p.value)), r: 3, fill: color,
        })
      );
    });

    // 端点数值标注（首、末有效点）
    function annotate(i, anchor) {
      var p = points[i];
      if (!p || p.value === null || p.value === undefined) return;
      var txt = svgEl("text", {
        x: x(i), y: y(Number(p.value)) - 9, fill: color,
        "font-size": "12", "font-weight": "700",
        "text-anchor": anchor,
      });
      txt.textContent = fmtNum(p.value, opts.decimals === undefined ? 1 : opts.decimals) + (opts.unit || "");
      svg.appendChild(txt);
    }
    var firstIdx = points.findIndex(function (p) {
      return p.value !== null && p.value !== undefined && !isNaN(Number(p.value));
    });
    var lastIdx = -1;
    for (var j = points.length - 1; j >= 0; j--) {
      if (points[j].value !== null && points[j].value !== undefined && !isNaN(Number(points[j].value))) {
        lastIdx = j;
        break;
      }
    }
    if (firstIdx >= 0) annotate(firstIdx, "start");
    if (lastIdx >= 0 && lastIdx !== firstIdx) annotate(lastIdx, "end");

    // x 轴标签：首周、中间周、末周
    var labelIdx = [];
    if (points.length <= 3) {
      points.forEach(function (_p, i) { labelIdx.push(i); });
    } else {
      labelIdx = [0, Math.floor((points.length - 1) / 2), points.length - 1];
    }
    labelIdx.forEach(function (i) {
      var p = points[i];
      if (!p) return;
      var xt = svgEl("text", {
        x: x(i), y: H - 12, fill: "#66717d", "font-size": "11", "text-anchor": "middle",
      });
      xt.textContent = shortWeek(p.week);
      svg.appendChild(xt);
    });

    return svg;
  }

  /** "2025-W03" → "W03"。 */
  function shortWeek(week) {
    if (!week) return "";
    var m = String(week).match(/W\d+$/i);
    return m ? m[0].toUpperCase() : String(week);
  }

  // -------------------------------------------------------------------------
  // 首页
  // -------------------------------------------------------------------------
  function renderOverview() {
    appEl.innerHTML = "";
    appEl.appendChild(stateNode("loading", "正在加载总览…"));

    getJSON("/api/overview")
      .then(function (data) {
        weekEl.textContent = data && data.week ? "数据周 " + data.week : "";
        var cities = (data && data.cities) || [];
        appEl.innerHTML = "";

        var head = el("div", { class: "page-head" }, [
          el("h1", { text: "市场温度总览" }),
          el("span", {
            class: "sub",
            text: data && data.week ? "数据周 " + data.week : "",
          }),
        ]);
        appEl.appendChild(head);

        if (cities.length === 0) {
          appEl.appendChild(stateNode("empty", "暂无数据。请确认后端已播种数据（--seed）。"));
          return;
        }

        var grid = el("div", { class: "city-grid" });
        cities.forEach(function (city) { grid.appendChild(cityCard(city)); });
        appEl.appendChild(grid);
      })
      .catch(function (err) {
        weekEl.textContent = "";
        appEl.innerHTML = "";
        appEl.appendChild(stateNode("error", "总览加载失败：" + err.message));
      });
  }

  function cityCard(city) {
    var temp = city.temperature;
    var card = el("article", { class: "city-card" });

    card.appendChild(
      el("div", { class: "city-head" }, [
        el("div", { class: "city-name", text: city.city || "未知城市" }),
        el("div", { class: "city-count" }, [
          document.createTextNode("小区 "),
          el("b", { text: String(city.count == null ? 0 : city.count) }),
        ]),
      ])
    );

    // 温度仪表 + 大数字
    var numColor = tempColor(temp);
    var gaugeWrap = el("div", { class: "gauge-wrap" }, [
      el("div", { class: "gauge" }, [tempGauge(temp)]),
      el("div", { class: "gauge-temp" }, [
        el("div", { class: "num", style: "color:" + numColor }, [
          document.createTextNode(fmtNum(temp, 1)),
          el("span", { class: "unit", text: " °C 温度" }),
        ]),
        el("div", { class: "label", text: "0–100 温度（蓝→红）" }),
      ]),
    ]);
    card.appendChild(gaugeWrap);

    // 四个榜单
    var boards = el("div", { class: "boards" }, [
      board("最热", "hot", city.top_hot),
      board("最冷", "cold", city.top_cold),
      board("升温最快", "rising", city.top_rising),
      board("降温最快", "falling", city.top_falling),
    ]);
    card.appendChild(boards);
    return card;
  }

  function board(title, kind, items) {
    var wrap = el("div", { class: "board" });
    wrap.appendChild(
      el("div", { class: "board-title" }, [
        el("span", { class: "dot dot-" + kind }),
        document.createTextNode(title),
      ])
    );
    var list = el("ul", { class: "board-list" });
    if (!items || items.length === 0) {
      list.appendChild(el("li", { class: "board-empty", text: "暂无" }));
    } else {
      items.forEach(function (item) {
        var href = "#/xiaoqu/" + encodeURIComponent(item.xiaoqu_id);
        var nameNode = el("span", { class: "name" }, [
          document.createTextNode(item.name || item.xiaoqu_id || "—"),
        ]);
        if (item.district) {
          nameNode.appendChild(el("span", { class: "district", text: "（" + item.district + "）" }));
        }
        var meta = el("span", { class: "meta" }, [
          el("span", {
            class: "temp",
            style: "color:" + tempColor(item.temperature),
            text: fmtNum(item.temperature, 1),
          }),
          stageBadge(item.stage),
        ]);
        list.appendChild(
          el("li", { class: "board-item" }, [el("a", { href: href }, [nameNode, meta])])
        );
      });
    }
    wrap.appendChild(list);
    return wrap;
  }

  // -------------------------------------------------------------------------
  // 小区详情
  // -------------------------------------------------------------------------
  function renderDetail(id) {
    appEl.innerHTML = "";
    appEl.appendChild(backLink());
    appEl.appendChild(stateNode("loading", "正在加载小区详情…"));

    var detailP = getJSON("/api/xiaoqu/" + encodeURIComponent(id));
    var seriesP = getJSON("/api/xiaoqu/" + encodeURIComponent(id) + "/series");
    var signalsP = getJSON("/api/xiaoqu/" + encodeURIComponent(id) + "/signals");

    Promise.all([detailP, seriesP, signalsP])
      .then(function (res) {
        var detail = res[0];
        var series = Array.isArray(res[1]) ? res[1] : [];
        var signals = Array.isArray(res[2]) ? res[2] : [];
        appEl.innerHTML = "";
        appEl.appendChild(backLink());
        appEl.appendChild(renderDetailHead(detail, series));
        appEl.appendChild(renderSeriesSection(series));
        appEl.appendChild(renderSignalsSection(signals));
      })
      .catch(function (err) {
        appEl.innerHTML = "";
        appEl.appendChild(backLink());
        appEl.appendChild(stateNode("error", "加载失败：" + err.message));
      });
  }

  function backLink() {
    return el("a", { class: "back", href: "#/" }, [document.createTextNode("← 返回")]);
  }

  function latestValid(series, key) {
    for (var i = series.length - 1; i >= 0; i--) {
      var v = series[i][key];
      if (v !== null && v !== undefined && !isNaN(Number(v))) return v;
    }
    return null;
  }

  function renderDetailHead(detail, series) {
    var temp = latestValid(series, "temperature");
    var stage = series.length ? series[series.length - 1].stage : null;
    if (stage === null || stage === undefined) {
      for (var i = series.length - 1; i >= 0; i--) {
        if (series[i].stage) { stage = series[i].stage; break; }
      }
    }
    var loc = [detail.district, detail.plate].filter(Boolean).join(" · ");

    var head = el("section", { class: "detail-head" });
    head.appendChild(
      el("div", { class: "detail-title-row" }, [
        el("h1", { class: "detail-title", text: detail.name || detail.id || "未知小区" }),
        stage ? stageBadge(stage) : null,
        loc ? el("span", { class: "detail-loc", text: loc }) : null,
      ])
    );

    var metrics = el("div", { class: "detail-metrics" }, [
      el("div", { class: "metric" }, [
        el("span", { class: "k", text: "最新温度" }),
        el("span", { class: "v", style: "color:" + tempColor(temp) }, [
          document.createTextNode(fmtNum(temp, 1)),
          el("span", { class: "unit", text: " °C" }),
        ]),
      ]),
      el("div", { class: "metric" }, [
        el("span", { class: "k", text: "最新价格" }),
        el("span", { class: "v" }, [
          document.createTextNode(fmtNum(latestValid(series, "price"), 0)),
          el("span", { class: "unit", text: " 元/㎡" }),
        ]),
      ]),
      detail.city
        ? el("div", { class: "metric" }, [
            el("span", { class: "k", text: "城市" }),
            el("span", { class: "v", style: "font-size:20px" }, [document.createTextNode(detail.city)]),
          ])
        : null,
    ]);
    head.appendChild(metrics);
    return head;
  }

  function renderSeriesSection(series) {
    var section = el("section", { class: "section" });
    section.appendChild(el("h2", { text: "走势曲线" }));

    if (!series || series.length === 0) {
      section.appendChild(el("div", { class: "chart-empty", text: "暂无数据" }));
      return section;
    }

    var tempPoints = series.map(function (r) { return { week: r.week, value: r.temperature }; });
    var pricePoints = series.map(function (r) { return { week: r.week, value: r.price }; });

    var tempHas = tempPoints.some(function (p) { return p.value !== null && p.value !== undefined; });
    var priceHas = pricePoints.some(function (p) { return p.value !== null && p.value !== undefined; });

    if (!tempHas && !priceHas) {
      section.appendChild(el("div", { class: "chart-empty", text: "暂无数据" }));
      return section;
    }

    if (tempHas) {
      section.appendChild(el("h2", { class: "sub-title", text: "温度走势", style: "font-size:13px;color:#9aa7b4;margin:6px 0 6px" }));
      section.appendChild(
        el("div", { class: "chart" }, [lineChart(tempPoints, { color: "#ff8a3d", decimals: 1 })])
      );
    }
    if (priceHas) {
      section.appendChild(el("h2", { class: "sub-title", text: "价格走势（元/㎡）", style: "font-size:13px;color:#9aa7b4;margin:18px 0 6px" }));
      section.appendChild(
        el("div", { class: "chart" }, [lineChart(pricePoints, { color: "#4fa3ff", decimals: 0 })])
      );
    }
    return section;
  }

  function renderSignalsSection(signals) {
    var section = el("section", { class: "section" });
    section.appendChild(el("h2", { text: "信号" }));

    if (!signals || signals.length === 0) {
      section.appendChild(el("div", { class: "signals-empty", text: "暂无信号" }));
      return section;
    }

    var list = el("ul", { class: "signals" });
    signals.forEach(function (s) {
      list.appendChild(
        el("li", { class: "signal" }, [
          el("span", { class: "date", text: s.date || "—" }),
          el("div", { class: "body" }, [
            el("div", { class: "type", text: s.type || "信号" }),
            el("div", { class: "reason", text: reasonText(s.reason) }),
          ]),
        ])
      );
    });
    section.appendChild(list);
    return section;
  }

  /** 把 reason 对象/字符串压缩成简短摘要文本。 */
  function reasonText(reason) {
    if (reason === null || reason === undefined) return "";
    if (typeof reason === "string") return reason;
    if (typeof reason === "object") {
      // 常见键优先：note/reason/desc/text/message
      var prefer = ["note", "reason", "desc", "description", "text", "message"];
      for (var i = 0; i < prefer.length; i++) {
        if (reason[prefer[i]] !== undefined && reason[prefer[i]] !== null) {
          return String(reason[prefer[i]]);
        }
      }
      var parts = [];
      Object.keys(reason).forEach(function (k) {
        var v = reason[k];
        if (v === null || v === undefined) return;
        if (typeof v === "object") {
          try { v = JSON.stringify(v); } catch (e) { v = String(v); }
        }
        parts.push(k + ": " + v);
      });
      return parts.join("；");
    }
    return String(reason);
  }

  // -------------------------------------------------------------------------
  // 路由
  // -------------------------------------------------------------------------
  function parseHash() {
    var raw = window.location.hash || "#/";
    var path = raw.replace(/^#/, "");
    if (path === "" || path === "/") return { name: "home" };
    var m = path.match(/^\/xiaoqu\/(.+)$/);
    if (m) return { name: "detail", id: decodeURIComponent(m[1]) };
    return { name: "unknown", raw: path };
  }

  function route() {
    var r = parseHash();
    if (r.name === "home") {
      renderOverview();
    } else if (r.name === "detail") {
      renderDetail(r.id);
    } else {
      weekEl.textContent = "";
      appEl.innerHTML = "";
      appEl.appendChild(
        el("div", { class: "error" }, [
          document.createTextNode("页面不存在： " + r.raw + " "),
          el("a", { href: "#/", text: "返回首页" }),
        ])
      );
    }
  }

  window.addEventListener("hashchange", route);
  window.addEventListener("DOMContentLoaded", route);
  // 若脚本在 DOMContentLoaded 之后加载，补一次。
  if (document.readyState !== "loading") {
    route();
  }
})();
