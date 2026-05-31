"""推荐引擎 — 编排各子模块。全管线: 加载→筛选→训练→预测→信号→回测→组装"""
import pandas as pd
from data.store import DataStore
from data.repository import FactorRepo, StockRepo
from factor.demon import DemonSignals
from engine.loader import get_training_data
from engine.screener import screen_and_split
from engine.trainer import train_model
from engine.predictor import predict_all
from engine.ranker import apply_demon_and_neutralize
from engine.backtest_runner import run_backtest
from engine.builder import build_result
from strategy.signals import generate_signals, generate_weights
from config.loader import get as cfg
from utils.logger import get_logger

logger = get_logger("pipeline")


class RecommendationEngine:
    def __init__(self, tushare_token: str = ""):
        self.store = DataStore(tushare_token=tushare_token)
        self.factors = FactorRepo(self.store)
        self.model = None
        self.last_result = None
        self.demon = DemonSignals()

    def run(self, top_n: int = None, batch_size: int = 500,
            method: str = "static", weight_method: str = "prediction") -> dict:
        """全量分析管线。

        Args:
            top_n: 推荐股票数，默认从 config 读取
            method: 回测模式
              - "static": 向量化回测，单次买入持有到期 (快，适合快速迭代)
              - "rebalance": 周频再平衡回测，每月重排名调仓 (真实调仓模拟)
              - "event": 事件驱动回测 (T+1/涨跌停/风控熔断)
            weight_method: 仓位分配方式
              - "equal": 等权
              - "prediction": 按预测得分加权 (推荐，强者重仓)
        """
        if top_n is None:
            top_n = cfg("backtest.max_positions", 3)

        data = get_training_data(self.store)
        if "error" in data:
            return data

        scr = screen_and_split(self.factors, data["close_df"], data["all_stocks"])
        passed = scr["passed"]
        if not passed:
            return {"error": "无通过 IC 筛选的因子"}

        logger.info(f"training on {len(data['all_stocks'])} stocks, {len(passed)} factors")
        self.model = train_model(
            self.factors, data["all_stocks"], passed,
            scr["train_dates_set"], scr["y_data_full"]
        )
        if self.model is None:
            return {"error": "模型训练失败"}

        pred_series = predict_all(
            self.factors, data["all_stocks"], self.model, passed, batch_size
        )
        pred_series = apply_demon_and_neutralize(
            pred_series, self.store, self.demon, StockRepo(self.store)
        )

        # 信号 → 仓位权重 (标准解耦点)
        top_pct = min(1.0, top_n / max(len(pred_series), 1))
        signals = generate_signals(pred_series, top_pct=top_pct)
        weights = generate_weights(signals, pred_series, method=weight_method)
        active_weights = weights[weights > 0]

        if method == "rebalance":
            from engine.rebalance import run_backtest_with_rebalancing
            bt_result = run_backtest_with_rebalancing(
                self.store, self.factors, data["all_stocks"], data["close_df"],
                passed, self.model, scr["all_dates"], scr["split_idx"],
                scr["test_dates_set"], pred_series=pred_series,
            )
        elif method == "event":
            bt_result = self._run_event_backtest(
                data, scr, active_weights, weight_method
            )
        else:
            bt_result = run_backtest(
                self.store, self.factors, data["all_stocks"], data["close_df"],
                passed, self.model, scr["all_dates"], scr["split_idx"],
                scr["test_dates_set"], pred_series=pred_series
            )

        screening = scr.get("screening", {})
        self.last_result = build_result(
            pred_series, self.store, data["close_df"], screening,
            self.model, data["all_stocks"], bt_result, top_n
        )
        return self.last_result

    def _run_event_backtest(self, data, scr, weights, weight_method):
        """使用事件驱动回测引擎。signal_fn 由 signals.py 生成。"""
        try:
            from backtest.event_engine import EventDrivenBacktest

            test_dates = [d for d in scr["all_dates"] if d in scr["test_dates_set"]]
            if len(test_dates) < 20:
                logger.warning("insufficient test dates for event engine")
                return {"metrics": {}, "equity_curve": pd.DataFrame(), "trades": pd.DataFrame()}

            test_close = data["close_df"].loc[data["close_df"].index.isin(test_dates)]
            if test_close.empty or weights.empty:
                return {"metrics": {}, "equity_curve": pd.DataFrame(), "trades": pd.DataFrame()}

            available = [s for s in weights.index if s in test_close.columns]
            if len(available) < 1:
                return {"metrics": {}, "equity_curve": pd.DataFrame(), "trades": pd.DataFrame()}

            target_weights = weights.loc[available]
            target_weights = target_weights / target_weights.sum()

            engine = EventDrivenBacktest(
                initial_capital=cfg("backtest.initial_capital", 5000),
                commission=cfg("backtest.commission", 0.0003),
                slippage=cfg("backtest.slippage", 0.003),
                max_weight=cfg("backtest.max_weight", 0.50),
                max_positions=cfg("backtest.max_positions", 3),
                max_drawdown=cfg("risk.max_drawdown", 0.80),
                daily_loss_limit=cfg("risk.daily_loss_limit", 0.25),
                t_plus_1=True,
            )

            prices = test_close[available]

            def signal_fn(date):
                return target_weights.copy()

            result = engine.run(prices, signal_fn)
            logger.info(f"event backtest: sharpe={result['metrics'].get('sharpe_ratio', 0):.3f}, "
                       f"trades={len(result.get('trades', []))}")
            return result

        except Exception as e:
            logger.warning(f"event engine failed: {e}, falling back to static")
            return run_backtest(
                self.store, self.factors, data["all_stocks"], data["close_df"],
                [], self.model, scr["all_dates"], scr["split_idx"], scr["test_dates_set"],
                pred_series=pd.Series()
            )
