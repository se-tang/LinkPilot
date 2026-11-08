let MACHINES = [];
let RUN_PORTS = {};          // 机器ID -> 本次测试单独指定的 iperf3 端口（未改过的机器不在此表）
let CURRENT_RUN = null;
let pollTimer = null;
let agentTimer = null;
// SSE：服务端推送「哪些数据变了」，变了才去拉对应接口；连不上就退回定时轮询
let SSE_SOURCE = null;
let SSE_OK = false;
let SSE_PRIMED = false;
let SSE_LAST = {};
let agentMachine = null;
let agentAwaitingReconnect = false;
let runFormInit = false;
let EXPANDED_RUN_GROUPS = new Set();
let EXPANDED_SCHED_CARDS = new Set();   // 手机端定时任务卡片默认收起，记住展开的id
let RUNS_FILTER = 'manual';  // 测试记录默认只看手动，定时任务记录单独一档，避免混排杂乱
let ALL_RUNS = [];           // 全量测试记录缓存，供总览页「最近测试」使用
const MACHINE_DRAG = {
  state: 'idle', mode: '', source: null, rows: [], items: [], oldRows: [],
  finalRows: [], startClientX: 0, startClientY: 0, pointerX: 0, pointerY: 0,
  layoutTop: 0, ghost: null, raf: 0, holdTimer: 0, suppressClickUntil: 0,
};
let MACHINE_ORDER_SAVING = false;
// 数据指纹：后台轮询拿到相同数据时直接跳过重绘，避免与用户操作撞车造成卡顿
let LAST_MACHINES_SIG = '', LAST_SCHEDULES_SIG = '', LAST_RUNS_SIG = '', LAST_CMP_SIG = '';
// 由模板写在 <body data-*> 上：静态脚本可以不随版本变化而失效
const LP_CFG = document.body.dataset;
const DEFAULT_AGENT_URL = LP_CFG.agentUrl || location.origin;
const ALLOW_INSECURE_AGENT = LP_CFG.allowInsecureAgent === '1';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

async function api(path, opts = {}) {
  const res = await fetch(path, {headers: {'Content-Type': 'application/json'}, ...opts});
  if (res.status === 401 && !path.startsWith('/api/login')) {
    location.href = '/login';
    throw new Error('未登录');
  }
  let data = null;
  try { data = await res.json(); } catch (e) {}
  if (!res.ok) throw new Error((data && data.error) || res.statusText);
  return data;
}

function toast(msg, ms = 3500, type = '') {
  const t = document.createElement('div');
  t.className = 'toast' + (type ? ` ${type}` : '');
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; }, Math.max(0, ms - 300));
  setTimeout(() => t.remove(), ms);
}
const errorToast = (msg, ms = 4200) => toast(msg, ms, 'error');

/* 统计卡数字滚动（系统开启"减少动态效果"时直接落值） */
function setStat(id, target) {
  const el = $(id);
  if (!el) return;
  target = Number(target) || 0;
  const from = Number(el.dataset.v || 0);
  el.dataset.v = String(target);
  if (from === target) { el.textContent = String(target); return; }
  if (window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches) {
    el.textContent = String(target);
    return;
  }
  cancelAnimationFrame(el._raf);
  const t0 = performance.now(), dur = 550;
  const step = t => {
    const p = Math.min(1, (t - t0) / dur);
    el.textContent = Math.round(from + (target - from) * (1 - Math.pow(1 - p, 3)));
    if (p < 1) el._raf = requestAnimationFrame(step);
  };
  el._raf = requestAnimationFrame(step);
}

/* 统计卡 sparkline：在线/后端/任务用会话内历史，记录数用按日统计 */
const SPARK_HIST = {sparkStatOnline: [], sparkStatBackends: [], sparkStatSchedules: []};
function drawSpark(id, data) {
  const svg = $(id);
  if (!svg) return;
  const vals = (data || []).filter(v => Number.isFinite(v));
  if (!vals.length) { svg.innerHTML = ''; return; }
  const W = 64, H = 22, pad = 2;
  const min = Math.min(...vals), max = Math.max(...vals);
  const x = i => vals.length === 1 ? W - 4 : pad + (W - pad * 2) * (i / (vals.length - 1));
  const y = v => (max === min ? H / 2 : pad + (H - pad * 2) * (1 - (v - min) / (max - min)));
  const pts = vals.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`);
  const last = pts[pts.length - 1].split(',');
  svg.innerHTML = `<polyline points="${pts.join(' ')}"/><circle cx="${last[0]}" cy="${last[1]}" r="2.4"/>`;
}
function pushSpark(id, v) {
  const arr = SPARK_HIST[id];
  if (!arr) return;
  arr.push(Number(v) || 0);
  if (arr.length > 24) arr.shift();
  drawSpark(id, arr);
}

/* 触觉反馈：支持的设备轻微震动，其余环境静默跳过 */
function buzz(ms = 8) {
  try { if (navigator.vibrate) navigator.vibrate(ms); } catch (e) {}
}

/* 空状态插画 */
const EMPTY_ICONS = {
  server: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="10" y="12" width="44" height="16" rx="4"/><rect x="10" y="36" width="44" height="16" rx="4"/><path d="M18 20h.5M18 44h.5M28 20h16M28 44h16"/></svg>',
  runs: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M16 8h24l10 10v38H16z"/><path d="M40 8v10h10M24 30h18M24 40h18M24 50h10"/></svg>',
  clock: '<svg viewBox="0 0 64 64" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="32" cy="36" r="18"/><path d="M32 27v9l6 4M24 8h16M48 10l5 5"/></svg>',
};
function emptyState(icon, title, sub, action) {
  return `<tr><td colspan="99" class="empty-cell"><div class="empty-state">${icon}`
    + `<b>${esc(title)}</b><span class="muted">${esc(sub)}</span>${action || ''}</div></td></tr>`;
}

function statusText(s) {
  return {running: '进行中', finished: '已完成', partial: '部分失败', failed: '失败', stopped: '已停止'}[s] || s;
}

function fmt(v, suffix) {
  if (v === null || v === undefined) return '—';
  return Number(v).toFixed(1).replace(/\.0$/, '') + (suffix || '');
}

function fmtN(v, digits = 1, suffix = '') {
  if (v === null || v === undefined) return '—';
  let text = Number(v).toFixed(digits);
  if (text.includes('.')) text = text.replace(/0+$/, '').replace(/\.$/, '');
  return text + (suffix || '');
}

function ratingClass(r) {
  const code = {'优秀': 'S', '良好': 'A', '一般': 'B', '较差': 'C',
                '失败': 'F', '数据不足': '?'}[r] || r;
  return {'S': 'rating-s', 'A': 'rating-a', 'B': 'rating-b',
          'C': 'rating-c', 'F': 'rating-f', '?': 'rating-unknown'}[code] || 'rating-unknown';
}

function ratingText(value) {
  const code = {'优秀': 'S', '良好': 'A', '一般': 'B', '较差': 'C',
                '失败': 'F', '数据不足': '?'}[value] || value;
  const label = {'S': '优秀', 'A': '良好', 'B': '一般',
                 'C': '较差', 'F': '失败', '?': '数据不足'}[code];
  return code && label ? `${code} ${label}` : (value || '—');
}

function ratingInfo(metrics, status, error) {
  const mt = metrics || {};
  const code = mt.rating_code || ({'优秀':'S','良好':'A','一般':'B','较差':'C','失败':'F','数据不足':'?'}[mt.rating]) || '';
  const label = mt.rating_label || ({'S':'优秀','A':'良好','B':'一般','C':'较差','F':'失败','?':'数据不足'}[code]) || mt.rating || '';
  if (code) {
    return {
      code, label,
      tags: Array.isArray(mt.rating_tags) ? mt.rating_tags : [],
      summary: mt.rating_summary || mt.rating_detail || '',
      diagnostics: Array.isArray(mt.rating_diagnostics) ? mt.rating_diagnostics : [],
    };
  }
  if (status === 'failed' || status === 'stopped') {
    return {
      code: 'F', label: '失败', tags: ['测试失败'],
      summary: error || '测试未完成，无法评估线路质量。',
      diagnostics: [{name:'测试状态', status:'critical', text:error || '测试未完成'}],
    };
  }
  return {
    code: '?', label: '数据不足', tags: ['关键指标缺失'],
    summary: '当前记录缺少完整评级数据，请查看原始输出。',
    diagnostics: [{name:'数据完整性', status:'critical', text:'缺少结构化评级数据'}],
  };
}

function ratingBadgeHtml(info) {
  return `<span class="rb ${ratingClass(info.code)}">${esc(info.code)} ${esc(info.label)}</span>`;
}

function ratingCellHtml(metrics, status, error) {
  const info = ratingInfo(metrics, status, error);
  return ratingBadgeHtml(info);
}

function ratingExplanationHtml(metrics, status, error) {
  const info = ratingInfo(metrics, status, error);
  return `<span class="quality-summary">${esc(info.summary || '—')}</span>`;
}

function copyText(text, tip) {
  const done = () => toast(tip || '已复制');
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => copyFallback(text, done));
  } else {
    copyFallback(text, done);
  }
}

async function copyRichText(text, html, tip) {
  if (navigator.clipboard && navigator.clipboard.write && window.ClipboardItem) {
    try {
      await navigator.clipboard.write([new ClipboardItem({
        'text/plain': new Blob([text], {type: 'text/plain'}),
        'text/html': new Blob([html], {type: 'text/html'}),
      })]);
      toast(tip || '已复制');
      return;
    } catch (e) {}
  }
  if (copyHtmlFallback(html)) {
    toast(tip || '已复制');
    return;
  }
  copyText(text, tip + '（当前浏览器不支持富文本，仅复制文字）');
}

function copyHtmlFallback(html) {
  const holder = document.createElement('div');
  holder.contentEditable = 'true';
  holder.innerHTML = html;
  holder.style.cssText = 'position:fixed;left:-10000px;top:0;width:900px;background:#fff';
  document.body.appendChild(holder);
  const range = document.createRange();
  range.selectNodeContents(holder);
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
  let ok = false;
  try { ok = document.execCommand('copy'); } catch (e) {}
  selection.removeAllRanges();
  holder.remove();
  return ok;
}

function copyFallback(text, done) {
  const ta = document.createElement('textarea');
  ta.value = text;
  document.body.appendChild(ta);
  ta.select();
  document.execCommand('copy');
  ta.remove();
  done();
}

/* ---------------- 机器管理 ---------------- */

async function loadMachines(force = false) {
  // 拖拽期间跳过后台刷新，避免机器行被整体重绘打断手势。
  if (MACHINE_DRAG.state !== 'idle' || MACHINE_ORDER_SAVING) return;
  // 时间戳参数防缓存：即使浏览器忽略 no-cache 头，也不会用到升级前的旧数据；
  // 数据指纹没变就跳过全部重绘（8 秒一轮的刷新在静态数据下是零开销）
  const list = await api('/api/machines?_=' + Date.now());
  const sig = JSON.stringify(list);
  if (!force && sig === LAST_MACHINES_SIG) return;
  if (MACHINE_DRAG.state !== 'idle' || MACHINE_ORDER_SAVING) return;
  LAST_MACHINES_SIG = sig;
  MACHINES = list;
  renderMachines();
  renderRunForm();
  renderScheduleForm();
}

function renderMachines() {
  const tb = $('machinesBody');
  const onlineCount = MACHINES.filter(m => m.online).length;
  const backendCount = MACHINES.filter(m => m.role === 'backend' && m.online).length;
  setStat('statOnline', onlineCount);
  setStat('statBackends', backendCount);
  pushSpark('sparkStatOnline', onlineCount);
  pushSpark('sparkStatBackends', backendCount);
  if ($('machinesSummary')) {
    $('machinesSummary').textContent = MACHINES.length
      ? `${MACHINES.length} 台机器 · ${onlineCount} 在线 · ${MACHINES.filter(m => m.role === 'backend').length} 后端`
      : '暂无机器';
  }
  const outdated = MACHINES.filter(m => m.agent_outdated).length;
  if ($('updateAllAgentsBtn')) {
    $('updateAllAgentsBtn').classList.toggle('hidden', outdated === 0);
    $('updateAllAgentsBtn').textContent = `↑ 全部更新 Agent（${outdated}）`;
  }
  if (!MACHINES.length) {
    tb.innerHTML = emptyState(EMPTY_ICONS.server, '还没有机器', '添加目标机与后端机后，就能发起线路测试',
      '<button class="small" data-action="openMachineForm">＋ 添加机器</button>');
    return;
  }
  tb.innerHTML = MACHINES.map(m => `
    <tr data-machine-id="${m.id}">
      <td class="machine-name"><span class="machine-drag-handle" aria-hidden="true"
          title="按住拖动排序"><svg viewBox="0 0 10 16" fill="currentColor"><circle cx="3" cy="3" r="1.2"/><circle cx="7" cy="3" r="1.2"/><circle cx="3" cy="8" r="1.2"/><circle cx="7" cy="8" r="1.2"/><circle cx="3" cy="13" r="1.2"/><circle cx="7" cy="13" r="1.2"/></svg></span><span class="dot ${m.online ? 'on' : 'off'}"></span><b>${esc(m.name)}</b>
        <div class="quiet">${m.active_job ? '执行中' : (m.online ? '在线' : '离线')}</div></td>
      <td class="machine-address"><span class="badge ${m.role}">${m.role === 'target' ? '目标' : '后端'}</span>
        <div class="quiet">${addrCell(m)}</div></td>
      <td class="machine-meta">${esc(m.region) || '—'} · ${esc(m.bandwidth) || '—'}
        ${m.agent_outdated ? `<span class="badge update" title="面板上的 Agent 脚本比这台机器新；点「更新」可直接推送，无需重跑接入命令">Agent 可更新</span>` : ''}
        <div class="quiet">${esc(m.hostname) || '—'}</div></td>
      <td class="machine-actions"><div class="btn-grid">
        <button class="small" data-action="showAgentCmd" data-id="${m.id}">接入</button>
        ${m.agent_outdated ? `<button class="small" data-action="updateAgent" data-id="${m.id}"
            title="${m.agent_push_supported ? '直接推送到这台机器（Agent 下载 + root 助手替换）' : '这台机器的 Agent 太旧，无法推送更新，需要重跑接入命令'}">更新</button>` : ''}
        <button class="small" data-action="discoverMachine" data-id="${m.id}" title="重新探测本机 IPv4 / IPv6 地址">探测</button>
        <button class="small" data-action="editMachine" data-id="${m.id}">编辑</button>
        <button class="small danger" data-action="deleteMachine" data-id="${m.id}">删除</button>
      </div></td>
    </tr>`).join('');
}

function machineDragControl(target) {
  return !!(target.closest && target.closest(
    'button,a,input,select,textarea,label,summary,[contenteditable="true"]'));
}

function clearMachineDragPending() {
  clearTimeout(MACHINE_DRAG.holdTimer);
  MACHINE_DRAG.holdTimer = 0;
  if (MACHINE_DRAG.state === 'pending') {
    if (MACHINE_DRAG.source) MACHINE_DRAG.source.classList.remove('machine-drag-pending');
    MACHINE_DRAG.state = 'idle';
    MACHINE_DRAG.source = null;
  }
}

function prepareMachineDrag(row, clientX, clientY, mode) {
  if (!row || MACHINE_DRAG.state !== 'idle' || MACHINE_ORDER_SAVING ||
      machineDragControl(row)) return;
  const rows = [...$('machinesBody').querySelectorAll('tr[data-machine-id]')];
  if (rows.length < 2) return;
  Object.assign(MACHINE_DRAG, {
    state: 'pending', mode, source: row, pointerX: clientX, pointerY: clientY,
    startClientX: clientX, startClientY: clientY,
  });
  row.classList.add('machine-drag-pending');
  // 触屏必须短暂按住才开始拖拽，否则会与页面纵向滚动争抢手势。
  if (mode === 'touch') {
    MACHINE_DRAG.holdTimer = setTimeout(() => {
      if (MACHINE_DRAG.state === 'pending') activateMachineDrag();
    }, 220);
  }
}

function activateMachineDrag() {
  if (MACHINE_DRAG.state !== 'pending') return;
  const source = MACHINE_DRAG.source;
  if (!source || !source.isConnected) return clearMachineDragPending();
  clearTimeout(MACHINE_DRAG.holdTimer);
  const rows = [...$('machinesBody').querySelectorAll('tr[data-machine-id]')];
  const scrollY = window.scrollY;
  const rect = source.getBoundingClientRect();
  const items = rows.map(row => {
    const r = row.getBoundingClientRect();
    return {
      row,
      id: Number(row.dataset.machineId),
      top: r.top + scrollY,
      height: r.height,
    };
  });
  Object.assign(MACHINE_DRAG, {
    state: 'active', source, rows, items,
    oldRows: rows.slice(), finalRows: rows.slice(),
    layoutTop: Math.min(...items.map(item => item.top)),
    ghost: null, raf: 0,
  });
  source.classList.remove('machine-drag-pending');
  source.classList.add('machine-drag-placeholder');
  source.setAttribute('aria-grabbed', 'true');
  rows.forEach(row => row.classList.add('machine-row-shifting'));

  // 浮层只保留机器名和角色，固定单行高度；不能克隆整张移动端卡片，
  // 否则表格列在拖动时会重新排版、文字竖排并撑高浮层。
  const machine = MACHINES.find(item => item.id === Number(source.dataset.machineId)) || {};
  const ghost = document.createElement('div');
  ghost.className = 'machine-drag-ghost';
  const roleLabel = machine.role === 'target' ? '目标' : '后端';
  const stateLabel = machine.active_job ? '执行中' : (machine.online ? '在线' : '离线');
  ghost.innerHTML = `
    <span class="machine-drag-handle" aria-hidden="true">
      <svg viewBox="0 0 10 16" fill="currentColor">
        <circle cx="3" cy="3" r="1.2"/><circle cx="7" cy="3" r="1.2"/>
        <circle cx="3" cy="8" r="1.2"/><circle cx="7" cy="8" r="1.2"/>
        <circle cx="3" cy="13" r="1.2"/><circle cx="7" cy="13" r="1.2"/>
      </svg>
    </span>
    <span class="ghost-name">${esc(machine.name || source.querySelector('.machine-name b')?.textContent || '')}</span>
    <span class="badge ${machine.role === 'target' ? 'target' : 'backend'}">${roleLabel}</span>
    <span class="ghost-state">${stateLabel}</span>`;
  const ghostWidth = Math.min(rect.width, window.innerWidth - 24, 460);
  ghost.style.width = `${Math.max(220, ghostWidth)}px`;
  ghost.style.left = `${rect.left + Math.max(0, (rect.width - ghostWidth) / 2)}px`;
  ghost.style.top = `${rect.top}px`;
  document.body.appendChild(ghost);
  MACHINE_DRAG.ghost = ghost;
  document.body.classList.add('machine-dragging');
  MACHINE_DRAG.raf = requestAnimationFrame(machineDragFrame);
}

function machineDragFrame() {
  if (MACHINE_DRAG.state !== 'active') return;
  const viewportY = MACHINE_DRAG.pointerY;
  const topEdge = 64;
  const bottomEdge = window.innerHeight - (window.innerWidth <= 720 ? 108 : 24);
  let scrollStep = 0;
  if (viewportY < topEdge) scrollStep = -Math.ceil((topEdge - viewportY) / 4);
  else if (viewportY > bottomEdge) scrollStep = Math.ceil((viewportY - bottomEdge) / 4);
  if (scrollStep) window.scrollBy(0, Math.max(-18, Math.min(18, scrollStep)));

  const pointerDocumentY = MACHINE_DRAG.pointerY + window.scrollY;
  const remaining = MACHINE_DRAG.items.filter(item => item.row !== MACHINE_DRAG.source);
  let insertIndex = 0;
  while (insertIndex < remaining.length &&
         pointerDocumentY > remaining[insertIndex].top + remaining[insertIndex].height / 2) {
    insertIndex += 1;
  }
  const sourceItem = MACHINE_DRAG.items.find(item => item.row === MACHINE_DRAG.source);
  const finalItems = remaining.slice(0, insertIndex).concat(sourceItem, remaining.slice(insertIndex));
  let cursor = MACHINE_DRAG.layoutTop;
  for (const item of finalItems) {
    const offset = cursor - item.top;
    if (item.row.classList.contains('machine-drag-placeholder')) {
      item.row.style.transform = `translate3d(0,${offset}px,0)`;
    } else {
      item.row.style.transform = `translate3d(0,${offset.toFixed(2)}px,0)`;
    }
    cursor += item.height;
  }
  MACHINE_DRAG.finalRows = finalItems.map(item => item.row);
  if (MACHINE_DRAG.ghost) {
    const dx = MACHINE_DRAG.pointerX - MACHINE_DRAG.startClientX;
    const dy = MACHINE_DRAG.pointerY - MACHINE_DRAG.startClientY;
    MACHINE_DRAG.ghost.style.transform =
      `translate3d(${dx}px,${dy}px,0) rotate(.45deg) scale(1.012)`;
  }
  MACHINE_DRAG.raf = requestAnimationFrame(machineDragFrame);
}

async function saveMachineOrder(ids) {
  MACHINE_ORDER_SAVING = true;
  try {
    const result = await api('/api/machines/order', {
      method: 'PUT',
      body: JSON.stringify({ids}),
    });
    if (MACHINE_DRAG.state === 'idle' && Array.isArray(result.machines)) {
      MACHINES = result.machines;
      LAST_MACHINES_SIG = JSON.stringify(result.machines);
    }
  } catch (e) {
    errorToast('机器顺序保存失败：' + e.message);
    LAST_MACHINES_SIG = '';
    MACHINE_ORDER_SAVING = false;
    try { await loadMachines(true); } catch (ignored) {}
    return;
  }
  MACHINE_ORDER_SAVING = false;
}

function finishMachineDrag(cancel = false) {
  if (MACHINE_DRAG.state === 'pending') {
    clearMachineDragPending();
    return;
  }
  if (MACHINE_DRAG.state !== 'active') return;
  cancelAnimationFrame(MACHINE_DRAG.raf);
  MACHINE_DRAG.raf = 0;
  const rows = MACHINE_DRAG.rows;
  const targetRows = cancel ? MACHINE_DRAG.oldRows : MACHINE_DRAG.finalRows;
  const beforeTop = new Map(rows.map(row => [row, row.getBoundingClientRect().top]));
  const reduced = window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  if (MACHINE_DRAG.ghost) MACHINE_DRAG.ghost.remove();
  document.body.classList.remove('machine-dragging');
  rows.forEach(row => {
    row.style.transition = 'none';
    row.style.transform = '';
    row.classList.remove('machine-drag-placeholder', 'machine-row-shifting',
      'machine-drag-pending');
    row.removeAttribute('aria-grabbed');
  });
  const tb = $('machinesBody');
  targetRows.forEach(row => tb.appendChild(row));
  rows.forEach(row => { row.style.transition = ''; });

  if (!reduced) {
    rows.forEach(row => {
      const delta = beforeTop.get(row) - row.getBoundingClientRect().top;
      if (Math.abs(delta) < .5) return;
      row.animate(
        [{transform: `translate3d(0,${delta}px,0)`}, {transform: 'translate3d(0,0,0)'}],
        {
          duration: 480,
          easing: 'cubic-bezier(.2,1.3,.3,1)',
        });
    });
  }

  const byId = new Map(MACHINES.map(machine => [Number(machine.id), machine]));
  MACHINES = targetRows.map(row => byId.get(Number(row.dataset.machineId))).filter(Boolean);
  LAST_MACHINES_SIG = '';
  renderRunForm();
  renderScheduleForm();
  const oldIds = MACHINE_DRAG.oldRows.map(row => Number(row.dataset.machineId));
  const newIds = targetRows.map(row => Number(row.dataset.machineId));
  const changed = oldIds.some((id, index) => id !== newIds[index]);
  MACHINE_DRAG.suppressClickUntil = Date.now() + 360;
  MACHINE_DRAG.state = 'idle';
  MACHINE_DRAG.source = null;
  MACHINE_DRAG.rows = [];
  MACHINE_DRAG.items = [];
  MACHINE_DRAG.oldRows = [];
  MACHINE_DRAG.finalRows = [];
  if (!cancel && changed) saveMachineOrder(newIds);
}

function updateMachineDragPoint(clientX, clientY) {
  MACHINE_DRAG.pointerX = clientX;
  MACHINE_DRAG.pointerY = clientY;
}

$('machinesBody').addEventListener('pointerdown', e => {
  if (e.pointerType === 'touch' || (e.pointerType === 'mouse' && e.button !== 0)) return;
  const row = e.target.closest('tr[data-machine-id]');
  if (!row || machineDragControl(e.target)) return;
  prepareMachineDrag(row, e.clientX, e.clientY, e.pointerType || 'mouse');
});

document.addEventListener('pointermove', e => {
  if (MACHINE_DRAG.state === 'pending' && MACHINE_DRAG.mode !== 'touch') {
    const dx = e.clientX - MACHINE_DRAG.startClientX;
    const dy = e.clientY - MACHINE_DRAG.startClientY;
    if (Math.hypot(dx, dy) >= 5) activateMachineDrag();
  }
  if (MACHINE_DRAG.state !== 'active' || MACHINE_DRAG.mode === 'touch') return;
  e.preventDefault();
  updateMachineDragPoint(e.clientX, e.clientY);
});

document.addEventListener('pointerup', () => {
  if (MACHINE_DRAG.mode === 'touch') return;
  if (MACHINE_DRAG.state === 'active') finishMachineDrag();
  else clearMachineDragPending();
});

document.addEventListener('pointercancel', () => {
  if (MACHINE_DRAG.mode === 'touch') return;
  if (MACHINE_DRAG.state === 'active') finishMachineDrag(true);
  else clearMachineDragPending();
});

$('machinesBody').addEventListener('touchstart', e => {
  if (!e.touches || e.touches.length !== 1) return;
  const row = e.target.closest('tr[data-machine-id]');
  if (!row || machineDragControl(e.target)) return;
  const touch = e.touches[0];
  prepareMachineDrag(row, touch.clientX, touch.clientY, 'touch');
}, {passive: true});

document.addEventListener('touchmove', e => {
  if (MACHINE_DRAG.state === 'pending') {
    const touch = e.touches[0];
    if (!touch) return;
    const dx = touch.clientX - MACHINE_DRAG.startClientX;
    const dy = touch.clientY - MACHINE_DRAG.startClientY;
    if (Math.hypot(dx, dy) > 8) clearMachineDragPending();
    return;
  }
  if (MACHINE_DRAG.state !== 'active' || MACHINE_DRAG.mode !== 'touch') return;
  const touch = e.touches[0];
  if (!touch) return;
  e.preventDefault();
  updateMachineDragPoint(touch.clientX, touch.clientY);
}, {passive: false});

document.addEventListener('touchend', () => {
  if (MACHINE_DRAG.mode !== 'touch') return;
  if (MACHINE_DRAG.state === 'active') finishMachineDrag();
  else clearMachineDragPending();
}, {passive: true});

document.addEventListener('touchcancel', () => {
  if (MACHINE_DRAG.mode !== 'touch') return;
  if (MACHINE_DRAG.state === 'active') finishMachineDrag(true);
  else clearMachineDragPending();
}, {passive: true});

document.addEventListener('contextmenu', e => {
  if ((MACHINE_DRAG.state === 'pending' || MACHINE_DRAG.state === 'active') &&
      e.target.closest('#machinesBody tr[data-machine-id]')) {
    e.preventDefault();
  }
});

window.addEventListener('blur', () => {
  if (MACHINE_DRAG.state === 'active') finishMachineDrag(true);
  else clearMachineDragPending();
});

function testAddr(m, ver) {
  // 与后端 db.machine_test_ip 同一套规则：手动指定优先，其次自动探测到的该协议族地址
  const ov = String(m.addr_override || '').trim();
  if (ov && (ver === 6) === ov.includes(':')) return ov;
  return (ver === 6 ? m.ip6 : m.ip4) || '';
}

function hasV6(m) { return !!testAddr(m, 6); }

function addrCell(m) {
  const manual = String(m.addr_override || '').trim()
    ? ' <span class="muted" title="机器资料里手动指定的测试地址（自动探测结果被忽略）">手动</span>' : '';
  const a4 = testAddr(m, 4);
  const a6 = testAddr(m, 6);
  // 关键区分：有可用地址时（NAT 机用面板观测到的出口 IP）探测提示只是说明，不是故障；
  // 一个地址都没有时才用 ⚠️ 提示需要处理
  const note = m.probe_error
    ? (a4 || a6
        ? ` <span style="cursor:help;font-size:11px" class="muted" title="${esc(m.probe_error)}">ℹ️</span>`
        : ` <span style="cursor:help" title="${esc(m.probe_error)}">⚠️</span>`)
    : '';
  if (!a4 && !a6) {
    return (m.agent_ip
      ? `<span class="muted">探测中…（接入 IP ${esc(m.agent_ip)}）</span>`
      : '<span class="muted">未接入</span>') + manual + note;
  }
  // 双栈机器只显示 IPv4（IPv6 是否存在在后端机器备注里标出，不占地址列空间）；
  // 只有 IPv6 的机器才把 IPv6 当测试地址显示
  if (a4) return `<span class="muted">v4</span> ${esc(a4)}${manual}${note}`;
  return `<span class="muted">v6</span> ${esc(a6)}${manual}${note}`;
}

function updateRoleHint() {
  $('mRoleHint').textContent = $('mRole').value === 'target'
    ? '⚠️ 改为目标机：测试时运行 iperf3 -s（端口在「发起测试」里可自定义，默认 5201），需放行该端口与 ICMP。修改即时生效。'
    : '发起测试端。修改即时生效。';
}

function openMachineForm(m) {
  $('mFormTitle').textContent = m ? '编辑机器' : '添加机器';
  $('mId').value = m ? m.id : '';
  $('mName').value = m ? m.name : '';
  $('mRole').value = m ? m.role : 'backend';
  $('mRegion').value = m ? m.region : '';
  $('mBandwidth').value = m ? m.bandwidth : '';
  $('mAddr').value = m ? (m.addr_override || '') : '';
  updateRoleHint();
  $('mStepForm').classList.remove('hidden');
  $('mStepAgent').classList.add('hidden');
  stopAgentPoll();
  agentMachine = null;
  $('modal').classList.remove('hidden');
  $('mName').focus();
}

function buildAgentCmd() {
  if (!agentMachine || !agentMachine.registration_code) return '';
  let url = $('agentPanelUrl').value.trim().replace(/\/+$/, '');
  if (url && !/^https?:\/\//.test(url)) {
    url = (DEFAULT_AGENT_URL.startsWith('https://') ? 'https://' : 'http://') + url;
  }
  if (!/^https?:\/\/[^\s'"]+$/.test(url)) return 'Agent 接入地址格式无效';
  if (url.startsWith('http://') && !ALLOW_INSECURE_AGENT) {
    return '当前仅允许 HTTPS 接入；请先为面板配置域名和 TLS。';
  }
  const q = (s) => "'" + String(s).replace(/'/g, "'\\''") + "'";
  // 全新机器可能只装了 curl 或只装了 wget：两种下载器自适应，避免接入命令跑不起来
  const fetchCmd = `command -v curl >/dev/null 2>&1 && curl -fsSL ${url}/agent/install.sh || wget -qO- ${url}/agent/install.sh`;
  const env = url.startsWith('http://') ? 'LINKPILOT_INSECURE_HTTP=1 ' : '';
  return `( ${fetchCmd} ) | ${env}bash -s -- ${q(url)} ${q(agentMachine.registration_code)}`;
}

async function issueAgentCode(m) {
  const data = await api(`/api/machines/${m.id}/registration-code`, {method: 'POST'});
  agentMachine = {...m, registration_code: data.code, agent_url: data.agent_url};
  agentAwaitingReconnect = !!m.online;
  $('agentPanelUrl').value = data.agent_url || DEFAULT_AGENT_URL;
  return agentMachine;
}

async function showAgentStep(m) {
  await issueAgentCode(m);
  $('agentStepName').textContent = m.name;
  $('mStepForm').classList.add('hidden');
  $('mStepAgent').classList.remove('hidden');
  $('agentDoneBtn').classList.add('hidden');
  $('regenBtn').classList.remove('hidden');
  $('agentWait').textContent = agentAwaitingReconnect
    ? '⏳ 旧 Agent 仍在线；在机器上执行新命令后会自动切换凭据'
    : '⏳ 等待 Agent 接入...（在机器上执行上方命令后约 3 秒上线）';
  $('agentCmd').textContent = buildAgentCmd();
  stopAgentPoll();
  agentTimer = setInterval(pollAgent, 2000);
  pollAgent();
}

function stopAgentPoll() {
  clearInterval(agentTimer);
  agentTimer = null;
}

async function pollAgent() {
  if (!agentMachine) return;
  let m;
  try { m = await api(`/api/machines/${agentMachine.id}?_=${Date.now()}`); } catch (e) { return; }
  if (agentAwaitingReconnect) {
    if (!m.online) {
      agentAwaitingReconnect = false;
      $('agentWait').textContent = '⏳ 旧 Agent 已断开，等待新凭据上线...';
    }
    return;
  }
  if (m.online) {
    $('agentWait').innerHTML = `<span style="color:var(--green)">✅ Agent 已上线（${esc(m.agent_ip || '')} ${esc(m.hostname || '')}）</span>`;
    $('agentDoneBtn').classList.remove('hidden');
    stopAgentPollKeepMachine();
    loadMachines();
  }
}
function stopAgentPollKeepMachine() {
  clearInterval(agentTimer);
  agentTimer = null;
}

function editMachine(id) { openMachineForm(MACHINES.find(x => x.id === Number(id))); }

async function showAgentCmd(id) {
  const m = MACHINES.find(x => x.id === Number(id));
  if (!m) return;
  $('mStepForm').classList.add('hidden');
  $('mStepAgent').classList.remove('hidden');
  $('modal').classList.remove('hidden');
  try {
    await showAgentStep(m);
  } catch (e) {
    errorToast('生成接入码失败: ' + e.message);
    closeModal();
  }
}

function closeModal() {
  $('modal').classList.add('hidden');
  stopAgentPoll();
  agentMachine = null;
  agentAwaitingReconnect = false;
}

async function saveMachine() {
  const body = {
    name: $('mName').value, role: $('mRole').value,
    region: $('mRegion').value, bandwidth: $('mBandwidth').value,
    addr_override: $('mAddr').value.trim(),
  };
  const id = $('mId').value;
  try {
    if (id) {
      await api(`/api/machines/${id}`, {method: 'PUT', body: JSON.stringify(body)});
      closeModal();
      await loadMachines();
      toast('已保存');
    } else {
      const m = await api('/api/machines', {method: 'POST', body: JSON.stringify(body)});
      await loadMachines();
      await showAgentStep(m);
    }
  } catch (e) {
    errorToast('保存失败: ' + e.message);
  }
}

function regenToken(anchor) {
  if (!agentMachine) return;
  const machine = agentMachine;
  showPopconfirm(anchor, {
    title: '生成新接入码？',
    message: '旧接入码立即失效，重新执行新命令后 Agent 才会切换凭据。',
    confirmText: '生成',
    errorPrefix: '生成失败',
    onConfirm: async () => {
      await issueAgentCode(machine);
      $('agentCmd').textContent = buildAgentCmd();
      $('agentWait').textContent = '⏳ 已生成新的单次接入码，旧接入码立即失效。';
      toast('新接入码已生成', 3500, 'success');
    },
  });
}

function deleteMachine(id, anchor) {
  const m = MACHINES.find(x => x.id === Number(id));
  if (!m) return;
  // 与测试记录/定时任务的删除保持一致：走 Popconfirm，而不是原生 confirm
  showPopconfirm(anchor, {
    title: '删除机器',
    message: `「${m.name}」删除后不可恢复`,
    confirmText: '删除',
    danger: true,
    errorPrefix: '删除失败',
    onConfirm: async () => {
      await api(`/api/machines/${id}`, {method: 'DELETE'});
      await loadMachines();
      toast('已删除；卸载指令将在 Agent 下次心跳时自动执行');
    },
  });
}

async function discoverMachine(id) {
  const m = MACHINES.find(x => x.id === Number(id));
  if (!m) return;
  try {
    await api(`/api/machines/${id}/discover`, {method: 'POST'});
    toast(`已向「${m.name}」派发地址探测，几秒后自动刷新`);
    setTimeout(() => loadMachines().catch(() => {}), 4000);
  } catch (e) {
    errorToast('探测失败: ' + e.message);
  }
}

/* 面板推送 Agent 更新：Agent 自己下载 → root 助手验签替换 → 服务重启 */
async function updateAgent(id, anchor) {
  const m = MACHINES.find(x => x.id === Number(id));
  if (!m) return;
  if (!m.agent_push_supported) {
    errorToast('这台机器的 Agent 太旧，无法推送更新：请点「接入」重新生成接入码并重跑一次命令');
    return;
  }
  showPopconfirm(anchor, {
    title: '推送 Agent 更新',
    message: `把「${m.name}」的 Agent 更新到面板当前版本（约 2 秒，测试中断会失败）`,
    confirmText: '推送更新',
    errorPrefix: '推送失败',
    onConfirm: async () => {
      await api(`/api/machines/${id}/update-agent`, {method: 'POST'});
      toast('已排队，Agent 会自行下载并重启', 4000, 'success');
      setTimeout(() => loadMachines().catch(() => {}), 3000);
      setTimeout(() => loadMachines().catch(() => {}), 9000);
    },
  });
}

async function updateAllAgents(anchor) {
  const outdated = MACHINES.filter(m => m.agent_outdated);
  if (!outdated.length) return toast('所有 Agent 都是最新版本');
  const pushable = outdated.filter(m => m.agent_push_supported && m.online);
  if (!pushable.length) {
    return errorToast('没有可推送的机器：都离线，或 Agent 太旧需要重跑接入命令');
  }
  showPopconfirm(anchor, {
    title: '批量更新 Agent',
    message: `可推送 ${pushable.length} 台，跳过 ${outdated.length - pushable.length} 台（离线/需重跑接入命令）`,
    confirmText: '开始更新',
    errorPrefix: '批量更新失败',
    onConfirm: async () => {
      const r = await api('/api/machines/update-all', {method: 'POST'});
      toast(`已排队 ${r.queued.length} 台；跳过 ${r.skipped.length} 台`, 4500, 'success');
      setTimeout(() => loadMachines().catch(() => {}), 4000);
      setTimeout(() => loadMachines().catch(() => {}), 12000);
    },
  });
}

async function logout() {
  try { await api('/api/logout', {method: 'POST'}); } catch (e) {}
  location.href = '/login';
}

/* ---------------- 发起测试 ---------------- */

function runPort(m) {
  // 该机器本次使用的端口：单独改过就用改的值，否则跟随上面的「默认端口」
  const v = RUN_PORTS[m.id];
  return (v === undefined || v === null || v === '') ? ($('cfgPort').value || '5201') : v;
}

function renderRunForm() {
  // 正在「发起测试」区域里输入时不要重建 DOM：每 8 秒一次的机器列表刷新
  // 会把节点换掉，导致光标丢失、输入被打断（值本身存在 RUN_PORTS 里不会丢）
  const ae = document.activeElement;
  if (ae && ae.classList && (ae.classList.contains('item-port')
      || ['cfgStreams', 'cfgDuration', 'cfgPort', 'cfgPing', 'cfgUdpBw'].includes(ae.id))) {
    return;
  }
  const targets = MACHINES.filter(m => m.role === 'target');
  const backends = MACHINES.filter(m => m.role === 'backend');
  const prevTarget = $('targetSelect').value;
  const prevChecked = runFormInit
    ? new Set([...document.querySelectorAll('#backendList input:checked')].map(x => x.value))
    : null;
  $('targetSelect').innerHTML = targets.length
    ? targets.map(m => {
        // 测试地址同样只显示 IPv4（没有 IPv4 的机器才显示 IPv6），手动指定优先
        const a4 = testAddr(m, 4);
        const a6 = testAddr(m, 6);
        const addr = a4 ? esc(a4) : (a6 ? `${esc(a6)}（仅 IPv6）` : '地址未探测');
        return `<option value="${m.id}">${esc(m.name)}（${addr} · ${esc(m.region) || '未标记'} · ${esc(m.bandwidth) || '未标记'}）</option>`;
      }).join('')
    : '<option value="">— 请先添加一台角色为「目标」的机器 —</option>';
  $('backendList').innerHTML = backends.length
    ? backends.map(m => {
        // 备注里标出 IPv6 情况（地址列不重复展示；两种状态颜色区分开）；
        // 端口默认跟目标机端口一致，只有勾了「每台机器单独指定端口」才显示输入框
        const meta = `${esc(m.region) || '未标记'} · ${esc(m.bandwidth) || '未标记'}`;
        const v6 = hasV6(m)
          ? '<span class="v6-yes">有 V6</span>'
          : `<span class="v6-no" style="cursor:help" title="${esc(m.probe_error || '本机未探测到全局 IPv6（NAT 机属正常，测试用面板观测到的出口 IP）')}">无 IPv6</span>`;
        const adv = $('cfgAdvPorts').checked
          ? `<label class="muted" style="display:flex;align-items:center;gap:4px;font-size:12px"
                    title="该机器本次连接目标机用的端口（目标机会为它额外起 server）">连接端口
               <input type="number" class="item-port" data-id="${m.id}" value="${esc(runPort(m))}"
                      min="1" max="65535" step="1" style="width:76px;padding:2px 6px">
             </label>`
          : '';
        return `<div class="chk" style="gap:8px;flex-wrap:wrap">
          <label class="chk" style="gap:6px"><input type="checkbox" value="${m.id}"> ${esc(m.name)}</label>
          <span class="muted">(${meta} · ${v6})</span>
          ${adv}
        </div>`;
      }).join('')
    : '<span class="muted">暂无后端机器</span>';
  if (runFormInit) {
    if ([...$('targetSelect').options].some(o => o.value === prevTarget)) $('targetSelect').value = prevTarget;
    document.querySelectorAll('#backendList input[type=checkbox]').forEach(cb => { cb.checked = prevChecked.has(cb.value); });
  } else {
    document.querySelectorAll('#backendList input[type=checkbox]').forEach(cb => { cb.checked = true; });
  }
  runFormInit = true;
  updateIpv6Ui();
}

function updateIpv6Ui() {
  const cb = $('useIpv6');
  const target = MACHINES.find(m => String(m.id) === $('targetSelect').value);
  const t6 = target ? testAddr(target, 6) : '';
  const t4 = target ? testAddr(target, 4) : '';
  const hints = [];
  if (!target || !t6) {
    // 目标机没有 IPv6：禁用选项并说明原因（不再静默失败）
    cb.disabled = true;
    cb.checked = false;
    hints.push(target
      ? '目标机未探测到 IPv6 地址，只能做 IPv4 测试（可点「探测地址」重试，或在机器资料里手动指定）'
      : '请先选择目标机器');
  } else {
    cb.disabled = false;
    // 目标机只有 IPv6（没有 IPv4）时自动选中，否则默认 IPv4 会直接报错
    if (!t4 && !cb.checked) {
      cb.checked = true;
      hints.push('该目标机只有 IPv6 地址，已自动选择 IPv6 测试');
    }
    hints.push(`目标机 IPv6：${t6}`);
    const checked = [...document.querySelectorAll('#backendList input[type=checkbox]')].map(x => Number(x.value));
    const no6 = MACHINES.filter(m => checked.includes(m.id) && !hasV6(m)).map(m => m.name);
    if (no6.length) hints.push(`⚠️ 以下后端机器未探测到 IPv6，IPv6 测试可能失败：${no6.join('、')}`);
  }
  $('ipv6Hint').textContent = hints.join(' · ');
}

function startRun(anchor) {
  const target_id = parseInt($('targetSelect').value, 10);
  const backend_ids = [...document.querySelectorAll('#backendList input[type=checkbox]:checked')].map(x => parseInt(x.value, 10));
  const ip_version = $('useIpv6').disabled ? 4 : ($('useIpv6').checked ? 6 : 4);
  if (!target_id) return errorToast('请先添加一台角色为「目标」的机器并接入 Agent');
  if (!backend_ids.length) return errorToast('请至少勾选一台后端机器');
  // 参数校验（服务端还会再校验一次，这里只为了少跑一趟）
  const readNum = (id, lo, hi, label) => {
    const v = parseInt($(id).value, 10);
    if (!Number.isFinite(v) || v < lo || v > hi) {
      errorToast(`${label} 需在 ${lo}–${hi} 之间`);
      return null;
    }
    return v;
  };
  const streams = readNum('cfgStreams', 1, 32, '线程数');
  const duration = readNum('cfgDuration', 1, 300, '单向时长（秒）');
  const port = readNum('cfgPort', 1, 65535, 'iperf3 端口');
  const ping_count = readNum('cfgPing', 1, 2000, 'ping 次数');
  if (streams === null || duration === null || port === null || ping_count === null) return;
  const udp = $('cfgUdp').checked;
  const udp_bandwidth = ($('cfgUdpBw').value || '').trim().toUpperCase() || '100M';
  if (udp && !/^\d{1,5}(\.\d{1,3})?[KMG]$/.test(udp_bandwidth)) {
    return errorToast('UDP 目标带宽必须带单位（示例：100M、1G、500K）');
  }
  // 每台后端机的端口：没单独改过的用默认端口（不提交，后端按目标机端口落库）
  const advPorts = $('cfgAdvPorts').checked;
  const ports = {};
  if (advPorts) {
    for (const id of backend_ids) {
      const raw = RUN_PORTS[id];
      if (raw === undefined || raw === null || raw === '') continue;
      const p = parseInt(raw, 10);
      if (!Number.isFinite(p) || p < 1 || p > 65535) {
        return errorToast(`机器端口需在 1–65535 之间（收到 ${raw}）`);
      }
      ports[id] = p;
    }
  }
  const proto = ip_version === 6 ? 'IPv6' : 'IPv4';
  // 把实际要用的目标地址与端口写进确认框，地址取错能立刻看出来
  const tgt = MACHINES.find(m => String(m.id) === String(target_id)) || {};
  const tgtAddr = testAddr(tgt, ip_version) || '地址未探测';
  const message = `目标 ${tgt.region || tgt.name || target_id} · ${tgtAddr}:${port} · 后端 ${backend_ids.length} 台`;
  showPopconfirm(anchor, {
    title: `开始 ${proto} 线路测试？`,
    message,
    confirmText: '开始测试',
    busyText: '启动中…',
    errorPrefix: '启动失败',
    onConfirm: async () => {
      const r = await api('/api/runs', {method: 'POST', body: JSON.stringify({
        target_id, backend_ids, ip_version, streams, duration, port,
        udp, udp_bandwidth, ping_count, ports,
      })});
      await loadRuns();
      openRun(r.run_id);
      toast('测试已启动');
    },
  });
}

/* ---------------- 测试记录 ---------------- */

/* 测试记录渲染：纯本地，按 RUNS_FILTER 过滤 ALL_RUNS（切筛选零请求、零联动重绘） */
function renderRuns() {
  const tb = $('runsBody');
  if (!tb) return;
  const isSched = (r) => r.source === 'schedule' || r.schedule_id;
  let runs = ALL_RUNS;
  if (RUNS_FILTER === 'manual') runs = runs.filter(r => !isSched(r));
  else if (RUNS_FILTER === 'schedule') runs = runs.filter(isSched);
  if (!runs.length) {
    const emptyCfg = {
      manual: ['还没有手动测试', '选好目标机与后端机，发起第一轮线路测试',
        '<button class="small primary" data-action="gotoRun">⚡ 去发起测试</button>'],
      schedule: ['暂无定时任务记录', '在「任务」页开启定时任务后，逐轮结果会出现在这里', ''],
      all: ['暂无测试记录', '发起一轮测试后，结果会汇总在这里', ''],
    }[RUNS_FILTER] || ['暂无测试记录', '', ''];
    tb.innerHTML = emptyState(EMPTY_ICONS.runs, emptyCfg[0], emptyCfg[1], emptyCfg[2]);
    return;
  }
  const groups = new Map();
  const entries = [];
  for (const r of runs) {
    if (r.source === 'schedule' || r.schedule_id) {
      const sid = Number(r.schedule_id || 0);
      if (!groups.has(sid)) groups.set(sid, []);
      groups.get(sid).push(r);
    } else {
      entries.push({kind: 'run', at: r.created_at, rid: r.id, run: r});
    }
  }
  for (const [sid, groupRuns] of groups) {
    entries.push({kind: 'group', at: groupRuns[0].created_at, rid: groupRuns[0].id,
                  sid, runs: groupRuns});
  }
  entries.sort((a, b) => String(b.at).localeCompare(String(a.at)) || b.rid - a.rid);

  const runRow = (r, child) => {
    const src = `<span class="badge scheduled">定时</span>`;
    return `<tr class="${child ? 'run-child' : ''}">
      <td>#${r.id}</td>
      <td>${child ? src : '<span class="badge backend">手动</span>'}</td>
      <td>${esc(r.target_name)} <span class="muted">${r.ip_version === 6 ? 'IPv6' : 'IPv4'} · `
        + `${r.udp ? 'UDP' : 'TCP'} · ${r.streams}线程/${r.duration}s · :${r.port}</span></td>
      <td>${r.done_count}/${r.backend_total}${r.fail_count ? ` · <span style="color:var(--red)">${r.fail_count} 失败</span>` : ''}</td>
      <td><span class="badge ${r.status}">${statusText(r.status)}</span></td>
      <td class="muted">${r.created_at}</td>
      <td class="run-actions"><div class="actions">
        <button class="small" data-action="openRun" data-id="${r.id}">查看</button>
        <button class="small danger" data-action="removeRun" data-id="${r.id}">删除</button>
      </div></td>
    </tr>`;
  };

  tb.innerHTML = entries.map(entry => {
    if (entry.kind === 'run') return runRow(entry.run, false);
    const open = EXPANDED_RUN_GROUPS.has(entry.sid);
    const latest = entry.runs[0];
    const running = entry.runs.find(r => r.status === 'running' || r.status === 'pending');
    const status = running ? running.status : latest.status;
    const failed = entry.runs.filter(r => ['failed', 'partial', 'interrupted'].includes(r.status)).length;
    const name = latest.schedule_name || `定时任务 #${entry.sid || '?'}`;
    const group = `<tr class="run-group" data-action="toggleRunGroup" data-id="${entry.sid}">
      <td colspan="7"><div class="run-group-main">
        <button class="small group-toggle ${open ? 'open' : ''}" data-action="toggleRunGroup" data-id="${entry.sid}" title="${open ? '收起' : '展开'}">›</button>
        <span class="badge scheduled">定时任务</span>
        <b>${esc(name)}</b>
        <span class="muted">${entry.runs.length} 次记录${failed ? ` · ${failed} 次失败` : ''}</span>
        <span class="badge ${status}">${statusText(status)}</span>
        <span class="muted">最近 ${latest.created_at}</span>
        <span style="flex:1"></span>
        <button class="small" data-action="compareSchedule" data-id="${entry.sid}">长期对比</button>
      </div></td>
    </tr>`;
    const children = open ? entry.runs.map(r => runRow(r, true)).join('') : '';
    return group + children;
  }).join('');
}

async function loadRuns() {
  let runs = [];
  try { runs = await api('/api/runs'); } catch (e) { return; }
  const sig = JSON.stringify(runs);
  if (sig === LAST_RUNS_SIG) return;
  LAST_RUNS_SIG = sig;
  ALL_RUNS = runs;
  setStat('statRuns', runs.length);
  const byDay = {};
  runs.forEach(r => { const d = String(r.created_at || '').slice(0, 10); if (d) byDay[d] = (byDay[d] || 0) + 1; });
  drawSpark('sparkStatRuns', Object.keys(byDay).sort().slice(-8).map(d => byDay[d]));
  renderRuns();
  renderHome();
}

function setRunsFilter(filter) {
  RUNS_FILTER = ['manual', 'schedule', 'all'].includes(filter) ? filter : 'manual';
  document.querySelectorAll('#runsFilter button').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.filter === RUNS_FILTER);
  });
  // 纯本地过滤渲染：不发请求、不重建总览，切换即时完成
  if (ALL_RUNS.length) renderRuns();
  else loadRuns();
}

function toggleRunGroup(sid) {
  sid = Number(sid);
  if (EXPANDED_RUN_GROUPS.has(sid)) EXPANDED_RUN_GROUPS.delete(sid);
  else EXPANDED_RUN_GROUPS.add(sid);
  renderRuns();
}

const EVENT_STAGE_LABELS = {
  precheck: '预检', ping: 'Ping 测试', uplink: '上行测试',
  downlink: '下行测试', analysis: '结果分析', complete: '测试完成',
};
const EVENT_STATUS_LABELS = {
  running: '进行中', done: '完成', warning: '需关注',
  failed: '失败', stopped: '已停止', partial: '部分失败',
};

function renderRunSummary(summary) {
  const box = $('runSummary');
  if (!box) return;
  if (!summary) {
    box.classList.add('hidden');
    return;
  }
  box.innerHTML = `
    <div><b>${esc(summary.target_name || '—')}</b><span>目标机</span></div>
    <div><b>${summary.backend_total || 0}</b><span>后端总数</span></div>
    <div><b>${summary.success_count || 0}</b><span>成功</span></div>
    <div><b>${summary.failure_count || 0}</b><span>失败</span></div>
    <div><b>${summary.attention_count || 0}</b><span>需关注</span></div>
    <div class="summary-status"><b>${esc(summary.overall_label || '—')}</b><span>整体状态</span></div>`;
  box.classList.remove('hidden');
}

function renderRunEvents(events) {
  const box = $('eventStream');
  if (!box) return;
  if (!events || !events.length) {
    box.innerHTML = '<div class="muted" style="font-size:12px;padding:8px 0">暂无结构化事件，可展开下方原始日志查看完整过程。</div>';
    return;
  }
  const groups = new Map();
  for (const event of events) {
    const stage = event.stage || 'complete';
    if (!groups.has(stage)) groups.set(stage, []);
    groups.get(stage).push(event);
  }
  const order = ['precheck', 'ping', 'uplink', 'downlink', 'analysis', 'complete'];
  const stages = [...groups.keys()].sort((a, b) => {
    const ai = order.indexOf(a), bi = order.indexOf(b);
    return (ai < 0 ? 99 : ai) - (bi < 0 ? 99 : bi);
  });
  box.innerHTML = stages.map(stage => `<div class="event-stage">── ${esc(EVENT_STAGE_LABELS[stage] || stage)} ──</div>`
    + groups.get(stage).map(event => `<div class="event-line ${esc(event.status || 'done')}">
        <span class="event-time">[${esc(event.time || '')}]</span>
        <span class="event-title">${esc(event.title || '')}</span>
        ${event.summary ? `<span class="event-summary">${esc(event.summary)}</span>` : ''}
      </div>`).join('')
  ).join('');
}

function openRun(id) {
  CURRENT_RUN = Number(id);
  document.body.classList.add('app-detail-open');
  $('runDetail').classList.remove('hidden');
  $('runDetail').scrollIntoView({behavior: 'smooth'});
  $('runDetailTitle').textContent = `测试 #${CURRENT_RUN}`;
  $('runDetailMeta').textContent = '正在加载测试记录…';
  $('logPre').textContent = '';
  $('eventStream').innerHTML = '';
  renderRunSummary(null);
  $('detailTableBody').innerHTML = '<tr><td colspan="10" class="muted">加载中…</td></tr>';
  $('rawWrap').innerHTML = '';
  $('reportPre').textContent = '';
  clearTimeout(pollTimer);
  pollRun();
  schedulePoll();
}

function schedulePoll() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    if (!CURRENT_RUN) return;
    const d = await pollRun();
    if (d && (d.run.status === 'running' || d.run.status === 'pending')) {
      schedulePoll();
    } else {
      loadRuns();
    }
  }, 2000);
}

async function pollRun() {
  if (!CURRENT_RUN) return null;
  let d;
  try { d = await api(`/api/runs/${CURRENT_RUN}`); } catch (e) {
    $('runDetailMeta').textContent = '加载失败: ' + e.message;
    toast('测试记录加载失败: ' + e.message);
    return null;
  }
  const run = d.run;
  $('runDetailTitle').innerHTML = `测试 #${run.id} <span class="badge ${run.status}">${statusText(run.status)}</span>`;
  $('runDetailMeta').textContent =
    `目标: ${run.target_name} (${run.target_host}) · 协议: ${run.ip_version === 6 ? 'IPv6' : 'IPv4'}`
    + ` · ${run.udp ? `UDP -b ${run.udp_bandwidth}/流` : 'TCP'} ${run.streams} 线程`
    + ` · 上/下行各 ${run.duration} 秒 · 端口 ${run.port}`
    + ((d.items || []).some(it => it.port && it.port !== run.port) ? '（部分机器连接端口不同，见下表）' : '')
    + ` · ping ${run.ping_count} 次`
    + ((run.source === 'schedule' || run.schedule_id) ? ` · 来源: 定时任务 #${run.schedule_id || '?'}` : ' · 来源: 手动')
    + ` · 开始: ${run.created_at}`
    + (run.finished_at ? ` · 结束: ${run.finished_at}` : '');
  const pre = $('logPre');
  const autoScroll = $('autoScroll') && $('autoScroll').checked;
  const lines = d.log || [];
  pre.innerHTML = lines.length
    ? lines.map(l => {
        const e = esc(l);
        if (l.includes('✅')) return `<div class="log-line log-ok">${e}</div>`;
        if (l.includes('❌') || l.includes('[致命错误]')) return `<div class="log-line log-bad">${e}</div>`;
        if (l.includes('===')) return `<div class="log-line log-hl">${e}</div>`;
        return `<div class="log-line">${e}</div>`;
      }).join('')
    : '（暂无日志）';
  renderRunEvents(d.events || []);
  renderRunSummary(d.summary);
  if (autoScroll) {
    pre.scrollTop = pre.scrollHeight;
    $('eventStream').scrollTop = $('eventStream').scrollHeight;
  }
  renderRunDetail(d);
  return d;
}

function renderRunDetail(d) {
  const udp = !!d.run.udp;
  // UDP 没有重传，那一列改成 UDP 丢包（上/下）
  if ($('thRetr')) $('thRetr').textContent = udp ? 'UDP 丢包 上/下' : '重传';
  const rows = d.items.map((it, i) => {
    const region = esc(it.machine_region) || '—';
    const bw = esc(it.machine_bandwidth) || '—';
    const port = it.port && d.run.port && it.port !== d.run.port
      ? ` <span class="muted">:${it.port}</span>` : '';
    const name = `后端机器${i + 1}：${esc(it.machine_name)}${port}`;
    if (it.status === 'done' && it.metrics_obj) {
      const mt = it.metrics_obj;
      const last = udp
        ? `${fmt(mt.up_udp_loss_pct, '%')} / ${fmt(mt.down_udp_loss_pct, '%')}`
        : (mt.retr_total ?? '—');
      return `<tr>
        <td>${name}</td><td>${region}</td><td>${bw}</td>
        <td>${fmt(mt.loss_pct, '%')}</td>
        <td>${fmt(mt.rtt_avg, ' ms')}</td>
        <td>${mt.mdev_ms === null || mt.mdev_ms === undefined ? '—' : mt.mdev_ms + ' ms'}</td>
        <td>${fmt(mt.up_mbits, ' Mbit/s')}</td>
        <td>${fmt(mt.down_mbits, ' Mbit/s')}</td>
        <td>${last}</td>
        <td>${ratingCellHtml(mt, it.status, it.error)}</td>
        <td>${ratingExplanationHtml(mt, it.status, it.error)}</td>
      </tr>`;
    }
    if (it.status === 'running') {
      return `<tr>
        <td>${name}</td><td>${region}</td><td>${bw}</td>
        <td colspan="6"><span class="badge running">⏳ ${esc(it.phase || '执行中')}…</span></td>
        <td class="muted">—</td><td class="muted">等待完成</td>
      </tr>`;
    }
    return `<tr>
      <td>${name}</td><td>${region}</td><td>${bw}</td>
      <td colspan="6" class="muted">—</td>
      <td>${ratingCellHtml(it.metrics_obj, it.status, it.error)}</td>
      <td>${ratingExplanationHtml(it.metrics_obj, it.status, it.error)}</td>
    </tr>`;
  }).join('');
  $('detailTableBody').innerHTML = rows || '<tr><td colspan="10" class="muted">等待执行...</td></tr>';

  $('rawWrap').innerHTML = d.items.map((it, i) => {
    const mt = it.metrics_obj || {};
    let content;
    if (it.status === 'done') {
      content = [mt.ping_block, mt.up_block, mt.down_block].filter(Boolean).join('\n\n') || '（无输出）';
    } else {
      content = ['测试失败: ' + (it.error || it.status), it.up_raw, it.down_raw]
        .filter(Boolean).join('\n\n');
    }
    return `<details>
      <summary>后端机器${i + 1}：${esc(it.machine_name)}（${esc(it.machine_region) || '未标记'} · ${esc(it.machine_bandwidth) || '未标记'} · ${esc(it.machine_host)}）</summary>
      <pre>${esc(content)}</pre>
    </details>`;
  }).join('');

  $('reportPre').textContent = d.run.report ||
    (d.run.status === 'running' ? '（测试结束后自动生成）' : '（无报告）');
  $('reportRender').classList.add('hidden');
  $('reportRender').innerHTML = '';
  $('reportToggleBtn').textContent = '👁 渲染视图';
}

function copyReport() {
  const text = $('reportPre').textContent;
  if (!text || text.startsWith('（')) return errorToast('报告还未生成');
  copyText(text, '已复制 Markdown 报告');
}

/* --- 轻量 Markdown 渲染（先转义再转换，杜绝注入） --- */
function mdInline(s) {
  return esc(s)
    .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
    .replace(/`([^`]+)`/g, '<code>$1</code>');
}

function mdFlushTable(rows) {
  const parsed = rows
    .map(r => r.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim()))
    .filter(cells => !cells.every(c => /^:?-{2,}:?$/.test(c) || c === ''));
  if (!parsed.length) return '';
  const head = parsed[0];
  let h = '<table class="md-table"><thead><tr>' + head.map(c => '<th>' + mdInline(c) + '</th>').join('') + '</tr></thead><tbody>';
  for (const row of parsed.slice(1)) h += '<tr>' + row.map(c => '<td>' + mdInline(c) + '</td>').join('') + '</tr>';
  return h + '</tbody></table>';
}

function renderMarkdown(src) {
  const parts = src.split('```');
  let html = '';
  let tableRows = [];
  const flush = () => { const t = mdFlushTable(tableRows); tableRows = []; return t; };
  for (let i = 0; i < parts.length; i++) {
    if (i % 2 === 1) {
      html += '<pre>' + esc(parts[i].replace(/^\n/, '')) + '</pre>';
      continue;
    }
    for (const line of parts[i].split('\n')) {
      if (/^\s*\|.*\|\s*$/.test(line)) { tableRows.push(line); continue; }
      if (tableRows.length) html += flush();
      if (/^### /.test(line)) html += `<h4>${mdInline(line.slice(4))}</h4>`;
      else if (/^## /.test(line)) html += `<h3>${mdInline(line.slice(3))}</h3>`;
      else if (/^# /.test(line)) html += `<h2>${mdInline(line.slice(2))}</h2>`;
      else if (/^- /.test(line)) html += `<p>• ${mdInline(line.slice(2))}</p>`;
      else if (/^> /.test(line)) html += `<blockquote>${mdInline(line.replace(/^>\s?/, ''))}</blockquote>`;
      else if (line.trim() === '') continue;
      else html += `<p>${mdInline(line)}</p>`;
    }
  }
  if (tableRows.length) html += flush();
  return html;
}

let THEME_TRANSITIONING = false;
const THEME_PRESS_ORIGIN = {x: 0, y: 0, t: 0};

function rememberThemePress(event) {
  const button = event.target && event.target.closest &&
    event.target.closest('#themeBtn');
  if (!button) return;
  const point = event.touches ? event.touches[0] : event;
  if (!point) return;
  THEME_PRESS_ORIGIN.x = point.clientX;
  THEME_PRESS_ORIGIN.y = point.clientY;
  THEME_PRESS_ORIGIN.t = Date.now();
}
document.addEventListener('pointerdown', rememberThemePress, {capture: true, passive: true});
document.addEventListener('touchstart', rememberThemePress, {capture: true, passive: true});

function revealThemeWithSnapshot(nextTheme, origin, finish) {
  const overlay = document.createElement('div');
  const page = document.createElement('div');
  overlay.className = 'theme-reveal-overlay';
  overlay.setAttribute('aria-hidden', 'true');
  overlay.inert = true;
  page.className = `theme-reveal-page ${document.body.className}`.trim();
  Object.entries(document.body.dataset).forEach(([key, value]) => {
    page.dataset[key] = value;
  });
  page.dataset.theme = nextTheme;

  // 读取目标主题的计算变量，仅用于这一帧的视觉副本；不改变当前页面状态。
  const previousTheme = document.body.dataset.theme;
  document.body.dataset.theme = nextTheme;
  const themed = getComputedStyle(document.body);
  const variables = {};
  for (let i = 0; i < themed.length; i += 1) {
    const name = themed[i];
    if (name.startsWith('--')) variables[name] = themed.getPropertyValue(name);
  }
  if (previousTheme) document.body.dataset.theme = previousTheme;
  else document.body.removeAttribute('data-theme');
  Object.entries(variables).forEach(([name, value]) => overlay.style.setProperty(name, value));
  overlay.style.colorScheme = nextTheme;

  [...document.body.children].forEach(child => {
    if (child.tagName === 'SCRIPT') return;
    page.appendChild(child.cloneNode(true));
  });
  page.querySelectorAll('script').forEach(node => node.remove());
  // cloneNode 不复制用户当前输入值，单独同步，避免揭示时看到旧表单内容。
  const sourceFields = document.body.querySelectorAll('input,textarea,select');
  const clonedFields = page.querySelectorAll('input,textarea,select');
  sourceFields.forEach((source, index) => {
    const cloned = clonedFields[index];
    if (!cloned) return;
    if ('value' in cloned) cloned.value = source.value;
    if (cloned.type === 'checkbox' || cloned.type === 'radio') cloned.checked = source.checked;
    if (cloned.tagName === 'SELECT') cloned.selectedIndex = source.selectedIndex;
  });
  ['#eventStream', '.md-render', '.tbl-wrap', '#chartWrap', 'pre'].forEach(selector => {
    const sourceNodes = document.body.querySelectorAll(selector);
    const clonedNodes = page.querySelectorAll(selector);
    sourceNodes.forEach((source, index) => {
      if (!clonedNodes[index]) return;
      clonedNodes[index].scrollTop = source.scrollTop;
      clonedNodes[index].scrollLeft = source.scrollLeft;
    });
  });
  const clonedLabel = page.querySelector('#themeLabel');
  const clonedButton = page.querySelector('#themeBtn');
  const nextLabel = nextTheme === 'light' ? '深色' : '浅色';
  const nextAction = nextTheme === 'light' ? '切换到深色模式' : '切换到浅色模式';
  if (clonedLabel) clonedLabel.textContent = nextLabel;
  if (clonedButton) {
    clonedButton.setAttribute('aria-label', nextAction);
    clonedButton.setAttribute('title', nextAction);
  }

  overlay.appendChild(page);
  ['pointerdown', 'touchstart', 'click', 'contextmenu'].forEach(type => {
    overlay.addEventListener(type, event => {
      event.preventDefault();
      event.stopImmediatePropagation();
    }, {capture: true, passive: false});
  });
  document.body.appendChild(overlay);
  overlay.scrollTop = window.scrollY;
  const x = Math.max(0, Math.min(window.innerWidth, origin.x));
  const y = Math.max(0, Math.min(window.innerHeight, origin.y));
  const radius = Math.hypot(Math.max(x, window.innerWidth - x),
    Math.max(y, window.innerHeight - y)) + 48;
  let animation;
  try {
    animation = overlay.animate([
      {clipPath: `circle(0px at ${x}px ${y}px)`},
      {clipPath: `circle(${radius}px at ${x}px ${y}px)`},
    ], {
      duration: 580,
      easing: 'cubic-bezier(.2,.72,.25,1)',
      fill: 'forwards',
    });
  } catch (e) {
    finish();
    overlay.remove();
    THEME_TRANSITIONING = false;
    const button = $('themeBtn');
    if (button) button.removeAttribute('aria-busy');
    return;
  }
  animation.finished.catch(() => {}).finally(() => {
    finish();
    overlay.remove();
    THEME_TRANSITIONING = false;
    const button = $('themeBtn');
    if (button) button.removeAttribute('aria-busy');
  });
}

function applyTheme(t, origin = null) {
  t = t === 'dark' ? 'dark' : 'light';
  const change = () => {
    document.body.dataset.theme = t;
    try { localStorage.setItem('fleet-theme', t); } catch (e) {}
    const label = t === 'light' ? '深色' : '浅色';
    const action = t === 'light' ? '切换到深色模式' : '切换到浅色模式';
    const labelNode = $('themeLabel');
    const button = $('themeBtn');
    if (labelNode) labelNode.textContent = label;
    if (button) {
      button.setAttribute('aria-label', action);
      button.setAttribute('title', action);
    }
    // 手机状态栏/浏览器 chrome 跟随主题色
    const meta = $('metaTheme');
    if (meta) meta.setAttribute('content', t === 'dark' ? '#0b0b12' : '#f7f8fc');
  };
  const reduced = window.matchMedia &&
    window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (THEME_TRANSITIONING) return;
  // 桌面端直接切换，不创建任何覆盖层；圆形揭示只在手机端保留。
  if (!origin || reduced || window.innerWidth > 720) {
    change();
    return;
  }
  if (!document.startViewTransition) {
    THEME_TRANSITIONING = true;
    const button = $('themeBtn');
    if (button) button.setAttribute('aria-busy', 'true');
    revealThemeWithSnapshot(t, origin, change);
    return;
  }
  const x = Math.max(0, Math.min(window.innerWidth, origin.x));
  const y = Math.max(0, Math.min(window.innerHeight, origin.y));
  const radius = Math.hypot(Math.max(x, window.innerWidth - x),
    Math.max(y, window.innerHeight - y)) + 48;
  const root = document.documentElement;
  root.style.setProperty('--theme-origin-x', `${x}px`);
  root.style.setProperty('--theme-origin-y', `${y}px`);
  root.style.setProperty('--theme-radius', `${radius}px`);
  THEME_TRANSITIONING = true;
  const button = $('themeBtn');
  if (button) button.setAttribute('aria-busy', 'true');
  let transition;
  try {
    transition = document.startViewTransition(change);
  } catch (e) {
    THEME_TRANSITIONING = false;
    if (button) button.removeAttribute('aria-busy');
    change();
    return;
  }
  transition.finished.catch(() => {}).finally(() => {
    THEME_TRANSITIONING = false;
    if (button) button.removeAttribute('aria-busy');
  });
}

function toggleTheme(anchor) {
  const next = document.body.dataset.theme === 'dark' ? 'light' : 'dark';
  let origin = {x: window.innerWidth / 2, y: window.innerHeight / 2};
  if (anchor) {
    const rect = anchor.getBoundingClientRect();
    origin = {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2};
    if (Date.now() - THEME_PRESS_ORIGIN.t < 2000) {
      origin = {x: THEME_PRESS_ORIGIN.x, y: THEME_PRESS_ORIGIN.y};
      THEME_PRESS_ORIGIN.t = 0;
    } else if (LAST_CLICK && Date.now() - LAST_CLICK.t < 1200 &&
        LAST_CLICK.x >= rect.left && LAST_CLICK.x <= rect.right &&
        LAST_CLICK.y >= rect.top && LAST_CLICK.y <= rect.bottom) {
      origin = {x: LAST_CLICK.x, y: LAST_CLICK.y};
    }
  }
  applyTheme(next, origin);
}

function toggleReportView() {
  const pre = $('reportPre'), div = $('reportRender'), btn = $('reportToggleBtn');
  if (pre.classList.contains('hidden')) {
    div.classList.add('hidden');
    pre.classList.remove('hidden');
    btn.textContent = '👁 渲染视图';
    return;
  }
  div.innerHTML = renderMarkdown(pre.textContent);
  div.classList.remove('hidden');
  pre.classList.add('hidden');
  btn.textContent = '📄 源码';
}

function downloadReport() {
  if (CURRENT_RUN) window.open(`/api/runs/${CURRENT_RUN}/report`);
}

function stopCurrentRun(anchor) {
  if (!CURRENT_RUN) return;
  const runId = CURRENT_RUN;
  showPopconfirm(anchor, {
    title: '停止当前测试？',
    message: '已完成的单机结果会保留，未完成部分记为停止。',
    confirmText: '停止',
    danger: true,
    busyText: '停止中…',
    errorPrefix: '停止失败',
    onConfirm: async () => {
      await api(`/api/runs/${runId}/stop`, {method: 'POST'});
      toast('停止指令已发送，正在收尾...');
    },
  });
}

let activePopconfirm = null;

function closePopconfirm() {
  if (!activePopconfirm) return;
  activePopconfirm.bubble.remove();
  document.removeEventListener('click', activePopconfirm.outside, true);
  window.removeEventListener('resize', activePopconfirm.close);
  window.removeEventListener('scroll', activePopconfirm.close, true);
  activePopconfirm = null;
}

function showPopconfirm(anchor, {
  title, message, confirmText = '确认', onConfirm,
  danger = false, errorPrefix = '操作失败', busyText = '处理中…',
}) {
  closePopconfirm();
  if (!anchor) return;
  const bubble = document.createElement('div');
  bubble.className = 'popconfirm';
  bubble.innerHTML = `<h4>${esc(title)}</h4>${message ? `<p>${esc(message)}</p>` : ''}`
    + `<div class="actions"><button class="small" data-pop="cancel">取消</button>`
    + `<button class="small ${danger ? 'danger' : 'primary'}" data-pop="confirm">${esc(confirmText)}</button></div>`;
  document.body.appendChild(bubble);

  const rect = anchor.getBoundingClientRect();
  const box = bubble.getBoundingClientRect();
  // 确认键尽量贴着点击落点：气泡右下角（确认键位置）对准光标；
  // 键盘等无落点场景退回到按钮右缘对齐
  const c = LAST_CLICK && Date.now() - LAST_CLICK.t < 1500
    && LAST_CLICK.x >= rect.left - 8 && LAST_CLICK.x <= rect.right + 8
    && LAST_CLICK.y >= rect.top - 8 && LAST_CLICK.y <= rect.bottom + 8
    ? LAST_CLICK : null;
  let left, top;
  if (c) {
    left = c.x - box.width + 59;
    top = c.y - box.height + 38;
  } else {
    left = rect.right - box.width;
    top = rect.bottom + 6;
  }
  left = Math.min(window.innerWidth - box.width - 8, Math.max(8, left));
  top = Math.min(window.innerHeight - box.height - 8, Math.max(8, top));
  bubble.style.left = left + 'px';
  bubble.style.top = top + 'px';

  const outside = (event) => {
    if (!bubble.contains(event.target)) closePopconfirm();
  };
  const close = () => closePopconfirm();
  document.addEventListener('click', outside, true);
  window.addEventListener('resize', close);
  window.addEventListener('scroll', close, true);
  activePopconfirm = {bubble, outside, close};

  bubble.querySelector('[data-pop="cancel"]').addEventListener('click', closePopconfirm);
  bubble.querySelector('[data-pop="confirm"]').addEventListener('click', async (event) => {
    const btn = event.currentTarget;
    buzz(8);
    btn.disabled = true;
    btn.textContent = busyText;
    try {
      await onConfirm();
      closePopconfirm();
    } catch (error) {
      btn.disabled = false;
      btn.textContent = confirmText;
      toast(`${errorPrefix}：` + error.message);
    }
  });
}

function removeRun(id, anchor) {
  showPopconfirm(anchor, {
    title: '删除测试记录',
    message: '该轮结果与原始数据将无法恢复',
    confirmText: '删除',
    danger: true,
    errorPrefix: '删除失败',
    onConfirm: async () => {
      await api(`/api/runs/${id}`, {method: 'DELETE'});
      if (CURRENT_RUN === Number(id)) {
        CURRENT_RUN = null;
        $('runDetail').classList.add('hidden');
      }
      await loadRuns();
      toast('测试记录已删除');
    },
  });
}

/* ---------------- 定时任务（长期对比） ---------------- */

let SCHEDULES = [];
let CURRENT_SCHED = null;
let CURRENT_COMPARISON = null;
let CHART_STATE = {mode: 'multi', direction: 'both', selected: new Set(), scheduleId: null};
let COMPARE_ABORT = null;

function intervalSeconds() {
  const v = parseInt($('sInterval').value, 10);
  const unit = parseInt($('sIntervalUnit').value, 10) || 3600;
  if (!Number.isFinite(v) || v < 1) return null;
  return v * unit;
}

function lifetimeSeconds() {
  if (!$('sLifetimeEnabled').checked) return 0;
  const v = parseInt($('sLifetimeValue').value, 10);
  const unit = parseInt($('sLifetimeUnit').value, 10) || 86400;
  if (!Number.isFinite(v) || v < 1) return null;
  return v * unit;
}

function syncUdpControls(checkId, bandwidthId) {
  const on = $(checkId).checked;
  const bandwidth = $(bandwidthId);
  bandwidth.disabled = !on;
  bandwidth.style.opacity = on ? '1' : '.55';
  if (bandwidth.closest('.field')) bandwidth.closest('.field').classList.toggle('hidden', !on);
}

function syncLifetimeControls() {
  const on = $('sLifetimeEnabled').checked;
  $('sLifetimeRow').classList.toggle('is-off', !on);
  $('sLifetimeValue').disabled = !on;
  $('sLifetimeUnit').disabled = !on;
}

function humanDuration(sec) {
  sec = Number(sec) || 0;
  // 用不换行空格：表格窄列里「5 分钟」不该被拆成两行
  if (sec % 86400 === 0) return `${sec / 86400}\u00a0天`;
  if (sec % 3600 === 0) return `${sec / 3600}\u00a0小时`;
  if (sec % 60 === 0) return `${sec / 60}\u00a0分钟`;
  return `${sec}\u00a0秒`;
}

function schedStatusText(s) {
  return {
    running: '执行中', finished: '正常', failed: '失败', partial: '部分失败',
    stopped: '已停止', skipped: '本轮跳过', interrupted: '已中断', waiting: '等待空档',
    expired: '已到期',
  }[s] || s || '待执行';
}

async function loadSchedules() {
  const list = await api('/api/schedules?_=' + Date.now());
  const sig = JSON.stringify(list);
  if (sig === LAST_SCHEDULES_SIG) return;
  LAST_SCHEDULES_SIG = sig;
  SCHEDULES = list;
  const enabledCount = SCHEDULES.filter(s => s.enabled).length;
  setStat('statSchedules', enabledCount);
  pushSpark('sparkStatSchedules', enabledCount);
  renderSchedules();
  renderHome();
}

function renderSchedules() {
  const tb = $('schedulesBody');
  if (!tb) return;
  if (!SCHEDULES.length) {
    tb.innerHTML = emptyState(EMPTY_ICONS.clock, '暂无定时任务', '挂一个长期对比，吞吐和丢包趋势会自动沉淀',
      '<button class="small primary" data-action="openScheduleForm">＋ 新建定时任务</button>');
    return;
  }
  tb.innerHTML = SCHEDULES.map(s => {
    const expired = !!s.expired || s.last_status === 'expired';
    const st = expired ? 'expired' : (s.enabled ? (s.last_status || '') : 'stopped');
    const badge = expired
      ? '<span class="badge expired">已到期</span>'
      : s.enabled
      ? `<span class="badge ${st === 'finished' ? 'done' : st}">${schedStatusText(st)}</span>`
      : '<span class="badge stopped">已停用</span>';
    const okCount = Math.max(0, s.run_count - s.fail_count);
    const params = `${s.ip_version === 6 ? 'IPv6' : 'IPv4'} · ${s.udp ? `UDP -b ${esc(s.udp_bandwidth)}` : 'TCP'}`
      + ` -P ${s.streams} -t ${s.duration}s · 端口 ${s.port} · ping ${s.ping_count}`;
    const next = expired
      ? '<span class="muted">运行时长已结束</span>'
      : s.enabled
      ? (s.next_run_in ? `${esc(s.next_run_in)}` : '—') + `<br><span class="muted" style="font-size:11px">${esc(s.next_run_at || '')}</span>`
      : '<span class="muted">已停用</span>';
    const lifetime = `运行 ${esc(s.lifetime_text || '不限')}`
      + (s.ends_at ? `<br><span class="muted" style="font-size:11px">至 ${esc(s.ends_at)}</span>` : '');
    const last = s.last_error
      ? `<span style="color:var(--orange)" title="${esc(s.last_error)}">${esc(s.last_error.slice(0, 28))}${s.last_error.length > 28 ? '…' : ''}</span>`
      : (s.last_run_at ? `<span class="muted">${esc(s.last_run_at)}</span>` : '<span class="muted">尚未执行</span>');
    const open = EXPANDED_SCHED_CARDS.has(s.id);
    return `<tr class="sched-card ${open ? 'sched-open' : ''}" data-action="toggleSchedCard" data-id="${s.id}">
      <td>${badge}</td>
      <td class="sched-name"><button class="small sched-chev ${open ? 'open' : ''}" data-action="toggleSchedCard" data-id="${s.id}" title="${open ? '收起详情' : '展开详情'}">›</button>${esc(s.name)}<br><span class="muted" style="font-size:11px">#${s.id}</span></td>
      <td class="sched-x" data-label="目标">${esc(s.target_name)} <span class="muted">←</span> ${s.backend_names.map(esc).join('、') || '—'}
        <br><span class="muted" style="font-size:11px">${s.backend_count} 台后端</span></td>
      <td class="sched-x muted" data-label="参数" style="font-size:12px">${params}</td>
      <td class="sched-x" data-label="周期">${esc(s.interval_text)}<br><span class="muted" style="font-size:11px">${lifetime}</span></td>
      <td class="sched-always" data-label="下次">${next}</td>
      <td class="sched-x" data-label="累计">${okCount} / ${s.fail_count} / ${s.skip_count}
        <br><span class="muted" style="font-size:11px">共 ${s.run_count} 轮完成</span></td>
      <td class="sched-x" data-label="最近" style="font-size:12px">${last}</td>
      <td class="sched-actions"><div class="actions">
        <button class="small" data-action="compareSchedule" data-id="${s.id}">汇总对比</button>
        <button class="small" data-action="runScheduleNow" data-id="${s.id}"
          ${expired ? 'disabled title="请先延长运行时长"' : ''}>立即跑一轮</button>
        ${expired ? '' : `<button class="small" data-action="toggleSchedule" data-id="${s.id}">${s.enabled ? '停用' : '启用'}</button>`}
        <button class="small" data-action="editSchedule" data-id="${s.id}">${expired ? '延长时间' : '编辑'}</button>
        <button class="small danger" data-action="deleteSchedule" data-id="${s.id}">删除</button>
      </div></td>
    </tr>`;
  }).join('');
}

/* 总览页（手机端首页）信息卡：最近测试 + 定时任务动态，行可点直达 */
function renderHome() {
  const runsBox = $('homeRunsList');
  if (runsBox) {
    const runs = ALL_RUNS.slice(0, 3);
    runsBox.innerHTML = runs.length ? runs.map(r => `
      <button class="home-row" data-action="openRun" data-id="${r.id}">
        <span class="badge ${r.status}">${statusText(r.status)}</span>
        <span class="home-row-main">
          <b>${esc(r.target_name)}</b>
          <span class="quiet">${(r.source === 'schedule' || r.schedule_id) ? '定时' : '手动'} #${r.id} · ${esc(String(r.created_at || '').slice(5, 16))} · ${r.done_count}/${r.backend_total}</span>
        </span>
        <span class="home-row-chev">›</span>
      </button>`).join('')
      : '<p class="muted home-empty">还没有测试记录，点上方「发起一次测试」跑一轮</p>';
  }
  const schedBox = $('homeSchedList');
  if (schedBox) {
    const list = SCHEDULES.filter(s => s.enabled).slice(0, 3);
    schedBox.innerHTML = list.length ? list.map(s => {
      const expired = !!s.expired || s.last_status === 'expired';
      const st = expired ? 'expired' : (s.last_status || '');
      const sub = expired
        ? '已到期'
        : `下次 ${s.next_run_in || '—'} · 成功 ${Math.max(0, s.run_count - s.fail_count)}/${s.run_count}`;
      return `
      <button class="home-row" data-action="compareSchedule" data-id="${s.id}">
        <span class="badge ${st === 'finished' ? 'done' : st}">${schedStatusText(st)}</span>
        <span class="home-row-main">
          <b>${esc(s.name)}</b>
          <span class="quiet">${esc(sub)}</span>
        </span>
        <span class="home-row-chev">›</span>
      </button>`;
    }).join('')
      : '<p class="muted home-empty">暂无启用中的定时任务，去「任务」页新建一个长期对比</p>';
  }
}

function toggleSchedCard(id) {
  id = Number(id);
  if (EXPANDED_SCHED_CARDS.has(id)) EXPANDED_SCHED_CARDS.delete(id);
  else EXPANDED_SCHED_CARDS.add(id);
  renderSchedules();
}

function renderScheduleForm() {
  const sel = $('sTarget');
  if (!sel) return;
  const targets = MACHINES.filter(m => m.role === 'target');
  const prev = sel.value;
  sel.innerHTML = targets.length
    ? targets.map(m => {
        const a4 = testAddr(m, 4);
        const a6 = testAddr(m, 6);
        const addr = a4 ? esc(a4) : (a6 ? `${esc(a6)}（仅 IPv6）` : '地址未探测');
        return `<option value="${m.id}">${esc(m.name)}（${addr}）</option>`;
      }).join('')
    : '<option value="">— 请先添加一台角色为「目标」的机器 —</option>';
  if ([...sel.options].some(o => o.value === prev)) sel.value = prev;

  const box = $('sBackendList');
  const prevChecked = new Set([...box.querySelectorAll('input:checked')].map(x => x.value));
  const backends = MACHINES.filter(m => m.role === 'backend');
  box.innerHTML = backends.length
    ? backends.map(m => {
        const v6 = hasV6(m) ? '<span class="v6-yes">有 V6</span>' : '<span class="v6-no">无 IPv6</span>';
        return `<label class="sched-backend-option">
          <input type="checkbox" value="${m.id}">
          <span class="sched-backend-name" title="${esc(m.name)}">${esc(m.name)}</span>
          <span class="sched-backend-meta">
            <span>${esc(m.region) || '未标记地区'}</span>
            <span>${esc(m.bandwidth) || '未标记带宽'}</span>
            ${v6}
          </span>
        </label>`;
      }).join('')
    : '<span class="muted">暂无后端机器</span>';
  box.querySelectorAll('input').forEach(cb => {
    cb.checked = prevChecked.size ? prevChecked.has(cb.value) : true;
  });
}

function seedScheduleForm(s) {
  const cur = {
    streams: parseInt($('cfgStreams').value, 10) || 1,
    duration: parseInt($('cfgDuration').value, 10) || 10,
    port: parseInt($('cfgPort').value, 10) || 5201,
    ping_count: parseInt($('cfgPing').value, 10) || 200,
    udp: $('cfgUdp').checked,
    udp_bandwidth: ($('cfgUdpBw').value || '100M').trim().toUpperCase(),
  };
  const src = s || cur;
  $('sStreams').value = src.streams;
  $('sDuration').value = src.duration;
  $('sPort').value = src.port;
  $('sPing').value = src.ping_count;
  $('sUdp').checked = !!src.udp;
  $('sUdpBw').value = src.udp_bandwidth || '100M';
  syncUdpControls('sUdp', 'sUdpBw');
  $('sIpv6').checked = !!(s ? s.ip_version === 6 : ($('useIpv6').checked && !$('useIpv6').disabled));
  $('sWait').checked = !!(s && s.on_busy === 'wait');
  // 定时任务的默认参数直接跟随「发起测试」区域的当前设置，避免两处不一致
  $('sShowAdv').checked = !!s;
  $('sAdvBox').classList.toggle('hidden', !s);
}

function openScheduleForm(s) {
  CURRENT_SCHED = s ? s.id : null;
  $('sFormTitle').textContent = s ? `编辑定时任务 #${s.id}` : '新建定时任务';
  $('sId').value = s ? s.id : '';
  $('sName').value = s ? s.name : '';
  const unit = s
    ? (s.interval_seconds % 86400 === 0 ? 86400 : (s.interval_seconds % 3600 === 0 ? 3600 : 60))
    : 3600;
  $('sIntervalUnit').value = String(unit);
  $('sInterval').value = s ? Math.max(1, Math.round(s.interval_seconds / unit)) : 1;
  const lifetime = s ? parseInt(s.lifetime_seconds || 0, 10) : 7 * 86400;
  $('sLifetimeEnabled').checked = lifetime > 0;
  const lifeUnit = lifetime > 0 && lifetime % 86400 === 0
    ? 86400 : (lifetime > 0 && lifetime % 3600 === 0 ? 3600 : 60);
  $('sLifetimeUnit').value = String(lifeUnit);
  $('sLifetimeValue').value = lifetime > 0
    ? Math.max(1, Math.round(lifetime / lifeUnit))
    : 7;
  syncLifetimeControls();
  renderScheduleForm();
  if (s) {
    $('sTarget').value = String(s.target_id);
    const box = $('sBackendList');
    const want = new Set((s.backend_ids || []).map(String));
    box.querySelectorAll('input').forEach(cb => { cb.checked = want.has(cb.value); });
  }
  seedScheduleForm(s);
  $('schedModal').classList.remove('hidden');
  $('sName').focus();
}

function closeSchedModal() {
  $('schedModal').classList.add('hidden');
  CURRENT_SCHED = null;
}

function saveSchedule(anchor) {
  const interval = intervalSeconds();
  if (!interval) return errorToast('请填写间隔时间（≥ 1）');
  if (interval < 300 || interval > 30 * 86400) {
    return errorToast('间隔需在 5 分钟 ~ 30 天之间（当前 ' + humanDuration(interval) + '）');
  }
  const lifetime = lifetimeSeconds();
  if (lifetime === null) return errorToast('请填写有效的任务运行时长（≥ 1）');
  if (lifetime > 365 * 86400) {
    return errorToast('任务运行时长最长 365 天（当前 ' + humanDuration(lifetime) + '）');
  }
  const target_id = parseInt($('sTarget').value, 10);
  if (!target_id) return errorToast('请先添加一台角色为「目标」的机器');
  const backend_ids = [...$('sBackendList').querySelectorAll('input:checked')]
    .map(x => parseInt(x.value, 10));
  if (!backend_ids.length) return errorToast('请至少勾选一台后端机器');
  const readNum = (id, lo, hi, label) => {
    const v = parseInt($(id).value, 10);
    if (!Number.isFinite(v) || v < lo || v > hi) {
      errorToast(`${label} 需在 ${lo}–${hi} 之间`);
      return null;
    }
    return v;
  };
  const streams = readNum('sStreams', 1, 32, '线程数');
  const duration = readNum('sDuration', 1, 300, '单向时长（秒）');
  const port = readNum('sPort', 1, 65535, 'iperf3 端口');
  const ping_count = readNum('sPing', 1, 2000, 'ping 次数');
  if (streams === null || duration === null || port === null || ping_count === null) return;
  const udp = $('sUdp').checked;
  const udp_bandwidth = ($('sUdpBw').value || '100M').trim().toUpperCase() || '100M';
  if (udp && !/^\d{1,5}(\.\d{1,3})?[KMG]$/.test(udp_bandwidth)) {
    return errorToast('UDP 目标带宽必须带单位（示例：100M、1G、500K）');
  }
  const ip_version = $('sIpv6').checked ? 6 : 4;
  const ports = {};
  const body = {
    name: $('sName').value.trim() || '定时任务', target_id, backend_ids, ip_version,
    streams, duration, port, ping_count, udp, udp_bandwidth, ports,
    interval_seconds: interval, lifetime_seconds: lifetime,
    on_busy: $('sWait').checked ? 'wait' : 'skip',
  };
  const editingId = CURRENT_SCHED;
  showPopconfirm(anchor, {
    title: editingId ? '保存定时任务修改？' : '创建定时任务？',
    message: `${body.name} · 每 ${humanDuration(interval)} · ${backend_ids.length} 台后端`
      + (editingId ? '' : ' · 保存后立即开始首轮'),
    confirmText: editingId ? '保存修改' : '创建任务',
    busyText: editingId ? '保存中…' : '创建中…',
    errorPrefix: '保存失败',
    onConfirm: async () => {
      if (editingId) {
        await api(`/api/schedules/${editingId}`, {method: 'PUT', body: JSON.stringify(body)});
        toast('已保存（下一轮按新设置执行）');
      } else {
        const created = await api('/api/schedules', {method: 'POST', body: JSON.stringify(body)});
        toast(created.started
          ? `定时任务已创建，首轮 ${created.start_note || '已开始'}`
          : `定时任务已创建，首轮未启动：${created.start_note || '请稍后重试'}`,
          created.started ? 5000 : 8000);
        if (created.started && created.last_run_id) {
          await loadRuns();
          openRun(created.last_run_id);
        }
      }
      closeSchedModal();
      await loadSchedules();
    },
  });
}

function editSchedule(id) {
  const s = SCHEDULES.find(x => x.id === Number(id));
  if (s) openScheduleForm(s);
}

function toggleSchedule(id, anchor) {
  const s = SCHEDULES.find(x => x.id === Number(id));
  if (!s) return;
  showPopconfirm(anchor, {
    title: s.enabled ? '停用定时任务？' : '启用定时任务？',
    message: s.enabled ? `「${s.name}」停用后不再自动执行` : `「${s.name}」按当前间隔重新排期`,
    confirmText: s.enabled ? '停用' : '启用',
    danger: s.enabled,
    errorPrefix: s.enabled ? '停用失败' : '启用失败',
    onConfirm: async () => {
      await api(`/api/schedules/${id}/toggle`, {method: 'POST'});
      await loadSchedules();
      toast(s.enabled ? '已停用' : '已启用');
    },
  });
}

function deleteSchedule(id, anchor) {
  const s = SCHEDULES.find(x => x.id === Number(id));
  if (!s) return;
  showPopconfirm(anchor, {
    title: '删除定时任务',
    message: `「${s.name}」的测试记录与对比数据会保留`,
    confirmText: '删除',
    danger: true,
    errorPrefix: '删除失败',
    onConfirm: async () => {
      await api(`/api/schedules/${id}`, {method: 'DELETE'});
      if (CURRENT_SCHED === Number(id)) closeSchedDetail();
      await loadSchedules();
      toast('定时任务已删除，历史记录保留');
    },
  });
}

function runScheduleNow(id, anchor) {
  const s = SCHEDULES.find(x => x.id === Number(id));
  if (!s) return;
  showPopconfirm(anchor, {
    title: '立即执行一轮？',
    message: `「${s.name}」排期不变`,
    confirmText: '立即执行',
    busyText: '启动中…',
    errorPrefix: '启动失败',
    onConfirm: async () => {
      const r = await api(`/api/schedules/${id}/run-now`, {method: 'POST'});
      toast(r.note || '已开始');
      await loadSchedules();
      await loadRuns();
      const st = await api('/api/status');
      if (st.active_run_id) openRun(st.active_run_id);
    },
  });
}

/* --- 对比视图 --- */

async function compareSchedule(id) {
  document.body.classList.add('app-detail-open');
  const s = SCHEDULES.find(x => x.id === Number(id)) || {};
  if (COMPARE_ABORT) COMPARE_ABORT.abort();
  COMPARE_ABORT = new AbortController();
  const request = COMPARE_ABORT;
  $('schedDetailTitle').textContent = `长期对比：${s.name || ('#' + id)}`;
  $('schedDetail').classList.remove('hidden');
  $('schedDetail').scrollIntoView({behavior: 'smooth'});
  $('schedDetailMeta').textContent = '正在加载趋势数据…';
  $('chartWrap').innerHTML = '<div class="skeleton-chart"></div>';
  $('chartLegend').innerHTML = '';
  $('schedStatsBody').innerHTML = '';
  $('schedRoundsBody').innerHTML = '';
  let d;
  try { d = await api(`/api/schedules/${id}/comparison`, {signal: request.signal}); } catch (e) {
    if (e.name === 'AbortError') return;
    $('schedDetailMeta').textContent = '加载失败: ' + e.message;
    return;
  } finally {
    if (COMPARE_ABORT === request) COMPARE_ABORT = null;
  }
  CURRENT_SCHED = Number(id);
  renderScheduleComparison(d);
}

function closeSchedDetail() {
  $('schedDetail').classList.add('hidden');
  CURRENT_SCHED = null;
  CURRENT_COMPARISON = null;
  CHART_STATE.scheduleId = null;
  document.body.classList.remove('app-detail-open');
}

function schedTrendText(st) {
  if (st.trend_pct === null || st.trend_pct === undefined) return '样本不足';
  const v = Number(st.trend_pct);
  return (v > 0 ? '+' : '') + fmtN(v, 1) + '%';
}

/* 精简版长期报告：时间节点必须保留，抖动/重传/轮次编号等细节留在面板里看 */
function buildSchedReportText(d) {
  const s = d.schedule || {};
  const rounds = d.rounds || [];
  const lines = [];
  lines.push(`# LinkPilot 长期对比：${s.name || '未命名任务'}`);
  lines.push('');
  lines.push(`- 目标机：${s.target_name || '—'}（${s.backend_count ?? (d.machines || []).length} 台后端）`);
  lines.push(`- 节奏：每 ${s.interval_text || '—'}，共 ${d.total_rounds ?? rounds.length} 轮`);
  lines.push(`- 时间范围：${rounds.length ? `${rounds[0].created_at} ~ ${rounds[rounds.length - 1].created_at}` : '暂无数据'}`);

  const stats = d.stats || [];
  if (stats.length) {
    lines.push('');
    lines.push('## 汇总');
    lines.push('| 机器 | 上行 均/低/高 Mbit/s | 下行 均/低/高 Mbit/s | 丢包 | RTT | 趋势 | 评级 |');
    lines.push('|---|---|---|---|---|---|---|');
    const tri = (agg, nd) => agg ? `${fmtN(agg.avg, nd)}/${fmtN(agg.min, nd)}/${fmtN(agg.max, nd)}` : '—';
    for (const st of stats) {
      const ratings = [st.best_rating, st.worst_rating].filter(Boolean).map(ratingText).join(' / ') || '—';
      lines.push(`| ${st.name} | ${tri(st.up, 1)} | ${tri(st.down, 1)}`
        + ` | ${st.loss ? fmtN(st.loss.avg, 2) + '%' : '—'} | ${st.rtt ? fmtN(st.rtt.avg, 1) + ' ms' : '—'}`
        + ` | ${schedTrendText(st)} | ${ratings} |`);
    }
  }

  if (rounds.length) {
    lines.push('');
    lines.push('## 逐轮记录');
    lines.push('| 时间 | 机器 | 上行 Mbit/s | 下行 Mbit/s | 丢包 | 评级 |');
    lines.push('|---|---|---|---|---|---|');
    const byRound = {};
    (d.machines || []).forEach(m => (m.series || []).forEach(pt => {
      (byRound[pt.run_id] = byRound[pt.run_id] || []).push({name: m.name, pt});
    }));
    for (const r of rounds) {
      const pts = byRound[r.run_id] || [];
      if (!pts.length) {
        lines.push(`| ${r.created_at} | — | — | — | — | 本轮无数据 |`);
        continue;
      }
      for (const {name, pt} of pts) {
        const at = pt.at || r.created_at;
        if (pt.status === 'done') {
          lines.push(`| ${at} | ${name} | ${fmt(pt.up)} | ${fmt(pt.down)} | ${fmt(pt.loss, '%')} | ${pt.rating ? ratingText(pt.rating) : '—'} |`);
        } else {
          const err = String(pt.error || pt.status || '失败').replace(/\|/g, '/').slice(0, 40);
          lines.push(`| ${at} | ${name} | — | — | — | 失败（${err}）|`);
        }
      }
    }
  }
  return lines.join('\n');
}

async function copySchedReport() {
  if (!CURRENT_COMPARISON) return errorToast('对比数据还没有加载完成');
  const btn = document.querySelector('[data-action="copySchedReport"]');
  const old = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '生成报告中…'; }
  try {
    const text = buildSchedReportText(CURRENT_COMPARISON);
    const built = buildTrendChartSvg(CURRENT_COMPARISON, true);
    const html = `<div style="font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;color:#202238;line-height:1.65">
      <style>
        table{border-collapse:collapse;width:100%;font-size:13px;margin:10px 0}
        th,td{border:1px solid #e3e4f0;padding:6px 9px;text-align:left}
        th{background:#f4f5fb;color:#6d7187}
        h1{font-size:20px}h2{font-size:16px;margin-top:18px}
        p{margin:4px 0}
        svg{max-width:100%;height:auto;border:1px solid #e3e4f0;border-radius:8px;background:#fff}
      </style>
      ${renderMarkdown(text)}
      ${built.svg ? `<h2>吞吐趋势</h2>${built.svg}<p style="color:#6d7187;font-size:12px">${built.legend}</p>` : ''}
    </div>`;
    await copyRichText(text, html, '长期对比报告已复制');
  } catch (e) {
    errorToast('复制失败: ' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = old; }
  }
}

function downloadSchedReport() {
  if (!CURRENT_COMPARISON) return errorToast('对比数据还没有加载完成');
  const s = CURRENT_COMPARISON.schedule || {};
  const safe = String(s.name || '长期对比').replace(/[\\/:*?"<>|]/g, '_');
  const blob = new Blob([buildSchedReportText(CURRENT_COMPARISON)], {type: 'text/markdown;charset=utf-8'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `LinkPilot-长期对比-${safe}.md`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

function comparisonRows(d) {
  const headers = [
    '轮次', '开始时间', '结束时间', '轮次状态', '目标机', '后端机器', '地区', '带宽',
    '上行 Mbit/s', '下行 Mbit/s', '丢包 %', 'RTT ms', '抖动 ms', '重传',
    'UDP 丢包 %', '评价', '错误',
  ];
  const clean = v => String(v ?? '').replace(/[\t\r\n]+/g, ' ');
  const value = v => v === null || v === undefined ? '' : v;
  const byRound = {};
  (d.machines || []).forEach(m => (m.series || []).forEach(pt => {
    (byRound[pt.run_id] = byRound[pt.run_id] || []).push({name: m.name, region: m.region,
      bandwidth: m.bandwidth, pt});
  }));
  const rows = [];
  (d.rounds || []).forEach(r => {
    const pts = byRound[r.run_id] || [{}];
    pts.forEach(x => {
      const pt = x.pt || {};
      rows.push([
        r.run_id, r.created_at, r.finished_at, r.status, d.schedule.target_name,
        x.name, x.region, x.bandwidth, value(pt.up), value(pt.down), value(pt.loss),
        value(pt.rtt), value(pt.mdev), value(pt.retr), value(pt.udp_loss),
        pt.rating ? ratingText(pt.rating) : '', pt.error,
      ].map(clean));
    });
  });
  return {headers, rows};
}

function comparisonTsv(d) {
  const {headers, rows} = comparisonRows(d);
  return [headers.join('\t'), ...rows.map(row => row.join('\t'))].join('\n');
}

function comparisonTableHtml(d) {
  const {headers, rows} = comparisonRows(d);
  const th = headers.map(h => `<th>${esc(h)}</th>`).join('');
  const body = rows.map(row => `<tr>${row.map(v => `<td>${esc(v)}</td>`).join('')}</tr>`).join('');
  return `<table style="border-collapse:collapse;width:100%;font-size:12px">
    <thead><tr style="background:#f4f5fb">${th}</tr></thead><tbody>${body}</tbody></table>`;
}

function copySchedTable() {
  if (!CURRENT_COMPARISON) return errorToast('对比数据还没有加载完成');
  const d = CURRENT_COMPARISON;
  const note = `最近 ${d.view_rounds || d.rounds.length}/${d.total_rounds || d.rounds.length} 轮`;
  const html = `<div style="font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;color:#302c26">
    <h3>LinkPilot 逐轮对比数据</h3><p style="color:#776f62">${esc(note)}</p>${comparisonTableHtml(d)}</div>`;
  copyRichText(comparisonTsv(d), html, '逐轮对比表格已复制');
}

function renderScheduleComparison(d) {
  CURRENT_COMPARISON = d;
  const s = d.schedule || {};
  $('schedDetailTitle').textContent = `长期对比：${s.name || '历史记录'}`;
  const rounds = d.rounds || [];
  const totalRounds = Number(d.total_rounds ?? rounds.length);
  const viewRounds = Number(d.view_rounds ?? rounds.length);
  $('schedDetailMeta').textContent =
    `${s.name} · 每 ${s.interval_text} · 运行 ${s.lifetime_text}`
    + ` · 已完成 ${s.run_count} 轮`
    + `（成功 ${Math.max(0, s.run_count - s.fail_count)} / 失败 ${s.fail_count} / 未跑成 ${s.skip_count}）`
    + ` · 目标机: ${s.target_name} ← ${s.backend_count} 台后端`
    + (rounds.length ? ` · 展示最近 ${viewRounds}/${totalRounds} 轮: ${rounds[0].created_at} ~ ${rounds[rounds.length - 1].created_at}` : '')
    + (s.archived ? ' · 定时任务已删除，历史记录仍可查看和复制'
      : s.expired ? ` · 已到期${s.ends_at ? '（' + s.ends_at + '）' : ''}`
      : (s.enabled ? ` · 下次: ${s.next_run_in || '—'}` : ' · 已停用'))
    + (rounds.length > 500 ? ' · 页面明细显示最近 500 轮，复制/下载包含全部数据' : '');

  drawTrendChart(d);

  const stats = d.stats || [];
  $('schedStatsBody').innerHTML = stats.length ? stats.map(st => {
    const trend = st.trend_pct === null || st.trend_pct === undefined
      ? `<span class="trend-flat" title="${esc(st.trend_reason || '结果不足')}">样本不足</span>`
      : (st.trend_pct > 3 ? `<span class="trend-up"><b>+${fmt(st.trend_pct)}%</b></span>`
        : (st.trend_pct < -3 ? `<span class="trend-down"><b>${fmt(st.trend_pct)}%</b></span>`
          : `<span class="trend-flat"><b>${fmt(st.trend_pct)}%</b></span>`));
    const tri = (agg, nd) => agg
      ? `${fmtN(agg.avg, nd)} / ${fmtN(agg.min, nd)} / ${fmtN(agg.max, nd)}`
      : '—';
    const avg = (agg, nd, suffix = '') => agg ? fmtN(agg.avg, nd, suffix) : '—';
    const ratings = [st.best_rating, st.worst_rating].filter(Boolean);
    return `<tr>
      <td>${esc(st.name)}</td><td>${esc(st.region) || '—'}</td><td>${st.samples}</td>
      <td>${tri(st.up, 1)}</td><td>${tri(st.down, 1)}</td>
      <td>${avg(st.loss, 2, '%')}</td><td>${avg(st.rtt, 1, ' ms')}</td>
      <td>${avg(st.mdev, 2, ' ms')}</td><td>${avg(st.retr, 0)}</td>
      <td>${trend}</td>
      <td>${ratings.length ? ratings.map(r => `<span class="rb ${ratingClass(r)}">${ratingText(r)}</span>`).join(' / ') : '—'}</td>
    </tr>`;
  }).join('') : '<tr><td colspan="11" class="muted">还没有完成任何一轮测试</td></tr>';

  const byRound = {};
  (d.machines || []).forEach(m => (m.series || []).forEach(pt => {
    (byRound[pt.run_id] = byRound[pt.run_id] || []).push({name: m.name, pt});
  }));
  const rows = [];
  rounds.slice(-500).forEach(r => {
    const pts = byRound[r.run_id] || [];
    if (!pts.length) {
      rows.push(`<tr><td>#${r.run_id}</td><td class="muted">${r.created_at}</td>
        <td colspan="6" class="muted">—</td>
        <td><button class="small" data-action="openRun" data-id="${r.run_id}">查看</button></td></tr>`);
      return;
    }
    pts.forEach((x, i) => {
      const pt = x.pt;
      const first = i === 0;
      const body = pt.status === 'done'
        ? `<td>${fmt(pt.up, ' Mbit/s')}</td><td>${fmt(pt.down, ' Mbit/s')}</td>
           <td>${fmt(pt.loss, '%')}</td><td>${fmt(pt.rtt, ' ms')}</td>
           <td><span class="rb ${ratingClass(pt.rating)}">${ratingText(pt.rating)}</span></td>`
        : `<td colspan="4" style="color:var(--red)">❌ ${esc(pt.error || pt.status)}</td><td>—</td>`;
      rows.push(`<tr>
        <td>${first ? '#' + r.run_id : ''}</td>
        <td class="muted">${first ? r.created_at : ''}</td>
        <td>${esc(x.name)}</td>${body}
        <td>${first ? `<button class="small" data-action="openRun" data-id="${r.run_id}">查看</button>` : ''}</td>
      </tr>`);
    });
  });
    $('schedRoundsBody').innerHTML = rows.length
    ? rows.join('') : '<tr><td colspan="9" class="muted">暂无数据</td></tr>';
}

function renderChartControls(d) {
  const machines = (d.machines || []).filter(m => (m.series || []).length);
  const valid = new Set(machines.map(m => String(m.key)));
  if (CHART_STATE.scheduleId !== Number(CURRENT_SCHED)) {
    CHART_STATE.scheduleId = Number(CURRENT_SCHED);
    CHART_STATE.selected = new Set(valid);
  }
  for (const key of [...CHART_STATE.selected]) {
    if (!valid.has(key)) CHART_STATE.selected.delete(key);
  }
  if (CHART_STATE.mode === 'single' && CHART_STATE.selected.size !== 1 && machines.length) {
    CHART_STATE.selected = new Set([String(machines[0].key)]);
  }
  document.querySelectorAll('#chartModeButtons button').forEach(btn => {
    btn.classList.toggle('on', btn.dataset.mode === CHART_STATE.mode);
  });
  document.querySelectorAll('#chartDirButtons button').forEach(btn => {
    btn.classList.toggle('on', btn.dataset.direction === CHART_STATE.direction);
  });
  const picker = $('chartPicker');
  if (picker) {
    picker.innerHTML = machines.map((m, index) => {
      const key = String(m.key);
      const on = CHART_STATE.selected.has(key);
      const color = CHART_COLORS[index % CHART_COLORS.length];
      return `<button class="chip ${on ? 'on' : ''}" data-action="toggleChartMachine"
        data-key="${esc(key)}"><i style="background:${color}"></i>${esc(m.name)}</button>`;
    }).join('') || '<span class="muted">暂无可绘制线路</span>';
  }
}

function setChartMode(mode) {
  CHART_STATE.mode = mode === 'single' ? 'single' : 'multi';
  if (CHART_STATE.mode === 'single' && CHART_STATE.selected.size > 1) {
    CHART_STATE.selected = new Set([[...CHART_STATE.selected][0]]);
  }
  if (CURRENT_COMPARISON) drawTrendChart(CURRENT_COMPARISON);
}

function setChartDirection(direction) {
  CHART_STATE.direction = ['up', 'down', 'both'].includes(direction) ? direction : 'both';
  if (CURRENT_COMPARISON) drawTrendChart(CURRENT_COMPARISON);
}

function toggleChartMachine(key) {
  key = String(key);
  if (CHART_STATE.mode === 'single') {
    CHART_STATE.selected = new Set([key]);
  } else if (CHART_STATE.selected.has(key)) {
    CHART_STATE.selected.delete(key);
  } else {
    CHART_STATE.selected.add(key);
  }
  if (CURRENT_COMPARISON) drawTrendChart(CURRENT_COMPARISON);
}

const CHART_COLORS = ['#4f5cff', '#8b5cf6', '#0ea5a4', '#e05a8b', '#f59e0b',
                      '#6366f1', '#14b8a6', '#d946ef', '#f43f5e', '#84cc16'];

/* Catmull-Rom 转三次贝塞尔：折线平滑但仍保留转折趋势 */
function smoothPath(pts) {
  if (pts.length < 2) return '';
  if (pts.length === 2) return `M ${pts[0][0].toFixed(1)} ${pts[0][1].toFixed(1)} L ${pts[1][0].toFixed(1)} ${pts[1][1].toFixed(1)}`;
  let d = `M ${pts[0][0].toFixed(1)} ${pts[0][1].toFixed(1)}`;
  for (let i = 0; i < pts.length - 1; i++) {
    const p0 = pts[Math.max(0, i - 1)], p1 = pts[i], p2 = pts[i + 1], p3 = pts[Math.min(pts.length - 1, i + 2)];
    const c1x = p1[0] + (p2[0] - p0[0]) / 6, c1y = p1[1] + (p2[1] - p0[1]) / 6;
    const c2x = p2[0] - (p3[0] - p1[0]) / 6, c2y = p2[1] - (p3[1] - p1[1]) / 6;
    d += ` C ${c1x.toFixed(1)} ${c1y.toFixed(1)}, ${c2x.toFixed(1)} ${c2y.toFixed(1)}, ${p2[0].toFixed(1)} ${p2[1].toFixed(1)}`;
  }
  return d;
}

let CHART_CTX = null;   // 当前图的数据上下文，供 hover 十字线 / 数值气泡读取

function hideChartTip() {
  const tip = $('chartTip'), cross = $('chartCross');
  if (tip) tip.hidden = true;
  if (cross) cross.hidden = true;
}

function chartTipMove(e) {
  if (!CHART_CTX) return;
  const svg = $('chartWrap').querySelector('svg'), box = $('chartBox');
  if (!svg || !box) return;
  const svgRect = svg.getBoundingClientRect(), boxRect = box.getBoundingClientRect();
  if (e.clientX < svgRect.left || e.clientX > svgRect.right) { hideChartTip(); return; }
  const {W, PL, PR, xs, dirs, series, roundIds, roundAt} = CHART_CTX;
  const ratio = svgRect.width / W;
  const vx = (e.clientX - svgRect.left) / ratio;
  if (vx < PL - 8 || vx > W - PR + 8) { hideChartTip(); return; }
  let best = 0, bestD = Infinity;
  xs.forEach((xv, i) => { const d2 = Math.abs(xv - vx); if (d2 < bestD) { bestD = d2; best = i; } });
  const cross = $('chartCross'), tip = $('chartTip');
  if (!cross || !tip) return;
  const screenX = svgRect.left + xs[best] * ratio - boxRect.left;
  cross.hidden = false;
  cross.style.left = screenX + 'px';
  const rows = series.map(s => {
    const parts = dirs.map(dir => {
      const v = s[dir][best];
      return v === undefined ? '' : `<span class="tip-v"><i style="background:${s.color}"></i>`
        + `${dir === 'up' ? '↑ 上行' : '↓ 下行'} ${fmt(v)} Mbit/s</span>`;
    }).join('');
    return parts ? `<div class="tip-row"><b>${esc(s.name)}</b>${parts}</div>` : '';
  }).join('');
  const t = String(roundAt[roundIds[best]] || '');
  tip.innerHTML = `<div class="tip-head">第 ${best + 1} 轮${t ? ` · ${esc(t.slice(5, 16))}` : ''}</div>`
    + (rows || '<div class="tip-row muted">该轮无可绘制数据</div>');
  tip.hidden = false;
  const tipW = tip.offsetWidth;
  let left = screenX + 10;
  if (left + tipW > boxRect.width - 4) left = screenX - tipW - 10;
  tip.style.left = Math.max(4, left) + 'px';
  tip.style.top = '10px';
}

function buildTrendChartSvg(d, forClipboard = false) {
  const allMachines = d.machines || [];
  const selected = allMachines.filter(m => CHART_STATE.selected.has(String(m.key)));
  const roundIds = (d.rounds || []).map(r => r.run_id);
  const roundAt = Object.fromEntries((d.rounds || []).map(r => [r.run_id, r.created_at]));
  if (!selected.length || !roundIds.length) { CHART_CTX = null; return {svg: '', legend: ''}; }
  const dirs = CHART_STATE.direction === 'both' ? ['up', 'down'] : [CHART_STATE.direction];
  const values = [];
  selected.forEach(m => m.series.forEach(p => dirs.forEach(k => {
    if (p.status === 'done' && p[k] !== null && p[k] !== undefined) values.push(p[k]);
  })));
  if (!values.length) { CHART_CTX = null; return {svg: '', legend: ''}; }
  const W = 720, H = 250, PL = 44, PR = 12, PT = 18, PB = 26;
  let max = Math.max(...values), min = Math.min(...values);
  max = max * 1.08;
  min = Math.max(0, min * 0.9);
  if (max - min < 1) max = min + 1;
  const x = i => roundIds.length === 1
    ? PL + (W - PL - PR) / 2
    : PL + (W - PL - PR) * (i / (roundIds.length - 1));
  const y = v => PT + (H - PT - PB) * (1 - (v - min) / (max - min));
  const roundIndex = new Map(roundIds.map((rid, i) => [rid, i]));
  const idxOf = rid => roundIndex.has(rid) ? roundIndex.get(rid) : -1;
  const grid = forClipboard ? '#e3e4f0' : 'var(--border)';
  const text = forClipboard ? '#6d7187' : 'var(--muted)';
  // 单机视图给线下渐变面积；多机叠加只画线，避免颜色糊在一起
  const area = selected.length === 1;
  const clip = forClipboard ? 'c' : 's';
  let defs = '', g = '';
  if (area) {
    const acolor = CHART_COLORS[allMachines.findIndex(m => String(m.key) === String(selected[0].key)) % CHART_COLORS.length];
    dirs.forEach(dir => {
      defs += `<linearGradient id="ga-${dir}-${clip}" x1="0" y1="0" x2="0" y2="1">`
        + `<stop offset="0%" stop-color="${acolor}" stop-opacity="${forClipboard ? 0.22 : 0.16}"/>`
        + `<stop offset="100%" stop-color="${acolor}" stop-opacity="0"/></linearGradient>`;
    });
  }
  for (let k = 0; k <= 4; k++) {
    const v = min + (max - min) * k / 4;
    const yy = y(v).toFixed(1);
    g += `<line x1="${PL}" y1="${yy}" x2="${W - PR}" y2="${yy}" stroke="${grid}" stroke-width="1"/>`
      + `<text x="${PL - 8}" y="${(+yy + 4).toFixed(1)}" font-size="11" text-anchor="end" fill="${text}">${v.toFixed(v >= 100 ? 0 : 1)}</text>`;
  }
  const step = Math.max(1, Math.ceil(roundIds.length / 8));
  roundIds.forEach((rid, i) => {
    if (i % step !== 0 && i !== roundIds.length - 1) return;
    g += `<text x="${x(i).toFixed(1)}" y="${H - 6}" font-size="10" text-anchor="middle" fill="${text}">第 ${i + 1} 轮</text>`;
  });

  selected.forEach(m => {
    const color = CHART_COLORS[allMachines.findIndex(x0 => String(x0.key) === String(m.key)) % CHART_COLORS.length];
    const pts = m.series.map(p => ({i: idxOf(p.run_id), pt: p})).filter(p => p.i >= 0);
    dirs.forEach(dir => {
      const dash = dir === 'down' && CHART_STATE.direction === 'both' ? ' stroke-dasharray="6 4"' : '';
      let cur = [];
      const flush = () => {
        if (cur.length > 1) {
          if (area && cur.length > 2) {
            g += `<path d="${smoothPath(cur)} L ${cur[cur.length - 1][0].toFixed(1)} ${H - PB} L ${cur[0][0].toFixed(1)} ${H - PB} Z"`
              + ` fill="url(#ga-${dir}-${clip})" stroke="none"/>`;
          }
          g += `<path d="${smoothPath(cur)}" fill="none" stroke="${color}" stroke-width="2.2" stroke-linecap="round"${dash}/>`;
        }
        cur = [];
      };
      pts.forEach(({i, pt}) => {
        const v = pt.status === 'done' ? pt[dir] : null;
        if (v === null || v === undefined) { flush(); return; }
        cur.push([x(i), y(v)]);
        g += `<circle cx="${x(i).toFixed(1)}" cy="${y(v).toFixed(1)}" r="4" fill="${color}" opacity="0">`
          + `<title>${esc(m.name)} · ${dir === 'up' ? '上行' : '下行'} ${fmt(v)} Mbit/s · ${esc(pt.at || '')}</title></circle>`;
      });
      flush();
    });
  });
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="吞吐趋势折线图">`
    + `<rect width="${W}" height="${H}" fill="${forClipboard ? '#ffffff' : 'transparent'}"/>`
    + (defs ? `<defs>${defs}</defs>` : '') + `${g}</svg>`;
  const legend = selected.map(m => {
    const color = CHART_COLORS[allMachines.findIndex(x0 => String(x0.key) === String(m.key)) % CHART_COLORS.length];
    return `<span><i style="background:${color}"></i>${esc(m.name)}</span>`;
  }).join('') + (CHART_STATE.direction === 'both'
    ? '<span class="quiet">实线=上行 · 虚线=下行</span>' : '');
  if (!forClipboard) {
    // 记录数据上下文，供图表 hover 十字线 / 气泡读取
    CHART_CTX = {
      W, PL, PR, dirs, roundIds, roundAt,
      xs: roundIds.map((_, i) => x(i)),
      series: selected.map(m => ({
        name: m.name,
        color: CHART_COLORS[allMachines.findIndex(x0 => String(x0.key) === String(m.key)) % CHART_COLORS.length],
        up: {}, down: {},
      })),
    };
    selected.forEach((m, mi) => m.series.forEach(p => {
      const i = idxOf(p.run_id);
      if (i < 0) return;
      dirs.forEach(dir => {
        const v = p.status === 'done' ? p[dir] : null;
        if (v !== null && v !== undefined) CHART_CTX.series[mi][dir][i] = v;
      });
    }));
  }
  return {svg, legend};
}

function drawTrendChart(d) {
  renderChartControls(d);
  hideChartTip();
  const built = buildTrendChartSvg(d, false);
  const wrap = $('chartWrap'), legend = $('chartLegend');
  if (!built.svg) {
    wrap.innerHTML = '<p class="muted" style="padding:14px;margin:0">当前选择没有可绘制的数据</p>';
    legend.innerHTML = '';
    return;
  }
  wrap.innerHTML = built.svg;
  legend.innerHTML = built.legend;
}



/* ---------------- 手机端 App 底部导航（标签页切换） ---------------- */

const APP_TABS = ['home', 'machines', 'run', 'tasks', 'records'];

function moveNavInd() {
  const nav = $('appNav');
  const ind = nav && nav.querySelector('.nav-ind');
  const btn = nav && nav.querySelector('button.active');
  if (!ind || !btn) return;
  ind.style.width = btn.offsetWidth + 'px';
  ind.style.transform = `translateX(${btn.offsetLeft}px)`;
}
window.addEventListener('resize', moveNavInd);

function setAppTab(tab) {
  if (!APP_TABS.includes(tab)) return;
  buzz(6);
  document.body.dataset.appTab = tab;
  document.querySelectorAll('#appNav button').forEach(btn => {
    const on = btn.dataset.tab === tab;
    btn.classList.toggle('active', on);
    if (on) btn.setAttribute('aria-current', 'page');
    else btn.removeAttribute('aria-current');
  });
  moveNavInd();
  // 重新触发新页面卡片的淡入动画
  const mainEl = document.querySelector('main');
  mainEl.classList.remove('tab-anim');
  void mainEl.offsetWidth;
  mainEl.classList.add('tab-anim');
  // 切换标签页时收起打开中的详情整页
  if (!$('runDetail').classList.contains('hidden')) closeRunDetail();
  if (!$('schedDetail').classList.contains('hidden')) closeSchedDetail();
  window.scrollTo(0, 0);
}

/* 手机端左右滑动切换标签页（避开输入框、横向滚动区与弹层） */
let swipeX = 0, swipeY = 0, swipeT = 0;
document.addEventListener('touchstart', e => {
  swipeT = 0;
  if (window.innerWidth > 720 || !e.touches || e.touches.length !== 1) return;
  if (!$('modal').classList.contains('hidden') || !$('schedModal').classList.contains('hidden')) return;
  if (document.body.classList.contains('app-detail-open')) return;
  if (e.target.closest && e.target.closest('input,textarea,select,.tbl-wrap,#chartWrap,.chart-picker,.segmented,#appNav,a')) return;
  const t = e.touches[0];
  swipeX = t.clientX;
  swipeY = t.clientY;
  swipeT = Date.now();
}, {passive: true});
document.addEventListener('touchend', e => {
  if (!swipeT || Date.now() - swipeT > 700) { swipeT = 0; return; }
  const t = e.changedTouches[0];
  const dx = t.clientX - swipeX, dy = t.clientY - swipeY;
  swipeT = 0;
  if (Math.abs(dx) < 56 || Math.abs(dx) < Math.abs(dy) * 1.6) return;
  const idx = APP_TABS.indexOf(document.body.dataset.appTab || 'home');
  const next = dx < 0 ? idx + 1 : idx - 1;
  if (next >= 0 && next < APP_TABS.length) setAppTab(APP_TABS[next]);
}, {passive: true});

function closeRunDetail() {
  $('runDetail').classList.add('hidden');
  CURRENT_RUN = null;
  clearTimeout(pollTimer);
  document.body.classList.remove('app-detail-open');
}

/* ---------------- 事件委托（所有按钮统一走这里，弹窗取消/关闭稳定可用） ---------------- */

const ACTIONS = {
  openMachineForm: () => openMachineForm(),
  closeModal,
  saveMachine,
  showAgentCmd: (d) => showAgentCmd(d.id),
  editMachine: (d) => editMachine(d.id),
  deleteMachine: (d, el) => deleteMachine(d.id, el),
  discoverMachine: (d) => discoverMachine(d.id),
  updateAgent: (d, el) => updateAgent(d.id, el),
  updateAllAgents: (d, el) => updateAllAgents(el),
  saveAlertSettings: () => saveAlertSettings(),
  testAlert: () => testAlert(),
  exportRunCsv: () => exportRunCsv(),
  exportRunJson: () => exportRunJson(),
  exportSchedCsv: () => exportSchedCsv(),
  regenToken: (d, el) => regenToken(el),
  copyAgentCmd: () => copyText($('agentCmd').textContent, '已复制接入命令'),
  logout,
  startRun: (d, el) => startRun(el),
  loadRuns: () => loadRuns(),
  setRunsFilter: (d) => setRunsFilter(d.filter),
  openRun: (d) => openRun(d.id),
  removeRun: (d, el) => removeRun(d.id, el),
  toggleRunGroup: (d) => toggleRunGroup(d.id),
  copyReport,
  toggleReportView,
  toggleTheme: (d, el) => toggleTheme(el),
  downloadReport,
  stopCurrentRun: (d, el) => stopCurrentRun(el),
  openScheduleForm: () => openScheduleForm(null),
  closeSchedModal,
  saveSchedule: (d, el) => saveSchedule(el),
  editSchedule: (d) => editSchedule(d.id),
  toggleSchedule: (d, el) => toggleSchedule(d.id, el),
  deleteSchedule: (d, el) => deleteSchedule(d.id, el),
  runScheduleNow: (d, el) => runScheduleNow(d.id, el),
  compareSchedule: (d) => compareSchedule(d.id),
  setChartMode: (d) => setChartMode(d.mode),
  setChartDirection: (d) => setChartDirection(d.direction),
  toggleChartMachine: (d) => toggleChartMachine(d.key),
  closeSchedDetail,
  copySchedReport,
  copySchedTable,
  downloadSchedReport,
  setAppTab: (d) => setAppTab(d.tab),
  toggleSchedCard: (d) => toggleSchedCard(d.id),
  gotoRun: () => {
    if (window.innerWidth <= 720) setAppTab('run');
    else $('runCard').scrollIntoView({behavior: 'smooth'});
  },
  closeRunDetail,
};

let LAST_CLICK = null;   // 最近一次点击落点，用于把确认键贴到鼠标旁边

document.addEventListener('click', e => {
  if (Date.now() < MACHINE_DRAG.suppressClickUntil &&
      e.target.closest('#machinesBody')) {
    e.preventDefault();
    e.stopPropagation();
    return;
  }
  const el = e.target.closest('[data-action]');
  if (!el) return;
  LAST_CLICK = { x: e.clientX, y: e.clientY, t: Date.now() };
  const fn = ACTIONS[el.dataset.action];
  if (fn) {
    e.preventDefault();
    fn(el.dataset, el);
  }
});

// 点击遮罩空白处或按 ESC 也可关闭弹窗
$('modal').addEventListener('click', e => {
  if (e.target === $('modal')) closeModal();
});
$('schedModal').addEventListener('click', e => {
  if (e.target === $('schedModal')) closeSchedModal();
});
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  if (!$('schedModal').classList.contains('hidden')) closeSchedModal();
  else if (!$('modal').classList.contains('hidden')) closeModal();
  else if (!$('runDetail').classList.contains('hidden')) closeRunDetail();
  else if (!$('schedDetail').classList.contains('hidden')) closeSchedDetail();
});

$('agentPanelUrl').addEventListener('input', () => {
  if (agentMachine) $('agentCmd').textContent = buildAgentCmd();
});

$('mRole').addEventListener('change', updateRoleHint);

$('targetSelect').addEventListener('change', updateIpv6Ui);
$('backendList').addEventListener('change', updateIpv6Ui);
$('useIpv6').addEventListener('change', () => {
  // 勾选 IPv6 时给出一次显式确认，避免误选后整轮失败
  const cb = $('useIpv6');
  if (!cb.checked) return;
  cb.checked = false;
  showPopconfirm(cb, {
    title: '启用 IPv6 测试？',
    message: '要求所有参与机器都有 IPv6，缺一台该台即失败',
    confirmText: '启用 IPv6',
    onConfirm: async () => {
      cb.checked = true;
      updateIpv6Ui();
    },
  });
});

$('cfgUdp').addEventListener('change', () => {
  // 只有 UDP 需要 -b（TCP 无限速）；关掉时保留填写的值，下次勾选还在
  syncUdpControls('cfgUdp', 'cfgUdpBw');
});

$('cfgAdvPorts').addEventListener('change', () => {
  // 关掉「每台机器单独指定端口」就全部回归目标机端口，避免残留旧值造成对不上
  if (!$('cfgAdvPorts').checked) RUN_PORTS = {};
  renderRunForm();
});

$('backendList').addEventListener('input', e => {
  // 记录每台机器单独改过的端口；列表每 8 秒会重绘，值存在 RUN_PORTS 里不会丢
  const t = e.target;
  if (t && t.classList && t.classList.contains('item-port')) RUN_PORTS[t.dataset.id] = t.value;
});

/* 图表 hover 十字线 / 数值气泡（pointermove 同时覆盖鼠标与触屏拖动） */
const chartBox = $('chartBox');
if (chartBox) {
  chartBox.addEventListener('pointermove', chartTipMove);
  chartBox.addEventListener('pointerleave', hideChartTip);
  chartBox.addEventListener('pointercancel', hideChartTip);
  chartBox.addEventListener('pointerup', e => {
    if (e.pointerType !== 'mouse') setTimeout(hideChartTip, 1400);
  });
}

$('cfgPort').addEventListener('input', () => {
  // 改「默认端口」时，把还没单独改过的机器一起带过去
  const nv = $('cfgPort').value;
  const ov = $('cfgPort').dataset.prev || '5201';
  document.querySelectorAll('#backendList .item-port').forEach(inp => {
    if (inp.value === ov) {
      inp.value = nv;
      delete RUN_PORTS[inp.dataset.id];
    }
  });
  $('cfgPort').dataset.prev = nv;
});

/* ---------------- 定时任务表单交互 ---------------- */

$('sShowAdv').addEventListener('change', () => {
  // 关掉自定义参数就回到「跟随发起测试区域」，避免留下看不出来的旧值
  $('sAdvBox').classList.toggle('hidden', !$('sShowAdv').checked);
  if (!$('sShowAdv').checked) seedScheduleForm(null);
});

$('sUdp').addEventListener('change', () => {
  syncUdpControls('sUdp', 'sUdpBw');
});

$('sLifetimeEnabled').addEventListener('change', () => {
  syncLifetimeControls();
});

/* ---------------- 初始化 ---------------- */

async function init() {
  try {
    applyTheme(localStorage.getItem('fleet-theme')
      || (window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'));
    setAppTab('home');
    syncUdpControls('cfgUdp', 'cfgUdpBw');
    syncUdpControls('sUdp', 'sUdpBw');
    const me = await api('/api/me');
    $('meUser').textContent = me.user ? `👤 ${me.user}` : '';
    await loadMachines();
    await loadRuns();
    await loadSchedules();
    loadAlertSettings();
    const st = await api('/api/status');
    if (st.active_run_id) openRun(st.active_run_id);
    startStream();
    // 以下轮询是 SSE 连不上时的兜底：SSE 正常时它们直接返回，不再发请求
    // 定时任务状态（下次执行倒计时、最近一轮结果）比机器在线状态变得快，单独 10 秒刷一次
    setInterval(async () => {
      if (document.hidden || SSE_OK) return;
      if ($('modal').classList.contains('hidden') && $('schedModal').classList.contains('hidden')) {
        try { await loadSchedules(); } catch (e) {}
      }
    }, 10000);
    // 定时任务在后台自动开跑后，测试记录与对比数据要能自己冒出来
    setInterval(async () => {
      if (document.hidden || SSE_OK) return;
      if ($('modal').classList.contains('hidden') && $('schedModal').classList.contains('hidden')) {
        if (SCHEDULES.some(s => s.last_status === 'running')) {
          try { await loadRuns(); } catch (e) {}
        }
      }
    }, 15000);
    setInterval(async () => {
      if (document.hidden) return;
      if (CURRENT_SCHED && !$('schedDetail').classList.contains('hidden')) {
        const active = SCHEDULES.find(s => s.id === CURRENT_SCHED);
        if (active && ['running', 'waiting'].includes(active.last_status)) {
          try {
            const d = await api(`/api/schedules/${CURRENT_SCHED}/comparison`);
            const sig = JSON.stringify(d);
            if (sig !== LAST_CMP_SIG) {
              LAST_CMP_SIG = sig;
              CURRENT_COMPARISON = d;
              renderScheduleComparison(d);
            }
          } catch (e) {}
        }
      }
    }, 60000);
    // 定时刷新机器在线状态（弹窗打开时暂停，避免打断接入流程）
    setInterval(async () => {
      if (document.hidden || SSE_OK) return;
      if ($('modal').classList.contains('hidden')) {
        try { await loadMachines(); } catch (e) {}
      }
    }, 8000);
  } catch (e) {
    if (e.message !== '未登录') errorToast('面板初始化失败: ' + e.message);
  }
}

/* ---------------- SSE：服务端推「哪些数据变了」 ---------------- */

function startStream() {
  if (!window.EventSource || SSE_SOURCE) return;
  let es;
  try { es = new EventSource('/api/stream'); } catch (e) { SSE_OK = false; return; }
  SSE_SOURCE = es;
  es.addEventListener('state', (ev) => {
    SSE_OK = true;
    let st;
    try { st = JSON.parse(ev.data); } catch (e) { return; }
    // 首帧只记录基线：init() 已经把三份数据拉过一遍
    if (!SSE_PRIMED) { SSE_PRIMED = true; SSE_LAST = st; return; }
    if (st.machines !== SSE_LAST.machines) {
      SSE_LAST.machines = st.machines;
      if ($('modal').classList.contains('hidden')) loadMachines().catch(() => {});
    }
    if (st.runs !== SSE_LAST.runs) {
      SSE_LAST.runs = st.runs;
      loadRuns().catch(() => {});
    }
    if (st.schedules !== SSE_LAST.schedules) {
      SSE_LAST.schedules = st.schedules;
      if ($('modal').classList.contains('hidden') && $('schedModal').classList.contains('hidden')) {
        loadSchedules().catch(() => {});
      }
    }
  });
  es.onerror = () => {
    // EventSource 自己会重连；断开期间由上面的定时轮询兜底
    SSE_OK = false;
    SSE_PRIMED = false;
  };
}
/* ---------------- 异常告警设置 ---------------- */

async function loadAlertSettings() {
  try {
    const s = await api('/api/settings');
    $('alertEnabled').checked = !!s.enabled;
    $('alertWebhook').value = s.webhook || '';
    $('alertCooldown').value = s.cooldown_seconds;
    $('alertLoss').value = s.min_loss_pct;
    $('alertThroughput').value = s.min_throughput_pct;
    $('alertStatus').textContent = s.webhook
      ? (s.enabled ? '已启用：测试结束后按阈值推送' : '已填写地址，但未启用')
      : '未配置 webhook（不会发送）';
  } catch (e) {
    $('alertStatus').textContent = '读取设置失败：' + e.message;
  }
}

async function saveAlertSettings() {
  try {
    await api('/api/settings', {
      method: 'POST',
      body: JSON.stringify({
        enabled: $('alertEnabled').checked,
        webhook: $('alertWebhook').value.trim(),
        cooldown_seconds: $('alertCooldown').value,
        min_loss_pct: $('alertLoss').value,
        min_throughput_pct: $('alertThroughput').value,
      }),
    });
    toast('告警设置已保存', 3000, 'success');
    loadAlertSettings();
  } catch (e) {
    errorToast('保存失败: ' + e.message);
  }
}

async function testAlert() {
  try {
    const r = await api('/api/settings/test-alert', {method: 'POST'});
    toast(`测试告警已发送（HTTP ${r.status}）`, 3500, 'success');
  } catch (e) {
    errorToast('测试失败: ' + e.message);
  }
}

/* ---------------- 导出 ---------------- */

function exportRunCsv() {
  if (!CURRENT_RUN) return errorToast('还没有打开测试记录');
  location.href = `/api/runs/${CURRENT_RUN}/export.csv`;
}

function exportRunJson() {
  if (!CURRENT_RUN) return errorToast('还没有打开测试记录');
  location.href = `/api/runs/${CURRENT_RUN}/export.json`;
}

function exportSchedCsv() {
  if (!CURRENT_SCHED) return errorToast('还没有打开定时任务');
  location.href = `/api/schedules/${CURRENT_SCHED}/export.csv`;
}

init();
