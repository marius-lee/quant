"""Experiment + Trace system — the unified feedback loop.

Based on RD-Agent's core abstractions (Trace, Hypothesis, ExperimentFeedback) but
adapted for our computational evaluation pipeline rather than LLM-driven code generation.

Every evaluation run, backtest variant, and factor lifecycle event becomes a node
in a DAG (directed acyclic graph), enabling:
- Full traceability: "why was this factor retired?"
- SOTA discovery: "which factor combination produced the best Sharpe?"
- Feedback propagation: "did the walk-forward results change our IC thresholds?"

Architecture:
   Experiment (one evaluation/backtest run)
     |-- Hypothesis: what we believed before the run
     |-- sub_results: phase-level outcomes
     |-- ExperimentFeedback: verdict + reasoning
   Trace (DAG of Experiments)
     |-- hist: list of (Experiment, ExperimentFeedback) pairs
     |-- dag_parent: parent indices forming the graph
     |-- get_sota(): walk the DAG to find best-performing node
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from quant.config.constants import _market_db_path


@dataclass
class Hypothesis:
    """What we believed before running an experiment."""
    hypothesis: str
    reason: str = ""
    source: str = ""

    def __str__(self) -> str:
        return f"Hypothesis: {self.hypothesis}\nReason: {self.reason}"


@dataclass
class ExperimentFeedback:
    """Verdict after executing an experiment. decision=True means hypothesis supported."""
    decision: bool
    reason: str
    metrics: dict[str, float] = field(default_factory=dict)
    exception: str | None = None

    def __bool__(self) -> bool:
        return self.decision

    def __str__(self) -> str:
        parts = [f"Decision: {self.decision}", f"Reason: {self.reason}"]
        if self.metrics:
            parts.append(f"Metrics: {json.dumps(self.metrics)}")
        return "\n".join(parts)


@dataclass
class Experiment:
    """One complete evaluation/backtest run."""
    experiment_id: str
    action: str
    timestamp: str
    hypothesis: Hypothesis | None = None
    parent_id: str | None = None
    sub_results: dict[str, Any] = field(default_factory=dict)
    feedback: ExperimentFeedback | None = None

    @property
    def is_accepted(self) -> bool:
        return self.feedback is not None and self.feedback.decision


class Trace:
    """Ordered DAG of experiments. Persists to market.db experiments table."""

    def __init__(self) -> None:
        self.hist: list[Experiment] = []
        self._init_db()

    @staticmethod
    def _init_db() -> None:
        db_path = _market_db_path()
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS experiments (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                experiment_id   TEXT UNIQUE NOT NULL,
                parent_id       TEXT,
                action          TEXT NOT NULL,
                timestamp       TEXT NOT NULL,
                hypothesis      TEXT,
                hypothesis_reason TEXT,
                decision        INTEGER,
                reason          TEXT,
                metrics_json    TEXT,
                sub_results_json TEXT,
                created_at      TEXT DEFAULT (datetime('now','localtime'))
            )
        """)
        conn.commit()
        conn.close()

    def record(self, exp: Experiment) -> None:
        self.hist.append(exp)
        self._save_to_db(exp)

    def _save_to_db(self, exp: Experiment) -> None:
        db_path = _market_db_path()
        conn = sqlite3.connect(db_path)
        fb = exp.feedback
        conn.execute(
            """INSERT INTO experiments
               (experiment_id, parent_id, action, timestamp, hypothesis, hypothesis_reason,
                decision, reason, metrics_json, sub_results_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                exp.experiment_id, exp.parent_id, exp.action, exp.timestamp,
                exp.hypothesis.hypothesis if exp.hypothesis else None,
                exp.hypothesis.reason if exp.hypothesis else None,
                int(fb.decision) if fb else None,
                fb.reason if fb else None,
                json.dumps(fb.metrics) if fb and fb.metrics else None,
                json.dumps(exp.sub_results, ensure_ascii=False, default=str),
            ),
        )
        conn.commit()
        conn.close()

    def load_from_db(self, limit: int = 100) -> list[Experiment]:
        db_path = _market_db_path()
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT experiment_id, parent_id, action, timestamp, hypothesis, hypothesis_reason, "
            "decision, reason, metrics_json, sub_results_json "
            "FROM experiments ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        result = []
        for row in reversed(rows):
            fb = None
            if row[6] is not None:
                metrics = json.loads(row[8]) if row[8] else {}
                fb = ExperimentFeedback(decision=bool(row[6]), reason=row[7] or "", metrics=metrics)
            sub = json.loads(row[9]) if row[9] else {}
            exp = Experiment(
                experiment_id=row[0], parent_id=row[1], action=row[2], timestamp=row[3],
                hypothesis=Hypothesis(hypothesis=row[4] or "", reason=row[5] or ""),
                feedback=fb, sub_results=sub,
            )
            result.append(exp)
        self.hist = result
        return result

    def get_sota(self, metric: str = "sharpe") -> Experiment | None:
        best = None
        best_val = float("-inf")
        for exp in self.hist:
            if exp.is_accepted and exp.feedback:
                val = exp.feedback.metrics.get(metric, float("-inf"))
                if val > best_val:
                    best_val = val
                    best = exp
        return best

    def get_by_action(self, action: str, limit: int = 10) -> list[Experiment]:
        return [e for e in reversed(self.hist) if e.action == action][:limit]

    def get_children_of(self, parent_id: str) -> list[Experiment]:
        return [e for e in self.hist if e.parent_id == parent_id]

    def last_of(self, action: str) -> Experiment | None:
        for e in reversed(self.hist):
            if e.action == action:
                return e
        return None

    def __len__(self) -> int:
        return len(self.hist)

    def __iter__(self):
        return iter(self.hist)


_global_trace: Trace | None = None


def get_trace() -> Trace:
    global _global_trace
    if _global_trace is None:
        _global_trace = Trace()
        _global_trace.load_from_db()
    return _global_trace


def make_experiment(
    action: str,
    hypothesis: Hypothesis | None = None,
    parent_id: str | None = None,
    **sub_results: Any,
) -> Experiment:
    import uuid
    ts = datetime.now().isoformat()
    exp_id = uuid.uuid4().hex[:12]
    return Experiment(
        experiment_id=exp_id,
        action=action,
        timestamp=ts,
        hypothesis=hypothesis,
        parent_id=parent_id,
        sub_results=sub_results,
    )
