#!/usr/bin/env python3
"""一键复活归档因子 → evaluating 重评 (v519/v520).

用途: 用户拍板 — 将归档类别中的 C1(Phase2 IC/ICIR 不达标 64)、
      C2(实盘 DSR 衰减 18)、C3(重评失败/marginal 13) 共 95 个因子
      全部迁移回 evaluating 状态, 交由周六 weekly_eval 自动重评
      (v519 后为完整评估: p2+p3+p4+DSR 单一裁决)。

用法:
    PYTHONPATH=. .venv/bin/python scripts/reevaluate_archived_factors.py          # dry-run 预览
    PYTHONPATH=. .venv/bin/python scripts/reevaluate_archived_factors.py --execute # 实际迁移

幂等性: 已在 evaluating 的因子跳过; 重复执行不产生副作用。
排除:   DATA_SPARSE / DATA_DEAD (北向) / [OPS] 搁置 / 其他未归类 — 保持 archived。

版本: v1.0 (2026-08-16)
"""
import sqlite3
import sys
import time

from quant.config.paths import MARKET_DB
from quant.factor.state_machine import FactorStateMachine

CAT_LABELS = {
    "C1_ICICIR": "C1: 评估 Phase2 IC/ICIR 不达标",
    "C2_DSR": "C2: 实盘 IC 持续衰减 DSR<1",
    "C3_REEVAL": "C3: 重评失败/marginal (最接近通过线)",
    "EXCLUDE": "排除: DATA_SPARSE/DATA_DEAD(北向)/[OPS]搁置/其他",
}


def _categorize(reason: str) -> str:
    r = reason or ""
    if "DATA_DEAD" in r or "DATA_SPARSE" in r or "[OPS]" in r \
            or "Insufficient cross-sectional" in r:
        return "EXCLUDE"
    if "below all thresholds" in r or "IC/ICIR below" in r:
        return "C1_ICICIR"
    if "IC_PERSISTENT" in r or ("DSR" in r and "衰减" in r):
        return "C2_DSR"
    if "marginal" in r or "reeval" in r or "Phase 3" in r or "Phase 4" in r:
        return "C3_REEVAL"
    return "EXCLUDE"


def main() -> int:
    execute = "--execute" in sys.argv
    t0 = time.time()

    conn = sqlite3.connect(MARKET_DB, timeout=10)
    rows = conn.execute(
        "SELECT name, status_reason, retry_count FROM factor_registry "
        "WHERE status='archived'").fetchall()
    conn.close()

    by_cat: dict[str, list[tuple[str, str, int | None]]] = {}
    for name, reason, rc in rows:
        by_cat.setdefault(_categorize(reason), []).append((name, reason, rc))

    for cat in ("C1_ICICIR", "C2_DSR", "C3_REEVAL", "EXCLUDE"):
        items = by_cat.get(cat, [])
        print(f"{CAT_LABELS[cat]}: {len(items)}")
        for name, reason, rc in items:
            tag = f"retry={rc}" if rc else ""
            print(f"  - {name} {tag} | {(reason or '')[:70]}")

    to_restore = (by_cat.get("C1_ICICIR", []) + by_cat.get("C2_DSR", [])
                  + by_cat.get("C3_REEVAL", []))
    print(f"\n将复活: {len(to_restore)} 个 (排除: {len(by_cat.get('EXCLUDE', []))} 个)")

    if not execute:
        print("[dry-run] 未执行迁移, 加 --execute 执行")
        return 0

    fsm = FactorStateMachine()
    ok = skipped = failed = 0
    for i, (name, _, rc) in enumerate(to_restore, 1):
        try:
            current = fsm.get_status(name)
            if current != "archived":
                skipped += 1
                print(f"  [{i}/{len(to_restore)}] {name}: 非 archived ({current}), 跳过")
                continue
            fsm.transition(
                name, "RETRY_RESTORE",
                reason="[OPS] user-ordered bulk re-evaluation (v520): archived → evaluating, "
                       "awaiting weekly_eval full pipeline judgment")
            ok += 1
        except Exception as e:
            failed += 1
            print(f"  [{i}/{len(to_restore)}] {name}: FAILED — {e}")
        if i % 10 == 0:
            print(f"  ... {i}/{len(to_restore)} done (ok={ok}, skip={skipped}, fail={failed})")

    print(f"复活完成: ok={ok}, skipped={skipped}, failed={failed}, 耗时 {time.time()-t0:.1f}s")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())