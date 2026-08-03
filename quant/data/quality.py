"""数据质量门禁 (P2a) — 每日数据同步后的质量检查.

业界标准 (Qlib/DolphinDB): 数据 pipeline 后必须有质量校验步骤。
在 daily_data 任务后、晚间链继续前运行, 异常告警但不阻断 (soft gate).
"""

import pandas as pd
import numpy as np
from quant.utils.logger import get_logger
from quant.data.repos._base import DatabaseManager

_log = get_logger("data.quality")


def check_daily_quality(date_str: str) -> dict:
    """对当日日线数据执行质量检查, 返回 {check_name: {status, detail}}.

    status: "ok" | "warn" | "error"
    warn 级别记录日志但不阻断; error 级别需人工介入.
    """
    conn = DatabaseManager.market()
    results = {}

    # ── 1. 股票数量波动检查 ──
    try:
        row = conn.execute(
            "SELECT COUNT(DISTINCT symbol) FROM daily WHERE date = ?", (date_str,)
        ).fetchone()
        today_count = row[0] if row else 0

        prev = conn.execute(
            "SELECT date, COUNT(DISTINCT symbol) as n FROM daily "
            "WHERE date < ? GROUP BY date ORDER BY date DESC LIMIT 5",
            (date_str,)
        ).fetchall()
        if prev and today_count > 0:
            avg_prev = sum(r[1] for r in prev) / len(prev)
            pct_change = (today_count - avg_prev) / avg_prev * 100
            if abs(pct_change) > 20:
                results["symbol_count"] = {
                    "status": "warn",
                    "detail": f"今日 {today_count} 只, 近5日均值 {avg_prev:.0f} (变化 {pct_change:+.1f}%)"
                }
                _log.warning(f"[{date_str}] quality: symbol count anomaly — {results['symbol_count']['detail']}")
            else:
                results["symbol_count"] = {"status": "ok", "detail": f"{today_count} 只"}
        else:
            results["symbol_count"] = {"status": "error", "detail": f"0 只股票"}
    except Exception as e:
        results["symbol_count"] = {"status": "error", "detail": str(e)}
        _log.error(f"[{date_str}] quality: symbol_count check failed: {e}")

    # ── 2. 涨跌停比例异常 ──
    try:
        board_limits = {
            "主板10%": ("symbol NOT LIKE '300%' AND symbol NOT LIKE '688%' "
                        "AND symbol NOT LIKE '8%' AND symbol NOT LIKE '4%'", 0.10),
            "双创20%": ("symbol LIKE '300%' OR symbol LIKE '688%'", 0.20),
            "北交30%": ("symbol LIKE '8%' OR symbol LIKE '4%'", 0.30),
        }
        for label, (cond, limit_pct) in board_limits.items():
            row = conn.execute(
                f"SELECT COUNT(*) FROM daily d WHERE d.date = ? AND {cond}",
                (date_str,)
            ).fetchone()
            total = row[0] if row else 0
            if total == 0:
                continue
            hit = conn.execute(
                f"SELECT COUNT(*) FROM daily d "
                f"JOIN daily d2 ON d.symbol = d2.symbol AND d2.date = date(d.date, '-1 day') "
                f"WHERE d.date = ? AND {cond} "
                f"AND (d.close - d2.close) / d2.close >= ?",
                (date_str, limit_pct * 0.95)
            ).fetchone()
            hit_count = hit[0] if hit else 0
            hit_pct = hit_count / total * 100
            if hit_pct > 15:
                results[f"limit_up_{label}"] = {
                    "status": "warn",
                    "detail": f"涨停 {hit_count}/{total} ({hit_pct:.0f}%)"
                }
    except Exception as e:
        _log.warning(f"[{date_str}] quality: limit check failed (non-fatal): {e}")

    # ── 3. 必要字段缺失检测 ──
    try:
        required_fields = ["open", "high", "low", "close", "volume", "amount"]
        for field in required_fields:
            null_count = conn.execute(
                f"SELECT COUNT(*) FROM daily WHERE date = ? AND {field} IS NULL",
                (date_str,)
            ).fetchone()[0]
            if null_count > 0:
                results[f"null_{field}"] = {
                    "status": "warn",
                    "detail": f"{field} 缺失 {null_count} 行"
                }
                _log.warning(f"[{date_str}] quality: {field} has {null_count} nulls")
    except Exception as e:
        _log.warning(f"[{date_str}] quality: field check failed (non-fatal): {e}")

    # ── 4. 极端价格检测 (≤0) ──
    try:
        bad = conn.execute(
            "SELECT COUNT(*) FROM daily WHERE date = ? AND (close <= 0 OR open <= 0)",
            (date_str,)
        ).fetchone()[0]
        if bad > 0:
            results["zero_price"] = {
                "status": "error",
                "detail": f"{bad} 行价格 ≤ 0"
            }
            _log.error(f"[{date_str}] quality: {bad} rows with zero/negative price")
    except Exception as e:
        _log.warning(f"[{date_str}] quality: price check failed: {e}")

    conn.close()

    has_error = any(v.get("status") == "error" for v in results.values())
    has_warn = any(v.get("status") == "warn" for v in results.values())
    overall = "error" if has_error else ("warn" if has_warn else "ok")

    _log.info(f"[{date_str}] data quality: {overall} ({len(results)} checks)")
    return {"date": date_str, "overall": overall, "checks": results}
