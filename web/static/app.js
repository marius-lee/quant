// 北极星 · 实时监控面板
var POSITIONS = [];

async function get(path) {
  const r = await fetch('/api'+path);
  return await r.json();
}

async function renderCapital(state) {
  try {
    const perf = await (await fetch('/api/performance?strategy=chen')).json();
    const totalAsset = perf.total_asset || state.total_asset || 5000;
    const capital = perf.capital || 0;
    const ret = ((totalAsset/5000 - 1)*100);
    document.getElementById('capital-amount').textContent = '¥'+totalAsset.toLocaleString();
    document.getElementById('capital-available').textContent = '¥'+capital.toLocaleString();
    const el = document.getElementById('capital-return');
    el.textContent = (ret>=0?'+':'')+ret.toFixed(1)+'%';
    el.style.color = ret>=0 ? '#ef4444' : '#10b981';
    var cls = perf.total_pnl>=0 ? 'color:#ef4444' : 'color:#10b981';
    document.getElementById('perf-stats').innerHTML =
      '已实现: ¥'+(perf.realized_pnl>=0?'+':'')+perf.realized_pnl.toLocaleString()+
      ' | 浮动: ¥'+(perf.unrealized_pnl>=0?'+':'')+perf.unrealized_pnl.toLocaleString()+
      ' | <span style=\"'+cls+'\">总计: ¥'+(perf.total_pnl>=0?'+':'')+perf.total_pnl.toLocaleString()+'</span>'+
      ' | '+perf.total_buys+'买 '+perf.total_sells+'卖'+
      ' | 胜率: '+perf.win_rate+'%';
  } catch(e) {}
}

function renderMood(state) {
  const mood = state.mood || {}, stage = mood.stage || '';
  const status = state.status || 'idle';
  const map = {
    '冰点':{icon:'🧊',label:'冰点 — 空仓'},
    '复苏':{icon:'🌱',label:'复苏 — 试错'},
    '扩张':{icon:'📈',label:'扩张 — 加仓'},
    '高潮':{icon:'🔥',label:'高潮 — 重仓'},
    '退潮':{icon:'🌊',label:'退潮 — 清仓'},
  };
  const m = map[stage]||{icon:'⏳',label:'计算中...'};

  // 盘中：情绪+信号合并  非盘中：状态覆盖
  if (status === '盘中') {
    var signals = state.all_signals || [];
    var progress = state.progress || '';
    if (progress) {
      m.icon = '⏳'; m.label = progress;
    } else {
      var moodLabel = stage ? m.label : '';
      var todaySig = state.today_signal_count || 0;
      var sigLabel = todaySig > 0 ? todaySig+'信号' : '';
      m.label = [moodLabel, sigLabel].filter(Boolean).join(' · ') || '盘中';
    }
  } else if (status === '盘前') {
    m.icon = '🌅'; m.label = '盘前 · 等待开盘';
  } else if (status === '午休') {
    m.icon = '🍱'; m.label = '午间休市 · 13:00恢复';
  } else if (status === '已收盘') {
    m.icon = '🏁'; m.label = '已收盘';
  } else if (status === '休市') {
    m.icon = '🌙'; m.label = '休市';
  }

  document.getElementById('mood-icon').textContent = m.icon;
  document.getElementById('mood-label').textContent = m.label;
  document.getElementById('status-dot').textContent = status==='盘中'?'🟢':'⚪';
}

function renderPositions() {
  const list = document.getElementById('position-list');
  const count = document.getElementById('pos-count');
  const ps = POSITIONS;
  const panel = document.getElementById('positions-panel');
  if (!ps || ps.length===0) {
    list.innerHTML = '';
    count.textContent = '';
    panel.style.display = 'none';
    return;
  }
  panel.style.display = 'block';
  count.textContent = ps.length+'只';
  var html = '<table class="pos-table"><thead><tr>'+
    '<th>代码</th><th>名称</th><th>数量</th><th>成本</th><th>现价</th><th>盈亏</th><th>市值</th>'+
  '</tr></thead><tbody>';
  ps.forEach(p => {
    var cost = p.price || 0;
    var current = p.current || p.current_price || cost;
    var pnl = ((current/cost - 1)*100) || 0;
    var value = p.value || (p.shares * current);
    var cls = pnl>=0 ? 'up' : 'down';
    html += '<tr>'+
      '<td>'+p.symbol+'<div class="sub">'+(p.board_count||0)+'连板 · '+(p.date||'')+'</div></td>'+
      '<td>'+(p.name||'')+'</td>'+
      '<td>'+p.shares+'股</td>'+
      '<td>¥'+cost.toFixed(2)+'</td>'+
      '<td>¥'+current.toFixed(2)+'</td>'+
      '<td class="'+cls+'">'+(pnl>=0?'+':'')+pnl.toFixed(1)+'%</td>'+
      '<td>¥'+value.toLocaleString()+'</td>'+
    '</tr>';
  });
  html += '</tbody></table>';
  list.innerHTML = html;
}

function renderSignals(state) {
  const all = state.all_signals || [];
  const final = state.final_signals || [];
  const golden = state.golden_signals || [];
  document.getElementById('signal-count').textContent = all.length+'个';
  document.getElementById('no-signals').style.display = all.length===0?'block':'none';

  // 合并去重, 最新在前
  const seen = new Set();
  const merged = [];
  for (const s of [...final, ...golden, ...all]) {
    const key = s.symbol+s.mode;
    if (!seen.has(key)) { seen.add(key); merged.push(s); }
  }
  merged.sort((a,b) => (b.time||'').localeCompare(a.time||''));

  document.getElementById('signal-list').innerHTML = merged.slice(0,10).map(function(s) {
    var m = s.mode || '';
    var isGold = m.includes('B4') || m.includes('B3');
    var cls = 'signal-card ' + (isGold ? 'golden' : 'final');
    var leader = s.is_leader ? ' <span class=\"badge-leader\">👑龙头</span>' : '';
    var time = s.time ? ' <span class=\"sig-time\">'+s.time+'</span>' : '';
    var ret = s.daily_ret || 0;
    var cls2 = 'sig-ret ' + (ret >= 0 ? 'up' : 'down');
    return '<div class=\"'+cls+'\">'+
      '<div class=\"sig-symbol\">'+s.symbol+leader+' <span class=\"sig-mode\">'+m+'</span>'+time+'</div>'+
      '<div class=\"sig-price\">¥'+(s.price||0).toFixed(2)+
        ' <span class=\"'+cls2+'\">'+(ret>=0?'+':'')+ret.toFixed(1)+'%</span></div>'+
      '<div class=\"sig-meta\">'+(s.board_count||0)+'连板 | 跳空'+(s.gap_pct||0).toFixed(1)+'% | '+(s.reason||'')+'</div>'+
    '</div>';
  }).join('');
}

function renderExits(state) {
  document.getElementById('exit-alerts').innerHTML = (state.exits||[]).map(e =>
    '<div class="exit-alert">⚠️ '+e.symbol+': '+e.reason+'</div>'
  ).join('');
}

function renderTrades(trades) {
  document.getElementById('trade-list').innerHTML = (trades||[]).slice(-8).reverse().map(t =>
    '<div class="trade-row '+t.side+'">'+
      (t.side==='buy'?'🟢':'🔴')+' '+t.date+' '+t.symbol+' '+
      (t.side==='buy'?'买入':'卖出')+' ¥'+t.price.toFixed(2)+' ×'+t.shares+'股'+
      (t.pnl?' PnL ¥'+(t.pnl>=0?'+':'')+t.pnl+' ('+t.pnl_pct+'%)':'')+
    '</div>'
  ).join('');
}

// v17
async function renderGrinold() {
  try {
    const m = await (await fetch('/api/performance/icir?strategy=chen')).json();
    var card = document.getElementById('icir-card');
    if (!card) return;
    var html = '<div class="icir-grid">';
    var icLabels = [['IC₁', m.ic_pearson_1d], ['IC₃', m.ic_pearson_3d],
                    ['IC₅', m.ic_pearson_5d], ['IC₂₀', m.ic_pearson_20d]];
    icLabels.forEach(function(p){
      var v = p[1]; var cls = v==null?'muted':(v>=0?'up':'down');
      var txt = v!=null ? ((v>=0?'+':'')+v.toFixed(3)) : 'N/A';
      html += '<span class="icir-item"><span class="icir-label">'+p[0]+'</span><span class="'+cls+'">'+txt+'</span></span>';
    });
    var ir = m.ir_annualized; var icls = ir==null?'muted':(ir>=0.5?'up':'down');
    html += '<span class="icir-item"><span class="icir-label">IR</span><span class="'+icls+'">'+(ir!=null?((ir>=0?'+':'')+ir.toFixed(2)):'N/A')+'</span></span>';
    var br = m.br_bets_per_year;
    html += '<span class="icir-item"><span class="icir-label">BR/yr</span><span>'+(br!=null?br.toFixed(0):'N/A')+'</span></span>';
    if (m.ir_implied != null) {
      html += '<span class="icir-item"><span class="icir-label">IC×√BR</span><span>'+m.ir_implied.toFixed(2)+'</span></span>';
    }
    html += '</div>';
    html += '<div class="icir-footnote">'+(m.data_quality||'')+' | '+m.n_signals+'信号 '+m.n_trades+'交易</div>';
    card.innerHTML = html;
  } catch(e) {}
}

async function poll() {
  const state = await get('/state');
  const td = await get('/trades?strategy=chen');
  POSITIONS = state.positions || td.positions || [];
  await renderCapital(state);
  renderGrinold();
  renderMood(state);
  renderPositions();
  renderSignals(state);
  renderExits(state);
  renderTrades(td.trades||[]);
}

async function loadReview() {
  try {
    const r = await (await fetch('/api/review')).json();
    const m = r.signals.by_mode;
    const sigs = Object.entries(m).map(([k,v]) =>
      '<span style="margin-right:12px">'+k.replace('_',' ')+': <b>'+v.count+'</b> (买'+v.bought+')</span>'
    ).join('');
    document.getElementById('review-content').innerHTML =
      '<div style="margin-bottom:8px">'+
        '<div>📈 信号: '+sigs+'</div>'+
        '<div style="margin-top:4px">💰 总资产: ¥'+r.portfolio.total_asset.toLocaleString()+
        ' | 💵 可用: ¥'+r.portfolio.available_cash.toLocaleString()+
        ' | 📊 已买: '+r.signals.bought+'/'+r.signals.total+'</div>'+
      '</div>';
    document.getElementById('review-panel').style.display = 'block';
  } catch(e) {}
}

poll();
setInterval(poll, 3000);
loadReview();
