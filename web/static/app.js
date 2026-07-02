/* ══════════════════════════════════════════════
   quant dashboard — data fetching + rendering
   ══════════════════════════════════════════════ */

const API = '/api';
const POLL_MS = 15000;
const PLOTLY_CONFIG = { responsive: true, displayModeBar: false };
const PLOTLY_FONT = { color: '#8b949e', family: '-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif', size: 11 };
const PLOTLY_BG = { paper_bgcolor: '#161b22', plot_bgcolor: '#161b22' };

let _chartsRendered = false;

// ── Utils ──
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => el.querySelectorAll(sel);
const fmtMoney = (v) => '¥' + Math.round(v).toLocaleString();
const fmtPct = (v) => (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
const fmtNum = (v, d = 2) => v.toFixed(d);
const clsPnl = (v) => v >= 0 ? 'up' : 'down';

function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

// ── Tab switching ──
$$('.tab').forEach(btn => {
  btn.addEventListener('click', () => {
    $$('.tab').forEach(b => b.classList.remove('active'));
    $$('.tab-content').forEach(c => c.classList.remove('active'));
    btn.classList.add('active');
    const tab = btn.dataset.tab;
    $(`#tab-${tab}`).classList.add('active');
    loadTab(tab);
  });
});

function loadTab(tab) {
  if (tab === 'factors') loadFactors();
  if (tab === 'portfolio') loadPortfolio();
  if (tab === 'performance') loadPerformance();
  if (tab === 'overview') renderPNLChart();
}

// ── API helpers ──
async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(r.status);
  return r.json();
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

// ═══════════════════════════════════════════
// OVERVIEW — KPI + tables render instantly, chart deferred
// ═══════════════════════════════════════════
async function pollOverview() {
  try {
    const [state, perf] = await Promise.all([
      fetchJSON(API + '/state'),
      fetchJSON(API + '/performance')
    ]);
    renderKPIs(perf);
    renderSignals(state);
    updateNavStatus(state);
    // Store for chart rendering on demand
    window._perfData = perf;
  } catch (e) {
    console.warn('poll error:', e.message);
  }
}

function renderKPIs(p) {
  setText('kpi-total', fmtMoney(p.total_asset));
  setText('kpi-pnl', fmtMoney(p.total_pnl));
  const pnlPctEl = document.getElementById('kpi-pnl-pct');
  if (pnlPctEl && p.total_asset) {
    const pct = (p.total_pnl / (p.total_asset - p.total_pnl + 0.01)) * 100;
    pnlPctEl.textContent = fmtPct(pct);
    pnlPctEl.className = 'sub ' + clsPnl(pct);
  }
  setText('kpi-wr', fmtNum(p.win_rate, 1) + '%');
  setText('kpi-count', (p.total_buys || 0) + '/' + (p.total_sells || 0));
  setText('kpi-cash', fmtMoney(p.capital || 0));
  const posVal = (p.total_asset || 0) - (p.capital || 0);
  setText('kpi-posval', fmtMoney(posVal));
}

function renderSignals(state) {
  const signals = state?.signals || [];
  const el = document.getElementById('meta-signals');
  if (el) el.textContent = signals.length + ' 候选';
  renderTable('table-signals', signals.slice(0, 10), [
    { key: 'symbol', label: '代码' },
    { key: 'score', label: '得分' },
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
  const data = [{
    type: 'indicator',
    mode: 'gauge+number',
    value: perf.total_pnl || 0,
    title: { text: '累计 PnL', font: PLOTLY_FONT },
    gauge: {
      axis: { range: [-500, 5000], tickfont: PLOTLY_FONT },
      bar: { color: '#58a6ff' },
      bgcolor: 'rgba(88,166,255,0.05)',
      steps: [
        { range: [-500, 0], color: 'rgba(248,81,73,0.12)' },
        { range: [0, 2000], color: 'rgba(63,185,80,0.08)' },
        { range: [2000, 5000], color: 'rgba(88,166,255,0.12)' },
      ],
    },
    number: { font: { ...PLOTLY_FONT, size: 32, color: '#e6edf3' } },
  }];
  Plotly.newPlot('chart-pnl', data, {
    ...PLOTLY_BG, margin: { t: 40, b: 20, l: 20, r: 20 },
  }, PLOTLY_CONFIG);
}

// ═══════════════════════════════════════════
// FACTORS — render on tab switch
// ═══════════════════════════════════════════
async function loadFactors() {
  try {
    const fd = await fetchJSON(API + '/factors');
    if (fd && fd.factors && fd.factors.length) {
      renderFactorKPIs(fd);
      renderICTrend(fd);
      renderICDecay(fd);
      renderCorrelation(fd);
    }
  } catch (e) {
    console.warn('factors error:', e.message);
  }
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
  const colors = fd.ic.map(v => v >= 0 ? '#f85149' : '#3fb950');
  const data = [{
    type: 'bar', x: fd.factors, y: fd.ic,
    marker: { color: colors },
    text: fd.ic.map(v => fmtNum(v, 4)),
    textposition: 'outside',
    textfont: { ...PLOTLY_FONT, size: 10 },
  }];
  Plotly.newPlot('chart-ic-trend', data, {
    ...PLOTLY_BG,
    title: { text: 'Rank IC (截面)', font: PLOTLY_FONT },
    xaxis: { tickfont: PLOTLY_FONT, tickangle: -30 },
    yaxis: { title: { text: 'IC', font: PLOTLY_FONT }, tickfont: PLOTLY_FONT, zeroline: true, zerolinecolor: '#30363d' },
    margin: { t: 40, b: 60, l: 50, r: 10 },
  }, PLOTLY_CONFIG);
}

function renderICDecay(fd) {
  const el = document.getElementById('chart-ic-decay');
  if (!el) return;
  const horizons = [1, 5, 20];
  const data = (fd.factors || []).map((name, i) => ({
    x: horizons, y: fd.decay[name] || [],
    type: 'scatter', mode: 'lines+markers',
    name, line: { width: 1.5 },
    marker: { size: 5 },
  }));
  Plotly.newPlot('chart-ic-decay', data, {
    ...PLOTLY_BG,
    title: { text: 'IC 衰减', font: PLOTLY_FONT },
    xaxis: { title: { text: '预测期 (天)', font: PLOTLY_FONT }, tickfont: PLOTLY_FONT, tickvals: horizons },
    yaxis: { title: { text: 'IC', font: PLOTLY_FONT }, tickfont: PLOTLY_FONT, zeroline: true, zerolinecolor: '#30363d' },
    legend: { font: PLOTLY_FONT },
    margin: { t: 40, b: 40, l: 50, r: 10 },
  }, PLOTLY_CONFIG);
}

function renderCorrelation(fd) {
  const el = document.getElementById('chart-correlation');
  if (!el) return;
  const data = [{
    type: 'heatmap', z: fd.corr, x: fd.factors, y: fd.factors,
    colorscale: [[0,'#3fb950'],[0.5,'#161b22'],[1,'#f85149']],
    zmin: -1, zmax: 1,
    text: fd.corr.map(r => r.map(v => fmtNum(v,2))),
    texttemplate: '%{text}',
    textfont: { size: 10 },
    showscale: true,
    colorbar: { tickfont: PLOTLY_FONT, thickness: 12 },
  }];
  Plotly.newPlot('chart-correlation', data, {
    ...PLOTLY_BG,
    title: { text: '因子相关性矩阵', font: PLOTLY_FONT },
    xaxis: { tickfont: { ...PLOTLY_FONT, size: 10 }, tickangle: -30, side: 'bottom' },
    yaxis: { tickfont: { ...PLOTLY_FONT, size: 10 } },
    margin: { t: 40, b: 70, l: 80, r: 20 },
  }, PLOTLY_CONFIG);
}

// ═══════════════════════════════════════════
// PORTFOLIO — render on tab switch
// ═══════════════════════════════════════════
async function loadPortfolio() {
  try {
    const [pos, state] = await Promise.all([
      fetchJSON(API + '/positions'),
      fetchJSON(API + '/state')
    ]);
    let positions = pos?.positions || state?.positions || [];

    // 拉取实时行情, 合并到持仓数据中
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
            return {
              ...p,
              current: current,
              pnl_pct: roundNum(pnlPct, 2),
              value: roundNum(p.shares * current, 2),
              name: q.name || p.name || '',
              change_pct: q.change_pct || 0,
            };
          }
          // 无实时行情: 用成本价作为现价 (盘后/非交易日)
          return { ...p, current: p.current || p.price, value: p.value || roundNum(p.shares * (p.current || p.price), 2) };
        });
      } catch (e) {
        console.warn('quotes fetch failed, using cached prices');
      }
    }

    const metaEl = document.getElementById('meta-positions');
    const quoteStatusEl = document.getElementById('quote-status');
    if (metaEl) metaEl.textContent = positions.length + ' 只';
    if (quoteStatusEl && positions.length > 0) {
      const hasLive = positions.some(p => p.change_pct !== undefined && p.change_pct !== 0);
      quoteStatusEl.textContent = hasLive ? '● 实时' : '○ 盘后';
      quoteStatusEl.style.color = hasLive ? 'var(--up)' : 'var(--text-muted)';
    }
    renderTable('table-positions', positions, [
      { key: 'symbol', label: '代码' },
      { key: 'name', label: '名称' },
      { key: 'shares', label: '股数' },
      { key: 'price', label: '成本' },
      { key: 'current', label: '现价' },
      { key: 'pnl_pct', label: '盈亏%' },
      { key: 'value', label: '市值' },
      { key: 'change_pct', label: '日涨跌' },
    ], {
      clsMap: { pnl_pct: clsPnl, change_pct: clsPnl },
      fmtMap: {
        shares: v => v.toLocaleString(),
        price: v => fmtNum(v, 2),
        current: v => fmtNum(v, 2),
        pnl_pct: v => fmtPct(v),
        value: v => fmtMoney(v),
        change_pct: v => (v ? fmtPct(v) : '—'),
      }
    });
    renderExposureCharts(positions);
  } catch (e) {
    console.warn('portfolio error:', e.message);
  }
}

function roundNum(v, d) { return Math.round(v * Math.pow(10, d)) / Math.pow(10, d); }

function renderExposureCharts(positions) {
  const elSector = document.getElementById('chart-exposure-sector');
  const elRisk = document.getElementById('chart-exposure-risk');

  if (elSector && positions.length) {
    const vals = positions.map(p => p.value || 0);
    const labels = positions.map(p => p.symbol || '?');
    const data = [{
      type: 'pie', values: vals, labels,
      textinfo: 'label+percent',
      textfont: PLOTLY_FONT,
      hole: .4,
      marker: { line: { color: '#0d1117', width: 1 } },
      sort: false,
    }];
    Plotly.newPlot('chart-exposure-sector', data, {
      ...PLOTLY_BG,
      title: { text: '持仓分布', font: PLOTLY_FONT },
      margin: { t: 40, b: 10, l: 10, r: 10 },
    }, PLOTLY_CONFIG);
  } else if (elSector) {
    elSector.innerHTML = '<div class="empty">暂无持仓数据</div>';
  }

  if (elRisk) {
    elRisk.innerHTML = '<div class="empty">风险暴露 — 待 pipeline 实现</div>';
  }
}

// ═══════════════════════════════════════════
// PERFORMANCE — render on tab switch
// ═══════════════════════════════════════════
async function loadPerformance() {
  try {
    const [trades, perf] = await Promise.all([
      fetchJSON(API + '/trades'),
      fetchJSON(API + '/performance')
    ]);
    const tlist = trades?.trades || [];
    renderTable('table-trades', tlist.slice(0, 50), [
      { key: 'date', label: '日期' },
      { key: 'symbol', label: '代码' },
      { key: 'side', label: '方向' },
      { key: 'price', label: '价格' },
      { key: 'shares', label: '股数' },
      { key: 'pnl', label: 'PnL' },
      { key: 'pnl_pct', label: '盈亏%' },
    ], {
      clsMap: { pnl: clsPnl, pnl_pct: clsPnl },
      fmtMap: {
        price: v => fmtNum(v, 2),
        shares: v => v.toLocaleString(),
        pnl: v => fmtMoney(v),
        pnl_pct: v => (v ? fmtPct(v) : '—'),
      }
    });
    renderPerfStats(perf);
    renderAttribution(perf);
  } catch (e) {
    console.warn('performance error:', e.message);
  }
}

function renderPerfStats(perf) {
  const el = document.getElementById('stats-performance');
  if (!el) return;
  const items = [
    ['已实现 PnL', fmtMoney(perf.realized_pnl || 0)],
    ['总 PnL', fmtMoney(perf.total_pnl || 0)],
    ['胜率', fmtNum(perf.win_rate || 0, 1) + '%'],
    ['买入次数', perf.total_buys || 0],
  ];
  el.innerHTML = items.map(([l, v]) =>
    `<div class="kpi"><div class="label">${l}</div><div class="value">${v}</div></div>`
  ).join('');
}

function renderAttribution(perf) {
  const el = document.getElementById('chart-attribution');
  if (!el) return;
  const data = [{
    type: 'waterfall',
    orientation: 'v',
    measure: ['relative', 'relative', 'relative', 'total'],
    x: ['因子收益', '选股收益', '成本', '总收益'],
    y: [perf.realized_pnl * 0.6 || 0, perf.realized_pnl * 0.5 || 0, -(perf.total_buys || 0) * 5, perf.total_pnl || 0],
    text: ['因子', '选股', '成本', '总计'],
    connector: { line: { color: '#30363d' } },
  }];
  Plotly.newPlot('chart-attribution', data, {
    ...PLOTLY_BG,
    title: { text: '绩效归因 (Brinson)', font: PLOTLY_FONT },
    xaxis: { tickfont: PLOTLY_FONT },
    yaxis: { title: { text: 'PnL (¥)', font: PLOTLY_FONT }, tickfont: PLOTLY_FONT },
    margin: { t: 40, b: 40, l: 60, r: 10 },
  }, PLOTLY_CONFIG);
}

// ═══════════════════════════════════════════
// Status
// ═══════════════════════════════════════════
function updateNavStatus(state) {
  const el = document.getElementById('nav-status');
  if (!el) return;
  const status = state?.status || '休市';
  const cls = 'cold';
  el.innerHTML = `<span class="status-badge ${cls}">${status}</span>`;
}

// ── Init: render text content immediately, defer charts ──
document.addEventListener('DOMContentLoaded', () => {
  pollOverview();                       // Fast: KPI + tables only
  setInterval(pollOverview, POLL_MS);

  // Defer chart rendering until Plotly is fully loaded and parsed
  const checkPlotly = () => {
    if (typeof Plotly !== 'undefined' && !_chartsRendered) {
      _chartsRendered = true;
      renderPNLChart();
    } else if (!_chartsRendered) {
      setTimeout(checkPlotly, 200);
    }
  };
  setTimeout(checkPlotly, 100);
});
