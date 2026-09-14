"""FactorStore query/maintenance methods mixin."""
import json
import os
import time as _time
import numpy as np
import pandas as pd
from quant.config.constants import _require_cfg
from quant.utils.logger import get_logger

logger = get_logger("factor.store.query")


class FactorStoreQueryMixin:
    """Query and maintenance methods for FactorStore."""

def load(self, date_str: str, symbols=None, factor_names=None) -> dict:
    """从缓存读取单日因子值。返回 {factor_name: Series(symbol→value)}。"""
    trading_days = self._load_trading_days()
    if date_str not in trading_days:
        return {}
    date_idx = trading_days.index(date_str)
    year = int(date_str[:4])

    sym_map = self._load_symbol_map()
    idx_to_sym = {v: k for k, v in sym_map.items()}

    result = {}
    # 遍历 parquet_f/{factor}/{year}.parquet
    for fname in os.listdir(self._parquet_dir):
        if factor_names and fname not in factor_names:
            continue
        fdir = os.path.join(self._parquet_dir, fname)
        if not os.path.isdir(fdir):
            continue
        ppath = os.path.join(fdir, f"{year}.parquet")
        if not os.path.exists(ppath):
            continue
        df = pd.read_parquet(ppath, filters=[('date_i16', '=', date_idx)])
        if df.empty:
            continue
        series = pd.Series(
            df['value_f32'].values,
            index=[idx_to_sym.get(int(s), str(s)) for s in df['symbol_i16'].values],
            name=fname,
        )
        if symbols:
            series = series.reindex(symbols)
        result[fname] = series
    return result

def bulk_load(self, dates: list[str], symbols=None, factor_names=None) -> dict[str, dict]:
    """批量加载多日因子值。返回 {date_str: {factor_name: Series}}."""
    cache: dict[str, dict] = {}
    trading_days = self._load_trading_days()
    sym_map = self._load_symbol_map()
    idx_to_sym = {v: k for k, v in sym_map.items()}

    years = set(int(d[:4]) for d in dates)
    date_to_idx = {d: trading_days.index(d) for d in dates if d in trading_days}

    # 预加载各因子各年的 parquet
    factor_year_dfs: dict[str, dict[int, pd.DataFrame]] = {}
    for fname in os.listdir(self._parquet_dir):
        if factor_names and fname not in factor_names:
            continue
        fdir = os.path.join(self._parquet_dir, fname)
        if not os.path.isdir(fdir):
            continue
        factor_year_dfs[fname] = {}
        for f in os.listdir(fdir):
            if f.endswith('.parquet'):
                yr = int(f[:-len('.parquet')])
                if yr in years:
                    factor_year_dfs[fname][yr] = pd.read_parquet(
                        os.path.join(fdir, f),
                        filters=[('date_i16', 'in', [date_to_idx[d] for d in dates if d in date_to_idx and d[:4] == str(yr)])],
                    )

    for date_str in dates:
        if date_str not in date_to_idx:
            continue
        date_idx = date_to_idx[date_str]
        year = int(date_str[:4])
        fv = {}
        for fname, year_dfs in factor_year_dfs.items():
            df = year_dfs.get(year)
            if df is None or df.empty:
                continue
            rows = df[df['date_i16'] == date_idx]
            if rows.empty:
                continue
            series = pd.Series(
                rows['value_f32'].values,
                index=[idx_to_sym.get(int(s), str(s)) for s in rows['symbol_i16'].values],
                name=fname,
            )
            if symbols:
                series = series.reindex(symbols)
            fv[fname] = series
        if fv:
            cache[date_str] = fv
    return cache

# ── 查询 ──

def _get_existing_factors(self, date_str: str) -> set:
    """返回该日期已物化的因子名集合 (v471: 读因子 meta dates 集合, 免 parquet 逐日扫描)。"""
    existing = set()
    for fname in os.listdir(self._parquet_dir):
        fdir = os.path.join(self._parquet_dir, fname)
        if not os.path.isdir(fdir):
            continue
        meta = self._load_factor_meta(fname)
        if not meta or date_str not in set(meta.get("dates", [])):
            continue
        # source_hash 不匹配 → 因子代码已变, 该日期需重算 (per-factor 口径)
        if "source_hash" in meta and meta["source_hash"] != _source_hash_single(fname):
            continue
        # v539: 删除整库 data_hash 判定 — 整库指纹随每日增量变化
        # (晚间链拉新数据 → daily COUNT/MAX(date) 变) → 全段缓存误判
        # 缺失 → 回测 IC 检查全缺报错 (2026-08-18 实证 239 天全缺).
        # 数据指纹仅作审计字段保留; 历史回填后须 force 全量重物化
        # (scripts/materialize_full.sh, v529 force 语义)
        existing.add(fname)
    return existing

def is_materialized(self, date_range: list[str], factor_names: list[str]) -> bool:
    """检查是否所有日期都覆盖全部因子。"""
    if not date_range:
        return False
    check_dates = [date_range[0], date_range[-1]]
    step = max(1, len(date_range) // 20)
    for i in range(step, len(date_range) - 1, step):
        check_dates.append(date_range[i])
    return all(not self._date_missing_factors(d, factor_names) for d in check_dates)

def _date_missing_factors(self, date_str: str, factor_names: list[str],
                          source_hash: str = None) -> list[str]:
    """返回该日期缺失的因子列表 (空 = 已完全物化)。"""
    existing = self._get_existing_factors(date_str)
    return sorted(set(factor_names) - existing)

def list_cached_dates(self) -> list[str]:
    """返回缓存中全部已物化日期 (升序)。"""
    return self._load_trading_days()

def latest_cached_date(self) -> str | None:
    """返回缓存中最新物化日期。"""
    dates = self.list_cached_dates()
    return dates[-1] if dates else None

def trim_to_max_days(self, max_days: int) -> int:
    """删除超过 max_days 天前的旧缓存 (重映射 date_i16 保持全局索引一致)。"""
    if max_days <= 0:
        return 0
    latest = self.latest_cached_date()
    if not latest:
        return 0
    dates = self._load_trading_days()
    cutoff_idx = max(0, len(dates) - max_days)
    if cutoff_idx == 0:
        return 0
    cutoff_date = dates[cutoff_idx]
    _log.info("factor_cache: trim — cutoff=%s (max_days=%d, latest=%s)", cutoff_date, max_days, latest)
    deleted = 0
    # 删除超过 cutoff_idx 的日期在 parquet 中的行, 并重映射 date_i16 →
    # 新索引 (保全局交易日序号连续, 与 trading_days.json 一致)
    for fname in os.listdir(self._parquet_dir):
        fdir = os.path.join(self._parquet_dir, fname)
        if not os.path.isdir(fdir):
            continue
        for f in os.listdir(fdir):
            if not f.endswith('.parquet'):
                continue
            ppath = os.path.join(fdir, f)
            df = pd.read_parquet(ppath)
            kept = df[df['date_i16'] >= cutoff_idx].copy()
            if len(kept) < len(df):
                if len(kept) == 0:
                    os.remove(ppath)
                else:
                    kept['date_i16'] = kept['date_i16'] - cutoff_idx
                    kept['date_i16'] = kept['date_i16'].astype('int16')
                    kept.to_parquet(ppath, compression='zstd', compression_level=3, index=False)
                deleted += len(df) - len(kept)
    # 更新 trading_days (索引 0 = cutoff_date, 与重映射后的 date_i16 对齐)
    new_dates = dates[cutoff_idx:]
    self._save_trading_days(new_dates)
    # 因子 meta 的 dates 字段同样裁剪
    for fname in os.listdir(self._parquet_dir):
        meta = self._load_factor_meta(fname)
        if not meta or "dates" not in meta:
            continue
        meta["dates"] = [d for d in meta.get("dates", []) if d >= cutoff_date]
        if not meta["dates"]:
            meta.pop("dates", None)
        meta["first_date"] = meta["dates"][0] if meta.get("dates") else None
        meta["last_date"] = meta["dates"][-1] if meta.get("dates") else None
        self._save_factor_meta(fname, meta)
    return deleted

# ── 维护 ──

def _log_materialization(self, start, end, n_factors, n_symbols,
                         n_dates, n_rows, elapsed, force):
    try:
        record = {
            "ts": pd.Timestamp.now().isoformat(),
            "date_start": start, "date_end": end,
            "n_factors": n_factors, "n_symbols": n_symbols,
            "n_dates": n_dates, "n_rows": n_rows,
            "elapsed_sec": round(elapsed, 1), "force": bool(force),
        }
        with open(self._log_file, 'a') as f:
            f.write(json.dumps(record) + "\n")
    except Exception as _e:
        _log.warning("factor_cache: failed to log materialization: %s", _e)

# ── checkpoint ──

def _load_blocked(self) -> dict:
    """读 blocked 记录 {date: {factor: ts}} — 缺数据空结果因子 (v483).

    date1: 记录在因子缓存目录 blocked.json; TTL 过期自动解除 (数据补齐后
    下一轮重算, 能算出非空结果即恢复正常); 零 fallback: 损坏按空处理。
    """
    if not os.path.exists(_BLOCKED_PATH):
        return {}
    try:
        with open(_BLOCKED_PATH, "r") as f:
            raw = json.load(f)
    except Exception as e:
        _log.warning("factor_cache: blocked.json unreadable, treating as empty: %s", e)
        return {}
    ttl = _require_cfg("factor.compute.cache_checkpoint_ttl_sec")   # 86400s = 1天
    now = _time.time()
    out = {}
    for d, facs in raw.items():
        if not isinstance(facs, dict):
            continue
        alive = {f: ts for f, ts in facs.items()
                 if isinstance(ts, (int, float)) and now - ts < ttl}
        if alive:
            out[d] = alive
        else:
            _log.info("factor_cache: blocked expired for %s (%d records) — 重试",
                      d, len(facs) if isinstance(facs, dict) else 0)
    return out

def _save_blocked(self, blocked: dict) -> None:
    """持久化 blocked 记录 (chunk 结束后写, 崩溃可续)."""
    try:
        os.makedirs(os.path.dirname(_BLOCKED_PATH), exist_ok=True)
        with open(_BLOCKED_PATH, "w") as f:
            json.dump(blocked, f, indent=1)
    except Exception as e:
        _log.warning("factor_cache: blocked save failed (non-fatal): %s", e)

def _clear_blocked_for(self, date_str: str, factor_or_none: str = None) -> None:
    """数据补齐后手动解除 blocked (因子能算出非空结果时将不再 blocked; 此方法供故障排查调用)."""
    blocked = self._load_blocked()
    if date_str not in blocked:
        return
    if factor_or_none is None:
        blocked.pop(date_str, None)
    else:
        blocked.get(date_str, {}).pop(factor_or_none, None)
        if not blocked.get(date_str):
            blocked.pop(date_str, None)
    self._save_blocked(blocked)

def _write_checkpoint(self, last_date: str, chunk_done: int, n_chunks: int,
                      failed_dates: list[str], source_hash: str):
    try:
        record = {
            "last_date": last_date,
            "chunk_done": chunk_done,
            "n_chunks": n_chunks,
            "failed_dates": list(failed_dates or []),
            "source_hash": source_hash,
            "ts": pd.Timestamp.now().isoformat(),
        }
        os.makedirs(os.path.dirname(self._checkpoint_path), exist_ok=True)
        with open(self._checkpoint_path, 'w') as f:
            json.dump(record, f)
    except Exception as e:
        _log.debug("checkpoint write failed (non-fatal): %s", e)

def _read_checkpoint(self) -> dict | None:
    if not os.path.exists(self._checkpoint_path):
        return None
    try:
        with open(self._checkpoint_path, 'r') as f:
            data = json.load(f)
        ts = pd.Timestamp(data.get("ts", "1970-01-01"))
        ttl = _require_cfg("factor.compute.cache_checkpoint_ttl_sec")
        if (pd.Timestamp.now() - ts).total_seconds() > ttl:
            os.remove(self._checkpoint_path)
            return None
        return data
    except Exception as e:
        _log.warning("factor_cache: checkpoint 读取失败, 续传失效从头跑: %s", e)
        return None

def _clear_checkpoint(self):
    try:
        if os.path.exists(self._checkpoint_path):
            os.remove(self._checkpoint_path)
    except Exception:
        pass

# ── fundamentals panel ──

def _build_fundamentals_panel(self, store, symbols: list[str],
                              chunk_dates: list[str],
                              data_full: pd.DataFrame = None) -> dict[str, pd.DataFrame]:
    """构建基本面 PIT panel，返回 {date_str: DataFrame(symbol×field)}。"""
    if not chunk_dates:
        return {}

    val_start = chunk_dates[0]
    val_end = chunk_dates[-1]
    mconn = store._connect()
    ph_stocks = ",".join("?" * len(symbols))

    val_df = pd.read_sql_query(
        f"SELECT symbol, date, pe_ttm, pb, ps_ttm, pcf_ttm, market_cap, source "
        f"FROM daily_valuation "
        f"WHERE date >= ? AND date <= ? ORDER BY date",
        mconn, params=(val_start, val_end)
    )
    stocks_df = pd.read_sql_query(
        f"SELECT symbol, pe, pe_ttm, pb, total_mv, roe, industry, high_52w, eps, bvps "
        f"FROM stocks WHERE symbol IN ({ph_stocks})",
        mconn, params=symbols
    ).set_index("symbol")
    daily_df = pd.read_sql_query(
        f"SELECT symbol, date, close FROM daily "
        f"WHERE symbol IN ({ph_stocks}) AND date >= ? AND date <= ? ORDER BY date",
        mconn, params=symbols + [val_start, val_end]
    )

    if not val_df.empty:
        val_df["date"] = pd.to_datetime(val_df["date"])
        # v554 (P0-1): 三源三单位换算 (与 live 路径 data/store.py P0-2 同口径)
        # — eastmoney 写元, jqdata/tushare 写万元; 无 source 列时按 jqdata 假设 ×1e4
        _mc = val_df["market_cap"]
        _src = val_df["source"] if "source" in val_df.columns else None
        if _src is not None:
            _conv = pd.Series(1.0, index=_mc.index)
            _conv[_src == "jqdata"] = 1e4
            _conv[_src == "tushare"] = 1e4
            val_df["total_mv"] = _mc * _conv
        else:
            val_df["total_mv"] = _mc * 1e4
        val_df["pe"] = val_df["pe_ttm"]  # compute_ep_ratio 优先 pe_ttm
        val_piv = val_df.pivot(index="date", columns="symbol",  # v629: 加入 ps_ttm, pcf_ttm
                               values=["pe_ttm", "pb", "ps_ttm", "pcf_ttm",
                                       "market_cap", "total_mv", "pe"]).ffill()
    else:
        val_piv = None

    if data_full is not None and "close" in data_full.columns.levels[0]:
        close_piv = data_full["close"]
        high_52w = close_piv.rolling(244, min_periods=60).max()
    elif not daily_df.empty:
        daily_df["date"] = pd.to_datetime(daily_df["date"])
        close_piv = daily_df.pivot(index="date", columns="symbol",
                                   values="close").ffill()
        high_52w = close_piv.rolling(244, min_periods=60).max()
    else:
        close_piv = None
        high_52w = None

    # v554 (P0-1): 基本面快照列全 PIT 化 — 原 _static_cols 把 stocks 当前快照的
    # total_mv/roe/pe/eps/bvps 铺到全部历史日期 (前视, 污染回测+IC评估+LGB训练,
    # 2026-07-26 P0-4 只修了 live 路径, 物化端漏修)。与 live 同口径:
    # 快照列置 NaN (诚实缺数据), 逐日由 daily_valuation PIT 覆盖;
    # 无 PIT 源 (roe/eps/bvps) → NaN, 因子按缺失处理。industry 由 v502 PIT 覆盖。
    _static_cols = {c: stocks_df[c] for c in stocks_df.columns
                    if c not in ("pe_ttm", "pb", "market_cap", "close_latest",
                                 "high_52w", "total_mv", "roe", "pe", "eps", "bvps")}
    _static_index = stocks_df.index
    # 全部被排除列显式补 NaN 占位 (覆盖外日期 df 仍含这些列,
    # 下游 null_roe 派生/pe 过滤依赖列存在; 原 _fallback 提供快照=前视)
    for _c in ("pe_ttm", "pb", "ps_ttm", "pcf_ttm", "market_cap", "close_latest", "high_52w",  # v629: +ps_ttm,pcf_ttm
               "total_mv", "roe", "pe", "eps", "bvps"):
        _static_cols[_c] = pd.Series(np.nan, index=_static_index)

    # v502 (PIT industry): industry_history → per-date 最大段 Series.
    # 静态 stocks_df["industry"] (tushare 申万当前快照) 为后视, 逐日替换.
    _ih_static = None
    try:
        _ih_rows = pd.read_sql_query(
            "SELECT symbol, effective_from, industry FROM industry_history "
            "WHERE effective_from <= ? ORDER BY effective_from",
            mconn, params=(val_end,))
        if not _ih_rows.empty:
            _ih_static = _ih_rows.set_index(["symbol", "effective_from"])["industry"]
    except Exception:
        _ih_static = None

    result = {}
    for date_str in chunk_dates:
        ts = pd.Timestamp(date_str)
        # v554: 删除 _fallback (stocks 快照 pe_ttm/pb/market_cap) —
        # 覆盖外日期 → NaN (与 live "覆盖外日期 → NaN" 一致), 原快照回退=前视
        _dyn = {}

        if val_piv is not None and ts in val_piv.index:
            row = val_piv.loc[ts]
            for col in ["pe_ttm", "pb", "ps_ttm", "pcf_ttm", "market_cap", "total_mv", "pe"]:  # v629: +ps_ttm,pcf_ttm
                if col in row.index.get_level_values(0):
                    _dyn[col] = row[col].reindex(_static_index)

        if close_piv is not None and ts in close_piv.index:
            _dyn["close_latest"] = close_piv.loc[ts].reindex(_static_index)
        if high_52w is not None and ts in high_52w.index:
            _dyn["high_52w"] = high_52w.loc[ts].reindex(_static_index)

        df = pd.DataFrame({**_static_cols, **_dyn}, index=_static_index)

        # v502: industry PIT — 取该日期 effective_from<=ts 的最大段, 覆盖静态列
        if _ih_static is not None:
            try:
                _sel = _ih_static[_ih_static.index.get_level_values("effective_from") <= ts.strftime("%Y-%m-%d")]
                _last = _sel.groupby(level=0).last()
                df["industry"] = df.index.map(_last.get)
            except Exception:
                pass

        null_roe = df["roe"].isna() | (df["roe"] <= 0)
        if null_roe.any():
            pe_col = "pe_ttm" if "pe_ttm" in df.columns else "pe"
            if pe_col in df.columns:
                derived = df["pb"] / df[pe_col].replace(0, None)
                derived = derived.where((derived > 0) & (derived < 100))
                df.loc[null_roe, "roe"] = derived.loc[null_roe]

        df.loc[df["pe"] <= 0, "pe"] = None
        df.loc[df["pe"] > 1000, "pe"] = None
        df.loc[df["pb"] <= 0, "pb"] = None

        result[date_str] = df

    return result
