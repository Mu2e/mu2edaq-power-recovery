"""Phase 1 -- survey the cluster.  Read-only, by construction.

Project-Description.md is explicit that this phase takes no corrective action,
so it runs only registered checks; nothing here calls an IPMI verb that changes
state, and the IPMI client is in dry-run mode for the whole phase regardless of
--execute.

The checks are ordered to answer the operator's questions in the order they
actually ask them:

  1. are the gateways up, and can I log in to them?
  2. do the gateways have the disks and routing they should?
  3. from a gateway, what is reachable, and what does the BMC say about the
     rest?
  4. for anything that answers, what state is it in?
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from ..checks import Status
from ..orchestrator import NodeAssessment, Orchestrator, group_by_class
from ..topology import Node
from .base import PhaseResult, overall_status

log = logging.getLogger(__name__)

PHASE_NAME = "assess"
PHASE_NUMBER = 1
PHASE_TITLE = "Initial state"


def run(orch: Orchestrator, nodes: Optional[Sequence[Node]] = None,
        progress: Optional[Any] = None) -> PhaseResult:
    """Survey every node and return the initial-state result."""
    started = time.monotonic()
    result = PhaseResult(name=PHASE_NAME, number=PHASE_NUMBER, title=PHASE_TITLE,
                         started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    targets = list(nodes if nodes is not None else orch.nodes())
    if not targets:
        result.status = Status.UNKNOWN
        result.summary = "no nodes are configured for the selected locations"
        result.notes.append(
            "check topology.locations, and note that the MC-1 inventory in "
            "config/topology.yaml is still empty")
        return result

    orch.store.start_phase(PHASE_NAME, PHASE_NUMBER)
    orch.store.record_event(f"phase 1 (assess) started over {len(targets)} node(s)")

    # --- 1: the gateways, first and alone ---------------------------------
    gateways = [n for n in targets if n.node_class == "gateway"]
    others = [n for n in targets if n.node_class != "gateway"]

    gateway_results: List[NodeAssessment] = []
    if gateways:
        log.info("assessing %d gateway(s) before anything else", len(gateways))
        gateway_results = orch.assess_nodes(gateways, concurrency=len(gateways),
                                            progress=progress)
        result.assessments.extend(gateway_results)

        usable = [a for a in gateway_results
                  if not any(r.check_id == "ssh.login" and r.status.is_bad
                             for r in a.results)]
        if not usable:
            # Everything downstream is probed *from* a gateway, so this is the
            # one failure that genuinely stops the phase rather than just
            # colouring a table red.
            result.status = Status.FAIL
            result.summary = ("no gateway could be logged in to -- nothing "
                              "behind them can be assessed")
            result.notes.append(
                "check that the gateways have power and that your Kerberos "
                "ticket is valid; every other check in this phase depends on "
                "reaching a node through one of them")
            orch.record(result.assessments)
            orch.store.finish_phase("failed", result.summary, {"gateways_down": True})
            result.duration = time.monotonic() - started
            result.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            return result
        result.notes.append(
            f"{len(usable)} of {len(gateways)} gateway(s) usable: "
            + ", ".join(a.node.short for a in usable))

    # --- 2: everything else, in parallel ----------------------------------
    if others:
        result.assessments.extend(orch.assess_nodes(others, progress=progress))

    # --- 3: roll up -------------------------------------------------------
    result.status = overall_status(result.assessments)
    counts = result.counts
    result.summary = (f"{counts['total']} node(s): {counts['ok']} ok, "
                      f"{counts['warn']} warning, {counts['fail']} failed, "
                      f"{counts['unknown']} unreachable")
    result.data = {
        "power_states": _power_summary(result.assessments),
        "by_class": {cls: [a.node.hostname for a in group]
                     for cls, group in group_by_class(result.assessments).items()},
        "unreachable": [a.node.hostname for a in result.assessments
                        if a.status is Status.UNKNOWN],
        "powered_off": [a.node.hostname for a in result.assessments
                        if a.power_state == "off"],
        "ready_for_phase2": _ready_for_phase2(result.assessments),
    }
    result.notes.extend(orch.notes)

    orch.record(result.assessments)
    orch.store.finish_phase(
        "complete" if result.status is not Status.FAIL else "complete_with_failures",
        result.summary, result.data)
    orch.store.record_event(f"phase 1 complete: {result.summary}")

    result.duration = time.monotonic() - started
    result.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return result


def _power_summary(assessments: Sequence[NodeAssessment]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for a in assessments:
        counts[a.power_state or "unread"] = counts.get(a.power_state or "unread", 0) + 1
    return counts


def _ready_for_phase2(assessments: Sequence[NodeAssessment]) -> Dict[str, Any]:
    """Whether phase 2 has what it needs: a usable gateway and BMC answers.

    Surfacing this from phase 1 is the point of running phase 1 separately --
    the operator finds out that the BMC network is unreachable before they
    commit to a power-on sequence, not halfway through one.
    """
    gateways = [a for a in assessments if a.node.node_class == "gateway"]
    gateway_ok = any(a.status is not Status.UNKNOWN and
                     all(r.status is not Status.FAIL for r in a.results
                         if r.check_id in ("ssh.login", "ssh.login_root"))
                     for a in gateways)
    bmc_answered = sum(1 for a in assessments
                       if a.power_state in ("on", "off"))
    bmc_silent = [a.node.hostname for a in assessments
                  if a.power_state is None and a.node.ipmi_host]
    return {
        "gateway_usable": gateway_ok,
        "bmc_answered": bmc_answered,
        "bmc_silent": bmc_silent,
        "ready": bool(gateway_ok and bmc_answered),
    }
