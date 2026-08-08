"""调度任务清单 — 单一真相源 (v428 重构).

替代 orchestrator 内硬编码的时间窗口/依赖/超时:
  - 每个任务一行声明: 触发窗口、依赖、执行模式、超时
  - 编排器 (orchestrator._run) 只读清单做决策, 不再散落魔数
  - 周频/晚间链也入清单, 消除"多路触发 + 多种状态语义"的重复逻辑
    (v420-v427 的遮蔽/双触发/重复评估 bug 均源于此)

任务类别 (mode):
  inline    — orchestrator 进程内同步执行 (signals/execute/snapshot/reconcile)
  monitor   — 盘中长驻窗口任务 (09:30-15:00 持续循环, 内部管理午休暂停)
  subprocess — 独立子进程 (晚间链/周度评估 — 重计算, 不阻塞 orchestrator)

依赖语义:
  depends_ok      — 上游状态 == ok 才触发 (严格: 今日成功完成)
  depends_attempt — 上游今日曾尝试过 (running/ok/failed/aborted 任一) 即触发
  (历史行为: execute 只要 signals 在 status 字典里就执行 — 用 attempt 语义
   保守还原, 避免改变盘中业务行为)

时间均以 datetime.time 表达; 每个任务仅在其窗口内可触发,
窗口关闭但未完成 → 当日放弃 (由 restart 补跑语义兜底)。
"""
from dataclasses import dataclass, field
from datetime import time

from quant.utils.logger import get_logger

_log = get_logger(__name__)


@dataclass(frozen=True)
class TaskSpec:
    name: str
    label: str                     # UI 显示名
    schedule: str                  # UI 展示的排程描述
    window: tuple[time, time]      # 可触发时间窗 [run_at, run_at] — 触发闭区间
    mode: str = "inline"           # inline | monitor | subprocess
    depends_ok: tuple[str, ...] = ()
    depends_attempt: tuple[str, ...] = ()
    grace_s: int = 300             # _tk_start 宽限 (防双触发误 abort)
    timeout_s: int | None = None   # 超时判定 (None=不超时, 如 monitor 随窗口)
    weekday: int | None = None     # 仅某星期可触发 (0=Mon..6=Sun)
    desc: str = ""
    group: str = "其他"
    has_multiprocess: bool = False
    # subprocess 专用
    subprocess_cmd: str = ""       # python -c 内容 (mode=subprocess)

    def in_window(self, hhmm: time, weekday: int) -> bool:
        if self.weekday is not None and weekday != self.weekday:
            return False
        return self.window[0] <= hhmm <= self.window[1]


# ─────────────────────────────────────────────────────────────────────
# 日线任务清单 (交易日)
# ─────────────────────────────────────────────────────────────────────
_DAYLINE: list[TaskSpec] = [
    TaskSpec(
        name="signals", label="信号生成", schedule="08:30",
        window=(time(8, 0), time(15, 30)),
        grace_s=1800, timeout_s=1800,
        mode="inline", group="盘中",
        desc="计算所有 using 因子，生成 Alpha 信号与目标持仓",
        has_multiprocess=True,
    ),
    TaskSpec(
        name="execute", label="交易执行", schedule="09:30",
        window=(time(9, 20), time(14, 56)),
        depends_attempt=("signals",),  # 原始: signals 尝试过即可 (不要求 ok)
        grace_s=1800, timeout_s=1800,
        mode="inline", group="盘中",
        desc="读取信号、获取行情、执行调仓订单",
        has_multiprocess=True,
    ),
    TaskSpec(
        name="snapshot_open", label="开盘快照", schedule="10:00 (execute后)",
        window=(time(10, 0), time(14, 55)),
        depends_attempt=("execute",),  # 保持原行为: execute 尝试过即 snapshot
        grace_s=300, timeout_s=300,
        mode="inline", group="盘中",
        desc="快照所有A股开盘30分钟实时价+量, 供日内反转/量比因子",
    ),
    TaskSpec(
        name="monitor", label="盘中风控", schedule="09:35-15:00",
        window=(time(9, 30), time(15, 0)),
        grace_s=21600, timeout_s=None,   # 长驻窗口任务, 随窗口自然结束
        mode="monitor", group="盘中",
        desc="每30s轮询 止损/止盈/熔断, 触发后立即卖出 (窗口结束自退)",
    ),
    TaskSpec(
        name="snapshot_close", label="尾盘快照", schedule="15:00 (收盘)",
        # v428 修正: 原 14:55 触发拉到的是收盘前瞬间价, 非收盘数据.
        # 尾盘区间=14:55-15:00; 快照应在 15:00 收盘后拉 收盘价+全日累计量.
        # (若要"尾盘5分钟增量量能"需 14:55 + 15:00 两次取差 -- 未实现)
        window=(time(15, 0), time(15, 5)),
        grace_s=300, timeout_s=300,
        mode="inline", group="盘中",
        desc="快照所有A股收盘价+全日量, 供尾盘异动因子 (收盘后触发)",
    ),
    TaskSpec(
        name="reconcile", label="日终对账", schedule="15:05",
        window=(time(15, 5), time(16, 0)),
        depends_ok=("monitor",),   # v428: monitor 自然结束 (ok) 才对账
        grace_s=600, timeout_s=600,
        mode="inline", group="盘后",
        desc="OMS 对账闭环: 持仓/现金/订单三账核对, break 超阈值告警",
    ),
    TaskSpec(
        name="evening_chain", label="晚间链", schedule="19:00 (daily_data起)",
        window=(time(19, 0), time(23, 59)),
        grace_s=14400, timeout_s=14400,
        mode="subprocess", group="盘后", has_multiprocess=True,
        subprocess_cmd=(
            "from quant.utils.excepthook import setup; setup();"
            "from quant.scheduler.evening import _run;"
        ),
        desc="daily_data → adj_factor → factor_cache → attribution → lgb/xgb",
    ),
]


# ─────────────────────────────────────────────────────────────────────────────────
# 周六 06:00-12:00 — 周度因子评估 (subprocess; 非交易日也触发, 独立窗口)
# ─────────────────────────────────────────────────────────────────────────────────
_WEEKLY: list[TaskSpec] = [
    TaskSpec(
        name="weekly_eval", label="因子评估(总)", schedule="周六 06:00",
        window=(time(6, 0), time(12, 0)), weekday=5,
        grace_s=43200, timeout_s=43200,
        mode="subprocess", group="研究", has_multiprocess=True,
        subprocess_cmd=(
            "from quant.utils.excepthook import setup; setup();"
            "from quant.scheduler.weekly import _run;"
        ),
        desc="全自动五阶段评估: 策展→数据→IC→CPCV→成本→状态同步",
    ),
]


# ── 统一注册表: 决策顺序 = 清单顺序 ──
ALL: dict[str, TaskSpec] = {s.name: s for s in _DAYLINE + _WEEKLY}

# 执行顺序 (依赖拓扑序): signals → execute → snapshot_open → monitor →
# snapshot_close → reconcile → evening_chain; weekly 独立 (周六)
_PLAN_ORDER: list[str] = [s.name for s in _DAYLINE + _WEEKLY]


def spec(name: str) -> TaskSpec:
    return ALL[name]