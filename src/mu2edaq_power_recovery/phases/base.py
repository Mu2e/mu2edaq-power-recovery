"""Common shape for a phase's outcome."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..checks import Status
from ..orchestrator import TIMEOUT_SUMMARY, NodeAssessment, tally


__all__ = ["Deadline", "PhaseResult", "TIMEOUT_SUMMARY", "overall_status",
           "phase_deadline"]


class Deadline:
    """A monotonic wall-clock budget.

    ``budget`` of None or <= 0 means unbounded: :meth:`remaining` is infinite,
    :meth:`expired` is always False and :meth:`cap` passes timeouts through.

    The clock is injectable (the orchestrator's, normally ``time.monotonic``)
    so a test can run a whole phase against a fake clock without sleeping.
    Other modules use a Deadline by duck type -- ``remaining()``,
    ``expired()``, ``cap()``, ``expires_at`` -- because transport/ and the
    orchestrator sit below phases/ in the import graph.
    """

    def __init__(self, budget: Optional[float],
                 clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.started = clock()
        self.budget: Optional[float] = (float(budget) if budget is not None
                                        and float(budget) > 0 else None)

    @property
    def expires_at(self) -> float:
        """The clock value at which the budget runs out (inf when unbounded)."""
        if self.budget is None:
            return math.inf
        return self.started + self.budget

    def remaining(self) -> float:
        """Seconds left, never negative; inf when unbounded."""
        if self.budget is None:
            return math.inf
        return max(0.0, self.expires_at - self.clock())

    def expired(self) -> bool:
        return self.budget is not None and self.clock() >= self.expires_at

    def cap(self, timeout: Optional[float]) -> Optional[float]:
        """``min(timeout, remaining)``; *timeout* unchanged when unbounded."""
        if self.budget is None:
            return timeout
        if timeout is None:
            return self.remaining()
        return min(float(timeout), self.remaining())

    def child(self, budget: Optional[float]) -> "Deadline":
        """A deadline of ``min(budget, remaining)``, on the same clock.

        A stage deadline: it can never outlive the phase that holds it.
        """
        limit = self.remaining()
        if budget is not None and float(budget) > 0:
            limit = min(limit, float(budget))
        out = Deadline(None, self.clock)
        if limit != math.inf:
            out.budget = limit
            out.started = self.clock()
            if limit <= 0:
                # Already exhausted: expired() must be True, which a budget of
                # 0 would not give (0 reads as "unbounded").
                out.budget = 0.0
        return out

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"Deadline(budget={self.budget}, "
                f"remaining={self.remaining():.1f})")


def phase_deadline(orch: Any) -> Deadline:
    """The ``run.phase_timeout`` deadline for a phase, on the orchestrator's clock."""
    return Deadline(orch.settings.get("run.phase_timeout"),
                    getattr(orch, "clock", time.monotonic))


@dataclass
class PhaseResult:
    """What one phase produced.

    Phases return this rather than writing HTML or printing tables directly,
    so that the same result can be rendered to the console, to the report site
    and to the logbook entry without three code paths drifting apart.
    """

    name: str
    number: int
    title: str
    status: Status = Status.OK
    summary: str = ""
    assessments: List[NodeAssessment] = field(default_factory=list)
    #: Phase-specific payload: stage outcomes, mesh matrices, publication info.
    data: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""
    duration: float = 0.0

    @property
    def counts(self) -> Dict[str, int]:
        return tally(self.assessments)

    def failed_nodes(self) -> List[NodeAssessment]:
        return [a for a in self.assessments if a.status.is_bad]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "number": self.number,
            "title": self.title,
            "status": self.status.value,
            "summary": self.summary,
            "counts": self.counts,
            "notes": list(self.notes),
            "data": self.data,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration": round(self.duration, 2),
            "assessments": [a.as_dict() for a in self.assessments],
        }


def overall_status(assessments: Sequence[NodeAssessment]) -> Status:
    """Worst node verdict in the set; OK when there are no nodes."""
    if not assessments:
        return Status.UNKNOWN
    return max((a.status for a in assessments), key=lambda s: s.rank)
