"""Pipeline phase tracker — structured timing and status for multi-step workflows.

Usage:
    tracker = PhaseTracker()
    with tracker.phase("load"):
        data = store.get_daily(...)
    with tracker.phase("factor"):
        factors = compute_all_factors(data, ...)
    logger.info(f"done: {tracker.summary()}")
    # → done: load(2.1s/ok) -> factor(18.3s/ok) -> risk(0.5s/ok) -> optimize(1.2s/ok)

Inspired by: daily_stock_analysis project's Timer-based pipeline tracking.
"""

from __future__ import annotations

import time
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class PhaseResult:
    """Single phase execution record."""
    name: str
    started: float = 0.0
    finished: float = 0.0
    status: str = "pending"  # ok | skipped | failed
    errors: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def elapsed(self) -> float:
        return self.finished - self.started


class PhaseTracker:
    """Track named phases with timing, status, and error counts.

    Thread-safe for single-thread use (backtest / pipeline are serial).
    Reports structured logs and a human-readable summary.
    """

    def __init__(self, name: str = ""):
        self.name = name
        self.phases: list[PhaseResult] = []

    @contextmanager
    def phase(self, name: str, extra: Optional[dict] = None):
        p = PhaseResult(name=name, started=time.time(), extra=extra or {})
        try:
            yield p
            p.status = "ok"
        except Exception:
            p.status = "failed"
            p.errors = 1
            raise
        finally:
            p.finished = time.time()
            self.phases.append(p)

    def summary(self) -> str:
        """One-line summary: load(2.1s/ok) -> factor(18.3s/ok) -> ..."""
        parts = []
        for p in self.phases:
            parts.append(f"{p.name}({p.elapsed:.1f}s/{p.status})")
        return " -> ".join(parts) if parts else "(no phases)"

    def report(self) -> dict:
        """Structured report dict for downstream consumers (evaluation_runs, logs)."""
        return {
            "name": self.name,
            "phases": [
                {
                    "name": p.name,
                    "elapsed_s": round(p.elapsed, 3),
                    "status": p.status,
                    "errors": p.errors,
                    **p.extra,
                }
                for p in self.phases
            ],
            "total_elapsed_s": round(
                sum(p.elapsed for p in self.phases), 3
            ),
            "total_errors": sum(p.errors for p in self.phases),
        }

    def __repr__(self) -> str:
        return f"PhaseTracker({self.name!r}, phases={len(self.phases)})"
