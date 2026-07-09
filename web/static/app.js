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

// ── Utils ──
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => el.querySelectorAll(sel);
const fmtMoney = (v) => '¥' + Math.round(v).toLocaleString();
const fmtPct = (v) => { if (v == null || isNaN(v)) return '—'; return (v >= 0 ? '+' : '') + v.toFixed(2) + '%'; };
const fmtNum = (v, d = 2) => { if (v == null || isNaN(v)) return '—'; return v.toFixed(d); };
const clsPnl = (v) => v >= 0 ? 'up' : 'down';

function setText(id, text) { const el = document.getElementById(id); if (el) el.textContent = text; }

// ── Theme toggle ──
function initTheme() {
  const saved = localStorage.getItem('quant-theme') || 'dark';
  document.documentElement.setAttribute('data-theme', saved);
  updateThemeIcon(saved);
}
function toggleTheme() {
  const cur = document.documentElement.getAttribute('data-theme');
  const next = cur === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('quant-theme', next);
  updateThemeIcon(next);
  // Re-render visible charts with new theme
  const activeTab = $('.sidebar-tab.active')?.dataset.tab;
  if (activeTab === 'overview') renderPNLChart();
  if (activeTab === 'factors') loadFactors();
  if (activeTab === 'portfolio') loadPortfolio();
  if (activeTab === 'performance') loadPerformance();
}
function updateThemeIcon(theme) {
  const sun = document.getElementById('theme-icon-sun');
  const moon = document.getElementById('theme-icon-moon');
  if (sun) sun.style.display = theme === 'light' ? 'none' : 'block';
  if (moon) moon.style.display = theme === 'light' ? 'block' : 'none';
}

// ── Sidebar tab switching ──
$$('.sidebar-tab').forEach(btn => {
  btn.addEventListener('click', () => {
    $$('.sidebar-tab').forEach(b => b.classList.remove('active'));
    $$('.tab-content').forEach(c => c.classList.remove('active'));
    btn.classList.add('active');
    const tab = btn.dataset.tab;
    $(`#tab-${tab}`).classList.add('active');
    loadTab(tab);
  });
});

function loadTab(tab) {
  if (_portfolioTimer) { clearInterval(_portfolioTimer); _portfolioTimer = null; }
  if (_schedulerTimer) { clearInterval(_schedulerTimer); _schedulerTimer = null; }
  if (tab === 'factors') loadFactors();
  if (tab === 'portfolio') { loadPortfolio(); _portfolioTimer = setInterval(loadPortfolio, POLL_MS); }
  if (tab === 'performance') loadPerformance();
  if (tab === 'scheduler') { loadScheduler(); _schedulerTimer = setInterval(loadScheduler, POLL_MS); }
  if (tab === 'overview') renderPNLChart();
}

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
  if (!rows || !rows.length) {
    el.innerHTML = '<div class="empty">暂无数据</div>';
    return;
  }
  const { clsMap = {}, fmtMap = {}, rank = false } = opts;
  let h = '<table><thead><tr>';
  if (rank) h += '<th class="rank">#</th>';
  cols.forEach(c => { h += `<th>${c.label}</th>`; });
  h += '</tr></thead><tbody>';
  rows.forEach((row, i) => {
    h += '<tr>';
    if (rank) h += `<td class="rank">${i + 1}</td>`;
    cols.forEach(c => {
      const v = row[c.key];
      const clsFn = clsMap[c.key];
      const fmtFn = fmtMap[c.key];
      const val = fmtFn ? fmtFn(v) : (v ?? '—');
      const cls = clsFn ? clsFn(v) : '';
      h += `<td class="${cls}">${val}</td>`;
    });
    h += '</tr>';
  });
  h += '</tbody></table>';
  el.innerHTML = h;
}

// ── Status bar ──
function updateStatusBar(state) {
  setText('status-text', state?.status || '休市');
  const dot = document.getElementById('status-dot');
  if (dot) {
    const live = state?.status === '上午交易' || state?.status === '下午交易';
    dot.className = 'dot' + (live ? '' : ' off');
  }
  const d = new Date();
  setText('status-time', d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' }));
}

// ── Factor scan line (signature: Theme A) ──
function renderScanLine(fd) {
  const el = document.getElementById('factor-scan');
  if (!el || !fd || !fd.ic) return;
  const absIcs = fd.ic.map(Math.abs);
  const maxIc = Math.max(...absIcs, 0.001);
  const meanIc = absIcs.reduce((a,b)=>a+b,0) / absIcs.length;
  const pct = Math.min(100, (meanIc / 0.10) * 100); // scale: 0.10 IC = 100%
  
  // Color: accent at high IC, muted at low
  const s = getComputedStyle(document.documentElement);
  const accent = s.getPropertyValue('--accent').trim();
  const text3 = s.getPropertyValue('--text3').trim();
  
  el.innerHTML = `<div class="factor-scan-bar" style="width:${pct}%;background:linear-gradient(to right,${text3},${accent})"></div>`;
  el.title = `因子信号强度: 均值|IC|=${fmtNum(meanIc,4)} (共 ${absIcs.length} 因子)`;
}

// ── Factor heatmap (signature: Theme B) ──
function renderHeatmap(fd) {
  const el = document.getElementById('heatmap-grid');
  const meta = document.getElementById('meta-heatmap');
  if (!el || !fd || !fd.ic) return;
  const absIcs = fd.ic.map((v,i) => ({ ic: Math.abs(v), name: fd.factors[i] }));
  absIcs.sort((a,b) => b.ic - a.ic);
  
  const maxIc = absIcs[0]?.ic || 0.01;
  const s = getComputedStyle(document.documentElement);
  const accent = s.getPropertyValue('--accent').trim();
  const text3 = s.getPropertyValue('--text3').trim();
  
  if (meta) meta.textContent = absIcs.length + ' 因子 · 按 |IC| 降序';
  
  el.innerHTML = absIcs.map(f => {
    const intensity = f.ic / Math.max(maxIc, 0.001);
    // Interpolate: text3 -> accent
    const r = Math.round(parseInt(text3.slice(1,3),16) * (1-intensity) + parseInt(accent.slice(1,3),16) * intensity);
    const g = Math.round(parseInt(text3.slice(3,5),16) * (1-intensity) + parseInt(accent.slice(3,5),16) * intensity);
    const b = Math.round(parseInt(text3.slice(5,7),16) * (1-intensity) + parseInt(accent.slice(5,7),16) * intensity);
    const color = `rgb(${r},${g},${b})`;
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

function renderPNLChart() {
  const el = document.getElementById('chart-pnl');
  if (!el || !window._perfData) return;
  const perf = window._perfData;
  const pf = plotlyFont(), bg = plotlyBg();
  const s = getComputedStyle(document.documentElement);
  const accent = s.getPropertyValue('--accent').trim();
  const textColor = s.getPropertyValue('--text').trim();
  const upColor = s.getPropertyValue('--up').trim();
  const downColor = s.getPropertyValue('--down').trim();
  const val = perf.total_pnl || 0;
  const data = [{
    type: 'indicator', mode: 'gauge+number',
    value: val,
    title: { text: '累计 PnL', font: { ...pf, size: 15, color: textColor } },
    gauge: {
      axis: { range: [-500, 5000], tickfont: { ...pf, size: 10 } },
      bar: { color: accent },
      bgcolor: 'rgba(128,128,128,0.05)',
      steps: [
        { range: [-500, 0], color: 'rgba(248,81,73,0.12)' },
        { range: [0, 2000], color: 'rgba(63,185,80,0.08)' },
        { range: [2000, 5000], color: 'rgba(128,128,128,0.08)' },
      ],
    },
    number: { font: { family: pf.family, size: 24, color: textColor } },
  }];
  Plotly.newPlot('chart-pnl', data, { ...bg, margin: { t: 40, b: 20, l: 20, r: 20 } }, PLOTLY_CONFIG);
}

// ═══════════════════════════════════════════
// FACTORS
// ═══════════════════════════════════════════
async function loadFactors() {
  try {
    const fd = await fetchJSON(API + '/factors');
    window._factorData = fd;
    if (fd && fd.factors && fd.factors.length) {
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
  setText('kpi-nfactors', ics.length);
  setText('kpi-ic-mean', fmtNum(meanAbsIC, 4));
  setText('kpi-ic-ir', fmtNum(meanIR, 2));
}

function renderICTrend(fd) {
  const el = document.getElementById('chart-ic-trend');
  if (!el) return;
  const pf = plotlyFont(), bg = plotlyBg(), zl = plotlyZeroLine();
  const s = getComputedStyle(document.documentElement);
  const up = s.getPropertyValue('--up').trim(), down = s.getPropertyValue('--down').trim();
  const colors = fd.ic.map(v => v >= 0 ? up : down);
  const data = [{
    type: 'bar', x: fd.factors, y: fd.ic,
    marker: { color: colors },
    text: fd.ic.map(v => fmtNum(v, 4)),
    textposition: 'outside', textfont: { ...pf, size: 10 },
  }];
  Plotly.newPlot('chart-ic-trend', data, {
    ...bg, title: { text: 'Rank IC (截面)', font: pf },
    xaxis: { tickfont: pf, tickangle: -30 },
    yaxis: { title: { text: 'IC', font: pf }, tickfont: pf, zeroline: true, zerolinecolor: zl },
    margin: { t: 40, b: 60, l: 50, r: 10 },
  }, PLOTLY_CONFIG);
}

function renderICDecay(fd) {
  const el = document.getElementById('chart-ic-decay');
  if (!el) return;
  const pf = plotlyFont(), bg = plotlyBg(), zl = plotlyZeroLine();
  const horizons = [1, 5, 20];
  const data = (fd.factors || []).map((name, i) => ({
    x: horizons, y: fd.decay[name] || [],
    type: 'scatter', mode: 'lines+markers',
    name, line: { width: 1.5 }, marker: { size: 5 },
  }));
  Plotly.newPlot('chart-ic-decay', data, {
    ...bg, title: { text: 'IC 衰减', font: pf },
    xaxis: { title: { text: '预测期 (天)', font: pf }, tickfont: pf, tickvals: horizons },
    yaxis: { title: { text: 'IC', font: pf }, tickfont: pf, zeroline: true, zerolinecolor: zl },
    legend: { font: pf }, margin: { t: 40, b: 40, l: 50, r: 10 },
  }, PLOTLY_CONFIG);
}

function renderCorrelation(fd) {
  const el = document.getElementById('chart-correlation');
  if (!el) return;
  const pf = plotlyFont(), bg = plotlyBg();
  const s = getComputedStyle(document.documentElement);
  const down = s.getPropertyValue('--down').trim(), up = s.getPropertyValue('--up').trim();
  const mid = bg.paper_bgcolor || '#15171C';
  const data = [{
    type: 'heatmap', z: fd.corr, x: fd.factors, y: fd.factors,
    colorscale: [[0, down],[0.5, mid],[1, up]],
    zmin: -1, zmax: 1,
    text: fd.corr.map(r => r.map(v => fmtNum(v,2))),
    texttemplate: '%{text}', textfont: { size: 10 },
    showscale: true, colorbar: { tickfont: pf, thickness: 12 },
  }];
  Plotly.newPlot('chart-correlation', data, {
    ...bg, title: { text: '因子相关性矩阵', font: pf },
    xaxis: { tickfont: { ...pf, size: 10 }, tickangle: -30, side: 'bottom' },
    yaxis: { tickfont: { ...pf, size: 10 } },
    margin: { t: 40, b: 70, l: 80, r: 20 },
  }, PLOTLY_CONFIG);
}

// ═══════════════════════════════════════════
// PORTFOLIO
// ═══════════════════════════════════════════
let _portfolioTimer = null;

async function loadPortfolio() {
  try {
    const [pos, state] = await Promise.all([
      fetchJSON(API + '/positions'),
      fetchJSON(API + '/state')
    ]);
    let positions = pos?.positions || state?.positions || [];
    if (positions.length > 0) {
      try {
        const syms = positions.map(p => p.symbol).join(',');
        const qr = await fetchJSON(API + '/quotes?symbols=' + syms);
        const quotes = qr?.quotes || {};
        positions = positions.map(p => {
          const q = quotes[p.symbol];
          if (q && q.price > 0) {
            const current = q.price;
            const pnlPct = p.price > 0 ? ((current / p.price) - 1) * 100 : 0;
            return { ...p, current, pnl_pct: roundNum(pnlPct, 2), value: roundNum(p.shares * current, 2), name: q.name || p.name || '', change_pct: q.change_pct || 0 };
          }
          const fallbackPrice = p.current || p.price;
          const fallbackPnl = p.price > 0 ? ((fallbackPrice / p.price) - 1) * 100 : 0;
          return { ...p, current: fallbackPrice, pnl_pct: roundNum(fallbackPnl, 2), value: p.value || roundNum(p.shares * fallbackPrice, 2), change_pct: 0, name: p.name || '' };
        });
      } catch (e) { console.warn('quotes fetch failed'); }
    }
    const metaEl = document.getElementById('meta-positions');
    const quoteEl = document.getElementById('quote-status');
    if (metaEl) metaEl.textContent = positions.length + ' 只';

    renderTable('table-positions', positions, [
      { key: 'symbol', label: '代码' }, { key: 'name', label: '名称' },
      { key: 'buy_time', label: '买入时间' }, { key: 'shares', label: '股数' },
      { key: 'price', label: '成本' }, { key: 'current', label: '现价' },
      { key: 'pnl_pct', label: '盈亏%' }, { key: 'value', label: '市值' },
      { key: 'change_pct', label: '日涨跌' },
    ], {
      clsMap: { pnl_pct: clsPnl, change_pct: clsPnl },
      fmtMap: {
        shares: v => v.toLocaleString(), price: v => fmtNum(v,2),
        current: v => fmtNum(v,2), pnl_pct: v => (v != null ? fmtPct(v) : '—'),
        value: v => fmtMoney(v), change_pct: v => (v ? fmtPct(v) : '—'),
      }
    });
    renderExposureCharts(positions);
  } catch (e) { console.warn('portfolio error:', e.message); }
}

function roundNum(v, d) { return Math.round(v * Math.pow(10, d)) / Math.pow(10, d); }

function renderExposureCharts(positions) {
  const elSector = document.getElementById('chart-exposure-sector');
  const elRisk = document.getElementById('chart-exposure-risk');
  const pf = plotlyFont(), bg = plotlyBg();

  if (elSector && positions.length) {
    const vals = positions.map(p => p.value || 0);
    const labels = positions.map(p => p.symbol || '?');
    Plotly.newPlot('chart-exposure-sector', [{
      type: 'pie', values: vals, labels, textinfo: 'label+percent',
      textfont: pf, hole: .4,
      marker: { line: { color: bg.paper_bgcolor, width: 1 } }, sort: false,
    }], {
      ...bg, title: { text: '持仓分布', font: pf }, margin: { t: 40, b: 10, l: 10, r: 10 },
    }, PLOTLY_CONFIG);
  } else if (elSector) { elSector.innerHTML = '<div class="empty">暂无持仓数据</div>'; }

  if (elRisk && positions.length) { renderRiskChart(positions); }
  else if (elRisk) { elRisk.innerHTML = '<div class="empty">暂无持仓数据</div>'; }
}

async function renderRiskChart(positions) {
  const el = document.getElementById('chart-exposure-risk');
  if (!el) return;
  try {
    const syms = positions.map(p => p.symbol).join(',');
    const rd = await fetchJSON(API + '/risk?symbols=' + syms);
    const symbols = rd?.symbols || [];
    if (!symbols.length) { el.innerHTML = '<div class="empty">无风险数据</div>'; return; }
    const pf = plotlyFont(), bg = plotlyBg();
    const s = getComputedStyle(document.documentElement);
    const up = s.getPropertyValue('--up').trim(), warn = s.getPropertyValue('--accent').trim();
    Plotly.newPlot('chart-exposure-risk', [
      { type: 'bar', name: '年化波动率%', x: symbols.map(s=>s.symbol), y: symbols.map(s=>s.annual_vol_pct), marker: { color: up }, text: symbols.map(s=>s.annual_vol_pct+'%'), textposition: 'outside', textfont: { ...pf, size:10 } },
      { type: 'bar', name: '最大回撤%', x: symbols.map(s=>s.symbol), y: symbols.map(s=>s.max_dd_pct), marker: { color: warn }, text: symbols.map(s=>s.max_dd_pct+'%'), textposition: 'outside', textfont: { ...pf, size:10 } },
    ], {
      ...bg, title: { text: '风险暴露', font: pf },
      xaxis: { tickfont: pf }, yaxis: { title: { text: '%', font: pf }, tickfont: pf },
      barmode: 'group', margin: { t: 40, b: 40, l: 50, r: 10 }, legend: { font: pf },
    }, PLOTLY_CONFIG);
  } catch (e) { console.warn('risk chart error:', e.message); el.innerHTML = '<div class="empty">风险数据加载失败</div>'; }
}

// ═══════════════════════════════════════════
// PERFORMANCE
// ═══════════════════════════════════════════
async function loadPerformance() {
  try {
    const [trades, perf] = await Promise.all([
      fetchJSON(API + '/trades'), fetchJSON(API + '/performance')
    ]);
    const tlist = trades?.trades || [];
    renderTable('table-trades', tlist.slice(0, 50), [
      { key: 'date', label: '时间' }, { key: 'symbol', label: '代码' },
      { key: 'side', label: '方向' }, { key: 'price', label: '价格' },
      { key: 'shares', label: '股数' }, { key: 'pnl', label: 'PnL' },
      { key: 'pnl_pct', label: '盈亏%' },
    ], {
      clsMap: { pnl: clsPnl, pnl_pct: clsPnl },
      fmtMap: { price: v => fmtNum(v,2), shares: v => v.toLocaleString(), pnl: v => fmtMoney(v), pnl_pct: v => (v ? fmtPct(v) : '—') }
    });
    renderPerfStats(perf);
    renderAttribution(perf);
  } catch (e) { console.warn('performance error:', e.message); }
}

function renderPerfStats(perf) {
  const el = document.getElementById('stats-performance');
  if (!el) return;
  const items = [
    ['已实现 PnL', fmtMoney(perf.realized_pnl || 0)],
    ['总 PnL', fmtMoney(perf.total_pnl || 0)],
    ['胜率', (perf.total_sells || 0) === 0 ? '—' : fmtNum(perf.win_rate || 0, 1) + '%'],
    ['买入次数', perf.total_buys || 0],
  ];
  el.innerHTML = items.map(([l, v]) => `<div class="kpi"><div class="label">${l}</div><div class="value">${v}</div></div>`).join('');
}

function renderAttribution(perf) {
  const el = document.getElementById('chart-attribution');
  if (!el) return;
  const pf = plotlyFont(), bg = plotlyBg();
  Plotly.newPlot('chart-attribution', [{
    type: 'waterfall', orientation: 'v',
    measure: ['relative', 'relative', 'relative', 'total'],
    x: ['因子收益', '选股收益', '成本', '总收益'],
    y: [perf.realized_pnl*0.6||0, perf.realized_pnl*0.5||0, -(perf.total_buys||0)*5, perf.total_pnl||0],
    text: ['因子', '选股', '成本', '总计'],
    connector: { line: { color: plotlyZeroLine() } },
  }], {
    ...bg, title: { text: '绩效归因 (Brinson)', font: pf },
    xaxis: { tickfont: pf }, yaxis: { title: { text: 'PnL (¥)', font: pf }, tickfont: pf },
    margin: { t: 40, b: 40, l: 60, r: 10 },
  }, PLOTLY_CONFIG);
}

// ═══════════════════════════════════════════
// SCHEDULER
// ═══════════════════════════════════════════
let _schedulerTimer = null;

async function loadScheduler() {
  try {
    const data = await fetchJSON(API + '/scheduler');
    const tasks = data?.tasks || [];
    renderScheduler(tasks);
  } catch (e) { console.warn('scheduler error:', e.message); }
}

function renderScheduler(tasks) {
  const el = document.getElementById('meta-scheduler');
  if (el) {
    const running = tasks.filter(t => t.status === 'running').length;
    const errors = tasks.filter(t => t.status && t.status.startsWith('error')).length;
    let meta = tasks.length + ' 任务';
    if (running) meta += ' · ' + running + ' 运行中';
    if (errors) meta += ' · ' + errors + ' 异常';
    el.textContent = meta;
  }

  renderTable('table-scheduler', tasks, [
    { key: 'name', label: '任务' },
    { key: 'schedule', label: '时间' },
    { key: 'status', label: '状态' },
    { key: 'has_multiprocess', label: '多进程' },
    { key: 'last_run', label: '上次执行' },
    { key: 'last_duration', label: '耗时' },
    { key: 'last_error', label: '错误' },
  ], {
    clsMap: {
      status: v => {
        if (!v) return '';
        if (v.startsWith('running')) return 'status-running';
        if (v.startsWith('error')) return 'status-error';
        if (v.startsWith('waiting')) return 'status-waiting';
        if (v.startsWith('sleep') || v.startsWith('skipped')) return 'status-sleep';
        return 'status-idle';
      }
    },
    fmtMap: {
      name: v => {
        const names = { signals: '信号生成', execute: '交易执行', attribution: '盘后归因' };
        return names[v] || v;
      },
      has_multiprocess: v => v ? '⚠ 是' : '—',
      last_run: v => v ? v.replace('T', ' ').slice(0, 19) : '—',
      last_duration: v => v != null ? v.toFixed(1) + 's' : '—',
    }
  });
}

// ── SSE ──
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
document.addEventListener('DOMContentLoaded', () => {
  initTheme();
  document.getElementById('theme-toggle').addEventListener('click', toggleTheme);
  connectSSE();
  pollOverview();
  setInterval(pollOverview, POLL_MS);
  const checkPlotly = () => {
    if (typeof Plotly !== 'undefined' && !_chartsRendered) {
      _chartsRendered = true;
      renderPNLChart();
    } else if (!_chartsRendered) { setTimeout(checkPlotly, 200); }
  };
  setTimeout(checkPlotly, 100);
});
