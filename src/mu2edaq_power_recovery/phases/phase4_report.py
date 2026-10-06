"""Phase 4 -- the consolidated report.

This phase probes nothing.  It reads a *finished* run back out of the store and
assembles the narrative the detail page and the logbook entry share.  It is
split in two so that nothing is rendered or posted from a half-written record:

1. :func:`assemble` -- records the report phase against the selected run,
   then builds the narrative from the final :meth:`RunStore.export_run`.  The
   driver calls it only after the run has been finished (status and
   ``finished_at`` set), so the narrative never says "still running".
2. :func:`post` -- after the driver has rendered the run's report bundle,
   posts the narrative to the electronic logbook with that bundle's pages
   attached, and records the outcome as an event on the same run.

Keeping it read-only is what makes it re-runnable: an operator can regenerate
and repost a report hours later without touching the cluster, and without a
Kerberos ticket unless the logbook post itself needs Vault.

**Current state is reconciled, not accumulated.**  Re-running a phase appends
rows rather than replacing them, so a node can fail a check in phase 1 and pass
the same check when phase 2 re-checks it.  :func:`reconcile` takes the newest
result per ``(hostname, check_id)``; a superseded failure is listed as
*resolved* (it stays in the evidence and the timeline) and only failures that
are still current are *outstanding*.  Phase 2's profiles re-check subsets, so a
phase-1 failure of a check phase 2 never ran again stays outstanding.  The
headline, counts, verdict, next steps, logbook text and the driver's exit code
all come from that one reconciled table.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..checks import Status
from ..checks.base import rollup
from ..report.ecl import ECLPoster
from .base import PhaseResult

log = logging.getLogger(__name__)

PHASE_NAME = "report"
PHASE_NUMBER = 4
PHASE_TITLE = "Recovery report"

_BAD = ("fail", "unknown")
_STATUS = {s.value: s for s in Status}


def _status(value: Any) -> Status:
    return _STATUS.get(str(value), Status.UNKNOWN)


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------


def assemble(store: Any, run_id: int, record: bool = True) -> PhaseResult:
    """Build the report for the stored run *run_id*.

    With *record* (the phase-4 path), a ``report`` phase row and an event are
    written against *run_id* -- explicitly, not against whatever run the store
    currently points at -- and the narrative is then rebuilt from the export
    that includes that finished row. With *record* false nothing is written:
    the driver uses that for the index page of a run that did not ask for
    phase 4.
    """
    started = time.monotonic()
    result = PhaseResult(name=PHASE_NAME, number=PHASE_NUMBER, title=PHASE_TITLE,
                         started_at=_now())
    export = store.export_run(run_id) if run_id is not None else {}
    if not export:
        result.status = Status.UNKNOWN
        result.summary = f"run {run_id} is not in the store"
        return result

    if record:
        phase_id = store.start_phase(PHASE_NAME, PHASE_NUMBER, run_id=run_id)
        narrative = build_narrative(export)
        status = _status(narrative["status"])
        store.finish_phase("complete", narrative["headline"], {
            "run_id": run_id,
            "headline": narrative["headline"],
            "_result": {"status": status.value, "title": PHASE_TITLE,
                        "notes": [], "counts": narrative["counts"],
                        "duration": round(time.monotonic() - started, 2)},
        }, phase_id=phase_id)
        store.record_event(f"phase 4: report assembled for run {run_id}: "
                           f"{narrative['headline']}", run_id=run_id)
        export = store.export_run(run_id)

    narrative = build_narrative(export)
    result.data = {"run_id": run_id, "narrative": narrative, "export": export}
    result.status = _status(narrative["status"])
    result.summary = narrative["headline"]
    result.duration = time.monotonic() - started
    result.finished_at = _now()
    return result


def run(orch: Any, run_id: Optional[int] = None) -> PhaseResult:
    """Assemble the report for *run_id* (default: the current or latest run).

    Posting is deliberately not part of this: see :func:`post`, which needs
    the rendered bundle to attach.
    """
    rid = run_id or orch.store.run_id or orch.store.latest_run_id()
    if rid is None:
        return PhaseResult(name=PHASE_NAME, number=PHASE_NUMBER,
                           title=PHASE_TITLE, status=Status.UNKNOWN,
                           summary="no run found to report on")
    return assemble(orch.store, rid)


# ---------------------------------------------------------------------------
# posting
# ---------------------------------------------------------------------------


def make_vault(orch: Any) -> Any:
    """The Vault client for the ECL credentials, created only when posting.

    A report-only run never calls prepare_credentials(), so there is usually
    no client yet; one made by a full run is reused.
    """
    if getattr(orch, "vault", None) is not None:
        return orch.vault
    from ..creds import VaultCredentials
    return VaultCredentials(orch.settings, local=orch.local)


def post(orch: Any, run_id: int, narrative: Dict[str, Any],
         attachments: Sequence[str] = (),
         vault_factory: Any = None) -> Dict[str, Any]:
    """Post *narrative* to the logbook; never raises for a logbook failure.

    Returns ``{"posted": bool, ...}`` and records the outcome as an event on
    *run_id*. A failure leaves the local report exactly as it was rendered.
    A simulated run posts nothing: the lazy Vault client would otherwise be
    the one real network contact a rehearsal makes.
    """
    if getattr(orch, "simulate", False):
        info = {"posted": False,
                "reason": "simulated run: nothing was posted to the logbook"}
        orch.store.record_event(info["reason"], run_id=run_id)
        return info
    try:
        vault = (vault_factory or make_vault)(orch)
        entry = ECLPoster(orch.settings, vault).post(narrative,
                                                     attachments=attachments)
    except Exception as exc:  # noqa: BLE001 - ECLError, VaultError, anything
        log.error("ECL posting failed: %s", exc)
        orch.store.record_event(f"logbook posting failed for run {run_id}: {exc}",
                                level="error", run_id=run_id)
        return {"posted": False, "error": str(exc)}
    orch.store.record_event(
        f"posted the recovery report for run {run_id} to the ECL "
        f"({entry.get('url') or 'ok'}; {len(entry.get('attachments') or [])} "
        f"attachment(s))", run_id=run_id)
    return {"posted": True, **entry}


# ---------------------------------------------------------------------------
# reconciliation
# ---------------------------------------------------------------------------


def _unreachable(node_row: Dict[str, Any]) -> bool:
    data = node_row.get("data") or {}
    if "unreachable" in data:
        return bool(data["unreachable"])
    # Rows written before the flag was stored: the roll-up was UNKNOWN.
    return node_row.get("status") == "unknown"


def reconcile(export: Dict[str, Any]) -> Dict[str, Any]:
    """The run's current state: newest result per (hostname, check_id).

    Returns ``current`` (key -> check row plus its ``phase``), ``resolved``
    (failures a later result superseded with a good one), ``node_status``
    (hostname -> status value: the roll-up of that node's *current* checks;
    when its latest assessment could not reach it, the worse of UNKNOWN and
    that roll-up, so a current FAIL stays FAIL), ``node_class``,
    ``network`` (the latest phase-3 verdict, or None) and ``status`` (the
    run's overall verdict).
    """
    current: Dict[Tuple[str, str], Dict[str, Any]] = {}
    superseded_bad: Dict[Tuple[str, str], Dict[str, Any]] = {}
    latest_node: Dict[str, Dict[str, Any]] = {}
    network: Optional[Dict[str, Any]] = None

    for phase in export.get("phases", []):
        name = phase.get("name", "")
        if name == PHASE_NAME:
            continue
        if name == "network":
            network = phase
        for node in phase.get("nodes", []):
            latest_node[node["hostname"]] = node
        for check in sorted(phase.get("checks", []), key=lambda c: c.get("id") or 0):
            key = (check["hostname"], check["check_id"])
            previous = current.get(key)
            if previous is not None and previous.get("status") in _BAD:
                superseded_bad[key] = previous
            current[key] = {**check, "phase": name}

    resolved = []
    for key, old in sorted(superseded_bad.items()):
        now = current[key]
        if now.get("status") in _BAD:
            continue
        resolved.append({"node": key[0], "check": key[1],
                         "status": old.get("status"),
                         "summary": old.get("summary") or "",
                         "phase": old.get("phase", ""),
                         "resolved_by": now.get("phase", ""),
                         "current_status": now.get("status")})

    by_host: Dict[str, List[Status]] = {}
    for (host, _check), row in current.items():
        by_host.setdefault(host, []).append(_status(row.get("status")))
    node_status: Dict[str, str] = {}
    for host in sorted(set(latest_node) | set(by_host)):
        row = latest_node.get(host)
        if row is not None and _unreachable(row):
            # Unreachable now says "we could not look"; it does not erase a
            # failure that nothing has superseded.  Take the worse of the two
            # (FAIL outranks UNKNOWN), so a node that failed a check and then
            # stopped answering is still counted as failed.
            node_status[host] = rollup(by_host.get(host, [])
                                       + [Status.UNKNOWN]).value
        elif by_host.get(host):
            node_status[host] = rollup(by_host[host]).value
        else:
            node_status[host] = (row or {}).get("status", Status.UNKNOWN.value)

    network_status: Optional[Dict[str, Any]] = None
    if network is not None:
        data = network.get("data") or {}
        stored = (data.get("_result") or {}).get("status")
        if stored is None:
            nets = data.get("networks") or []
            stored = (max((_status(n.get("status")) for n in nets),
                          key=lambda s: s.rank).value if nets
                      else Status.UNKNOWN.value)
        network_status = {"status": stored, "summary": network.get("summary") or ""}

    verdicts = [_status(s) for s in node_status.values()]
    if network_status is not None:
        verdicts.append(_status(network_status["status"]))
    overall = rollup(verdicts) if verdicts else Status.UNKNOWN
    if overall is Status.SKIP:
        overall = Status.OK

    return {
        "current": current,
        "resolved": resolved,
        "node_status": node_status,
        "node_class": {h: r.get("node_class", "other") for h, r in latest_node.items()},
        "network": network_status,
        "status": overall,
    }


def overall_status(export: Dict[str, Any]) -> Status:
    """The run's reconciled verdict -- what the exit status is built from."""
    return reconcile(export)["status"]


def build_narrative(export: Dict[str, Any]) -> Dict[str, Any]:
    """Turn the stored run into the structure the report and the ECL entry share.

    The shape is chosen for a reader who was not present: what was done, what
    is still wrong, and what has to happen next -- in that order.  Everything
    is derived from stored rows through :func:`reconcile`, so the narrative
    cannot disagree with itself or with the evidence tables beside it.
    """
    run = export.get("run", {})
    phases = export.get("phases", [])
    actions = export.get("actions", [])
    state = reconcile(export)
    node_status = state["node_status"]

    failed = sorted(h for h, s in node_status.items() if s == "fail")
    unknown = sorted(h for h, s in node_status.items() if s == "unknown")
    warned = sorted(h for h, s in node_status.items() if s == "warn")
    healthy = sorted(h for h, s in node_status.items() if s == "ok")
    skipped = sorted(h for h, s in node_status.items() if s == "skip")

    powered = [a for a in actions if a.get("action") == "power_on"
               and a.get("outcome") == "power_on"]
    refused = [a for a in actions if a.get("outcome") in ("refused", "no_bmc",
                                                          "unavailable", "failed",
                                                          "credentials_refused")]

    outstanding: List[Dict[str, str]] = []
    for (host, check_id), row in sorted(state["current"].items()):
        if row.get("status") in _BAD:
            outstanding.append({
                "node": host,
                "check": check_id,
                "status": row["status"],
                "summary": row.get("summary") or "",
                "phase": row.get("phase", ""),
            })

    network = state["network"]
    network_bad = network is not None and _status(network["status"]).is_bad

    total = len(node_status)
    if total and not failed and not unknown:
        headline = f"All {total} node(s) verified healthy"
        if warned:
            headline += f" ({len(warned)} with warnings)"
    elif total:
        headline = (f"{len(healthy)}/{total} node(s) healthy; "
                    f"{len(failed)} failed, {len(unknown)} unreachable")
    elif network is not None:
        headline = "No nodes were assessed; network checked"
    else:
        headline = "No nodes were assessed"
    if network_bad:
        headline += ("; network problems found"
                     if network["status"] == Status.FAIL.value
                     else "; some network paths could not be tested")

    return {
        "headline": headline,
        "status": state["status"].value,
        "run": run,
        "run_status": run.get("status"),
        "dry_run": bool(run.get("dry_run")),
        "phases": [{"name": p.get("name"), "number": p.get("number"),
                    "status": p.get("status"), "summary": p.get("summary"),
                    "started_at": p.get("started_at"),
                    "finished_at": p.get("finished_at"),
                    "node_count": len(p.get("nodes", []))}
                   for p in phases],
        "counts": {"total": total, "ok": len(healthy), "warn": len(warned),
                   "fail": len(failed), "unknown": len(unknown),
                   "skip": len(skipped)},
        "healthy": healthy,
        "warned": warned,
        "failed": failed,
        "unreachable": unknown,
        "node_status": node_status,
        "node_class": state["node_class"],
        "network": network,
        "powered_on": [a["hostname"] for a in powered],
        "power_problems": [{"node": a["hostname"], "outcome": a["outcome"],
                            "detail": a.get("detail", "")} for a in refused],
        "outstanding": outstanding,
        "resolved": state["resolved"],
        "events": export.get("events", []),
        "next_steps": _next_steps(failed, unknown, outstanding, refused,
                                  state["resolved"],
                                  network if network_bad else None),
    }


def _next_steps(failed: List[str], unknown: List[str],
                outstanding: List[Dict[str, str]],
                refused: List[Dict[str, Any]],
                resolved: Optional[List[Dict[str, Any]]] = None,
                network: Optional[Dict[str, Any]] = None) -> List[str]:
    """Concrete follow-ups, derived from what is still wrong."""
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
    if network is not None and network["status"] == Status.FAIL.value:
        steps.append(f"Phase 3 found network problems ({network['summary']}); "
                     f"see the network page.")
    elif network is not None:
        steps.append(f"Phase 3 could not test every path ({network['summary']}); "
                     f"untested paths are unknown, not failed -- see the "
                     f"network page.")
    if refused:
        steps.append(
            f"{len(refused)} power action(s) did not complete "
            f"({', '.join(sorted({a['outcome'] for a in refused}))}); review "
            f"whether they need to be done by hand.")
    if not steps and not failed and not outstanding:
        steps.append("No follow-up required: every current check passed.")
    if resolved:
        steps.append(f"{len(resolved)} earlier failure(s) were cleared by a later "
                     f"re-check; they are listed as resolved, not outstanding.")
    return steps


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
