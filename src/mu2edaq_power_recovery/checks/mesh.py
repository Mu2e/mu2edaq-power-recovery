"""Phase 3: inter-node connectivity across the DAQ network segments.

Phases 1 and 2 establish that each node is reachable *from the gateway*.  That
is not the same as the nodes being able to reach each other: after a power
event a switch can come back with a VLAN missing, a port in the wrong group,
or jumbo frames disabled on one uplink, and every node still passes its own
health checks while the DAQ cannot move data.

This module probes node-to-node.  On the data network -- the one the DAQ
actually uses, and small enough to afford it -- the probe is a full mesh.  On
the lab and IPMI networks a full mesh would be O(N^2) ssh sessions for little
information, so each node is probed against a small set of anchors instead.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..transport.base import TransportError
from .base import Status
from .parsers import parse_ping
from .reachability import _ping_command

log = logging.getLogger(__name__)


@dataclass
class MeshEdge:
    """One source -> target probe on one network."""

    source: str
    target: str
    network: str
    ok: bool
    loss_pct: float = 100.0
    rtt_avg_ms: Optional[float] = None
    mtu_ok: Optional[bool] = None
    detail: str = ""

    @property
    def status(self) -> Status:
        if not self.ok:
            return Status.FAIL
        if self.mtu_ok is False:
            return Status.WARN
        if self.loss_pct > 0:
            return Status.WARN
        return Status.OK

    def as_dict(self) -> Dict[str, Any]:
        return {"source": self.source, "target": self.target, "network": self.network,
                "ok": self.ok, "loss_pct": self.loss_pct, "rtt_avg_ms": self.rtt_avg_ms,
                "mtu_ok": self.mtu_ok, "status": self.status.value,
                "detail": self.detail}


@dataclass
class MeshResult:
    """Every edge probed on one network, plus the derived summary."""

    network: str
    full_mesh: bool
    edges: List[MeshEdge] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)
    targets: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)

    @property
    def failures(self) -> List[MeshEdge]:
        return [e for e in self.edges if not e.ok]

    @property
    def mtu_failures(self) -> List[MeshEdge]:
        return [e for e in self.edges if e.mtu_ok is False]

    @property
    def status(self) -> Status:
        if not self.edges:
            return Status.UNKNOWN
        if self.failures:
            return Status.FAIL
        if self.mtu_failures:
            return Status.WARN
        return Status.OK

    def isolated_nodes(self) -> List[str]:
        """Sources that failed to reach *every* one of their targets.

        Worth separating from the raw failure list: one node that reaches
        nothing is a node problem, whereas one target that nobody reaches is a
        switch-port problem, and the operator should not have to infer which
        from a wall of failed pairs.
        """
        by_source: Dict[str, List[MeshEdge]] = {}
        for edge in self.edges:
            by_source.setdefault(edge.source, []).append(edge)
        return sorted(src for src, edges in by_source.items()
                      if edges and not any(e.ok for e in edges))

    def unreachable_targets(self) -> List[str]:
        """Targets that no source could reach."""
        by_target: Dict[str, List[MeshEdge]] = {}
        for edge in self.edges:
            by_target.setdefault(edge.target, []).append(edge)
        return sorted(tgt for tgt, edges in by_target.items()
                      if edges and not any(e.ok for e in edges))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "network": self.network,
            "full_mesh": self.full_mesh,
            "status": self.status.value,
            "edge_count": len(self.edges),
            "failures": [e.as_dict() for e in self.failures],
            "mtu_failures": [e.as_dict() for e in self.mtu_failures],
            "isolated_nodes": self.isolated_nodes(),
            "unreachable_targets": self.unreachable_targets(),
            "sources": list(self.sources),
            "targets": list(self.targets),
            "skipped": list(self.skipped),
            "edges": [e.as_dict() for e in self.edges],
        }


class MeshProbe:
    """Runs the phase-3 probes.

    One SSH session per (source, batch of targets): the whole probe list for a
    source is packed into a single remote shell invocation.  A full mesh over
    30 data-network nodes is 870 pairs, and doing that as 870 ssh sessions
    would take longer than the rest of the recovery put together.
    """

    def __init__(self, ssh_factory: Any, checks_config: Dict[str, Any],
                 max_workers: int = 8):
        self.ssh_factory = ssh_factory
        self.config = checks_config.get("mesh", {}) or {}
        self.thresholds = checks_config.get("thresholds", {}) or {}
        self.max_workers = max_workers

    # -- target selection --------------------------------------------------

    def _targets(self, nodes: Sequence[Any], network: str,
                 full_mesh: bool) -> List[Any]:
        if full_mesh:
            return [n for n in nodes if n.has_network(network)]
        anchors = set(self.config.get("anchors", []) or [])
        selected = [n for n in nodes
                    if n.has_network(network) and n.hostname in anchors]
        # An anchor that is not in this run's node set would leave the probe
        # with nothing to aim at; fall back to the first few nodes so the
        # network still gets tested.
        if not selected:
            selected = [n for n in nodes if n.has_network(network)][:3]
        return selected

    # -- probing -----------------------------------------------------------

    def _script(self, targets: Sequence[str], network: str,
                mtu_probe: bool) -> str:
        """One shell script probing every target, printing parseable blocks.

        Each target's output is bracketed by markers so the results can be
        split apart reliably even when ping's own output format varies between
        the RHEL and AlmaLinux images in the cluster.
        """
        count = int(self.config.get("count", self.thresholds.get("ping_count", 3)))
        timeout = int(self.config.get("timeout_s", self.thresholds.get("ping_timeout_s", 5)))
        payload = int(self.config.get("mtu_payload_bytes", 8972))
        lines = []
        for target in targets:
            lines.append(f"echo '===BEGIN {target}==='")
            lines.append(_ping_command(target, count, timeout) + " 2>&1 || true")
            if mtu_probe:
                lines.append(f"echo '---MTU {target}---'")
                # One large, do-not-fragment packet.  Success means every hop
                # on the path carries a full jumbo frame.
                lines.append(_ping_command(target, 1, timeout, payload=payload)
                             + " 2>&1 || true")
            lines.append(f"echo '===END {target}==='")
        return "\n".join(lines)

    @staticmethod
    def _split(output: str, target: str) -> Tuple[str, str]:
        """Return (ping block, mtu block) for one target."""
        begin, end = f"===BEGIN {target}===", f"===END {target}==="
        try:
            body = output.split(begin, 1)[1].split(end, 1)[0]
        except IndexError:
            return "", ""
        marker = f"---MTU {target}---"
        if marker in body:
            ping_part, mtu_part = body.split(marker, 1)
            return ping_part, mtu_part
        return body, ""

    def _probe_source(self, source: Any, targets: Sequence[Any], network: str,
                      mtu_probe: bool) -> List[MeshEdge]:
        target_names = [t.networks[network] for t in targets
                        if t.hostname != source.hostname]
        if not target_names:
            return []
        script = self._script(target_names, network, mtu_probe)
        transport = self.ssh_factory.for_node(source)
        budget = 20 + len(target_names) * (
            int(self.config.get("count", 3)) * int(self.config.get("timeout_s", 5))
            + (5 if mtu_probe else 0))
        try:
            res = transport.run(["/bin/sh", "-c", script], timeout=budget)
        except TransportError as exc:
            # The source itself is unreachable: every edge from it is unknown,
            # not failed.  Reported as one edge per target so the matrix stays
            # rectangular and the report can colour the whole row.
            return [MeshEdge(source=source.hostname, target=name, network=network,
                             ok=False, detail=f"source unreachable: {exc}")
                    for name in target_names]

        edges: List[MeshEdge] = []
        for name in target_names:
            ping_block, mtu_block = self._split(res.output, name)
            stats = parse_ping(ping_block)
            mtu_ok: Optional[bool] = None
            if mtu_probe and mtu_block:
                mtu_ok = parse_ping(mtu_block).alive
            edges.append(MeshEdge(
                source=source.hostname, target=name, network=network,
                ok=stats.alive, loss_pct=stats.loss_pct,
                rtt_avg_ms=stats.rtt_avg_ms, mtu_ok=mtu_ok,
                detail="" if stats.alive else ping_block.strip()[-200:],
            ))
        return edges

    def run(self, nodes: Sequence[Any], network: str,
            full_mesh: bool = True, mtu_probe: bool = False) -> MeshResult:
        """Probe *network* across *nodes*."""
        sources = [n for n in nodes if n.has_network(network)]
        skipped = [n.hostname for n in nodes if not n.has_network(network)]
        targets = self._targets(nodes, network, full_mesh)
        out = MeshResult(network=network, full_mesh=full_mesh,
                         sources=[n.hostname for n in sources],
                         targets=[n.hostname for n in targets],
                         skipped=skipped)
        if not sources or not targets:
            log.warning("mesh %s: %d source(s), %d target(s) -- nothing to probe",
                        network, len(sources), len(targets))
            return out

        log.info("mesh %s: probing %d source(s) against %d target(s)%s",
                 network, len(sources), len(targets),
                 " with MTU probe" if mtu_probe else "")
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(self._probe_source, src, targets, network,
                                   mtu_probe): src for src in sources}
            for future in as_completed(futures):
                src = futures[future]
                try:
                    out.edges.extend(future.result())
                except Exception as exc:  # noqa: BLE001 - one bad source must
                    # not abandon the other 40; record it and carry on.
                    log.exception("mesh probe from %s failed", src.hostname)
                    out.edges.append(MeshEdge(
                        source=src.hostname, target="(all)", network=network,
                        ok=False, detail=f"probe raised {type(exc).__name__}: {exc}"))
        out.edges.sort(key=lambda e: (e.source, e.target))
        return out

    def run_all(self, nodes: Sequence[Any]) -> List[MeshResult]:
        """Probe every network listed in checks.yaml's mesh section."""
        results: List[MeshResult] = []
        for entry in self.config.get("networks", []) or []:
            results.append(self.run(
                nodes,
                network=entry["name"],
                full_mesh=bool(entry.get("full_mesh", True)),
                mtu_probe=bool(entry.get("mtu_probe", False)),
            ))
        return results
