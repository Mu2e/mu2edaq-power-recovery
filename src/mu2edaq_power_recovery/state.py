"""Run store: what happened, when, and with what evidence.

SQLite by default, Postgres by setting ``database.url`` -- the schema is plain
SQLAlchemy Core and uses nothing backend-specific, so switching is a URL
change (CLAUDE.md: SQLite with hooks for Postgres).

The store is what makes the phases independent.  Phase 4 does not re-probe
anything; it reads the run back out of here.  Re-running phase 1 after a repair
creates a new phase row against the same run, so the report can show the
before-and-after rather than overwriting the evidence that the repair was
needed.
"""
from __future__ import annotations

import json
import logging
import os
import socket
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from sqlalchemy import (JSON, Column, DateTime, Float, ForeignKey, Integer,
                        MetaData, String, Table, Text, create_engine, select)
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)

metadata = MetaData()

#: One invocation of the driver, however many phases it ran.
runs = Table(
    "runs", metadata,
    Column("id", Integer, primary_key=True),
    Column("label", String(255)),
    Column("started_at", DateTime),
    Column("finished_at", DateTime, nullable=True),
    Column("operator", String(128)),
    Column("workstation", String(255)),
    Column("dry_run", Integer, default=1),
    Column("status", String(32), default="running"),
    Column("version", JSON),
    Column("settings", JSON),
)

phases = Table(
    "phases", metadata,
    Column("id", Integer, primary_key=True),
    Column("run_id", Integer, ForeignKey("runs.id"), index=True),
    Column("name", String(64)),
    Column("number", Integer),
    Column("started_at", DateTime),
    Column("finished_at", DateTime, nullable=True),
    Column("status", String(32), default="running"),
    Column("summary", Text, default=""),
    Column("data", JSON),
)

#: One node as seen in one phase -- its rolled-up verdict and class.
node_states = Table(
    "node_states", metadata,
    Column("id", Integer, primary_key=True),
    Column("phase_id", Integer, ForeignKey("phases.id"), index=True),
    Column("hostname", String(255), index=True),
    Column("location", String(64)),
    Column("node_class", String(64)),
    Column("status", String(32)),
    Column("power_state", String(32), nullable=True),
    Column("summary", Text, default=""),
    Column("data", JSON),
)

check_results = Table(
    "check_results", metadata,
    Column("id", Integer, primary_key=True),
    Column("phase_id", Integer, ForeignKey("phases.id"), index=True),
    Column("hostname", String(255), index=True),
    Column("check_id", String(64), index=True),
    Column("status", String(32)),
    Column("summary", Text),
    Column("detail", Text),
    Column("data", JSON),
    Column("evidence", JSON),
    Column("duration", Float),
    Column("recorded_at", DateTime),
)

#: Every state-changing thing the tools did, including the ones they refused.
#: This is the audit trail the logbook entry is built from.
actions = Table(
    "actions", metadata,
    Column("id", Integer, primary_key=True),
    Column("run_id", Integer, ForeignKey("runs.id"), index=True),
    Column("phase_id", Integer, ForeignKey("phases.id"), nullable=True),
    Column("hostname", String(255)),
    Column("action", String(64)),
    Column("target", String(255)),
    Column("outcome", String(32)),
    Column("dry_run", Integer, default=1),
    Column("detail", Text),
    Column("recorded_at", DateTime),
)

#: Free-form timeline: phase boundaries, operator decisions, warnings.
events = Table(
    "events", metadata,
    Column("id", Integer, primary_key=True),
    Column("run_id", Integer, ForeignKey("runs.id"), index=True),
    Column("level", String(16)),
    Column("message", Text),
    Column("recorded_at", DateTime),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RunStore:
    """Thin persistence layer over the tables above."""

    def __init__(self, url: str, echo: bool = False):
        self.url = url
        connect_args = {}
        if url.startswith("sqlite"):
            # Phase workers write results from several threads; SQLite's
            # default thread check would reject that, and the writes are
            # serialised by the engine's own lock anyway.
            connect_args["check_same_thread"] = False
        self.engine: Engine = create_engine(url, echo=echo, future=True,
                                            connect_args=connect_args)
        metadata.create_all(self.engine)
        self.run_id: Optional[int] = None
        self.phase_id: Optional[int] = None

    # -- construction ------------------------------------------------------

    @classmethod
    def from_settings(cls, settings: Any) -> "RunStore":
        url = settings.get("database.url")
        if not url:
            path = settings.resolve_path(settings.get("database.path",
                                                      "data/power-recovery.db"))
            path.parent.mkdir(parents=True, exist_ok=True)
            url = f"sqlite:///{path}"
        return cls(url)

    @contextmanager
    def _connect(self) -> Iterator[Any]:
        with self.engine.begin() as conn:
            yield conn

    # -- runs --------------------------------------------------------------

    def start_run(self, label: str, dry_run: bool, version: Dict[str, Any],
                  settings: Dict[str, Any]) -> int:
        with self._connect() as conn:
            result = conn.execute(runs.insert().values(
                label=label,
                started_at=_now(),
                operator=os.environ.get("USER") or os.environ.get("USERNAME") or "unknown",
                workstation=socket.gethostname(),
                dry_run=1 if dry_run else 0,
                status="running",
                version=version,
                settings=settings,
            ))
            self.run_id = int(result.inserted_primary_key[0])
        log.info("run %s started (%s)", self.run_id, "dry run" if dry_run else "LIVE")
        return self.run_id

    def finish_run(self, status: str = "complete") -> None:
        if self.run_id is None:
            return
        with self._connect() as conn:
            conn.execute(runs.update().where(runs.c.id == self.run_id).values(
                finished_at=_now(), status=status))

    # -- phases ------------------------------------------------------------

    def start_phase(self, name: str, number: int) -> int:
        with self._connect() as conn:
            result = conn.execute(phases.insert().values(
                run_id=self.run_id, name=name, number=number,
                started_at=_now(), status="running", data={}))
            self.phase_id = int(result.inserted_primary_key[0])
        return self.phase_id

    def finish_phase(self, status: str, summary: str = "",
                     data: Optional[Dict[str, Any]] = None) -> None:
        if self.phase_id is None:
            return
        with self._connect() as conn:
            conn.execute(phases.update().where(phases.c.id == self.phase_id).values(
                finished_at=_now(), status=status, summary=summary, data=data or {}))

    # -- results -----------------------------------------------------------

    def record_checks(self, results: List[Any]) -> None:
        """Bulk-insert check results for the current phase."""
        if not results:
            return
        rows = [{
            "phase_id": self.phase_id,
            "hostname": r.node,
            "check_id": r.check_id,
            "status": r.status.value if hasattr(r.status, "value") else str(r.status),
            "summary": r.summary,
            "detail": r.detail,
            "data": r.data,
            "evidence": r.evidence,
            "duration": r.duration,
            "recorded_at": _now(),
        } for r in results]
        with self._connect() as conn:
            conn.execute(check_results.insert(), rows)

    def record_node(self, hostname: str, location: str, node_class: str,
                    status: str, summary: str = "",
                    power_state: Optional[str] = None,
                    data: Optional[Dict[str, Any]] = None) -> None:
        with self._connect() as conn:
            conn.execute(node_states.insert().values(
                phase_id=self.phase_id, hostname=hostname, location=location,
                node_class=node_class, status=status, summary=summary,
                power_state=power_state, data=data or {}))

    def record_action(self, hostname: str, action: str, target: str,
                      outcome: str, dry_run: bool, detail: str = "") -> None:
        """Record a state-changing attempt -- including refusals.

        Called *before* the command is issued wherever the command is
        destructive, so that a tool that dies mid-power-cycle still leaves a
        record of what it was doing.
        """
        with self._connect() as conn:
            conn.execute(actions.insert().values(
                run_id=self.run_id, phase_id=self.phase_id, hostname=hostname,
                action=action, target=target, outcome=outcome,
                dry_run=1 if dry_run else 0, detail=detail, recorded_at=_now()))

    def record_event(self, message: str, level: str = "info") -> None:
        with self._connect() as conn:
            conn.execute(events.insert().values(
                run_id=self.run_id, level=level, message=message, recorded_at=_now()))

    # -- reads -------------------------------------------------------------

    def _rows(self, statement) -> List[Dict[str, Any]]:
        with self.engine.connect() as conn:
            return [dict(row._mapping) for row in conn.execute(statement)]

    def get_run(self, run_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        rid = run_id or self.run_id
        if rid is None:
            return None
        rows = self._rows(select(runs).where(runs.c.id == rid))
        return rows[0] if rows else None

    def latest_run_id(self) -> Optional[int]:
        rows = self._rows(select(runs.c.id).order_by(runs.c.id.desc()).limit(1))
        return rows[0]["id"] if rows else None

    def list_runs(self, limit: int = 30) -> List[Dict[str, Any]]:
        return self._rows(select(runs).order_by(runs.c.id.desc()).limit(limit))

    def get_phases(self, run_id: Optional[int] = None) -> List[Dict[str, Any]]:
        rid = run_id or self.run_id
        return self._rows(select(phases).where(phases.c.run_id == rid)
                          .order_by(phases.c.id))

    def get_checks(self, phase_id: int) -> List[Dict[str, Any]]:
        return self._rows(select(check_results)
                          .where(check_results.c.phase_id == phase_id)
                          .order_by(check_results.c.hostname, check_results.c.id))

    def get_nodes(self, phase_id: int) -> List[Dict[str, Any]]:
        return self._rows(select(node_states)
                          .where(node_states.c.phase_id == phase_id)
                          .order_by(node_states.c.hostname))

    def get_actions(self, run_id: Optional[int] = None) -> List[Dict[str, Any]]:
        rid = run_id or self.run_id
        return self._rows(select(actions).where(actions.c.run_id == rid)
                          .order_by(actions.c.id))

    def get_events(self, run_id: Optional[int] = None) -> List[Dict[str, Any]]:
        rid = run_id or self.run_id
        return self._rows(select(events).where(events.c.run_id == rid)
                          .order_by(events.c.id))

    def latest_phase(self, name: str,
                     run_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """The most recent phase of a given name in a run.

        Re-running a phase appends rather than replaces, so 'latest' is what a
        report page means by "the current state".
        """
        rid = run_id or self.run_id
        rows = self._rows(select(phases)
                          .where(phases.c.run_id == rid, phases.c.name == name)
                          .order_by(phases.c.id.desc()).limit(1))
        return rows[0] if rows else None

    def export_run(self, run_id: Optional[int] = None) -> Dict[str, Any]:
        """The whole run as JSON-serialisable data -- the phase-4 input."""
        rid = run_id or self.run_id
        run = self.get_run(rid)
        if run is None:
            return {}
        out: Dict[str, Any] = {"run": _jsonable(run), "phases": []}
        for phase in self.get_phases(rid):
            out["phases"].append({
                **_jsonable(phase),
                "nodes": [_jsonable(n) for n in self.get_nodes(phase["id"])],
                "checks": [_jsonable(c) for c in self.get_checks(phase["id"])],
            })
        out["actions"] = [_jsonable(a) for a in self.get_actions(rid)]
        out["events"] = [_jsonable(e) for e in self.get_events(rid)]
        return out

    def close(self) -> None:
        self.engine.dispose()


def _jsonable(row: Dict[str, Any]) -> Dict[str, Any]:
    """Convert datetimes to ISO strings so a row can be JSON-dumped."""
    return {k: (v.isoformat() if isinstance(v, datetime) else v)
            for k, v in row.items()}
