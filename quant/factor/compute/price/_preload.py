def preload_ztd_cache(dates: list, all_symbols: list):
    """一次性预计算所有日期的 ztd, 消除每日重复 SQLite 查询.

    dates: 回测窗口内所有交易日 (YYYY-MM-DD)
    all_symbols: 全量股票代码列表
    """
    global _ztd_cache
    _ztd_cache.clear()
    if not dates or not all_symbols:
        return

    import pandas as pd
    earliest = pd.Timestamp(min(dates)) - pd.Timedelta(days=_require_cfg("data.lookback_days") + 10)
    latest = pd.Timestamp(max(dates))

    conn = DatabaseManager.market()
    ph = ",".join(["?"] * len(all_symbols))
    rows = conn.execute(
        f"""SELECT date, symbol, volume
            FROM daily
            WHERE date BETWEEN ? AND ?
              AND symbol IN ({ph})
            ORDER BY symbol, date""",
        [earliest.strftime("%Y-%m-%d"), latest.strftime("%Y-%m-%d")] + list(all_symbols)
    ).fetchall()
    conn.close()

    if not rows:
        _log.warning("preload_ztd_cache: no rows for %d symbols x %d days",
                    len(all_symbols), len(dates))
        return

    df = pd.DataFrame(rows, columns=['date', 'symbol', 'volume'])
    df['date'] = pd.to_datetime(df['date'])

    # 向量化 (审计 P1-7, 2026-07-26): 原每日期全表过滤 + groupby
    # (134 dates 实测 91s) → 每股 rolling(250) 一次 + merge_asof 查表。
    # 语义: 每股最后 250 行 (≤d) 中零成交占比, 与原实现一致;
    # 唯一差异: volume NaN 行不计入 total (原按行数, daily.volume 实际无 NaN)。
    df = df.sort_values(['symbol', 'date'])
    df['zero'] = (df['volume'] == 0).astype(float)
    rolled = df.groupby('symbol', sort=False).rolling(250, min_periods=1)
    df['ztd'] = (rolled['zero'].sum() / rolled['volume'].count()).values
    df = df.sort_values('date')
    grid = pd.MultiIndex.from_product(
        [sorted(pd.Timestamp(d) for d in dates), df['symbol'].unique()],
        names=['date', 'symbol']).to_frame(index=False)
    merged = pd.merge_asof(grid, df[['date', 'symbol', 'ztd']],
                           on='date', by='symbol')
    for d in dates:
        s = (merged.loc[merged['date'] == pd.Timestamp(d)]
             .set_index('symbol')['ztd'].dropna())
        if s.empty:
            continue
        _ztd_cache[d] = s

    _log.info("preload_ztd_cache: precomputed %d dates for %d symbols",
             len(_ztd_cache), len(all_symbols))


