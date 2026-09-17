"""Phase 4 -- the consolidated report.

This phase probes nothing.  It reads the run back out of the store, renders the
detailed narrative page, and -- when configured -- posts it to the electronic
logbook.  Keeping it read-only is what makes it re-runnable: an operator can
regenerate and repost a report hours later without touching the cluster.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..checks import Status
from ..orchestrator import Orchestrator
from ..report.ecl import ECLPoster, ECLError
from .base import PhaseResult

log = logging.getLogger(__name__)

PHASE_NAME = "report"
PHASE_NUMBER = 4
PHASE_TITLE = "Recovery report"


def run(orch: Orchestrator, run_id: Optional[int] = None,
        post: Optional[bool] = None,
        html_paths: Optional[List[str]] = None) -> PhaseResult:
    """Assemble the run narrative and optionally post it to the logbook."""
    started = time.monotonic()
    result = PhaseResult(name=PHASE_NAME, number=PHASE_NUMBER, title=PHASE_TITLE,
                         started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    rid = run_id or orch.store.run_id or orch.store.latest_run_id()
    if rid is None:
        result.status = Status.UNKNOWN
        result.summary = "no run found to report on"
        return result

    export = orch.store.export_run(rid)
    if not export:
        result.status = Status.UNKNOWN
        result.summary = f"run {rid} is not in the store"
        return result

    orch.store.start_phase(PHASE_NAME, PHASE_NUMBER)
    narrative = build_narrative(export)
    result.data = {"run_id": rid, "narrative": narrative, "export": export}
    result.status = _overall(export)
    result.summary = narrative["headline"]

    should_post = orch.settings.get("ecl.enabled", False) if post is None else post
    if should_post:
        try:
            poster = ECLPoster(orch.settings, orch.vault)
            entry = poster.post(narrative, attachments=html_paths or [])
            result.data["ecl"] = entry
            result.notes.append(f"posted to the logbook: {entry.get('url') or 'ok'}")
            orch.store.record_event(f"posted recovery report to the ECL: {entry}")
        except ECLError as exc:
            result.notes.append(f"logbook posting failed: {exc}")
            log.error("ECL posting failed: %s", exc)
    else:
        result.notes.append("logbook posting not enabled (ecl.enabled: false)")

    orch.store.finish_phase("complete", result.summary,
                            {"run_id": rid, "headline": narrative["headline"]})
    result.duration = time.monotonic() - started
    result.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return result


def _overall(export: Dict[str, Any]) -> Status:
    order = {s.value: s for s in Status}
    worst = Status.OK
    for phase in export.get("phases", []):
        for node in phase.get("nodes", []):
            status = order.get(node.get("status", "unknown"), Status.UNKNOWN)
            if status.rank > worst.rank:
                worst = status
    return worst


def build_narrative(export: Dict[str, Any]) -> Dict[str, Any]:
    """Turn the stored run into the structure the report and the ECL entry share.

    The shape is chosen for a reader who was not present: what was done, what
    is still wrong, and what has to happen next -- in that order.  Everything
    is derived from stored rows, so the narrative cannot disagree with the
    evidence tables beside it.
    """
    run = export.get("run", {})
    phases = export.get("phases", [])
    actions = export.get("actions", [])

    node_status: Dict[str, str] = {}
    node_class: Dict[str, str] = {}
    for phase in phases:
        for node in phase.get("nodes", []):
            node_status[node["hostname"]] = node["status"]
            node_class[node["hostname"]] = node.get("node_class", "other")

    failed = sorted(h for h, s in node_status.items() if s == "fail")
    unknown = sorted(h for h, s in node_status.items() if s == "unknown")
    warned = sorted(h for h, s in node_status.items() if s == "warn")
    healthy = sorted(h for h, s in node_status.items() if s == "ok")

    powered = [a for a in actions if a.get("action") == "power_on"
               and a.get("outcome") == "power_on"]
    refused = [a for a in actions if a.get("outcome") in ("refused", "no_bmc",
                                                          "unavailable", "failed")]

    outstanding: List[Dict[str, str]] = []
    for phase in phases:
        for check in phase.get("checks", []):
            if check.get("status") in ("fail", "unknown"):
                outstanding.append({
                    "node": check["hostname"],
                    "check": check["check_id"],
                    "status": check["status"],
                    "summary": check.get("summary", ""),
                    "phase": phase.get("name", ""),
                })

    total = len(node_status)
    if total and not failed and not unknown:
        headline = f"All {total} node(s) verified healthy"
        if warned:
            headline += f" ({len(warned)} with warnings)"
    elif total:
        headline = (f"{len(healthy)}/{total} node(s) healthy; "
                    f"{len(failed)} failed, {len(unknown)} unreachable")
    else:
        headline = "No nodes were assessed"

    return {
        "headline": headline,
        "run": run,
        "dry_run": bool(run.get("dry_run")),
        "phases": [{"name": p.get("name"), "number": p.get("number"),
                    "status": p.get("status"), "summary": p.get("summary"),
                    "started_at": p.get("started_at"),
                    "finished_at": p.get("finished_at"),
                    "node_count": len(p.get("nodes", []))}
                   for p in phases],
        "counts": {"total": total, "ok": len(healthy), "warn": len(warned),
                   "fail": len(failed), "unknown": len(unknown)},
        "healthy": healthy,
        "warned": warned,
        "failed": failed,
        "unreachable": unknown,
        "node_class": node_class,
        "powered_on": [a["hostname"] for a in powered],
        "power_problems": [{"node": a["hostname"], "outcome": a["outcome"],
                            "detail": a.get("detail", "")} for a in refused],
        "outstanding": outstanding,
        "events": export.get("events", []),
        "next_steps": _next_steps(failed, unknown, outstanding, refused),
    }


def _next_steps(failed: List[str], unknown: List[str],
                outstanding: List[Dict[str, str]],
                refused: List[Dict[str, Any]]) -> List[str]:
    """Concrete follow-ups, derived from what actually failed."""
    steps: List[str] = []
    if unknown:
        steps.append(
            f"{len(unknown)} node(s) never answered: {', '.join(unknown[:6])}"
            + (" ..." if len(unknown) > 6 else "")
            + ". Check BMC power state from a gateway, then physical power and "
              "network at the rack.")
    by_check: Dict[str, List[str]] = {}
    for item in outstanding:
        by_check.setdefault(item["check"], []).append(item["node"])
    for check_id, nodes in sorted(by_check.items(), key=lambda kv: -len(kv[1])):
        if len(nodes) >= 3:
            steps.append(
                f"{check_id} failed on {len(nodes)} nodes -- a fault this wide is "
                f"usually one shared cause (a server, a switch, or a mount that "
                f"has not come back), not {len(nodes)} separate ones.")
    if refused:
        steps.append(
            f"{len(refused)} power action(s) did not complete "
            f"({', '.join(sorted({a['outcome'] for a in refused}))}); review "
            f"whether they need to be done by hand.")
    if not steps and not failed:
        steps.append("No follow-up required: every check passed.")
    return steps
