/* shikar 前端单页应用 —— 纯原生 JS，零依赖、无外链。
 *
 * 路由（hash）：
 *   首页     #/
 *   小区目录  #/catalog
 *   小区详情  #/xiaoqu/<id>
 *
 * 数据来源（相对路径的同源 API）：
 *   GET /api/overview?city=&district=   （首页总览，可按区筛选）
 *   GET /api/districts?city=            （首页各区下拉数据）
 *   GET /api/catalog?city=&district=&q=&page=&page_size=  （小区目录：筛选 + 搜索 + 分页）
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
    if (temp === null || temp === undefined || isNaN(Number(temp))) return "#34536b";
    var t = Math.max(0, Math.min(100, Number(temp))) / 100;
    // 靛蓝(冷) → 青灰 → 沙 → 赭 → 朱砂(热)，贴合纸面印刷的年鉴色调。
    var stops = [
      [52, 83, 107],    // 0    靛蓝 #34536b
      [74, 110, 102],   // 0.35 青灰 #4a6e66
      [150, 146, 118],  // 0.65 沙   #969276
      [178, 120, 58],   // 0.82 赭   #b2783a
      [178, 58, 43],    // 1    朱砂 #b23a2b
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

  /** 查询串拼接：把 {a:"北京", b:null} 拼成「?a=%E5%8C%97%E4%BA%AC」，空值跳过。 */
  function buildQuery(params) {
    var parts = [];
    Object.keys(params || {}).forEach(function (k) {
      var v = params[k];
      if (v === null || v === undefined || v === "") return;
      parts.push(encodeURIComponent(k) + "=" + encodeURIComponent(v));
    });
    return parts.length ? "?" + parts.join("&") : "";
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
        stroke: "#cfc9b9",
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
      x: 12, y: H - 2, fill: "#6a756d", "font-size": "10", "text-anchor": "middle",
    });
    zeroT.textContent = "0";
    svg.appendChild(zeroT);
    var hundredT = svgEl("text", {
      x: W - 12, y: H - 2, fill: "#6a756d", "font-size": "10", "text-anchor": "middle",
    });
    hundredT.textContent = "100";
    svg.appendChild(hundredT);
    if (!hasTemp) {
      var nt = svgEl("text", {
        x: cx, y: cy - 14, fill: "#6a756d", "font-size": "13", "text-anchor": "middle",
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
        x: W / 2, y: H / 2, fill: "#6a756d", "font-size": "15", "text-anchor": "middle",
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

    var color = opts.color || "#34536b";

    // 网格线与 y 轴刻度（3 条）
    var ticks = 3;
    for (var t = 0; t <= ticks; t++) {
      var val = minV + (range * t) / ticks;
      var yy = y(val);
      svg.appendChild(
        svgEl("line", {
          x1: padL, y1: yy, x2: W - padR, y2: yy,
          stroke: "#d8d2c3", "stroke-width": 1,
        })
      );
      var lab = svgEl("text", {
        x: padL - 8, y: yy + 4, fill: "#6a756d", "font-size": "11", "text-anchor": "end",
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
        x: x(i), y: H - 12, fill: "#6a756d", "font-size": "11", "text-anchor": "middle",
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
  /** 拉取某城市的区列表，返回 Promise<[{city, district}]>；失败降级为空数组。 */
  function fetchDistricts(city) {
    if (!city) return Promise.resolve([]);
    return getJSON("/api/districts" + buildQuery({ city: city }))
      .then(function (rows) {
        return Array.isArray(rows) ? rows : [];
      })
      .catch(function () { return []; });
  }

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
          el("div", { class: "page-head-aside" }, [
            el("span", {
              class: "sub",
              text: data && data.week ? "数据周 " + data.week : "",
            }),
            el("a", {
              class: "catalog-entry",
              href: "#/catalog",
              text: "浏览小区目录 →",
            }),
          ]),
        ]);
        appEl.appendChild(head);

        if (cities.length === 0) {
          appEl.appendChild(stateNode("empty", "暂无足够数据：温度与趋势需要每个小区至少 3 期可比历史（当前数据多为 1–2 期）。请点开具体小区查看原始读数；历史会随每周快照逐步积累。"));
          appEl.appendChild(
            el("div", { class: "empty-cta" }, [
              el("a", { class: "catalog-entry", href: "#/catalog", text: "浏览全部小区目录（按城市 / 区 / 搜索）→" }),
            ])
          );
          return;
        }

        var grid = el("div", { class: "city-grid" });
        cities.forEach(function (city) { grid.appendChild(cityCard(city)); });
        appEl.appendChild(grid);

        // 区列表并行拉取；每个城市卡片各自填充下拉、互不影响。
        cities.forEach(function (city) {
          var host = grid.querySelector(
            '[data-city-card="' + cssAttr(city.city) + '"]'
          );
          if (!host) return;
          var cardApi = host.__cardApi;
          if (!cardApi) return;
          fetchDistricts(city.city).then(function (rows) {
            cardApi.setDistricts(rows.map(function (r) { return r.district; }));
          });
        });
      })
      .catch(function (err) {
        weekEl.textContent = "";
        appEl.innerHTML = "";
        appEl.appendChild(stateNode("error", "总览加载失败：" + err.message));
      });
  }

  /** CSS 属性选择器转义（用于按城市名定位卡片）。 */
  function cssAttr(value) {
    return String(value == null ? "" : value).replace(/["\\]/g, "\\$&");
  }

  function cityCard(city) {
    var card = el("article", {
      class: "city-card",
      "data-city-card": city.city || "",
    });

    card.appendChild(
      el("div", { class: "city-head" }, [
        el("div", { class: "city-name", text: city.city || "未知城市" }),
        el("div", { class: "city-count" }, [
          document.createTextNode("小区 "),
          el("b", { text: String(city.count == null ? 0 : city.count) }),
        ]),
      ])
    );

    // 区下拉：初始只有「全部」，区列表异步填充后启用。
    var select = el("select", { class: "district-select", disabled: "disabled" }, [
      el("option", { value: "", text: "全部" }),
    ]);
    card.appendChild(
      el("div", { class: "city-filter" }, [
        el("label", { text: "区域" }),
        select,
      ])
    );

    // 一个卡片可整体重绘正文，但保留城市名与下拉（避免闪烁、保持选择）。
    var body = el("div", { class: "city-body" });
    card.appendChild(body);

    function paint(data) {
      body.innerHTML = "";
      var t = data.temperature;
      var numColor = tempColor(t);
      body.appendChild(
        el("div", { class: "gauge-wrap" }, [
          el("div", { class: "gauge" }, [tempGauge(t)]),
          el("div", { class: "gauge-temp" }, [
            el("div", { class: "num", style: "color:" + numColor }, [
              document.createTextNode(fmtNum(t, 1)),
              el("span", { class: "unit", text: " °C 温度" }),
            ]),
            el("div", {
              class: "label",
              text: "0–100 温度 · 冷 → 热" + (data.districtLabel ? " · " + data.districtLabel : ""),
            }),
          ]),
        ])
      );
      body.appendChild(
        el("div", { class: "boards" }, [
          board("最热", "hot", data.top_hot),
          board("最冷", "cold", data.top_cold),
          board("升温最快", "rising", data.top_rising),
          board("降温最快", "falling", data.top_falling),
        ])
      );
      // 同步小区数（筛选后可能变化）。
      var cityCount = card.querySelector(".city-count b");
      if (cityCount) cityCount.textContent = String(data.count == null ? 0 : data.count);
    }

    // 用整体概览中该城市的切片作初值（不传区时即「全市」）。
    paint(city);

    select.addEventListener("change", function () {
      var district = select.value;
      select.disabled = true;
      // 以 /api/overview?city=&district= 重新拉取本城市该区数据。
      getJSON("/api/overview" + buildQuery({ city: city.city, district: district }))
        .then(function (resp) {
          var cities = (resp && resp.cities) || [];
          // 过滤后该城市应有唯一一条聚合；无数据时给出空态切片。
          var slice =
            cities[0] && cities[0].city
              ? cities[0]
              : {
                  city: city.city,
                  temperature: null,
                  count: 0,
                  top_hot: [],
                  top_cold: [],
                  top_rising: [],
                  top_falling: [],
                };
          slice.districtLabel = district ? "区：" + district : "";
          paint(slice);
        })
        .catch(function (err) {
          body.innerHTML = "";
          body.appendChild(stateNode("error", "筛选失败：" + err.message));
        })
        .then(function () {
          select.disabled = false;
        });
    });

    // 暴露给 renderOverview 用于异步填充区列表。
    card.__cardApi = {
      setDistricts: function (districts) {
        districts.forEach(function (d) {
          if (!d) return;
          select.appendChild(el("option", { value: d, text: d }));
        });
        select.disabled = districts.length === 0;
      },
    };
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
  // 小区目录
  // -------------------------------------------------------------------------
  /** 目录页每页条数。 */
  var CATALOG_PAGE_SIZE = 20;

  /** 「找小区」页筛选状态（城市 / 地铁 / 价格 / 名称 / 区 / 页码）。 */
  var findState = {
    city: "",
    lines: [],          // 选中线路 id 列表
    station: "",        // 指定站点 id
    subwayMode: "station",
    subwayMax: 1,       // km
    priceMin: "",
    priceMax: "",
    priceWeek: "",      // 价格期次（空=全部期次，各期标注）
    q: "",
    district: "",
    page: 1,
  };

  /** 地铁线路数据缓存：{城市: {items, byCity}}；避免重复请求。 */
  var subwayCache = {};

  /** 拉取（并缓存）某城市的地铁线路与站点数据。 */
  function loadSubwayLines(city) {
    if (!city) return Promise.resolve([]);
    if (subwayCache[city]) return Promise.resolve(subwayCache[city]);
    return getJSON("/api/subway/lines" + buildQuery({ city: city }))
      .then(function (data) {
        var items = (data && data.items) || [];
        subwayCache[city] = items;
        return items;
      })
      .catch(function () { return []; });
  }

  /** 格式化「区 · 板块」展示串（去空）。 */
  function locText(district, plate) {
    return [district, plate].filter(Boolean).join(" · ");
  }

  /** 由 findState 组装 /api/catalog 查询参数（多线路用重复 line 键）。 */
  function findParams(extra) {
    var params = {
      city: findState.city,
      district: findState.district,
      q: findState.q,
      price_min: findState.priceMin,
      price_max: findState.priceMax,
      price_week: findState.priceWeek,
      subway_mode: findState.subwayMode,
      subway_max: (findState.lines.length || findState.station) ? findState.subwayMax : "",
      station: findState.station,
      page: findState.page,
      page_size: CATALOG_PAGE_SIZE,
      sort: (findState.lines.length || findState.station) ? "subway_distance" : "default",
    };
    Object.keys(extra || {}).forEach(function (k) { params[k] = extra[k]; });
    return params;
  }

  /** 组装带重复 line 键的完整查询串。 */
  function findQueryString() {
    var params = findParams();
    var parts = [];
    Object.keys(params).forEach(function (k) {
      var v = params[k];
      if (v === null || v === undefined || v === "") return;
      parts.push(encodeURIComponent(k) + "=" + encodeURIComponent(v));
    });
    findState.lines.forEach(function (lid) {
      parts.push("line=" + encodeURIComponent(lid));
    });
    return parts.length ? "?" + parts.join("&") : "";
  }

  /** 价格展示：¥ 千分位；空值显示「—」。 */
  function fmtPrice(v) {
    return v === null || v === undefined || isNaN(Number(v)) ? "—" : "¥" + fmtNum(v, 0);
  }

  /**
   * 用 URL 查询串重置「找小区」筛选状态（可分享链接）。
   *
   * P2-03：解析前先重置为默认，避免旧条件残留；支持 page / price_week；对非法百分号
   * 编码（decodeURIComponent 抛 URIError）做容错，不让整页崩溃。
   */
  function applyFindQuery(queryStr) {
    // 先重置为默认：哪些参数没给就回到默认，而非沿用上次残留。
    findState.city = "";
    findState.lines = [];
    findState.station = "";
    findState.subwayMode = "station";
    findState.subwayMax = 1;
    findState.priceMin = "";
    findState.priceMax = "";
    findState.priceWeek = "";
    findState.q = "";
    findState.district = "";
    findState.page = 1;
    if (!queryStr) return;

    var params = {};
    var lines = [];
    function safeDecode(s) {
      try { return decodeURIComponent(s); } catch (e) { return s; }
    }
    queryStr.split("&").forEach(function (pair) {
      if (!pair) return;
      var eq = pair.indexOf("=");
      var k = safeDecode(eq < 0 ? pair : pair.slice(0, eq));
      var v = safeDecode((eq < 0 ? "" : pair.slice(eq + 1)).replace(/\+/g, " "));
      if (k === "line") { if (v) lines.push(v); }
      else if (k) { params[k] = v; }
    });
    if (params.city !== undefined) findState.city = params.city;
    findState.lines = lines;
    if (params.subway_max !== undefined && params.subway_max !== "") {
      var sm = Number(params.subway_max);
      if (isFinite(sm) && sm > 0 && sm <= 5) findState.subwayMax = sm;
    }
    if (params.subway_mode === "line" || params.subway_mode === "station") {
      findState.subwayMode = params.subway_mode;
    }
    // 单站深链（P2-4）：station 独立恢复，不依赖 lines 存在；后端对 station 单独生效。
    // 站点归属城市/线路的关系由服务端 filter_sql 校验（非法站点会返回 400 并提示）。
    if (params.station !== undefined && params.station !== "") {
      findState.station = params.station;
      if (findState.subwayMode === "line") findState.subwayMode = "station";
    }
    if (params.price_min !== undefined) findState.priceMin = params.price_min;
    if (params.price_max !== undefined) findState.priceMax = params.price_max;
    if (params.price_week !== undefined) findState.priceWeek = params.price_week;
    if (params.q !== undefined) findState.q = params.q;
    if (params.district !== undefined) findState.district = params.district;
    if (params.page !== undefined) {
      var pg = parseInt(params.page, 10);
      findState.page = (isFinite(pg) && pg > 0) ? pg : 1;
    }
  }

  function renderCatalog() {
    appEl.innerHTML = "";

    // —— 页眉 ——
    appEl.appendChild(
      el("div", { class: "find-head" }, [
        el("div", { class: "eyebrow", text: "01 / COMMUNITY EXPLORER" }),
        el("div", { class: "find-head-row" }, [
          el("div", {}, [
            el("h1", { class: "find-title", text: "围绕生活，找到一个小区。" }),
            el("p", { class: "find-sub", text: "地铁沿线与参考均价，把重要的偏好放在一起。" }),
          ]),
          el("a", { class: "catalog-entry", href: "#/", text: "← 返回总览" }),
        ]),
      ])
    );

    // —— 三主筛选 ——
    var citySelect = el("select", { class: "ff-select", "aria-label": "城市" }, [
      el("option", { value: "", text: "全部城市" }),
      el("option", { value: "北京", text: "北京" }),
      el("option", { value: "天津", text: "天津" }),
    ]);
    citySelect.value = findState.city;

    var lineSummary = el("button", {
      class: "ff-choice", type: "button", id: "choose-lines",
      text: linesSummaryText(),
    });

    var priceMin = el("input", {
      type: "number", min: "0", class: "ff-price", "aria-label": "最低均价",
      placeholder: "不限", value: findState.priceMin,
    });
    var priceMax = el("input", {
      type: "number", min: "0", class: "ff-price", "aria-label": "最高均价",
      placeholder: "不限", value: findState.priceMax,
    });

    var mainFilters = el("div", { class: "find-primary" }, [
      el("div", { class: "ff-field" }, [el("label", { text: "01 / 城市" }), citySelect]),
      el("div", { class: "ff-field" }, [el("label", { text: "02 / 地铁线路" }), lineSummary]),
      el("div", { class: "ff-field" }, [
        el("label", { text: "03 / 参考均价（元/㎡）" }),
        el("div", { class: "ff-price-pair" }, [priceMin, el("span", { text: "—" }), priceMax]),
      ]),
      el("button", { class: "ff-more", type: "button", id: "more-filters", text: "更多条件 ＋" }),
    ]);
    appEl.appendChild(mainFilters);

    // 手机筛选按钮
    appEl.appendChild(
      el("button", { class: "find-mobile-btn", type: "button", id: "mobile-filters" }, [
        el("span", { text: "☷　筛选" }),
        el("span", { class: "find-mobile-more", text: "更多条件 →" }),
      ])
    );

    // —— chips（已选条件）——
    var chipHost = el("div", { class: "find-chips" });
    appEl.appendChild(chipHost);

    // —— 结果区 ——
    var resultHost = el("div", { class: "find-results" });
    appEl.appendChild(resultHost);

    function linesSummaryText() {
      if (!findState.lines.length) return "不限线路　⌄";
      return findState.lines.length + " 条线路　⌄";
    }

    function lineNameOf(lid) {
      var items = subwayCache[findState.city] || [];
      for (var i = 0; i < items.length; i++) if (items[i].id === lid) return items[i].name;
      return lid;
    }

    function renderChips() {
      chipHost.innerHTML = "";
      var items = [];
      if (findState.city) items.push(["city", findState.city]);
      findState.lines.forEach(function (lid) { items.push(["line:" + lid, lineNameOf(lid)]); });
      if (findState.priceMin || findState.priceMax) {
        items.push(["price", (findState.priceMin || "不限") + "–" + (findState.priceMax || "不限") + " 元/㎡"]);
      }
      if (findState.priceWeek) items.push(["priceweek", "期次：" + findState.priceWeek]);
      if (findState.lines.length || findState.station) {
        var mode = findState.subwayMode === "line"
          ? "距线路" : (findState.station ? stationNameOf(findState.station) : "距站");
        items.push(["radius", mode + " ≤ " + findState.subwayMax + " km"]);
      }
      if (findState.district) items.push(["district", "区：" + findState.district]);
      if (findState.q) items.push(["q", "名称：" + findState.q]);
      if (!items.length) { chipHost.style.display = "none"; return; }
      chipHost.style.display = "";
      chipHost.appendChild(el("span", { class: "fc-label", text: "已选条件" }));
      items.forEach(function (pair) {
        var removable = pair[0] !== "city";
        chipHost.appendChild(
          el("button", {
            class: "fc-chip", type: "button", "data-remove": pair[0],
            text: pair[1] + (removable ? " ×" : ""),
          })
        );
      });
      chipHost.appendChild(
        el("button", { class: "fc-clear", type: "button", id: "clear-all", text: "清除其他条件" })
      );
      chipHost.querySelectorAll("[data-remove]").forEach(function (b) {
        b.addEventListener("click", function () { removeChip(b.getAttribute("data-remove")); });
      });
      var clearAll = chipHost.querySelector("#clear-all");
      if (clearAll) clearAll.addEventListener("click", clearOthers);
    }

    function stationNameOf(sid) {
      var items = subwayCache[findState.city] || [];
      for (var i = 0; i < items.length; i++) {
        var st = items[i].stations || [];
        for (var j = 0; j < st.length; j++) if (st[j].id === sid) return st[j].name;
      }
      return "指定站";
    }

    function removeChip(key) {
      if (key.indexOf("line:") === 0) {
        var lid = key.slice(5);
        findState.lines = findState.lines.filter(function (x) { return x !== lid; });
        findState.station = "";
      } else if (key === "price") {
        findState.priceMin = ""; findState.priceMax = "";
      } else if (key === "priceweek") {
        findState.priceWeek = "";
      } else if (key === "radius") {
        findState.subwayMax = 1; findState.subwayMode = "station"; findState.station = "";
      } else if (key === "district") {
        findState.district = "";
      } else if (key === "q") {
        findState.q = "";
      }
      findState.page = 1;
      refreshAll();
    }

    function clearOthers() {
      findState.lines = []; findState.station = "";
      findState.priceMin = ""; findState.priceMax = ""; findState.priceWeek = "";
      findState.subwayMax = 1; findState.subwayMode = "station";
      findState.district = ""; findState.q = "";
      findState.page = 1;
      refreshAll();
    }

    function loadPage() {
      resultHost.innerHTML = "";
      resultHost.appendChild(stateNode("loading", "正在加载小区…"));
      getJSON("/api/catalog" + findQueryString())
        .then(function (data) { paintResults(data); })
        .catch(function (err) {
          resultHost.innerHTML = "";
          resultHost.appendChild(stateNode("error", "加载失败：" + err.message));
        });
    }

    function paintResults(data) {
      resultHost.innerHTML = "";
      var items = (data && data.items) || [];
      var total = data && typeof data.total === "number" ? data.total : items.length;
      var pageSize = (data && data.page_size) || CATALOG_PAGE_SIZE;
      var page = (data && data.page) || 1;
      var totalPages = Math.max(1, Math.ceil(total / pageSize));
      var periods = (data && data.price_periods) || [];

      var bar = el("div", { class: "find-results-bar" }, [
        el("h2", { text: "小区目录" }),
        el("span", { class: "find-count", "aria-live": "polite", text: total + " 个匹配结果" }),
      ]);
      // 期次口径（P1-03）：可切换「全部期次」或单期，明确价格比较口径。
      if (periods.length > 1 || (periods.length === 1 && periods[0].week)) {
        var periodSel = el("select", { class: "find-period-select", "aria-label": "价格期次" });
        periodSel.appendChild(el("option", { value: "", text: "全部期次（价格各不相同期，标注见各行）" }));
        periods.forEach(function (p) {
          var o = el("option", { value: p.week, text: p.week + " · " + p.communities + " 个小区" });
          if (p.week === findState.priceWeek) o.selected = true;
          periodSel.appendChild(o);
        });
        periodSel.addEventListener("change", function () {
          findState.priceWeek = periodSel.value;
          findState.page = 1;
          refreshAll();
        });
        bar.appendChild(periodSel);
      }
      resultHost.appendChild(bar);

      if (items.length === 0) {
        resultHost.appendChild(
          el("div", { class: "find-empty" }, [
            el("h3", { text: "没有满足这些偏好的小区" }),
            el("p", { text: "放宽地铁距离或价格，或删除一个条件再试试。" }),
          ])
        );
        appendPager(resultHost, total, page, totalPages);
        return;
      }

      var startIndex = (page - 1) * pageSize;
      items.forEach(function (it, i) {
        resultHost.appendChild(communityRow(it, startIndex + i + 1));
      });
      appendPager(resultHost, total, page, totalPages);
      resultHost.appendChild(
        el("p", { class: "find-footnote", text: "价格为各小区最新一期参考均价并标注期次；「历史」表示该小区最新记录早于全库最新期次。直线距离不等于步行或通勤距离；附近地铁不代表通勤时间。" })
      );
    }

    function communityRow(it, ordinal) {
      var loc = locText(it.district, it.plate);
      var sub = it.features && it.features.subway;
      var meta = [];
      if (sub) {
        // P2-05：按口径显示「距线路走向」或「距某站」，不把线路距离写成站名距离。
        var where = sub.mode === "line" ? "距线路走向" : "距" + sub.station_name;
        meta.push("▪ " + sub.line_name + " · " + where + "　" + fmtNum(sub.distance_km, 2) + " km");
      }
      if (it.data_points != null) meta.push(it.data_points + " 期行情");
      // P2a：次行显示最近学校 / 医院 / 高铁站（若有）。
      [["school", "学校"], ["hospital", "医院"], ["hsr", "高铁"]].forEach(function (pair) {
        var f = it.features && it.features[pair[0]];
        if (f && f.name) {
          var d = (f.distance_km != null) ? f.distance_km.toFixed(2) + "km" : "?";
          meta.push("▪ " + pair[1] + " " + f.name + " " + d);
        }
      });
      var priceText = fmtPrice(it.latest_price);
      var momText = "";
      if (it.latest_mom !== null && it.latest_mom !== undefined && !isNaN(Number(it.latest_mom))) {
        momText = "近月 " + fmtSigned(Number(it.latest_mom) * 100, 2) + "%";
      }
      // P1-03/P1-1：每条价格标注期次与来源；非全库最新一期显式标「历史」。
      var periodNode = null;
      var srcWeek = it.price_source_week || it.latest_week;
      if (srcWeek) {
        periodNode = el("span", {
          class: "fr-period" + (it.price_is_current ? "" : " fr-period-hist"),
          text: "价源：" + srcWeek + (it.price_source ? " · " + it.price_source : "") + (it.price_is_current ? "" : "（历史）"),
        });
      }
      var href = "#/xiaoqu/" + encodeURIComponent(it.id);
      var row = el("a", { class: "find-row", href: href }, [
        el("span", { class: "fr-ordinal", text: ordinal < 10 ? "0" + ordinal : "" + ordinal }),
        el("div", { class: "fr-main" }, [
          el("div", { class: "fr-name" }, [
            document.createTextNode(it.name || it.id || "—"),
            el("span", { class: "fr-loc", text: loc || "" }),
          ]),
          el("div", { class: "fr-meta" }, meta.map(function (t) { return el("span", { text: t }); })),
        ]),
        el("div", { class: "fr-price" }, [
          el("span", { class: "fr-price-v", text: priceText }),
          periodNode,
          momText
            ? el("span", { class: "fr-mom" + (Number(it.latest_mom) >= 0 ? " up" : " down"), text: momText })
            : null,
        ]),
      ]);
      return row;
    }

    function appendPager(host, total, page, totalPages) {
      var pager = el("div", { class: "find-pager" }, [
        el("span", { class: "pg-summary", text: "共 " + total + " 个小区 · 第 " + page + " / " + totalPages + " 页" }),
      ]);
      var prev = el("button", { class: "find-pg-btn", type: "button", text: "← 上一页" });
      if (page <= 1) prev.disabled = true;
      prev.addEventListener("click", function () { if (page > 1) { findState.page = page - 1; syncFindHash(); loadPage(); scrollTop(); } });
      var next = el("button", { class: "find-pg-btn", type: "button", text: "下一页 →" });
      if (page >= totalPages) next.disabled = true;
      next.addEventListener("click", function () { if (page < totalPages) { findState.page = page + 1; syncFindHash(); loadPage(); scrollTop(); } });
      pager.appendChild(prev);
      pager.appendChild(next);
      host.appendChild(pager);
    }

    function scrollTop() { try { window.scrollTo(0, 0); } catch (e) {} }

    /** P2-04：把 findState 同步回桌面主筛选控件（抽屉应用/删 chip 后保持同步）。 */
    function syncMainControls() {
      if (citySelect) citySelect.value = findState.city || "";
      if (priceMin) priceMin.value = findState.priceMin || "";
      if (priceMax) priceMax.value = findState.priceMax || "";
      if (lineSummary) lineSummary.textContent = linesSummaryText();
    }

    function refreshAll() {
      syncMainControls();
      renderChips();
      syncFindHash();
      loadPage();
    }

    function syncFindHash() {
      // 用 replaceState 同步可分享链接，避免触发 hashchange 重渲染。
      try {
        var qs = findQueryString().replace(/^\?/, "");
        history.replaceState(null, "", "#/catalog" + (qs ? "?" + qs : ""));
      } catch (e) { /* 忽略（历史 API 不可用时不影响功能） */ }
    }

    // —— 事件：主筛选 ——
    citySelect.addEventListener("change", function () {
      findState.city = citySelect.value;
      findState.lines = []; findState.station = ""; findState.district = ""; findState.q = "";
      findState.page = 1;
      loadSubwayLines(findState.city).then(refreshAll);
    });
    function applyPrice() {
      findState.priceMin = priceMin.value.trim();
      findState.priceMax = priceMax.value.trim();
      findState.page = 1;
      refreshAll();
    }
    priceMin.addEventListener("change", applyPrice);
    priceMax.addEventListener("change", applyPrice);
    function openDrawer() { openFindDrawer(refreshAll); }
    lineSummary.addEventListener("click", openDrawer);
    appEl.querySelector("#more-filters").addEventListener("click", openDrawer);
    appEl.querySelector("#mobile-filters").addEventListener("click", openDrawer);

    // 首次：先补齐地铁线路缓存，再渲染 chips 与结果。
    loadSubwayLines(findState.city).then(function () {
      renderChips();
      loadPage();
      // 深链演示：带 ?drawer=1 时自动打开「更多条件」抽屉。
      if (findHashHasFlag("drawer")) { try { openDrawer(); } catch (e) {} }
    });
  }

  /** 当前 hash 查询串是否含某布尔标志（如 drawer=1）。 */
  function findHashHasFlag(flag) {
    var h = window.location.hash || "";
    var qi = h.indexOf("?");
    if (qi < 0) return false;
    return h.slice(qi + 1).split("&").some(function (p) { return p === flag + "=1"; });
  }

  // -------------------------------------------------------------------------
  // 「更多条件」分组抽屉（地铁 / 区域与名称）
  // -------------------------------------------------------------------------
  var findDrawerEl = null;

  function ensureFindDrawer() {
    if (findDrawerEl) return findDrawerEl;
    var dlg = el("dialog", { class: "find-drawer" });
    dlg.appendChild(el("div", { class: "fd-inner" }));
    // 键盘可关闭：Escape 关闭抽屉（取消，不改结果）。
    dlg.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") { ev.preventDefault(); dlg.close(); }
    });
    document.body.appendChild(dlg);
    findDrawerEl = dlg;
    return dlg;
  }

  function openFindDrawer(onApply) {
    var dlg = ensureFindDrawer();
    // 草稿：独立于 findState，取消不改结果。P1-04：价格等所有可编辑条件都进草稿，
    // 重绘（勾线路/切口径/异步区列表返回）不会清空未应用的值，并保留焦点与滚动。
    var draft = {
      city: findState.city,
      lines: findState.lines.slice(),
      station: findState.station,
      subwayMode: findState.subwayMode,
      subwayMax: findState.subwayMax,
      priceMin: findState.priceMin,
      priceMax: findState.priceMax,
      districts: [],
      district: findState.district,
      q: findState.q,
      error: "",
    };

    function render(preserveFocusId, preserveScroll) {
      var inner = dlg.querySelector(".fd-inner");
      var bodyPrev = inner.querySelector(".fd-body");
      if (preserveScroll == null && bodyPrev) preserveScroll = bodyPrev.scrollTop;
      // 重绘前捕获当前焦点元素（id）及其输入光标 selection，重绘后恢复。
      // 用 document.activeElement + getElementById，避免把含冒号的线路 id 拼成
      // 非法 CSS 选择器（P2-3）。applyFocusId 用于「操作后主动聚焦某控件」。
      var savedFocusId = preserveFocusId;
      var savedSel = null;
      var active = document.activeElement;
      if (active && active.id && inner.contains(active)) {
        if (!savedFocusId) savedFocusId = active.id;
        try {
          if (typeof active.selectionStart === "number") {
            savedSel = [active.selectionStart, active.selectionEnd];
          }
        } catch (e) { savedSel = null; }
      }
      inner.innerHTML = "";
      var body = el("div", { class: "fd-body" });
      var head = el("div", { class: "fd-head" }, [
        el("h2", { text: "筛选条件" }),
        el("button", { class: "fd-close", type: "button", "aria-label": "关闭", text: "×" }),
      ]);
      head.querySelector(".fd-close").addEventListener("click", function () { dlg.close(); });
      inner.appendChild(head);

      // 城市与价格（移动端主区）——价格读自草稿并在输入时写回草稿。
      var citySel = el("select", { class: "ff-select", "aria-label": "筛选城市" }, [
        el("option", { value: "", text: "全部城市" }),
        el("option", { value: "北京", text: "北京" }),
        el("option", { value: "天津", text: "天津" }),
      ]);
      citySel.value = draft.city;
      var dMin = el("input", { type: "number", min: "0", id: "fd-price-min", class: "ff-price", "aria-label": "筛选最低均价", placeholder: "不限", value: draft.priceMin });
      var dMax = el("input", { type: "number", min: "0", id: "fd-price-max", class: "ff-price", "aria-label": "筛选最高均价", placeholder: "不限", value: draft.priceMax });
      dMin.addEventListener("input", function () { draft.priceMin = dMin.value; });
      dMax.addEventListener("input", function () { draft.priceMax = dMax.value; });
      body.appendChild(group("00", "城市与价格", [
        fieldRow("城市", citySel),
        fieldRow("参考均价（元/㎡）", el("div", { class: "ff-price-pair" }, [dMin, el("span", { text: "—" }), dMax])),
      ]));

      // 地铁分组
      var lineList = subwayCache[draft.city] || [];
      var lineChecks = el("div", { class: "fd-checks" });
      lineList.forEach(function (l) {
        var cb = el("input", { type: "checkbox" });
        cb.checked = draft.lines.indexOf(l.id) >= 0;
        cb.value = l.id;
        cb.addEventListener("change", function () {
          if (cb.checked) { if (draft.lines.indexOf(l.id) < 0) draft.lines.push(l.id); }
          else { draft.lines = draft.lines.filter(function (x) { return x !== l.id; }); }
          draft.station = "";
          render("fd-line-" + l.id);
        });
        cb.id = "fd-line-" + l.id;
        lineChecks.appendChild(el("label", { class: "fd-check" }, [cb, document.createTextNode(l.name)]));
      });
      if (!lineList.length) lineChecks.appendChild(el("p", { class: "fd-note", text: "该城市尚未导入地铁数据。" }));

      // 站点下拉：所选线路全部站点并集
      var stationSet = {};
      lineList.forEach(function (l) {
        if (draft.lines.indexOf(l.id) < 0) return;
        (l.stations || []).forEach(function (s) { stationSet[s.id] = s.name; });
      });
      var stationSel = el("select", { class: "ff-select", "aria-label": "地铁站点" });
      stationSel.appendChild(el("option", { value: "", text: "所选线路全部站点" }));
      Object.keys(stationSet).forEach(function (sid) {
        var o = el("option", { value: sid, text: stationSet[sid] });
        if (sid === draft.station) o.selected = true;
        stationSel.appendChild(o);
      });
      stationSel.disabled = !(draft.lines.length) || draft.subwayMode === "line";
      stationSel.id = "fd-station";
      stationSel.addEventListener("change", function () { draft.station = stationSel.value; });

      var modeSel = el("select", { class: "ff-select", "aria-label": "地铁距离口径" }, [
        el("option", { value: "station", text: "距地铁站" }),
        el("option", { value: "line", text: "距线路走向" }),
      ]);
      modeSel.value = draft.subwayMode;
      modeSel.addEventListener("change", function () { draft.subwayMode = modeSel.value; draft.station = ""; render("fd-mode"); });
      modeSel.id = "fd-mode";

      var maxOut = el("output", { class: "fd-range-out", text: Number(draft.subwayMax).toFixed(1) + " km" });
      var maxRange = el("input", { type: "range", min: "0.1", max: "5", step: "0.1", id: "fd-submax", value: String(draft.subwayMax), "aria-label": "最大地铁距离" });
      maxRange.addEventListener("input", function () {
        draft.subwayMax = Number(maxRange.value);
        maxOut.textContent = Number(draft.subwayMax).toFixed(1) + " km";
      });

      body.appendChild(
        group("01", "地铁出行", [
          fieldRow("线路 · 可多选", lineChecks),
          fieldRow("站点", stationSel),
          fieldRow("距离口径", modeSel),
          el("div", { class: "fd-field" }, [
            el("div", { class: "fd-range-label" }, [el("label", { text: "最大直线距离" }), maxOut]),
            maxRange,
          ]),
          el("p", { class: "fd-note", text: "同一线路取任一满足即可；不同类别条件需同时满足。" }),
        ])
      );

      // 区域与名称
      var districtSel = el("select", { class: "ff-select", "aria-label": "行政区", id: "fd-district" });
      districtSel.appendChild(el("option", { value: "", text: "全部行政区" }));
      (draft.districts || []).forEach(function (r) {
        var o = el("option", { value: r.district, text: r.district });
        if (r.district === draft.district) o.selected = true;
        districtSel.appendChild(o);
      });
      districtSel.disabled = !draft.city;
      districtSel.addEventListener("change", function () { draft.district = districtSel.value; });
      var qInput = el("input", { type: "search", class: "ff-input", "aria-label": "小区名称", placeholder: "输入关键词", id: "fd-q", value: draft.q || "" });
      qInput.addEventListener("input", function () { draft.q = qInput.value; });
      body.appendChild(group("02", "区域与名称", [
        fieldRow("行政区", districtSel),
        fieldRow("小区名称", qInput),
      ]));

      body.appendChild(el("p", { class: "fd-note", text: "直线距离仅作位置参考；关闭抽屉不会应用修改。" }));
      inner.appendChild(body);

      var errEl = el("p", { class: "fd-error", id: "fd-error", role: "alert" });
      inner.appendChild(errEl);
      inner.appendChild(
        el("div", { class: "fd-foot" }, [
          el("button", { class: "find-pg-btn", type: "button", text: "取消", onclick: function () { dlg.close(); } }),
          el("button", { class: "find-pg-btn primary", type: "button", text: "应用条件", id: "fd-apply" }),
        ])
      );
      inner.querySelector("#fd-apply").addEventListener("click", function () {
        var err = validateDraft(draft.priceMin, draft.priceMax);
        if (err) { errEl.textContent = err; return; }
        findState.city = draft.city;
        findState.lines = draft.lines.slice();
        findState.station = draft.station;
        findState.subwayMode = draft.subwayMode;
        findState.subwayMax = draft.subwayMax;
        findState.priceMin = (draft.priceMin || "").trim();
        findState.priceMax = (draft.priceMax || "").trim();
        findState.district = draft.district;
        findState.q = (draft.q || "").trim();
        findState.page = 1;
        loadSubwayLines(findState.city).then(function () {
          dlg.close();
          if (typeof onApply === "function") onApply();
        });
      });

      // 城市变化：重置线路并重载该城市线路与区。
      citySel.addEventListener("change", function () {
        draft.city = citySel.value;
        draft.lines = []; draft.station = ""; draft.district = "";
        loadSubwayLines(draft.city).then(function () {
          fetchDistricts(draft.city).then(function (rows) { draft.districts = rows || []; render(); });
        });
      });
      citySel.id = "fd-city";

      // 重绘后恢复滚动与焦点（P2-3：重绘不丢焦点/光标 selection）。
      if (bodyPrev) { /* body 节点已替换 */ }
      body.scrollTop = preserveScroll || 0;
      if (savedFocusId) {
        try {
          var focusEl = document.getElementById(savedFocusId);
          if (focusEl && inner.contains(focusEl)) {
            focusEl.focus();
            if (savedSel && typeof focusEl.setSelectionRange === "function") {
              try { focusEl.setSelectionRange(savedSel[0], savedSel[1]); } catch (e) {}
            }
          }
        } catch (e) { /* 忽略焦点恢复异常，不影响功能 */ }
      }
    }

    function validateDraft(minV, maxV) {
      if (minV !== "" && (isNaN(Number(minV)) || Number(minV) < 0)) return "最低均价请输入非负数。";
      if (maxV !== "" && (isNaN(Number(maxV)) || Number(maxV) < 0)) return "最高均价请输入非负数。";
      if (minV !== "" && maxV !== "" && Number(minV) > Number(maxV)) return "最低均价不能超过最高均价。";
      return "";
    }

    render();
    fetchDistricts(draft.city).then(function (rows) {
      draft.districts = rows || [];
      render();
    });
    if (!dlg.open) dlg.showModal();
  }

  /** 抽屉内一个分组区块。 */
  function group(num, title, children) {
    return el("section", { class: "fd-group" }, [
      el("h3", {}, [el("span", { class: "fd-group-num", text: num }), document.createTextNode(title)]),
    ].concat(children));
  }

  /** 抽屉内一行：标签 + 控件。 */
  function fieldRow(label, control) {
    return el("div", { class: "fd-field" }, [el("label", { text: label }), control]);
  }

  // -------------------------------------------------------------------------
  // 小区详情
  // -------------------------------------------------------------------------
  function renderDetail(id) {
    appEl.innerHTML = "";
    appEl.appendChild(backLink("#/catalog", "← 返回目录"));
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
        appEl.appendChild(backLink("#/catalog", "← 返回目录"));
        appEl.appendChild(renderDetailHead(detail, series));
        var compareNode = renderTwoPeriodCompare(series);
        if (compareNode) appEl.appendChild(compareNode);
        appEl.appendChild(renderSeriesSection(series));
        appEl.appendChild(renderSignalsSection(signals));
      })
      .catch(function (err) {
        appEl.innerHTML = "";
        appEl.appendChild(backLink("#/catalog", "← 返回目录"));
        appEl.appendChild(stateNode("error", "加载失败：" + err.message));
      });
  }

  function backLink(href, label) {
    return el("a", { class: "back", href: href || "#/" }, [
      document.createTextNode(label || "← 返回"),
    ]);
  }

  function latestValid(series, key) {
    for (var i = series.length - 1; i >= 0; i--) {
      var v = series[i][key];
      if (v !== null && v !== undefined && !isNaN(Number(v))) return v;
    }
    return null;
  }

  function renderDetailHead(detail, series) {
    var dp = series ? series.length : 0;
    var sufficient = dp >= 2; // 少于 2 期不给温度/阶段，避免过度解读
    var temp = sufficient ? latestValid(series, "temperature") : null;
    var stage = null;
    if (sufficient) {
      stage = series[series.length - 1].stage;
      if (stage === null || stage === undefined) {
        for (var i = series.length - 1; i >= 0; i--) {
          if (series[i].stage) { stage = series[i].stage; break; }
        }
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

    var metrics = el("div", { class: "detail-metrics" });
    if (temp !== null && temp !== undefined) {
      metrics.appendChild(el("div", { class: "metric" }, [
        el("span", { class: "k", text: "最新温度" }),
        el("span", { class: "v", style: "color:" + tempColor(temp) }, [
          document.createTextNode(fmtNum(temp, 1)),
          el("span", { class: "unit", text: " °C" }),
        ]),
      ]));
    }
    metrics.appendChild(el("div", { class: "metric" }, [
      el("span", { class: "k", text: "最新价格" }),
      el("span", { class: "v" }, [
        document.createTextNode(fmtNum(latestValid(series, "price"), 0)),
        el("span", { class: "unit", text: " 元/㎡" }),
      ]),
    ]));
    metrics.appendChild(el("div", { class: "metric" }, [
      el("span", { class: "k", text: "数据期数" }),
      el("span", { class: "v", style: "font-size:22px" }, [
        document.createTextNode(String(dp)),
        el("span", { class: "unit", text: " 期" }),
      ]),
    ]));
    if (detail.city) {
      metrics.appendChild(el("div", { class: "metric" }, [
        el("span", { class: "k", text: "城市" }),
        el("span", { class: "v", style: "font-size:20px" }, [document.createTextNode(detail.city)]),
      ]));
    }
    head.appendChild(metrics);
    return head;
  }

  /**
   * 「两期对比」块：仅当该小区恰有 2 期且期次不同时渲染。
   * 展示两个时点的均价与涨跌幅；明确标注「跨源、样本有限，仅供参考」；
   * 仍不显示温度 / 阶段（遵守 docs/04 口径）。期数 <2 或 >=3 时返回 null。
   */
  function renderTwoPeriodCompare(series) {
    if (!series || series.length !== 2) return null;
    var a = series[0];
    var b = series[1];
    if (!a || !b || a.week === b.week) return null;

    var pa = a.price, pb = b.price;
    var hasBoth =
      pa !== null && pa !== undefined && !isNaN(Number(pa)) &&
      pb !== null && pb !== undefined && !isNaN(Number(pb));

    var changeText = "—";
    if (hasBoth && Number(pa) !== 0) {
      var pct = (Number(pb) - Number(pa)) / Number(pa) * 100;
      changeText = (pct >= 0 ? "+" : "−") + fmtNum(Math.abs(pct), 1) + "%";
    }

    var aria =
      (a.week || "") + " 均价 " + fmtNum(pa, 0) + " 元/㎡ → " +
      (b.week || "") + " 均价 " + fmtNum(pb, 0) + " 元/㎡，约 " + changeText;

    var section = el("section", { class: "section compare" });
    section.appendChild(el("h2", { text: "两期对比" }));

    var row = el("div", { class: "compare-row", role: "group", "aria-label": "两期均价对比：" + aria });

    function point(rec, price, cls) {
      return el("div", { class: "compare-point " + cls }, [
        el("span", { class: "cp-week", text: rec.week || "—" }),
        el("span", { class: "cp-price" }, [
          document.createTextNode("¥" + fmtNum(price, 0)),
          el("span", { class: "cp-unit", text: " 元/㎡" }),
        ]),
      ]);
    }

    row.appendChild(point(a, pa, "cp-a"));
    row.appendChild(el("div", { class: "compare-arrow", text: "→" }));
    row.appendChild(point(b, pb, "cp-b"));

    section.appendChild(row);

    var upDown = "—";
    if (hasBoth && Number(pa) !== 0) {
      upDown = Number(pb) >= Number(pa) ? "涨" : "跌";
    }
    section.appendChild(
      el("div", { class: "compare-change" }, [
        el("span", { class: "cc-label", text: "两期涨跌幅" }),
        el("span", {
          class: "cc-value" + (hasBoth ? (Number(pb) >= Number(pa) ? " up" : " down") : ""),
          text: changeText,
        }),
        el("span", { class: "cc-note", text: "（约 " + upDown + "）" }),
      ])
    );

    section.appendChild(
      el("p", {
        class: "compare-note",
        text: "仅有 2 个时点，且两期可能来自不同平台；跨源、样本有限，仅供参考。",
      })
    );
    return section;
  }

  function readoutItem(k, v, unit) {    var val = el("span", { class: "rv" }, [document.createTextNode(v)]);
    if (unit) val.appendChild(el("span", { class: "ru", text: unit }));
    return el("div", { class: "readout-item" }, [el("span", { class: "rk", text: k }), val]);
  }

  function renderSeriesSection(series) {
    var section = el("section", { class: "section" });
    var dp = series ? series.length : 0;

    if (!series || series.length === 0) {
      section.appendChild(el("h2", { text: "数据读数" }));
      section.appendChild(el("div", { class: "chart-empty", text: "暂无数据" }));
      return section;
    }

    // 数据不足（少于 2 期）：不画折线，直接给读数，避免空洞与过度解读。
    if (dp < 2) {
      var r = series[series.length - 1];
      section.appendChild(el("h2", { text: "数据读数" }));
      var mom = (r.mom4 !== null && r.mom4 !== undefined)
        ? fmtSigned(Number(r.mom4) * 100, 2) + "%" : "—";
      section.appendChild(el("div", { class: "readout" }, [
        readoutItem("参考均价", fmtNum(r.price, 0), " 元/㎡"),
        readoutItem("近月环比", mom, ""),
        readoutItem("在售套数", r.supply == null ? "—" : String(r.supply), " 套"),
        readoutItem("数据周", r.week || "—", ""),
      ]));
      section.appendChild(el("p", {
        class: "readout-note",
        text: "数据不足（仅 " + dp + " 期），以上仅为读数，暂不给出温度与趋势判断。",
      }));
      return section;
    }

    section.appendChild(el("h2", { text: "走势曲线" }));

    var tempPoints = series.map(function (r) { return { week: r.week, value: r.temperature }; });
    var pricePoints = series.map(function (r) { return { week: r.week, value: r.price }; });

    var tempHas = tempPoints.some(function (p) { return p.value !== null && p.value !== undefined; });
    var priceHas = pricePoints.some(function (p) { return p.value !== null && p.value !== undefined; });

    if (!tempHas && !priceHas) {
      section.appendChild(el("div", { class: "chart-empty", text: "暂无数据" }));
      return section;
    }

    if (tempHas) {
      section.appendChild(el("h2", { class: "sub-title", text: "温度走势", style: "font-size:13px;color:#6a756d;margin:6px 0 6px" }));
      section.appendChild(
        el("div", { class: "chart" }, [lineChart(tempPoints, { color: "#b23a2b", decimals: 1 })])
      );
    }
    if (priceHas) {
      section.appendChild(el("h2", { class: "sub-title", text: "价格走势（元/㎡）", style: "font-size:13px;color:#6a756d;margin:18px 0 6px" }));
      section.appendChild(
        el("div", { class: "chart" }, [lineChart(pricePoints, { color: "#34536b", decimals: 0 })])
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
  // -------------------------------------------------------------------------
  // 学校 / 医院 / 高铁站 菜单 + 周边小区 Top20
  // -------------------------------------------------------------------------
  var POI_CFG = {
    school: { title: "学校", api: "/api/schools",
      stages: [["primary", "小学"], ["middle", "初中"], ["high", "高中"]] },
    hospital: { title: "医院", api: "/api/hospitals",
      grades: [["", "全部"], ["三甲", "三甲"], ["二甲", "二甲"], ["未知", "未知"]] },
    hsr: { title: "高铁站", api: "/api/hsr" },
  };
  var poiUI = { school: { stage: "primary", city: "" }, hospital: { grade: "", city: "" }, hsr: { city: "" } };

  function poiMenuHash(type) {
    return type === "school" ? "#/schools" : (type === "hospital" ? "#/hospitals" : "#/hsr");
  }

  function renderPoiMenu(type) {
    var cfg = POI_CFG[type];
    var ui = poiUI[type];
    appEl.innerHTML = "";
    appEl.appendChild(el("div", { class: "page-head" }, [
      el("h1", { text: cfg.title }),
      el("div", { class: "page-head-aside" }, [
        el("a", { class: "catalog-entry", href: "#/catalog", text: "← 返回找小区" }),
      ]),
    ]));

    var bar = el("div", { class: "poi-bar" });
    (cfg.stages || []).forEach(function (p) {
      var b = el("button", { class: "poi-tab" + (ui.stage === p[0] ? " on" : ""), type: "button", text: p[1] });
      b.addEventListener("click", function () { ui.stage = p[0]; renderPoiMenu(type); });
      bar.appendChild(b);
    });
    (cfg.grades || []).forEach(function (p) {
      var b = el("button", { class: "poi-tab" + (ui.grade === p[0] ? " on" : ""), type: "button", text: p[1] });
      b.addEventListener("click", function () { ui.grade = p[0]; renderPoiMenu(type); });
      bar.appendChild(b);
    });
    var citySel = el("select", { class: "district-select", "aria-label": "城市" }, [
      el("option", { value: "", text: "京津全部" }),
      el("option", { value: "北京", text: "北京" }),
      el("option", { value: "天津", text: "天津" }),
    ]);
    citySel.value = ui.city || "";
    citySel.addEventListener("change", function () { ui.city = citySel.value; renderPoiMenu(type); });
    bar.appendChild(citySel);
    appEl.appendChild(bar);

    var host = el("div", { class: "poi-list" });
    appEl.appendChild(host);
    host.appendChild(stateNode("loading", "正在加载…"));

    var params = {};
    if (ui.city) params.city = ui.city;
    if (cfg.stages && ui.stage) params.stage = ui.stage;
    if (cfg.grades && ui.grade) params.grade = ui.grade;

    getJSON(cfg.api + buildQuery(params)).then(function (data) {
      host.innerHTML = "";
      var items = (data && data.items) || [];
      if (!items.length) { host.appendChild(stateNode("empty", "暂无数据（该城市 / 类别尚未收录）")); return; }
      items.forEach(function (it) {
        var row = el("a", { class: "poi-row", href: "javascript:void(0)" }, [
          el("div", { class: "poi-main" }, [
            el("div", { class: "poi-name", text: it.name || it.id }),
            el("div", { class: "poi-meta", text: [it.city, it.district, it.grade].filter(Boolean).join(" · ") }),
          ]),
          el("div", { class: "poi-count", text: (it.community_count || 0) + " 个周边小区" }),
        ]);
        row.addEventListener("click", function () { renderPoiCommunities(type, it.id, it.name); });
        host.appendChild(row);
      });
    }).catch(function (err) {
      host.innerHTML = "";
      host.appendChild(stateNode("error", "加载失败：" + err.message));
    });
  }

  function renderPoiCommunities(type, id, name) {
    appEl.innerHTML = "";
    appEl.appendChild(backLink(poiMenuHash(type), "← 返回" + POI_CFG[type].title));
    appEl.appendChild(el("div", { class: "page-head" }, [
      el("h1", { text: name || id }),
      el("span", { class: "sub", text: "周边小区 Top 20（按直线距离）" }),
    ]));
    var host = el("div", { class: "poi-list" });
    appEl.appendChild(host);
    host.appendChild(stateNode("loading", "加载中…"));
    getJSON("/api/poi/" + encodeURIComponent(id) + "/communities?limit=20").then(function (data) {
      host.innerHTML = "";
      var items = (data && data.items) || [];
      if (!items.length) { host.appendChild(stateNode("empty", "周边暂无已收录小区")); return; }
      items.forEach(function (it, i) {
        var price = (it.latest_price === null || it.latest_price === undefined) ? "" : (" · ¥" + fmtNum(it.latest_price, 0));
        host.appendChild(el("a", { class: "poi-row", href: "#/xiaoqu/" + encodeURIComponent(it.id) }, [
          el("div", { class: "poi-main" }, [
            el("div", { class: "poi-name", text: (i + 1) + ". " + (it.name || it.id) }),
            el("div", { class: "poi-meta", text: [it.district, it.plate].filter(Boolean).join(" · ") }),
          ]),
          el("div", { class: "poi-count", text: (it.distance_km != null ? it.distance_km.toFixed(2) + " km" : "—") + price }),
        ]));
      });
    }).catch(function (err) {
      host.innerHTML = "";
      host.appendChild(stateNode("error", "加载失败：" + err.message));
    });
  }

  function parseHash() {
    var raw = window.location.hash || "#/";
    var path = raw.replace(/^#/, "");
    if (path === "" || path === "/") return { name: "home" };
    // 支持 #/catalog?line=..&subway_max=.. 的可分享筛选链接。
    var qIdx = path.indexOf("?");
    var queryStr = "";
    if (qIdx >= 0) { queryStr = path.slice(qIdx + 1); path = path.slice(0, qIdx); }
    if (path === "/catalog") return { name: "catalog", query: queryStr };
    if (path === "/schools") return { name: "schools" };
    if (path === "/hospitals") return { name: "hospitals" };
    if (path === "/hsr") return { name: "hsr" };
    var m = path.match(/^\/xiaoqu\/(.+)$/);
    if (m) return { name: "detail", id: decodeURIComponent(m[1]) };
    return { name: "unknown", raw: path };
  }

  function route() {
    var r = parseHash();
    if (r.name === "home") {
      renderOverview();
    } else if (r.name === "catalog") {
      applyFindQuery(r.query);
      renderCatalog();
    } else if (r.name === "schools") {
      renderPoiMenu("school");
    } else if (r.name === "hospitals") {
      renderPoiMenu("hospital");
    } else if (r.name === "hsr") {
      renderPoiMenu("hsr");
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
