"""实盘偏差监控 — 对比回测预期 vs 实盘实际。

监控维度:
  1. 信号预期收益 vs 实际收益 (日/周/月)
  2. 成交价格 vs 信号发出时价格 (滑点)
  3. 推荐买入但无法成交的比例
  4. 持仓股票是否触发止损线
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta
from utils.logger import get_logger

logger = get_logger("execution.monitor")

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "monitor.db")
_conn = None  # 懒加载连接，任务结束时释放
def _get_conn():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH)
        _conn.execute("PRAGMA journal_mode=WAL")
    return _conn


def init_monitor_db():
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trade_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,          -- buy/sell
            signal_price REAL,           -- 信号发出时的价格
            actual_price REAL,           -- 实际成交价
            shares INTEGER,
            slippage_bps REAL,           -- 滑点(bps)
            status TEXT,                 -- filled/rejected/partial
            reason TEXT                  -- 未成交原因
        );
        CREATE TABLE IF NOT EXISTS daily_pnl (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL UNIQUE,
            expected_return REAL,        -- 信号预期收益
            actual_return REAL,          -- 实际收益
            cash REAL,
            portfolio_value REAL,
            n_positions INTEGER,
            alerts TEXT
        );
        CREATE TABLE IF NOT EXISTS deviation_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            alert_type TEXT NOT NULL,    -- slippage/fill_rate/drawdown/return_gap
            detail TEXT,
            severity TEXT                -- info/warning/critical
        );
    """)
    conn.commit()


def log_trade(date: str, symbol: str, side: str, signal_price: float,
              actual_price: float, shares: int, status: str, reason: str = ""):
    slippage = (actual_price / signal_price - 1) * 10000 if signal_price > 0 else 0
    conn = _get_conn()
    conn.execute("""
        INSERT INTO trade_log (trade_date, symbol, side, signal_price, actual_price, shares, slippage_bps, status, reason)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (date, symbol, side, signal_price, actual_price, shares, round(slippage), status, reason))
    conn.commit()

    if abs(slippage) > 50:  # 滑点>50bps告警
        _alert(date, "slippage", f"{symbol} {side} 滑点{slippage:.0f}bps (signal={signal_price:.2f} actual={actual_price:.2f})", "warning")


def update_daily_pnl(date: str, recs: list, cash: float, portfolio_value: float, n_positions: int,
                     expected_return: float = 0.0, actual_return: float = 0.0):
    """记录每日盈亏。recs: 当日信号推荐的股票列表"""
    conn = _get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO daily_pnl (date, expected_return, actual_return, cash, portfolio_value, n_positions, alerts)
        VALUES (?,?,?,?,?,?,?)
    """, (date, expected_return, actual_return, cash, portfolio_value, n_positions, json.dumps([])))
    conn.commit()


def compute_deviation(expected_daily_return: float, actual_daily_return: float, days: int = 1) -> dict:
    """计算信号偏差。
    Returns: {annualized_gap, sharpe_gap, assessment}
    """
    if days < 5:
        return {"annualized_gap": 0, "assessment": "insufficient_data"}
    gap = actual_daily_return - expected_daily_return
    annual_gap = gap * 252 * 100
    if abs(annual_gap) > 50:
        assessment = "critical_gap"
    elif abs(annual_gap) > 20:
        assessment = "significant_gap"
    elif abs(annual_gap) > 5:
        assessment = "minor_gap"
    else:
        assessment = "within_range"
    return {
        "annualized_gap": round(annual_gap, 2),
        "assessment": assessment,
    }


def get_monitor_summary(days: int = 30) -> dict:
    """获取最近N天的监控摘要"""
    conn = _get_conn()
    conn.row_factory = sqlite3.Row

    # 成交率
    total_trades = conn.execute("SELECT COUNT(*) FROM trade_log WHERE trade_date >= date('now', ?)", (f"-{days} days",)).fetchone()[0]
    failed = conn.execute("SELECT COUNT(*) FROM trade_log WHERE trade_date >= date('now', ?) AND status != 'filled'", (f"-{days} days",)).fetchone()[0]
    fill_rate = 1 - failed / max(total_trades, 1)

    # 平均滑点
    avg_slippage = conn.execute("SELECT AVG(slippage_bps) FROM trade_log WHERE trade_date >= date('now', ?) AND status='filled'", (f"-{days} days",)).fetchone()[0]
    avg_slippage = round(avg_slippage, 1) if avg_slippage else 0

    # 最近告警
    alerts = [dict(r) for r in conn.execute("SELECT * FROM deviation_alerts ORDER BY id DESC LIMIT 20").fetchall()]

    return {
        "total_trades": total_trades,
        "fill_rate": round(fill_rate * 100, 1),
        "avg_slippage_bps": avg_slippage,
        "recent_alerts": alerts,
    }


def _alert(date: str, alert_type: str, detail: str, severity: str = "info"):
    conn = _get_conn()
    conn.execute("INSERT INTO deviation_alerts (date, alert_type, detail, severity) VALUES (?,?,?,?)",
                 (date, alert_type, detail, severity))
    conn.commit()
    logger.warning(f"[{severity.upper()}] {alert_type}: {detail}")


if __name__ == "__main__":
    init_monitor_db()
    print("Monitor DB initialized at", DB_PATH)
