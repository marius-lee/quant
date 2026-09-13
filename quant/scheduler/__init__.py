"""调度器 — 单一编排器 + manifest 任务清单 (v428 重构 + v433 Runner 拆分).

v428: 废弃"每任务独立 _loop 线程"时代架构 (signals/execute/monitor/attribution/
weekly 各自的 _timed_loop/_weekly_loop 全部删除, _base.py 移除).
全部调度由 orchestrator 单进程驱动:
  - 日线任务: manifest._DAYLINE (时间窗+依赖+超时) → 主循环决策执行
  - 周频评估: manifest._WEEKLY (周六 06:00-12:00) → orchestrator subprocess
  - monitor: 长驻窗口任务 (09:30-15:00) → orchestrator 守护线程

v433 重构: 拆分为三大 Runner (InlineRunner/MonitorRunner/SubprocessRunner) + 共用决策函数.

启动入口: restart.sh → start_all() (兼容旧); 幂等, 双进程防御由 PID 锁 + grace dedup.

v565: Dagster 模式支持 — 环境变量 QUANT_ORCHESTRATOR=dagster 使用新架构.
v577 fix: Dagster daemon 正确启动, definitions 验证, Web UI /api/scheduler 统一.
Web 界面(/api/scheduler)通过 task_runs 表统一监控, 无论使用哪种模式.
"""
import os
import threading
import subprocess
from quant.utils.logger import get_logger
from quant.scheduler.runners import (
    run_inline_tasks as _run_inline_tasks,
    run_monitor as _run_monitor,
    run_evening_chain as _run_evening_chain,
    run_weekly_eval as _run_weekly_eval,
)

_log = get_logger(__name__)


def get_orchestrator_mode():
    """获取当前编排器模式: 'legacy' 或 'dagster'."""
    return os.environ.get("QUANT_ORCHESTRATOR", "legacy")


def start_all():
    """启动编排器 (v428: 单任务源 — weekly 由 manifest 窗口并入 orchestrator).

    v565: 如果 QUANT_ORCHESTRATOR=dagster, 使用 Dagster 模式 (非阻塞).
    """
    if get_orchestrator_mode() == "dagster":
        _start_dagster()
        return
    _start_orch()


def start_scheduler():
    start_all()


def _start_dagster():
    """启动 Dagster 编排器 (非阻塞 — Dagster Daemon 在独立进程管理).

    启动方式 (DAGSTER_LAUNCH_MODE 环境变量控制):
      - "daemon" (默认): 启动 dagster-daemon 进程管理调度 + dagster-webserver 提供 UI
      - "webserver" 仅: 仅启动 dagster-webserver (daemon 需独立部署)
      - "process": 进程内模拟 (开发测试用)

    实际调度由 Dagster Daemon (cron + Sensors) 在独立进程执行.
    Web 界面通过 task_runs 表监控, 与 legacy 模式统一.
    """
    from quant.orchestrator.dagster_assets import get_definitions

    # 验证 Definitions 可加载
    try:
        defs = get_definitions()
        _log.info(f"Dagster Definitions loaded: {[j.name for j in defs.jobs]}")
    except Exception as e:
        _log.error(f"Dagster Definitions 加载失败: {e}")
        raise

    launch_mode = os.environ.get("DAGSTER_LAUNCH_MODE", "daemon")
    proj_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    if launch_mode == "process":
        # 开发测试模式: 不启动外部进程, 仅记录日志
        _log.info("Dagster 模拟模式 (DAGSTER_LAUNCH_MODE=process) — "
                   "仅验证 definitions 可用, 不启动外部进程")
        return

    dagster_bin = os.path.join(proj_root, ".venv", "bin", "dagster")
    dagster_file = os.path.join(proj_root, "quant", "orchestrator", "dagster_assets.py")

    if launch_mode == "webserver":
        # 仅启动 dagster-webserver (daemon 需独立部署)
        cmd = [
            dagster_bin, "webserver",
            "-f", dagster_file,
            "-p", "3333",
            "--working-directory", proj_root,
        ]
        _log.info(f"Dagster webserver: {' '.join(cmd)}")
        proc = subprocess.Popen(cmd, cwd=proj_root, env={**os.environ})
        _log.info(f"Dagster webserver started (pid={proc.pid})")
        return

    # 默认: 启动 dagster-daemon (调度) + 线程启动 dagster-webserver (UI)
    daemon_cmd = [
        dagster_bin, "daemon",
        "-f", dagster_file,
        "--working-directory", proj_root,
    ]
    _log.info(f"Dagster daemon: {' '.join(daemon_cmd)}")

    def start_daemon():
        proc = subprocess.Popen(daemon_cmd, cwd=proj_root, env={**os.environ})
        _log.info(f"Dagster daemon started (pid={proc.pid})")

    def start_webserver():
        ws_cmd = [
            dagster_bin, "webserver",
            "-f", dagster_file,
            "-p", "3333",
            "--working-directory", proj_root,
        ]
        _log.info(f"Dagster webserver: {' '.join(ws_cmd)}")
        proc = subprocess.Popen(ws_cmd, cwd=proj_root, env={**os.environ})
        _log.info(f"Dagster webserver started (pid={proc.pid})")

    # daemon 在独立线程启动 (不阻塞主线程)
    threading.Thread(target=start_daemon, daemon=True, name="dagster-daemon").start()
    # webserver 在独立线程启动
    threading.Thread(target=start_webserver, daemon=True, name="dagster-webserver").start()


# 向后兼容导出
from quant.scheduler.orchestrator import _run as _run_orch

# 兼容旧导入
def start():
    start_all()


# 向后兼容: 供测试使用 (如 test_weekly_sat_trigger_v416.py)
_start_orch = _run_orch

# 导出供外部使用
__all__ = ["start_all", "start_scheduler", "start", "_run_orch", "get_orchestrator_mode"]
