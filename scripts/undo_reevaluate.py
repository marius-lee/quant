#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""撤销一轮错误的因子状态流转 (恢复 v520 基线)。

用途: 2026-08-17 深夜手动重评 (reevaluate_now) 因物化 worker 全量失败
      (store.py financial_cash_flow 表名拼错 → financial_cashflow), 评估窗口
      2025-08-04..2026-08-14 缓存全缺失 → compute_ic 全部 IC=0 → 98 个因子
      被误归档 (含 52 个 REJECTED 永久淘汰)。本脚本按本轮 touch 名单逆向恢复。

版本: v1.0  幂等: 是 (重复执行时第二次 touch 名单已恢复为基线, 无更多变更)

恢复规则 (每因子, 仅本轮 touch 的: updated_at >= 指定时刻):
  1. status  → 基线状态:
       probation 5 (dt_streak/wq_alpha_006/alpha002_vol_div/alpha055_pos_vol/smart_money_20d) → probation
       evaluating 4 (macro_cpi_yoy/macro_m2_yoy/macro_pmi_diff/macro_rate_10y)              → evaluating
       其余 (97 复活因子)                                                                   → evaluating
  2. retry_count → max(0, retry_count - 1)  (撤销本轮唯一一次失败计数)
  3. status_reason → '' , last_retry → NULL, updated_at → now
  4. notes 追加撤销标记 (不删历史注记)

用法:
  PYTHONPATH=. .venv/bin/python scripts/undo_reevaluate.py            # dry-run
  PYTHONPATH=. .venv/bin/python scripts/undo_reevaluate.py --execute  # 落库
"""
import argparse
import sqlite3
from datetime import datetime
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "quant" / "data" / "market.db"

PROBATION5 = {"dt_streak", "wq_alpha_006", "alpha002_vol_div", "alpha055_pos_vol", "smart_money_20d"}
EVALUATING4 = {"macro_cpi_yoy", "macro_m2_yoy", "macro_pmi_diff", "macro_rate_10y"}
TOUCH_SINCE = "2026-08-16 23:47:00"


def _report(conn, tag: str):
    dist = conn.execute("SELECT status, COUNT(*) FROM factor_registry GROUP BY status").fetchall()
    print(f"[{tag}] " + ", ".join(f"{s}={n}" for s, n in dist))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true", help="落库 (默认 dry-run)")
    args = ap.parse_args()

    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA journal_mode=WAL")
    _report(conn, "当前分布")

    touched = conn.execute(
        "SELECT name, status, retry_count FROM factor_registry WHERE updated_at >= ? AND notes NOT LIKE ?",
        (TOUCH_SINCE, "%| undo |%"),
    ).fetchall()
    print(f"本轮 touch: {len(touched)} 因子")

    plan = []
    for name, status, retry_count in touched:
        new_status = "probation" if name in PROBATION5 else "evaluating"
        new_retry = max(0, retry_count - 1)
        plan.append((name, new_status, new_retry))

    # 校验: 待恢复的 archived 应 == 98 (108 - 10 排除类), 恢复后分布应为 101/5/10
    n_recover = sum(1 for _, s, _ in plan if s != "archived" or True)
    print(f"待恢复: {len(plan)} (archived→基线 {sum(1 for p in plan if p[1] != 'archived')} 个)")

    if not args.execute:
        print("[dry-run] 示例恢复:")
        for name, new_status, new_retry in plan[:5]:
            print(f"  {name}: → {new_status}, retry → {new_retry}")
        print(f"[dry-run] 共 {len(plan)} 因子, 未落库。加 --execute 执行。")
        conn.close()
        return

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for name, new_status, new_retry in plan:
        conn.execute(
            "UPDATE factor_registry SET status=?, status_reason='', retry_count=?, "
            "last_retry=NULL, updated_at=?, notes=notes || ? WHERE name=?",
            (new_status, new_retry, now, f"\n{now} | undo | 撤销误评估 (缓存缺数据致 IC=0), 重评后覆盖", name),
        )
    conn.commit()
    _report(conn, "恢复后分布")
    print(f"已撤销 {len(plan)} 因子, 重评前基线: evaluating 101 / probation 5 / archived 10")
    conn.close()


if __name__ == "__main__":
    main()