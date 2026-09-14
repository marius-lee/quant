"""Backtest loop types."""

class BacktestEngine:
    """Convenience wrapper for parameterized backtesting."""

    def __init__(self, start="2022-01-01", end="2024-12-31", capital=5000):
        self.start = start
        self.end = end
        self.capital = capital

    def run(self):
        return run_backtest(self.start, self.end, self.capital)

    @property
    def default_params(self):
        return {"start": self.start, "end": self.end, "capital": self.capital}


