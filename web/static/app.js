/* ══════════════════════════════════════════════
   盈迹 dashboard — Theme A/B toggle + sidebar nav
   + factor scan line + heatmap + all original features
   ══════════════════════════════════════════════ */

const API = '/api';
const POLL_MS = 5000;
const PLOTLY_CONFIG = { responsive: true, displayModeBar: false };

function plotlyFont() {
  const s = getComputedStyle(document.documentElement);
  return { color: s.getPropertyValue('--text2').trim(), family: '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", sans-serif', size: 11 };
}
function plotlyBg() {
  const s = getComputedStyle(document.documentElement);
  return { paper_bgcolor: s.getPropertyValue('--bg2').trim(), plot_bgcolor: s.getPropertyValue('--bg2').trim() };
}
function plotlyZeroLine() { const s = getComputedStyle(document.documentElement); return s.getPropertyValue('--border').trim(); }

let _chartsRendered = false;
let _portfolioTimer = null;
let _schedulerTimer = null;

// ── Utils ──
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => el.querySelectorAll(sel);
const fmtMoney = (v) => '¥' + Number(v).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
const fmtPct = (v) => { if (v == null || isNaN(v)) return '—'; return (v >= 0 ? '+' : '') + v.toFixed(2) + '%'; };
const fmtNum = (v, d = 2) => { if (v == null || isNaN(v)) return '—'; return v.toFixed(d); };
const clsPnl = (v) => v >= 0 ? 'up' : 'down';

function setText(id, text) { const el = document.getElementById(id); if (el) el.textContent = text; }
function setHTML(id, html) { const el = document.getElementById(id); if (el) el.innerHTML = html; }

// C14 (CODE-REVIEW): XSS 防护 — 所有从 API 进入 innerHTML 的字符串必须 escape.
function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// ── Build factor objects from parallel arrays (API returns [names] + [ics] separately)
function buildFactorObjs(fd) {
  const keys = fd.factor_keys || [];
  const ics = fd.ic || [];
  const irs = fd.ic_ir || [];
  return keys.map((name, i) => ({ name, ic: ics[i] ?? null, ir: irs[i] ?? null }));
}

// ── Theme ──
function initTheme() {
  const saved = localStorage.getItem('theme');
  document.documentElement.setAttribute('data-theme', saved || 'dark');
}
function toggleTheme() {
  const el = document.documentElement;
  const next = el.getAttribute('data-theme') === 'light' ? 'dark' : 'light';
  el.setAttribute('data-theme', next);
  localStorage.setItem('theme', next);
}

// ── Sidebar tooltip (v504: 自定义即时提示, 替代被 overflow 裁切的 ::after) ──
const _tabLabels = {
  overview: '概览', factors: '因子', portfolio: '持仓', performance: '绩效',
  scheduler: '调度', strategies: '策略', systems: '系统'
};
let _tooltipEl = null;
function _ensureTooltip() {
  if (!_tooltipEl) {
    _tooltipEl = document.createElement('div');
    _tooltipEl.className = 'sidebar-tooltip';
    _tooltipEl.hidden = true;
    document.body.appendChild(_tooltipEl);
  }
  return _tooltipEl;
}
function _showTooltip(btn) {
  const tp = _ensureTooltip();
  const tab = btn.getAttribute('data-tab');
  const label = tab ? (_tabLabels[tab] || '') : (btn.title || btn.getAttribute('aria-label') || '');
  if (!label) return;
  tp.textContent = label;
  tp.hidden = false;
  const r = btn.getBoundingClientRect();
  const tw = tp.offsetWidth || 60;
  tp.style.left = (r.right + 10) + 'px';
  tp.style.top = (r.top + r.height / 2 - tp.offsetHeight / 2) + 'px';
}
function _hideTooltip() { if (_tooltipEl) _tooltipEl.hidden = true; }
function initSidebarTooltip() {
  document.querySelector('.sidebar').addEventListener('pointerover', (e) => {
    const b = e.target.closest('.sidebar-tab, .theme-toggle');
    if (b) _showTooltip(b);
  });
  document.querySelector('.sidebar').addEventListener('pointerout', (e) => {
    if (!e.target.closest('.sidebar-tab, .theme-toggle')) _hideTooltip();
  });
}

// ── Sidebar ──
function showTab(name) {
  $$('.tab-content').forEach(t => t.classList.remove('active'));
  $$('.sidebar-tab').forEach(b => b.classList.remove('active'));
  const tp = document.getElementById('tab-' + name);
  if (tp) tp.classList.add('active');
  const bt = document.querySelector(`.sidebar-tab[data-tab="${name}"]`);
  if (bt) bt.classList.add('active');
  const activeTab = name;
  if (activeTab === 'factors' && window._factorData) {
    renderFactorKPIs(window._factorData); renderScanLine(window._factorData);
    renderHeatmap(window._factorData); renderICTrend(window._factorData);
    renderICDecay(window._factorData); renderCorrelation(window._factorData);
  }
  if (activeTab === 'portfolio') {
    loadPortfolio();
    if (!_portfolioTimer) { _portfolioTimer = setInterval(loadPortfolio, POLL_MS); }
  } else {
    if (_portfolioTimer) { clearInterval(_portfolioTimer); _portfolioTimer = null; }
  }
  if (activeTab === 'performance') { loadPerformance(); }
  if (activeTab === 'scheduler') {
    loadScheduler();
    if (_schedulerTimer) clearInterval(_schedulerTimer);
    _schedulerTimer = setInterval(loadScheduler, 15000);
  } else {
    if (_schedulerTimer) { clearInterval(_schedulerTimer); _schedulerTimer = null; }
  }
  if (activeTab === 'overview' && window._perfData) {
    renderPNLChart();
  }
  if (activeTab === 'strategies') { loadStrategies(); }
  if (activeTab === 'systems') { loadSystems();
    if (!_systemsTimer) { _systemsTimer = setInterval(loadSystems, 15000); }
  } else if (_systemsTimer) { clearInterval(_systemsTimer); _systemsTimer = null; }
}

$$('.sidebar-tab').forEach(b => {
  b.addEventListener('click', () => showTab(b.dataset.tab));
});

// ── API helpers ──
async function fetchJSON(url) {
  const r = await fetch(url);
  const body = await r.json();
  if (body && typeof body.error !== 'undefined' && body.error) {
    const err = new Error(body.error.message || 'API error');
    err.code = body.error.code || 'INTERNAL';
    err.details = body.error.details || [];
    throw err;
  }
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return body.data !== undefined ? body.data : body;
}

function renderTable(containerId, rows, cols, opts = {}) {
  const el = document.getElementById(containerId);
  if (!el) return;
  if (!rows || !rows.length) { el.innerHTML = '<div class="empty">暂无数据</div>'; return; }
  let html = '<table><thead><tr>';
  if (opts.rank) html += '<th>#</th>';
  cols.forEach(c => { html += `<th>${c.label}</th>`; });
  html += '</tr></thead><tbody>';
  rows.forEach((r, i) => {
    html += '<tr>';
    if (opts.rank) html += `<td>${i + 1}</td>`;
    cols.forEach(c => {
      let v = r[c.key];
      if (opts.fmtMap && opts.fmtMap[c.key]) v = opts.fmtMap[c.key](v, r);
      else if (v == null) v = '—';
      else v = escapeHtml(v);  // C14: fmtMap 输出视为可信 HTML, 其余一律转义
      html += `<td>${v}</td>`;
    });
    html += '</tr>';
  });
  html += '</tbody></table>';
  el.innerHTML = html;
}

// ── Factor scan line ──
function renderScanLine(fd) {
  const el = document.getElementById('factor-scan');
  if (!el || !fd) return;
  const factors = buildFactorObjs(fd).filter(f => f.ic != null).sort((a, b) => Math.abs(b.ic) - Math.abs(a.ic)).slice(0, 20);
  if (!factors.length) { el.innerHTML = ''; return; }
  const maxAbsIC = Math.max(...factors.map(f => Math.abs(f.ic)), 0.001);
  const bars = factors.map(f => {
    const pct = maxAbsIC > 0 ? Math.abs(f.ic) / maxAbsIC : 0;
    const color = f.ic >= 0 ? 'var(--up)' : 'var(--down)';
    return `<span class="scan-bar" style="height:${(pct*100).toFixed(0)}%;background:${color}" title="${escapeHtml(f.name)}: IC=${fmtNum(f.ic,4)}"></span>`;
  }).join('');
  el.innerHTML = `<div class="scan-inner">${bars}</div>`;
}

// ── Factor Heatmap ──
function renderHeatmap(fd) {
  const el = document.getElementById('heatmap-grid');
  if (!el || !fd) return;
  const factors = buildFactorObjs(fd).filter(f => f.ic != null).sort((a, b) => Math.abs(b.ic) - Math.abs(a.ic));
  // Update count dynamically
  setText('meta-heatmap', factors.length + ' 因子');
  if (!factors.length) { el.innerHTML = '<div class="empty" style="color:var(--text3);font-size:12px;text-align:center;padding:20px">暂无 IC 数据</div>'; return; }
  const maxAbsIC = Math.max(0.001, ...factors.map(f => Math.abs(f.ic)));
  el.innerHTML = factors.map(f => {
    const intensity = Math.abs(f.ic) / maxAbsIC;
    const hue = f.ic >= 0 ? 120 : 0;
    const color = `hsl(${hue},${(intensity*80).toFixed(0)}%,${(65-intensity*30).toFixed(0)}%)`;
    return `<span class="heatmap-cell" style="background:${color}" title="${escapeHtml(f.name)}: |IC|=${fmtNum(f.ic,4)}"></span>`;
  }).join('');
}

// ═══════════════════════════════════════════
// OVERVIEW
// ═══════════════════════════════════════════
async function pollOverview() {
  try {
    const [state, perf, lgb, xgb] = await Promise.all([
      fetchJSON(API + '/state'),
      fetchJSON(API + '/performance'),
      fetchJSON(API + '/lgb').catch(() => null),
      fetchJSON(API + '/xgb').catch(() => null),
    ]);
    window._stateData = state;
    window._perfData = perf;
    renderKPIs(perf);
    renderSignals(state);
    updateStatusBar(state);
    renderAlerts(withSectorAlert(state));   // v513: 初始横幅 (SSE 断线/刷新兜底)
    if (lgb) renderLGB(lgb);
    if (xgb) renderXGB(xgb);
  } catch (e) { console.warn('poll error:', e.message); }
}

// ── v536: 行业暴露告警并入横幅 (pipeline 写入的 broker 状态字段) ──
function withSectorAlert(state) {
  const alerts = Array.isArray(state.alerts) ? state.alerts.slice() : [];
  if (state.sector_exposure_alert) {
    alerts.push({ rule: 'sector_exposure', level: 'warning', msg: '行业暴露: ' + state.sector_exposure_alert });
  }
  return alerts;
}

function renderKPIs(p) {
  const st = window._stateData;
  const totalAsset = (st && st.total_asset != null) ? st.total_asset : p.total_asset;
  const pnlTotal = (st && st.pnl && st.pnl.total != null) ? st.pnl.total : p.total_pnl;
  const capital = (st && st.capital != null) ? st.capital : (p.capital || 0);
  const posVal = (st && st.pos_value != null) ? st.pos_value : ((totalAsset || 0) - (capital || 0));
  setText('kpi-total', fmtMoney(totalAsset));
  setText('kpi-pnl', fmtMoney(pnlTotal));
  const pnlPctEl = document.getElementById('kpi-pnl-pct');
  if (pnlPctEl) {
    const initialCapital = p.initial_capital || 5000;
    const pct = initialCapital > 0 ? (pnlTotal / initialCapital) * 100 : 0;
    pnlPctEl.textContent = fmtPct(pct);
    pnlPctEl.className = 'sub ' + clsPnl(pct);
  }
  setText('kpi-wr', (p.total_sells || 0) === 0 ? '—' : fmtNum(p.win_rate, 1) + '%');
  setText('kpi-count', (p.total_buys || 0) + '/' + (p.total_sells || 0));
  setText('kpi-cash', fmtMoney(capital));
  setText('kpi-posval', fmtMoney(posVal));
}

function renderSignals(state) {
  const signals = state?.signals || [];
  const el = document.getElementById('meta-signals');
  if (el) el.textContent = signals.length + ' 候选';
  renderTable('table-signals', signals.slice(0, 5), [
    { key: 'symbol', label: '代码' },
    { key: 'name', label: '名称' },
    { key: 'price', label: '股价' },
    { key: 'score', label: '得分' },
    { key: 'reason', label: '信号' },
    { key: 'exec_note', label: '状态' },
  ], {
    fmtMap: {
      score: v => fmtNum(v, 2),
      reason: v => {
        if (!v) return '—';
        const ev = escapeHtml(v);
        const parts = ev.split(', ');
        if (parts.length <= 2) return '<span title="' + ev + '">' + ev + '</span>';
        const shown = parts.slice(0, 2).join(', ');
        return '<span title="' + ev + '" class="trunc-reason">' + shown + ', <em>+' + (parts.length - 2) + ' more</em></span>';
      },
      exec_note: v => {
        if (!v) return '<span class="badge badge-blue">待执行</span>';
        const map = { abandoned_sealed: '封死', abandoned_funds: '资金不足', filled: '已成交', engine_skip: '跳过' };
        const label = map[v] || escapeHtml(v);
        const cls = v === 'filled' ? 'badge-green' : 'badge-red';
        return '<span class="badge ' + cls + '">' + label + '</span>';
      }
    },
    rank: true
  });
}

function updateStatusBar(state) {
  const dot = document.getElementById('status-dot');
  const txt = document.getElementById('status-text');
  const time = document.getElementById('status-time');
  if (time) {
    const now = new Date();
    const days = ['周日','周一','周二','周三','周四','周五','周六'];
    time.textContent = now.toLocaleDateString('zh-CN') + ' ' + days[now.getDay()] + ' ' + now.toLocaleTimeString('zh-CN', {hour:'2-digit',minute:'2-digit',second:'2-digit'});
  }
  const s = state?.status || 'unknown';
  const regime = state?.regime;
  const regimeLabels = { bull: '🐂 牛市', sideways: '📊 震荡', bear: '🐻 熊市' };
  const regimeText = regime ? ` | ${regimeLabels[regime] || regime}` : '';
  const sizingPct = state?.regime_sizing ? ` (${Math.round(state.regime_sizing * 100)}%仓位)` : '';
  if (txt) {
    const labels = { pre_market: '盘前', trading: '交易中', post_market: '盘后', closed: '休市', unknown: '未知' };
    txt.textContent = (labels[s] || s) + regimeText + sizingPct;
  }
  if (dot) {
    dot.className = 'dot ' + (s === 'trading' ? 'on' : s === 'pre_market' || s === 'post_market' ? 'warn' : 'off');
  }
}

function renderPNLChart() {
  const el = document.getElementById('chart-pnl');
  if (!el) return;
  const val = parseFloat(el.dataset.pnl) || (window._perfData && window._perfData.total_pnl) || 0;
  const base = parseFloat(el.dataset.base) || (window._perfData && window._perfData.initial_capital) || 5000;
  // Plotly gauge
  const pf = plotlyFont(), bg = plotlyBg();
  const ss = getComputedStyle(document.documentElement);
  const accent = ss.getPropertyValue('--accent').trim();
  const textColor = ss.getPropertyValue('--text').trim();
  const rangeMax = Math.max(base * 0.3, 500);
  const data = [{
    type: 'indicator', mode: 'gauge+number',
    value: val,
    title: { text: '累计 PnL', font: { ...pf, size: 15, color: textColor } },
    gauge: {
      axis: { range: [-rangeMax, rangeMax], tickfont: { ...pf, size: 10 } },
      bar: { color: accent },
      bgcolor: 'rgba(128,128,128,0.05)',
      steps: [
        { range: [-rangeMax, 0], color: 'rgba(248,81,73,0.12)' },
        { range: [0, rangeMax * 0.5], color: 'rgba(63,185,80,0.10)' },
        { range: [rangeMax * 0.5, rangeMax], color: 'rgba(63,185,80,0.22)' },
      ],
    },
    number: { font: { family: pf.family, size: 24, color: textColor }, valueformat: '.2f' },
  }];
  try { Plotly.purge('chart-pnl'); } catch(_) {}
  Plotly.newPlot('chart-pnl', data, { ...bg, margin: { t: 40, b: 20, l: 20, r: 20 } }, PLOTLY_CONFIG);
}


// ═══════════════════════════════════════════
// FACTORS
// ═══════════════════════════════════════════
async function loadFactors() {
  try {
    const fd = await fetchJSON(API + '/factors');
    window._factorData = fd;
    if (fd && fd.factor_keys && fd.factor_keys.length) {
      renderFactorKPIs(fd);
      renderScanLine(fd);
      renderHeatmap(fd);
      renderICTrend(fd);
      renderICDecay(fd);
      renderCorrelation(fd);
    }
    if (fd && fd.registry) renderRegistry(fd);
  } catch (e) { console.warn('factors error:', e.message); }
}

function renderFactorKPIs(fd) {
  const ics = fd.ic || [];
  const absICs = ics.map(Math.abs);
  const meanAbsIC = absICs.length ? absICs.reduce((a,b)=>a+b)/absICs.length : 0;
  const meanIR = fd.ic_ir?.length ? fd.ic_ir.reduce((a,b)=>a+Math.abs(b))/fd.ic_ir.length : 0;
  setText('kpi-ntotal', fd.n_total ?? ((fd.n_active||0)+(fd.n_probation||0)+(fd.n_evaluating||0)+(fd.n_archived||0)+(fd.n_registered||0)));
  setText('kpi-nactive', fd.n_active ?? 0);
  setText('kpi-nprobation', fd.n_probation ?? fd.n_monitoring ?? 0);
  setText('kpi-nevaluating', fd.n_evaluating ?? fd.n_candidate ?? 0);
  setText('kpi-narchived', fd.n_archived ?? (fd.n_retired||0)+(fd.n_rejected||0));
  setText('kpi-nevaluated', fd.n_evaluated ?? fd.n_factors ?? 0);
  setText('kpi-ic-mean', fmtNum(meanAbsIC, 4));
  setText('kpi-ic-ir', fmtNum(meanIR, 3));
}

function renderICTrend(fd) {
  const el = document.getElementById('chart-ic-trend');
  if (!el || !fd) return;
  const factors = buildFactorObjs(fd).filter(f => f.ic != null).sort((a,b)=>Math.abs(b.ic)-Math.abs(a.ic)).slice(0, 8);
  if (!factors.length) return;
  const pf = plotlyFont(), bg = plotlyBg();
  Plotly.newPlot('chart-ic-trend', [{
    type: 'bar', orientation: 'h',
    x: factors.map(f => Math.abs(f.ic)),
    y: factors.map(f => f.name),
    marker: { color: factors.map(f => {
      const s = getComputedStyle(document.documentElement);
      return f.ic >= 0 ? s.getPropertyValue('--up').trim() : s.getPropertyValue('--down').trim();
    }) },  // (2026-07-21 audit M5: resolve CSS vars for Plotly)
  }], { ...bg, margin: { l: 120, r: 20, t: 10, b: 30 }, xaxis: { title: '|IC|', ...pf } }, PLOTLY_CONFIG);
}

function renderICDecay(fd) {
  const el = document.getElementById('chart-ic-decay');
  if (!el || !fd) return;
  const pf = plotlyFont(), bg = plotlyBg();
  let periods = [1, 3, 5], vals = [];
  // Decay is {factor_name: [lag1_ic, lag3_ic, lag5_ic], ...} per-factor dict
  if (fd.decay && typeof fd.decay === 'object' && !fd.decay.periods) {
    const allDecays = Object.values(fd.decay).filter(Array.isArray);
    if (allDecays.length && allDecays[0].length) {
      periods = allDecays[0].map((_, i) => (i + 1) * 2 - 1); // lag 1,3,5,...
      vals = periods.map((_, i) => {
        const atLag = allDecays.map(d => d[i] || 0).filter(v => v !== 0);
        return atLag.length ? atLag.reduce((a,b)=>a+b)/atLag.length : 0;
      });
    }
  } else if (fd.decay) {
    periods = fd.decay.periods || [1,3,5,10,20];
    vals = fd.decay.values || [];
  }
  if (!vals.length || vals.every(v => v === 0)) {
    el.innerHTML = '<div class="empty" style="color:var(--text3);font-size:12px;text-align:center;padding:20px">暂无衰减数据</div>';
    return;
  }
  Plotly.newPlot('chart-ic-decay', [{
    type: 'scatter', mode: 'lines+markers',
    x: periods, y: vals,
    line: { color: getComputedStyle(document.documentElement).getPropertyValue('--accent').trim(), width: 2 },
  }], { ...bg, margin: { l: 50, r: 20, t: 10, b: 30 }, xaxis: { title: '滞后期(日)', ...pf }, yaxis: { title: '均值|IC|', ...pf } }, PLOTLY_CONFIG);
}

function renderCorrelation(fd) {
  const el = document.getElementById('chart-correlation');
  if (!el || !fd || !fd.corr) return;
  const pf = plotlyFont(), bg = plotlyBg();
  const labels = fd.factor_keys || [];
  const z = fd.corr;
  // Replace null/None with 0 for Plotly compatibility
  const zClean = z.map(row => row.map(v => (v == null || isNaN(v)) ? 0 : v));
  Plotly.newPlot('chart-correlation', [{
    type: 'heatmap', z: zClean, x: labels, y: labels,
    colorscale: (() => {
      const s = getComputedStyle(document.documentElement);
      return [[0, s.getPropertyValue('--down').trim()],
              [0.5, s.getPropertyValue('--bg2').trim()],
              [1, s.getPropertyValue('--up').trim()]];
    })(),  // (2026-07-21 audit M5)
    zmin: -1, zmax: 1,
  }], { ...bg, margin: { l: 120, b: 100, t: 10, r: 20 }, xaxis: { tickangle: 45, ...pf }, yaxis: { ...pf } }, PLOTLY_CONFIG);
}

// ═══════════════════════════════════════════
// PORTFOLIO
// ═══════════════════════════════════════════
async function loadPortfolio() {
  try {
    const [positions, state] = await Promise.all([
      fetchJSON(API + '/positions'),
      fetchJSON(API + '/state')
    ]);
    const pos = positions?.positions || [];
    document.getElementById('meta-positions').textContent = pos.length + ' 持仓';
    renderTable('table-positions', pos, [
      { key: 'symbol', label: '代码' },
      { key: 'name', label: '名称' },
      { key: 'shares', label: '股数' },
      { key: 'price', label: '成本' },
      { key: 'current', label: '现价' },
      { key: 'pnl_pct', label: '盈亏%' },
    ], {
      fmtMap: { price: v => fmtNum(v, 2), current: v => fmtNum(v, 2), pnl_pct: v => fmtPct(v) },
      rank: true
    });
    if (pos.length) {
      try {
        const syms = pos.map(p => p.symbol).join(',');
        await fetchJSON(API + '/quotes?symbols=' + syms);
      } catch (e) { console.warn('quotes fetch failed'); }
      renderSectorExposure(pos);
    }
    try {
      const rd = await fetchJSON(API + '/risk?symbols=' + pos.map(p => p.symbol).join(','));
      if (rd) renderRiskExposure(rd);
    } catch (e) {}
    // Stress test
    try {
      const st = await fetchJSON(API + '/stress');
      if (st && st.scenarios) renderStressTest(st);
    } catch (e) {}
  } catch (e) { console.warn('portfolio error:', e.message); }
}

function renderSectorExposure(positions) {
  const el = document.getElementById('chart-exposure-sector');
  if (!el) return;
  const secMap = {};
  positions.forEach(p => {
    const sec = p.industry || p.sector || '其他';
    secMap[sec] = (secMap[sec] || 0) + (p.value || 0);
  });
  const labels = Object.keys(secMap);
  const vals = Object.values(secMap);
  const pf = plotlyFont(), bg = plotlyBg();
  Plotly.newPlot('chart-exposure-sector', [{
    type: 'pie', labels, values: vals, textinfo: 'label+percent',
  }], { ...bg, margin: { t: 10, b: 10 }, ...pf }, PLOTLY_CONFIG);
}

function renderRiskExposure(rd) {
  const el = document.getElementById('chart-exposure-risk');
  if (!el || !rd) return;
  const pf = plotlyFont(), bg = plotlyBg();
  // API returns {summary: {var_95_pct, cvar_95_pct, max_dd_pct}} (2026-07-21 audit M4)
  const s = rd.summary || rd;
  const varPct = s.var_95_pct || s.var || 0;
  const cvarPct = s.cvar_95_pct || s.cvar || 0;
  const mdd = s.max_dd_pct || s.max_drawdown || 0;
  Plotly.newPlot('chart-exposure-risk', [{
    type: 'bar',
    x: ['VaR 95%', 'CVaR 95%', 'MaxDD'],
    y: [varPct, cvarPct, mdd],
    marker: { color: [
      getComputedStyle(document.documentElement).getPropertyValue('--accent').trim(),
      '#e74c3c',
      getComputedStyle(document.documentElement).getPropertyValue('--down').trim(),
    ] },
  }], { ...bg, margin: { l: 50, r: 20, t: 10, b: 30 }, ...pf }, PLOTLY_CONFIG);
}

function renderStressTest(data) {
  const el = document.getElementById('stress-test');
  if (!el || !data.scenarios) return;
  const names = Object.keys(data.scenarios);
  const worst = names.reduce((a, b) => data.scenarios[a].loss_pct > data.scenarios[b].loss_pct ? a : b);
  el.innerHTML = `
    <div class="section-header"><h2>⚠ 压力测试</h2></div>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:8px">
      <div class="kpi"><div class="label">资产</div><div class="value">¥${fmtNum(data.capital)}</div></div>
      <div class="kpi"><div class="label">最严重</div><div class="value" style="color:#e74c3c">${escapeHtml(worst)} ${data.scenarios[worst].loss_pct}%</div></div>
      <div class="kpi"><div class="label">预估损失</div><div class="value" style="color:#e74c3c">¥${fmtNum(data.scenarios[worst].portfolio_loss_est)}</div></div>
    </div>
    ${names.map(n => {
      const s = data.scenarios[n];
      const pct = s.loss_pct;
      const color = pct > 20 ? '#e74c3c' : pct > 10 ? '#eab308' : '#4caf50';
      return '<div style="display:flex;justify-content:space-between;padding:6px 12px;margin:2px 0;background:var(--bg2);border-radius:4px"><span>'+escapeHtml(n)+'</span><span style="color:'+color+'">−'+pct+'%</span><span style="color:var(--text2);font-size:0.85rem">'+escapeHtml(s.description)+'</span></div>';
    }).join('')}
  `;
}

// ═══════════════════════════════════════════
// PERFORMANCE
// ═══════════════════════════════════════════
async function loadPerformance() {
  try {
    const [trades, perf] = await Promise.all([
      fetchJSON(API + '/trades'), fetchJSON(API + '/performance')
    ]);
    const pSection = document.getElementById('stats-performance');
    if (pSection && perf) {
      pSection.innerHTML = `
        <div class="kpi"><div class="label">累计收益</div><div class="value ${clsPnl(perf.total_return_pct||0)}">${fmtPct(perf.total_return_pct)}</div></div>
        <div class="kpi"><div class="label">胜率</div><div class="value">${perf.win_rate != null ? fmtNum(perf.win_rate,1)+'%' : '—'}</div></div>
        <div class="kpi"><div class="label">夏普</div><div class="value">${perf.sharpe != null ? fmtNum(perf.sharpe,2) : '—'}</div></div>
        <div class="kpi"><div class="label">最大回撤</div><div class="value down">${perf.max_drawdown != null ? fmtPct(perf.max_drawdown) : '—'}</div></div>
        <div class="kpi"><div class="label">总交易</div><div class="value">${(perf.total_buys||0)+'/'+(perf.total_sells||0)}</div></div>
        <div class="kpi"><div class="label">总资产</div><div class="value">${fmtMoney(perf.total_asset||0)}</div></div>
      `;
    }
    const sideLabel = { buy: '买入', sell: '卖出' };
    const tradesList = trades?.trades || [];
    renderTable('table-trades', tradesList.slice(0, 50), [
      { key: 'date', label: '日期' },
      { key: 'symbol', label: '代码' },
      { key: 'name', label: '名称' },
      { key: 'side', label: '方向' },
      { key: 'price', label: '价格' },
      { key: 'shares', label: '股数' },
      { key: 'pnl', label: 'PnL' },
      { key: 'pnl_pct', label: '收益%' },
    ], {
      fmtMap: {
        date: v => (v||'').replace('T',' ').slice(0,19),
        side: v => sideLabel[v] || v,
        price: v => fmtNum(v, 2),
        pnl: v => fmtMoney(v),
        pnl_pct: v => fmtPct(v)
      }
    });
  } catch (e) { console.warn('performance error:', e.message); }
  loadBenchmark();
  loadDailyRisk();
  loadBacktestHistory();
}

// ── v536: 基准跟踪 (累计曲线 + 滚动指标) ──
async function loadBenchmark() {
  try {
    const d = await fetchJSON(API + '/benchmark');
    const s = d.summary || d;
    setText('meta-benchmark', s.available ? '' : '无基准数据');
    const lr = d.latest_rolling || s.latest_rolling;
    const kpi = document.getElementById('kpi-benchmark');
    if (kpi) {
      const c = s.cumulative || {};
      kpi.innerHTML = `
        <div class="kpi"><div class="label">策略累计</div><div class="value ${clsPnl(c.strategy_pct||0)}">${fmtPct(c.strategy_pct)}</div></div>
        <div class="kpi"><div class="label">基准累计</div><div class="value ${clsPnl(c.benchmark_pct||0)}">${fmtPct(c.benchmark_pct)}</div></div>
        <div class="kpi"><div class="label">超额 α</div><div class="value ${clsPnl(c.alpha_pct||0)}">${fmtPct(c.alpha_pct)}</div></div>
        <div class="kpi"><div class="label">滚动 α(60d)</div><div class="value ${clsPnl((lr||{}).alpha_60d||0)}">${lr ? fmtPct(lr.alpha_60d) : '—'}</div></div>
        <div class="kpi"><div class="label">滚动 IR(60d)</div><div class="value">${lr ? fmtNum(lr.ir_60d,2) : '—'}</div></div>`;
    }
    const curves = (s.curves || []).map(r => ({
      date: r.date, strategy: r.strategy_equity, benchmark: r.benchmark_equity
    }));
    if (typeof Plotly !== 'undefined' && curves.length > 1) {
      Plotly.newPlot('chart-benchmark', [
        { x: curves.map(r => r.date), y: curves.map(r => r.strategy), name: '策略', type: 'scatter', mode: 'lines', line: { color: '#4e9fff', width: 2 } },
        { x: curves.map(r => r.date), y: curves.map(r => r.benchmark), name: '沪深300', type: 'scatter', mode: 'lines', line: { color: '#f5a623', width: 1.5, dash: 'dot' } },
      ], { margin: { l: 50, r: 12, t: 10, b: 28 }, paper_bgcolor: plotlyBg().paper_bgcolor, plot_bgcolor: plotlyBg().plot_bgcolor, font: plotlyFont(), legend: { orientation: 'h', y: 1.1 } }, PLOTLY_CONFIG);
    }
  } catch (e) { console.warn('benchmark error:', e.message); }
}

// ── v536: 每日 VaR/CVaR 历史 ──
async function loadDailyRisk() {
  try {
    const rows = await fetchJSON(API + '/risk/history');
    if (!Array.isArray(rows) || rows.length === 0) { setText('meta-daily-risk', '无数据'); return; }
    setText('meta-daily-risk', `${rows.length} 天`);
    const dates = rows.map(r => r.date);
    if (typeof Plotly !== 'undefined') {
      Plotly.newPlot('chart-daily-risk', [
        { x: dates, y: rows.map(r => r.var_95_pct), name: 'VaR 95%', type: 'scatter', mode: 'lines+markers', line: { color: '#e5484d', width: 2 }, marker: { size: 4 } },
        { x: dates, y: rows.map(r => r.cvar_95_pct), name: 'CVaR 95%', type: 'scatter', mode: 'lines+markers', line: { color: '#f5a623', width: 2 }, marker: { size: 4 } },
      ], { margin: { l: 50, r: 12, t: 10, b: 28 }, paper_bgcolor: plotlyBg().paper_bgcolor, plot_bgcolor: plotlyBg().plot_bgcolor, font: plotlyFont(), legend: { orientation: 'h', y: 1.1 }, yaxis: { ticksuffix: '%' } }, PLOTLY_CONFIG);
    }
  } catch (e) { console.warn('daily risk error:', e.message); }
}

// ── v536: 回测历史 ──
async function loadBacktestHistory() {
  try {
    const d = await fetchJSON(API + '/backtest/history');
    const runs = d.runs || d || [];
    setText('meta-backtest-history', `${runs.length} runs`);
    renderTable('table-backtest-history', runs, [
      { label: 'run_id', key: 'run_id' }, { label: '区间', key: 'start_date' },
      { label: '结束', key: 'end_date' }, { label: '本金', key: 'capital' },
      { label: 'Sharpe', key: 'sharpe' }, { label: 'CAGR', key: 'cagr' },
      { label: 'MDD', key: 'max_drawdown' }, { label: '状态', key: 'status' },
      { label: '创建', key: 'created_at' },
    ], { fmtMap: {
      capital: v => fmtMoney(v),
      sharpe: v => v == null ? '—' : Number(v).toFixed(3),
      cagr: v => v == null ? '—' : Number(v).toFixed(1) + '%',
      max_drawdown: v => v == null ? '—' : Number(v).toFixed(1) + '%',
      status: v => `<span class="badge">${escapeHtml(v)}</span>`,
      created_at: v => (v||'').replace('T',' ').slice(0,19),
    } });
  } catch (e) { console.warn('backtest history error:', e.message); }
}

// ═══════════════════════════════════════════
// SCHEDULER
// ═══════════════════════════════════════════
async function loadScheduler() {
  try {
    const data = await fetchJSON(API + '/scheduler');
    if (data && data.tasks) {
      renderTable('table-scheduler', data.tasks, [
        { key: 'task', label: '任务' },
        { key: 'schedule', label: '调度' },
        { key: 'status_label', label: '状态' },
        { key: 'last_run', label: '上次运行' },
        { key: 'error_msg', label: '错误信息' },
        { key: 'desc', label: '说明' },
      ], {
        rank: true,
        fmtMap: { status_label: v => v },  // 服务端 _badge() 生成的可信 HTML, 不强转义
      });
      document.getElementById('meta-scheduler').textContent = (data.tasks?.length || 0) + ' 任务';
    }
  } catch (e) { console.warn('scheduler error:', e.message); }
  loadRecon();
}

// ── 日终对账 (OMS recon) ──
async function loadRecon() {
  try {
    const data = await fetchJSON(API + '/recon');
    const meta = document.getElementById('meta-recon');
    if (!data || !data.date) {
      meta.textContent = '暂无对账数据';
      document.getElementById('recon-summary').innerHTML = '';
      renderTable('table-recon', [], []);
      return;
    }
    const brk = data.status === 'break';
    meta.textContent = `${data.date} · ${brk ? '⚠ 异常 ×' + data.breaks : '正常'}`;
    meta.style.color = brk ? 'var(--up)' : 'var(--down)';
    // 汇总卡: 现金两检查 + 持仓统计 + 订单数
    const cashRows = (data.rows || []).filter(r => r.kind === 'cash');
    const posRows = (data.rows || []).filter(r => r.kind === 'position' && r.status !== 'skip');
    const ordRows = (data.rows || []).filter(r => r.kind === 'order');
    const drifted = posRows.filter(r => r.status === 'break').length;
    const eq = cashRows.find(r => r.symbol === 'equity_cross');
    document.getElementById('recon-summary').innerHTML =
      `<span class="recon-chip">持仓 ${posRows.length - drifted}/${posRows.length} 一致</span>` +
      (eq ? `<span class="recon-chip">现金差异 ${eq.drift == null ? '—' : fmtNum(eq.drift, 2)}</span>` : '') +
      `<span class="recon-chip">订单组 ${ordRows.length}</span>`;
    renderTable('table-recon', data.rows, [
      { key: 'kind', label: '类型' },
      { key: 'symbol', label: '标的/检查' },
      { key: 'expected', label: '期望' },
      { key: 'actual', label: '实际' },
      { key: 'drift', label: '差异' },
      { key: 'status', label: '状态' },
    ], {
      fmtMap: {
        kind: v => ({ position: '持仓', cash: '现金', order: '订单' })[v] || v,
        symbol: v => ({ invariant: '现金≥0', equity_cross: '现金×权益', pnl_cross: 'PnL交叉' })[v] || v,
        expected: v => v == null ? '—' : fmtNum(v, 2),
        actual: v => v == null ? '—' : fmtNum(v, 2),
        drift: v => v == null ? '—' : fmtNum(v, 2),
        status: v => v === 'break' ? '<b style="color:var(--down)">异常</b>'
                   : v === 'skip' ? '<span style="color:var(--text2)">跳过</span>' : '正常',
      },
    });
  } catch (e) { console.warn('recon error:', e.message); }
}

// ── 因子策展提交 ──
async function submitCurator() {
  const name = document.getElementById('curator-name').value.trim();
  const expr = document.getElementById('curator-expr').value.trim();
  const source = document.getElementById('curator-source').value.trim();
  const dir = document.getElementById('curator-dir').value;
  const msg = document.getElementById('curator-msg');
  if (!name || !expr || !source) { msg.textContent = '请填写全部字段'; msg.style.color = 'var(--down)'; return; }
  msg.textContent = '提交中...'; msg.style.color = 'var(--text2)';
  try {
    const resp = await fetch(API + '/curator/submit', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, expression: expr, source, direction: dir})
    });
    const data = await resp.json();
    if (data.data && data.data.status === 'submitted') {
      msg.textContent = '已提交, 下次策展时自动评估';
      msg.style.color = 'var(--up)';
    } else {
      msg.textContent = (data.data?.error || data.error?.message || '未知错误');
      msg.style.color = 'var(--down)';
    }
  } catch (e) { msg.textContent = '网络错误'; msg.style.color = 'var(--down)'; }
}

// ═══════════════════════════════════════════
// SSE
// ═══════════════════════════════════════════
let _sseRetry = 0, _sseConn = null;

// ⚠ 告警横幅 (v513) — brokers/alerts SSE 推送, 红色闪烁常驻至解除
function renderAlerts(alerts) {
  const banner = document.getElementById('alert-banner');
  if (!banner) return;
  const list = Array.isArray(alerts) ? alerts.filter(a => a && a.msg) : [];
  if (!list.length) { banner.hidden = true; banner.innerHTML = ''; return; }
  const items = list.map(a => {
    const cls = a.level === 'critical' ? 'alert-critical' : 'alert-warn';
    return `<span class="${cls}">⚠ ${a.msg}</span>`;
  }).join(' &nbsp;|&nbsp; ');
  banner.innerHTML = items + '<span class="alert-close" title="暂时隐藏">✕</span>';
  banner.hidden = false;
  banner.querySelector('.alert-close').addEventListener('click', () => { banner.hidden = true; });
}

function connectSSE() {
  if (_sseConn) _sseConn.close();
  _sseConn = new EventSource(API + '/stream');
  _sseConn.onmessage = (e) => {
    _sseRetry = 0;
    try {
      const state = JSON.parse(e.data);
      if (state) {
        renderSignals(state);
        updateStatusBar(state);
        renderAlerts(withSectorAlert(state));
        window._stateData = state;
      }
    } catch (_) {}
  };
  _sseConn.onerror = () => {
    _sseConn.close();
    const delay = Math.min(5000 * Math.pow(2, Math.min(_sseRetry, 3)), 30000);
    _sseRetry++;
    setTimeout(connectSSE, delay);
  };
}

function renderLGB(d) {
  const statusEl = document.getElementById('lgb-status');
  if (!statusEl) return;
  if (!d.available) {
    setText('lgb-status', '未安装');
    setText('meta-lgb', 'pip install lightgbm');
    return;
  }
  if (!d.trained) {
    setText('lgb-status', '未训练');
    setText('meta-lgb', (d.models || []).length ? d.models.length + ' model(s) on disk' : 'run train_lgb_model()');
    return;
  }
  const enabled = !!d.enabled;
  setHTML('lgb-status', enabled
    ? '<span style="color:var(--up)">● 已启用</span>'
    : '<span style="color:var(--down)">● 就绪 · 未启用</span>');
  if (d.metadata) {
    setText('lgb-ic', (d.metadata.ic_mean || 0).toFixed(4));
    setText('lgb-oos-ic', (d.metadata.oos_ic_mean || 0).toFixed(4));
    setText('lgb-icir', (d.metadata.oos_icir || 0).toFixed(2));
    setText('lgb-samples', fmtNum(d.metadata.n_samples || 0));
    setText('lgb-features', d.metadata.n_features || 0);
    const win = d.metadata.train_start && d.metadata.train_end
      ? ' ' + d.metadata.train_start.slice(0, 10) + ' ~ ' + d.metadata.train_end.slice(0, 10)
      : '';
    setText('meta-lgb', 'trained ' + (d.metadata.train_date || '?') + win
      + ' · OOS ' + (d.metadata.oos_n_days || 0) + '天 · combine_mode=' + (d.combine_mode || '?'));
  }
}

// v421: XGBoost 模型状态 (与 LGB 对称)
function renderXGB(d) {
  const statusEl = document.getElementById('xgb-status');
  if (!statusEl) return;
  if (!d.available) {
    setText('xgb-status', '未安装');
    setText('meta-xgb', 'pip install xgboost');
    return;
  }
  if (!d.trained) {
    setText('xgb-status', '未训练');
    setText('meta-xgb', (d.models || []).length ? d.models.length + ' model(s) on disk' : 'run train_xgb_model()');
    return;
  }
  const enabled = !!d.enabled;
  setHTML('xgb-status', enabled
    ? '<span style="color:var(--up)">● 已启用</span>'
    : '<span style="color:var(--down)">● 就绪 · 未启用</span>');
  if (d.metadata) {
    setText('xgb-ic', (d.metadata.ic_mean || 0).toFixed(4));
    setText('xgb-oos-ic', (d.metadata.oos_ic_mean || 0).toFixed(4));
    setText('xgb-icir', (d.metadata.oos_icir || 0).toFixed(2));
    setText('xgb-samples', fmtNum(d.metadata.n_samples || 0));
    setText('xgb-features', d.metadata.n_features || 0);
    const win = d.metadata.train_start && d.metadata.train_end
      ? ' ' + d.metadata.train_start.slice(0, 10) + ' ~ ' + d.metadata.train_end.slice(0, 10)
      : '';
    setText('meta-xgb', 'trained ' + (d.metadata.train_date || '?') + win
      + ' · OOS ' + (d.metadata.oos_n_days || 0) + '天 · combine_mode=' + (d.combine_mode || '?'));
  }
}

// ── 因子注册表 (v505: 合并原因子平台) ──
function _statusBadge(s) {
  const cls = s === 'active' ? 'badge' :
    s === 'probation' ? 'badge badge-blue' :
    s === 'evaluating' ? 'badge badge-purple' : 'badge badge-gray';
  return `<span class="${cls}">${escapeHtml(s)}</span>`;
}
function renderRegistry(d) {
  setText('meta-registry', `${d.registry.length} 因子 · 唯一真相源 factor_registry (market.db)`);
  renderTable('table-registry', d.registry, [
    { label: '因子', key: 'name' },
    { label: '分类', key: 'category' },
    { label: '状态', key: 'status' },
    { label: '方向', key: 'direction' },
    { label: 'IC', key: 'ic_mean' },
    { label: 'IC_IR', key: 'ic_ir' },
    { label: '来源', key: 'academic_source' },
    { label: '更新时间', key: 'updated_at' },
    { label: '', key: '_lineage_btn' },
  ], {
    fmtMap: {
      status: v => _statusBadge(v),
      ic_mean: v => v == null ? '—' : fmtNum(v, 4),
      ic_ir: v => v == null ? '—' : fmtNum(v, 2),
      direction: v => v == null ? '—' : (v === 'positive' ? '正向' : v === 'negative' ? '负向' : escapeHtml(v)),
      academic_source: v => v ? `<span title="${escapeHtml(v)}">${escapeHtml(String(v).slice(0, 24))}${String(v).length > 24 ? '…' : ''}</span>` : '—',
      updated_at: v => v ? String(v).slice(0, 16) : '—',
      _lineage_btn: (raw, r) => `<button onclick="showLineage('${r.name}')" style="padding:3px 8px;font-size:0.75rem;background:var(--accent-dim);border:1px solid var(--border);border-radius:4px;color:var(--accent);cursor:pointer">血缘</button>`,
    },
  });
}
async function showLineage(name) {
  const panel = document.getElementById('lineage-panel');
  try {
    const d = await fetchJSON('/api/factors/lineage?name=' + encodeURIComponent(name));
    const up = (d.lineage.upstream || []).map(u =>
      `<li>${u.type === 'data' ? '📊' : '🏷️'} <code>${escapeHtml(u.label || u.ref || '')}</code></li>`).join('');
    const down = (d.lineage.downstream || []).map(x => `<li><code>${escapeHtml(x)}</code></li>`).join('');
    panel.innerHTML = `
      <div class="section-header"><h3>血缘: ${escapeHtml(name)}</h3><span class="meta">via fetch /api/factors/lineage</span></div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:8px">
        <div><div style="font-size:0.8rem;color:var(--text2);margin-bottom:4px">上游 (数据依赖)</div>
          <ul style="margin:0;padding-left:18px;font-size:0.85rem;color:var(--text)">${up || '<li style="color:var(--text3)">无推导</li>'}</ul></div>
        <div><div style="font-size:0.8rem;color:var(--text2);margin-bottom:4px">下游 (使用方)</div>
          <ul style="margin:0;padding-left:18px;font-size:0.85rem;color:var(--text)">${down || '<li style="color:var(--text3)">无下游</li>'}</ul></div>
      </div>`;
  } catch (e) {
    panel.innerHTML = `<div class="empty" style="color:var(--text3);font-size:12px">血缘查询失败: ${escapeHtml(e.message)}</div>`;
  }
}

// ── 多策略 (v502) ──
async function loadStrategies() {
  try {
    const g = await fetchJSON('/api/strategy/summary');
    setText('st-total', fmtMoney(g.total_asset ?? g.total_equity ?? 0));
    setText('st-cash', fmtMoney(g.total_cash ?? g.cash ?? 0));
    setText('st-pnl', fmtMoney(g.total_pnl ?? g.pnl ?? 0));
    setText('st-util', ((g.capital_utilization || 0) * 100).toFixed(1) + '%');
    const acts = g.active_strategies || 0;
    setText('st-active', acts);
    setText('meta-strategies', `${acts} active`);
    const rows = Object.entries(g.strategies || {}).map(([name, m]) => ({
      name, status: m.status, ...(m.metrics || {})
    }));
    renderTable('table-strategies', rows, [
      { label: '策略', key: 'name' }, { label: '状态', key: 'status' },
      { label: '持仓市值', key: 'position_value' }, { label: '现金', key: 'available_cash' },
      { label: '日 PnL', key: 'daily_pnl' }, { label: '总 PnL', key: 'total_pnl' },
      { label: '操作', key: '__actions__' },
    ], { fmtMap: { status: v => `<span class="badge">${escapeHtml(v)}</span>`,
                   position_value: v => fmtMoney(v), available_cash: v => fmtMoney(v),
                   daily_pnl: v => fmtMoney(v), total_pnl: v => fmtMoney(v),
                   __actions__: (raw, r) => `
                     <button class="action-btn" onclick="strategyAction('${r.name}','rebalance')">调仓</button>
                     <button class="action-btn" onclick="strategyAction('${r.name}','detail')">详情</button>` } });
    // v536: 详情展开区
    const detailEl = document.getElementById('strategy-detail');
    if (!detailEl) {
      const d = document.createElement('div');
      d.id = 'strategy-detail';
      d.style.cssText = 'margin-top:10px;font-size:0.85rem;color:var(--text2);white-space:pre-wrap';
      document.getElementById('table-strategies').after(d);
    }
  } catch (e) { setText('meta-strategies', '加载失败'); }
  loadSignalQuality();
}

// ── v536: 策略操作 (调仓 / 详情) ──
async function strategyAction(name, action) {
  try {
    if (action === 'detail') {
      const d = await fetchJSON(API + '/strategy/' + encodeURIComponent(name));
      const el = document.getElementById('strategy-detail');
      if (el) el.textContent = JSON.stringify(d, null, 2);
      return;
    }
    const r = await fetch(API + '/strategy/' + encodeURIComponent(name) + '/action', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action }),
    });
    const body = await r.json();
    const el = document.getElementById('strategy-detail');
    if (el) el.textContent = body.error ? '✗ ' + body.error.message : '✓ ' + action + ' 已提交: ' + JSON.stringify(body.data || {});
  } catch (e) {
    const el = document.getElementById('strategy-detail');
    if (el) el.textContent = '操作失败: ' + e.message;
  }
}

// ── v536: 信号质量 (今日 vs 历史) ──
async function loadSignalQuality() {
  try {
    const q = await fetchJSON(API + '/signals/quality');
    setText('meta-sig-quality', q.error ? '不可用' : '');
    const t = q.today || {}, h = q.historical || {}, c = q.comparison || {};
    const kpi = document.getElementById('kpi-sig-quality');
    if (kpi) kpi.innerHTML = `
      <div class="kpi"><div class="label">今日信号</div><div class="value">${t.count ?? '—'}</div></div>
      <div class="kpi"><div class="label">今日均分</div><div class="value">${t.avg_score != null ? fmtNum(t.avg_score,3) : '—'}</div></div>
      <div class="kpi"><div class="label">历史均数(20d)</div><div class="value">${h.avg_count != null ? h.avg_count : '—'}</div></div>
      <div class="kpi"><div class="label">数量偏差</div><div class="value ${clsPnl(c.count_pct||0)}">${c.count_pct != null ? (c.count_pct>=0?'+':'')+c.count_pct+'%' : '—'}</div></div>
      <div class="kpi"><div class="label">均分差</div><div class="value ${clsPnl(c.score_diff||0)}">${c.score_diff != null ? fmtNum(c.score_diff,4) : '—'}</div></div>`;
  } catch (e) { console.warn('signal quality error:', e.message); }
}

// ── 系统: 另类/分布式回测/模型/监控 (v502) ──
async function loadSystems() {
  try {
    const alt = await fetchJSON('/api/alternative/sources');
    setText('meta-alt', `${alt.sources.length} sources · ${alt.factor_rows} factor rows`);
    renderTable('table-alt-sources', alt.sources, [
      { label: '数据源', key: 'name' }, { label: '类型', key: 'source_type' },
      { label: '频率', key: 'frequency' }, { label: '启停', key: 'enabled' },
      { label: '优先级', key: 'priority' }, { label: '日期范围', key: 'range' },
    ], { fmtMap: { enabled: v => v ? '<span style="color:var(--up)">● 启用</span>' : '<span style="color:var(--text3)">● 停用</span>',
                   range: (raw, r) => `${escapeHtml(r.start_date || '?')} ~ ${escapeHtml(r.end_date || '?')}` } });
  } catch (e) { setText('meta-alt', '加载失败'); }

  try {
    const d = await fetchJSON('/api/backtest/dist/status');
    setText('meta-dist', d.recent ? `${d.recent.length} recent` : '—');
    setText('dist-state', d.running ? '<span style="color:var(--up)">运行中</span>' : '空闲');
    setText('dist-progress', `${d.done}/${d.total}`);
    setText('dist-runid', d.run_id || '—');
    setText('dist-err', d.error ? escapeHtml(d.error) : '—');
    renderTable('table-dist-results', d.recent || [], [
      { label: 'run_id', key: 'run_id' }, { label: '区间', key: 'range' },
      { label: '本金', key: 'capital' }, { label: 'Sharpe', key: 'sharpe' },
      { label: 'CAGR', key: 'cagr' }, { label: 'MDD', key: 'mdd' },
      { label: '终值', key: 'equity' }, { label: '耗时', key: 'elapsed' },
    ], { fmtMap: { range: (raw, r) => `${escapeHtml(r.start)} ~ ${escapeHtml(r.end)}`,
                   capital: v => fmtMoney(v), equity: v => fmtMoney(v),
                   sharpe: v => v == null ? '—' : Number(v).toFixed(3),
                   cagr: v => v == null ? '—' : Number(v).toFixed(1) + '%',
                   mdd: v => v == null ? '—' : Number(v).toFixed(1) + '%',
                   elapsed: v => v == null ? '—' : Number(v).toFixed(0) + 's' } });
  } catch (e) { setText('meta-dist', '加载失败'); }

  try {
    const m = await fetchJSON('/api/model/serving');
    document.getElementById('model-serving-state').innerHTML =
      m.available
        ? `<div class="empty">MLflow 可用 · ${m.models.length} 个模型 ${m.reason ? '· ' + escapeHtml(m.reason) : ''}</div>`
        : `<div class="empty">${escapeHtml(m.reason || '模型服务不可用')}</div>`;
  } catch (e) {
    document.getElementById('model-serving-state').innerHTML = '<div class="empty">模型服务查询失败</div>';
  }

  try {
    const g = await fetchJSON('/api/monitoring/grafana');
    const gEl = document.getElementById('mon-grafana');
    if (gEl) gEl.innerHTML = g.running
      ? `<a href="${g.url}" target="_blank" style="color:var(--up)">● 运行中</a>`
      : '<span style="color:var(--text3)">● 未运行</span>';
    setText('mon-hint', g.hint || '');
    let prom = null;
    try { prom = await fetchJSON('/api/monitoring/prometheus'); } catch (e) { /* stats optional */ }
    const mEl = document.getElementById('mon-metrics');
    if (mEl) {
      const p3000 = g.prometheus_running;
      const status = prom
        ? `${prom.count} 条指标序列` + (p3000 ? ' · Prometheus 9090 运行中' : ' · Prometheus 9090 未运行')
        : (p3000 ? 'Prometheus 9090 运行中' : 'Prometheus 9090 未运行');
      const links = g.running
        ? ` <a href="${g.url}" target="_blank" style="color:var(--accent)">面板</a> · <a href="http://localhost:9090" target="_blank" style="color:var(--accent)">Prometheus</a>`
        : '';
      mEl.innerHTML = `${escapeHtml(status)} <a href="/metrics" target="_blank" style="color:var(--accent)">查看</a>${links}`;
    }
  } catch (e) { /* ignore */ }

  // ── v536: 数据源摘要 + 指标快照 + 评估历史 + phase8 ──
  try {
    const ds = await fetchJSON('/api/monitoring/datasources');
    const pEl = document.getElementById('mon-datasources');
    if (pEl) pEl.textContent = `Prometheus ${ds.prometheus.configured ? '已配置' : '未配置'} (${ds.prometheus.url}) · Grafana ${ds.grafana.configured ? '已配置' : '未配置'} (${ds.grafana.url})`;
  } catch (e) { /* ignore */ }

  try {
    const m = await fetchJSON(API + '/metrics');
    const rows = Object.entries(m.counters || {}).map(([name, v]) => ({ name, type: 'counter', value: v }))
      .concat(Object.entries(m.gauges || {}).map(([name, v]) => ({ name, type: 'gauge', value: v })));
    setText('meta-metrics', `${rows.length} 项`);
    renderTable('table-metrics', rows, [
      { label: '指标', key: 'name' }, { label: '类型', key: 'type' }, { label: '值', key: 'value' },
    ], { fmtMap: { type: v => `<span class="badge">${escapeHtml(v)}</span>`, value: v => fmtNum(v, 3) } });
  } catch (e) { setText('meta-metrics', '无指标'); }

  try {
    const e = await fetchJSON(API + '/evaluations');
    const runs = e.runs || [];
    setText('meta-evals', `${runs.length} runs`);
    renderTable('table-evals', runs, [
      { label: 'run_id', key: 'id' }, { label: '时间', key: 'run_ts' },
      { label: 'Phase', key: 'phase' }, { label: '因子数', key: 'n_factors' },
      { label: '通过', key: 'n_passed' },
    ], { fmtMap: { run_ts: v => (v||'').replace('T',' ').slice(0,19) } });
  } catch (e) { setText('meta-evals', '无记录'); }

  loadPhase8();
}

// ── v536: phase8 一致性报告 ──
async function loadPhase8() {
  try {
    const d = await fetchJSON(API + '/phase8');
    setText('meta-phase8', d.error ? '不可用' : (d.status || ''));
    const el = document.getElementById('phase8-report');
    if (!el) return;
    if (d.error) { el.textContent = '查询失败: ' + d.error.message; return; }
    if (d.status === 'not_available') { el.textContent = d.message || '尚无报告'; return; }
    const dims = d.dimensions || {};
    let html = `<div>状态: <b style="color:${d.status==='ok'?'var(--up)':'var(--down)'}">${escapeHtml(d.status)}</b> · 综合得分: <b>${d.overall_score ?? '—'}</b> · 实盘区间: ${escapeHtml((d.live_date_range||[]).join(' ~ '))}</div>`;
    const dimNames = { D1: '信号一致性', D2: '收益一致性', D3: '持仓一致性', D4: '成本一致性' };
    for (const [k, v] of Object.entries(dims)) {
      const vd = v || {};
      html += `<div style="margin-top:6px">• ${escapeHtml(dimNames[k]||k)}: <span style="color:${vd.pass?'var(--up)':'var(--down)'}">${vd.pass ? '✓' : '✗'}</span> ${escapeHtml(String(vd.detail ?? vd.message ?? ''))} (匹配率 ${vd.match_rate != null ? (vd.match_rate*100).toFixed(1)+'%' : '—'})</div>`;
    }
    el.innerHTML = html;
  } catch (e) { setText('meta-phase8', '加载失败'); }
}

async function rerunPhase8() {
  const msg = document.getElementById('phase8-msg');
  if (msg) msg.textContent = '重跑中 (可耗时数分钟)…';
  try {
    const d = await fetchJSON(API + '/phase8?rerun=1');
    if (msg) msg.textContent = d.error ? '✗ ' + d.error.message : '✓ 完成';
    await loadPhase8();
  } catch (e) {
    if (msg) msg.textContent = '重跑失败: ' + e.message;
  }
}

async function submitDistGrid() {
  const raw = document.getElementById('dist-cfg').value.trim();
  let grid = {};
  if (raw) { try { grid = JSON.parse(raw); } catch (e) {
    document.getElementById('dist-msg').textContent = 'JSON 解析失败: ' + e.message; return; } }
  const payload = {
    param_grid: grid,
    fixed_params: { start_date: '2024-01-01', end_date: '2025-12-31', capital: 5000, universe_size: 100, retrain_freq: 20, combine_mode: 'ic_weighted', method: 'ic_weighted' },
    backend: 'thread', n_workers: 2,
  };
  const r = await fetch('/api/backtest/dist/submit', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const body = await r.json();
  document.getElementById('dist-msg').textContent = body.error
    ? '✗ ' + body.error.message : `已提交 ${body.data.run_id} · 共 ${body.data.total} 组`;
  if (!body.error) setTimeout(loadSystems, 3000);
}

// ── Init ──
document.addEventListener('DOMContentLoaded', async () => {
  initTheme();
  initSidebarTooltip();
  // version already rendered by server-side template — no JS needed
  document.getElementById('theme-toggle').addEventListener('click', toggleTheme);
  connectSSE();
  await pollOverview();
  setInterval(pollOverview, POLL_MS);
  // PnL 直接更新 (test-v92: 不再依赖 Plotly gauge)
  renderPNLChart();
  const checkPlotly = () => {
    if (typeof Plotly !== 'undefined' && !_chartsRendered) {
      _chartsRendered = true;
      // Plotly loaded — PnL already rendered above
    } else if (!_chartsRendered) { setTimeout(checkPlotly, 200); }
  };
  setTimeout(checkPlotly, 100);
  loadFactors();
});
