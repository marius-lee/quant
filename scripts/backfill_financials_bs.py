#!/usr/bin/env python3
"""baostock 财务数据回填 — 利润表/资产负债表/现金流量表.

回填 income 2023 + cash_flow 2019-2023 缺口.
baostock 免费, 无需 token, 逐股逐季查询.
运行:
  PYTHONPATH=. .venv/bin/python3 scripts/backfill_financials_bs.py
"""
import os, sys, sqlite3, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quant.utils.logger import get_logger
from quant.data.jq_financials import ensure_tables
from quant.config.constants import _require_cfg
from quant.utils.baostock_gate import bs_query, BaostockBlacklisted, BaostockQuotaExceeded

logger = get_logger("backfill.financials_bs")
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "quant", "data", "market.db")

# 回填目标
# income: 2023 全部季度
# cash_flow: 2019-2023 全部季度 (历史完全缺失)
TARGETS = {
    "financial_income": [(2023, q) for q in range(1, 5)],
    "financial_cashflow": [(y, q) for y in range(2019, 2024) for q in range(1, 5)],
}

# baostock API name + 表名 (不直接调 bs.login/query — 统一走 BaostockGate 限速)
API_MAP = {
    "financial_income": ("query_profit_data", "income"),
    "financial_cashflow": ("query_cash_flow_data", "cash_flow"),
}

# baostock → DB 字段映射 (通用, 保留所有 baostock 返回列)
# 两边都存原始字段名, DB schema 由 jq_financials.ensure_tables 维护


def _bs_code(symbol: str) -> str:
    """000001 → sh.000001 或 sz.000001."""
    if symbol.startswith(("6", "9")):
        return f"sh.{symbol}"
    return f"sz.{symbol}"


def _bs_row_to_db(bs_row: list, symbol: str, year: int, quarter: int) -> dict:
    """将 baostock 返回行转为 DB row dict."""
    result = {"symbol": symbol}
    # stat_date: YYYY-MM-DD (季度最后一天)
    last_day = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}
    result["stat_date"] = f"{year}-{last_day[quarter]}"
    result["pub_date"] = bs_row[0] if len(bs_row) > 0 else result["stat_date"]
    # 其他列直接映射 (baostock 列名与 DB 不完全一致, 只存有映射的)
    return result


def main():
    # 登录也走统一入口 (gate.acquire 限速 + 黑名单熔断)
    try:
        bs_query("login")
    except (BaostockBlacklisted, BaostockQuotaExceeded) as e:
        logger.error(f"baostock login blocked: {e}")
        return 1
    logger.info("baostock login OK")

    conn = sqlite3.connect(DB, timeout=30)
    ensure_tables(conn)

    # 加载已有
    existing = set()
    for tbl in TARGETS:
        rows = conn.execute(f"SELECT symbol, stat_date FROM {tbl}").fetchall()
        existing.update((r[0], str(r[1])[:10]) for r in rows)
    logger.info(f"existing records: {len(existing)}")

    symbols = [r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM stocks ORDER BY symbol"
    ).fetchall()]
    logger.info(f"symbols: {len(symbols)}")

    total = 0
    t0 = time.monotonic()

    # 计算总工作量
    _total_quarters = sum(len(qs) for qs in TARGETS.values())
    _q_done = 0

    for tbl, quarters in TARGETS.items():
        api_fn_name, label = API_MAP[tbl]
        # 查询该表的实际列
        db_cols = set(r[1] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall())

        for year, quarter in quarters:
            _q_done += 1
            last_day = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}
            stat_date = f"{year}-{last_day[quarter]}"
            logger.info(f"[{_q_done}/{_total_quarters}] {tbl} {stat_date}: checking {len(symbols)} stocks...")

            # 已有检查
            year_count = sum(1 for s, d in existing if d == stat_date)
            if year_count > 1000:
                logger.info(f"{tbl} {stat_date}: {year_count} rows exist, skip")
                continue

            batch = []
            n_total = len(symbols)
            n_done = 0
            gate_blocked = False
            for symbol in symbols:
                n_done += 1
                if n_done % 500 == 0:
                    logger.info(f"{tbl} {stat_date}: {n_done}/{n_total} ({n_done*100//n_total}%)")
                if (symbol, stat_date) in existing:
                    continue
                bs_sym = _bs_code(symbol)
                try:
                    rs = bs_query(api_fn_name, code=bs_sym, year=year, quarter=quarter)
                    if rs.error_code != "0":
                        continue
                    data = rs.get_data()
                    if data.empty:
                        continue
                    # 只取最后一行 (合并报表)
                    row_data = data.iloc[-1].to_dict()
                    row_data["symbol"] = symbol
                    row_data["stat_date"] = stat_date
                    row_data["pub_date"] = stat_date
                    # baostock → DB: 保留 DB 表中存在的列
                    db_row = {"symbol": symbol, "stat_date": stat_date, "pub_date": stat_date}
                    for k, v in row_data.items():
                        if k in db_cols:
                            db_row[k] = v
                    batch.append(db_row)
                except BaostockBlacklisted as e:
                    logger.error(f"{tbl} {stat_date}: baostock IP 黑名单, 立即停止: {e}")
                    gate_blocked = True
                    break
                except BaostockQuotaExceeded as e:
                    logger.error(f"{tbl} {stat_date}: baostock 配额已尽, 停止本轮: {e}")
                    gate_blocked = True
                    break
                except Exception:
                    continue

            if gate_blocked:
                break

            if not batch:
                logger.info(f"{tbl} {stat_date}: 0 new rows (baostock returned empty)")
                continue

            # 写入
            cols = [c for c in batch[0].keys() if c in db_cols]
            placeholders = ", ".join("?" for _ in cols)
            sql = (
                f"INSERT OR REPLACE INTO {tbl} ({', '.join(cols)}) "
                f"VALUES ({placeholders})"
            )
            for row in batch:
                conn.execute(sql, [row[c] for c in cols])
                existing.add((row["symbol"], stat_date))
            conn.commit()
            total += len(batch)
            elapsed = time.monotonic() - t0
            logger.info(f"{tbl} {stat_date}: {len(batch)} rows (total={total}, {elapsed:.0f}s)")

    conn.close()
    try:
        import baostock as _bs
        _bs.logout()
    except Exception:
        pass
    elapsed = time.monotonic() - t0
    logger.info(f"financials_bs backfill done: {total} rows in {elapsed/60:.1f}min")
    return 0


if __name__ == "__main__":
    sys.exit(main())