"""龙虎榜因子 — 游资/机构追踪，从 SQLite lhb_detail 表读取。

因子:
  1. lhb_appear  — 近20日上榜次数
  2. lhb_net_buy — 近20日净买入总额(万元)
  3. lhb_score   — 龙虎榜综合得分 (-0.5~1.0, 正=净买, 负=净卖)

接入: factor/compute.py 中调用 compute(store, symbols, date)。
"""
import numpy as np
import pandas as pd
from utils.logger import get_logger

logger = get_logger("factor.dragon_tiger")


def compute(store, symbols: list, date: str, lookback: int = 20) -> pd.DataFrame:
    """从 lhb_detail 表读取龙虎榜数据，计算因子。

    Args:
        store: DataStore 实例
        symbols: 股票代码列表
        date: 当前日期 (YYYY-MM-DD 或 YYYYMMDD)
        lookback: 回溯交易日数

    Returns DataFrame with columns: lhb_appear, lhb_net_buy, lhb_score,
           每列(stock × 1), 可 concat 到 all_wide。
    """
    conn = store._connect()
    date_clean = date.replace("-", "") if "-" in str(date) else str(date)
    # 从 daily 表查 lookback 天前的日期
    date_row = conn.execute(
        "SELECT date FROM daily WHERE date <= ? ORDER BY date DESC LIMIT 1",
        (date_clean,)
    ).fetchone()
    if not date_row:
        return pd.DataFrame(0.0, index=symbols,
                           columns=["lhb_appear", "lhb_net_buy", "lhb_score"])
    end_date = date_row[0]

    start_row = conn.execute(
        """SELECT date FROM daily WHERE date <= ?
           ORDER BY date DESC LIMIT 1 OFFSET ?""",
        (end_date, lookback - 1)
    ).fetchone()
    start_date = start_row[0] if start_row else end_date

    ph = ",".join("?" for _ in symbols)
    rows = conn.execute(
        f"""SELECT symbol, COUNT(*) as cnt, SUM(net_buy) as total_net
            FROM lhb_detail
            WHERE symbol IN ({ph})
              AND trade_date >= ? AND trade_date <= ?
            GROUP BY symbol""",
        symbols + [start_date, end_date]
    ).fetchall()

    # 构建结果
    result = pd.DataFrame(0.0, index=symbols,
                          columns=["lhb_appear", "lhb_net_buy", "lhb_score"])
    if not rows:
        return result

    for sym, cnt, total_net in rows:
        if sym not in result.index:
            continue
        result.at[sym, "lhb_appear"] = cnt
        result.at[sym, "lhb_net_buy"] = round(total_net or 0, 2)
        # 综合得分: 净买>0 → 正分, 净卖→负分, 按上榜次数封顶 ±1
        score = (1.0 if (total_net or 0) > 0 else -0.5) * min(cnt, 5) / 5
        result.at[sym, "lhb_score"] = round(score, 4)

    return result
