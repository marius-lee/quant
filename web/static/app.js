// 北极星 · 实时监控面板
var POSITIONS = [];

async function get(path) {
  const r = await fetch('/api'+path);
  return await r.json();
}

function renderCapital(state) {
  const totalAsset = state.total_asset || state.capital || 5000;
  const capital = state.capital || 0;
  const ret = ((totalAsset/5000 - 1)*100);
  document.getElementById('capital-amount').textContent = '¥'+totalAsset.toLocaleString();
  document.getElementById('capital-available').textContent = '¥'+capital.toLocaleString();
  const el = document.getElementById('capital-return');
  el.textContent = (ret>=0?'+':'')+ret.toFixed(1)+'%';
  el.style.color = ret>=0 ? '#ef4444' : '#10b981';
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
      var sigLabel = signals.length > 0 ? signals.length+'信号' : '';
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
  list.innerHTML = ps.map(p => {
    const pnl = p.current_price ? ((p.current_price/p.price-1)*100) : 0;
    const days = p.date ? Math.floor((Date.now()-new Date(p.date).getTime())/86400000) : 0;
    const sealedBadge = p.has_sealed ? ' 🔒封板' : '';
    return '<div class="signal-card held">'+
      '<div class="sig-symbol">'+p.symbol+' <span class="sig-mode">'+(p.board_count||0)+'连板 · '+days+'天</span></div>'+
      '<div class="sig-price">成本¥'+p.price.toFixed(2)+
        ' <span class="sig-ret '+(pnl>=0?'up':'down')+'">'+(pnl>=0?'+':'')+pnl.toFixed(1)+'%</span></div>'+
      '<div class="sig-meta">'+p.date+' 买入 ×'+p.shares+'股'+sealedBadge+'</div>'+
    '</div>';
  }).join('');
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

async function poll() {
  const state = await get('/state');
  const td = await get('/trades');
  POSITIONS = td.positions || [];
  renderCapital(state);
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
    const holds = (r.portfolio.holdings||[]).map(h =>
      '<div class="trade-row '+(h.pnl_pct>=0?'buy':'sell')+'">'+
        h.symbol+' 成本¥'+h.cost+' 现价¥'+h.current+
        ' <b>'+(h.pnl_pct>=0?'+':'')+h.pnl_pct.toFixed(1)+'%</b>'+
        ' ×'+h.shares+'股 ¥'+h.value+
      '</div>'
    ).join('');
    document.getElementById('review-content').innerHTML =
      '<div style="margin-bottom:8px">'+
        '<div>📈 信号: '+sigs+'</div>'+
        '<div style="margin-top:4px">💰 总资产: ¥'+r.portfolio.total_asset.toLocaleString()+
        ' | 💵 可用: ¥'+r.portfolio.available_cash.toLocaleString()+
        ' | 📊 持仓市值: ¥'+r.portfolio.positions_value.toLocaleString()+'</div>'+
      '</div>'+
      (holds?'<div style="margin-top:8px"><b>持仓估值</b>'+holds+'</div>':'');
    document.getElementById('review-panel').style.display = 'block';
  } catch(e) {}
}

poll();
setInterval(poll, 3000);
loadReview();
