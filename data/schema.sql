-- 北极星量化 · 数据库Schema
-- 排除: market.db (日线数据, 多GB, 不跟踪)
-- 跟踪: 所有应用层表结构 (存档/回溯/审计)

-- ═══ market.db (data/store.py) — 日线主库 ═══

CREATE TABLE IF NOT EXISTS stocks (
    symbol    TEXT PRIMARY KEY,
    name      TEXT,
    market    TEXT,
    list_date TEXT,
    industry  TEXT
);

CREATE TABLE IF NOT EXISTS daily (
    symbol   TEXT,
    date     TEXT,
    open     REAL,
    high     REAL,
    low      REAL,
    close    REAL,
    volume   REAL,
    amount   REAL,
    turnover REAL,
    PRIMARY KEY (symbol, date)
);
CREATE INDEX IF NOT EXISTS idx_daily_date ON daily(date);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS lhb_detail (
    symbol   TEXT,
    trade_date TEXT,
    close     REAL,
    change_pct REAL,
    turnover_rate REAL,
    net_buy    REAL,
    buy_amt   REAL,
    sell_amt  REAL,
    reason    TEXT,
    PRIMARY KEY (symbol, trade_date)
);

-- ═══ 产业链映射 (来源: 券商2025-2026展望研报) ═══

CREATE TABLE IF NOT EXISTS industry_chains (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chain_name TEXT NOT NULL,
    level TEXT NOT NULL CHECK(level IN ('上游','中游','下游')),
    sector_name TEXT NOT NULL,
    UNIQUE(chain_name, level, sector_name)
);

-- ═══ trades.db (intraday_runner.py) — 模拟/实盘交易 ═══

CREATE TABLE IF NOT EXISTS sim_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL, symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('buy','sell')),
    price REAL NOT NULL, shares INTEGER NOT NULL,
    board_count INTEGER DEFAULT 0,
    pnl REAL, pnl_pct REAL, capital_after REAL,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sim_date ON sim_trades(date);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    symbol TEXT NOT NULL,
    mode TEXT NOT NULL,
    price REAL,
    score REAL,
    board_count INTEGER DEFAULT 0,
    gap_pct REAL,
    daily_ret REAL,
    reason TEXT,
    is_bought INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_signals_date ON signals(date);
CREATE INDEX IF NOT EXISTS idx_signals_mode ON signals(date, mode);

-- ═══ results.db (web/db.py) — 分析结果 ═══
-- 注: runs/picks 表已随 ML 策略移除而弃用
-- get_conn() 仍提供共享连接 (paper/sim_broker/tracker 共用)

-- ═══ monitor.db (execution/monitor.py) — 实盘偏差监控 ═══

CREATE TABLE IF NOT EXISTS trade_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    signal_price REAL,
    actual_price REAL,
    shares INTEGER,
    slippage_bps REAL,
    status TEXT,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS daily_pnl (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    expected_return REAL,
    actual_return REAL,
    cash REAL,
    portfolio_value REAL,
    n_positions INTEGER,
    alerts TEXT
);

CREATE TABLE IF NOT EXISTS deviation_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    detail TEXT,
    severity TEXT
);

-- ═══ results.db·paper 扩展 (execution/paper.py) ═══

CREATE TABLE IF NOT EXISTS paper_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT,
    score REAL,
    signal_price REAL,
    rank INTEGER,
    actual_price REAL,
    change_pct REAL,
    is_up INTEGER,
    scored_at TEXT
);

CREATE TABLE IF NOT EXISTS paper_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    score_date TEXT NOT NULL UNIQUE,
    n_signals INTEGER,
    hit_rate REAL,
    avg_return REAL,
    cumulative_return REAL,
    score_corr REAL,
    raw_json TEXT
);

-- ═══ live.db (execution/live_broker.py) — 实盘交易 ═══

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    order_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT,
    side TEXT NOT NULL CHECK(side IN ('buy','sell')),
    shares INTEGER NOT NULL,
    signal_price REAL,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','executed','cancelled','expired')),
    filled_price REAL,
    filled_shares INTEGER,
    filled_at TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS live_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    name TEXT,
    shares INTEGER NOT NULL CHECK(shares > 0),
    cost_price REAL NOT NULL,
    total_cost REAL NOT NULL,
    buy_date TEXT NOT NULL,
    peak_price REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
    close_date TEXT,
    close_price REAL,
    close_reason TEXT
);

CREATE TABLE IF NOT EXISTS live_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT,
    side TEXT NOT NULL CHECK(side IN ('buy','sell')),
    shares INTEGER NOT NULL,
    price REAL NOT NULL,
    amount REAL NOT NULL,
    commission REAL NOT NULL,
    order_id INTEGER REFERENCES orders(id)
);

CREATE TABLE IF NOT EXISTS live_daily_pnl (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    cash REAL NOT NULL DEFAULT 5000,
    portfolio_value REAL NOT NULL,
    total_asset REAL NOT NULL,
    daily_return REAL,
    cumulative_return REAL,
    n_positions INTEGER,
    alerts TEXT
);

CREATE TABLE IF NOT EXISTS live_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ═══ results.db·sim 扩展 (engine/sim_broker.py) ═══

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    name TEXT,
    shares INTEGER NOT NULL,
    cost_price REAL NOT NULL,
    total_cost REAL NOT NULL,
    buy_date TEXT NOT NULL,
    run_id INTEGER REFERENCES runs(id),
    status TEXT DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS trade_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT,
    side TEXT NOT NULL,
    shares INTEGER NOT NULL,
    price REAL NOT NULL,
    cost REAL NOT NULL,
    commission REAL NOT NULL,
    run_id INTEGER REFERENCES runs(id)
);

-- ═══ results.db·tracking 扩展 (engine/tracker.py) ═══

CREATE TABLE IF NOT EXISTS tracking (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_date TEXT NOT NULL,
    rec_date TEXT NOT NULL,
    run_id INTEGER REFERENCES runs(id),
    symbol TEXT NOT NULL,
    name TEXT,
    rec_price REAL,
    rec_score REAL,
    rank INTEGER,
    latest_price REAL,
    change_pct REAL,
    days_held INTEGER,
    is_up INTEGER,
    is_limit_up INTEGER,
    benchmark_chg REAL,
    excess_return REAL
);

CREATE TABLE IF NOT EXISTS tracking_summary (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    track_date TEXT NOT NULL UNIQUE,
    rec_date TEXT NOT NULL,
    n_picks INTEGER,
    hit_rate REAL,
    avg_return REAL,
    max_return REAL,
    min_return REAL,
    excess_avg REAL,
    score_corr REAL,
    limit_up_hits INTEGER,
    raw_json TEXT
);
