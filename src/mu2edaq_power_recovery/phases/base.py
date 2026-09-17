"""Common shape for a phase's outcome."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..checks import Status
from ..orchestrator import NodeAssessment, tally


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
