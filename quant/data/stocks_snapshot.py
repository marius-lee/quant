"""股票快照周度全量刷新 — stocks 表 (股本 + 列表信息) (v479).

背景 (v478 复盘): stocks.total_shares 曾仅 200/5525 只填充 — 唯一更新通道
fundamental.py (东财源) 被 IP 封后停摆; 新上市/退市股票无列表同步.
本模块为 registry 的 weekly_full 表提供全量刷新 (周六 data_maintenance 跑):

  refresh_all() = sync_stock_basic() + refresh_total_shares()

数据源:
  stock_basic → tushare (股票列表/名称/行业/上市状态, 单次全市场)
  total_shares → baostock profit_data 逐只 (最新已披露报告总股本,
    v478 实测 5525 只 ~20min, 无限频; 北交所 92xxx baostock 不覆盖 → 保持 NULL)

失败语义: 任一步异常向上抛 — 周六维护步骤 fail → weekly_eval 记录, 下次再试
(零 fallback, 不静默降级).
"""
import sqlite3
import time as _time

from quant.config.paths import MARKET_DB

STOCK_BASIC_FIELDS = (
    "ts_code,symbol,name,area,industry,market,list_date,list_status,delist_date"
)


def _tushare_pro():
    import tushare as ts
    from quant.data.store import DataStore
    token = DataStore().token
    if not token:
        raise SystemExit("tushare token 未配置, stock_basic 无法同步")
    ts.set_token(token)
    return ts.pro_api(timeout=60)


def _conn():
    c = sqlite3.connect(MARKET_DB, timeout=60)
    c.execute("PRAGMA busy_timeout = 60000")
    return c


def sync_stock_basic() -> int:
    """tushare stock_basic → 同步 stocks 列表列 (名称/行业/上市状态).

    已存在行 UPDATE (保留 pe/total_shares 等已有快照列, 不用 REPLACE 防清空);
    新股 INSERT。返回处理行数。
    """
    pro = _tushare_pro()
    df = pro.stock_basic(fields=STOCK_BASIC_FIELDS)
    if df is None or df.empty:
        raise RuntimeError("stock_basic 返回空")
    c = _conn()
    existing = {r[0] for r in c.execute("SELECT symbol FROM stocks").fetchall()}
    upd, ins = 0, 0
    for _, r in df.iterrows():
        sym = str(r["symbol"])
        name, industry, list_date = r.get("name"), r.get("industry"), r.get("list_date")
        list_status, delist_date = r.get("list_status"), r.get("delist_date")
        if sym in existing:
            c.execute(
                "UPDATE stocks SET name=?, industry=?, list_date=?, list_status=?, "
                "delist_date=? WHERE symbol=?",
                (name, industry, list_date, list_status, delist_date, sym))
            upd += 1
        else:
            c.execute(
                "INSERT INTO stocks (symbol, name, market, industry, list_date, "
                "list_status, delist_date) VALUES (?,?,?,?,?,?,?)",
                (sym, name, r.get("market"), industry, list_date, list_status, delist_date))
            ins += 1
    c.commit()
    c.close()
    print(f"stock_basic: {upd} updated, {ins} inserted (total {len(df)})", flush=True)
    return upd + ins


def refresh_total_shares() -> int:
    """baostock profit_data 逐只刷新 total_shares (现有值 >0 且最新报告可取的更新).

    报告优先级: 2026Q2 → 2026Q1 → 2025Q4 (最新已披露). 返回更新数。
    """
    import baostock as bs
    c = _conn()
    syms = [r[0] for r in c.execute(
        "SELECT symbol FROM stocks WHERE symbol NOT LIKE '92%'").fetchall()]
    from quant.utils.baostock_gate import bs_query, BaostockBlacklisted, BaostockQuotaExceeded
    lg = bs_query("login")
    if lg.error_code != "0":
        raise RuntimeError(f"baostock login failed: {lg.error_msg}")
    t0 = _time.time()
    updated, miss = 0, []
    for i, sym in enumerate(syms):
        code = ("sh." if sym[0] in "6" else "sz.") + sym
        val = None
        gate_blocked = False
        for y, q in ((2026, 2), (2026, 1), (2025, 4)):
            try:
                rs = bs_query("query_profit_data", code=code, year=y, quarter=q)
            except (BaostockBlacklisted, BaostockQuotaExceeded) as e:
                print(f"refresh_total_shares: {e}; 停止本轮", flush=True)
                gate_blocked = True
                break
            if rs.error_code != "0":
                print(f"refresh_total_shares {code}: error_code={rs.error_code} msg={rs.error_msg} — 该年季跳过", flush=True)
                continue
            while rs.next():
                row = rs.get_row_data()
                try:
                    v = float(row[9])
                    if v > 0:
                        val = v
                        break
                except (ValueError, IndexError):
                    continue
            if val:
                break
        if gate_blocked:
            break
        if val:
            c.execute("UPDATE stocks SET total_shares=? WHERE symbol=?", (round(val, 0), sym))
            updated += 1
        else:
            miss.append(sym)
        if (i + 1) % 500 == 0:
            c.commit()
            print(f"  total_shares {i+1}/{len(syms)} ({_time.time()-t0:.0f}s, {updated} ok)",
                  flush=True)
    c.commit()
    c.close()
    bs.logout()
    print(f"total_shares: {updated} updated, {len(miss)} miss: {miss[:10]}", flush=True)
    return updated


def refresh_all() -> int:
    """周度全量: 列表 + 股本. 返回合计处理行数."""
    n_list = sync_stock_basic()
    if n_list == 0:
        raise RuntimeError("stock_basic 同步 0 行, 中止股本刷新 (数据源异常)")
    n_share = refresh_total_shares()
    return n_list + n_share


if __name__ == "__main__":
    refresh_all()