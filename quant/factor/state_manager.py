"""FactorStateManager — 因子状态机 (单一真相源).

所有模块通过此管理器转换因子状态，不直接写 factor_registry.status。
对标: WorldQuant 单一调度器 + QuantConnect 事件驱动状态机。

设计约束:
  - 零 fallback: 非法转换 → ValueError
  - 所有阈值从 config.yaml 读取
  - 状态转换表为纯数据结构，可单元测试
"""

from __future__ import annotations

from typing import Optional

from quant.config.constants import _require_cfg
from quant.utils.logger import get_logger

_log = get_logger("factor.state_manager")

# ── 有效状态 ──
VALID_STATUSES = frozenset({
    "evaluating",   # 待评估 (原 candidate)
    "active",       # 通过完整评估+IC未衰减 → 实盘信号生成, 完整权重
    "probation",    # IC 衰减观察期 (原 monitoring), 实盘信号衰减权重
    "archived",     # 归档 (原 retired+rejected 合并), status_reason 区分原因
})

# ── 状态转换表 ──
_TRANSITIONS: dict[tuple[str, str], str] = {
    # 评估流水线
    ("evaluating",  "EVAL_PASS"):       "active",
    ("evaluating",  "EVAL_MARGINAL"):   "probation",
    ("evaluating",  "EVAL_FAIL"):       "archived",

    # 实盘归因
    ("active",      "IC_DEGRADED"):     "probation",
    ("probation",   "IC_RECOVERED"):    "active",
    ("probation",   "IC_PERSISTENT"):   "archived",

    # 因子冗余 (相关性去重, P1-1)
    ("active",      "FACTOR_REDUNDANT"): "probation",
    ("probation",   "FACTOR_REDUNDANT"): "archived",

    # 快速降级 (ADR-040: 数据源死亡直接归档)
    ("active",      "DATA_SOURCE_DEAD"): "archived",
    ("probation",   "DATA_SOURCE_DEAD"): "archived",

    # 归档恢复
    ("archived",    "RETRY_RESTORE"):    "evaluating",
}

# 所有合法事件 (来源: _TRANSITIONS 的 value 去重)
_VALID_EVENTS = frozenset(event for _, event in _TRANSITIONS.keys())


class InvalidTransitionError(ValueError):
    """非法状态转换 — 零 fallback, 必须抛异常."""
    pass


class FactorStateManager:
    """因子状态机 — 唯一写入 factor_registry.status 的模块.

    使用方式:
        fsm = FactorStateManager()
        fsm.transition("momentum_63d", "EVAL_PASS",
                       reason="Phase 2+3+4 passed: IC=0.035, PBO=0.15")

    批量:
        fsm.batch_transition(["f1", "f2"], "EVAL_PASS", reason="...")

    查询 (无副作用):
        fsm.can_transition("active", "IC_DEGRADED")  # → True
        fsm.get_status("momentum_63d")                # → "active"
    """

    def __init__(self):
        from quant.data.repos import FactorRepo
        self._repo = FactorRepo()
        # 阈值来源: config.yaml (单一真相源)
        self._max_retries = _require_cfg("factor.evaluation.max_retries")
        # 来源: config.yaml factor.evaluation.max_retries=3
        # 语义: 因子连续 retired 次数达到此值 → 自动 rejected

    # ── 状态查询 (无副作用) ──

    def get_status(self, name: str) -> str | None:
        """读取因子当前状态. 返回 None 表示因子不存在."""
        factor = self._repo.get_factor_by_name(name)
        return factor["status"] if factor else None

    def can_transition(self, current: str, event: str) -> bool:
        """检查 (current, event) 是否为合法转换."""
        return (current, event) in _TRANSITIONS

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

    # ── 状态转换 (有副作用 — 写DB) ──

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
            retry_count: 手动指定 retry_count (仅 EVAL_FAIL/EVAL_REJECT 使用;
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
            if event == "EVAL_FAIL":
                # 读当前 retry_count + 1
                factor = self._repo.get_factor_by_name(name)
                current_retry = factor.get("retry_count", 0) if factor else 0
                new_retry = int(current_retry or 0) + 1
            elif event == "EVAL_PASS":
                new_retry = 0  # 通过评估, 重置计数
            elif event == "IC_RECOVERED":
                new_retry = 0  # IC恢复, 重置计数
            elif event == "DATA_SOURCE_DEAD":
                # 直接归档, 保留现有 retry_count
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

    # ── retry 管理 ──

    def check_and_reject_stale_retired(self) -> int:
        """扫描所有 retired 因子, retry_count≥max_retries 的自动转 rejected.

        来源: phase5_monitor.py retry_count 管理逻辑 (ADR-026 §4.5).
        返回: 转为 rejected 的因子数.
        """
        retired_factors = self._repo.get_all_by_status(("retired",))
        n = 0
        for f in retired_factors:
            name = f.get("name", "")
            count = f.get("retry_count", 0) or 0
            if count >= self._max_retries:
                try:
                    self.transition(
                        name, "EVAL_REJECT",
                        reason=(
                            f"[EVAL] 累计 {count} 次 retired "
                            f"(≥{self._max_retries}), 永久淘汰"
                        ),
                        retry_count=count,
                    )
                    n += 1
                except (InvalidTransitionError, ValueError) as e:
                    _log.warning("check_and_reject: %s skipped — %s", name, e)
        return n

    def get_retry_count(self, name: str) -> int:
        """读取因子 retry_count."""
        factor = self._repo.get_factor_by_name(name)
        return int(factor.get("retry_count", 0) or 0) if factor else 0

    # ── 便捷查询 ──

    def get_pool(self, pool: str) -> list[str]:
        """返回指定池的因子名列表.

        pool:
            'using'       → active + monitoring (实盘信号生成)
            'backtesting' → candidate + monitoring + retired (回测评估池)
            'all'         → 全部
        """
        if pool == "using":
            statuses = ("active", "monitoring")
        elif pool == "backtesting":
            statuses = ("candidate", "monitoring", "retired")
        elif pool == "all":
            statuses = tuple(VALID_STATUSES)
        else:
            raise ValueError(f"未知池: {pool!r} (允许: using, backtesting, all)")
        factors = self._repo.get_all_by_status(statuses)
        return [f["name"] for f in factors]

    def get_active_count(self) -> int:
        """active 因子数."""
        return len(self._repo.get_all_by_status(("active",)))

    def get_monitoring_count(self) -> int:
        """monitoring 因子数."""
        return len(self._repo.get_all_by_status(("monitoring",)))
