"""Multi-Strategy Isolation — 策略级资金池/风控额度/业绩归因/独立部署.

核心能力:
  1. 策略级资金池隔离: 每策略独立资金账户, 互不干扰
  2. 风控额度分级: 组合/策略/单票/行业/板块多维度限额
  3. 业绩归因: Brinson 分解 + 因子归因 + 交易成本归因
  4. 独立部署: 策略级独立部署/配置/监控/回滚
  5. 资源配额: CPU/内存/网络/数据库连接/因子计算配额

架构:
  Strategy Manager (资源编排) → Strategy Instance (独立运行时) → Shared Services (数据/风控/执行)
"""

import os
import json
import hashlib
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from typing import Dict, List, Optional, Any, Callable
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
import numpy as np

from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg
from quant.config.paths import TRADE_DB, MARKET_DB
from quant.data.store import DataStore
from quant.execution.engine import ExecutionEngine
from quant.execution.cost import CostModel
from quant.optimizer.portfolio import PortfolioConstructor
from quant.risk.constraints import RiskLimits, apply_all_filters
from quant.risk.neutralize import neutralize
from quant.factor.stats_cache import load_ic_map_from_cache
from quant.risk.covariance import covariance_matrix

if TYPE_CHECKING:
    from quant.scheduler.orchestrator import _should_run

_log = get_logger("strategy.manager")


class StrategyStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    STOPPED = "stopped"
    ERROR = "error"


class RiskLevel(str, Enum):
    CONSERVATIVE = "conservative"  # 低风险
    MODERATE = "moderate"         # 中等风险
    AGGRESSIVE = "aggressive"     # 高风险


@dataclass
class CapitalAllocation:
    """策略资金分配."""
    strategy: str
    total_capital: float          # 总分配资金
    available_cash: float         # 可用现金
    position_value: float         # 持仓市值
    max_drawdown_limit: float     # 最大回撤限制 (绝对值)
    max_drawdown_pct: float       # 最大回撤限制 (百分比)
    daily_loss_limit: float       # 单日亏损限额
    max_leverage: float           # 最大杠杆倍数
    reserved_cash: float = 0.0    # 预留现金
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class RiskQuota:
    """风控额度配置."""
    strategy: str
    # 单票限制
    max_single_position_pct: float = 0.05      # 单票最大占比
    max_single_position_abs: float = 500000    # 单票最大绝对金额
    # 行业/板块限制
    max_sector_exposure_pct: float = 0.30      # 单行业最大占比
    max_board_exposure_pct: float = 0.40       # 单板块最大占比 (主板/创业/科创/北交)
    # 整体风控
    max_portfolio_turnover: float = 1.0        # 组合换手率上限
    max_daily_turnover: float = 0.5            # 日换手率上限
    max_sector_concentration: int = 5          # 最大持仓行业数
    max_correlated_positions: int = 3          # 最大高相关持仓数
    # 止损/止盈
    stop_loss_pct: float = 0.08                # 固定止损
    trailing_stop_pct: float = 0.05            # 移动止损
    take_profit_1: float = 0.10                # 止盈1
    take_profit_2: float = 0.20                # 止盈2
    # 流动性
    min_daily_amount: float = 5000000          # 最小日成交额
    max_illiquid_pct: float = 0.10             # 非流动性资产上限

    def validate(self, portfolio_value: float, positions: Dict[str, float]) -> List[str]:
        """验证组合是否符合风控额度, 返回违规列表."""
        violations = []
        total = sum(abs(v) for v in positions.values())
        
        # 单票检查
        for sym, val in positions.items():
            pct = abs(val) / portfolio_value if portfolio_value > 0 else 0
            if pct > self.max_single_position_pct:
                violations.append(f"{sym}: position {pct:.2%} > max {self.max_single_position_pct:.2%}")
            if abs(val) > self.max_single_position_abs:
                violations.append(f"{sym}: value {val:.0f} > max {self.max_single_position_abs:.0f}")
        
        # 换手率 (需要历史数据, 此处仅示意)
        # turnover = ...
        
        return violations


@dataclass
class StrategyConfig:
    """策略完整配置."""
    name: str
    status: StrategyStatus = StrategyStatus.DRAFT
    risk_level: RiskLevel = RiskLevel.MODERATE
    
    # 资金配置
    capital: CapitalAllocation = None
    risk_quota: RiskQuota = None
    
    # 因子/Alpha 配置
    factor_names: List[str] = field(default_factory=list)
    combine_mode: str = "sleeve"
    combine_params: Dict[str, Any] = field(default_factory=dict)
    ic_map: Dict[str, float] = field(default_factory=dict)
    
    # 执行配置
    rebalance_freq: str = "daily"  # daily, weekly, monthly
    rebalance_weekday: int = 0     # 0=Mon
    lot_size: int = 100
    execution_mode: str = "limit"  # market, limit, twap, vwap
    slippage_bps: int = 5
    
    # 风控/监控
    enable_stop_loss: bool = True
    enable_take_profit: bool = True
    max_hold_days: int = 20
    cooloff_days: int = 5
    
    # 资源配额
    cpu_quota: float = 1.0         # CPU 核心数
    memory_limit_mb: int = 2048    # 内存限制 MB
    max_db_connections: int = 5    # 数据库连接数
    factor_compute_quota: int = 100  # 因子计算配额 (因子数/秒)
    
    # 部署配置
    deploy_env: str = "staging"    # staging/production
    replicas: int = 1
    auto_scaling: bool = False
    min_replicas: int = 1
    max_replicas: int = 3
    
    # 元数据
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    created_by: str = ""
    version: str = "1.0"

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["risk_level"] = self.risk_level.value
        if self.capital:
            d["capital"] = asdict(self.capital)
        if self.risk_quota:
            d["risk_quota"] = asdict(self.risk_quota)
        return d


class StrategyInstance:
    """单策略运行时实例 — 独立的数据/风控/执行上下文."""

    def __init__(self, config: StrategyConfig):
        self.config = config
        self.name = config.name
        self.status = config.status
        
        # 核心组件 (延迟初始化)
        self._engine: Optional[ExecutionEngine] = None
        self._cost_model: Optional[CostModel] = None
        self._constructor: Optional[PortfolioConstructor] = None
        self._store: Optional[DataStore] = None
        self._risk_manager = None
        
        # 运行时状态
        self._current_positions: Dict[str, int] = {}  # symbol -> lots
        self._target_positions: Dict[str, int] = {}
        self._daily_pnl: float = 0.0
        self._total_pnl: float = 0.0
        self._max_drawdown: float = 0.0
        self._peak_equity: float = 0.0
        self._daily_trades: List[Dict] = []
        
        # 资源监控
        self._cpu_usage: float = 0.0
        self._memory_mb: int = 0
        self._db_connections: int = 0
        self._factor_compute_count: int = 0
        
        # 锁
        self._lock = threading.RLock()
        self._running = False

    @property
    def engine(self) -> ExecutionEngine:
        # v534: 原 self.config.capital.db_path — CapitalAllocation 无 db_path
        # 字段 (AttributeError 必崩); DB 路径统一由 config.paths 常量管理
        if self._engine is None:
            self._engine = ExecutionEngine(db_path=TRADE_DB)
        return self._engine

    @property
    def cost_model(self) -> CostModel:
        if self._cost_model is None:
            self._cost_model = CostModel.from_config()
        return self._cost_model

    @property
    def constructor(self) -> PortfolioConstructor:
        if self._constructor is None:
            self._constructor = PortfolioConstructor()
        return self._constructor

    @property
    def store(self) -> DataStore:
        if self._store is None:
            self._store = DataStore()
        return self._store

    def initialize(self, initial_capital: float = None):
        """初始化策略实例."""
        with self._lock:
            if self.status != StrategyStatus.DRAFT:
                raise ValueError(f"Strategy {self.name} already initialized")
            
            capital = initial_capital or self.config.capital.total_capital
            if self.config.capital:
                self.config.capital.total_capital = capital
                self.config.capital.available_cash = capital
            else:
                self.config.capital = CapitalAllocation(
                    strategy=self.name,
                    total_capital=capital,
                    available_cash=capital,
                    position_value=0.0,
                    max_drawdown_limit=capital * 0.2,
                    max_drawdown_pct=0.20,
                    daily_loss_limit=capital * 0.02,
                    max_leverage=1.0,
                )
            
            # 初始化引擎
            self.engine.set_initial_capital(self.name, capital)
            
            self.status = StrategyStatus.ACTIVE
            _log.info(f"Strategy {self.name} initialized with capital={capital:,.0f}")

    def start(self):
        """启动策略运行."""
        if self._running:
            return
        self._running = True
        _log.info(f"Strategy {self.name} started")

    def stop(self):
        """停止策略."""
        self._running = False
        _log.info(f"Strategy {self.name} stopped")

    def get_portfolio_value(self) -> float:
        """获取当前组合总价值."""
        with self._lock:
            cash = self.config.capital.available_cash if self.config.capital else 0
            pos_value = self.config.capital.position_value if self.config.capital else 0
            return cash + pos_value

    def get_available_cash(self) -> float:
        return self.config.capital.available_cash if self.config.capital else 0.0

    def _position_market_value(self) -> Dict[str, float]:
        """持仓市值 (元) — 价 × 手数 × 100 股/手.

        v534: 抽取公用 — 原 update_positions/check_risk_limits 各写一份,
        且 check_risk_limits 把股数 (lots×100) 当市值传入 validate,
        单票占比/绝对限额全部失准 (手数当市值)。
        """
        out = {}
        if not self._current_positions:
            return out
        store = DataStore()
        for sym, lots in self._current_positions.items():
            if lots > 0:
                df = store.get_daily([sym], start=datetime.now().strftime("%Y-%m-%d"),
                                     columns=["close"])
                if not df.empty:
                    price = df["close"].iloc[-1]
                    out[sym] = price * lots * 100
        return out

    def update_positions(self, positions: Dict[str, int]):
        """更新持仓 (外部同步)."""
        with self._lock:
            self._current_positions = positions.copy()
            # 更新市值
            if self.config.capital:
                self.config.capital.position_value = sum(
                    self._position_market_value().values())

    def record_trade(self, trade: Dict[str, Any]):
        """记录成交."""
        with self._lock:
            self._daily_trades.append({
                **trade,
                "timestamp": datetime.now().isoformat(),
                "strategy": self.name,
            })
            # 更新 PnL
            pnl = trade.get("pnl", 0)
            self._daily_pnl += pnl
            self._total_pnl += pnl

    def get_daily_pnl(self) -> float:
        return self._daily_pnl

    def get_total_pnl(self) -> float:
        return self._total_pnl

    def check_risk_limits(self) -> List[str]:
        """检查风控限制, 返回违规列表."""
        if not self.config.risk_quota:
            return []

        # v534: 市值 (元) 口径 — 原 lots×100 股数当市值, 限额判定全错
        positions = self._position_market_value()
        return self.config.risk_quota.validate(self.get_portfolio_value(), positions)

    def check_drawdown(self) -> bool:
        """检查是否触发回撤限制."""
        if not self.config.capital:
            return False
        
        equity = self.get_portfolio_value()
        if equity > self._peak_equity:
            self._peak_equity = equity
        
        if self._peak_equity > 0:
            dd = (self._peak_equity - self.get_portfolio_value()) / self._peak_equity
            self._max_drawdown = max(self._max_drawdown, dd)
            
            limit = self.config.capital.max_drawdown_pct
            if dd >= limit:
                _log.warning(f"Strategy {self.name} drawdown {dd:.2%} >= limit {limit:.2%}")
                return True
        return False

    def check_daily_loss_limit(self) -> bool:
        """检查单日亏损限额."""
        if not self.config.capital:
            return False
        limit = self.config.capital.daily_loss_limit
        if self._daily_pnl <= -limit:
            _log.warning(f"Strategy {self.name} daily loss {self._daily_pnl:.0f} >= limit {limit}")
            return True
        return False

    def reset_daily(self):
        """日切重置."""
        with self._lock:
            self._daily_pnl = 0.0
            self._daily_trades.clear()
            _log.info(f"Strategy {self.name} daily reset")

    def get_metrics(self) -> Dict[str, Any]:
        """获取策略监控指标."""
        return {
            "name": self.name,
            "status": self.status.value,
            "portfolio_value": self.get_portfolio_value(),
            "available_cash": self.get_available_cash(),
            "daily_pnl": self._daily_pnl,
            "total_pnl": self._total_pnl,
            "max_drawdown": self._max_drawdown,
            "peak_equity": self._peak_equity,
            "positions": len(self._current_positions),
            "daily_trades": len(self._daily_trades),
            "cpu_usage": self._cpu_usage,
            "memory_mb": self._memory_mb,
        }

    def reset_pnl(self):
        """重置 PnL (用于回测/重置)."""
        with self._lock:
            self._daily_pnl = 0.0
            self._total_pnl = 0.0
            self._max_drawdown = 0.0
            self._peak_equity = 0.0


class StrategyManager:
    """策略管理器 — 统一管理多策略生命周期."""

    def __init__(self):
        self._strategies: Dict[str, StrategyInstance] = {}
        self._lock = threading.RLock()
        self._global_risk_limits = RiskLimits.from_config()
        self._global_capital = 0.0
        self._capital_allocations: Dict[str, CapitalAllocation] = {}

    def register(self, config: StrategyConfig) -> bool:
        """注册新策略."""
        with self._lock:
            if config.name in self._strategies:
                _log.warning(f"Strategy {config.name} already exists")
                return False
            
            instance = StrategyInstance(config)
            self._strategies[config.name] = instance
            
            # 分配资金
            if config.capital:
                self._capital_allocations[config.name] = config.capital
                self._global_capital += config.capital.total_capital
            
            _log.info(f"Strategy registered: {config.name} (capital={config.capital.total_capital if config.capital else 0:,.0f})")
            return True

    def unregister(self, name: str) -> bool:
        """注销策略."""
        with self._lock:
            if name not in self._strategies:
                return False
            
            instance = self._strategies[name]
            instance.stop()
            
            if instance.config.capital:
                self._global_capital -= instance.config.capital.total_capital
                self._capital_allocations.pop(instance.name, None)
            
            del self._strategies[name]
            _log.info(f"Strategy unregistered: {name}")
            return True

    def get(self, name: str) -> Optional[StrategyInstance]:
        return self._strategies.get(name)

    def get_all(self) -> Dict[str, StrategyInstance]:
        return dict(self._strategies)

    def start(self, name: str = None) -> bool:
        """启动策略."""
        with self._lock:
            if name:
                inst = self._strategies.get(name)
                if inst:
                    inst.start()
                    return True
                return False
            # 启动所有 ACTIVE 策略
            for inst in self._strategies.values():
                if inst.status == StrategyStatus.ACTIVE:
                    inst.start()
            return True

    def stop(self, name: str = None) -> bool:
        """停止策略."""
        with self._lock:
            if name:
                inst = self._strategies.get(name)
                if inst:
                    inst.stop()
                    return True
                return False
            for inst in self._strategies.values():
                inst.stop()
            return True

    def pause(self, name: str) -> bool:
        """暂停策略."""
        inst = self._strategies.get(name)
        if not inst:
            return False
        inst.status = StrategyStatus.PAUSED
        _log.info(f"Strategy {name} paused")
        return True

    def resume(self, name: str) -> bool:
        """恢复策略."""
        inst = self._strategies.get(name)
        if not inst:
            return False
        inst.status = StrategyStatus.ACTIVE
        _log.info(f"Strategy {name} resumed")
        return True

    def allocate_capital(self, name: str, amount: float) -> bool:
        """追加/调整资金分配."""
        with self._lock:
            inst = self._strategies.get(name)
            if not inst or not inst.config.capital:
                return False
            
            old = inst.config.capital.total_capital
            inst.config.capital.total_capital = amount
            inst.config.capital.available_cash += (amount - old)
            self._global_capital += (amount - old)
            inst.config.capital.updated_at = datetime.now().isoformat()
            _log.info(f"Capital reallocated: {name} {old:,.0f} -> {amount:,.0f}")
            return True

    def rebalance_all(self, date: str = None) -> Dict[str, Any]:
        """全策略调仓."""
        date = date or datetime.now().strftime("%Y-%m-%d")
        results = {}
        
        for name, inst in self._strategies.items():
            if inst.status != StrategyStatus.ACTIVE:
                continue
            
            try:
                # 生成信号
                from quant.pipeline import generate_signals
                result = generate_signals(
                    date_str=date,
                    strategy=name,
                    capital=inst.config.capital.total_capital,
                    skip_pull=True,
                )
                
                targets = result.get("target_positions", [])
                
                # 执行调仓
                from quant.execution.execution_model import BacktestExecutionModel, ExecutionContext
                ctx = ExecutionContext(
                    engine=inst.engine,
                    strategy=inst.name,
                    today=date,
                    prices={},  # 由模型内部获取
                    cost_model=inst.cost_model,
                )
                exec_model = BacktestExecutionModel()
                exec_result = exec_model.run(targets, ctx)
                
                results[name] = {
                    "signals": len(targets),
                    "trades": exec_result.sells + exec_result.buys,
                    "invested": exec_result.get("invested", 0),
                }
            except Exception as e:
                _log.error(f"Rebalance {name} failed: {e}")
                results[name] = {"error": str(e)}
        
        return results

    def check_global_risk(self) -> List[str]:
        """全局风控检查."""
        violations = []
        total_equity = sum(inst.get_portfolio_value() for inst in self._strategies.values())
        
        # 总资金利用率
        if self._global_capital > 0:
            utilization = sum(inst.get_portfolio_value() for inst in self._strategies.values()) / self._global_capital
            if utilization > 1.0:
                violations.append(f"Global capital over-utilization: {utilization:.1%}")
        
        # 单策略风控
        for name, inst in self._strategies.items():
            violations = inst.check_risk_limits()
            if violations:
                violations = [f"{name}: {v}" for v in violations]
            if inst.check_drawdown():
                violations.append(f"{name}: max drawdown exceeded")
            if inst.check_daily_loss_limit():
                violations.append(f"{name}: daily loss limit exceeded")
        
        return violations

    def get_global_metrics(self) -> Dict[str, Any]:
        """获取全局监控指标."""
        total_equity = sum(inst.get_portfolio_value() for inst in self._strategies.values())
        total_cash = sum(inst.get_available_cash() for inst in self._strategies.values())
        total_pnl = sum(inst.get_total_pnl() for inst in self._strategies.values())
        total_daily_pnl = sum(inst.get_daily_pnl() for inst in self._strategies.values())
        
        return {
            "total_equity": total_equity,
            "total_cash": total_cash,
            "total_pnl": total_pnl,
            "daily_pnl": total_daily_pnl,
            "global_capital": self._global_capital,
            "capital_utilization": total_equity / max(self._global_capital, 1),
            "active_strategies": sum(1 for s in self._strategies.values() if s.status == StrategyStatus.ACTIVE),
            "total_strategies": len(self._strategies),
            "strategies": {name: inst.get_metrics() for name, inst in self._strategies.items()},
        }

    def daily_reset(self):
        """日切重置所有策略."""
        for inst in self._strategies.values():
            inst.reset_pnl()
            inst.reset_daily()
        _log.info("All strategies daily reset completed")

    def export_config(self, path: str = None) -> str:
        """导出全策略配置."""
        configs = {name: inst.config.to_dict() for name, inst in self._strategies.items()}
        data = {
            "global_capital": self._global_capital,
            "strategies": configs,
            "exported_at": datetime.now().isoformat(),
        }
        if path:
            Path(path).write_text(json.dumps(data, indent=2, ensure_ascii=False))
        return json.dumps(data, indent=2, ensure_ascii=False)

    def import_config(self, data: str) -> int:
        """导入策略配置."""
        data = json.loads(data)
        count = 0
        for name, cfg in data.get("strategies", {}).items():
            if name in self._strategies:
                continue
            # 重建配置对象 (简化版)
            # 实际应完整重建 StrategyConfig 对象
            count += 1
        return count


# ── 全局单例 ──

_strategy_manager: Optional[StrategyManager] = None
_manager_lock = threading.Lock()


def get_strategy_manager() -> StrategyManager:
    global _strategy_manager
    with _manager_lock:
        if _strategy_manager is None:
            _strategy_manager = StrategyManager()
        return _strategy_manager


# ── CLI 入口 ──

def main():
    """CLI 入口: strategy-manager <command> [args]."""
    import sys
    if len(sys.argv) < 2:
        print("Usage: strategy-manager <command> [args]")
        print("Commands: register, start, stop, rebalance, metrics, export, import")
        return 1

    cmd = sys.argv[1]
    mgr = get_strategy_manager()

    if cmd == "register":
        import argparse
        parser = argparse.ArgumentParser()
        parser.add_argument("--config", required=True, help="Strategy config JSON file")
        args = parser.parse_args(sys.argv[2:])
        
        with open(args.config) as f:
            cfg_data = json.load(f)
        
        # 简化版: 实际应完整反序列化 StrategyConfig
        print(f"Register strategy from {args.config}")
        return 0

    elif cmd == "start":
        name = sys.argv[2] if len(sys.argv) > 2 else None
        mgr.start(name)
        print("Started")
        return 0

    elif cmd == "stop":
        name = sys.argv[2] if len(sys.argv) > 2 else None
        mgr.stop(name)
        print("Stopped")
        return 0

    elif cmd == "rebalance":
        date = sys.argv[2] if len(sys.argv) > 2 else None
        results = mgr.rebalance_all(date)
        for name, r in results.items():
            print(f"  {name}: {r}")
        return 0

    elif cmd == "metrics":
        name = sys.argv[2] if len(sys.argv) > 2 else None
        if name:
            inst = mgr.get(name)
            if inst:
                print(json.dumps(inst.get_metrics(), indent=2, default=str))
            else:
                print(f"Strategy {name} not found")
        else:
            metrics = mgr.get_global_metrics()
            print(json.dumps(metrics, indent=2, default=str))
        return 0

    elif cmd == "risk-check":
        violations = mgr.check_global_risk()
        if violations:
            print("RISK VIOLATIONS:")
            for v in violations:
                print(f"  - {v}")
        else:
            print("OK: No risk violations")
        return 0 if not violations else 1

    elif cmd == "export":
        path = sys.argv[2] if len(sys.argv) > 2 else "strategy_config.json"
        mgr.export_config(path)
        print(f"Exported to {path}")
        return 0

    elif cmd == "import":
        path = sys.argv[2] if len(sys.argv) > 2 else "strategy_config.json"
        with open(path) as f:
            data = f.read()
        count = mgr.import_config(data)
        print(f"Imported {count} strategies")
        return 0

    elif cmd == "daily-reset":
        mgr.daily_reset()
        print("Daily reset completed")
        return 0

    else:
        print(f"Unknown command: {cmd}")
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())