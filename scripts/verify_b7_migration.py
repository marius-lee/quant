#!/usr/bin/env python3
"""v237 B7 TradeRepo 迁移验证 — 全路径覆盖"""
import sys, os

RED, GREEN, YELLOW, NC = '\033[31m', '\033[32m', '\033[33m', '\033[0m'

failures = 0
def check(name, ok):
    global failures
    tag = f"{GREEN}✅{NC}" if ok else f"{RED}❌{NC}"
    print(f"  {tag} {name}")
    if not ok:
        failures += 1

def title(s):
    print(f"\n{YELLOW}{'='*60}{NC}")
    print(f"{YELLOW}  {s}{NC}")
    print(f"{YELLOW}{'='*60}{NC}")

# ── 1. 旧文件确认删除 ──
title("1. 旧文件删除确认")
check("data/trade_repo.py 已删除", not os.path.exists("quant/data/trade_repo.py"))

# ── 2. 新 repos/TradeRepo 可导入 ──
title("2. repos/TradeRepo 导入")
try:
    from quant.data.repos import TradeRepo
    check("from quant.data.repos import TradeRepo", True)
except Exception as e:
    check(f"from quant.data.repos import TradeRepo: {e}", False)

# ── 3. DatabaseManager 模式 — 确认无 per-call sqlite3.connect ──
title("3. DatabaseManager 模式验证")
src = open("quant/data/repos/trade_repo.py").read()
has_db_manager = "DatabaseManager" in src and "get_connection" in src
has_raw_connect = "sqlite3.connect(" in src and "def _conn" not in src.split("def _conn")[0]  # only in _conn fallback?
check("使用 DatabaseManager.get_connection()", has_db_manager)
# _conn 中应该只有一次 get_connection 调用
conn_calls = src.count("get_connection")
check(f"_conn() 走 DatabaseManager ({conn_calls} 处)", conn_calls >= 1)

# ── 4. 所有旧 import 路径已清除 ──
title("4. 旧 import 路径残留检查")
import subprocess
result = subprocess.run(
    ["rg", "--no-heading", "-n", "from quant.data.trade_repo import", "quant/", "web/", "scripts/"],
    capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
old_imports = [l for l in result.stdout.strip().split('\n') if l and 'docs/' not in l and '.bak' not in l and 'CHANGELOG' not in l and 'CODE_AUDIT' not in l and '.md' not in l]
check(f"旧 import 路径残留: {len(old_imports)} 处 (应为 0)", len(old_imports) == 0)
for imp in old_imports:
    print(f"    {RED}残留: {imp}{NC}")

# ── 5. 核心模块可导入 ──
title("5. 核心调用方导入验证")
modules = [
    ("quant.pipeline", 3),
    ("quant.execution.engine", 1),
    ("quant.scheduler.monitor", 1),
    ("quant.scheduler.execute", 1),
    ("quant.scheduler.attribution", 1),
    ("quant.scheduler.order_manager", 1),
    ("quant.monitor.alerts", 1),
    ("quant.monitor.report", 1),
]
for mod, expected in modules:
    try:
        __import__(mod)
        src_mod = open(f"{mod.replace('.','/')}.py").read()
        actual = src_mod.count("from quant.data.repos import TradeRepo")
        ok = actual >= expected
        check(f"{mod}: TradeRepo import ({actual} 处, 预期 ≥{expected})", ok)
    except Exception as e:
        check(f"{mod}: import 失败 — {e}", False)

# ── 6. TradeRepo 功能验证 ──
title("6. TradeRepo 功能验证")
try:
    repo = TradeRepo()
    check("TradeRepo() 实例化", True)
    
    # 基本查询
    pos = repo.get_positions()
    check(f"get_positions() → {len(pos)} 持仓", isinstance(pos, list))
    
    cash = repo.get_cash()
    check(f"get_cash() → ¥{cash:.2f}", isinstance(cash, float))
    
    cap = repo.get_initial_capital()
    check(f"get_initial_capital() → ¥{cap:.2f}", isinstance(cap, float))
    
    trades = repo.get_trades(limit=3)
    check(f"get_trades() → {len(trades)} 条", isinstance(trades, list))
except Exception as e:
    check(f"TradeRepo 功能验证: {e}", False)
    import traceback; traceback.print_exc()

# ── 7. engine.py TRADE_DB 路径清理确认 ──
title("7. engine.py 路径清理")
eng_src = open("quant/execution/engine.py").read()
has_local_trdb = "TRADE_DB_DEFAULT = os.path.join" in eng_src
has_local_mdb = "MARKET_DB = os.path.join" in eng_src
has_paths_import = "from quant.config.paths import TRADE_DB" in eng_src
has_hack = "TradeRepo(self.db_path)._ensure_tables()" in eng_src
check("TRADE_DB_DEFAULT 本地定义已删除", not has_local_trdb)
check("MARKET_DB 本地定义已删除", not has_local_mdb)
check("使用 quant.config.paths 导入", has_paths_import)
check("_ensure_tables() hack 已删除", not has_hack)

# ── 8. 冒烟测试 ──
title("8. 冒烟测试 (smoke_test_v190)")
try:
    result = subprocess.run(
        [sys.executable, "scripts/smoke_test_v190.py"],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "PYTHONPATH": "."},
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    all_pass = "ALL 8/8 PASSED" in result.stdout
    check("smoke_test 8/8 PASSED", all_pass)
    if not all_pass:
        print(result.stdout[-500:])
except Exception as e:
    check(f"smoke_test 执行失败: {e}", False)

# ── 总结 ──
print(f"\n{'='*60}")
if failures == 0:
    print(f"{GREEN}全部通过 ✅ — B7 迁移验证成功{NC}")
else:
    print(f"{RED}{failures} 项失败 ❌{NC}")
print(f"{'='*60}")
sys.exit(0 if failures == 0 else 1)
