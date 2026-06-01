// ── Themes ──
var THEMES=[
  {
    name:'Linear 暗',up:'#e5484d',down:'#0eb978',spUp:'#e5484d',spDown:'#0eb978',
    bg:'#0f0f11',bgPanel:'#161618',bgElevate:'#1c1c1f',bgHover:'#222226',
    border:'#1e1e22',border2:'#2c2c33',
    text:'#ffffff',textDim:'#dddddd',textMute:'#bbbbbb',
    accent:'#5e6ad2',accentDim:'rgba(94,106,210,0.12)',
    amber:'#e9a123',amberDim:'rgba(233,161,35,0.10)',
    blue:'#3578e5',blueDim:'rgba(53,120,229,0.10)',
    green:'#0eb978',greenDim:'rgba(14,185,120,0.10)',
    red:'#e5484d',redDim:'rgba(229,72,77,0.10)',
    white:'#ffffff'
  },{
    name:'Bloomberg 黑',up:'#ef4444',down:'#22c55e',spUp:'#ef4444',spDown:'#22c55e',
    bg:'#000000',bgPanel:'#121212',bgElevate:'#1c1c1c',bgHover:'#242424',
    border:'#2a2a2a',border2:'#3a3a3a',
    text:'#f0f0f0',textDim:'#c8c8c8',textMute:'#aaaaaa',
    accent:'#f5a623',accentDim:'rgba(245,166,35,0.12)',
    amber:'#f5a623',amberDim:'rgba(245,166,35,0.10)',
    blue:'#4da6ff',blueDim:'rgba(77,166,255,0.10)',
    green:'#22c55e',greenDim:'rgba(34,197,94,0.10)',
    red:'#ef4444',redDim:'rgba(239,68,68,0.10)',
    white:'#ffffff'
  },{
    name:'Stripe 暗',up:'#f06672',down:'#3ecf8e',spUp:'#f06672',spDown:'#3ecf8e',
    bg:'#1a1b26',bgPanel:'#1f2133',bgElevate:'#26283b',bgHover:'#2c2e42',
    border:'#2d2f43',border2:'#3d3f55',
    text:'#e2e3eb',textDim:'#a8a9c0',textMute:'#757693',
    accent:'#9b8bff',accentDim:'rgba(155,139,255,0.12)',
    amber:'#f2a054',amberDim:'rgba(242,160,84,0.10)',
    blue:'#6da8ff',blueDim:'rgba(109,168,255,0.10)',
    green:'#3ecf8e',greenDim:'rgba(62,207,142,0.10)',
    red:'#f06672',redDim:'rgba(240,102,114,0.10)',
    white:'#ffffff'
  }
];

function applyTheme(t,i){
  var r=document.documentElement;
  for(var k in t){r.style.setProperty('--'+k.replace(/([A-Z])/g,'-$1').toLowerCase(),t[k]);}
  var sel=document.getElementById('themeSelect');
  if(sel)sel.value=i;
  localStorage.setItem('quantTheme',i);
  themeIdx=i;
}

function setTheme(i){applyTheme(THEMES[i],parseInt(i));}

(function(){
  var sel=document.getElementById('themeSelect'),o='';
  for(var i=0;i<THEMES.length;i++)o+='<option value="'+i+'">'+THEMES[i].name+'</option>';
  sel.innerHTML=o;
})();

var saved=localStorage.getItem('quantTheme');
var themeIdx=saved!==null?parseInt(saved):0;
applyTheme(THEMES[themeIdx],themeIdx);

// ── State ──
var DATA=null,TRACK_DATA=null,prevMetrics=null,prevPortValue=null;

// ── Navigation ──
function switchPage(n){
  document.querySelectorAll('.nav-item').forEach(function(e,i){
    e.classList.remove('on');
    var d=e.querySelector('.dot');
    d.classList.remove('dim');
    if(n!==e.getAttribute('onclick').match(/'(\w+)'/)[1])d.classList.add('dim');
  });
  var it=document.querySelector('.nav-item[onclick*="'+n+'"]');
  it.classList.add('on');it.querySelector('.dot').classList.remove('dim');
  document.querySelectorAll('.page').forEach(function(e){e.classList.remove('on')});
  document.getElementById('page-'+n).classList.add('on');
  var t={picks:'选股推荐',tracking:'追踪回顾',positions:'持仓监控',trades:'交易记录'};
  document.getElementById('pageTitle').textContent=t[n];
  if(n==='tracking')loadTracking();
  if(n==='positions')loadPositions();
  if(n==='trades')loadTrades();
}

// ── Sort ──
function sortTbl(id,c){
  var t=document.getElementById(id),rows=Array.from(t.querySelectorAll('tbody tr'));
  if(!rows.length)return;
  var th=t.querySelectorAll('th')[c],asc=!th.classList.contains('srt');
  t.querySelectorAll('th').forEach(function(h){h.classList.remove('srt')});
  th.classList.add('srt');
  rows.sort(function(a,b){
    var x=(a.cells[c]||{}).textContent||'',y=(b.cells[c]||{}).textContent||'';
    var nx=parseFloat(x.replace(/[^0-9.\-+]/g,'')),ny=parseFloat(y.replace(/[^0-9.\-+]/g,''));
    if(!isNaN(nx)&&!isNaN(ny))return asc?nx-ny:ny-nx;
    return asc?x.localeCompare(y):y.localeCompare(x);
  });
  rows.forEach(function(r){t.querySelector('tbody').appendChild(r)});
}

// ── Sparkline ──
function spark(v){
  if(!v||v.length<2)return'';
  var mn=v[0],mx=v[0];
  for(var i=1;i<v.length;i++){if(v[i]<mn)mn=v[i];if(v[i]>mx)mx=v[i];}
  var r=mx-mn||1,w=60,h=20,p='';
  for(i=0;i<v.length;i++)p+=(i/(v.length-1)*w).toFixed(1)+','+(h-((v[i]-mn)/r)*(h-4)-2).toFixed(1)+' ';
  return'<svg class="spark-wrap" viewBox="0 0 '+w+' '+h+'"><polyline fill="none" stroke="'+(v[v.length-1]>=v[0]?THEMES[themeIdx].spUp:THEMES[themeIdx].spDown)+'" stroke-width="1.3" points="'+p+'"/></svg>';
}

// ── Trend arrow ──
function tnd(cur,prev){
  if(prev===null||prev===undefined||prev===0)return'';
  var d=(cur-prev)/Math.abs(prev||1);
  if(Math.abs(d)<0.005)return'<span style="color:var(--text-mute);font-size:10px"> →</span>';
  return d>0?'<span style="color:var(--red);font-size:10px"> ▲</span>':'<span style="color:var(--green);font-size:10px"> ▼</span>';
}

// ── Picks page ──
function renderAll(d){
  DATA=d;var m=d.metrics||{};
  var sh=m.sharpe_ratio||0,ar=(m.annual_return||0)*100,dd=(m.max_drawdown||0)*100,wr=(m.win_rate||0)*100;
  var pv=prevMetrics;
  document.getElementById('kpiBar').innerHTML=
    '<div class="kpi-card"><div class="lbl">夏普比率</div><div class="val '+(sh>=0?'up':'down')+'">'+sh.toFixed(3)+(pv?tnd(sh,pv.sharpe_ratio):'')+'</div></div>'+
    '<div class="kpi-card"><div class="lbl">年化收益</div><div class="val '+(ar>=0?'up':'down')+'">'+(ar>=0?'+':'')+ar.toFixed(1)+'%'+(pv?tnd(ar,pv.annual_return*100):'')+'</div></div>'+
    '<div class="kpi-card"><div class="lbl">最大回撤</div><div class="val down">'+dd.toFixed(1)+'%</div><div class="sub">'+d.data_range+'</div></div>'+
    '<div class="kpi-card"><div class="lbl">胜率</div><div class="val">'+wr.toFixed(1)+'%</div></div>'+
    '<div class="kpi-card"><div class="lbl">因子筛选</div><div class="val acc">'+(d.n_passed||0)+'</div><div class="sub">'+(d.n_all_factors||0)+' → '+(d.n_passed||0)+'</div></div>'+
    '<div class="kpi-card"><div class="lbl">集成模型</div><div class="val" style="font-size:10px;font-family:var(--font)">'+(d.model_info||'')+'</div></div>';
  prevMetrics={sharpe_ratio:sh,annual_return:m.annual_return||0};
  renderTable();
  if(TRACK_DATA)renderTrack();

  var bl={total_return:'总收益率',annual_return:'年化收益',annual_volatility:'年化波动',sharpe_ratio:'夏普比率',max_drawdown:'最大回撤',win_rate:'胜率',calmar_ratio:'Calmar比率',information_ratio:'信息比率',alpha:'Alpha',beta:'Beta'};
  var bh='';
  for(var k in bl){if(m[k]!==undefined){var v=m[k],pct=['total_return','annual_return','max_drawdown','win_rate'].indexOf(k)>=0;
    bh+='<div class="metric-row"><span class="l">'+bl[k]+'</span><span class="r">'+(pct?(v>=0?'+':'')+(v*100).toFixed(2)+'%':v.toFixed(3))+'</span></div>';
  }}
  if(d.benchmark){var bm=d.benchmark;
    bh+='<div style="margin-top:8px;padding-top:8px;border-top:1px solid var(--border);font-size:9px;color:var(--text-mute);letter-spacing:1px;margin-bottom:4px">基准 '+bm.code+'</div>';
    [{k:'benchmark_return',l:'基准年化'},{k:'benchmark_sharpe',l:'基准夏普'},{k:'alpha',l:'Alpha'},{k:'excess_sharpe',l:'超额夏普'}].forEach(function(x){
      if(bm[x.k]!==undefined){var v=bm[x.k];bh+='<div class="metric-row"><span class="l">'+x.l+'</span><span class="r">'+(x.k.indexOf('return')>=0?(v>=0?'+':'')+(v*100).toFixed(2)+'%':v.toFixed(3))+'</span></div>';}
    });
  }
  document.getElementById('backtestPanel').innerHTML=bh;

  var facts=d.top_factors||{},ents=Object.entries(facts).sort(function(a,b){return b[1]-a[1]}).slice(0,10);
  var mx=ents[0]?ents[0][1]:1;
  document.getElementById('factorPanel').innerHTML=ents.length?ents.map(function(e){
    return'<div class="fac-item"><span class="fac-name">'+e[0]+'</span><div class="fac-track"><div class="fac-fill" style="width:'+Math.max(2,(e[1]/mx*100).toFixed(0))+'%"></div></div><span class="fac-num">'+e[1].toFixed(3)+'</span></div>';
  }).join(''):'<div class="empty">暂无因子数据</div>';
  document.getElementById('factorBadge').textContent=(d.n_all_factors||'?')+' → '+(d.n_passed||'?');
}

function renderTable(){
  if(!DATA||!DATA.recommendations)return;
  var recs=DATA.recommendations;
  document.getElementById('recBadge').textContent='Top '+recs.length;
  document.getElementById('recTable').querySelector('tbody').innerHTML=recs.map(function(r,i){
    return'<tr><td><span class="badge-rank'+(i===0?' gold':'')+'">'+(i+1)+'</span></td><td>'+(r.sparkline&&r.sparkline.length>1?spark(r.sparkline):'<span style="color:var(--text-mute);font-size:10px">—</span>')+'</td><td class="sym">'+r.symbol+'</td><td class="name">'+(r.name||'')+'</td><td class="num" style="color:var(--amber)">'+r.score.toFixed(4)+'</td><td class="num sym">'+(r.last_price||0).toFixed(2)+'</td><td class="num '+(r.change_5d>=0?'up':'down')+'">'+(r.change_5d>=0?'+':'')+(r.change_5d||0).toFixed(1)+'%</td><td class="num" style="font-size:10px;color:var(--text-dim)">'+(r.volatility||0).toFixed(1)+'%</td></tr>';
  }).join('');
}

// ── Track widget ──
function renderTrack(){
  if(!TRACK_DATA||!TRACK_DATA.tracked)return;
  var n=(DATA&&DATA.recommendations)?DATA.recommendations.length:0;
  var h='<table class="tbl"><thead><tr><th>代码</th><th>名称</th><th class="num">推荐价</th><th class="num">最新价</th><th class="num">涨跌</th></tr></thead><tbody>';
  for(var i=0;i<n;i++){
    if(i<TRACK_DATA.tracked.length){var t=TRACK_DATA.tracked[i];h+='<tr><td class="sym">'+t.symbol+'</td><td class="name">'+t.name+'</td><td class="num sym">'+t.rec_price.toFixed(2)+'</td><td class="num sym">'+t.latest_price.toFixed(2)+'</td><td class="num '+(t.change_pct>=0?'up':'down')+'">'+(t.change_pct>=0?'+':'')+t.change_pct.toFixed(2)+'%</td></tr>';}
  }
  h+='</tbody></table>';
  document.getElementById('trackPanelR').innerHTML=h;
  document.getElementById('trackBadgeR').textContent='均'+TRACK_DATA.avg_change.toFixed(2)+'%';
}
function loadTrack(){fetch('/api/track').then(function(r){return r.json()}).then(function(d){if(d.ok&&d.tracked){TRACK_DATA=d;renderTrack();}});}

// ── Tracking page ──
function loadTracking(){
  fetch('/api/tracking').then(function(r){return r.json()}).then(function(d){
    if(!d.ok){document.getElementById('trackHistoryTable').querySelector('tbody').innerHTML='<tr><td colspan="7"><div class="empty"><div class="icon">○</div>暂无追踪数据<br><span style="font-size:10px;color:var(--text-mute)">每日分析完成后自动追踪</span></div></td></tr>';return;}
    var s=d.stats||{},h=d.history||[];
    document.getElementById('trackCount').textContent=h.length+'条记录';
    document.getElementById('trackKpi').innerHTML=
      '<div class="kpi-card"><div class="lbl">累计追踪</div><div class="val acc">'+s.total_tracked+'</div><div class="sub">只</div></div>'+
      '<div class="kpi-card"><div class="lbl">命中率</div><div class="val '+(s.hit_rate>=50?'up':'down')+'">'+s.hit_rate+'%</div></div>'+
      '<div class="kpi-card"><div class="lbl">平均收益</div><div class="val '+(s.avg_return>=0?'up':'down')+'">'+(s.avg_return>=0?'+':'')+s.avg_return+'%</div></div>'+
      '<div class="kpi-card"><div class="lbl">平均超额</div><div class="val '+(s.avg_excess>=0?'up':'down')+'">'+(s.avg_excess>=0?'+':'')+s.avg_excess+'%</div></div>'+
      '<div class="kpi-card"><div class="lbl">涨停命中</div><div class="val up">'+s.limit_up_hits+'</div><div class="sub">只</div></div>'+
      '<div class="kpi-card"><div class="lbl">#1 vs #2-3</div><div class="val" style="font-size:14px"><span style="color:var(--amber)">'+s.high_score_avg_return+'%</span> / '+s.mid_score_avg_return+'%</div></div>';
    document.getElementById('trackHistoryTable').querySelector('tbody').innerHTML=h.map(function(t){
      var det='';
      (t.details||[]).forEach(function(d){var c=d.change_pct;det+=d.symbol+'<span style="color:'+(c>=0?'var(--red)':'var(--green)')+'"> '+(c>=0?'+':'')+c+'%</span> ';});
      return'<tr><td class="sym">'+t.rec_date+'</td><td class="sym">'+t.track_date+'</td><td class="num">'+t.n_picks+'</td><td class="num">'+t.hit_rate+'%</td><td class="num '+(t.avg_return>=0?'up':'down')+'">'+(t.avg_return>=0?'+':'')+t.avg_return+'%</td><td class="num">'+(t.score_corr!=null?t.score_corr.toFixed(3):'—')+'</td><td style="font-size:10px;color:var(--text-dim);line-height:1.8">'+det+'</td></tr>';
    }).join('');
  });
}

// ── Positions page ──
function loadPositions(){
  fetch('/api/positions').then(function(r){return r.json()}).then(function(d){
    if(!d.ok)return;
    var s=d.summary||{},pos=d.positions||[];
    document.getElementById('posCount').textContent=s.n_positions+'只';
    document.getElementById('portSummary').innerHTML=
      '<div class="port-card"><div class="lbl">总投入</div><div class="val">¥'+(s.total_invested||0).toFixed(0)+'</div></div>'+
      '<div class="port-card"><div class="lbl">当前市值</div><div class="val">¥'+(s.current_value||0).toFixed(0)+'</div></div>'+
      '<div class="port-card"><div class="lbl">总盈亏</div><div class="val '+(s.total_pnl>=0?'up':'down')+'">'+(s.total_pnl>=0?'+':'')+'¥'+(s.total_pnl||0).toFixed(0)+(prevPortValue!==null?tnd(s.current_value,prevPortValue):'')+'</div></div>'+
      '<div class="port-card"><div class="lbl">收益率</div><div class="val '+(s.total_pnl_pct>=0?'up':'down')+'">'+(s.total_pnl_pct>=0?'+':'')+(s.total_pnl_pct||0).toFixed(1)+'%</div></div>'+
      '<div class="port-card"><div class="lbl">现金余额</div><div class="val">¥'+(s.cash_remaining||0).toFixed(0)+'</div></div>';
    prevPortValue=s.current_value;
    document.getElementById('posTable').querySelector('tbody').innerHTML=pos.length?pos.map(function(p){
      return'<tr><td class="sym">'+p.symbol+'</td><td class="name">'+p.name+'</td><td class="num">'+p.shares+'</td><td class="num">¥'+p.cost_price.toFixed(2)+'</td><td class="num">¥'+(p.latest_price||0).toFixed(2)+'</td><td class="num">¥'+(p.current_value||0).toFixed(0)+'</td><td class="num '+(p.pnl>=0?'up':'down')+'"><b>'+(p.pnl>=0?'+':'')+'¥'+p.pnl.toFixed(0)+'</b><br><small>'+(p.pnl_pct>=0?'+':'')+p.pnl_pct.toFixed(1)+'%</small></td><td class="sym">'+p.buy_date+'</td></tr>';
    }).join(''):'<tr><td colspan="8"><div class="empty"><div class="icon">○</div>暂无持仓<br><span style="font-size:10px;color:var(--text-mute)">推荐发布后自动模拟买入</span></div></td></tr>';
  });
}

// ── Trades page ──
function loadTrades(){
  fetch('/api/trades').then(function(r){return r.json()}).then(function(d){
    if(!d.ok)return;
    var trades=d.trades||[];
    document.getElementById('tradeTable').querySelector('tbody').innerHTML=trades.length?trades.map(function(t){
      return'<tr><td class="sym">'+t.trade_date+'</td><td class="sym">'+t.symbol+'</td><td class="name">'+t.name+'</td><td><span class="badge badge-'+(t.side==='buy'?'buy':'sell')+'">'+(t.side==='buy'?'买入':'卖出')+'</span></td><td class="num">'+t.shares+'</td><td class="num">¥'+t.price.toFixed(2)+'</td><td class="num">¥'+t.cost.toFixed(0)+'</td><td class="num">¥'+t.commission.toFixed(1)+'</td></tr>';
    }).join(''):'<tr><td colspan="8"><div class="empty"><div class="icon">○</div>暂无交易<br><span style="font-size:10px;color:var(--text-mute)">推荐发布后自动模拟买入</span></div></td></tr>';
  });
}

// ── Init ──
fetch('/api/latest').then(function(r){return r.json()}).then(function(d){
  document.getElementById('headerMeta').textContent=d.ok&&d.raw_json?'更新 '+d.run_at.replace('T',' ').substring(5,16)+' · 每日8:00/16:00自动':'等待首次分析';
  if(d.ok&&d.raw_json)try{renderAll(JSON.parse(d.raw_json));}catch(e){console.error(e);}
});
fetch('/api/auto-status').then(function(r){return r.json()}).then(function(d){
  document.getElementById('statusText').textContent=d.status==='success'?'系统运行中':'系统离线';
  if(d.alerts&&d.alerts.length){document.getElementById('alertBar').classList.add('show');document.getElementById('alertMsg').textContent=d.alerts[0].msg;}
});
loadTrack();setInterval(loadTrack,30000);
