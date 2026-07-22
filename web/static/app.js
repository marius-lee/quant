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
    return `<span class="scan-bar" style="height:${(pct*100).toFixed(0)}%;background:${color}" title="${f.name}: IC=${fmtNum(f.ic,4)}"></span>`;
  }).join('');
  el.innerHTML = `<div class="scan-inner">${bars}</div>`;
}

// ── Factor Heatmap ──
function renderHeatmap(fd) {
  const el = document.getElementById('heatmap-grid');
  if (!el || !fd) return;
  const factors = buildFactorObjs(fd).filter(f => f.ic != null).sort((a, b) => Math.abs(b.ic) - Math.abs(a.ic));
  if (!factors.length) { el.innerHTML = '<div class="empty" style="color:var(--text3);font-size:12px;text-align:center;padding:20px">暂无 IC 数据</div>'; return; }
  const maxAbsIC = Math.max(0.001, ...factors.map(f => Math.abs(f.ic)));
  el.innerHTML = factors.map(f => {
    const intensity = Math.abs(f.ic) / maxAbsIC;
    const hue = f.ic >= 0 ? 120 : 0;
    const color = `hsl(${hue},${(intensity*80).toFixed(0)}%,${(65-intensity*30).toFixed(0)}%)`;
    return `<span class="heatmap-cell" style="background:${color}" title="${f.name}: |IC|=${fmtNum(f.ic,4)}"></span>`;
  }).join('');
}

// ═══════════════════════════════════════════
// OVERVIEW
// ═══════════════════════════════════════════
async function pollOverview() {
  try {
    const [state, perf] = await Promise.all([
      fetchJSON(API + '/state'),
      fetchJSON(API + '/performance')
    ]);
    window._stateData = state;
    window._perfData = perf;
    renderKPIs(perf);
    renderSignals(state);
    updateStatusBar(state);
  } catch (e) { console.warn('poll error:', e.message); }
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
  renderTable('table-signals', signals.slice(0, 10), [
    { key: 'symbol', label: '代码' },
    { key: 'price', label: '最新价' },
    { key: 'shares', label: '股数' },
    { key: 'score', label: '得分' },
    { key: 'industry', label: '行业' },
    { key: 'reason', label: '信号' },
  ], {
    fmtMap: { score: v => fmtNum(v, 3) },
    rank: true
  });
}

function updateStatusBar(state) {
  const dot = document.getElementById('status-dot');
  const txt = document.getElementById('status-text');
  const time = document.getElementById('status-time');
  if (time) time.textContent = new Date().toLocaleTimeString('zh-CN');
  const s = state?.status || 'unknown';
  if (txt) {
    const labels = { pre_market: '盘前', trading: '交易中', post_market: '盘后', closed: '休市', unknown: '未知' };
    txt.textContent = labels[s] || s;
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
  } catch (e) { console.warn('factors error:', e.message); }
}

function renderFactorKPIs(fd) {
  const ics = fd.ic || [];
  const absICs = ics.map(Math.abs);
  const meanAbsIC = absICs.length ? absICs.reduce((a,b)=>a+b)/absICs.length : 0;
  const meanIR = fd.ic_ir?.length ? fd.ic_ir.reduce((a,b)=>a+Math.abs(b))/fd.ic_ir.length : 0;
  setText('kpi-ntotal', fd.n_total ?? ((fd.n_registered||0)+(fd.n_active||0)+(fd.n_candidate||0)+(fd.n_rejected||0)+(fd.n_retired||0)));
  setText('kpi-nregistered', fd.n_registered ?? 0);
  setText('kpi-nactive', fd.n_active ?? 0);
  setText('kpi-ncandidate', fd.n_candidate ?? 0);
  setText('kpi-nrejected', fd.n_rejected ?? 0);
  setText('kpi-nretired', fd.n_retired ?? 0);
  setText('kpi-nmonitoring', fd.n_monitoring ?? 0);
  setText('kpi-nfactors', fd.n_evaluated ?? 0);
  setText('kpi-ic-mean', fmtNum(meanAbsIC, 4));
  setText('kpi-ic-ir', fmtNum(meanIR, 3));
}

function renderICTrend(fd) {
  const el = document.getElementById('chart-ic-trend');
  if (!el) return;
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
    line: { color: 'var(--accent)', width: 2 },
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
    marker: { color: ['var(--accent)', 'var(--warn)', 'var(--down)'] },
  }], { ...bg, margin: { l: 50, r: 20, t: 10, b: 30 }, ...pf }, PLOTLY_CONFIG);
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
        { key: 'group', label: '分组' },
        { key: 'schedule', label: '调度' },
        { key: 'status_label', label: '状态' },
        { key: 'last_run', label: '上次运行' },
        { key: 'cron', label: 'Cron' },
        { key: 'error_msg', label: '错误信息' },
      ]);
      document.getElementById('meta-scheduler').textContent = (data.tasks?.length || 0) + ' 任务';
    }
  } catch (e) { console.warn('scheduler error:', e.message); }
}

// ═══════════════════════════════════════════
// SSE
// ═══════════════════════════════════════════
let _sseRetry = 0, _sseConn = null;
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

// ── Init ──
document.addEventListener('DOMContentLoaded', async () => {
  initTheme();
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
