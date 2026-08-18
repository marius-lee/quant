"""FactorStateMachine — 统一因子生命周期管理 (编译→评估→注册→状态迁移).

统合:
  - factor_curator.py: 编译、评估、注册逻辑
  - state_manager.py: 状态转换、重试计数、合法性校验

设计:
  - 单一类管理因子全生命周期: 编译 → 评估 → 注册 → 状态迁移
  - 零 fallback: 非法转换 → ValueError
  - 所有阈值从 config.yaml 读取
  - 状态转换表为纯数据结构，可单元测试
  - 编译/评估错误记录因子名+表达式，标记 compilation_failed

用法:
    fsm = FactorStateMachine()
    # 单因子编译评估注册
    result = fsm.compile_evaluate_register("turnover_accel")
    # 批量状态迁移
    fsm.batch_transition(["f1", "f2"], "EVAL_PASS", reason="Phase 2 passed")
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Literal

import numpy as np
import pandas as pd

from quant.config.constants import _require_cfg
from quant.utils.logger import get_logger
from quant.factor.compute.expr_compiler import compile_factor
from quant.factor.compute.price import _PRICE_FN_MAP
from quant.factor.compute.fundamental import _FUNDAMENTAL_FN_MAP
from quant.data.store import DataStore
from quant.data.repos import FactorRepo, UniverseRepo
from scipy.stats import spearmanr
# B36 (2026-08-18): curate_all 引用的 _CURATED_FACTORS 常量未导入 (NameError);
# 常量单一真相源仍在 factor_curator.py, 此处显式引用.
from quant.factor.factor_curator import _CURATED_FACTORS

_log = get_logger("factor.state_machine")


# ── 统一事件定义 ──
class FactorEvent:
    """因子生命周期事件 — 统一因子策展与状态机的事件定义。"""

    # 编译/评估阶段
    COMPILE_OK = "COMPILE_OK"
    COMPILE_FAIL = "COMPILE_FAIL"
    EVAL_OK = "EVAL_OK"
    EVAL_PASS = "EVAL_PASS"  # v519: phase5b 综合裁决 (p2+p3+p4 全 pass + DSR 显著) → active
    EVAL_MARGINAL = "EVAL_MARGINAL"
    EVAL_FAIL = "EVAL_FAIL"

    # 实盘归因
    IC_DEGRADED = "IC_DEGRADED"
    IC_RECOVERED = "IC_RECOVERED"
    IC_PERSISTENT = "IC_PERSISTENT"

    # 因子冗余 (相关性去重)
    FACTOR_REDUNDANT = "FACTOR_REDUNDANT"

    # 数据源异常
    DATA_SOURCE_DEAD = "DATA_SOURCE_DEAD"

    # 归档恢复
    RETRY_RESTORE = "RETRY_RESTORE"

    # 编译失败标记
    COMPILATION_FAILED = "COMPILATION_FAILED"


# ── 有效状态 ──
VALID_STATUSES = frozenset({
    "evaluating",   # 待评估 (原 candidate)
    "active",       # 通过完整评估+IC未衰减 → 实盘信号生成, 完整权重
    "probation",    # IC 衰减观察期 (原 monitoring), 实盘信号衰减权重
    "archived",     # 归档 (原 retired+rejected 合并), status_reason 区分原因
})

# ── 状态转换表 (合并策展事件 + 状态机事件) ──
_TRANSITIONS: dict[tuple[str, str], str] = {
    # 编译/评估阶段
    ("evaluating",  FactorEvent.COMPILE_OK): "evaluating",  # 编译通过，继续评估
    ("evaluating",  FactorEvent.COMPILE_FAIL): "archived",  # 编译失败 → 归档
    ("evaluating",  FactorEvent.EVAL_OK):       "active",
    ("evaluating",  FactorEvent.EVAL_PASS):     "active",  # v519: 修复 phase5b 死路径 — 完整评估通过即晋升
    ("evaluating",  FactorEvent.EVAL_MARGINAL): "probation",
    ("evaluating",  FactorEvent.EVAL_FAIL):      "archived",

    # 实盘归因
    ("active",      FactorEvent.IC_DEGRADED):     "probation",
    ("probation",   FactorEvent.IC_RECOVERED):    "active",
    ("probation",   FactorEvent.IC_PERSISTENT):   "archived",

    # 因子冗余 (相关性去重)
    ("active",      FactorEvent.FACTOR_REDUNDANT): "probation",
    ("probation",   FactorEvent.FACTOR_REDUNDANT): "archived",

    # 数据源异常
    ("active",      FactorEvent.DATA_SOURCE_DEAD): "archived",
    ("probation",   FactorEvent.DATA_SOURCE_DEAD): "archived",

    # 归档恢复
    ("archived",    FactorEvent.RETRY_RESTORE):    "evaluating",
}

# 所有合法事件
_VALID_EVENTS = frozenset(event for _, event in _TRANSITIONS.keys())

# 评估阶段专用事件
_EVAL_EVENTS = frozenset({
    FactorEvent.EVAL_OK,
    FactorEvent.EVAL_MARGINAL,
    FactorEvent.EVAL_FAIL,
})


class InvalidTransitionError(ValueError):
    """非法状态转换 — 零 fallback, 必须抛异常."""
    pass


@dataclass
class FactorCandidate:
    """待策展因子元数据。"""
    name: str
    expression: str
    source: str
    direction: str
    category: str
    # 编译后函数缓存
    _compiled_fn: Optional[callable] = None


class FactorStateMachine:
    """因子全生命周期状态机 — 编译 → 评估 → 注册 → 状态迁移。

    统合原 factor_curator.py (编译/评估/注册) + state_manager.py (状态迁移)。

    用法:
        fsm = FactorStateMachine()
        # 单因子编译评估注册
        result = fsm.compile_evaluate_register("turnover_accel")
        # 批量状态迁移
        fsm.batch_transition(["f1", "f2"], "EVAL_PASS", reason="Phase 2 passed")
    """

    def __init__(self):
        self._repo = FactorRepo()
        # 阈值来源: config.yaml (单一真相源)
        self._max_retries = _require_cfg("factor.evaluation.max_retries")
        self._ic_threshold = _require_cfg("factor.evaluation.ic_threshold")
        self._icir_threshold = _require_cfg("factor.evaluation.icir_threshold")
        self._half_life_threshold = _require_cfg("factor.evaluation.min_half_life")
        self._monitoring_ic = _require_cfg("factor.evaluation.monitoring_min_abs_ic")
        self._monitoring_icir = _require_cfg("factor.evaluation.monitoring_min_icir")
        self._compilation_failed_marker = "compilation_failed"

    # ── 静态方法: 事件/状态查询 (无副作用) ──

    @staticmethod
    def get_target(current: str, event: str) -> str:
        """返回转换后的目标状态 (不执行). 非法时抛 InvalidTransitionError."""
        target = _TRANSITIONS.get((current, event))
        if target is None:
            raise InvalidTransitionError(
                f"非法状态转换: {current} --({event})--> ? "
                f"(允许的事件: {[e for (s, e) in _TRANSITIONS if s == current]})"
            )
        return target

    @staticmethod
    def is_valid_event(event: str) -> bool:
        """检查事件名是否合法."""
        return event in _VALID_EVENTS

    @staticmethod
    def is_eval_event(event: str) -> bool:
        """检查是否为评估阶段事件."""
        return event in _EVAL_EVENTS

    # ── 状态查询 (无副作用) ──

    def get_status(self, name: str) -> str | None:
        """读取因子当前状态. 返回 None 表示因子不存在."""
        factor = self._repo.get_factor_by_name(name)
        return factor["status"] if factor else None

    def can_transition(self, current: str, event: str) -> bool:
        """检查 (current, event) 是否为合法转换."""
        return (current, event) in _TRANSITIONS

    # ── 编译/评估/注册 (核心业务逻辑) ──

    def compile_factor(self, name: str, expression: str) -> tuple[bool, Optional[callable], Optional[str]]:
        """编译因子表达式.

        Returns:
            (success, compiled_fn, error_msg)
        """
        try:
            # 优先检查内置函数映射
            if expression in _PRICE_FN_MAP:
                fn = _PRICE_FN_MAP[expression][0]
            elif expression in _FUNDAMENTAL_FN_MAP:
                fn = _FUNDAMENTAL_FN_MAP[expression][1]
            else:
                fn = compile_factor(expression)
            return True, fn, None
        except Exception as e:
            _log.error(f"factor compile: {expression} -> {e}")
            return False, None, str(e)

    def evaluate_factor(self, fn: callable, symbols: list[str], dates: list[str],
                       data_full: pd.DataFrame, fundamentals: pd.DataFrame) -> dict:
        """评估单个因子的 IC/ICIR/half-life.

        Returns:
            {mean_ic, icir, n_obs, half_life, verdict, ic_series}
        """
        from quant.factor.compute._dispatch import compute_all_factors
        from quant.factor.windows import max_factor_calendar_days

        ic_vals = []
        # B36: fwd 从未定义 (NameError). 从 data_full 计算 T+5 前向收益.
        fwd = data_full["close"].shift(-5) / data_full["close"] - 1
        for d in dates[:-5]:
            try:
                _d = pd.Timestamp(d)
                fv = fn(data_full.loc[:_d], d)
                fr = fwd.loc[d].dropna()
                common = fv.dropna().index.intersection(fr.index)
                if len(common) < 30:
                    continue
                ic, _ = spearmanr(fv[common], fr[common])
                if not np.isnan(ic):
                    ic_vals.append(ic)
            except Exception as _daily_err:
                _log.debug(f"daily eval failed: {_daily_err}")
                continue

        if len(ic_vals) < 10:
            return {"mean_ic": 0, "icir": 0, "n_obs": len(ic_vals),
                    "verdict": "insufficient", "ic_series": {}}

        mean_ic = np.mean(ic_vals)
        icir = mean_ic / np.std(ic_vals) if np.std(ic_vals) > 0 else 0

        # half-life: IC20/IC1 ≈ e^(-19/hl) -> hl = -19*ln(2)/ln(ratio)
        # P1-9 fix: half_life = -19*ln(2)/ln(ratio)
        # 简化: 这里不计算，由外层统一计算

        abs_ic = abs(mean_ic)
        abs_icir = abs(icir)

        verdict = "active"
        if abs_ic < self._ic_threshold or abs_icir < self._icir_threshold:
            verdict = "marginal"
        if abs_ic < self._ic_threshold * 0.5 or abs_icir < self._icir_threshold * 0.5:
            verdict = "failed"

        return {
            "mean_ic": mean_ic,
            "icir": icir,
            "n_obs": len(ic_vals),
            "verdict": verdict,
            "ic_series": {"ic_vals": ic_vals},
        }

    def register_factor(self, candidate: "FactorCandidate", verdict: str,
                        mean_ic: float, icir: float, n_obs: int) -> bool:
        """注册因子到 factor_registry."""
        try:
            status_map = {
                "active": "active",
                "marginal": "probation",
                "failed": "archived",
            }
            status = status_map.get(verdict, "archived")
            self._repo.register_factor(
                name=candidate.name,
                expression=candidate.expression,
                source=candidate.source,
                direction=candidate.direction,
                category=candidate.category,
                status=status,
            )
            return True
        except Exception as e:
            _log.error(f"register failed for {candidate.name}: {e}")
            return False

    def compile_evaluate_register(self, candidate: "FactorCandidate",
                                  n_symbols: int = 500, n_dates: int = 120) -> dict:
        """编译 → 评估 → 注册完整流程.

        Returns:
            {name, mean_ic, icir, n_obs, registered, verdict}
        """
        # 1. 编译
        success, fn, error = self.compile_factor(candidate.name, candidate.expression)
        if not success:
            # B36: candidate._error 属性不存在 (AttributeError) — compile_factor
            # 返回的 error 变量才是错误信息.
            return {**candidate.__dict__, "mean_ic": 0, "icir": 0, "n_obs": 0,
                    "registered": False, "verdict": "compilation_failed", "error": error}

        # 2. 准备评估数据
        store = DataStore()
        symbols = UniverseRepo().get_symbols(exclude_market='BJ')[:n_symbols]
        dates = [r[0] for r in store._connect().execute(
            "SELECT DISTINCT date FROM daily WHERE date >= date('now', ?) ORDER BY date DESC LIMIT ?",
            (f'-{120 * 3} days', 120)
        ).fetchall()]
        dates.sort()

        if len(dates) < 20:
            store.close()
            return {**candidate.__dict__, "mean_ic": 0, "icir": 0, "n_obs": 0,
                    "registered": False, "verdict": "insufficient_data"}

        data_full = store.get_daily(symbols, start=dates[0], end=dates[-1])
        close = data_full["close"]
        fwd = close.shift(-5) / close - 1  # T+5 前向收益

        # 2. 逐日计算 IC
        ic_vals = []
        for d in dates[:-5]:
            try:
                _d = pd.Timestamp(d)
                fv = fn(data_full.loc[:_d], d)
                fr = fwd.loc[d].dropna()
                common = fv.dropna().index.intersection(fr.index)
                if len(common) < 30:
                    continue
                ic, _ = spearmanr(fv[common], fr[common])
                if not np.isnan(ic):
                    ic_vals.append(ic)
            except Exception:
                continue

        if len(ic_vals) < 10:
            store.close()
            return {**candidate.__dict__, "mean_ic": 0, "icir": 0, "n_obs": len(ic_vals),
                    "registered": False, "verdict": "insufficient"}

        mean_ic = np.mean(ic_vals)
        icir = mean_ic / np.std(ic_vals) if np.std(ic_vals) > 0 else 0

        # 评估 verdict
        abs_ic = abs(mean_ic)
        abs_icir = abs(icir)
        verdict = "active"
        if abs_ic < self._ic_threshold or abs_icir < self._icir_threshold:
            verdict = "marginal"
        if abs_ic < self._ic_threshold * 0.5 or abs_icir < self._icir_threshold * 0.5:
            verdict = "failed"

        # 注册
        registered = False
        if verdict in ("active", "marginal"):
            registered = self.register_factor(candidate, verdict, mean_ic, icir, len(ic_vals))

        store.close()
        return {
            "name": candidate.name,
            "mean_ic": mean_ic,
            "icir": icir,
            "n_obs": len(ic_vals),
            "registered": registered,
            "verdict": verdict,
        }

    def curate_all(self, n_symbols: int = 500, n_dates: int = 120,
                   auto_register: bool = True) -> dict:
        """策展全部未注册因子.

        Returns:
            {n_evaluated, n_registered, results}
        """
        f_repo = FactorRepo()
        existing = set(f_repo.all_factor_names())

        candidates = [
            FactorCandidate(name=f["name"], expression=f["expression"],
                           source=f["source"], direction=f["direction"],
                           category=f["category"])
            for f in _CURATED_FACTORS
            if f["name"] not in existing
        ]
        if not candidates:
            _log.info("curator: all curated factors already registered")
            return {"n_evaluated": 0, "n_registered": 0, "results": []}

        _log.info(f"curator: evaluating {len(candidates)} new factors")

        results = []
        n_registered = 0
        for cf in candidates:
            result = self.compile_evaluate_register(cf, n_symbols, n_dates)
            results.append(result)
            if result["registered"]:
                n_registered += 1

        return {"n_evaluated": len(results), "n_registered": n_registered, "results": results}

    # ── 状态迁移 (原 state_manager.py 逻辑) ──

    def transition(
        self,
        name: str,
        event: str,
        reason: str,
        *,
        retry_count: int | None = None,
    ) -> bool:
        """原子状态转换.

        Args:
            name: 因子名
            event: 事件名 (必须来自 _VALID_EVENTS)
            reason: 转换原因 (写入 status_reason 字段)
            retry_count: 手动指定 retry_count (仅 EVAL_FAIL/IC_PERSISTENT 使用;
                         None 时保持现有值)

        Returns:
            True: 转换成功

        Raises:
            InvalidTransitionError: 非法转换
            ValueError: factor 不存在 或 event 非法
        """
        if not self.is_valid_event(event):
            raise ValueError(
                f"非法事件: {event!r} (允许: {sorted(_VALID_EVENTS)})"
            )

        current = self.get_status(name)
        if current is None:
            raise ValueError(f"因子不存在: {name!r}")

        target = self.get_target(current, event)

        # retry_count 管理
        new_retry = retry_count
        if new_retry is None:
            if event == FactorEvent.EVAL_FAIL:
                factor = self._repo.get_factor_by_name(name)
                current_retry = factor.get("retry_count", 0) if factor else 0
                new_retry = int(current_retry or 0) + 1
            elif event in (FactorEvent.EVAL_OK, FactorEvent.IC_RECOVERED):
                new_retry = 0
            elif event == FactorEvent.DATA_SOURCE_DEAD:
                factor = self._repo.get_factor_by_name(name)
                new_retry = (factor.get("retry_count", 0) or 0) + 1 if factor else 1

        # 写入 DB
        ok = self._repo.update_status(
            name, target, reason=reason, retry_count=new_retry
        )
        if ok:
            _log.info(
                "factor state: %s: %s → %s (event=%s, retry=%s)",
                name, current, target, event,
                new_retry if new_retry is not None else "-"
            )
        return ok

    def batch_transition(
        self,
        names: list[str],
        event: str,
        reason: str,
    ) -> int:
        """批量状态转换. 返回成功数.

        每因子独立执行 — 一个失败不阻塞其余.
        """
        if not names:
            return 0
        success = 0
        for name in names:
            try:
                if self.transition(name, event, reason):
                    success += 1
            except (InvalidTransitionError, ValueError) as e:
                _log.warning("batch_transition: %s skipped — %s", name, e)
        return success

    def get_active_factors(self) -> list[str]:
        """获取 active 状态因子列表."""
        factors = self._repo.get_all_by_status(("active",))
        return [f["name"] for f in factors]

    def get_probation_factors(self) -> list[str]:
        """获取 probation 状态因子列表."""
        factors = self._repo.get_all_by_status(("probation",))
        return [f["name"] for f in factors]

    def get_probation_count(self) -> int:
        """probation 因子数."""
        return len(self._repo.get_all_by_status(("probation",)))


# ── 向后兼容: 保留原 FactorStateManager 类名作为别名 ──
class FactorStateManager(FactorStateMachine):
    """向后兼容别名 — 即将废弃."""
    pass


# ── 异常类 ──
class InvalidTransitionError(ValueError):
    """非法状态转换 — 零 fallback, 必须抛异常."""
    pass