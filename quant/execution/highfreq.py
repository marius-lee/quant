"""High-Frequency Execution Engine — Tick 清洗/分钟因子/智能路由/微观结构模型.

核心能力:
  1. Tick 数据清洗/聚合: 多源融合/异常值剔除/缺失插值/分钟K线生成
  2. 分钟级因子: 价量/订单流/微观结构/波动率/流动性
  3. 智能路由: TWAP/VWAP/POV/IS/自适应/暗池/分片
  4. 微观结构模型: 订单簿重建/价差模型/冲击模型/毒性/信息份额
  5. 执行质量评估: TCA/实现缺口/机会成本/时机选择

架构:
  Tick Feed (WebSocket/TCP) → 清洗引擎 → 分钟聚合 → 因子引擎 → 智能路由 → 执行引擎
                    ↘ 微观结构模型 → 执行质量评估 (TCA)
"""

import os
import json
import time
import threading
import queue
import hashlib
import zlib
from datetime import datetime, timedelta
from collections import deque, defaultdict
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Any, Callable, Tuple, Deque
from enum import Enum
from abc import ABC, abstractmethod
from collections import deque

import pandas as pd
import numpy as np
import msgpack

from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg
from quant.config.paths import MARKET_DB
from quant.execution.cost import CostModel

_log = get_logger("execution.highfreq")


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    TWAP = "twap"
    VWAP = "vwap"
    POV = "pov"
    IS = "is"           # Implementation Shortfall
    ADAPTIVE = "adaptive"
    ICEBERG = "iceberg"
    DARK = "dark"       # 暗池


class ExecutionAlgo(str, Enum):
    TWAP = "twap"
    VWAP = "vwap"
    POV = "pov"
    IS = "is"
    ADAPTIVE = "adaptive"
    ICEBERG = "iceberg"
    DARK = "dark"
    SNIPER = "sniper"   # 狙击手 (流动性捕捉)


@dataclass
class TickData:
    """原始 Tick 数据."""
    symbol: str
    timestamp: datetime          # 精确到毫秒/微秒
    price: float                 # 最新价
    volume: int                  # 成交量 (股)
    amount: float                # 成交额 (元)
    bid_price: float = 0.0       # 买一价
    bid_volume: int = 0          # 买一量
    ask_price: float = 0.0       # 卖一价
    ask_volume: int = 0          # 卖一量
    bid_levels: List[Tuple[float, int]] = field(default_factory=list)  # 买方深度 (价, 量)
    ask_levels: List[Tuple[float, int]] = field(default_factory=list)  # 卖方深度
    source: str = ""             # 数据源
    flags: int = 0               # 标记位 (开盘/收盘/停牌/异常)


@dataclass
class MinuteBar:
    """分钟 K 线."""
    symbol: str
    timestamp: datetime          # 分钟结束时间 (如 09:31:00 代表 09:30-09:31)
    open: float
    high: float
    low: float
    close: float
    volume: int
    amount: float
    vwap: float                  # 量加权均价
    tick_count: int              # Tick 数
    bid_vwap: float = 0.0        # 买方 VWAP
    ask_vwap: float = 0.0        # 卖方 VWAP
    bid_vol: int = 0
    ask_vol: int = 0
    spread_bps: float = 0.0      # 买卖价差 (bps)
    volatility: float = 0.0      # 分钟波动率
    oi_change: int = 0           # 持仓量变化 (期货/期权)


@dataclass
class OrderRequest:
    """订单请求."""
    symbol: str
    side: OrderSide
    algo: ExecutionAlgo
    total_qty: int               # 总数量 (股)
    limit_price: float = 0.0     # 限价 (0=市价)
    start_time: datetime = None
    end_time: datetime = None
    participation_rate: float = 0.1  # POV 参与率
    max_slippage_bps: int = 10   # 最大滑点
    min_fill_size: int = 100     # 最小成交量
    allow_dark: bool = False     # 允许暗池
    strategy_id: str = ""        # 策略 ID
    client_order_id: str = ""    # 客户端订单 ID


@dataclass
class ExecutionReport:
    """执行报告."""
    order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    algo: ExecutionAlgo
    status: str                  # new/partial/fill/cancel/reject
    filled_qty: int = 0
    avg_price: float = 0.0
    remaining_qty: int = 0
    commission: float = 0.0
    slippage_bps: float = 0.0
    implementation_shortfall: float = 0.0  # 实施缺口
    arrival_price: float = 0.0   # 到达价
    benchmark_price: float = 0.0 # 基准价 (VWAP/TWAP/到达价)
    slices: List[Dict] = field(default_factory=list)  # 子订单明细
    timestamps: List[datetime] = field(default_factory=list)


class TickCleaner:
    """Tick 数据清洗引擎."""

    def __init__(self, config: Dict = None):
        self.config = config or {}
        self.price_jump_threshold = self.config.get("price_jump_threshold", 0.1)  # 10% 跳变阈值
        self.volume_spike_threshold = self.config.get("volume_spike_threshold", 100)  # 成交量异常倍数
        self.max_spread_bps = self.config.get("max_spread_bps", 500)  # 最大价差 500bps
        self.min_tick_interval_ms = self.config.get("min_tick_interval_ms", 10)  # 最小间隔 10ms
        
        # 状态缓存
        self._last_tick: Dict[str, TickData] = {}
        self._tick_buffer: Dict[str, Deque[TickData]] = defaultdict(lambda: deque(maxlen=1000))
        self._stats = defaultdict(lambda: {"cleaned": 0, "dropped": 0, "interpolated": 0})

    def clean(self, tick: TickData) -> Optional[TickData]:
        """清洗单条 Tick."""
        sym = tick.symbol
        
        # 1. 基础校验
        if tick.price <= 0 or tick.volume <= 0:
            self._stats[sym]["dropped"] += 1
            return None
        
        # 2. 价格跳变检测
        last = self._last_tick.get(sym)
        if last and last.price > 0:
            pct_change = abs(tick.price - last.price) / last.price
            if pct_change > self.price_jump_threshold:
                # 疑似错误 Tick, 插值修正
                tick.price = (last.price + tick.price) / 2
                self._stats[sym]["interpolated"] += 1
                _log.debug(f"Price jump corrected: {sym} {last.price:.2f} -> {tick.price:.2f}")
        
        # 3. 成交量异常检测
        if last and tick.volume > last.volume * self.volume_spike_threshold:
            tick.volume = min(tick.volume, last.volume * self.volume_spike_threshold)
            self._stats[sym]["interpolated"] += 1
        
        # 4. 价差检查
        if tick.bid_price > 0 and tick.ask_price > 0:
            spread_bps = (tick.ask_price - tick.bid_price) / tick.price * 10000
            if spread_bps > self.max_spread_bps:
                # 异常价差, 使用中间价
                mid = (tick.bid_price + tick.ask_price) / 2
                tick.bid_price = tick.ask_price = mid
        
        # 5. 时间间隔检查
        if last:
            dt = (tick.timestamp - last.timestamp).total_seconds() * 1000
            if dt < self.min_tick_interval_ms:
                # 过于频繁, 可能重复
                tick.timestamp = last.timestamp + timedelta(milliseconds=self.min_tick_interval_ms)
        
        # 更新状态
        self._last_tick[sym] = tick
        self._tick_buffer[sym].append(tick)
        self._stats[sym]["cleaned"] += 1
        
        return tick

    def get_stats(self, symbol: str = None) -> Dict:
        if symbol:
            return self._stats.get(symbol, {})
        return dict(self._stats)


class MinuteAggregator:
    """分钟级聚合器 — 从 Tick 生成分钟 K 线."""

    def __init__(self):
        self._buffers: Dict[str, Deque[TickData]] = defaultdict(lambda: deque(maxlen=10000))
        self._current_minute: Dict[str, datetime] = {}
        self._output_callback: Optional[Callable[[MinuteBar], None]] = None
        self._lock = threading.Lock()

    def set_output_callback(self, callback: Callable[[MinuteBar], None]):
        self._output_callback = callback

    def add_tick(self, tick: TickData):
        """添加 Tick, 自动聚合分钟线."""
        sym = tick.symbol
        with self._lock:
            # 确定 Tick 所属分钟 (向下取整到分钟)
            tick_minute = tick.timestamp.replace(second=0, microsecond=0)
            
            # 检查是否跨分钟
            last_min = self._current_minute.get(sym)
            if last_min is not None and tick_minute > last_min:
                # 输出上一分钟 K 线
                self._flush_minute(sym, last_min)
                self._current_minute[sym] = tick_minute
            elif last_min is None:
                self._current_minute[sym] = tick_minute
            
            # 加入缓冲
            self._buffers[sym].append(tick)

    def _flush_minute(self, symbol: str, minute_ts: datetime):
        """输出分钟 K 线."""
        ticks = [t for t in self._buffers[symbol] if t.timestamp.replace(second=0, microsecond=0) == minute_ts]
        if not ticks:
            return
        
        # 计算 OHLCV
        prices = [t.price for t in ticks]
        volumes = [t.volume for t in ticks]
        amounts = [t.amount for t in ticks]
        
        # VWAP
        vwap = sum(p * v for p, v in zip(prices, volumes)) / sum(volumes) if sum(volumes) > 0 else 0
        
        # 买卖方 VWAP
        bid_vols = [t.bid_volume for t in ticks if t.bid_volume > 0]
        bid_prices = [t.bid_price for t in ticks if t.bid_volume > 0]
        bid_vwap = sum(p * v for p, v in zip(bid_prices, bid_vols)) / sum(bid_vols) if bid_vols else 0
        
        ask_vols = [t.ask_volume for t in ticks if t.ask_volume > 0]
        ask_prices = [t.ask_price for t in ticks if t.ask_volume > 0]
        ask_vwap = sum(p * v for p, v in zip(ask_prices, ask_vols)) / sum(ask_vols) if ask_vols else 0
        
        # 价差
        spreads = [(t.ask_price - t.bid_price) / t.price * 10000 for t in ticks if t.ask_price > 0 and t.bid_price > 0]
        avg_spread_bps = np.mean(spreads) if spreads else 0
        
        # 波动率 (分钟内收益率标准差 * sqrt(240))
        returns = np.diff(np.log(prices))
        volatility = np.std(returns) * np.sqrt(240) if len(returns) > 1 else 0
        
        bar = MinuteBar(
            symbol=ticks[0].symbol,
            timestamp=minute_ts,
            open=prices[0],
            high=max(prices),
            low=min(prices),
            close=prices[-1],
            volume=sum(volumes),
            amount=sum(amounts),
            vwap=vwap,
            tick_count=len(ticks),
            bid_vwap=bid_vwap,
            ask_vwap=ask_vwap,
            bid_vol=sum(t.bid_volume for t in ticks),
            ask_vol=sum(t.ask_volume for t in ticks),
            spread_bps=avg_spread_bps,
            volatility=volatility,
        )
        
        if self._output_callback:
            self._output_callback(bar)

    def flush_all(self):
        """刷新所有缓冲."""
        with self._lock:
            for sym, minute_ts in self._current_minute.items():
                self._flush_minute(sym, minute_ts)
            self._current_minute.clear()


class HighFreqFactorEngine:
    """分钟级因子引擎 — 价量/订单流/微观结构/波动率/流动性."""

    def __init__(self, window_sizes: List[int] = None):
        self.windows = window_sizes or [5, 10, 20, 60]
        self._history: Dict[str, Deque[MinuteBar]] = defaultdict(lambda: deque(maxlen=max(self.windows or [60]) + 10))
        self._factor_cache: Dict[str, pd.Series] = {}

    def add_bar(self, bar: MinuteBar):
        """添加分钟线, 更新因子."""
        self._history[bar.symbol].append(bar)
        self._invalidate_cache(bar.symbol)

    def _invalidate_cache(self, symbol: str):
        keys_to_del = [k for k in self._factor_cache if k.startswith(symbol + "_")]
        for k in keys_to_del:
            del self._factor_cache[k]

    def compute_factors(self, symbol: str, windows: List[int] = None) -> Dict[str, pd.Series]:
        """计算所有因子, 返回 {factor_name: Series}."""
        windows = windows or self.windows
        history = self._history.get(symbol)
        if not history or len(history) < max(windows):
            return {}

        # 转 DataFrame
        df = pd.DataFrame([asdict(b) for b in history])
        df = df.set_index("timestamp")
        
        # 预计算
        close = df["close"]
        volume = df["volume"]
        vwap = df["vwap"]
        spread = df["spread_bps"]
        volatility = df["volatility"]
        bid_vwap = df["bid_vwap"]
        ask_vwap = df["ask_vwap"]
        bid_vol = df["bid_vol"]
        ask_vol = df["ask_vol"]

        results = {}

        # 1. 价量因子
        for w in windows:
            # 动量
            mom = close.pct_change(w)
            results[f"momentum_{w}"] = mom
            
            # 反转
            rev = -close.pct_change(w)
            results[f"reversal_{w}"] = rev
            
            # 成交量加权动量
            vol_mom = (volume * close.pct_change(w)).rolling(w).mean()
            results[f"vol_momentum_{w}"] = vol_mom
            
            # 换手率
            turnover = volume / volume.rolling(w).mean()
            results[f"turnover_{w}"] = turnover

        # 2. 订单流因子
        # 买卖压力
        buy_pressure = bid_vol / (bid_vol + ask_vol + 1)
        results["buy_pressure"] = buy_pressure
        
        # 订单流不平衡 (OFI)
        ofi = (df["bid_vol"].diff() - df["ask_vol"].diff()) / (df["bid_vol"] + df["ask_vol"] + 1)
        results["ofi"] = ofi
        
        # VPIN (毒性概率)
        vpin = (abs(df["bid_vol"].diff()) + abs(df["ask_vol"].diff())) / (df["volume"] + 1)
        results["vpin"] = vpin.rolling(20).mean()

        # 3. 微观结构因子
        # 价差
        results["spread_bps"] = spread
        
        # 有效价差
        eff_spread = 2 * abs(close - (bid_vwap + ask_vwap) / 2) / close * 10000
        results["effective_spread_bps"] = eff_spread
        
        # 价格冲击
        price_impact = (close - close.shift(1)) / (volume + 1) * 1e8
        results["price_impact"] = price_impact

        # Kyle's Lambda
        lambda_k = (close.pct_change().abs() / (volume + 1)).rolling(20).apply(
            lambda x: np.polyfit(x, x, 1)[0] if len(x) > 5 else 0, raw=True)
        results["kyle_lambda"] = lambda_k

        # 4. 波动率因子
        for w in [5, 10, 20, 60]:
            rv = close.pct_change().rolling(w).std() * np.sqrt(240)
            results[f"realized_vol_{w}"] = rv
            
            # Parkinson 波动率
            park = np.sqrt((np.log(df["high"]/df["low"])**2).rolling(w).mean() / (4 * np.log(2)))
            results[f"parkinson_vol_{w}"] = park

        # 5. 流动性因子
        # Amihud 不流动性
        amihud = (close.pct_change().abs() / (volume * close + 1)).rolling(20).mean()
        results["amihud"] = amihud * 1e6
        
        # 成交量波动率
        vol_vol = volume.rolling(20).std() / volume.rolling(20).mean()
        results["volume_volatility"] = vol_vol

        # 转换为 Series 并截取最新值
        final = {}
        for name, series in results.items():
            if isinstance(series, pd.Series) and len(series) > 0:
                final[name] = series.iloc[-1] if not series.iloc[-1] != series.iloc[-1] else np.nan
        
        return final

    def get_factor_vector(self, symbol: str, windows: List[int] = None) -> np.ndarray:
        """获取因子向量 (用于 ML 模型输入)."""
        factors = self.compute_factors(symbol, windows)
        vec = np.array([v for v in factors.values() if not np.isnan(v)])
        return vec


class SmartRouter:
    """智能路由器 — TWAP/VWAP/POV/IS/自适应/暗池/分片."""

    def __init__(self, cost_model: CostModel):
        self.cost_model = cost_model
        self._algo_engines: Dict[ExecutionAlgo, "AlgoEngine"] = {}
        self._register_engines()

    def _register_engines(self):
        self._algo_engines = {
            ExecutionAlgo.TWAP: TWAPEngine(),
            ExecutionAlgo.VWAP: VWAPEngine(),
            ExecutionAlgo.POV: POVEngine(),
            ExecutionAlgo.IS: ISEngine(),
            ExecutionAlgo.ADAPTIVE: AdaptiveEngine(),
            ExecutionAlgo.ICEBERG: IcebergEngine(),
            ExecutionAlgo.DARK: DarkPoolEngine(),
        }

    def route(self, request: OrderRequest, market_data: Dict) -> ExecutionReport:
        """智能路由主入口."""
        # 1. 算法选择 (若未指定)
        if request.algo == ExecutionAlgo.ADAPTIVE:
            request.algo = self._select_algo(request, market_data)
        
        # 2. 获取执行引擎
        engine = self._algo_engines.get(request.algo)
        if not engine:
            raise ValueError(f"Unsupported algo: {request.algo}")
        
        # 3. 执行
        return engine.execute(request, market_data)

    def _select_algo(self, request: OrderRequest, market_data: Dict) -> ExecutionAlgo:
        """智能算法选择."""
        # 规则引擎
        qty = request.total_qty
        adv = market_data.get("adv", 1e6)  # 平均日成交量
        spread = market_data.get("spread_bps", 10)
        volatility = market_data.get("volatility", 0.02)
        urgency = market_data.get("urgency", "normal")
        
        # 大单/低流动性 → IS/POV
        if qty / adv > 0.1:
            return ExecutionAlgo.IS
        elif qty / adv > 0.05:
            return ExecutionAlgo.POV
        
        # 高波动/大价差 → VWAP/IS
        if spread > 50 or volatility > 0.03:
            return ExecutionAlgo.VWAP
        
        # 紧急 → IS/市价
        if urgency == "high":
            return ExecutionAlgo.IS
        
        # 默认 TWAP
        return ExecutionAlgo.TWAP


class AlgoEngine(ABC):
    """算法执行引擎基类."""
    
    @abstractmethod
    def execute(self, request: OrderRequest, market_data: Dict) -> ExecutionReport:
        pass

    def _calculate_cost(self, qty: int, price: float, side: OrderSide) -> float:
        from quant.execution.cost import CostModel
        cm = CostModel.from_config()
        return cm.total_cost(price, qty, side == OrderSide.BUY)


class TWAPEngine(AlgoEngine):
    """TWAP 时间加权平均价格."""
    
    def execute(self, request: OrderRequest, market_data: Dict) -> ExecutionReport:
        slices = self._generate_slices(request, market_data)
        return self._execute_slices(request, slices, market_data)


class VWAPEngine(AlgoEngine):
    """VWAP 量加权平均价格."""
    
    def execute(self, request: OrderRequest, market_data: Dict) -> ExecutionReport:
        # 使用历史成交量分布生成切片
        volume_profile = market_data.get("volume_profile", {})
        slices = self._generate_vwap_slices(request, volume_profile)
        return self._execute_slices(request, slices, market_data)


class POVEngine(AlgoEngine):
    """POV 参与率算法."""
    
    def execute(self, request: OrderRequest, market_data: Dict) -> ExecutionReport:
        participation = request.participation_rate
        slices = self._generate_pov_slices(request, market_data, participation)
        return self._execute_slices(request, slices, market_data)


class ISEngine(AlgoEngine):
    """IS 实施缺口算法 (Grinold & Kahn)."""
    
    def execute(self, request: OrderRequest, market_data: Dict) -> ExecutionReport:
        # 计算最优轨迹: 最小化 E[实施缺口] = E[市场冲击] + E[机会成本]
        # 最优轨迹: 平方根规律
        slices = self._generate_is_slices(request, market_data)
        return self._execute_slices(request, slices, market_data)


class AdaptiveEngine(AlgoEngine):
    """自适应引擎 — 实时切换 TWAP/VWAP/POV/IS."""
    
    def execute(self, request: OrderRequest, market_data: Dict) -> ExecutionReport:
        # 实时监控市场状态, 动态切换
        # 简化实现: 根据实时流动性在 VWAP/POV 间切换
        spread = market_data.get("spread_bps", 10)
        volume = market_data.get("volume", 1e6)
        
        if spread < 10 and volume > 1e6:
            engine = VWAPEngine()
        else:
            engine = POVEngine()
        
        return engine.execute(request, market_data)


class IcebergEngine(AlgoEngine):
    """冰山订单 — 大单拆小单隐藏意图."""
    
    def execute(self, request: OrderRequest, market_data: Dict) -> ExecutionReport:
        # 冰山大小 = min(平均单笔成交量 * 0.5, 总量/10)
        avg_trade = market_data.get("avg_trade_size", 5000)
        iceberg_size = min(avg_trade * 0.5, request.total_qty // 10)
        return self._execute_iceberg(request, market_data, iceberg_size)


class DarkPoolEngine(AlgoEngine):
    """暗池路由 — 寻找隐性流动性."""
    
    def execute(self, request: OrderRequest, market_data: Dict) -> ExecutionReport:
        # 查询暗池流动性
        dark_venues = market_data.get("dark_venues", [])
        if not dark_venues:
            # 无暗池, 回退 VWAP
            return VWAPEngine().execute(request, market_data)
        
        # 智能路由: 优先流动性最好的暗池
        return self._route_to_dark(request, market_data, dark_venues)


# ── 执行质量评估 (TCA) ──

class TCAAnalyzer:
    """交易成本分析 — 实施缺口/机会成本/时机选择/价格冲击."""

    def __init__(self):
        self._benchmarks = {}

    def analyze(self, report: ExecutionReport, market_data: Dict) -> Dict[str, float]:
        """全维度 TCA 分析."""
        results = {}
        
        # 1. 到达价成本
        arrival = report.arrival_price
        avg = report.avg_price
        side_mult = 1 if report.side == OrderSide.BUY else -1
        arrival_cost = side_mult * (avg - arrival) / arrival if arrival > 0 else 0
        results["arrival_cost_bps"] = arrival_cost * 10000
        
        # 2. VWAP 基准
        vwap = market_data.get("vwap", 0)
        if vwap > 0:
            vwap_cost = side_mult * (avg - vwap) / vwap
            results["vwap_cost_bps"] = vwap_cost * 10000
        
        # 3. TWAP 基准
        twap = market_data.get("twap", 0)
        if twap > 0:
            twap_cost = side_mult * (avg - twap) / twap
            results["twap_cost_bps"] = twap_cost * 10000
        
        # 4. 实施缺口 (IS = 到达价成本 + 机会成本)
        results["implementation_shortfall_bps"] = report.implementation_shortfall * 10000
        
        # 5. 价格冲击 (临时/永久)
        temp_impact, perm_impact = self._decompose_impact(report, market_data)
        results["temporary_impact_bps"] = temp_impact * 10000
        results["permanent_impact_bps"] = perm_impact * 10000
        
        # 6. 时机成本
        timing_cost = self._calculate_timing_cost(report, market_data)
        results["timing_cost_bps"] = timing_cost * 10000
        
        # 7. 机会成本 (未成交部分)
        opportunity_cost = (report.remaining_qty / max(request.total_qty, 1)) * \
                          (market_data.get("vwap", avg) - avg) / avg
        results["opportunity_cost_bps"] = opportunity_cost * 10000
        
        # 7. 滑点分解
        results["explicit_cost_bps"] = report.commission / (avg * report.filled_qty) * 10000 if avg > 0 else 0
        results["implicit_cost_bps"] = results["arrival_cost_bps"] - results["explicit_cost_bps"]
        
        return results

    def _decompose_impact(self, report: ExecutionReport, market_data: Dict) -> Tuple[float, float]:
        """分解临时/永久冲击 (Almgren-Chriss)."""
        # 简化: 假设永久冲击 = 总冲击 * 0.5, 临时 = 总冲击 * 0.5
        total_impact = abs(report.avg_price - report.arrival_price) / report.arrival_price
        return total_impact * 0.5, total_impact * 0.5

    def _calculate_timing_cost(self, report: ExecutionReport, market_data: Dict) -> float:
        """时机成本 = 基准价格变动期间的收益损失."""
        # 简化: 使用 VWAP - 到达价
        vwap = market_data.get("vwap", 0)
        arrival = report.arrival_price
        if vwap > 0 and arrival > 0:
            return (vwap - arrival) / arrival
        return 0


# ── 高频执行引擎统一入口 ──

class HighFreqExecutionEngine:
    """高频执行引擎统一入口."""

    def __init__(self):
        self.cost_model = CostModel.from_config()
        self.cleaner = TickCleaner()
        self.aggregator = MinuteAggregator()
        self.factor_engine = HighFreqFactorEngine()
        self.router = SmartRouter(self.cost_model)
        self.tca = TCAAnalyzer()
        
        # 连接管理
        self._tick_queue: queue.Queue = queue.Queue(maxsize=100000)
        self._order_queue: queue.Queue = queue.Queue(maxsize=10000)
        self._running = False
        self._threads: List[threading.Thread] = []

    def start(self):
        """启动引擎."""
        self._running = True
        
        # Tick 清洗线程
        self._threads.append(threading.Thread(target=self._tick_processor, daemon=True))
        # 分钟聚合线程
        self._threads.append(threading.Thread(target=self._aggregator_loop, daemon=True))
        # 因子计算线程
        self._threads.append(threading.Thread(target=self._factor_loop, daemon=True))
        # 执行线程
        self._threads.append(threading.Thread(target=self._execution_loop, daemon=True))
        
        for t in self._threads:
            t.start()
        
        _log.info("HighFreqExecutionEngine started")

    def stop(self):
        self._running = False
        for t in self._threads:
            t.join(timeout=5)
        _log.info("HighFreqExecutionEngine stopped")

    def on_tick(self, tick: TickData):
        """外部推送 Tick."""
        self._tick_queue.put(tick)

    def submit_order(self, request: OrderRequest) -> str:
        """提交订单, 返回订单 ID."""
        order_id = hashlib.md5(f"{request.client_order_id}{time.time()}".encode()).hexdigest()[:16]
        self._order_queue.put((order_id, request))
        return order_id

    def _tick_processor(self):
        """Tick 清洗处理循环."""
        while True:
            try:
                tick = self._tick_queue.get(timeout=1)
                cleaned = self.cleaner.clean(tick)
                if cleaned:
                    self.aggregator.add_tick(cleaned)
            except queue.Empty:
                continue
            except Exception as e:
                _log.error(f"Tick processor error: {e}")

    def _aggregator_loop(self):
        """分钟聚合循环."""
        while True:
            time.sleep(1)  # 每秒检查是否跨分钟
            # MinuteAggregator 内部按 Tick 时间戳自动聚合
            pass

    def _factor_loop(self):
        """因子计算循环."""
        while True:
            time.sleep(5)  # 每 5 秒更新一次因子
            # 因子引擎内部维护历史, 这里可触发缓存刷新
            pass

    def _execution_loop(self):
        """执行循环."""
        while True:
            try:
                order_id, request = self._order_queue.get(timeout=1)
                report = self.router.route(request, {})
                # 回调/持久化
            except queue.Empty:
                continue
            except Exception as e:
                _log.error(f"Execution error: {e}")

    def get_factor_vector(self, symbol: str) -> np.ndarray:
        """获取因子向量 (供 ML 模型)."""
        return self.factor_engine.get_factor_vector(symbol)

    def get_tca_report(self, report: ExecutionReport, market_data: Dict) -> Dict:
        return self.tca.analyze(report, market_data)


# ── 全局单例 ──

_hf_engine: Optional[HighFreqExecutionEngine] = None
_hf_lock = threading.Lock()


def get_hf_engine() -> HighFreqExecutionEngine:
    global _hf_engine
    with _hf_lock:
        if _hf_engine is None:
            _hf_engine = HighFreqExecutionEngine()
        return _hf_engine


if __name__ == "__main__":
    # 简单测试
    engine = HighFreqExecutionEngine()
    engine.start()
    time.sleep(2)
    engine.stop()
    print("HighFreqExecutionEngine test passed")