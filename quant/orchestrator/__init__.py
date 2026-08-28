"""Orchestrator package — supports both legacy and Dagster modes.

入口:
  - quant.scheduler.start_all()  —  legacy orchestrator (默认, 向后兼容)
  - quant.orchestrator.dagster.get_definitions() — Dagster Definitions (新架构)

模式选择:
  环境变量 QUANT_ORCHESTRATOR=legacy|dagster
  - legacy (默认): quant/scheduler/orchestrator.py 30s 轮询模式
  - dagster: quant/orchestrator/dagster_assets.py Dagster Daemon 模式

Web 界面(/api/scheduler)通过 task_runs 表统一监控, 无论使用哪种模式.
"""
import os

def get_mode() -> str:
    """获取当前编排器模式."""
    return os.environ.get("QUANT_ORCHESTRATOR", "legacy")

def is_dagster_mode() -> bool:
    """是否使用 Dagster 新架构."""
    return get_mode() == "dagster"

def get_dagster_definitions():
    """获取 Dagster Definitions (新架构入口)."""
    from quant.orchestrator.dagster_assets import get_definitions
    return get_definitions()

__all__ = ["get_mode", "is_dagster_mode", "get_dagster_definitions"]
