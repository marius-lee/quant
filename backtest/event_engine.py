"""事件驱动回测引擎 — QuantConnect 风格。

逐日模拟: 行情→信号→下单→成交→持仓→净值，逼近真实交易。
"""

import numpy as np
import pandas as pd
from typing import Callable
from backtest import compute_commission
from utils.logger import get_logger

logger = get_logger("backtest.engine")


class EventDrivenBacktest:
    """事件驱动回测引擎"""

    def __init__(self, initial_capital: float = 1_000_000,
                 commission: float = 0.0003, slippage: float = 0.001,
                 max_weight: float = 0.10, max_positions: int = 30,
                 max_drawdown: float = 0.15, daily_loss_limit: float = 0.05,
                 t_plus_1: bool = True):
        self.capital = initial_capital
        self.init_capital = initial_capital
        self.commission = commission
        self.slippage = slippage
        self.max_weight = max_weight
        self.max_positions = max_positions
        self.max_drawdown = max_drawdown
        self.daily_loss_limit = daily_loss_limit
        self.t_plus_1 = t_plus_1  # A股 T+1

        # 状态
        self.cash = initial_capital
        self.positions = {}       # symbol → shares
        self.pending_buys = {}    # T+1 待买入
        self.equity = []          # 每日净值
        self.trades = []
        self.peak_equity = initial_capital  # 风控用
        self.stopped = False

    def run(self, prices: pd.DataFrame, signal_fn: Callable,
            volumes: pd.DataFrame = None) -> dict:
        """逐日运行回测"""
        dates = prices.index.sort_values()
        stocks = prices.columns.tolist()
        logger.info(f"event engine starting: {len(dates)} dates, {len(stocks)} stocks, "
                    f"volumes={'yes' if volumes is not None else 'no'}")

        for i, date in enumerate(dates):
            if i < 60:
                self.equity.append({"date": date, "value": self.init_capital})
                continue

            today_prices = prices.iloc[i]
            today_volume = volumes.iloc[i] if volumes is not None else None

            # 1. T+1: 昨天买的今天到账
            self._settle_pending(today_prices)

            # 2. 生成今日信号
            try:
                weights = signal_fn(date)
            except Exception:
                logger.warning(f"signal_fn failed on {date}, using zero weights")
                weights = pd.Series(0.0, index=stocks)

            if weights.sum() > 0:
                weights = weights.clip(upper=self.max_weight)
                weights = weights / weights.sum()

            # 3. 计算目标仓位并下单
            total_value = self._total_value(today_prices)
            planned_buys = []  # (sym, shares, price, cost) 收集买入意向，先不执行
            for sym in stocks:
                target_value = total_value * weights.get(sym, 0)
                current_value = self.positions.get(sym, 0) * today_prices.get(sym, 0)
                diff_value = target_value - current_value

                if np.isnan(diff_value) or abs(diff_value) < 500:
                    continue

                price = today_prices.get(sym, 0)
                if np.isnan(price) or price <= 0:
                    continue

                # 成交量约束: 仅对买入限制（不超过当日成交量的5%），卖出不受此约束
                max_buy_shares = int(today_volume.get(sym, 1e9) * 0.05 // 100 * 100) if today_volume is not None else 999999900
                target_shares = int(diff_value / price / 100) * 100
                # 买入: 上限为 max_buy_shares；卖出: 无成交量上限
                if target_shares > 0:
                    shares = min(max_buy_shares, target_shares)
                else:
                    shares = target_shares  # 卖出不做成交量限制

                if shares == 0:
                    continue

                trade_value = abs(shares) * price
                fee, stamp, total_fee = compute_commission(trade_value, is_sell=(shares < 0))
                cost = trade_value * (1 + self.slippage) + total_fee

                if shares > 0:
                    planned_buys.append((sym, shares, price, cost))
                else:
                    current_shares = self.positions.get(sym, 0)
                    sell_shares = min(abs(shares), current_shares)
                    if sell_shares > 0:
                        sell_value = sell_shares * price
                        sell_fee, sell_stamp, sell_total = compute_commission(sell_value, is_sell=True)
                        net_proceeds = sell_value * (1 - self.slippage) - sell_total
                        self.positions[sym] = current_shares - sell_shares
                        self.cash += net_proceeds
                        self.trades.append({
                            "date": date, "symbol": sym, "shares": -sell_shares,
                            "price": price, "side": "sell",
                        })

            # 买入: 受 max_positions 约束，按资金分配从大到小排序后截断
            slots = self.max_positions - len(self.positions) - len(self.pending_buys)
            if slots > 0 and planned_buys:
                planned_buys.sort(key=lambda x: x[3], reverse=True)  # by cost desc
                if i == 60:  # 首次交易日记录一次
                    logger.info(f"first trading day: {slots} slots, {len(planned_buys)} planned buys, "
                                f"max cost={planned_buys[0][3]:.0f}, min cost={planned_buys[-1][3]:.0f}")
                for sym, shares, price, cost in planned_buys[:slots]:
                    if self.cash >= cost:
                        if self.t_plus_1:
                            self.pending_buys[sym] = self.pending_buys.get(sym, 0) + shares
                        else:
                            self.positions[sym] = self.positions.get(sym, 0) + shares
                        self.cash -= cost
                        self.trades.append({
                            "date": date, "symbol": sym, "shares": shares,
                            "price": price, "side": "buy",
                        })

            # 4. 按市价估值
            value = self._total_value(today_prices)
            self.equity.append({"date": date, "value": value})

            # 5. 风控检查
            self.peak_equity = max(self.peak_equity, value)
            drawdown = (self.peak_equity - value) / self.peak_equity
            if drawdown > self.max_drawdown:
                self._liquidate(today_prices, date, f"max_drawdown {drawdown:.1%}")
                self.stopped = True
                break

            if i > 0:
                prev_value = self.equity[-2]["value"]
                daily_loss = (prev_value - value) / prev_value if prev_value > 0 else 0
                if daily_loss > self.daily_loss_limit:
                    self._liquidate(today_prices, date, f"daily_loss {daily_loss:.1%}")
                    self.stopped = True
                    break

        logger.info(f"event engine: {len(self.trades)} trades, {len(self.positions)} final positions")
        return self._build_result(prices)

    def _liquidate(self, prices: pd.Series, date, reason: str):
        """风控强平：卖出所有持仓"""
        self.pending_buys.clear()
        for sym, shares in list(self.positions.items()):
            p = prices.get(sym, 0)
            if shares > 0 and not np.isnan(p) and p > 0:
                sell_value = shares * p
                sell_fee, sell_stamp, sell_total = compute_commission(sell_value, is_sell=True)
                self.cash += sell_value * (1 - self.slippage) - sell_total
                self.trades.append({
                    "date": date, "symbol": sym, "shares": -shares,
                    "price": p, "side": "sell", "reason": reason,
                })
            del self.positions[sym]

    def _settle_pending(self, prices: pd.Series):
        for sym, shares in list(self.pending_buys.items()):
            self.positions[sym] = self.positions.get(sym, 0) + shares
            del self.pending_buys[sym]

    def _total_value(self, prices: pd.Series) -> float:
        pos_value = 0.0
        for sym, shares in self.positions.items():
            p = prices.get(sym, 0)
            if not np.isnan(p):
                pos_value += shares * p
        return self.cash + pos_value

    def _build_result(self, prices: pd.DataFrame) -> dict:
        from backtest.metrics import compute_metrics

        eq = pd.DataFrame(self.equity).set_index("date")
        eq["return"] = eq["value"].pct_change()
        returns = eq["return"].dropna()

        metrics = compute_metrics(returns, initial_capital=self.init_capital)

        return {
            "equity_curve": eq,
            "trades": pd.DataFrame(self.trades) if self.trades else pd.DataFrame(),
            "metrics": metrics,
        }


import os
if __name__ == "__main__":
    import warnings; warnings.filterwarnings("ignore")
    from data.store import DataStore

    store = DataStore(tushare_token=os.environ.get("TUSHARE_TOKEN", ""))
    conn = store._connect()
    stocks = [r[0] for r in conn.execute(
        "SELECT symbol FROM daily GROUP BY symbol HAVING COUNT(*)>=250 LIMIT 30"
    ).fetchall()]

    raw = store.get_daily(stocks)
    prices = raw["close"].sort_index().dropna(how="all")

    engine = EventDrivenBacktest(initial_capital=1_000_000)

    def random_signal(date):
        w = pd.Series(np.random.random(len(stocks)), index=stocks)
        top = w.nlargest(10)
        result = pd.Series(0.0, index=stocks)
        result[top.index] = 1.0 / 10
        return result

    result = engine.run(prices, random_signal)
    m = result["metrics"]
    print(f"事件驱动回测: 夏普={m['sharpe_ratio']:.3f} 最大回撤={m['max_drawdown']:.2%}")
    print(f"交易次数: {len(result['trades'])}")
