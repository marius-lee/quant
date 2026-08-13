"""daily_valuation 缺日补齐 — tushare free 档 1/h 限流不可行时用 baostock.

baostock query_history_k_data_plus 逐只拉单日 peTTM/pbMRQ/psTTM/pcfNcfTTM/turn
(market_cap 无股本源 → NULL, source='baostock').
仅补 daily 存在而 daily_valuation 缺失的日期 (2026-07-01/02 等).
"""
import sqlite3
import sys
import time

from quant.config.paths import MARKET_DB

DATES = sys.argv[1:] or None  # 未指定 → 自动差分定位


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(MARKET_DB)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute(f"PRAGMA busy_timeout=60000")
    return c


def main() -> None:
    import baostock as bs
    c = _conn()
    if DATES:
        miss = DATES
    else:
        miss = [r[0] for r in c.execute("""SELECT DISTINCT date FROM daily
            WHERE date >= '2019-01-01'
            AND date NOT IN (SELECT DISTINCT date FROM daily_valuation) ORDER BY date""").fetchall()]
    print(f"baostock valuation: {len(miss)} missing dates: {miss}", flush=True)
    if not miss:
        return

    lg = bs.login()
    if lg.error_code != "0":
        raise SystemExit(f"baostock login failed: {lg.error_msg}")

    # 总股本 (market_cap = close × totalShare, 单位元 — 与 tushare total_mv×1e4 一致).
    # 取最新已披露报告 (2026Q1 → fallback 2025Q4); 股本季度间变动低频可忽略.
    _shares: dict[str, float] = {}

    def _total_shares(sym: str) -> float | None:
        if sym in _shares:
            return _shares[sym]
        code = ("sh." if sym[0] in "6" else "sz.") + sym
        for y, q in [(2026, 1), (2025, 4)]:
            rs = bs.query_profit_data(code=code, year=y, quarter=q)
            if rs.error_code != "0":
                continue
            while rs.next():
                row = rs.get_row_data()
                if row[9] not in ("", "None"):  # totalShare 列
                    _shares[sym] = float(row[9])
                    return _shares[sym]
        return None

    total = 0
    _t0 = time.time()
    for ds in miss:
        syms = [r[0] for r in c.execute("SELECT DISTINCT symbol FROM daily WHERE date=?", (ds,))]
        updated = 0
        for i, sym in enumerate(syms):
            code = ("sh." if sym[0] in "6" else "sz.") + sym
            rs = bs.query_history_k_data_plus(
                code, "date,close,peTTM,pbMRQ,psTTM,pcfNcfTTM,turn",
                start_date=ds, end_date=ds, frequency="d")
            row = None
            while rs.next():
                row = rs.get_row_data()
            if row is None or row[1] in ("", "None"):
                continue
            try:
                close = float(row[1]) if row[1] not in ("", "None") else None
                pe = float(row[2]) if row[2] not in ("", "None") else None
                pb = float(row[3]) if row[3] not in ("", "None") else None
                ps = float(row[4]) if row[4] not in ("", "None") else None
                pcf = float(row[5]) if row[5] not in ("", "None") else None
                turn = float(row[6]) if row[6] not in ("", "None") else None
            except ValueError:
                continue
            ts = _total_shares(sym)
            mcap = close * ts if (close and ts) else None
            c.execute("""INSERT OR REPLACE INTO daily_valuation
                (symbol, date, pe_ttm, pb, ps_ttm, pcf_ttm, market_cap, turnover_rate, source)
                VALUES (?,?,?,?,?,?,?,?,'baostock')""",
                      (sym, ds, pe, pb, ps, pcf, mcap, turn))
            updated += 1
            if updated % 200 == 0:
                c.commit()
                _el = time.time() - _t0
                _rate = updated / max(_el, 0.001)
                print(f"  {ds}: {updated}/{len(syms)} ({_rate:.1f}/s ETA={(len(syms)-updated)/_rate/60:.0f}min)", flush=True)
        c.commit()
        total += updated
        print(f"  {ds}: {updated} rows (source=baostock)", flush=True)

    bs.logout()
    print(f"baostock valuation: done — {total} rows total", flush=True)


if __name__ == "__main__":
    main()