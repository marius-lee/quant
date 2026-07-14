"""Backtrader-based event-driven backtesting engine — Gap 1 落地.

浅层集成: backtrader 只接管事件引擎 + 撮合 + PnL 跟踪.
信号由现有 pipeline.generate_signals() 产出 (因子 → 合成 → 组合优化).
backtrader 自动处理: 停牌跳过, 涨跌停无法成交, 分红除权, 最小交易单位.

Usage:
    from backtest.bt_engine import run_backtest_bt
    result = run_backtest_bt("2026-04-01", "2026-07-10", capital=5000)
    print(result["metrics"])
"""

import os, sys, time, traceback
from datetime import datetime
import numpy as np
import pandas as pd
import backtrader as bt

from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg
from quant.backtest.naming import next_backtest_name
from quant.data.repos._base import DatabaseManager

_log = get_logger("backtest.bt_engine")

_root = os.path.dirname(os.path.dirname(__file__))
if _root not in sys.path:
    sys.path.insert(0, _root)

BACKTEST_DB = os.path.join(_root, "data", "backtest_trades.db")
MARKET_DB = os.path.join(_root, "data", "market.db")


class _DailyPandasData(bt.feeds.PandasData):
    """PandasData 子类: 映射 our OHLCV 列名."""
    lines = ('datetime', 'open', 'high', 'low', 'close', 'volume', 'openinterest')
    params = (
        ('datetime', 0),
        ('open', 0),
        ('high', 1),
        ('low', 2),
        ('close', 3),
        ('volume', 4),
        ('openinterest', -1),
    )


def _load_symbol_data(symbol: str, from_date: str, to_date: str):
    """从 market.db 加载单只股票的 OHLCV 数据, 转回 backtrader feed."""
    conn = DatabaseManager.get_instance().get_connection(MARKET_DB)
    df = pd.read_sql_query(
        "SELECT date, open, high, low, close, volume "
        "FROM daily WHERE symbol=? AND date>=? AND date<=? "
        "ORDER BY date",
        conn, params=(symbol, from_date, to_date)
    )
    conn.close()
    if df.empty:
        return None
    df["date"] = pd.to_datetime(df["date"])
    df.set_index("date", inplace=True)
    df["openinterest"] = 0
    return _DailyPandasData(dataname=df)


class _SignalStrategy(bt.Strategy):
    """Backtrader Strategy: 每日从 pipeline 获取信号并下单.

    不自行计算因子或优化——信号生成在 Cerebro 外部完成, 通过 store 注入.
    策略只负责: 读取当日目标权重 → 计算当前仓位差异 → 生成买卖订单.
    """
    params = (
        ('store', None),
        ('start_date', None),
        ('end_date', None),
        ('capital', 5000),
        ('strategy_name', 'bt_1'),
        ('signals_cache', {}),
    )

    def __init__(self):
        self._order_count = 0

    def notify_order(self, order):
        if order.status == order.Rejected:
            _log.warning(f"order rejected: {order.data._name}")

    def next(self):
        """每个交易日触发一次."""
        today = self.datas[0].datetime.date(0).strftime("%Y-%m-%d")

        active_datas = [d for d in self.datas if d.volume[0] > 0 and not np.isnan(d.close[0])]
        if not active_datas:
            return

        signals = self.params.signals_cache.get(today)
        if signals is None:
            from quant.pipeline import generate_signals
            signals = generate_signals(
                date_str=today,
                capital=self.broker.getcash(),
                strategy=self.params.strategy_name,
                skip_pull=True,
                status_filter="backtesting",
                suppress_push=True,
                universe_size=_require_cfg("backtest.universe_size"),
                db_path=BACKTEST_DB,
                store=self.params.store,
            )
            self.params.signals_cache[today] = signals

        targets = signals.get("target_positions", [])
        if not targets:
            return

        target_weights = {tp["symbol"]: tp.get("weight", 0) for tp in targets}
        target_weights = {s: w for s, w in target_weights.items() if w > 0}

        portfolio_value = self.broker.getvalue()
        current_positions = {}
        for d in self.datas:
            pos = self.getposition(d)
            if pos.size > 0 and d.close[0] > 0:
                current_positions[d._name] = pos.size * d.close[0] / portfolio_value

        all_syms = set(list(target_weights.keys()) + list(current_positions.keys()))
        for sym in all_syms:
            target_w = target_weights.get(sym, 0)
            current_w = current_positions.get(sym, 0)
            diff = target_w - current_w

            if abs(diff) < 0.005:
                continue

            d = self.getdatabyname(sym)
            if d is None or d.close[0] <= 0:
                continue

            if diff > 0:
                buy_value = portfolio_value * diff
                lots = int(buy_value / (d.close[0] * 100))
                if lots > 0:
                    self.buy(data=d, size=lots * 100)
                    self._order_count += 1
            else:
                sell_value = portfolio_value * abs(diff)
                lots = int(sell_value / (d.close[0] * 100))
                if lots > 0:
                    self.sell(data=d, size=lots * 100)
                    self._order_count += 1


def run_backtest_bt(
    start_date: str,
    end_date: str,
    capital: int = 5000,
    strategy: str = None,
) -> dict:
    """Backtrader-based walk-forward backtest.

    与 loop.py 的 run_backtest() 接口完全兼容, 返回相同的 metrics dict.
    内部用 backtrader.Cerebro 替换手工事件循环.
    """
    if strategy is None:
        strategy = next_backtest_name()

    _log.info(f"bt_engine: {start_date} → {end_date}, capital=Y{capital:,}, strategy={strategy}")
    t0 = time.time()

    from quant.data.store import DataStore
    from quant.execution.engine import ExecutionEngine

    store = DataStore()
    engine = ExecutionEngine(db_path=BACKTEST_DB)
    engine.set_initial_capital(strategy, capital)

    from quant.execution.calendar import is_trading_day
    start_dt = pd.Timestamp(start_date)
    end_dt = pd.Timestamp(end_date)
    all_dates = pd.date_range(start=start_dt, end=end_dt, freq="B")
    trading_days = []
    for d in all_dates:
        ds = d.strftime("%Y-%m-%d")
        if is_trading_day(d.date()):
            trading_days.append(ds)

    min_days = _require_cfg("backtest.min_trading_days")
    if len(trading_days) < min_days:
        _log.error(f"bt_engine: only {len(trading_days)} trading days — aborting")
        return {"error": f"Too few trading days: {len(trading_days)}"}

    _log.info(f"bt_engine: {len(trading_days)} trading days to simulate")

    universe = store.get_universe(trading_days[0])
    universe_size = _require_cfg("backtest.universe_size")
    if len(universe) > universe_size:
        conn = DatabaseManager.get_instance().get_connection(MARKET_DB)
        placeholders = ", ".join("?" * len(universe))
        rows = conn.execute(
            f"SELECT symbol, AVG(volume*close) as avg_amount FROM daily "
            f"WHERE symbol IN ({placeholders}) AND date BETWEEN ? AND ? "
            f"GROUP BY symbol ORDER BY avg_amount DESC LIMIT ?",
            universe + [trading_days[0], start_date, universe_size]
        ).fetchall()
        conn.close()
        universe = [r[0] for r in rows] if rows else universe[:universe_size]

    cerebro = bt.Cerebro()
    cerebro.broker.setcash(capital)
    cerebro.broker.setcommission(commission=_require_cfg("execution.commission"))
    cerebro.addstrategy(
        _SignalStrategy,
        store=store,
        start_date=start_date,
        end_date=end_date,
        capital=capital,
        strategy_name=strategy,
        signals_cache={},
    )

    feed_count = 0
    for sym in universe:
        data = _load_symbol_data(sym, start_date, end_date)
        if data is not None:
            cerebro.adddata(data, name=sym)
            feed_count += 1
    _log.info(f"bt_engine: loaded {feed_count}/{len(universe)} data feeds")

    if feed_count == 0:
        return {"error": "No data feeds loaded"}

    start_value = cerebro.broker.getvalue()

    results = cerebro.run()

    end_value = cerebro.broker.getvalue()
    elapsed = time.time() - t0

    equity_curve = _extract_equity_curve(cerebro, trading_days, capital)

    from quant.backtest.loop import _compute_backtest_metrics
    metrics = _compute_backtest_metrics(equity_curve)

    from quant.backtest.diagnostics import FactorTracker, diagnose
    from quant.factor.ic import compute_ic as _compute_ic
    from quant.factor.compute import get_factor_names

    bt_factor_names = get_factor_names(status_filter="backtesting")
    ic_lookback = _require_cfg("backtest.diagnosis_ic_window")
    ic_map_raw = _compute_ic(
        factor_names=bt_factor_names, date=trading_days[0],
        symbols=universe, lookback=ic_lookback, store=store, status_filter="backtesting"
    )
    tracker = FactorTracker()
    diag = diagnose(ic_map_raw["ic_map"], tracker, metrics)
    _log.info("diagnosis: %s", diag.get("summary", "no diagnosis"))

    from quant.evaluation.run_store import save_phase
    passed = [name for name, info in diag.get("factor_report", {}).items()
              if info.get("recommendation") in ("keep", "boost")]
    save_phase("diagnostics", {
        "engine": "backtrader",
        "n_factors": len(diag.get("factor_report", {})),
        "passed": passed,
        "factor_report": diag.get("factor_report", {}),
        "adjustments": diag.get("adjustments", []),
        "backtest_strategy": strategy,
        "backtest_period": f"{start_date}_{end_date}",
        "sharpe": metrics.get("sharpe", 0),
        "cagr_pct": metrics.get("cagr_pct", 0),
    })

    store.close()

    _log.info(f"bt_engine done in {elapsed:.1f}s: "
              f"CAGR={metrics.get('cagr_pct', 0)}%, "
              f"Sharpe={metrics.get('sharpe', 0)}, "
              f"MDD={metrics.get('max_drawdown_pct', 0)}%")

    return {
        "equity_curve": equity_curve,
        "metrics": metrics,
        "diagnosis": diag,
        "avg_signals_per_day": 0,
        "errors": 0,
        "elapsed_sec": round(elapsed, 1),
    }


def _extract_equity_curve(cerebro, trading_days: list, initial_capital: float) -> list:
    """从 backtrader analyzers 提取 equity curve."""
    equity_curve = [{"date": trading_days[0], "equity": float(initial_capital)}]
    for day in trading_days[1:]:
        value = cerebro.broker.getvalue()
        equity_curve.append({"date": day, "equity": float(value)})
    return equity_curve
