#!/usr/bin/env python3
"""一次性: 53 个 rejected 因子按拒绝原因重新分类。

分类规则:
  1. northbound_* (数据源死亡) → 保留 rejected
  2. Phase 2: IC/ICIR/t/half-life thresholds not met (部分达标) → monitoring, retry_count=0
  3. Phase 2: IC/ICIR below all thresholds → retired, retry_count=1
  4. Phase 3: CPCV failed → retired, retry_count=1
  5. Phase 4: net-of-costs Sharpe too low → monitoring, retry_count=0

来源: 2026-07-18 因子状态系统改造 — 评估三档输出 + retry_count 机制
"""
from quant.utils.excepthook import setup; setup()
import sqlite3

DB = "quant/data/market.db"
conn = sqlite3.connect(DB)

# 读取所有 rejected 因子及其 status_reason
rows = conn.execute(
    "SELECT name, status_reason FROM factor_registry WHERE status='rejected'"
).fetchall()

print(f"Found {len(rows)} rejected factors")

rejected_keep = []   # northbound — 保持不变
to_monitoring = []   # 部分达标 → monitoring
to_retired = []      # 完全未达标 → retired

for name, reason in rows:
    reason_lower = (reason or "").lower()
    
    if "northbound" in reason_lower or "northbound" in name.lower():
        rejected_keep.append(name)
        print(f"  [DATA_DEAD] {name}: northbound → remains rejected")
        # 更新 reason 加 [EVAL] [DATA_DEAD] 前缀
        conn.execute(
            "UPDATE factor_registry SET status_reason=?, retry_count=NULL, "
            "updated_at=datetime('now','localtime') WHERE name=?",
            (f"[EVAL] [DATA_DEAD] {reason or 'northbound data source permanently unavailable'}", name)
        )
    
    elif "thresholds not met" in reason_lower or "thresholds not met" in reason:
        # Phase 2: 部分门槛未通过（但可能有微弱信号）→ monitoring
        to_monitoring.append(name)
        print(f"  [MONITORING] {name}: thresholds not met → monitoring (retry=0)")
        conn.execute(
            "UPDATE factor_registry SET status='monitoring', status_reason=?, "
            "retry_count=0, updated_at=datetime('now','localtime') WHERE name=?",
            (f"[EVAL] reclassified: {reason} → monitoring (signal marginal, awaiting re-evaluation)", name)
        )
    
    elif "Phase 4" in reason_lower or "phase4" in reason_lower or "net-of-costs" in reason_lower or "costs" in reason_lower:
        # Phase 4: 扣费后 Sharpe 太低 → monitoring (微亏, 观察)
        to_monitoring.append(name)
        print(f"  [MONITORING] {name}: Phase 4 → monitoring (retry=0)")
        conn.execute(
            "UPDATE factor_registry SET status='monitoring', status_reason=?, "
            "retry_count=0, updated_at=datetime('now','localtime') WHERE name=?",
            (f"[EVAL] reclassified: {reason} → monitoring (marginal, awaiting re-evaluation)", name)
        )
    
    elif "Phase 3" in reason_lower or "phase3" in reason_lower or "cpcv" in reason_lower or "pbo" in reason_lower or "oos" in reason_lower:
        # Phase 3: CPCV 失败 → retired (过拟合, retry=1)
        to_retired.append(name)
        print(f"  [RETIRED] {name}: Phase 3 → retired (retry=1)")
        conn.execute(
            "UPDATE factor_registry SET status='retired', status_reason=?, "
            "retry_count=1, last_retry='2026-07-18', updated_at=datetime('now','localtime') WHERE name=?",
            (f"[EVAL] reclassified: {reason} → retired (retry=1/3)", name)
        )
    
    elif "Phase 2" in reason_lower or "phase2" in reason_lower or "ic/icir below" in reason_lower or "below all thresholds" in reason_lower or "below all" in reason_lower:
        # Phase 2: IC/ICIR below all thresholds → retired (retry=1)
        to_retired.append(name)
        print(f"  [RETIRED] {name}: Phase 2 below all → retired (retry=1)")
        conn.execute(
            "UPDATE factor_registry SET status='retired', status_reason=?, "
            "retry_count=1, last_retry='2026-07-18', updated_at=datetime('now','localtime') WHERE name=?",
            (f"[EVAL] reclassified: {reason} → retired (retry=1/3)", name)
        )
    
    else:
        # 未知原因 → monitoring (保守处理)
        to_monitoring.append(name)
        print(f"  [MONITORING] {name}: unknown reason '{reason}' → monitoring (conservative)")
        conn.execute(
            "UPDATE factor_registry SET status='monitoring', status_reason=?, "
            "retry_count=0, updated_at=datetime('now','localtime') WHERE name=?",
            (f"[EVAL] reclassified: {reason} → monitoring (unknown reason, conservative)", name)
        )

conn.commit()

# 验证
new_dist = conn.execute(
    "SELECT status, COUNT(*) FROM factor_registry GROUP BY status"
).fetchall()
print(f"\n=== After reclassification ===")
for s, n in sorted(new_dist):
    print(f"  {s:15s}: {n}")
print(f"  [DATA_DEAD]: {len(rejected_keep)} (kept rejected)")
print(f"  → monitoring: {len(to_monitoring)}")
print(f"  → retired:    {len(to_retired)}")

conn.close()
print("\nDone. Run PYTHONPATH=. bash scripts/eval_standard.sh to let new evaluation pipeline handle them.")
