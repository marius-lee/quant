"""Simulated Broker — wraps execution logic, reuses DataStore connections.

Encapsulates what was scattered in _get_prices and execute_signals calls
in backtest/loop.py. Provides a clean API for backtest event handling.
"""

from typing import Optional


class SimulatedBroker:
    """Simulated broker for backtesting — wraps DataStore + ExecutionEngine.

    test-v398 (perf): 接收 data_full 预加载数据，get_prices 优先从内存切片，
    消除每日期 SQLite round-trip。
    """

    def __init__(self, store, engine, db_path, data_full=None):
        self.store = store
        self.engine = engine
        self.db_path = db_path
        self.data_full = data_full

    def get_prices(self, symbols, date, field="open"):
        """Get prices — fast path from preloaded data_full, fallback DataStore DB.

        test-v398 (perf): 回测中 data_full 预加载全量日线，直接从内存切片。
        """
        from quant.backtest.loop import _get_prices
        return _get_prices(symbols, date, self.store, field=field, data_full=self.data_full)

    def execute(self, targets, date, strategy="quant", suppress_push=True):
        """Execute target positions at specified date prices."""
        from quant.pipeline import execute_signals
        all_syms = set()
        for tp in targets:
            all_syms.add(tp["symbol"])
        for p in self.engine.get_positions(strategy):
            all_syms.add(p["symbol"])

        open_prices = self.get_prices(list(all_syms), date, field="open")
        if not open_prices:
            return {"executed": [], "wealth": self.engine.get_capital(strategy), "skipped": True}

        # B8 (CODE-REVIEW): 回测涨跌停成交模拟 —
        # 一字板 (open==high==low==涨/跌停价) 实际无法成交, 原回测按开盘价
        # 100% 成交 → 过度乐观. ohlc 字典供 BacktestExecutionModel 阻断.
        ohlc = self._day_ohlc(list(all_syms), date)

        result = execute_signals(
            targets, date, strategy=strategy,
            prices=open_prices,
            db_path=self.db_path,
            suppress_push=suppress_push,
            ohlc=ohlc,
        )
        # B-06 fix: 净值按当日收盘价 MTM (原成本价 → 净值只在交易日变动, 指标失真)
        result["wealth"] = self.get_mtm_capital(strategy, date)
        return result

    def _day_ohlc(self, symbols, date):
        """构造执行日 OHLC + 前收盘 for 涨跌停判定 (B8).

        前收盘取 data_full 中日期 < date 的最近 T-1 close;
        无数据或缺失 → 返回 {sym: None...} 标注, 判定端安全跳过.
        """
        out = {}
        if self.data_full is None:
            return out
        try:
            if date not in self.data_full.index:
                return {}
            row = self.data_full.loc[date]
            prev_dates = [d for d in self.data_full.index if d < date]
            prev_close = self.data_full.loc[prev_dates[-1], "close"] if prev_dates else None
            for sym in symbols:
                for f in ("open", "high", "low"):
                    try:
                        v = float(row[f].get(sym, 0) or 0)
                    except (KeyError, TypeError, AttributeError):
                        v = 0.0
                    out.setdefault(sym, {})[f] = v
                pc = None
                if prev_close is not None:
                    try:
                        pc = float(prev_close.get(sym, 0) or 0)
                    except (KeyError, TypeError):
                        pc = None
                out[sym]["prev_close"] = pc
        except Exception as _e:
            from quant.utils.logger import get_logger
            get_logger("backtest.broker").warning(
                "B8: day_ohlc for %s failed (non-fatal): %s", date, _e)
        return out

    def execute_risk_only(self, date, strategy="quant"):
        """非调仓日 (rebalance_freq=weekly): 只跑硬止损, 不做组合再平衡.

        targets 空列表 + risk_only=True → ExecutionModel 跳过 delta/分单,
        仅风控段生效. 返回结构与 execute() 一致 (含 wealth/stopped_out).
        """
        from quant.pipeline import execute_signals
        positions = self.engine.get_positions(strategy)
        all_syms = [p["symbol"] for p in positions]
        open_prices = self.get_prices(all_syms, date, field="open") if all_syms else {}
        if not open_prices:
            return {"executed": [], "wealth": self.get_mtm_capital(strategy, date),
                    "skipped": True, "stopped_out": []}
        result = execute_signals(
            [], date, strategy=strategy,
            prices=open_prices,
            db_path=self.db_path,
            suppress_push=True,
            risk_only=True,
        )
        result["wealth"] = self.get_mtm_capital(strategy, date)
        return result
    def get_mtm_capital(self, strategy, date):
        """按 date 收盘价计算 MTM 总资产 (B-06). 缺价标的回退成本价。"""
        positions = self.engine.get_positions(strategy)
        closes = {}
        if positions:
            closes = self.get_prices([p["symbol"] for p in positions], date, field="close")
        return self.engine.get_capital(strategy, prices=closes)

    def get_capital(self, strategy="quant"):
        return self.engine.get_capital(strategy)

    def get_positions(self, strategy="quant"):
        return self.engine.get_positions(strategy)
