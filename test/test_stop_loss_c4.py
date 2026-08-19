"""C4 (CODE-REVIEW 2026-08-07): ATR 止盈止损边界语义回归.

修复前:
  - TP1 (shares=100 一手): `max(100, 100//2//100*100)` → 卖 100 股 = 卖全仓
  - TP2 残留: `shares - max(100, shares//2//100*100)` 亏手数时 = 0 → 卖 0 股
  - trail_sl 无门槛: peak>cost 即启用 → 微利噪音波动出场
"""
import pytest

from quant.execution.stop_loss import RiskManager

_ATR = 8.0


def _atr(sym, period=20, as_of=None):
    return _ATR


@pytest.fixture(autouse=True)
def _patch_atr(monkeypatch):
    import quant.execution.stop_loss as sl
    monkeypatch.setattr(sl, "_compute_atr", _atr)


def _pos(**kw):
    d = {"symbol": "600519", "price": 100.0, "shares": 300}
    d.update(kw)
    return d


def _quotes(price):
    return {"600519": {"price": price}}


def _check(pos, price):
    return RiskManager(cooloff_store={}).check([pos], _quotes(price), "2026-07-25")


def tp_share(res, prefix):
    hits = [r for r in res if r["reason"].startswith(prefix)]
    assert hits, f"{prefix} 应触发: {[r['reason'] for r in res]}"
    return hits[0]["shares"]


def test_tp1_300_shares_sells_round_half():
    """300 股触碰 TP1 → 卖 100 (一半向下取整手), 非 300 全卖."""
    res = _check(_pos(shares=300), 100 + 2 * _ATR)
    assert tp_share(res, "TP1") == 100


def test_tp1_200_shares_sells_100():
    res = _check(_pos(shares=200), 116.0)
    assert tp_share(res, "TP1") == 100


def test_tp1_single_lot_marks_hit_no_sell():
    """B8 (2026-08-18): 一手 100 股时 TP1 无法对半卖整手 → 不卖出, 仅标记
    tp1_hit, 等 TP2/trail 全卖 — 原实现 TP1 即全仓卖出, 提前清仓 (止盈语义错)."""
    pos = _pos(shares=100)
    res = _check(pos, 116.0)
    assert not any(r["reason"].startswith("TP") for r in res)
    assert pos.get("_tp1_hit") is True


def test_tp1_single_lot_then_tp2_sells_all():
    """B8: 100 股 TP1 标记后, 触碰 TP2 → 全卖 (完整止盈链)."""
    pos = _pos(shares=100, _tp1_hit=True)
    res = _check(pos, 124.0)
    assert tp_share(res, "TP2") == 100


def test_tp2_residual_lot_sells_rest_not_zero():
    """TP1 已卖 100 (剩 200), 触碰 TP2 → 卖剩余 100; 修前 200-100=100 仍对;
    C4 关键: 剩 100 时 (一手) → 卖 100 而非 0."""
    pos = _pos(shares=100, _tp1_hit=True)  # 剩一手
    res = _check(pos, 100 + 3 * _ATR)
    assert tp_share(res, "TP2") == 100


def test_tp2_300_shares_sells_rest():
    """v553: TP2 清仓语义 — TP1 已触发后剩余 300 股全部卖出
    (原卖 200 留 100, 偶数整手永远留 25% 尾巴横盘无限期持有)."""
    pos = _pos(shares=300, _tp1_hit=True)  # TP1 已卖 100 → 剩 200
    res = _check(pos, 124.0)
    assert tp_share(res, "TP2") == 300


def test_trail_sl_needs_tp1_level_profit():
    """peak 微利 (+0.25ATR) 回撤也不触发 trail_sl (原任何 peak>cost 都触发)."""
    pos = _pos(shares=100, _peak=102.0)
    res = _check(pos, 96.0)  # 从 peak 回撤 > 2ATR
    assert not any(r["reason"].startswith("trail") for r in res)


def test_trail_sl_fires_after_tp_level():
    """peak ≥ cost+2ATR 且从峰值回撤 ≥2ATR → trail_sl 触发全卖."""
    pos = _pos(shares=100, _peak=120.0)
    res = _check(pos, 104.0)  # 120→104 回撤 16 = 2ATR
    assert any(r["reason"] == "trail_sl(2.0ATR from peak)" for r in res)


def _check(pos, price):
    return RiskManager(cooloff_store={}).check([pos], _quotes(price), "2026-07-25")


def tp_share(res, prefix):
    hits = [r for r in res if r["reason"].startswith(prefix)]
    assert hits, f"{prefix} 未触发: {[r['reason'] for r in res]}"
    return hits[0]["shares"]