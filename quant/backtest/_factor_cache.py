class _FactorCache:
    """test-v398 (perf): 内存优化的因子缓存包装器。
    内部存 dict-of-DataFrame (共享 Index, ~192KB/日期),
    对外 API 不变: .get(date) → {factor: Series}。
    对比原始 dict-of-Series: 全域回测 ~350MB vs ~3GB (省 ~2.5GB)。
    """
    __slots__ = ("_cache",)
    def __init__(self, raw: dict[str, dict]):
        self._cache: dict[str, "pd.DataFrame"] = {}
        for date, fv in raw.items():
            if fv:
                self._cache[date] = pd.DataFrame(fv)
    def get(self, date: str, default=None):
        df = self._cache.get(date)
        if df is None:
            return default if default is not None else {}
        # 按需转回 dict-of-Series (O(factors), 每日期 ~30 个 Series 构造)
        return {col: df[col].dropna() for col in df.columns}
    def __len__(self):
        return len(self._cache)
    def __contains__(self, date: str) -> bool:
        return date in self._cache
def _persist_backtest_result(strategy, start, end, capital, metrics, diagnosis, elapsed, avg_signals, errors):
    """ADR-037: 回测结果持久化到 backtest_runs 表，便于历史对比。"""
    import json, sqlite3
    try:
        conn = sqlite3.connect(BACKTEST_DB)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS backtest_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy TEXT NOT NULL,
                started_at TEXT DEFAULT (datetime('now','localtime')),
                start_date TEXT, end_date TEXT,
                initial_capital REAL,
                sharpe REAL, cagr_pct REAL, max_dd_pct REAL,
                sortino REAL, calmar REAL, win_rate REAL,
                dsr REAL,
                alpha REAL, info_ratio REAL, beta REAL,
                final_equity REAL, total_return_pct REAL,
                n_days INTEGER, avg_signals REAL,
                errors INTEGER, elapsed_sec REAL,
                diagnosis_json TEXT,
                UNIQUE(strategy, started_at)
            )
        """)
        conn.execute(
            "INSERT OR REPLACE INTO backtest_runs "
            "(strategy, start_date, end_date, initial_capital, "
            "sharpe, cagr_pct, max_dd_pct, sortino, calmar, win_rate, dsr, "
            "alpha, info_ratio, beta, final_equity, total_return_pct, "
            "n_days, avg_signals, errors, elapsed_sec, diagnosis_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (strategy, start, end, capital,
             metrics.get("sharpe"), metrics.get("cagr_pct"),
             metrics.get("max_drawdown_pct"),
             metrics.get("sortino"), metrics.get("calmar"),
             metrics.get("win_rate"), metrics.get("dsr"),
             metrics.get("alpha"), metrics.get("info_ratio"),
             metrics.get("beta"),
             metrics.get("final_equity"), metrics.get("total_return_pct"),
             metrics.get("n_days"), avg_signals,
             errors, elapsed,
             json.dumps(diagnosis.get("factor_report", {}), default=str)),
        )
        conn.commit()
        conn.close()
        _log.info("backtest: result persisted to backtest_runs")
    except Exception as e:
        _log.warning(f"backtest: failed to persist result (non-fatal): {e}")