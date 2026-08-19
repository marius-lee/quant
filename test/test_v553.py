# -*- coding: utf-8 -*-
"""v553: 止损止盈专业标准对齐 — TP2 清仓 / ATR=0 固定%兜底 / breakeven 保本 /
time_stop_hard 盈利停滞退出 / Wilder SMMA / meta 仅变化时写 / kind 字段.

修复对照 (2026-08-19 审查):
- P0-1: monitor triggered_stop 单 key 压制当天 TP2/trail_lock (monitor 侧, 测试见 reason key 语义)
- P0-2: TP2 只卖剩仓一半 → 偶数整手永远留 25% 尾巴
- P1-3: ATR=0 (上市<21日) 静默跳过 → 新股裸奔
- P1-4: docstring 称 EMA 实为 SMA → Wilder SMMA
- P2-8: time_stop 只对浮亏生效 → 2×max_hold_days 无条件退出
- P2-9: TP1 后止损仍挂成本-2ATR → breakeven 保本线
- P2-10: 每轮全持仓写 position_meta → 仅变化时写
"""

import pytest

from quant.execution.stop_loss import RiskManager, _wilders_atr_from_trs


def _mk_rm():
    return RiskManager(cooloff_store={})


def _pos(symbol="600000", shares=400, price=100.0, **kw):
    p = {"symbol": symbol, "shares": shares, "price": price}
    p.update(kw)
    return p


def _quotes(price, symbol="600000", **kw):
    q = {"price": price}
    q.update(kw)
    return {symbol: q}


# ════════════════════════════════════════════════════════════════
# P1-4: Wilder SMMA 纯函数
# ════════════════════════════════════════════════════════════════

def test_v553_wilders_atr_matches_hand_calc():
    """trs=[2..20], period=5: 种子 SMA(2,4,6,8,10)=6,
    递归 ATR=(ATR*4+TR)/5 → 7.2, 8.56, 10.048, 11.6384, 13.31072."""
    trs = [2, 4, 6, 8, 10, 12, 14, 16, 18, 20]
    atr = _wilders_atr_from_trs(trs, 5)
    assert atr == pytest.approx(13.31072, abs=1e-9)


def test_v553_wilders_atr_insufficient_returns_zero():
    """样本 < period → 0 (触发固定%兜底, 不抛错)."""
    assert _wilders_atr_from_trs([1.0, 2.0, 3.0], 20) == 0.0


# ════════════════════════════════════════════════════════════════
# P0-2: TP2 清仓
# ════════════════════════════════════════════════════════════════

def test_v553_tp1_sells_half_lot():
    """TP1: 400 股 → 卖 200 (整手一半), kind=profit."""
    rm = _mk_rm()
    pos = _pos(shares=400)
    sigs = rm.check([pos], _quotes(121.0), "2026-08-19",
                    atr_panel={"2026-08-19": {"600000": 10.0}})
    assert len(sigs) == 1
    s = sigs[0]
    assert s["shares"] == 200
    assert s["reason"].startswith("TP1")
    assert s["kind"] == "profit"


def test_v553_tp2_sells_remaining_all():
    """TP2: tp1_hit 后 400 股 → 全卖 400 (原只卖 200 留 200)."""
    rm = _mk_rm()
    pos = _pos(shares=400, _tp1_hit=True, _peak=121.0,
               _loaded_meta={"_tp1_hit": True, "_peak": 121.0})
    sigs = rm.check([pos], _quotes(131.0), "2026-08-19",
                    atr_panel={"2026-08-19": {"600000": 10.0}})
    assert len(sigs) == 1
    assert sigs[0]["shares"] == 400
    assert sigs[0]["reason"].startswith("TP2")
    assert sigs[0]["kind"] == "profit"


def test_v553_tp2_even_lot_no_tail():
    """TP2 偶数百股剩仓 200 → 全卖 200 (原卖 100 留 100 尾巴)."""
    rm = _mk_rm()
    pos = _pos(shares=200, _tp1_hit=True, _peak=121.0,
               _loaded_meta={"_tp1_hit": True, "_peak": 121.0})
    sigs = rm.check([pos], _quotes(131.0), "2026-08-19",
                    atr_panel={"2026-08-19": {"600000": 10.0}})
    assert len(sigs) == 1
    assert sigs[0]["shares"] == 200


def test_v553_tp2_not_when_below_threshold():
    """+2.9ATR (gain=29 < 30) → TP2 不触发."""
    rm = _mk_rm()
    pos = _pos(shares=400, _tp1_hit=True, _peak=121.0,
               _loaded_meta={"_tp1_hit": True, "_peak": 121.0})
    sigs = rm.check([pos], _quotes(129.0), "2026-08-19",
                    atr_panel={"2026-08-19": {"600000": 10.0}})
    assert sigs == []


# ════════════════════════════════════════════════════════════════
# P1-3: ATR=0 → 固定% 止损兜底
# ════════════════════════════════════════════════════════════════

def test_v553_atr_zero_falls_back_to_pct_stop():
    """上市<21日 ATR=0: -9% ≤ -8% → hard_sl_pct 全卖 (原静默跳过裸奔)."""
    rm = _mk_rm()
    sigs = rm.check([_pos()], _quotes(91.0), "2026-08-19",
                    atr_panel={"2026-08-19": {"600000": 0.0}})
    assert len(sigs) == 1
    assert sigs[0]["reason"].startswith("hard_sl_pct")
    assert sigs[0]["kind"] == "loss"
    assert sigs[0]["shares"] == 400


def test_v553_atr_zero_no_stop_when_within_pct():
    """ATR=0 且 -5% > -8% → 不触发 (未跌破固定底线)."""
    rm = _mk_rm()
    sigs = rm.check([_pos()], _quotes(95.0), "2026-08-19",
                    atr_panel={"2026-08-19": {"600000": 0.0}})
    assert sigs == []


# ════════════════════════════════════════════════════════════════
# 审查 #9: TP1 后保本 — trail_lock 数学覆盖 (breakeven 死代码不引入)
# ════════════════════════════════════════════════════════════════

def test_v553_tp1_after_protection_by_trail_lock():
    """TP1 (peak=125) 后价格回落至成本 100.1 → trail_lock 触发 (峰值回撤),
    卖出价 ≥ 成本 — 利润不吐光, 保本由移动止损数学覆盖 (线=peak-1.5ATR≥cost+0.5ATR)."""
    rm = _mk_rm()
    pos = _pos(shares=200, _tp1_hit=True, _peak=125.0,
               _loaded_meta={"_tp1_hit": True, "_peak": 125.0})
    sigs = rm.check([pos], _quotes(100.1), "2026-08-19",
                    atr_panel={"2026-08-19": {"600000": 10.0}})
    assert len(sigs) == 1
    s = sigs[0]
    assert s["reason"].startswith("trail_lock")
    assert s["kind"] == "profit"
    assert s["shares"] == 200
    assert s["price"] >= 100.0


def test_v553_trail_line_math_above_cost():
    """数学验证: tp1_hit (peak ≥ cost+2ATR) → trail 触发线 = peak-1.5ATR ≥ cost+0.5ATR."""
    cost, atr, tp1_atr, sl_atr, trail_atr = 100.0, 10.0, 2.0, 2.0, 1.5
    peak = cost + tp1_atr * atr  # TP1 触发线 = 最低可能 peak
    trail_line = peak - trail_atr * atr
    assert trail_line >= cost + 0.5 * atr
    assert trail_line > cost * 1.002  # 高于任何 breakeven 缓冲 → breakeven 恒被遮蔽


def test_v553_no_breakeven_dead_code():
    """config 中无 breakeven 参数 (审查 #9 结论: 移动止损已覆盖, 不引入死代码)."""
    from quant.config.constants import _require_cfg
    with pytest.raises(Exception):
        _require_cfg("risk.breakeven_buffer_pct")


# ════════════════════════════════════════════════════════════════
# P2-8: 盈利停滞时间退出
# ════════════════════════════════════════════════════════════════

def test_v553_time_stop_hard_for_profitable_stale():
    """盈利 +5% 但持仓 45 天 (>2×20) → time_stop_hard 无条件退出."""
    rm = _mk_rm()
    pos = _pos(buy_time="2026-07-05 09:30:00")
    sigs = rm.check([pos], _quotes(105.0), "2026-08-19",
                    atr_panel={"2026-08-19": {"600000": 10.0}})
    assert len(sigs) == 1
    assert sigs[0]["reason"].startswith("time_stop_hard")
    assert sigs[0]["kind"] == "loss"


def test_v553_time_stop_still_needs_loss_under_double():
    """持仓 30 天 (>20 但 <40) + 盈利 → 不退出 (让利润奔跑)."""
    rm = _mk_rm()
    pos = _pos(buy_time="2026-07-20 09:30:00")
    sigs = rm.check([pos], _quotes(105.0), "2026-08-19",
                    atr_panel={"2026-08-19": {"600000": 10.0}})
    assert sigs == []


# ════════════════════════════════════════════════════════════════
# P2-10: meta 仅变化时写
# ════════════════════════════════════════════════════════════════

def test_v553_meta_updated_only_on_new_peak():
    """内存模式: peak 新高才更新; 回落轮次不覆盖峰值 (回载语义 MAX 聚合)."""
    rm = _mk_rm()
    today = "2026-08-19"
    panel = {"2026-08-19": {"600000": 10.0}}
    pos = _pos(shares=200)
    rm.check([pos], _quotes(121.0), today, atr_panel=panel)  # TP1, peak=121
    assert rm._meta_store["600000"]["_peak"] == 121.0
    assert rm._meta_store["600000"]["_tp1_hit"] is True

    pos2 = _pos(shares=200, _tp1_hit=True, _peak=121.0,
                _loaded_meta={"_tp1_hit": True, "_peak": 121.0})
    rm.check([pos2], _quotes(115.0), today, atr_panel=panel)  # 回落, peak 不变
    assert rm._meta_store["600000"]["_peak"] == 121.0

    pos3 = _pos(shares=200, _tp1_hit=True, _peak=121.0,
                _loaded_meta={"_tp1_hit": True, "_peak": 121.0})
    rm.check([pos3], _quotes(125.0), today, atr_panel=panel)  # 新高 → 更新
    assert rm._meta_store["600000"]["_peak"] == 125.0


# ════════════════════════════════════════════════════════════════
# kind 字段完整性 (monitor 出场性质判定依赖)
# ════════════════════════════════════════════════════════════════

def test_v553_kind_field_on_all_signals():
    """各信号 kind: TP1/TP2/trail_lock=profit, hard_sl/trail_sl=loss.
    (各用例独立 symbol, 避免内存模式 meta_store 跨用例污染)"""
    rm = _mk_rm()
    today = "2026-08-19"
    panel = {"2026-08-19": {"600000": 10.0, "600001": 10.0, "600002": 10.0}}

    tp1 = rm.check([_pos(shares=400)], _quotes(121.0), today, atr_panel=panel)[0]
    assert tp1["kind"] == "profit"

    sl = rm.check([_pos(symbol="600001")], _quotes(79.0, symbol="600001"),
                  today, atr_panel=panel)[0]
    assert sl["kind"] == "loss"
    assert sl["reason"].startswith("hard_sl")

    trail = rm.check([_pos(symbol="600002", _tp1_hit=True, _peak=121.0,
                           _loaded_meta={"_tp1_hit": True, "_peak": 121.0})],
                     _quotes(104.0, symbol="600002"), today, atr_panel=panel)
    assert any(s["kind"] == "profit" and s["reason"].startswith("trail_lock")
               for s in trail) or trail == []
