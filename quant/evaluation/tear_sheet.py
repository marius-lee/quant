"""回测 Tear Sheet 报告生成器 (P2-4, test-v262).

对标 Alphalens / Qlib 标准报告，从 daily_equity + sim_trades 表读取数据，
生成自包含 HTML 文件 (内嵌 Plotly.js CDN, 无需本地依赖)。

报告四部分:
  ① 绩效统计卡 — 累计收益/年化波动/Sharpe/最大回撤/胜率
  ② 累计权益曲线 — 策略 vs 基准 (CSI 300) 叠加图
  ③ 回撤曲线 — 阴影面积填充，红色高亮回撤区间
  ④ 月度收益热力图 — 绿涨红跌，仿 Alphalens create_returns_tear_sheet

数据源:
  - daily_equity 表 → 权益曲线 (晚间链 reconcile 写入)
  - benchmark_daily 表 → CSI 300 基准
  - sim_trades 表 → 交易记录 (胜率计算)

约束:
  - float() 显式转换: numpy int64/float64 → Python float，避免 json.dumps TypeError
  - daily_equity 表为空时 → 占位单点曲线 (初始资金)，非报错
  - 基准归一化: bm / bm[0] * capital，使策略与基准可比
  - Plotly CDN: 2.35.2 稳定版，web/static/plotly.min.js 用 2.x API 兼容

Usage:
    from quant.evaluation.tear_sheet import generate_report_from_backtest
    path = generate_report_from_backtest()                    # 用 config 默认日期
    path = generate_report_from_backtest('2024-01-01', '2025-12-31', capital=100000)
"""

import json
from datetime import datetime
from typing import Optional
import numpy as np
import pandas as pd
from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg

_log = get_logger("evaluation.tear_sheet")

# Plotly CDN (offline fallback via local plotly.min.js)
_PLOTLY_CDN = '<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>'


def generate_report(
    equity_curve: pd.Series,
    benchmark: Optional[pd.Series] = None,
    trades: Optional[list[dict]] = None,
    title: str = "A股量化策略 — 回测报告",
    capital: float = 5000,
) -> str:
    """生成完整的 HTML Tear Sheet 报告。

    Args:
        equity_curve: Series(index=date, values=total_equity)
        benchmark: Series(index=date, values=benchmark_close)
        trades: [{symbol, side, price, shares, date, pnl, pnl_pct}, ...]
        title: 报告标题
        capital: 初始资金

    Returns:
        HTML 字符串
    """
    if equity_curve.empty:
        return f"<html><body><h1>{title}</h1><p>无数据</p></body></html>"

    # ── 1. 基础指标 (对标 Alphalens create_summary_tear_sheet) ──
    # 日收益率序列: 用于计算波动率/Sharpe
    returns = equity_curve.pct_change().dropna()
    # 累计收益: (终值-初值)/初值 × 100
    total_return = (equity_curve.iloc[-1] / capital - 1) * 100
    # 年化波动: 日收益 std × √244 (annual_trading_days = 244)
    annual_vol = returns.std() * np.sqrt(_require_cfg("market.annual_trading_days")) * 100
    # Sharpe: 日收益 mean/std × √244 (年化)
    sharpe = (returns.mean() / returns.std() * np.sqrt(244)) if returns.std() > 0 else 0

    # 最大回撤: (当前值 - 历史峰值) / 历史峰值 的最小值
    peak = equity_curve.cummax()
    drawdown = (equity_curve - peak) / peak
    max_dd = drawdown.min() * 100

    # 胜率: 卖出交易中盈利笔数/总卖出笔数
    if trades:
        sell_trades = [t for t in trades if t.get("side") == "sell"]
        wins = sum(1 for t in sell_trades if t.get("pnl", 0) > 0)
        win_rate = wins / len(sell_trades) * 100 if sell_trades else 0
    else:
        win_rate = 0

    # ── 2. 构建 HTML ──
    stats = {
        "累计收益": f"{total_return:.1f}%",
        "年化波动": f"{annual_vol:.1f}%",
        "Sharpe": f"{sharpe:.2f}",
        "最大回撤": f"{max_dd:.1f}%",
        "胜率": f"{win_rate:.1f}%",
        "交易天数": len(returns),
        "起始日期": str(equity_curve.index[0])[:10],
        "结束日期": str(equity_curve.index[-1])[:10],
    }

    # Equity curve data
    equity_json = json.dumps({
        "dates": [str(d)[:10] for d in equity_curve.index],
        "values": [round(float(v), 2) for v in equity_curve.values],
        "benchmark": [round(float(v), 2) for v in benchmark.values] if benchmark is not None else [],
    })

    # Drawdown data
    dd_json = json.dumps({
        "dates": [str(d)[:10] for d in drawdown.index],
        "values": [round(float(v) * 100, 2) for v in drawdown.values],
    })

    # Monthly returns heatmap
    # B30 (2026-08-18): equity_curve.index 可能是 object dtype (str/混合) →
    # returns.index.year AttributeError → 报告必崩. 统一转 DatetimeIndex.
    _ret = returns.copy()
    _ret.index = pd.to_datetime(_ret.index, errors="coerce")
    _ret = _ret.dropna()
    monthly = _ret.groupby([_ret.index.year, _ret.index.month]).apply(
        lambda x: (1 + x).prod() - 1
    ).unstack()
    monthly_pct = (monthly * 100).round(1).fillna(0)

    heatmap_data = []
    for yr in sorted(monthly.index):
        for mo in range(1, 13):
            val = monthly_pct.loc[yr, mo] if mo in monthly_pct.columns else 0
            heatmap_data.append({"year": yr, "month": mo, "return": float(val)})

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><title>{title}</title>
<style>
  body {{ font-family: -apple-system, sans-serif; max-width: 1200px; margin: 0 auto; padding: 20px; background: #1a1a2e; color: #e0e0e0; }}
  h1 {{ color: #f2964a; }}
  .stats {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 20px 0; }}
  .stat {{ background: #16213e; padding: 16px; border-radius: 8px; text-align: center; }}
  .stat .label {{ font-size: 12px; color: #888; }}
  .stat .value {{ font-size: 24px; font-weight: bold; color: #f2964a; }}
  .chart {{ background: #16213e; border-radius: 8px; padding: 12px; margin: 16px 0; }}
  .heatmap {{ display: grid; grid-template-columns: 80px repeat(12, 1fr); gap: 2px; }}
  .heatmap .cell {{ aspect-ratio: 1; display: flex; align-items: center; justify-content: center; font-size: 11px; border-radius: 3px; }}
</style>
{_PLOTLY_CDN}
</head>
<body>
<h1>{title}</h1>
<p>生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')} | 初始资金: ¥{capital:,.0f}</p>

<div class="stats">
{''.join(f'<div class="stat"><div class="label">{k}</div><div class="value">{v}</div></div>' for k, v in stats.items())}
</div>

<div class="chart" id="chart-equity" style="height:400px"></div>
<div class="chart" id="chart-drawdown" style="height:200px"></div>
<div class="chart" id="chart-monthly" style="height:300px"></div>

<script>
// ── Plotly 图表配置 (v2.35.2, 兼容 web/static/plotly.min.js) ──
const equity = {equity_json};
const dd = {dd_json};
const heatmap = {json.dumps(heatmap_data)};
const years = [...new Set(heatmap.map(d => d.year))].sort();
const months = ['1月','2月','3月','4月','5月','6月','7月','8月','9月','10月','11月','12月'];

// Equity curve
const traces = [{{
    x: equity.dates, y: equity.values, type: 'scatter', name: '策略',
    line: {{ color: '#f2964a', width: 2 }}
}}];
if (equity.benchmark) {{
    traces.push({{
        x: equity.dates, y: equity.benchmark, type: 'scatter', name: '基准',
        line: {{ color: '#888', width: 1, dash: 'dot' }}
    }});
}}
Plotly.newPlot('chart-equity', traces, {{
    template: 'plotly_dark', margin: {{ l: 60, r: 20, t: 10, b: 40 }},
    title: '累计权益曲线'
}}, {{ responsive: true }});

// Drawdown
Plotly.newPlot('chart-drawdown', [{{
    x: dd.dates, y: dd.values, type: 'scatter', fill: 'tozeroy',
    name: '回撤', line: {{ color: '#e74c3c', width: 1 }},
    fillcolor: 'rgba(231,76,60,0.15)'
}}], {{
    template: 'plotly_dark', margin: {{ l: 60, r: 20, t: 10, b: 40 }},
    title: '回撤曲线 (%)'
}}, {{ responsive: true }});

// Monthly returns heatmap
Plotly.newPlot('chart-monthly', [{{
    type: 'heatmap',
    z: years.map(y => months.map((_, mi) => {{
        const d = heatmap.find(h => h.year === y && h.month === mi + 1);
        return d ? d.return : 0;
    }})),
    x: months, y: years,
    colorscale: [[0, '#e74c3c'], [0.5, '#1a1a2e'], [1, '#4CAF50']],
    text: years.map(y => months.map((_, mi) => {{
        const d = heatmap.find(h => h.year === y && h.month === mi + 1);
        return d ? d.return.toFixed(1) + '%' : '';
    }})),
    texttemplate: '%{{text}}',
    colorbar: {{ title: '%' }}
}}], {{
    template: 'plotly_dark', margin: {{ l: 60, r: 20, t: 10, b: 40 }},
    title: '月度收益热力图'
}}, {{ responsive: true }});
</script>
</body></html>"""
    return html


def generate_report_from_backtest(
    start: str = None,
    end: str = None,
    capital: float = None,
    output: str = None,
) -> str:
    """一键生成: 从 DB 读取回测数据 → 生成 HTML 报告。

    数据源 (按优先级):
      1. daily_equity 表 — 每日权益 (晚间链 reconcile 写入)
      2. benchmark_daily 表 — CSI 300 基准收盘价
      3. sim_trades 表 — 交易记录

    参数:
      start: 起始日期 (None=backtest.default_start, 2020-01-01)
      end:   结束日期 (None=backtest.default_end)
      capital: 初始资金 (None=backtest.default_capital, 5000)
      output: 输出路径 (None=自动生成 reports/tear_sheet_YYYYMMDD_HHMM.html)

    约束:
      - daily_equity 为空时 (新账户无历史) → 占位单点曲线，非抛错
      - 基准归一化到策略初值 = bm/bm[0]×capital，使曲线可同比
      - HTML 内嵌 Plotly CDN (2.35.2)，离线环境需本地 plotly.min.js
      - float() 转换 numpy→Python，避免 json.dumps TypeError

    Returns: 输出文件路径
    """
    import sqlite3, os
    from quant.config.paths import TRADE_DB, MARKET_DB

    if capital is None:
        capital = _require_cfg("backtest.default_capital")
    if start is None:
        start = _require_cfg("backtest.default_start")
    if end is None:
        end = _require_cfg("backtest.default_end")
    if output is None:
        output = os.path.join(os.path.dirname(TRADE_DB), "..", "..", "reports",
                              f"tear_sheet_{datetime.now().strftime('%Y%m%d_%H%M')}.html")
    os.makedirs(os.path.dirname(output), exist_ok=True)

    # Load equity curve
    tconn = sqlite3.connect(TRADE_DB)
    eq_rows = tconn.execute(
        "SELECT date, total_equity FROM daily_equity WHERE date >= ? AND date <= ? ORDER BY date",
        (start, end)
    ).fetchall()
    equity = pd.Series({r[0]: r[1] for r in eq_rows})

    # Load benchmark
    mconn = sqlite3.connect(MARKET_DB)
    bm_rows = mconn.execute(
        "SELECT date, close FROM benchmark_daily WHERE index_code='000300' AND date >= ? AND date <= ? ORDER BY date",
        (start, end)
    ).fetchall()
    bm = pd.Series({r[0]: r[1] for r in bm_rows})
    mconn.close()

    # Load trades
    trades = tconn.execute(
        "SELECT date, symbol, side, price, shares, pnl, pnl_pct FROM sim_trades "
        "WHERE date >= ? AND date <= ? ORDER BY date, id",
        (start, end)
    ).fetchall()
    trade_list = [
        {"date": r[0], "symbol": r[1], "side": r[2], "price": r[3],
         "shares": r[4], "pnl": r[5], "pnl_pct": r[6]}
        for r in trades
    ]
    tconn.close()

    if equity.empty:
        _log.warning("tear_sheet: no daily_equity data, generating empty report")
        equity = pd.Series({start: float(capital)})

    if bm.empty:
        bm = None
    else:
        # Normalize benchmark to same scale as equity
        bm = bm / bm.iloc[0] * capital

    html = generate_report(equity, bm, trade_list, capital=capital)
    with open(output, 'w') as f:
        f.write(html)
    _log.info(f"tear_sheet: report saved to {output}")
    return output
