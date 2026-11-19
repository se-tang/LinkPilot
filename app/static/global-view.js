/* ---------------- 全球视图（测试链路 / 日常链路监测） ---------------- */
// CSP 限制下不加载任何外部地图库：底图取 /static/world-110m.json（已按
// Natural Earth I 投影预算成单位坐标，见 tools/gen_world_map.py），这里只做
// 线性缩放；节点/连线用同一投影现场换算。

// 常用地区标记 → 经纬度。没登记的地区不上图，明细列表里仍会出现，
// 提示用户补一个标准地区名（或直接使用下面的英文别名）。
const GEO_COORDS = {
  '北京': [116.41, 39.90], '上海': [121.47, 31.23], '广州': [113.26, 23.13], '深圳': [114.06, 22.55],
  '杭州': [120.16, 30.27], '南京': [118.80, 32.06], '苏州': [120.58, 31.30], '成都': [104.07, 30.57],
  '重庆': [106.55, 29.56], '武汉': [114.31, 30.59], '西安': [108.94, 34.34], '郑州': [113.63, 34.75],
  '青岛': [120.38, 36.07], '济南': [117.12, 36.65], '天津': [117.20, 39.08], '沈阳': [123.43, 41.81],
  '大连': [121.61, 38.91], '哈尔滨': [126.53, 45.80], '长春': [125.32, 43.90], '厦门': [118.09, 24.48],
  '福州': [119.30, 26.08], '长沙': [112.94, 28.23], '合肥': [117.28, 31.86], '昆明': [102.83, 24.88],
  '贵阳': [106.63, 26.65], '南宁': [108.37, 22.82], '海口': [110.20, 20.04], '兰州': [103.83, 36.06],
  '乌鲁木齐': [87.62, 43.83], '拉萨': [91.11, 29.66], '呼和浩特': [111.75, 40.84], '西宁': [101.78, 36.62],
  '银川': [106.23, 38.49], '石家庄': [114.51, 38.04], '太原': [112.55, 37.87], '南昌': [115.89, 28.68],
  '香港': [114.17, 22.32], '澳门': [113.55, 22.20], '台北': [121.56, 25.03], '高雄': [120.31, 22.62],
  '东京': [139.69, 35.69], '大阪': [135.50, 34.69], '首尔': [126.98, 37.57], '釜山': [129.08, 35.18],
  '新加坡': [103.82, 1.35], '吉隆坡': [101.69, 3.14], '曼谷': [100.50, 13.76], '雅加达': [106.85, -6.21],
  '马尼拉': [120.98, 14.60], '河内': [105.83, 21.03], '胡志明市': [106.63, 10.82], '金边': [104.92, 11.56],
  '仰光': [96.16, 16.87], '达卡': [90.41, 23.81], '科伦坡': [79.86, 6.93], '孟买': [72.88, 19.08],
  '新德里': [77.21, 28.61], '金奈': [80.27, 13.08], '加尔各答': [88.36, 22.57],
  '迪拜': [55.27, 25.20], '阿布扎比': [54.37, 24.45], '利雅得': [46.68, 24.71], '多哈': [51.53, 25.29],
  '德黑兰': [51.39, 35.69], '伊斯坦布尔': [28.98, 41.01], '特拉维夫': [34.78, 32.08],
  '伦敦': [-0.13, 51.51], '巴黎': [2.35, 48.86], '法兰克福': [8.68, 50.11], '柏林': [13.40, 52.52],
  '慕尼黑': [11.58, 48.14], '阿姆斯特丹': [4.90, 52.37], '布鲁塞尔': [4.35, 50.85], '苏黎世': [8.54, 47.38],
  '维也纳': [16.37, 48.21], '马德里': [-3.70, 40.42], '巴塞罗那': [2.17, 41.39], '罗马': [12.50, 41.90],
  '米兰': [9.19, 45.46], '里斯本': [-9.14, 38.72], '都柏林': [-6.26, 53.35], '斯德哥尔摩': [18.07, 59.33],
  '奥斯陆': [10.75, 59.91], '哥本哈根': [12.57, 55.68], '赫尔辛基': [24.94, 60.17], '华沙': [21.01, 52.23],
  '布拉格': [14.44, 50.08], '布达佩斯': [19.04, 47.50], '布加勒斯特': [26.10, 44.43], '雅典': [23.73, 37.98],
  '苏瓦': [178.44, -18.14], '努库阿洛法': [-175.20, -21.14],
  '莫斯科': [37.62, 55.76], '圣彼得堡': [30.34, 59.93], '基辅': [30.52, 50.45], '明斯克': [27.57, 53.90],
  '纽约': [-74.01, 40.71], '洛杉矶': [-118.24, 34.05], '圣何塞': [-121.89, 37.34], '西雅图': [-122.33, 47.61],
  '芝加哥': [-87.63, 41.88], '达拉斯': [-96.80, 32.78], '迈阿密': [-80.19, 25.77], '亚特兰大': [-84.39, 33.75],
  '波士顿': [-71.06, 42.36], '华盛顿': [-77.04, 38.91], '阿什本': [-77.49, 39.04], '弗吉尼亚': [-77.49, 39.04],
  '凤凰城': [-112.07, 33.45], '丹佛': [-104.99, 39.74], '盐湖城': [-111.89, 40.76], '波特兰': [-122.68, 45.52],
  '休斯顿': [-95.37, 29.76], '明尼阿波利斯': [-93.27, 44.98], '堪萨斯城': [-94.58, 39.10],
  '多伦多': [-79.38, 43.65], '温哥华': [-123.12, 49.28], '蒙特利尔': [-73.57, 45.50], '墨西哥城': [-99.13, 19.43],
  '巴拿马城': [-79.52, 8.98], '波哥大': [-74.07, 4.71], '利马': [-77.04, -12.05],
  '圣保罗': [-46.63, -23.55], '里约热内卢': [-43.17, -22.91], '布宜诺斯艾利斯': [-58.38, -34.60],
  '圣地亚哥': [-70.67, -33.45], '加拉加斯': [-66.90, 10.49], '蒙特维多': [-56.16, -34.90],
  '开罗': [31.24, 30.04], '约翰内斯堡': [28.05, -26.20], '开普敦': [18.42, -33.92], '内罗毕': [36.82, -1.29],
  '拉各斯': [3.38, 6.52], '卡萨布兰卡': [-7.59, 33.57], '毛里求斯': [57.50, -20.16],
  '悉尼': [151.21, -33.87], '墨尔本': [144.96, -37.81], '布里斯班': [153.03, -27.47], '珀斯': [115.86, -31.95],
  '奥克兰': [174.76, -36.85], '惠灵顿': [174.78, -41.29],
  // 常见英文别名
  'Hong Kong': [114.17, 22.32], 'Taipei': [121.56, 25.03], 'Tokyo': [139.69, 35.69],
  'Seoul': [126.98, 37.57], 'Singapore': [103.82, 1.35], 'Los Angeles': [-118.24, 34.05],
  'San Jose': [-121.89, 37.34], 'Silicon Valley': [-121.89, 37.34], 'Seattle': [-122.33, 47.61],
  'Frankfurt': [8.68, 50.11], 'London': [-0.13, 51.51], 'Amsterdam': [4.90, 52.37],
  'Paris': [2.35, 48.86], 'Dubai': [55.27, 25.20], 'Mumbai': [72.88, 19.08],
  'Ashburn': [-77.49, 39.04], 'Virginia': [-77.49, 39.04], 'Sydney': [151.21, -33.87],
  'Moscow': [37.62, 55.76], 'Sao Paulo': [-46.63, -23.55], 'Istanbul': [28.98, 41.01],
};

const GEO = (() => {
  const RATING_STEPS = [[60, 's', '优', '--green'], [120, 'a', '良', '--lime'],
                        [200, 'b', '中', '--orange'], [Infinity, 'c', '差', '--red']];
  const STATUS_BADGE = {warn: ['网络异常', 'warn'], down: ['不可达', 'down'],
                        stale: ['数据过期', 'stale']};
  const NS = 'http://www.w3.org/2000/svg';
  const VIEW_INTERVAL = 5000;      // 全球视图数据轮询间隔
  const LS_KEY = 'lp-geo-view';    // 视图偏好（来源/视图模式/自动刷新）本地持久化
  const RTT_SCALE = 300;           // 表格 RTT 条形图满格对应的 ms

  let inited = false, active = false, pollTimer = null, rafId = null;
  let source = 'test';             // 'test' = 最近一轮测试；'daily' = 日常监测
  let viewMode = 'A';              // 日常链路的「数据视图」：切 A/B 不影响后台任务
  let taskFilter = null;           // null = 当前模式的全部任务
  let runFilter = null;            // null = 最新一轮测试记录
  let autoRefresh = true;          // 仅控制前端轮询，与任务运行无关
  let daily = null, test = null;   // 两个接口的最近数据
  let selected = null, hoverKey = null;
  let W = 0, H = 0, S = 1, TX = 0, TY = 0;
  let gLand, gGrat, gLinks, gDots, gNodes, gLabels, gHit, gWorld, dotPat;
  let vk = 1, vx = 0, vy = 0;      // 地图视图变换：屏幕 = 世界 × k + t（拖拽/缩放）
  let wheelArmed = false, hintTimer = null;   // 点击激活后才捕获滚轮，避免上/下滑页面被地图半路截停
  let linkEls = new Map(), dotEls = new Map();
  let reducing = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  let historyRange = '4h';
  let historyCache = {key: '', data: null};
  let watchTasksCache = [], pendingDeleteId = null;
  let forceVisible = false;        // 诊断/自动化测试用：绕过后台暂停（正常 UI 不触达）
  let relayoutTimer = null;

  // 视图偏好恢复（LinkPilot 自身的 localStorage，替代原型的 window.zcode 依赖）
  try {
    const saved = JSON.parse(localStorage.getItem(LS_KEY) || '{}');
    if (saved.source) source = saved.source === 'daily' ? 'daily' : 'test';
    if (saved.viewMode) viewMode = saved.viewMode === 'B' ? 'B' : 'A';
    if (typeof saved.autoRefresh === 'boolean') autoRefresh = saved.autoRefresh;
  } catch (e) {}
  const persistView = () => {
    try { localStorage.setItem(LS_KEY, JSON.stringify({source, viewMode, autoRefresh})); }
    catch (e) {}
  };

  const rating = (rtt) => {
    if (rtt == null) return {k: 'off', label: '无数据', color: 'var(--muted)'};
    for (const [max, k, label, v] of RATING_STEPS) {
      if (rtt < max) return {k, label, color: `var(${v})`};
    }
    return RATING_STEPS[RATING_STEPS.length - 1];
  };
  const statusBadge = (s) => {
    const it = STATUS_BADGE[s];
    return it ? `<span class="geo-status ${it[1]}">${it[0]}</span>` : '';
  };
  const coord = (m) => {
    // 优先机器上手动填写的坐标（十进制小数），其次地区标记匹配
    if (m && m.lat && m.lng) {
      const lat = parseFloat(m.lat), lng = parseFloat(m.lng);
      if (isFinite(lat) && isFinite(lng) && Math.abs(lat) <= 90 && Math.abs(lng) <= 180) {
        return [lng, lat];
      }
    }
    const r = String((m && m.region) || '').trim();
    return (r && GEO_COORDS[r]) || null;
  };
  const linkCoord = (l, which) => {
    if (source === 'daily' && daily) {
      const mid = which === 'from' ? l.machine_id : l.dest_id;
      const m = (daily.machines || []).find(x => x.id === mid);
      if (m) return coord(m);
      if (which === 'to' && l.dest_id === 0) {
        // 模式 B 的面板端：按面板地区设置落点
        const region = (daily.settings || {}).panel_region || '';
        return coord({region});
      }
    }
    if (typeof MACHINES !== 'undefined' && Array.isArray(MACHINES)) {
      const mid = which === 'from' ? l.machine_id : l.dest_id;
      const m = MACHINES.find(x => x.id === mid);
      if (m) {
        const mc = coord(m);
        if (mc) return mc;
      }
    }
    const r = String((which === 'from' ? (l.machine_region || l.from?.region) :
                      (l.dest_region || l.to?.region)) || '').trim();
    return (r && GEO_COORDS[r]) || null;
  };
  const pct = (v) => v == null ? '—' : (+v).toFixed(1) + '%';
  const ms = (v) => v == null ? '—' : Math.round(v);
  const fmtAgo = (ts) => {
    if (!ts) return '—';
    const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
    if (s < 5) return '刚刚';
    if (s < 3600) return s + 's 前';
    if (s < 86400) return Math.round(s / 60) + ' 分钟前';
    return Math.round(s / 86400) + ' 天前';
  };
  const linkRtt = (l) => (l.avg != null ? l.avg : l.rtt);
  // 粗略估算文字宽度（CJK 按全角、其余按半角），用于标签避让
  const textW = (s, size) => {
    let w = 0;
    for (const ch of String(s)) w += /[\u2E80-\uFFFF]/.test(ch) ? size : size * 0.58;
    return w;
  };

  // Natural Earth I 投影（与底图同一公式），单位坐标 → 画布像素
  const PJ = (lon, lat) => {
    const rad = Math.PI / 180, phi = lat * rad, lam = lon * rad;
    const p2 = phi * phi, p4 = p2 * p2;
    const x = lam * (0.8707 - 0.131979 * p2 + p4 * (-0.013791 + p4 * (0.003971 * p2 - 0.001529 * p4)));
    const y = phi * (1.007226 + p2 * (0.015085 + p4 * (-0.044475 + 0.028874 * p2 - 0.005916 * p4)));
    return [x * S + TX, TY - y * S];
  };

  const el = (tag, attrs, parent) => {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
    (parent || $('geoSvg')).appendChild(e);
    return e;
  };

  // 反缩放锚点：内容按屏幕像素绘制，缩放地图时标记/文字大小不变
  const anchor = (x, y, parent, cls) => {
    const g = el('g', {class: 'geo-anchor' + (cls ? ' ' + cls : '')}, parent);
    g._x = x; g._y = y;
    g.setAttribute('transform', `translate(${x.toFixed(1)},${y.toFixed(1)}) scale(${(1 / vk).toFixed(4)})`);
    return g;
  };

  // 底图生成器把跨 ±180° 的环拆成了开放段，各自闭合会在俄罗斯远东拉出楔形色块；
  // 这里把首尾相接的段重新拼回一条环
  function stitchPolys(polys) {
    const closed = [], open = [];
    for (const p of polys) {
      const n = p.length;
      if (p[0] === p[n - 2] && p[1] === p[n - 1]) closed.push(p); else open.push(p.slice());
    }
    let merged = true;
    while (merged) {
      merged = false;
      outer: for (let i = 0; i < open.length; i++) {
        for (let j = 0; j < open.length; j++) {
          if (i === j) continue;
          const a = open[i], b = open[j];
          if (a[a.length - 2] === b[0] && a[a.length - 1] === b[1]) {
            open[i] = a.concat(b.slice(2));
            open.splice(j, 1);
            merged = true;
            break outer;
          }
        }
      }
    }
    return closed.concat(open);
  }

  function fitMap() {
    const box = $('geoMapBox');
    W = Math.max(320, box.clientWidth || 760);
    // Natural Earth I 世界高宽比 2.8454/5.4708：高度按宽度推算，保证完整收录
    // 北极圈到南极（此前钳 500px 会把俄罗斯 / 新西兰裁掉）
    S = (W - 32) / 5.4708;
    H = Math.round(2.8454 * S) + 36;
    TX = W / 2;
    TY = H / 2 + 4;
    const svg = $('geoSvg');
    svg.setAttribute('width', W);
    svg.setAttribute('height', H);
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  }

  function spherePath() {
    let d = '';
    for (let lat = -90; lat <= 90; lat += 2) {
      const p = PJ(180, lat);
      d += (d ? 'L' : 'M') + p[0].toFixed(1) + ',' + p[1].toFixed(1);
    }
    for (let lat = 90; lat >= -90; lat -= 2) {
      const p = PJ(-180, lat);
      d += 'L' + p[0].toFixed(1) + ',' + p[1].toFixed(1);
    }
    return d + 'Z';
  }

  function buildBase() {
    const svg = $('geoSvg');
    svg.innerHTML = '';
    const pitch = Math.max(3.4, W / 260);
    const defs = el('defs');
    defs.innerHTML =
      '<radialGradient id="geoOcean" cx="50%" cy="42%" r="62%">' +
        '<stop offset="0%" style="stop-color:var(--geo-ocean-1)"/>' +
        '<stop offset="100%" style="stop-color:var(--geo-ocean-2)"/></radialGradient>' +
      '<linearGradient id="geoBrand" x1="0" y1="0" x2="1" y2="1">' +
        '<stop offset="0%" stop-color="#9a9cf5"/><stop offset="100%" stop-color="#595CD3"/></linearGradient>' +
      `<pattern id="geoDotPat" width="${pitch}" height="${pitch}" patternUnits="userSpaceOnUse">` +
        `<circle cx="${pitch / 2}" cy="${pitch / 2}" r="${(pitch * 0.25).toFixed(2)}" class="geo-dotpat"/></pattern>` +
      '<filter id="geoGlow" x="-100%" y="-100%" width="300%" height="300%">' +
        '<feGaussianBlur stdDeviation="3" result="b"/>' +
        '<feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>';
    dotPat = defs.querySelector('#geoDotPat');
    dotPat._pitch = pitch;
    // 全幅透明底板：海洋/空白区域也能接收按下事件（拖拽的起点不限于陆地）
    el('rect', {x: 0, y: 0, width: W, height: H, fill: 'transparent'});
    gWorld = el('g');
    const sp = spherePath();
    el('path', {d: sp, class: 'geo-sphere'}, gWorld);
    gGrat = el('g', {class: 'geo-grat'}, gWorld);
    gLand = el('g', {class: 'geo-land'}, gWorld);
    gLinks = el('g', {class: 'geo-links'}, gWorld);
    gDots = el('g', {class: 'geo-dots'}, gWorld);
    gNodes = el('g', {class: 'geo-nodes'}, gWorld);
    gLabels = el('g', {class: 'geo-labels'}, gWorld);
    gHit = el('g', {}, gWorld);
    el('path', {d: sp, class: 'geo-sphere-edge'}, gGrat);
    let d = '';
    for (let lon = -150; lon <= 180; lon += 30) {
      d += linePath(Array.from({length: 17}, (_, i) => [lon, -80 + i * 10]));
    }
    for (let lat = -60; lat <= 60; lat += 30) {
      d += linePath(Array.from({length: 37}, (_, i) => [-180 + i * 10, lat]));
    }
    el('path', {d}, gGrat);
    const eq = linePath(Array.from({length: 37}, (_, i) => [-180 + i * 10, 0]));
    el('path', {d: eq, class: 'eq'}, gGrat);
    if (window.WORLD_POLYS) {
      let pd = '';
      for (const poly of window.WORLD_POLYS) {
        for (let i = 0; i < poly.length; i += 2) {
          pd += (i ? 'L' : 'M') + (poly[i] / 1000 * S + TX).toFixed(1) + ',' +
                (TY - poly[i + 1] / 1000 * S).toFixed(1);
        }
        pd += 'Z';
      }
      // 实色底 + 点阵纹理 + 海岸线三层，单 path 合并绘制，拖拽更流畅
      el('path', {d: pd, class: 'fill'}, gLand);
      el('path', {d: pd, class: 'dots', fill: 'url(#geoDotPat)'}, gLand);
      el('path', {d: pd, class: 'coast'}, gLand);
    }
    applyView();
  }

  // ---- 地图拖拽 / 缩放 ----
  function applyView() {
    if (!gWorld) return;
    gWorld.setAttribute('transform',
      `translate(${vx.toFixed(1)},${vy.toFixed(1)}) scale(${vk.toFixed(3)})`);
    const inv = (1 / vk).toFixed(4);
    gWorld.querySelectorAll('.geo-anchor').forEach(g => {
      g.setAttribute('transform', `translate(${g._x.toFixed(1)},${g._y.toFixed(1)}) scale(${inv})`);
    });
    // 点阵纹理随缩放反向调整，保持屏幕上的点距不变
    if (dotPat) {
      const p = dotPat._pitch / Math.min(vk, 4);
      dotPat.setAttribute('width', p.toFixed(3));
      dotPat.setAttribute('height', p.toFixed(3));
      const c = dotPat.firstChild;
      c.setAttribute('cx', (p / 2).toFixed(3));
      c.setAttribute('cy', (p / 2).toFixed(3));
      c.setAttribute('r', (p * 0.25).toFixed(3));
    }
    const box = $('geoMapBox');
    if (box) {
      box.classList.toggle('zoomed', vk > 1.001);
      box.classList.toggle('armed', wheelArmed);
    }
  }

  function setWheelArmed(on) {
    wheelArmed = !!on;
    const box = $('geoMapBox'), hint = $('geoScrollHint');
    if (box) box.classList.toggle('armed', wheelArmed);
    if (wheelArmed && hint) hint.classList.remove('show');
  }

  function flashScrollHint() {
    const hint = $('geoScrollHint');
    if (!hint) return;
    hint.classList.add('show');
    clearTimeout(hintTimer);
    hintTimer = setTimeout(() => hint.classList.remove('show'), 1300);
  }

  function clampView() {
    vk = Math.min(12, Math.max(1, vk));
    const mw = W * vk, mh = H * vk;
    vx = Math.min(0, Math.max(W - mw, vx));
    vy = Math.min(0, Math.max(H - mh, vy));
    if (vk === 1) { vx = 0; vy = 0; }
    applyView();
  }

  // 缩放后重新做标签避让（防抖）：放大后节点散开，能显示更多标签
  function scheduleRelayout() {
    clearTimeout(relayoutTimer);
    relayoutTimer = setTimeout(() => { if (inited && active) renderMap(buildLinks()); }, 160);
  }

  function zoomAt(cx, cy, factor) {
    const k2 = Math.min(12, Math.max(1, vk * factor));
    if (k2 === vk) return;
    vx = cx - (cx - vx) * (k2 / vk);
    vy = cy - (cy - vy) * (k2 / vk);
    vk = k2;
    if (vk <= 1.001) setWheelArmed(false);
    clampView();
    scheduleRelayout();
  }

  function resetView() {
    vk = 1; vx = 0; vy = 0;
    setWheelArmed(false);
    clampView();
    scheduleRelayout();
  }

  function bindMapGestures() {
    const svg = $('geoSvg');
    const box = $('geoMapBox');
    const pointers = new Map();
    let panLast = null, pinchDist = 0, dragged = false;
    svg.addEventListener('wheel', e => {
      // 按住 Ctrl / ⌘ + 滚轮：直接激活并缩放地图
      if (e.ctrlKey || e.metaKey) setWheelArmed(true);
      // 未激活时：不拦截滚轮，让页面无论是上滑还是下滑都能顺畅滚过地图
      if (!wheelArmed) {
        flashScrollHint();
        return;
      }
      // 已缩到最小状态（vk === 1）且继续向下滚（缩小方向）：自动解除激活并放行给页面滚动
      if (e.deltaY > 0 && vk <= 1.001) {
        setWheelArmed(false);
        return;
      }
      e.preventDefault();
      const rect = svg.getBoundingClientRect();
      zoomAt(e.clientX - rect.left, e.clientY - rect.top, e.deltaY < 0 ? 1.2 : 1 / 1.2);
    }, {passive: false});
    svg.addEventListener('pointerdown', e => {
      if (e.button !== 0 && e.pointerType === 'mouse') return;
      e.preventDefault();   // 阻止原生文字选取 / 原生拖拽
      setWheelArmed(true);  // 点击或拖拽地图即激活滚轮缩放
      try { svg.setPointerCapture(e.pointerId); } catch (e2) {}
      pointers.set(e.pointerId, {x: e.clientX, y: e.clientY});
      if (pointers.size === 1) { panLast = {x: e.clientX, y: e.clientY}; dragged = false; hideTip(); }
    });
    if (box) {
      box.addEventListener('pointerleave', () => { setWheelArmed(false); hideTip(); });
    }
    document.addEventListener('pointerdown', e => {
      if (wheelArmed && box && !box.contains(e.target)) setWheelArmed(false);
    });
    svg.addEventListener('pointermove', e => {
      if (pointers.size && dragged) e.preventDefault();
      if (!pointers.has(e.pointerId)) return;
      pointers.set(e.pointerId, {x: e.clientX, y: e.clientY});
      if (pointers.size === 2) {
        const [p1, p2] = [...pointers.values()];
        const d = Math.hypot(p1.x - p2.x, p1.y - p2.y);
        if (pinchDist > 0) {
          const rect = svg.getBoundingClientRect();
          zoomAt((p1.x + p2.x) / 2 - rect.left, (p1.y + p2.y) / 2 - rect.top, d / pinchDist);
        }
        pinchDist = d;
        dragged = true;
        return;
      }
      if (!panLast) return;
      const dx = e.clientX - panLast.x, dy = e.clientY - panLast.y;
      if (Math.abs(dx) + Math.abs(dy) > 4) dragged = true;
      panLast = {x: e.clientX, y: e.clientY};
      vx += dx; vy += dy;
      clampView();
      if (dragged) svg.classList.add('panning');
    });
    const endPtr = e => {
      pointers.delete(e.pointerId);
      if (pointers.size < 2) pinchDist = 0;
      if (!pointers.size) { panLast = null; svg.classList.remove('panning'); }
    };
    svg.addEventListener('pointerup', endPtr);
    svg.addEventListener('pointercancel', endPtr);
    // 拖拽/捏合结束后的 click 不触发选中
    svg.addEventListener('click', e => {
      if (dragged) { e.stopPropagation(); e.preventDefault(); dragged = false; }
    }, true);
    $('geoResetBtn').addEventListener('click', resetView);
    const zoomBtn = (id, f) => {
      const b = $(id);
      if (b) b.addEventListener('click', () => { setWheelArmed(true); zoomAt(W / 2, H / 2, f); });
    };
    zoomBtn('geoZoomIn', 1.5);
    zoomBtn('geoZoomOut', 1 / 1.5);
  }

  function linePath(points) {
    let d = '';
    let prev = null;
    for (const [lon, lat] of points) {
      const p = PJ(lon, lat);
      if (!prev || Math.abs(p[0] - prev[0]) > W / 2) d += 'M' + p[0].toFixed(1) + ',' + p[1].toFixed(1);
      else d += 'L' + p[0].toFixed(1) + ',' + p[1].toFixed(1);
      prev = p;
    }
    return d;
  }

  // 两点间的「传输线」：屏幕空间二次贝塞尔，弓向背离地图中心；返回路径、控制点与中点
  function arcPath(a, b, k) {
    const dx = b[0] - a[0], dy = b[1] - a[1];
    const dist = Math.hypot(dx, dy) || 1;
    const mx = (a[0] + b[0]) / 2, my = (a[1] + b[1]) / 2;
    let nx = dy / dist, ny = -dx / dist;
    if (nx * (mx - W / 2) + ny * (my - H / 2) < 0) { nx = -nx; ny = -ny; }
    const cx = mx + nx * dist * k, cy = my + ny * dist * k;
    const at = (t) => [(1 - t) * (1 - t) * a[0] + 2 * (1 - t) * t * cx + t * t * b[0],
                       (1 - t) * (1 - t) * a[1] + 2 * (1 - t) * t * cy + t * t * b[1]];
    return {
      d: `M${a[0].toFixed(1)},${a[1].toFixed(1)}Q${cx.toFixed(1)},${cy.toFixed(1)} ${b[0].toFixed(1)},${b[1].toFixed(1)}`,
      mid: at(0.5), at, len: dist,
    };
  }

  // 把接口数据整理成统一链路列表
  function buildLinks() {
    const list = [];
    if (source === 'test') {
      const run = test && test.run;
      if (!run) return list;
      for (const l of run.links || []) {
        list.push({
          key: 't' + l.machine_id, task_id: null, task_name: '',
          run_id: run.run_id,
          from: {
            name: l.machine_name || ('#' + l.machine_id),
            region: l.region || '', ip: '', isp: '',
            c: linkCoord({machine_id: l.machine_id}, 'from') ||
               (GEO_COORDS[l.region] || null),
          },
          to: {
            name: run.target_name, region: run.target_region, ip: '',
            c: linkCoord({dest_id: run.target_id, dest_region: run.target_region}, 'to') ||
               (GEO_COORDS[run.target_region] || null),
            target: true,
          },
          rtt: l.rtt, avg: l.rtt, jit: l.jit, loss: l.loss,
          up: l.up, down: l.down, status: l.status === 'done' ? 'ok' : 'down',
          spark: (run.trend || {})[l.machine_id] || [], when: run.created_at,
        });
      }
      return list;
    }
    if (!daily) return list;
    for (const l of daily.links || []) {
      list.push({
        key: 'd' + l.task_id + '_' + l.machine_id + '_' + l.dest_id,
        task_id: l.task_id, task_name: l.task_name,
        machine_id: l.machine_id, dest_id: l.dest_id,
        from: {
          name: l.machine_name, region: l.machine_region,
          ip: l.machine_ip, isp: l.machine_isp, c: linkCoord(l, 'from'),
        },
        to: {
          name: l.dest_name || '面板端', region: l.dest_region,
          ip: l.dest_ip, c: linkCoord(l, 'to'), target: true,
        },
        rtt: l.rtt, avg: l.avg, avg1m: l.avg1m, min: l.min, max: l.max,
        jit: l.jit, loss: l.loss, lossAvg: l.loss_avg, n: l.n, lastTs: l.last_ts,
        status: l.status, spark: l.spark || [], when: null,
      });
    }
    return list;
  }

  const dailyMachines = () => (daily && daily.machines) || [];

  function render() {
    if (!inited) return;
    const links = buildLinks();
    renderMap(links);
    renderTable(links);
    renderDetail(links);
    renderHud(links);
    $('geoUpdated').innerHTML = `<i></i>更新于 ${new Date().toLocaleTimeString()}`;
  }

  // 地图左上角的浮动概览
  function renderHud(links) {
    const hud = $('geoHud');
    if (!hud) return;
    const good = links.filter(x => linkRtt(x) != null);
    const avg = good.length ? good.reduce((s, x) => s + linkRtt(x), 0) / good.length : null;
    const bad = links.filter(x => x.status && x.status !== 'ok').length;
    const best = good.length ? Math.min(...good.map(linkRtt)) : null;
    hud.classList.toggle('empty', !links.length);
    hud.innerHTML = links.length ? `
      <div class="hud-k"><b>${good.length}<small>/${links.length}</small></b><span>在线链路</span></div>
      <div class="hud-k"><b style="color:${avg == null ? 'var(--muted)' : rating(avg).color}">${avg == null ? '—' : Math.round(avg)}<small>ms</small></b><span>平均 RTT</span></div>
      <div class="hud-k"><b style="color:${best == null ? 'var(--muted)' : rating(best).color}">${best == null ? '—' : Math.round(best)}<small>ms</small></b><span>最佳</span></div>
      <div class="hud-k"><b class="${bad ? 'bad' : 'okc'}">${bad}</b><span>异常</span></div>`
      : `<div class="hud-empty">${source === 'test' ? '还没有已完成的测试' : '当前视图暂无监控链路'}</div>`;
  }

  // 选择不与已占用区域重叠的位置（贪心）；全部冲突时返回重叠最少者
  function pickSpot(cands, boxes) {
    let best = null, bestHit = Infinity;
    for (const c of cands) {
      let hit = 0;
      for (const b of boxes) {
        const ox = Math.min(c.x2, b.x2) - Math.max(c.x1, b.x1);
        const oy = Math.min(c.y2, b.y2) - Math.max(c.y1, b.y1);
        if (ox > 0 && oy > 0) hit += ox * oy;
      }
      if (hit === 0) return {c, hit};
      if (hit < bestHit) { best = c; bestHit = hit; }
    }
    return {c: best, hit: bestHit};
  }

  function renderMap(links) {
    gLinks.innerHTML = '';
    gDots.innerHTML = '';
    gNodes.innerHTML = '';
    gLabels.innerHTML = '';
    gHit.innerHTML = '';
    linkEls.clear();
    dotEls.clear();
    const nodes = new Map();
    const nodeOf = (n) => {
      if (!n.c) return null;
      const key = n.name + '|' + n.region;
      if (!nodes.has(key)) nodes.set(key, {...n, p: PJ(n.c[0], n.c[1]), links: []});
      return nodes.get(key);
    };
    const drawn = [];
    let i = 0;
    for (const l of links) {
      const a = nodeOf(l.from), b = nodeOf(l.to);
      if (!a || !b) continue;
      a.links.push(l); b.links.push(l);
      const st = rating(linkRtt(l));
      const arc = arcPath(a.p, b.p, 0.14 + (i++ % 4) * 0.035);
      const isSel = selected === l.key, isHov = hoverKey === l.key;
      const dim = selected && !isSel;
      const broken = st.k === 'off' || l.status === 'down';
      const g = el('g', {class: 'geo-linkg' + (dim ? ' dim' : '') + (isSel || isHov ? ' on' : '') +
                                   (broken ? ' broken' : ''), style: `--c:${st.color}`}, gLinks);
      el('path', {d: arc.d, class: 'geo-link-glow'}, g);
      const path = el('path', {d: arc.d, class: 'geo-link'}, g);
      linkEls.set(l.key, g);
      drawn.push({l, arc, st, isSel, isHov, dim});
      const hit = el('path', {d: arc.d, class: 'geo-link-hit', 'data-key': l.key}, gHit);
      hit.addEventListener('mousemove', e => showTip(l, e));
      hit.addEventListener('mouseenter', () => { hoverKey = l.key; paintLinks(true); });
      hit.addEventListener('mouseleave', () => { hideTip(); hoverKey = null; paintLinks(true); });
      hit.addEventListener('click', () => select(l.key));
      if (!reducing && !broken) {
        const dg = anchor(arc.at(0)[0], arc.at(0)[1], gDots, 'geo-comet');
        dg.style.setProperty('--c', st.color);
        el('circle', {r: 7, class: 'halo'}, dg);
        el('circle', {r: 2.6, class: 'core'}, dg);
        dotEls.set(l.key, {g: dg, path, phase: Math.random(), rtt: l.rtt});
      }
    }

    // —— 节点 ——
    const boxes = [];      // 已占用的屏幕区域（视图像素，相对 gWorld 原点 × vk）
    const sp = (p) => [p[0] * vk, p[1] * vk];
    const nodeList = [...nodes.values()];
    for (const n of nodeList) {
      const q = sp(n.p);
      const r = n.target ? 10 : 6;
      boxes.push({x1: q[0] - r, y1: q[1] - r, x2: q[0] + r, y2: q[1] + r});
    }
    for (const n of nodeList) {
      const g = anchor(n.p[0], n.p[1], gNodes, 'geo-node' + (n.target ? ' is-target' : ''));
      const touching = n.links;
      const worst = touching.reduce((w, l) => (l.status && l.status !== 'ok') ? l : w, null);
      const st = rating(touching.length ? linkRtt(touching[0]) : null);
      const isOn = touching.some(l => l.key === selected || l.key === hoverKey);
      if (n.target) {
        el('circle', {r: 15, class: 'tgt-pulse'}, g);
        el('circle', {r: 11, class: 'tgt-ring'}, g);
        el('rect', {x: -5.5, y: -5.5, width: 11, height: 11, rx: 2.5, transform: 'rotate(45)',
                    fill: 'url(#geoBrand)', class: 'tgt-core', filter: 'url(#geoGlow)'}, g);
        el('circle', {r: 1.8, class: 'tgt-dot'}, g);
      } else {
        const sc = worst && (worst.status === 'down' || worst.status === 'warn')
          ? (worst.status === 'down' ? 'var(--red)' : 'var(--orange)') : st.color;
        g.style.setProperty('--c', sc);
        if (!worst || worst.status !== 'stale') el('circle', {r: 5, class: 'pulse'}, g);
        el('circle', {r: 4.6, class: 'mk'}, g);
      }
      if (isOn) el('circle', {r: n.target ? 18 : 10, class: 'sel-ring'}, g);
      g.addEventListener('mousemove', e => {
        const l = touching.find(x => x.key === selected) || touching[0];
        if (l) showTip(l, e);
      });
      g.addEventListener('mouseleave', hideTip);
      g.addEventListener('click', () => {
        if (!touching.length) return;
        // 目标节点关联多条链路：重复点击依次切换
        const idx = touching.findIndex(l => l.key === selected);
        select(touching[(idx + 1) % touching.length].key, true);
      });
    }

    // —— 节点标签（目标优先，其后选中/悬停相关，再按链路顺序），避让已占区域 ——
    const order = nodeList.slice().sort((a, b) => {
      const pa = a.target ? 0 : (a.links.some(l => l.key === selected || l.key === hoverKey) ? 1 : 2);
      const pb = b.target ? 0 : (b.links.some(l => l.key === selected || l.key === hoverKey) ? 1 : 2);
      return pa - pb;
    });
    for (const n of order) {
      const q = sp(n.p);
      const on = n.links.some(l => l.key === selected || l.key === hoverKey);
      const fs = n.target ? 12 : 11;
      const tw = textW(n.name, fs) + 6, th = fs + 4;
      const off = n.target ? 15 : 9;
      const cands = [
        {dx: off, dy: 0, anchor: 'start'}, {dx: -off, dy: 0, anchor: 'end'},
        {dx: 0, dy: -off - 4, anchor: 'middle'}, {dx: 0, dy: off + 6, anchor: 'middle'},
        {dx: off - 2, dy: -off, anchor: 'start'}, {dx: -off + 2, dy: -off, anchor: 'end'},
        {dx: off - 2, dy: off + 2, anchor: 'start'}, {dx: -off + 2, dy: off + 2, anchor: 'end'},
      ].map(c => {
        const x1 = c.anchor === 'start' ? q[0] + c.dx : c.anchor === 'end' ? q[0] + c.dx - tw : q[0] - tw / 2;
        const yc = q[1] + c.dy;
        return {...c, x1, x2: x1 + tw, y1: yc - th / 2, y2: yc + th / 2};
      });
      const {c, hit} = pickSpot(cands, boxes);
      if (hit > 0 && !(n.target || on)) continue;   // 放不下就不显示，悬停/选中时再出现
      boxes.push(c);
      const t = el('text', {
        class: 'geo-label' + (n.target ? ' target' : (on ? ' on' : '')),
        x: c.dx, y: c.dy, 'dominant-baseline': 'central', 'text-anchor': c.anchor,
      }, anchor(n.p[0], n.p[1], gLabels));
      t.textContent = n.name;
    }

    // —— 连线 RTT 胶囊：在弧线上找不冲突的位置 ——
    const pills = drawn.filter(d => linkRtt(d.l) != null)
      .sort((a, b) => (b.isSel || b.isHov) - (a.isSel || a.isHov));
    for (const d of pills) {
      const txt = Math.round(linkRtt(d.l)) + 'ms';
      const on = d.isSel || d.isHov;
      const pw = textW(txt, 10.5) + 12, ph = 17;
      if (d.arc.len * vk < pw + 26 && !on) continue;   // 链路太短，放不下
      const cands = [0.5, 0.4, 0.6, 0.32, 0.68, 0.25, 0.75].map(t => {
        const p = d.arc.at(t), q = sp(p);
        return {p, x1: q[0] - pw / 2, x2: q[0] + pw / 2, y1: q[1] - ph / 2, y2: q[1] + ph / 2};
      });
      const {c, hit} = pickSpot(cands, boxes);
      if (hit > 0 && !on) continue;
      boxes.push(c);
      const g = anchor(c.p[0], c.p[1], gLabels, 'geo-pill' + (on ? ' on' : '') + (d.dim ? ' dim' : ''));
      g.style.setProperty('--c', d.st.color);
      el('rect', {x: -pw / 2, y: -ph / 2, width: pw, height: ph, rx: ph / 2}, g);
      const t = el('text', {x: 0, y: 0.5, 'text-anchor': 'middle', 'dominant-baseline': 'central'}, g);
      t.textContent = txt;
    }
  }

  function renderTable(links) {
    const sorted = [...links].sort((a, b) => (linkRtt(a) ?? 1e9) - (linkRtt(b) ?? 1e9));
    $('geoTbody').innerHTML = sorted.map(l => {
      const st = rating(linkRtt(l));
      const sel = selected === l.key ? ' class="sel"' : '';
      const sub = [l.from.region, l.from.isp, l.from.bw].filter(Boolean).join(' · ');
      const taskTxt = source === 'daily' && !taskFilter && l.task_name ? `<em>${esc(l.task_name)}</em>` : '';
      const noCoord = !l.from.c ? ' <span class="geo-status stale">未上图</span>' : '';
      const rttMain = linkRtt(l) != null ? Math.round(linkRtt(l)) : null;
      const rttLast = l.rtt != null ? Math.round(l.rtt) : null;
      const bar = rttMain == null ? 0 : Math.max(4, Math.min(100, rttMain / RTT_SCALE * 100));
      const lossHi = l.loss != null && +l.loss > 0;
      return `<tr data-key="${esc(l.key)}"${sel} style="--c:${st.color}">
        <td><span class="geo-dot-st ${esc(l.status || 'ok')}"></span><span class="rb rating-${st.k}">${st.k === 'off' ? '–' : st.k.toUpperCase()}</span>${statusBadge(l.status)}</td>
        <td><div class="geo-route"><b>${esc(l.from.name)}</b>${noCoord}<span class="arrow">→</span><span class="to">${esc(l.to.name)}</span></div><span class="geo-sub">${esc(sub)}${taskTxt}</span></td>
        <td class="num geo-rttcell"><div class="v">${rttMain == null ? `<span class="muted">${source === 'test' ? '失败' : '—'}</span>` : rttMain + '<small> ms</small>'}${rttLast != null && l.avg != null && rttLast !== rttMain ? `<small class="last">最近 ${rttLast}</small>` : ''}</div><div class="geo-bar-track"><i style="width:${bar}%"></i></div></td>
        <td class="num">${l.jit == null ? '—' : (+l.jit).toFixed(1) + '<small> ms</small>'}</td>
        <td class="num${lossHi ? ' geo-loss-hi' : ''}">${pct(l.loss)}</td>
        <td>${sparkSvg(l.spark, 84, 24, false, st.color)}</td>
      </tr>`;
    }).join('') || `<tr class="geo-empty"><td colspan="6">${source === 'test' ? '还没有已完成的测试结果' : '暂无链路数据，创建监控任务后这里会出现链路'}</td></tr>`;
    $('geoTbody').querySelectorAll('tr[data-key]').forEach(tr => {
      tr.addEventListener('click', () => select(tr.dataset.key));
      tr.addEventListener('mouseenter', () => { hoverKey = tr.dataset.key; paintLinks(true); });
      tr.addEventListener('mouseleave', () => { hoverKey = null; paintLinks(true); });
    });
  }

  // 只更新高亮态；hoverOnly 时不重建详情卡（避免悬停时反复请求历史数据）
  function paintLinks(hoverOnly) {
    for (const [key, g] of linkEls) {
      const isSel = selected === key, isHov = hoverKey === key;
      g.classList.toggle('dim', !!selected && !isSel && !isHov);
      g.classList.toggle('on', isSel || isHov);
    }
    // 行选中态就地更新：整表重建会让鼠标下的行消失，悬停态就再也出不来
    $('geoTbody').querySelectorAll('tr').forEach(tr => {
      tr.classList.toggle('sel', tr.dataset.key === selected);
      tr.classList.toggle('hov', tr.dataset.key === hoverKey);
    });
    if (!hoverOnly) {
      const links = buildLinks();
      renderMap(links);
      renderDetail(links);
    }
  }

  // 过采样点的平滑三次贝塞尔曲线（单调斜率限制：平滑且绝不越过极值点）
  function smoothCurve(pts) {
    const n = pts.length;
    if (!n) return '';
    const f = v => v.toFixed(1);
    if (n === 1) return `M${f(pts[0][0])},${f(pts[0][1])}`;
    if (n === 2) return `M${f(pts[0][0])},${f(pts[0][1])}L${f(pts[1][0])},${f(pts[1][1])}`;
    const dx = [], dy = [], m = [];
    for (let i = 0; i < n - 1; i++) {
      dx[i] = pts[i + 1][0] - pts[i][0] || 1e-6;
      dy[i] = pts[i + 1][1] - pts[i][1];
      m[i] = dy[i] / dx[i];
    }
    const tg = [m[0]];
    for (let i = 1; i < n - 1; i++) {
      tg[i] = m[i - 1] * m[i] <= 0 ? 0 : (m[i - 1] + m[i]) / 2;
    }
    tg[n - 1] = m[n - 2];
    for (let i = 0; i < n - 1; i++) {
      if (m[i] === 0) { tg[i] = 0; tg[i + 1] = 0; continue; }
      const s = Math.hypot(tg[i] / m[i], tg[i + 1] / m[i]);
      if (s > 3) { const k = 3 / s; tg[i] *= k; tg[i + 1] *= k; }
    }
    let d = `M${f(pts[0][0])},${f(pts[0][1])}`;
    for (let i = 0; i < n - 1; i++) {
      const h = dx[i] / 3;
      d += `C${f(pts[i][0] + h)},${f(pts[i][1] + tg[i] * h)} ` +
           `${f(pts[i + 1][0] - h)},${f(pts[i + 1][1] - tg[i + 1] * h)} ` +
           `${f(pts[i + 1][0])},${f(pts[i + 1][1])}`;
    }
    return d;
  }

  function sparkSvg(series, w, h, stretch, color) {
    const vals = (series || []).slice(-30);
    const good = vals.filter(v => v != null);
    if (!good.length) return '<span class="geo-spark-empty"></span>';
    const c = color || 'var(--accent)';
    let mn = Math.min(...good), mx = Math.max(...good);
    if (mx - mn < 8) { const m = (mx + mn) / 2; mn = m - 4; mx = m + 4; }
    const pad = 3;
    const X = i => pad + i * (w - 2 * pad) / Math.max(1, vals.length - 1);
    const Y = v => pad + (1 - (v - mn) / (mx - mn)) * (h - 2 * pad);
    const segs = [];
    let cur = [];
    vals.forEach((v, i) => {
      if (v == null) { if (cur.length > 1) segs.push(cur); cur = []; }
      else cur.push([X(i), Y(v)]);
    });
    if (cur.length > 1) segs.push(cur);
    const sizeAttr = stretch
      ? `width="100%" height="${h}" preserveAspectRatio="none"`
      : `width="${w}" height="${h}"`;
    const lastSeg = segs[segs.length - 1];
    const last = lastSeg ? lastSeg[lastSeg.length - 1] : null;
    return `<svg class="geo-spark" viewBox="0 0 ${w} ${h}" ${sizeAttr} aria-hidden="true">` +
      segs.map(s => `<path fill="${c}" fill-opacity=".14" stroke="none" d="${smoothCurve(s)}L${s[s.length - 1][0].toFixed(1)},${h}L${s[0][0].toFixed(1)},${h}Z"/>`).join('') +
      segs.map(s => `<path fill="none" stroke="${c}" stroke-width="1.6" stroke-linecap="round" vector-effect="non-scaling-stroke" d="${smoothCurve(s)}"/>`).join('') +
      (last && !stretch ? `<circle cx="${last[0].toFixed(1)}" cy="${last[1].toFixed(1)}" r="2" fill="${c}"/>` : '') +
      `</svg>`;
  }

  function kpi(label, val, unit, color) {
    return `<div class="kpi"><span>${label}</span><b${color ? ` style="color:${color}"` : ''}>${val}${unit && val !== '—' ? `<small>${unit}</small>` : ''}</b></div>`;
  }

  function renderDetail(links) {
    const box = $('geoDetail');
    const l = links.find(x => x.key === selected);
    if (!l) {
      const good = links.filter(x => linkRtt(x) != null);
      const avg = good.length ? good.reduce((s, x) => s + linkRtt(x), 0) / good.length : null;
      const worst = [...links].sort((a, b) => (linkRtt(b) ?? -1) - (linkRtt(a) ?? -1))[0];
      const avgLoss = links.length ? links.reduce((s, x) => s + (x.loss ?? 0), 0) / links.length : null;
      const dist = {s: 0, a: 0, b: 0, c: 0, off: 0};
      links.forEach(x => { dist[rating(linkRtt(x)).k]++; });
      const tot = links.length || 1;
      const distBar = ['s', 'a', 'b', 'c', 'off'].map(k => dist[k]
        ? `<i class="d-${k}" style="flex:${dist[k]}" title="${dist[k]}"></i>` : '').join('');
      box.innerHTML = `<div class="geo-dhead"><h3>${source === 'test' ? '本轮概览' : '链路汇总'}</h3>
          <span class="muted">${links.length} 条链路</span></div>
        <div class="sum-grid">
          <div class="st"><span>${source === 'test' ? '有效链路' : '有数据链路'}</span><b>${good.length}<small> / ${links.length}</small></b></div>
          <div class="st"><span>平均 RTT</span><b style="color:${avg == null ? 'var(--muted)' : rating(avg).color}">${avg == null ? '—' : Math.round(avg)}<small> ms</small></b></div>
          <div class="st"><span>最差链路</span><b class="sm">${worst && linkRtt(worst) != null ? `${esc(worst.from.name)} <small style="color:${rating(linkRtt(worst)).color}">${Math.round(linkRtt(worst))}ms</small>` : '—'}</b></div>
          <div class="st"><span>丢包均值</span><b>${avgLoss == null ? '—' : avgLoss.toFixed(1)}<small>${avgLoss == null ? '' : ' %'}</small></b></div>
        </div>
        ${links.length ? `<div class="geo-dist"><div class="cap"><span>质量分布</span><span>${Math.round((dist.s + dist.a) / tot * 100)}% 优良</span></div>
          <div class="bar">${distBar}</div>
          <div class="keys"><span><i class="d-s"></i>优 ${dist.s}</span><span><i class="d-a"></i>良 ${dist.a}</span><span><i class="d-b"></i>中 ${dist.b}</span><span><i class="d-c"></i>差 ${dist.c}</span><span><i class="d-off"></i>无数据 ${dist.off}</span></div></div>` : ''}
        <p class="hint">点击地图上的节点、连线或列表行，查看单条链路详情。</p>`;
      return;
    }
    const st = rating(linkRtt(l));
    const spark = l.spark || [];
    const good = spark.filter(v => v != null);
    const mn = good.length ? Math.min(...good) : null;
    const mx = good.length ? Math.max(...good) : null;
    const head = (extra) => `<div class="geo-dhead"><h3>链路详情</h3>
        <button type="button" class="geo-x" data-geo-close aria-label="关闭详情">✕</button></div>
      <div class="geo-path" style="--c:${st.color}">
        <div class="end"><i class="pt"></i><b>${esc(l.from.name)}</b><span>${esc(l.from.region) || '—'}</span></div>
        <div class="wire"><span class="rb rating-${st.k}">${st.k === 'off' ? '无数据' : st.k.toUpperCase() + ' ' + st.label}</span>${extra || ''}</div>
        <div class="end r"><i class="pt tgt"></i><b>${esc(l.to.name)}</b><span>${esc(l.to.region) || (l.to.ip ? esc(l.to.ip) : '—')}</span></div>
      </div>`;
    const bindClose = () => {
      const x = box.querySelector('[data-geo-close]');
      if (x) x.addEventListener('click', () => select(l.key));
    };
    if (source === 'test') {
      box.innerHTML = `${head()}
        <div class="big"><b style="color:${st.color}">${l.rtt == null ? '—' : Math.round(l.rtt)}</b><small>ms · RTT avg</small></div>
        <div class="kpis">
          ${kpi('抖动', l.jit == null ? '—' : (+l.jit).toFixed(1), 'ms')}
          ${kpi('丢包', l.loss == null ? '—' : (+l.loss).toFixed(1), '%', l.loss > 0 ? 'var(--orange)' : '')}
          ${kpi('上行', l.up == null ? '—' : Math.round(l.up), 'M')}
          ${kpi('下行', l.down == null ? '—' : Math.round(l.down), 'M')}
        </div>
        <div class="spark-wrap"><div class="cap"><span>近 10 轮测试 RTT</span>
          <span>${mn != null ? Math.round(mn) + '–' + Math.round(mx) + 'ms' : ''}</span></div>
          ${sparkSvg(l.spark, 560, 70, true, st.color)}</div>
        <div class="foot">${l.run_id ? `测试记录 <b>#${l.run_id}</b> · ` : ''}测试时间 ${esc(l.when || '—')}</div>`;
      bindClose();
      return;
    }
    // 日常链路：多窗口指标 + 可切换时间范围的历史趋势
    box.innerHTML = `${head(statusBadge(l.status))}
      <div class="big"><b style="color:${st.color}">${l.rtt == null ? '—' : Math.round(l.rtt)}</b>
        <small>ms · 最近一次探测</small><span class="ago">${fmtAgo(l.lastTs)}</span></div>
      <div class="kpis">
        ${kpi('窗口均值', l.avg == null ? '—' : Math.round(l.avg), 'ms')}
        ${kpi('1 分钟均值', l.avg1m == null ? '—' : Math.round(l.avg1m), 'ms')}
        ${kpi('最低', l.min == null ? '—' : Math.round(l.min), 'ms')}
        ${kpi('最高', l.max == null ? '—' : Math.round(l.max), 'ms')}
        ${kpi('抖动', l.jit == null ? '—' : (+l.jit).toFixed(1), 'ms')}
        ${kpi('丢包', l.loss == null ? '—' : (+l.loss).toFixed(1), '%', l.loss > 0 ? 'var(--orange)' : '')}
        ${kpi('样本', l.n ?? '—', ' 轮')}
        ${kpi('运营商', esc(l.from.isp) || '—', '')}
      </div>
      <div class="fields">
        <div><span>测试地址</span>${esc(l.from.ip) || '—'}</div>
        <div><span>目标</span>${esc(l.to.name)}${l.to.ip ? ' · ' + esc(l.to.ip) : ''}</div>
      </div>
      <div class="spark-wrap"><div class="cap"><span>近期逐轮 RTT</span>
        <span>${mn != null ? Math.round(mn) + '–' + Math.round(mx) + 'ms' : ''}</span></div>
        ${sparkSvg(l.spark, 560, 56, true, st.color)}</div>
      <div class="spark-wrap">
        <div class="cap">
          <span>历史趋势</span>
          <span class="geo-range-seg">
            <button type="button" data-range="1h" class="${historyRange === '1h' ? 'on' : ''}">1小时</button>
            <button type="button" data-range="4h" class="${historyRange === '4h' ? 'on' : ''}">4小时</button>
            <button type="button" data-range="1d" class="${historyRange === '1d' ? 'on' : ''}">1天</button>
            <button type="button" data-range="7d" class="${historyRange === '7d' ? 'on' : ''}">7天</button>
          </span>
        </div>
        <div id="geoHistory" class="geo-history"><span class="geo-skel"></span></div>
        <div class="cap foot-cap"><span id="geoHistoryCap"></span></div>
      </div>
      <div class="foot">所属任务 <b>${esc(l.task_name || '—')}</b></div>`;
    box.querySelectorAll('.geo-range-seg button').forEach(b => {
      b.addEventListener('click', () => {
        historyRange = b.dataset.range;
        renderDetail(buildLinks());
      });
    });
    bindClose();
    const hKey = `${l.task_id}|${l.machine_id}|${l.dest_id}|${viewMode}|${historyRange}`;
    if (historyCache.key === hKey && historyCache.data) drawHistory(historyCache.data);
    loadHistory(l, hKey);
  }

  function drawHistory(h) {
    const holder = $('geoHistory');
    if (!holder) return;
    const pts = (h.points || []).filter(p => p.rtt != null);
    if (!pts.length) {
      holder.innerHTML = '<span class="geo-hist-empty">该时间范围内暂无数据（样本保留 7 天）</span>';
      return;
    }
    const vals = pts.map(p => p.rtt);
    let mn = Math.min(...vals), mx = Math.max(...vals);
    if (mx - mn < 8) { const m = (mx + mn) / 2; mn = m - 4; mx = m + 4; }
    const w = 720, hgt = 110, pad = 6;
    const X = i => pad + i * (w - 2 * pad) / Math.max(1, pts.length - 1);
    const Y = v => pad + (1 - (v - mn) / (mx - mn)) * (hgt - 2 * pad);
    const coords = pts.map((p, i) => [X(i), Y(p.rtt)]);
    const line = smoothCurve(coords);
    const avg = vals.reduce((s, v) => s + v, 0) / vals.length;
    const c = rating(avg).color;
    const cap = $('geoHistoryCap');
    if (cap) cap.textContent = `${Math.round(Math.min(...vals))}–${Math.round(Math.max(...vals))}ms · 均值 ${Math.round(avg)}ms · ${pts.length} 点`;
    const grid = [0.25, 0.5, 0.75].map(f => `<line x1="0" x2="${w}" y1="${(hgt * f).toFixed(1)}" y2="${(hgt * f).toFixed(1)}" class="grid"/>`).join('');
    holder.innerHTML = `<svg viewBox="0 0 ${w} ${hgt}" width="100%" height="${hgt}" preserveAspectRatio="none" aria-hidden="true">
      ${grid}
      <path d="${line}L${X(pts.length - 1).toFixed(1)},${hgt}L${X(0).toFixed(1)},${hgt}Z"
            fill="${c}" fill-opacity=".14" stroke="none"/>
      <line x1="0" x2="${w}" y1="${Y(avg).toFixed(1)}" y2="${Y(avg).toFixed(1)}" class="avg" stroke="${c}"/>
      <path d="${line}" fill="none" stroke="${c}" stroke-width="1.8" stroke-linecap="round" vector-effect="non-scaling-stroke"/>
    </svg><span class="ax ax-top">${Math.round(mx)}</span><span class="ax ax-bot">${Math.round(mn)}</span>`;
  }

  async function loadHistory(l, hKey) {
    if (source !== 'daily' || !l.task_id || !$('geoHistory')) return;
    try {
      const h = await api(`/api/watch/tasks/${l.task_id}/history?machine_id=${l.machine_id}` +
        `&dest_id=${l.dest_id}&mode=${encodeURIComponent(viewMode)}&range=${historyRange}`);
      historyCache = {key: hKey, data: h};
      if (selected === l.key) drawHistory(h);
    } catch (e) {
      if (historyCache.key === hKey) return;
      const holder = $('geoHistory');
      if (holder) holder.innerHTML = `<span class="geo-hist-empty">趋势加载失败</span>`;
    }
  }

  function showTip(l, e) {
    const tip = $('geoTip');
    const box = $('geoMapBox').getBoundingClientRect();
    const st = rating(linkRtt(l));
    tip.style.setProperty('--c', st.color);
    tip.innerHTML = `<div class="tip-h"><b>${esc(l.from.name)}</b><span class="rb rating-${st.k}">${st.k === 'off' ? '–' : st.k.toUpperCase()}</span></div>
      <span class="sub">${esc(l.from.region || '')} → ${esc(l.to.name)}${l.task_name ? ' · ' + esc(l.task_name) : ''}</span>
      <div class="tip-big"><b>${l.rtt == null ? '—' : Math.round(l.rtt)}</b><small>${l.rtt == null ? '无数据' : 'ms RTT'}</small>${statusBadge(l.status)}</div>
      <div class="r"><span>窗口均值</span><b>${l.avg == null ? '—' : Math.round(l.avg) + ' ms'}</b></div>
      <div class="r"><span>抖动</span><b>${l.jit == null ? '—' : (+l.jit).toFixed(1) + ' ms'}</b></div>
      <div class="r"><span>丢包</span><b>${pct(l.loss)}</b></div>`;
    tip.classList.add('show');
    const x = e.clientX - box.left, y = e.clientY - box.top;
    const flip = x > box.width - 250;
    tip.style.left = (flip ? x - tip.offsetWidth - 16 : x + 16) + 'px';
    tip.style.top = Math.max(8, Math.min(y - 14, box.height - tip.offsetHeight - 8)) + 'px';
  }

  function hideTip() { $('geoTip').classList.remove('show'); }

  function select(key, keep) {
    selected = (selected === key && !keep) ? null : key;
    paintLinks();
  }

  // 探测光点：一个光点完成「请求 → 应答」的完整往返，速度与 RTT 挂钩
  function animateDots() {
    if (reducing) return;
    const step = () => {
      if (active && !document.hidden) {
        const t = performance.now();
        const inv = (1 / vk).toFixed(4);
        for (const {g, path, phase, rtt} of dotEls.values()) {
          let len;
          try { len = path.getTotalLength(); } catch (e) { continue; }
          if (!len) continue;
          const dur = (650 + (rtt || 80) * 5.5) * 2;   // 往返
          const prog = ((t / dur) + phase) % 2;
          const at = prog <= 1 ? prog * len : (2 - prog) * len;
          const pt = path.getPointAtLength(at);
          g._x = pt.x; g._y = pt.y;
          g.setAttribute('transform', `translate(${pt.x.toFixed(1)},${pt.y.toFixed(1)}) scale(${inv})`);
        }
      }
      rafId = requestAnimationFrame(step);
    };
    if (!rafId) rafId = requestAnimationFrame(step);
  }

  async function refresh() {
    if (document.hidden && !forceVisible) return;
    try {
      if (source === 'daily') {
      const q = new URLSearchParams({mode: viewMode});
      if (taskFilter) q.set('task', String(taskFilter));
      daily = await api('/api/geo/daily?' + q.toString());
      if (selected && !(daily.links || []).some(l => ('d' + l.task_id + '_' + l.machine_id + '_' + l.dest_id) === selected)) {
        selected = null;
      }
      render();
      syncDailyBar();
    } else {
      const q = runFilter ? `?run_id=${encodeURIComponent(runFilter)}` : '';
      test = await api('/api/geo/test' + q);
      if (selected && !(test && test.run && (test.run.links || []).some(l => ('t' + l.machine_id) === selected))) {
        selected = null;
      }
      render();
      syncTestBar();
    }
    } catch (e) { /* 轮询失败保持上一帧，不打扰 */ }
  }

  function updateSubtitle() {
    const el2 = $('geoSubtitle');
    if (source === 'test') {
      el2.textContent = 'Backend / Target Test Links';
    } else {
      el2.textContent = viewMode === 'B'
        ? 'Nodes to Panel Network Monitoring'
        : 'Backend to Target Network Monitoring';
    }
    const card = $('geoCard');
    if (card) card.classList.toggle('is-live', source === 'daily' && autoRefresh);
  }

  function syncDailyBar() {
    const tasks = (daily && daily.tasks) || [];
    const running = tasks.filter(t => t.status === 'running').length;
    const sel = $('geoTaskFilter');
    if (document.activeElement !== sel) {
      sel.innerHTML = '<option value="">全部任务</option>' + tasks.map(t =>
        `<option value="${t.id}"${taskFilter === t.id ? ' selected' : ''}>` +
        `${esc(t.name)}${t.status !== 'running' ? '（' + (t.status === 'paused' ? '已暂停' : '已停止') + '）' : ''}</option>`).join('');
    }
    $('geoTaskStats').innerHTML = `<b>${tasks.length}</b> 个任务 · <b class="${running ? 'okc' : ''}">${running}</b> 运行中`;
    $('geoDirSeg').querySelectorAll('button').forEach(b =>
      b.classList.toggle('active', b.dataset.dir === viewMode));
    $('geoAutoRefresh').checked = autoRefresh;
    const settings = (daily && daily.settings) || {};
    if (document.activeElement !== $('geoCount')) $('geoCount').value = String(settings.count || 4);
    if (document.activeElement !== $('geoPanelRegion')) $('geoPanelRegion').value = settings.panel_region || '';
  }

  function syncTestBar() {
    const bar = $('geoTestBar');
    if (!bar) return;
    const run = test && test.run;
    const runs = (test && test.runs) || (run && run.runs) || (run ? [run] : []);
    const sel = $('geoRunFilter');
    const curId = run ? run.run_id : null;
    if (sel && document.activeElement !== sel) {
      sel.innerHTML = runs.length
        ? runs.map((r, i) => {
            const rid = r.run_id ?? r.id;
            const src = r.source === 'schedule' ? '定时' : '手动';
            const cnt = r.total != null ? ` · ${r.done}/${r.total}` : '';
            return `<option value="${rid}"${rid === curId ? ' selected' : ''}>` +
              `#${rid}${i === 0 ? '（最新）' : ''} · ${esc(r.target_name || '')} · ${esc(r.created_at || '')} · ${src}${cnt}</option>`;
          }).join('')
        : '<option value="">暂无已完成的测试记录</option>';
    }
    const idx = runs.findIndex(r => (r.run_id ?? r.id) === curId);
    const prev = $('geoRunPrev'), next = $('geoRunNext'), openBtn = $('geoOpenRunBtn');
    if (prev) prev.disabled = idx < 0 || idx >= runs.length - 1;
    if (next) next.disabled = idx <= 0;
    if (openBtn) openBtn.classList.toggle('hidden', !curId || typeof openRun !== 'function');
    const st = $('geoRunStats');
    if (st) {
      st.innerHTML = run
        ? `共 <b>${runs.length}</b> 轮记录 · 当前 <b>#${curId}</b>（${run.source === 'schedule' ? '定时任务' : '手动测试'} · ${esc(run.created_at || '')}）`
        : '还没有已完成的测试记录';
    }
  }

  function setRunFilter(v) {
    runFilter = v ? +v : null;
    refresh();
  }

  function stepRun(dir) {
    const run = test && test.run;
    const runs = (test && test.runs) || (run && run.runs) || [];
    if (!runs.length || !run) return;
    const idx = runs.findIndex(r => (r.run_id ?? r.id) === run.run_id);
    const nextIdx = idx + dir;
    if (nextIdx >= 0 && nextIdx < runs.length) {
      const targetId = runs[nextIdx].run_id ?? runs[nextIdx].id;
      runFilter = nextIdx === 0 ? null : targetId;
      refresh();
    }
  }

  function setSource(v) {
    source = v === 'daily' ? 'daily' : 'test';
    selected = null;
    persistView();
    $('geoSourceSeg').querySelectorAll('button').forEach(b =>
      b.classList.toggle('active', b.dataset.source === source));
    $('geoDailyBar').classList.toggle('hidden', source !== 'daily');
    if ($('geoTestBar')) $('geoTestBar').classList.toggle('hidden', source !== 'test');
    updateSubtitle();
    if (source === 'daily') syncDailyBar(); else syncTestBar();
    refresh();
  }

  // 视图级切换：只换地图数据来源，绝不触碰后台任务
  function setDir(dir) {
    viewMode = dir === 'B' ? 'B' : 'A';
    selected = null;
    if (taskFilter != null) taskFilter = null;   // 任务属于特定模式，切视图后回全部
    persistView();
    updateSubtitle();
    syncDailyBar();
    refresh();
  }

  function setTaskFilter(v) {
    taskFilter = v ? +v : null;
    selected = null;
    refresh();
  }

  function setAutoRefresh(on) {
    autoRefresh = !!on;
    persistView();
    updateSubtitle();
    clearInterval(pollTimer);
    pollTimer = null;
    if (autoRefresh && active) pollTimer = setInterval(refresh, VIEW_INTERVAL);
  }

  // ---------------- 监控任务管理（弹窗） ----------------

  async function openWatchTasks() {
    $('watchModal').classList.remove('hidden');
    $('watchFormView').classList.add('hidden');
    $('watchListView').classList.remove('hidden');
    pendingDeleteId = null;
    await loadWatchTaskList();
  }

  function closeWatchModal() { $('watchModal').classList.add('hidden'); }

  async function loadWatchTaskList() {
    try {
      const data = await api('/api/watch/tasks');
      watchTasksCache = data.tasks || [];
      renderWatchList(watchTasksCache);
    } catch (e) { errorToast('加载任务列表失败: ' + e.message); }
  }

  function renderWatchList(tasks) {
    $('watchTaskBody').innerHTML = tasks.map(t => {
      const st = t.status === 'running'
        ? '<span class="geo-status ok"><i class="geo-live-dot"></i>运行中</span>'
        : t.status === 'paused' ? '<span class="geo-status warn">已暂停</span>'
        : '<span class="geo-status stale">已停止</span>';
      const rel = t.mode === 'A'
        ? `${t.backend_ids.length} 台 → ${t.target_ids.length} 目标 · ${t.probe_count} 条`
        : `${t.backend_ids.length} 台 → 面板 · ${t.probe_count} 条`;
      const ops = [];
      if (t.status === 'running') ops.push(['pause', '暂停'], ['stop', '停止']);
      else ops.push(['start', t.status === 'paused' ? '恢复' : '启动']);
      ops.push(['edit', '编辑'], ['view', '查看地图']);
      let deleteCell = `<button type="button" class="small geo-op-del" data-wtask="${t.id}" data-wact="delete">删除</button>`;
      if (pendingDeleteId === t.id) {
        deleteCell = `<span class="geo-del-confirm"><span class="muted">删除：</span>` +
          `<button type="button" class="small" data-wtask="${t.id}" data-wact="purge-no">保留历史</button>` +
          `<button type="button" class="small danger" data-wtask="${t.id}" data-wact="purge-yes">连同历史</button>` +
          `<button type="button" class="small" data-wtask="${t.id}" data-wact="cancel-del">取消</button></span>`;
      }
      return `<tr>
        <td><b>${esc(t.name)}</b></td>
        <td><span class="geo-mode m-${t.mode === 'B' ? 'b' : 'a'}">${t.mode === 'B' ? 'B · 节点→面板' : 'A · 后端→目标'}</span></td>
        <td>${esc(rel)}</td>
        <td class="num">${t.interval_seconds}s</td>
        <td>${st}</td>
        <td class="num">${fmtAgo(t.last_report_at)}</td>
        <td class="watch-ops">${ops.map(([a, label]) =>
          `<button type="button" class="small geo-op-${a}" data-wtask="${t.id}" data-wact="${a}">${label}</button>`).join('')}${deleteCell}</td>
      </tr>`;
    }).join('') ||
      '<tr class="geo-empty"><td colspan="7">还没有监控任务，点「＋ 创建监控任务」开始。</td></tr>';
    $('watchTaskBody').querySelectorAll('button[data-wact]').forEach(btn => {
      btn.addEventListener('click', () => watchTaskOp(+btn.dataset.wtask, btn.dataset.wact));
    });
  }

  async function watchTaskOp(id, act) {
    if (act === 'delete') { pendingDeleteId = id; renderWatchList(watchTasksCache); return; }
    if (act === 'cancel-del') { pendingDeleteId = null; renderWatchList(watchTasksCache); return; }
    if (act === 'purge-no' || act === 'purge-yes') {
      try {
        await api(`/api/watch/tasks/${id}?purge=${act === 'purge-yes' ? 1 : 0}`, {method: 'DELETE'});
        toast(act === 'purge-yes' ? '任务与历史数据已删除' : '任务已删除（历史数据保留，到期自动清理）');
        pendingDeleteId = null;
        await loadWatchTaskList();
        refresh();
      } catch (e) { errorToast('删除失败: ' + e.message); }
      return;
    }
    if (act === 'edit') {
      const t = watchTasksCache.find(x => x.id === id);
      if (t) openWatchForm(t);
      return;
    }
    if (act === 'view') {
      const t = watchTasksCache.find(x => x.id === id);
      if (!t) return;
      closeWatchModal();
      source = 'daily';
      taskFilter = t.id;
      viewMode = t.mode;
      selected = null;
      persistView();
      $('geoSourceSeg').querySelectorAll('button').forEach(b =>
        b.classList.toggle('active', b.dataset.source === 'daily'));
      $('geoDailyBar').classList.remove('hidden');
      if ($('geoTestBar')) $('geoTestBar').classList.add('hidden');
      updateSubtitle();
      syncDailyBar();
      refresh();
      return;
    }
    try {
      await api(`/api/watch/tasks/${id}/action`, {method: 'POST', body: JSON.stringify({action: act})});
      await loadWatchTaskList();
      refresh();   // 配置版本变化，Agent 将在下个心跳周期内同步启停
    } catch (e) { errorToast('操作失败: ' + e.message); }
  }

  async function openWatchForm(task) {
    let machines = dailyMachines();
    if (!machines.length) {
      try { machines = (await api('/api/geo/daily?mode=A')).machines || []; }
      catch (e) { return errorToast('加载机器失败: ' + e.message); }
    }
    watchFormMachines = machines;
    $('watchFormTitle').textContent = task ? '编辑监控任务' : '创建监控任务';
    $('wTaskId').value = task ? task.id : '';
    $('wName').value = task ? task.name : '';
    $('wMode').value = task ? task.mode : 'A';
    $('wInterval').value = String(task ? task.interval_seconds : 10);
    renderWatchFormChecks(task ? {backends: task.backend_ids, targets: task.target_ids} : null);
    $('watchListView').classList.add('hidden');
    $('watchFormView').classList.remove('hidden');
    $('watchModal').classList.remove('hidden');
  }

  let watchFormMachines = [];

  function renderWatchFormChecks(sel) {
    const mode = $('wMode').value;
    sel = sel || {
      backends: [...document.querySelectorAll('.w-backend:checked')].map(x => +x.value),
      targets: [...document.querySelectorAll('.w-target:checked')].map(x => +x.value),
    };
    const backends = watchFormMachines.filter(m => mode === 'B' || m.role === 'backend');
    $('wBackends').innerHTML = backends.map(m =>
      `<label class="form-check"><input type="checkbox" class="w-backend" value="${m.id}"${sel.backends.includes(m.id) ? ' checked' : ''}><span class="nm">${esc(m.name)}</span><span class="rg">${esc(m.region) || '—'}</span>` +
      `${m.watch_supported ? '' : ' <span class="geo-status stale">需更新 Agent</span>'}</label>`).join('') ||
      '<span class="muted" style="font-size:12px">没有可参与的机器</span>';
    const targets = watchFormMachines.filter(m => m.role === 'target');
    $('wTargets').innerHTML = targets.map(m =>
      `<label class="form-check"><input type="checkbox" class="w-target" value="${m.id}"${sel.targets.includes(m.id) ? ' checked' : ''}><span class="nm">${esc(m.name)}</span><span class="rg">${esc(m.region) || '—'}</span></label>`).join('') ||
      '<span class="muted" style="font-size:12px">还没有目标机器：请先在「机器」页添加目标机器</span>';
    $('wTargetsField').classList.toggle('hidden', mode === 'B');
    updateConflictHint();
  }

  function updateConflictHint() {
    const hint = $('wConflictHint');
    const body = collectWatchForm();
    const exclude = $('wTaskId').value ? +$('wTaskId').value : null;
    const conflicts = watchTasksCache.filter(t => !t.archived && t.id !== exclude && t.mode === body.mode)
      .filter(t => t.backend_ids.some(id => body.backend_ids.includes(id)))
      .filter(t => body.mode === 'B' ||
                   t.target_ids.some(id => body.target_ids.includes(id)));
    if (conflicts.length) {
      hint.classList.remove('hidden');
      hint.innerHTML = '⚠ 与已有任务存在相同探测关系：' +
        conflicts.map(t => esc(t.name)).join('、') +
        '。保存后这些关系会被重复探测（浪费少量资源），如不需要请调整参与机器或目标。';
    } else {
      hint.classList.add('hidden');
    }
  }

  function collectWatchForm() {
    return {
      name: $('wName').value,
      mode: $('wMode').value,
      interval: +$('wInterval').value,
      backend_ids: [...document.querySelectorAll('.w-backend:checked')].map(x => +x.value),
      target_ids: [...document.querySelectorAll('.w-target:checked')].map(x => +x.value),
    };
  }

  async function saveWatchTask() {
    const body = collectWatchForm();
    const id = $('wTaskId').value;
    try {
      const res = id
        ? await api(`/api/watch/tasks/${id}`, {method: 'POST', body: JSON.stringify(body)})
        : await api('/api/watch/tasks', {method: 'POST', body: JSON.stringify(body)});
      toast(res.conflicts && res.conflicts.length
        ? `任务已保存；注意它与 ${res.conflicts.length} 个任务存在相同探测关系，将按你的选择同时运行`
        : '任务已保存，参与机器将在下一个心跳周期开始探测');
      await loadWatchTaskList();
      $('watchFormView').classList.add('hidden');
      $('watchListView').classList.remove('hidden');
      refresh();
    } catch (e) { errorToast('保存失败: ' + e.message); }
  }

  function toggleChecks(all) {
    // 模式 B 没有目标机器选择，只全选/反选参与机器
    const scope = $('wMode').value === 'B' ? '.w-backend' : '.w-backend,.w-target';
    document.querySelectorAll(scope)
      .forEach(cb => { cb.checked = all ? true : !cb.checked; });
    updateConflictHint();
  }

  async function savePanelSettings(patch) {
    try {
      await api('/api/watch/settings', {method: 'POST', body: JSON.stringify(patch)});
      await refresh();
    } catch (e) { errorToast('保存设置失败: ' + e.message); }
  }

  function onResize() {
    if (!inited || !active) return;
    fitMap();
    buildBase();
    render();
  }

  function init() {
    if (inited) return;
    inited = true;
    fitMap();
    buildBase();
    updateSubtitle();
    fetch('/static/world-110m.json').then(r => r.ok ? r.json() : null).then(j => {
      if (j && j.polys) {
        window.WORLD_POLYS = stitchPolys(j.polys);
        if (active) { buildBase(); render(); }
      }
    }).catch(() => {});   // 底图加载失败：退化为经纬网，节点与链路不受影响
    $('geoSourceSeg').addEventListener('click', e => {
      const b = e.target.closest('button[data-source]');
      if (b) setSource(b.dataset.source);
    });
    $('geoDirSeg').addEventListener('click', e => {
      const b = e.target.closest('button[data-dir]');
      if (b) setDir(b.dataset.dir);
    });
    $('geoTaskFilter').addEventListener('change', e => setTaskFilter(e.target.value));
    if ($('geoRunFilter')) $('geoRunFilter').addEventListener('change', e => setRunFilter(e.target.value));
    if ($('geoRunPrev')) $('geoRunPrev').addEventListener('click', () => stepRun(1));
    if ($('geoRunNext')) $('geoRunNext').addEventListener('click', () => stepRun(-1));
    if ($('geoOpenRunBtn')) $('geoOpenRunBtn').addEventListener('click', () => {
      const rid = test && test.run && test.run.run_id;
      if (rid && typeof openRun === 'function') openRun(rid);
    });
    $('geoAutoRefresh').addEventListener('change', e => setAutoRefresh(e.target.checked));
    $('geoCount').addEventListener('change', e => savePanelSettings({count: +e.target.value}));
    let regionTimer = null;
    $('geoPanelRegion').addEventListener('change', e => {
      clearTimeout(regionTimer);
      regionTimer = setTimeout(() => savePanelSettings({panel_region: e.target.value.trim()}), 300);
    });
    // 表单里切换模式 / 勾选机器时同步刷新目标区与冲突提示
    $('wMode').addEventListener('change', () => renderWatchFormChecks());
    $('watchFormView').addEventListener('change', e => {
      if (e.target.matches('.w-backend,.w-target')) updateConflictHint();
    });
    // 点遮罩空白处关闭弹窗；Esc 关闭弹窗 / 取消选中
    $('watchModal').addEventListener('click', e => { if (e.target === $('watchModal')) closeWatchModal(); });
    document.addEventListener('keydown', e => {
      if (e.key !== 'Escape' || !active) return;
      if (!$('watchModal').classList.contains('hidden')) closeWatchModal();
      else if (selected) select(selected);
    });
    bindMapGestures();
    window.addEventListener('resize', onResize);
    document.addEventListener('linkpilot:appearance', onResize);
    // 页面从后台回到前台时立刻补一次刷新，避免停留在旧画面
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && active && source === 'daily') refresh();
    });
    animateDots();
  }

  function onTab(on) {
    active = on;
    if (on) {
      init();
      fitMap();
      buildBase();
      $('geoSourceSeg').querySelectorAll('button').forEach(b =>
        b.classList.toggle('active', b.dataset.source === source));
      $('geoDailyBar').classList.toggle('hidden', source !== 'daily');
      if ($('geoTestBar')) $('geoTestBar').classList.toggle('hidden', source !== 'test');
      updateSubtitle();
      render();
      refresh();
      clearInterval(pollTimer);
      pollTimer = autoRefresh ? setInterval(refresh, VIEW_INTERVAL) : null;
    } else {
      clearInterval(pollTimer);
      pollTimer = null;
      hideTip();
      closeWatchModal();
    }
  }

  return {onTab, setSource, setDir, openWatchTasks, closeWatchModal, openWatchForm,
          saveWatchTask, toggleChecks, setAutoRefresh,
          // 诊断出口：自动化验证用（正常 UI 不触达）
          __refresh: refresh,
          __setForceVisible: v => { forceVisible = !!v; },
          __state: () => ({source, viewMode, taskFilter, autoRefresh,
                           tasks: (daily && daily.tasks || []).length,
                           links: (daily && daily.links || []).length})};
})();
