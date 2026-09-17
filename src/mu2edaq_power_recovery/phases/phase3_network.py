"""Phase 3 -- inter-node connectivity.

Phases 1 and 2 prove each node is reachable from a gateway.  This one proves
the nodes can reach each other, which is a different and weaker property: a
switch that came back with a VLAN missing, a port in the wrong group, or jumbo
frames off on one uplink leaves every node individually healthy and the DAQ
unable to move data.

Only nodes that passed phase 2 are probed by default -- probing a node that is
known to be down adds a full ping timeout per pair and tells the operator
nothing they do not already know.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from ..checks import Status
from ..checks.mesh import MeshProbe, MeshResult
from ..orchestrator import Orchestrator
from ..topology import Node
from .base import PhaseResult

log = logging.getLogger(__name__)

PHASE_NAME = "network"
PHASE_NUMBER = 3
PHASE_TITLE = "Network connectivity"


def run(orch: Orchestrator, nodes: Optional[Sequence[Node]] = None,
        include_failed: bool = False) -> PhaseResult:
    """Probe the connectivity mesh across the configured networks."""
    started = time.monotonic()
    result = PhaseResult(name=PHASE_NAME, number=PHASE_NUMBER, title=PHASE_TITLE,
                         started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    targets = list(nodes if nodes is not None else orch.nodes())
    if not include_failed:
        targets, excluded = _reachable_only(orch, targets)
        if excluded:
            result.notes.append(
                f"{len(excluded)} node(s) excluded because they did not pass an "
                f"earlier phase: {', '.join(sorted(excluded)[:8])}"
                + (" ..." if len(excluded) > 8 else "")
                + ". Use --include-failed to probe them anyway.")
    if not targets:
        result.status = Status.UNKNOWN
        result.summary = "no nodes available to probe"
        return result

    orch.store.start_phase(PHASE_NAME, PHASE_NUMBER)
    orch.store.record_event(f"phase 3 (network) started over {len(targets)} node(s)")

    probe = MeshProbe(orch.ssh_factory, orch.checks_config,
                      max_workers=int(orch.settings.get("ssh.max_sessions", 16)))
    mesh_results: List[MeshResult] = probe.run_all(targets)

    statuses = [m.status for m in mesh_results]
    result.status = max(statuses, key=lambda s: s.rank) if statuses else Status.UNKNOWN
    result.summary = "; ".join(
        f"{m.network}: {len(m.edges) - len(m.failures)}/{len(m.edges)} paths ok"
        + (f", {len(m.mtu_failures)} jumbo-frame failure(s)" if m.mtu_failures else "")
        for m in mesh_results) or "no networks probed"
    result.data = {
        "networks": [m.as_dict() for m in mesh_results],
        "isolated_nodes": sorted({n for m in mesh_results for n in m.isolated_nodes()}),
        "unreachable_targets": sorted({t for m in mesh_results
                                       for t in m.unreachable_targets()}),
    }

    # An interpretation, not just a matrix: a node that reaches nothing and a
    # target nobody reaches have different causes, and saying which is which
    # here saves the operator reading a 900-cell table to work it out.
    for m in mesh_results:
        # Aggregated, not one note per host: a fabric-wide failure would
        # otherwise produce fifty identical lines and bury the one diagnosis
        # that differs.  Counts plus a sample is what an operator can act on.
        isolated = m.isolated_nodes()
        if isolated:
            result.notes.append(
                f"{m.network}: {len(isolated)} node(s) reached nothing at all "
                f"({_sample(isolated)}). With this many, look for one shared "
                f"cause -- the segment's switch or uplink -- before looking at "
                f"individual NICs."
                if len(isolated) > 2 else
                f"{m.network}: {_sample(isolated)} reached nothing -- look at "
                f"that node's interface and its switch port, not at the fabric")
        one_way = [h for h in m.unreachable_targets() if h not in isolated]
        if one_way:
            result.notes.append(
                f"{m.network}: {len(one_way)} host(s) could not be reached by "
                f"anyone although they probe out themselves ({_sample(one_way)}) "
                f"-- suspect a one-way path, an ARP problem, or a host firewall")
        if m.mtu_failures and not m.failures:
            result.notes.append(
                f"{m.network}: connectivity is fine but {len(m.mtu_failures)} "
                f"path(s) cannot carry a full jumbo frame -- a switch or an "
                f"interface came back with the wrong MTU")

    orch.store.finish_phase(
        "complete" if result.status is not Status.FAIL else "complete_with_failures",
        result.summary, result.data)
    orch.store.record_event(f"phase 3 complete: {result.summary}")

    result.duration = time.monotonic() - started
    result.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return result


def _sample(hosts: Sequence[str], limit: int = 5) -> str:
    """A short, readable subset of a host list for a note."""
    short = [h.split(".")[0] for h in hosts]
    if len(short) <= limit:
        return ", ".join(short)
    return ", ".join(short[:limit]) + f", and {len(short) - limit} more"


def _reachable_only(orch: Orchestrator, nodes: Sequence[Node]):
    """Split *nodes* into those that passed an earlier phase and those that did not.

    Reads the run store rather than re-probing: phases 1 and 2 already
    established this, and the whole point of persisting them is not to ask
    twice.  With no earlier phase recorded, every node is included -- phase 3
    must be usable on its own.
    """
    verdicts: Dict[str, str] = {}
    for phase in orch.store.get_phases():
        for row in orch.store.get_nodes(phase["id"]):
            verdicts[row["hostname"]] = row["status"]
    if not verdicts:
        return list(nodes), []
    keep = [n for n in nodes if verdicts.get(n.hostname, "ok") not in ("fail", "unknown")]
    dropped = [n.hostname for n in nodes if n not in keep]
    return keep, dropped
