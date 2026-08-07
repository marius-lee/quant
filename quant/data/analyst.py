"""分析师预测数据同步 — 盈利预测 + 投资评级。

数据源: akshare.stock_profit_forecast_em (全量, 无symbol参数)
        akshare.stock_rank_forecast_cninfo (每日评级变化)
表: analyst_forecast (symbol, date, report_count, buy_ratio, 动态eps列)
频率: 随时更新 (API返回最新数据)

eps列名根据当前年份动态生成: eps_{year}, eps_{year+1}, eps_{year+2}
列在首次遇到时通过 ALTER TABLE ADD COLUMN 自动添加。
"""

import os, sqlite3, time, re
from datetime import datetime

import pandas as pd
from quant.utils.logger import get_logger
from quant.config.paths import MARKET_DB

logger = get_logger("data.analyst")
DB_PATH = MARKET_DB

# ── 动态 EPS 列 — 基于当前年份, 每年自动滚动 ──
_CURRENT_YEAR = datetime.now().year
_EPS_YEARS = [_CURRENT_YEAR, _CURRENT_YEAR + 1, _CURRENT_YEAR + 2]
_EPS_COLS = [f"eps_{y}" for y in _EPS_YEARS]
_EPS_CN_MAP = {f"{y}预测每股收益": f"eps_{y}" for y in _EPS_YEARS}
logger.info(f"analyst EPS columns: {_EPS_COLS} (year range {_EPS_YEARS[0]}-{_EPS_YEARS[-1]})")


def _ensure_table(conn):
    """建表 + 动态添加年份 EPS 列。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS analyst_forecast (
            symbol TEXT NOT NULL,
            sync_date TEXT NOT NULL,
            report_count INTEGER,
            buy_count INTEGER,
            overweight_count INTEGER,
            neutral_count INTEGER,
            underweight_count INTEGER,
            PRIMARY KEY (symbol, sync_date)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_af_date ON analyst_forecast(sync_date)")

    # 动态添加 eps_{year} 列 (每年自动扩展)
    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(analyst_forecast)").fetchall()}
    for col in _EPS_COLS:
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE analyst_forecast ADD COLUMN {col} REAL")
            logger.info(f"analyst_forecast: added column {col}")

    conn.commit()


def _build_col_map(df_columns: list) -> dict:
    """从 API 返回的列名动态构建 col_map。

    固定列 + 动态 EPS 列 (匹配 '{year}预测每股收益' 模式)。
    """
    col_map = {
        '代码': 'symbol', '名称': 'name', '研报数': 'report_count',
        '机构投资评级(近六个月)-买入': 'buy_count',
        '机构投资评级(近六个月)-增持': 'overweight_count',
        '机构投资评级(近六个月)-中性': 'neutral_count',
        '机构投资评级(近六个月)-减持': 'underweight_count',
    }
    # 动态 EPS 列: 匹配 '2026预测每股收益' → 'eps_2026'
    for cn_col in df_columns:
        m = re.match(r'(\d{4})预测每股收益', cn_col)
        if m:
            year = int(m.group(1))
            col_map[cn_col] = f"eps_{year}"
            # 确保该列在表中存在
            if f"eps_{year}" not in _EPS_COLS:
                _EPS_COLS.append(f"eps_{year}")
    return col_map


def sync_forecasts(conn=None) -> int:
    """同步全量分析师盈利预测 (单次 API 调用, ~2781只)。"""
    import akshare as ak
    from quant.data.datasource_retry import datasource_retry
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        close_conn = True

    _ensure_table(conn)
    today = datetime.now().strftime("%Y-%m-%d")

    @datasource_retry
    def _fetch_forecasts():
        return ak.stock_profit_forecast_em()

    df = _fetch_forecasts()
    if df is None or df.empty:
        return 0

    col_map = _build_col_map(list(df.columns))
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    if 'symbol' in df.columns:
        df['symbol'] = df['symbol'].astype(str).str.zfill(6)

    # 收集当前行包含的所有列名 (动态 INSERT)
    base_cols = ['symbol', 'sync_date', 'report_count', 'buy_count',
                 'overweight_count', 'neutral_count', 'underweight_count']
    eps_cols_in_row = [c for c in _EPS_COLS if c in df.columns]

    n = 0
    for _, row in df.iterrows():
        sym = str(row.get('symbol', '')).strip()
        if len(sym) < 6:
            continue
        values = [
            sym, today,
            row.get('report_count'), row.get('buy_count'),
            row.get('overweight_count'), row.get('neutral_count'),
            row.get('underweight_count'),
        ]
        for ec in eps_cols_in_row:
            values.append(row.get(ec))

        placeholders = ', '.join(['?'] * len(values))
        cols_str = ', '.join(base_cols + eps_cols_in_row)
        conn.execute(
            f"INSERT OR REPLACE INTO analyst_forecast ({cols_str}) VALUES ({placeholders})",
            values
        )
        n += 1
    conn.commit()

    if close_conn:
        conn.close()
    logger.info(f"  analyst forecasts: {today} — {n} stocks synced")
    return n


if __name__ == "__main__":
    sync_forecasts()
