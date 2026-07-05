-- ============================================================
-- Migration 001: Schema 基线 (2026-07-05)
-- 模板 7 审计前 — 记录现有所有表结构作为历史基线
-- ============================================================

-- === market.db ===

CREATE TABLE IF NOT EXISTS stocks (
    symbol    TEXT PRIMARY KEY,
    name      TEXT,
    market    TEXT,
    list_date TEXT,
    pe        REAL, pe_ttm REAL, pb REAL,
    total_mv  REAL, circ_mv REAL, div_yield REAL,
    eps       REAL, bvps REAL, cfps REAL,
    high_52w  REAL, low_52w REAL, turnover_rate REAL,
    industry  TEXT, roe REAL
);

CREATE TABLE IF NOT EXISTS daily (
    symbol   TEXT, date TEXT,
    open REAL, high REAL, low REAL, close REAL,
    volume REAL, amount REAL, turnover REAL,
    PRIMARY KEY (symbol, date)
);
CREATE INDEX IF NOT EXISTS idx_daily_date ON daily(date);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS lhb_detail (
    symbol TEXT, trade_date TEXT,
    close REAL, change_pct REAL, turnover_rate REAL,
    net_buy REAL, buy_amt REAL, sell_amt REAL, reason TEXT,
    PRIMARY KEY (symbol, trade_date)
);

CREATE TABLE IF NOT EXISTS daily_valuation (
    symbol TEXT, date TEXT,
    pe_ttm REAL, pb REAL, ps_ttm REAL, pcf_ttm REAL,
    market_cap REAL, turnover_rate REAL, source TEXT DEFAULT 'jqdata',
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS factor_registry (
    name TEXT PRIMARY KEY, category TEXT, compute_fn TEXT,
    academic_source TEXT, status TEXT, status_reason TEXT,
    ic_mean REAL, ic_ir REAL, direction TEXT,
    last_evaluated TEXT, created_at TEXT, updated_at TEXT
);

CREATE TABLE IF NOT EXISTS factor_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data TEXT, created_at TEXT, n_symbols INTEGER, lookback INTEGER
);

CREATE TABLE IF NOT EXISTS financial_balance (
    symbol TEXT NOT NULL, stat_date TEXT NOT NULL,
    pub_date TEXT, total_assets REAL, total_liability REAL,
    total_owner_equities REAL, equities_parent_company_owners REAL,
    minority_interests REAL, fixed_assets REAL, intangible_assets REAL,
    good_will REAL, inventories REAL, account_receivable REAL,
    total_current_assets REAL, total_current_liability REAL,
    shortterm_loan REAL, longterm_loan REAL,
    PRIMARY KEY (symbol, stat_date)
);

CREATE TABLE IF NOT EXISTS financial_income (
    symbol TEXT NOT NULL, stat_date TEXT NOT NULL,
    pub_date TEXT,
    total_operating_revenue REAL, operating_revenue REAL,
    operating_cost REAL, operating_profit REAL, net_profit REAL,
    total_profit REAL, income_tax_expense REAL, administration_expense REAL,
    PRIMARY KEY (symbol, stat_date)
);

CREATE TABLE IF NOT EXISTS financial_cash_flow (
    symbol TEXT NOT NULL, stat_date TEXT NOT NULL,
    pub_date TEXT,
    net_operate_cash_flow REAL, net_invest_cash_flow REAL,
    net_finance_cash_flow REAL, cash_and_equivalents_at_end REAL,
    goods_sale_and_service_render_cash REAL, fix_intan_other_asset_acqui_cash REAL,
    PRIMARY KEY (symbol, stat_date)
);

-- === trades.db ===

CREATE TABLE IF NOT EXISTS sim_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL, symbol TEXT NOT NULL,
    side TEXT NOT NULL,   -- 修复前无 CHECK 约束
    price REAL NOT NULL, shares INTEGER NOT NULL,
    pnl REAL DEFAULT 0, pnl_pct REAL DEFAULT 0,
    capital_after REAL DEFAULT 0, strategy TEXT DEFAULT 'quant',
    board_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS strategy_config (
    strategy TEXT PRIMARY KEY, config_json TEXT
);
