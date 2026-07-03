"""分析师预测数据同步 — 盈利预测 + 投资评级。

数据源: akshare.stock_profit_forecast_em (全量, 无symbol参数)
        akshare.stock_rank_forecast_cninfo (每日评级变化)
表: analyst_forecast (symbol, date, report_count, buy_ratio, eps_2026, eps_2027, eps_2028)
频率: 随时更新 (API返回最新数据)
"""

import os, sqlite3, time
from datetime import datetime

import pandas as pd
from utils.logger import get_logger

logger = get_logger("data.analyst")
DB_PATH = os.path.join(os.path.dirname(__file__), "market.db")


def _ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS analyst_forecast (
            symbol TEXT NOT NULL,
            sync_date TEXT NOT NULL,
            report_count INTEGER,
            buy_count INTEGER,
            overweight_count INTEGER,
            neutral_count INTEGER,
            underweight_count INTEGER,
            eps_2026 REAL,
            eps_2027 REAL,
            eps_2028 REAL,
            PRIMARY KEY (symbol, sync_date)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_af_date ON analyst_forecast(sync_date)")
    conn.commit()


def sync_forecasts(conn=None) -> int:
    """同步全量分析师盈利预测 (单次 API 调用, ~2781只)。"""
    import akshare as ak
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        close_conn = True

    _ensure_table(conn)
    today = datetime.now().strftime("%Y-%m-%d")

    try:
        df = ak.stock_profit_forecast_em()
        if df is None or df.empty:
            return 0

        col_map = {
            '代码': 'symbol', '名称': 'name', '研报数': 'report_count',
            '机构投资评级(近六个月)-买入': 'buy_count',
            '机构投资评级(近六个月)-增持': 'overweight_count',
            '机构投资评级(近六个月)-中性': 'neutral_count',
            '机构投资评级(近六个月)-减持': 'underweight_count',
            '2026预测每股收益': 'eps_2026',
            '2027预测每股收益': 'eps_2027',
            '2028预测每股收益': 'eps_2028',
        }
        df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
        if 'symbol' in df.columns:
            df['symbol'] = df['symbol'].astype(str).str.zfill(6)

        n = 0
        for _, row in df.iterrows():
            try:
                sym = str(row.get('symbol', '')).strip()
                if len(sym) < 6:
                    continue
                conn.execute("""
                    INSERT OR REPLACE INTO analyst_forecast
                    (symbol, sync_date, report_count, buy_count, overweight_count,
                     neutral_count, underweight_count, eps_2026, eps_2027, eps_2028)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (sym, today,
                      row.get('report_count'), row.get('buy_count'),
                      row.get('overweight_count'), row.get('neutral_count'),
                      row.get('underweight_count'),
                      row.get('eps_2026'), row.get('eps_2027'), row.get('eps_2028')))
                n += 1
            except Exception:
                pass
        conn.commit()

        print(f"  {today}: {n} stocks")
        return n

    except Exception as e:
        logger.warning(f"analyst forecast sync: {e}")
        return 0
    finally:
        if close_conn:
            conn.close()


if __name__ == "__main__":
    sync_forecasts()
