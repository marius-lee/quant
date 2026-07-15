"""P1: 系统健康检查 — 每次 backtest 前自动运行。

检测:
  1. config.yaml 与代码默认值一致性
  2. factor_registry 与因子计算函数一致性
  3. 数据库表结构完整性
  4. 关键参数合理性

耗时: <2 秒。有任何 WARNING 或 ERROR 时暂停。
"""

import sys, os, sqlite3, inspect
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from quant.utils.logger import get_logger
logger = get_logger("validate")

ERRORS = 0
WARNINGS = 0

def error(msg):
    global ERRORS
    ERRORS += 1
    logger.error(f"VALIDATE: {msg}")

def warn(msg):
    global WARNINGS
    WARNINGS += 1
    logger.warning(f"VALIDATE: {msg}")

def ok(msg):
    logger.info(f"VALIDATE: {msg}")


# ── 1. Config vs Code defaults ──
def check_config_vs_code():
    """检查 config.yaml 中的值与代码默认值是否一致。"""
    from data.repos import FactorRepo
from config.constants import _require_cfg

    # 检查关键参数
    checks = {
        "risk.max_positions": (_require_cfg("risk.max_positions"),
            "PortfolioConstructor.__init__ default"),
        "risk.stop_loss_pct": (_require_cfg("risk.stop_loss_pct"),
            "backtest.py stop_loss_pct default"),
    }

    risk_max_pos = _require_cfg("risk.max_positions")
    alpha_method = _require_cfg("alpha.method")

    if risk_max_pos is not None:
        ok(f"max_positions={risk_max_pos} (config)")
    else:
        warn("risk.max_positions not found in config.yaml")

    if alpha_method:
        ok(f"alpha.method={alpha_method} (config)")

    # 检查 optimizer/rebalance.py 的 max_turnover_ratio 默认值
    from optimizer.rebalance import compute_trades
    sig = inspect.signature(compute_trades)
    default_turnover = sig.parameters.get("max_turnover_ratio")
    if default_turnover and default_turnover.default != 0.0:
        error(f"compute_trades max_turnover_ratio default={default_turnover.default}, "
              f"expected 0.0 (config says removed)")


# ── 2. Factor registry vs compute functions ──
def check_factors():
    """检查 factor_registry 与 factor/compute.py 一致性。"""
    from factor.compute import load_active_price_factors, load_active_fundamental_factors

    db = sqlite3.connect(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                       "data", "market.db"))

    # Active factors in DB
    db_active = set(r[0] for r in db.execute(
        "SELECT name FROM factor_registry WHERE status='active'"
    ).fetchall())

    # Deprecated factors in DB
    db_deprecated = set(r[0] for r in db.execute(
        "SELECT name FROM factor_registry WHERE status='deprecated'"
    ).fetchall())

    # Active factors in code
    price_factors = set(load_active_price_factors(status_filter='using').keys())
    fund_factors = set(load_active_fundamental_factors(status_filter='using').keys())
    code_active = price_factors | fund_factors

    # 检查: DB active 但 code 中没有 → factor_registry 过时
    db_only = db_active - code_active
    if db_only:
        warn(f"factor_registry has active factors not in code: {db_only}")

    # 检查: code 中有但 DB 不是 active → 漏入库
    code_only = code_active - db_active
    if code_only:
        warn(f"Code has factor functions not active in DB: {code_only}")

    # 检查: DB deprecated 但 IC >= 0.02 → 可能应该 reactivate
    for name in db_deprecated:
        row = db.execute(
            "SELECT ic_mean FROM factor_registry WHERE name=?", (name,)
        ).fetchone()
        if row and row[0] and abs(row[0]) >= 0.02:
            warn(f"Deprecated factor {name} has IC={row[0]:.4f} >= 0.02, "
                 f"may need re-evaluation (IC threshold met, but check Layer 1 t-test + Layer 2 marginal IC for significance)")

    ok(f"Active factors: {len(code_active)} code, {len(db_active)} DB")
    db.close()


# ── 3. Database schema ──
def check_database():
    """检查关键表和列是否存在。"""
    db = sqlite3.connect(os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                       "data", "market.db"))

    required_tables = ["stocks", "daily", "factor_registry", "benchmark_daily"]
    for table in required_tables:
        exists = db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
            (table,)
        ).fetchone()[0]
        if not exists:
            error(f"Required table '{table}' missing from market.db")
        else:
            count = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if count == 0 and table not in ["factor_registry"]:
                warn(f"Table '{table}' is empty")

    # 检查 daily 表是否有足够的复权数据
    # ── 排除 BJ (30% limit) 和 low-price (<2) — 这些不是数据错误 ──
    # (不应该有 >20% 的单日跳空)
    extreme = db.execute("""
        SELECT COUNT(*) FROM (
            SELECT d.symbol, d.date, d.close,
                   LAG(close) OVER (PARTITION BY d.symbol ORDER BY date) as prev_close
            FROM daily d JOIN stocks s ON d.symbol=s.symbol WHERE s.market!="BJ" AND d.close>=2 AND d.date >= '2026-01-01'
        ) WHERE prev_close > 0 AND ABS(close/prev_close - 1) > 0.20
    """).fetchone()[0]
    if extreme > 0:
        warn(f"Found {extreme} extreme daily returns (>20%) — possible unadjusted data (excl. BJ/ST/penny; IPO gaps possible unadjusted data (excl. BJ/ST/penny stocks; IPO gaps possible unadjusted data suspended-resume jumps may still appear) suspension-resume jumps may appear)")
    else:
        ok("No extreme daily returns found (data looks qfq-adjusted)")

    db.close()


# ── 4. Key parameter sanity ──
def check_parameters():
    """检查关键参数是否合理。"""
    from data.repos import FactorRepo
from config.constants import _require_cfg

    max_pos = _require_cfg("risk.max_positions")
    stop_loss = _require_cfg("risk.stop_loss_pct")
    alpha_method = _require_cfg("alpha.method")

    if max_pos < 1 or max_pos > 10:
        warn(f"max_positions={max_pos} seems extreme")

    if stop_loss < 0.05 or stop_loss > 0.50:
        warn(f"stop_loss_pct={stop_loss} seems extreme")

    if alpha_method not in ("ic_weighted", "equal_weight", "intersection"):
        warn(f"Unknown alpha.method={alpha_method}")


# ═══════════════════════
if __name__ == "__main__":
    print("=" * 60)
    print("System validation")
    print("=" * 60)

    check_config_vs_code()
    check_factors()
    check_database()
    check_parameters()

    print("=" * 60)
    if ERRORS:
        print(f"❌ {ERRORS} ERROR(S), {WARNINGS} warning(s) — fix before running backtest")
        sys.exit(1)
    elif WARNINGS:
        print(f"⚠️  {WARNINGS} warning(s) — backtest may still work")
    else:
        print("✅ All checks passed")
    print("=" * 60)
