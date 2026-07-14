"""Simulated Broker — wraps execution logic, reuses DataStore connections.

Encapsulates what was scattered in _get_prices and execute_signals calls
in backtest/loop.py. Provides a clean API for backtest event handling.
"""

from typing import Optional


class SimulatedBroker:
    """Simulated broker for backtesting — wraps DataStore + ExecutionEngine."""

    def __init__(self, store, engine, db_path):
        self.store = store
        self.engine = engine
        self.db_path = db_path

    def get_prices(self, symbols, date, field="open"):
        """Get prices from DataStore — reuses connection + LRU cache."""
        from quant.backtest.loop import _get_prices
        return _get_prices(symbols, date, self.store, field=field)

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

        result = execute_signals(
            targets, date, strategy=strategy,
            prices=open_prices,
            db_path=self.db_path,
            suppress_push=suppress_push,
        )
        result["wealth"] = self.engine.get_capital(strategy)
        return result

    def get_capital(self, strategy="quant"):
        return self.engine.get_capital(strategy)

    def get_positions(self, strategy="quant"):
        return self.engine.get_positions(strategy)
