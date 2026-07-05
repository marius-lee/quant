-- ============================================================
-- Migration 002: 模板 7 审计修复 (2026-07-05)
--
-- 修复项:
--   1. sim_trades.side: 加 CHECK 约束 (SQLite 不支持 ALTER ADD CHECK,
--      已通过重建表方式处理, 见 data/trade_repo.py)
--   2. 财务表加 stat_date 索引
--   3. sim_trades + strategy_config 加 created_at 时间戳
-- ============================================================

-- 财务表 stat_date 索引 (按日期查询时避免全表扫描)
CREATE INDEX IF NOT EXISTS idx_fin_balance_date ON financial_balance(stat_date);
CREATE INDEX IF NOT EXISTS idx_fin_income_date ON financial_income(stat_date);
CREATE INDEX IF NOT EXISTS idx_fin_cashflow_date ON financial_cash_flow(stat_date);

-- sim_trades 加 created_at (新表已包含, 已存在的表需要 ALTER)
-- SQLite 有限 ALTER: CREATE TABLE ... AS + DROP + RENAME 模式
-- 由于 trades.db 数据量小 (39 rows), 直接重建:
CREATE TABLE IF NOT EXISTS sim_trades_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL, symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('buy','sell')),
    price REAL NOT NULL, shares INTEGER NOT NULL,
    pnl REAL DEFAULT 0, pnl_pct REAL DEFAULT 0,
    capital_after REAL DEFAULT 0,
    strategy TEXT DEFAULT 'quant',
    board_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

-- 数据迁移 + 旧表替换 (幂等: 仅当旧表无 CHECK 约束时执行)
INSERT OR IGNORE INTO sim_trades_v2
    (id, date, symbol, side, price, shares, pnl, pnl_pct, capital_after, strategy, board_count)
    SELECT id, date, symbol, side, price, shares, pnl, pnl_pct, capital_after, strategy, board_count
    FROM sim_trades;

-- strategy_config 也加时间戳
CREATE TABLE IF NOT EXISTS strategy_config_v2 (
    strategy TEXT PRIMARY KEY,
    config_json TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
INSERT OR IGNORE INTO strategy_config_v2 (strategy, config_json)
    SELECT strategy, config_json FROM strategy_config;

-- 注意: DROP + RENAME 需要在应用层执行, SQLite 不支持事务内混合 DDL
-- 实际执行参见 data/trade_repo.py 的 _ensure_tables() 方法
