"""B1-B7 卖出责任链 — 每个handler检查一个条件, 命中返回reason, 否则传递.
来源: 陈小群三层卖出体系 + Grinold MCVA + Harris波动分解 + Narang时间止
"""
import math
from ops.performance import RESIDUAL_VOL_DEFAULT, IC_PRIOR, SELL_COST, alpha_from_score, mcva_trailing_stop
from ops.liquidity import volatility_decompose
from ops.signal_algo import zscore_peaks


class SellHandler:
    def __init__(self): self.next = None
    def then(self, handler):
        self.next = handler; return handler
    def check(self, pos, st, ctx) -> str | None:
        reason = self._check(pos, st, ctx)
        return reason if reason else (self.next.check(pos, st, ctx) if self.next else None)
    def _check(self, pos, st, ctx) -> str | None: return None


class B1_HardStop(SellHandler):
    def _check(self, pos, st, ctx):
        pnl_pct = (st["close"] / pos["price"] - 1) * 100
        return f"止损({pnl_pct:.1f}%)" if pnl_pct <= -5 else None

class B2_LateBreak(SellHandler):
    def _check(self, pos, st, ctx):
        if ctx.get("now").hour >= 14 and ctx["now"].minute >= 30:
            if pos.get("was_at_limit") and not st["is_at_limit"] and pos.get("has_sealed"):
                return "尾盘炸板"
        return None

class B3_RepeatedBreak(SellHandler):
    def _check(self, pos, st, ctx):
        return "反复烂板" if st["broken_count"] >= 3 and not st["is_at_limit"] else None

class B4_DeathTurnover(SellHandler):
    def _check(self, pos, st, ctx):
        if st.get("close", 0) <= 0: return None
        prev_vol = ctx.get("prev_volume_map", {}).get(pos["symbol"], 0)
        vol_ratio = st.get("volume", 0) / prev_vol if prev_vol > 0 else 0
        return f"死亡换手({vol_ratio:.0%})" if vol_ratio > 0.60 else None

class B5_ShrinkAccelerate(SellHandler):
    def _check(self, pos, st, ctx):
        if not (st["is_at_limit"] and st.get("close", 0) > 0): return None
        prev_vol = ctx.get("prev_volume_map", {}).get(pos["symbol"], 0)
        vol_ratio = st.get("volume", 0) / prev_vol if prev_vol > 0 else 0
        return f"缩量加速({vol_ratio:.0%})" if 0 < vol_ratio < 0.08 else None

class B6_MCVA(SellHandler):
    def _check(self, pos, st, ctx):
        peak = pos.get("peak_price", pos["price"])
        entry_alpha = pos.get("entry_alpha", alpha_from_score(0.50))
        daily_vol = RESIDUAL_VOL_DEFAULT / math.sqrt(252)
        pnl_dec = st["close"] / pos["price"] - 1
        if daily_vol <= 0: return None
        alpha_now = RESIDUAL_VOL_DEFAULT * IC_PRIOR * (pnl_dec / daily_vol)
        tpos = ctx.get("track_positions", [])
        total_val = ctx.get("track_capital", 5000)
        for p2 in tpos:
            total_val += p2["shares"] * st.get("close", p2["price"])
        pos_pct = (pos["shares"] * st["close"]) / max(total_val, 1)
        dist = mcva_trailing_stop(entry_alpha, alpha_now, pos_pct)
        if dist < 0:
            return f"MCVA止盈(α={alpha_now:.4f}<阈值)"
        if peak > pos["price"] and st["close"] < peak * 0.95:
            return f"移动止盈(最高¥{peak:.2f}→现¥{st['close']:.2f})"
        return None

class B6z_ZScorePeak(SellHandler):
    """z-score峰值检测: 近期出现峰值→趋势可能逆转→提前止盈 (来源: 程序员量化笔记)"""
    def _check(self, pos, st, ctx):
        try:
            prices = st.get("prices", [])
            if len(prices) < 10: return None
            closes = [p[1] for p in prices[-20:]]
            sigs, _, _ = zscore_peaks(closes, lag=5, threshold=3.0, influence=0.5)
            if sigs and sigs[-1] == -1:  # 最近一个信号是向下峰值
                pnl = (st["close"] / pos["price"] - 1) * 100
                if pnl > 0:  # 盈利中才提前止盈
                    return f"z-score峰值({pnl:.1f}%)"
        except Exception: pass
        return None


class B6c_HarrisVolatility(SellHandler):
    def _check(self, pos, st, ctx):
        try:
            vol = volatility_decompose(pos["symbol"], ctx.get("conn"))
            if not (vol["valid"] and vol["transitory_ratio"] > 0.5): return None
            pnl_dec = st["close"] / pos["price"] - 1
            daily_vol = RESIDUAL_VOL_DEFAULT / math.sqrt(252)
            alpha_now = RESIDUAL_VOL_DEFAULT * IC_PRIOR * (pnl_dec / daily_vol) if daily_vol > 0 else 0
            tpos = ctx.get("track_positions", [])
            total_val = ctx.get("track_capital", 5000)
            for p2 in tpos:
                total_val += p2["shares"] * st.get("close", p2["price"])
            pos_pct = (pos["shares"] * st["close"]) / max(total_val, 1)
            tight_cost = SELL_COST * 0.67
            risk_term = 2 * 0.10 * 0.05 * pos_pct * RESIDUAL_VOL_DEFAULT**2
            if alpha_now < -(tight_cost + risk_term):
                return f"Harris临时波动({vol['transitory_ratio']:.0%}): 非知情驱动→预期逆转 α={alpha_now:.4f}"
        except Exception: pass
        return None

class B7_TimeStop(SellHandler):
    def _check(self, pos, st, ctx):
        board = pos.get("board_count", 0)
        max_hold = 10 if board >= 3 else 5
        days_held = ctx.get("days_held", 0)
        return f"时间止({days_held}天>{max_hold}天, Narang三元退出)" if days_held >= max_hold else None


def make_chain():
    b1 = B1_HardStop()
    b2 = B2_LateBreak()
    b3 = B3_RepeatedBreak()
    b4 = B4_DeathTurnover()
    b5 = B5_ShrinkAccelerate()
    b6 = B6_MCVA()
    b6z = B6z_ZScorePeak()
    b6c = B6c_HarrisVolatility()
    b7 = B7_TimeStop()
    b1.then(b2).then(b3).then(b4).then(b5).then(b6).then(b6z).then(b6c).then(b7)
    return b1
