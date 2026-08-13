"""数据完整性回填脚本 (2026-08-13) — akshare IP 被封, 数据源重排。

数据源约定:
  adj_factor 2019  → baostock (tushare 免费档 3-4批/天不可用)
  benchmark_daily → baostock query_history_k_data_plus (sh.000300)
  financials      → tushare income/cashflow/balancesheet 按季度 (无积分则明确报错)
  limit_up_pool   → tushare limit_list_d 逐日 (无积分则报错, 4 因子受影响)
  dividend        → tushare dividend 逐只 (无积分则报错)
  daily_valuation → jq_valuation 脚本补最近缺失日期 (JQData 独立 venv)

用法:
  PYTHONPATH=. .venv/bin/python scripts/data_backfill_integrity.py <subcommand>

subcommand:
  macro_cleanup   本地 SQL: 归一化 macro_indicator 日期格式并按 PK 去重
  benchmark       baostock 拉 000300 2019-01-01 起 → INSERT OR IGNORE (PK 去重)
  adj_2019        baostock 逐只拉 adj_factor (start=2019-01-01, ~27min)
  financials      tushare 财报 2019Q1-2023Q4 (income/cashflow) + balance 2023 缺口
  limit_up        tushare limit_list_d 2019-04-08..2026-06-11 (若报错=无积分)
  dividend        tushare dividend 逐只 2019+ (若报错=无积分)
  verify          复跑审计: 逐表 2019+ 覆盖统计
"""
import argparse
import sqlite3
import sys
import time as _time

from quant.config.paths import MARKET_DB
from quant.utils.date import to_compact


def _conn():
    c = sqlite3.connect(MARKET_DB, timeout=60)
    c.execute("PRAGMA busy_timeout = 60000")
    return c


def _tushare_pro():
    import tushare as ts
    from quant.data.store import DataStore
    token = DataStore().token
    if not token:
        raise SystemExit("tushare token 未配置, financials/limit_up/dividend 均无法回填")
    ts.set_token(token)
    return ts.pro_api(timeout=60)


ALL_SYMS = "SELECT symbol FROM stocks WHERE symbol NOT LIKE 'BJ%'"


# ── macro_cleanup ──────────────────────────────────────────────────────────
def macro_cleanup() -> None:
    import re
    c = _conn()
    n_before = c.execute("SELECT COUNT(*) FROM macro_indicator").fetchone()[0]
    pat = re.compile(r"^(\d{4})年(\d{1,2})月(份)?$")
    rows = c.execute("SELECT indicator, date, value FROM macro_indicator").fetchall()
    norm = {}
    dup = 0
    for ind, d, v in rows:
        m = pat.match(str(d))
        if m:
            d = f"{m.group(1)}-{int(m.group(2)):02d}"
        elif re.match(r"^\d{6}$", str(d)):
            d = f"{d[:4]}-{d[4:6]}"
        if (ind, d) in norm:
            dup += 1
        norm[(ind, d)] = v  # 同键后写覆盖 (等价 REPLACE)
    c.execute("DELETE FROM macro_indicator")
    c.executemany("INSERT OR REPLACE INTO macro_indicator VALUES (?,?,?)",
                  [(i, d, v) for (i, d), v in norm.items()])
    c.commit()
    n_after = len(norm)
    print(f"macro_cleanup: {n_before} rows → {n_after} rows (collapsed {dup} dup)")
    dups = c.execute("""SELECT indicator, date, COUNT(*) FROM macro_indicator
                        GROUP BY indicator, date HAVING COUNT(*) > 1""").fetchall()
    assert not dups, f"macro 仍有重复: {dups[:5]}"
    print("macro_cleanup: 唯一性验证 OK")


# ── benchmark (baostock) ───────────────────────────────────────────────────
def benchmark() -> None:
    import baostock as bs
    lg = bs.login()
    assert lg.error_code == "0", f"baostock login failed: {lg.error_msg}"
    fields = "date,open,high,low,close,volume,amount"
    rs = bs.query_history_k_data_plus("sh.000300", fields,
                                      start_date="2019-01-01",
                                      end_date="2099-12-31",
                                      frequency="d", adjustflag="3")
    assert rs.error_code == "0", f"query_history_k_data_plus failed: {rs.error_msg}"
    c = _conn()
    n = 0
    while rs.next():
        r = rs.get_row_data()
        d = r[0]
        c.execute("""INSERT OR IGNORE INTO benchmark_daily
                     (index_code, date, open, high, low, close, volume, amount)
                     VALUES ('000300',?,?,?,?,?,?,?)""",
                  (d, *(float(x or 0) for x in r[1:8])))
        n += 1
    c.commit()
    rng = c.execute("SELECT MIN(date), MAX(date), COUNT(DISTINCT date) FROM benchmark_daily "
                    "WHERE index_code='000300'").fetchone()
    print(f"benchmark: fetched {n}, now range={rng}")
    bs.logout()


# ── adj_factor 2019 (baostock) ─────────────────────────────────────────────
def adj_2019() -> None:
    from quant.data.store import DataStore
    ds = DataStore()
    c = _conn()
    ds._ensure_adj_factor_tables(c)
    syms = [r[0] for r in c.execute(ALL_SYMS).fetchall()]
    t0 = _time.time()
    rows, done = ds._sync_adj_factor_baostock(c, syms, "2019-01-01")
    el = (_time.time() - t0) / 60
    n_2019 = c.execute("SELECT COUNT(*) FROM adj_factor WHERE date < '2020-01-01'").fetchone()[0]
    print(f"adj_2019: {rows} new rows ({done} symbols) in {el:.1f}min; 2019 行数={n_2019}")


# ── financials (tushare) ───────────────────────────────────────────────────
_FIN_MAP = {
    "financial_income": {
        "api": "income", "periods": ["20190331", "20190630", "20190930", "20191231",
                                     "20200331", "20200630", "20200930", "20201231",
                                     "20210331", "20210630", "20210930", "20211231",
                                     "20220331", "20220630", "20220930", "20221231",
                                     "20230331", "20230630", "20230930", "20231231"],
        "fields": {"total_revenue": "total_operating_revenue", "revenue": "operating_revenue",
                   "operate_cost": "operating_cost", "operate_profit": "operating_profit",
                   "n_income": "net_profit", "total_profit": "total_profit",
                   "income_tax_expense": "income_tax_expense", "admin_expense": "administration_expense"},
        "key": "ts_code", "date_key": "end_date"},
    "financial_cash_flow": {
        "api": "cashflow", "periods": ["20190331", "20190630", "20190930", "20191231",
                                       "20200331", "20200630", "20200930", "20201231",
                                       "20210331", "20210630", "20210930", "20211231",
                                       "20220331", "20220630", "20220930", "20221231",
                                       "20230331", "20230630", "20230930", "20231231"],
        "fields": {"n_cashflow_act": "net_operate_cash_flow", "n_cash_flow_inv_act": "net_invest_cash_flow",
                   "n_cash_flow_fnc_act": "net_finance_cash_flow", "c_cash_equ_end_period": "cash_and_equivalents_at_end",
                   "c_pay_goods_serv": "goods_sale_and_service_render_cash",
                   "c_paid_acqui_fa_etc": "fix_intan_other_asset_acqui_cash"},
        "key": "ts_code", "date_key": "end_date"},
    "financial_balance": {
        "api": "balancesheet", "periods": ["20230331", "20230630", "20230930", "20231231"],
        "fields": {"total_assets": "total_assets", "total_liab": "total_liability",
                   "total_hldr_equity_inc_min_int": "total_owner_equities",
                   "total_hldr_equity_exc_min_int": "equities_parent_company_owners",
                   "min_int": "minority_interests", "fix_assets": "fixed_assets",
                   "intan_assets": "intangible_assets", "goodwill": "good_will",
                   "inventories": "inventories", "accounts_receiv": "account_receivable",
                   "total_cur_assets": "total_current_assets", "total_cur_liab": "total_current_liability",
                   "st_borr": "shortterm_loan", "lt_borr": "longterm_loan"},
        "key": "ts_code", "date_key": "end_date"},
}


def financials() -> None:
    from quant.data.jq_financials import upsert_balance, upsert_income, upsert_cash_flow
    pro = _tushare_pro()
    c = _conn()
    for tbl, spec in _FIN_MAP.items():
        api = getattr(pro, spec["api"])
        total = 0
        for period in spec["periods"]:
            df = api(period=period, fields=",".join(spec["fields"]) + "," + spec["key"] + "," + spec["date_key"])
            if df is None or df.empty:
                print(f"  {tbl} {period}: EMPTY (接口不可用/无权限?)")
                sys.exit(1)
            rows = []
            for _, r in df.iterrows():
                rows.append({"symbol": r[spec["key"]].split(".")[0],
                             "stat_date": f"{r[spec['date_key']][:4]}-{r[spec['date_key']][4:6]}-{r[spec['date_key']][6:]}",
                             **{dst_: (None if r[cur] != r[cur] else float(r[cur]))
                                for cur, dst_ in spec["fields"].items()}})
            if tbl == "financial_balance":
                upsert_balance(c, rows)
            elif tbl == "financial_income":
                upsert_income(c, rows)
            else:
                upsert_cash_flow(c, rows)
            total += len(rows)
            print(f"  {tbl} {period}: {len(rows)} rows")
        print(f"financials: {tbl} 回填 {total} rows")


# ── limit_up (tushare) ─────────────────────────────────────────────────────
def limit_up() -> None:
    pro = _tushare_pro()
    c = _conn()
    dates = [r[0] for r in c.execute(
        "SELECT DISTINCT date FROM daily WHERE date >= '2019-01-01' AND date <= '2026-06-11' "
        "AND date NOT IN (SELECT DISTINCT date FROM limit_up_pool) ORDER BY date").fetchall()]
    print(f"limit_up: {len(dates)} 个缺失交易日")
    t0 = _time.time()
    ok, fail = 0, 0
    for i, d in enumerate(dates):
        try:
            df = pro.limit_list_d(trade_date=d.replace("-", ""))
        except Exception as e:
            msg = str(e)
            if "权限" in msg or "积分" in msg or "freq" in msg.lower():
                print(f"\nlimit_up: tushare 无权限 (第 {i} 天 {d}): {msg[:120]}")
                sys.exit(1)
            print(f"  {d} failed: {msg[:80]}"); fail += 1
            continue
        if df is None or df.empty:
            continue
        for _, r in df.iterrows():
            c.execute("""INSERT OR REPLACE INTO limit_up_pool
                (date, symbol, name, change_pct, close, amount, circ_mv, total_mv,
                 turnover_rate, lock_capital, first_time, last_time, open_times,
                 zt_stat, limit_up_times, industry)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (d, str(r["ts_code"]).split(".")[0], None,
                 r.get("pct_chg"), r.get("close"), r.get("amount"), None, None,
                 r.get("turnover_ratio"), r.get("fund"), r.get("first_time"),
                 r.get("last_time"), r.get("open_times"), r.get("up_stat"),
                 r.get("limit_times"), r.get("industry")))
        ok += 1
        c.commit()
        if (i + 1) % 100 == 0:
            print(f"  {i+1}/{len(dates)} ({(i+1)/len(dates)*100:.0f}%, {( _time.time()-t0)/60:.1f}min)")
    print(f"limit_up: done {ok} days, failed {fail}, {( _time.time()-t0)/60:.1f}min")


# ── dividend (tushare) ─────────────────────────────────────────────────────
def dividend() -> None:
    pro = _tushare_pro()
    c = _conn()
    syms = [r[0] for r in c.execute(ALL_SYMS).fetchall()]
    t0 = _time.time()
    total = 0
    for i, sym in enumerate(syms):
        try:
            df = pro.dividend(ts_code=sym)
        except Exception as e:
            msg = str(e)
            if "权限" in msg or "积分" in msg or "freq" in msg.lower():
                print(f"\ndividend: tushare 无权限 (第 {i} 只 {sym}): {msg[:120]}")
                sys.exit(1)
            print(f"  {sym} failed: {msg[:80]}"); continue
        if df is None or df.empty:
            continue
        for _, r in df.iterrows():
            ex = r.get("ex_date")
            if not ex or str(ex) == "nan" or str(ex) < "20190101":
                continue
            c.execute("""INSERT OR REPLACE INTO dividend
                (symbol, end_date, div_year, cash_div, stk_div, record_date, ex_date)
                VALUES (?,?,?,?,?,?,?)""",
                (sym, f"{ex[:4]}-{ex[4:6]}-{ex[6:]}", int(ex[:4]),
                 float(r.get("cash_div_tax") or 0) / 10.0,
                 (float((r.get("stk_div") or 0) if r.get("stk_div") else 0) +
                  float((r.get("stk_bo_rate") or 0) if r.get("stk_bo_rate") else 0)) / 10.0,
                 r.get("record_date"), f"{ex[:4]}-{ex[4:6]}-{ex[6:]}"))
            total += 1
        if (i + 1) % 500 == 0:
            c.commit()
            print(f"  {i+1}/{len(syms)} ({( _time.time()-t0)/60:.1f}min)")
    c.commit()
    print(f"dividend: {total} rows")


def valuation_recent() -> None:
    """daily_valuation 缺失日期按 daily 差分定位, tushare daily_basic 逐日补.

    v476 fix: 实测 daily_basic 免费档限频 1次/小时 (not 1/min) — 原 75s 重试
    8 次全撞限流 → 2026-07-01/02 误报 EMPTY. 按限流消息自适应等待
    ("小时"→3600s, else→75s), 每次成功后再等 65s 防分钟级偶发.
    """
    import time as _t
    pro = _tushare_pro()
    c = _conn()
    miss = [r[0] for r in c.execute("""SELECT DISTINCT date FROM daily
        WHERE date >= '2019-01-01' AND date NOT IN (SELECT DISTINCT date FROM daily_valuation) ORDER BY date""").fetchall()]
    print(f"valuation_recent: {len(miss)} missing dates: {miss}")
    if not miss:
        return
    c = _conn()
    for ds in miss:
        df = None
        for attempt in range(12):
            try:
                df = pro.daily_basic(
                    trade_date=ds.replace("-", ""),
                    fields="ts_code,close,pe_ttm,pb,ps_ttm,pcf_ttm,total_mv,turnover_rate")
                break
            except Exception as e:
                msg = str(e)
                if "频率超限" in msg:
                    wait = 3600 if "小时" in msg else 75
                    print(f"  {ds}: rate-limited({'1/h' if wait == 3600 else '1/min'}), sleep {wait}s (try {attempt + 1})", flush=True)
                    _t.sleep(wait)
                else:
                    print(f"  {ds}: {msg[:100]}", flush=True)
                    _t.sleep(10)
        if df is None or df.empty:
            print(f"  {ds}: EMPTY after retries — 保留待补", flush=True)
            continue
        rows = [(r["ts_code"].split(".")[0], ds,
                 None if r.get("pe_ttm") != r.get("pe_ttm") else float(r["pe_ttm"]),
                 None if r.get("pb") != r.get("pb") else float(r["pb"]),
                 None if r.get("ps_ttm") != r.get("ps_ttm") else float(r["ps_ttm"]),
                 None if r.get("pcf_ttm") != r.get("pcf_ttm") else float(r["pcf_ttm"]),
                 float(r["total_mv"]) * 1e4,
                 None if r.get("turnover_rate") != r.get("turnover_rate") else float(r["turnover_rate"]),
                 "tushare") for _, r in df.iterrows()]
        c.executemany("""INSERT OR REPLACE INTO daily_valuation
            (symbol, date, pe_ttm, pb, ps_ttm, pcf_ttm, market_cap, turnover_rate, source)
            VALUES (?,?,?,?,?,?,?,?,?)""", rows)
        c.commit()
        print(f"  {ds}: {len(rows)} rows", flush=True)
        if len(miss) > 1:
            _t.sleep(65)
    print("valuation_recent: done —",
          c.execute("SELECT COUNT(DISTINCT date) FROM daily_valuation WHERE date>='2026-01-01'").fetchone())


def verify() -> None:
    c = _conn()
    for t, dcol in [("daily", "date"), ("daily_valuation", "date"), ("adj_factor", "date"),
                    ("benchmark_daily", "date"), ("limit_up_pool", "date"),
                    ("lhb_detail", "trade_date"), ("margin_detail", "date"),
                    ("financial_balance", "stat_date"), ("financial_income", "stat_date"),
                    ("financial_cash_flow", "stat_date"), ("dividend", "ex_date")]:
        rng = c.execute(f"SELECT MIN({dcol}), MAX({dcol}), COUNT(*) FROM {t}").fetchone()
        print(f"  {t}: {rng}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sub", choices=["macro_cleanup", "benchmark", "adj_2019", "valuation_recent",
                                    "financials", "limit_up", "dividend", "verify"])
    args = ap.parse_args()
    globals()[args.sub]()


if __name__ == "__main__":
    main()