"""Phase 3: inter-node connectivity across the DAQ network segments.

Phases 1 and 2 establish that each node is reachable *from the gateway*.  That
is not the same as the nodes being able to reach each other: after a power
event a switch can come back with a VLAN missing, a port in the wrong group,
or jumbo frames disabled on one uplink, and every node still passes its own
health checks while the DAQ cannot move data.

This module probes node-to-node.  On the data network -- the one the DAQ
actually uses, and small enough to afford it -- the probe is a full mesh.  On
the lab network a full mesh would be O(N^2) ssh sessions for little
information, so each node is probed against a small set of anchors instead.

The IPMI network is different in kind.  A node's ``ipmi`` entry names its BMC,
a separate interface on a private subnet that the host OS has no route to;
only the gateways reach it (which is why ``ipmitool`` runs there).  So each
checks.yaml network entry names its probe *origin* -- ``nodes`` (every node on
the network probes) or ``gateways`` (each location's gateways probe) -- and its
*targets* -- ``all`` or ``anchors``.  The IPMI entry is ``origin: gateways,
targets: all``: it proves every BMC answers ICMP from the hosts that will
later drive it.

An edge carries ``tested``.  A source that could not be reached, a probe that
raised, output without the target's BEGIN/END markers, or a block in which
ping never produced its statistics summary (ping missing, not permitted, or
rejecting an option) means the path was never looked at: that edge is
UNKNOWN, not FAIL, and is excluded from the isolation analysis.  A completed
ping with no replies is FAIL, and so is a name that does not resolve or a
source with no route to it -- those are answers about the path.

With ``origin: gateways`` coverage is decided per *target*: a BMC is tested if
any gateway of its location completed a probe to it.  One dark gateway whose
partner tested every target does not make the network UNKNOWN -- the verdict
stands on the tested edges, the dark gateway is listed in
``unreachable_sources`` and a note says so.  A location with targets but no
gateway gets one untested edge per target from a pseudo-source
``(no gateway: <location>)``, so the network is UNKNOWN rather than silently
OK.
"""
from __future__ import annotations

import logging
import shlex
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Collection, Dict, List, Optional, Sequence, Tuple

from ..transport.base import TransportError
from .base import Status
from .parsers import parse_ping
from .reachability import _ping_command

log = logging.getLogger(__name__)


#: ping's messages for a name that does not resolve (iputils, older iputils,
#: BSD/macOS).
_NXDOMAIN = ("Name or service not known", "unknown host", "cannot resolve")

#: Messages ping prints *instead of* a statistics summary that are still an
#: answer about the path: the name does not resolve, or the source's routing
#: table has nothing for it.  Any other block without a summary means ping
#: itself never ran -- ``sh: ping: not found``, ``Operation not permitted``
#: without cap_net_raw, BusyBox rejecting ``-M do`` -- and the script's
#: ``|| true`` hides that exit status, so the block is all there is to go on.
_PATH_ANSWERS = _NXDOMAIN + ("Network is unreachable", "No route to host")

#: The same for the jumbo-frame probe: a local or path MTU refusal is the
#: answer the probe exists to get.
_MTU_ANSWERS = _PATH_ANSWERS + ("Message too long", "message too long",
                                "Frag needed")


@dataclass
class MeshEdge:
    """One source -> target probe on one network.

    ``tested`` is False when the probe never ran to completion for this pair
    -- the source was unreachable, the probe raised, the output lacked this
    target's markers, or ping itself never ran (no statistics summary).  Such an edge says nothing about the path and is
    UNKNOWN; only a tested edge can be FAIL.
    """

    source: str
    target: str
    network: str
    ok: bool
    loss_pct: float = 100.0
    rtt_avg_ms: Optional[float] = None
    mtu_ok: Optional[bool] = None
    detail: str = ""
    tested: bool = True

    @property
    def status(self) -> Status:
        if not self.tested:
            return Status.UNKNOWN
        if not self.ok:
            return Status.FAIL
        if self.mtu_ok is False:
            return Status.WARN
        if self.loss_pct > 0:
            return Status.WARN
        return Status.OK

    def as_dict(self) -> Dict[str, Any]:
        return {"source": self.source, "target": self.target, "network": self.network,
                "ok": self.ok, "tested": self.tested, "loss_pct": self.loss_pct,
                "rtt_avg_ms": self.rtt_avg_ms, "mtu_ok": self.mtu_ok,
                "status": self.status.value, "detail": self.detail}


#: Detail on an edge never probed because run.phase_timeout ran out. Matches
#: orchestrator.TIMEOUT_SUMMARY; not imported, to keep checks/ below it.
TIMEOUT_DETAIL = "not run: phase_timeout expired"


def _untested(source: str, target: str, network: str, detail: str) -> MeshEdge:
    return MeshEdge(source=source, target=target, network=network, ok=False,
                    tested=False, detail=detail)


@dataclass
class MeshResult:
    """Every edge probed on one network, plus the derived summary."""

    network: str
    full_mesh: bool
    edges: List[MeshEdge] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)
    targets: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    origin: str = "nodes"
    target_mode: str = "all"
    #: Plain-language notes the phase copies into its result.
    notes: List[str] = field(default_factory=list)
    #: Pseudo-sources standing for "this location has no gateway": their
    #: edges are untested, but they are not hosts that could not be reached.
    pseudo_sources: List[str] = field(default_factory=list)
    #: target name on this network -> the node's hostname (its ssh name).
    target_hosts: Dict[str, str] = field(default_factory=dict)

    @property
    def tested(self) -> List[MeshEdge]:
        return [e for e in self.edges if e.tested]

    @property
    def untested(self) -> List[MeshEdge]:
        return [e for e in self.edges if not e.tested]

    @property
    def failures(self) -> List[MeshEdge]:
        """Tested paths that did not answer.  Untested edges are not failures."""
        return [e for e in self.edges if e.tested and not e.ok]

    @property
    def mtu_failures(self) -> List[MeshEdge]:
        """Paths that answer the ordinary ping but cannot carry a jumbo frame.

        A path that lost every packet also fails the jumbo probe; counting it
        here too reported 496 "jumbo-frame failures" on a live run in which
        nine paths actually had an MTU problem.
        """
        return [e for e in self.edges if e.tested and e.ok and e.mtu_ok is False]

    def uncovered_targets(self) -> List[str]:
        """Targets with no tested edge at all -- nothing is known about them."""
        tested = {e.target for e in self.edges if e.tested}
        return sorted({e.target for e in self.edges} - tested)

    @property
    def status(self) -> Status:
        if not self.edges:
            return Status.UNKNOWN
        if self.failures:
            return Status.FAIL
        if self.origin == "gateways":
            # Per target: a BMC one gateway reached is tested, whatever its
            # location's other gateway managed.
            if self.uncovered_targets():
                return Status.UNKNOWN
        elif self.untested:
            return Status.UNKNOWN
        if self.mtu_failures:
            return Status.WARN
        return Status.OK

    def unreachable_sources(self) -> List[str]:
        """Sources none of whose edges could be tested (source down, probe raised)."""
        by_source: Dict[str, List[MeshEdge]] = {}
        for edge in self.edges:
            by_source.setdefault(edge.source, []).append(edge)
        return sorted(src for src, edges in by_source.items()
                      if edges and src not in self.pseudo_sources
                      and not any(e.tested for e in edges))

    def isolated_nodes(self) -> List[str]:
        """Sources that failed to reach *every* one of their tested targets.

        Worth separating from the raw failure list: one node that reaches
        nothing is a node problem, whereas one target that nobody reaches is a
        switch-port problem, and the operator should not have to infer which
        from a wall of failed pairs.  Untested edges are left out: a source
        that could not be logged into has not been shown to reach nothing.
        """
        by_source: Dict[str, List[MeshEdge]] = {}
        for edge in self.tested:
            by_source.setdefault(edge.source, []).append(edge)
        return sorted(src for src, edges in by_source.items()
                      if edges and not any(e.ok for e in edges))

    def unresolved_targets(self) -> List[str]:
        """Tested targets whose name did not resolve on the source.

        An inventory or DNS problem, not a network one: on the live cluster
        four teststand BMC names in the topology have no DNS entry at all.
        """
        return sorted({e.target for e in self.failures
                       if any(m in (e.detail or "") for m in _NXDOMAIN)})

    def unreachable_targets(self) -> List[str]:
        """Targets that no source reached, among the sources that were tested."""
        by_target: Dict[str, List[MeshEdge]] = {}
        for edge in self.tested:
            by_target.setdefault(edge.target, []).append(edge)
        return sorted(tgt for tgt, edges in by_target.items()
                      if edges and not any(e.ok for e in edges))

    def counts(self) -> Dict[str, int]:
        tested = self.tested
        return {
            "edges": len(self.edges),
            "tested": len(tested),
            "ok": sum(1 for e in tested if e.ok),
            "failed": len(self.failures),
            "unknown": len(self.edges) - len(tested),
            "unreachable_sources": len(self.unreachable_sources()),
            "uncovered_targets": len(self.uncovered_targets()),
        }

    def as_dict(self) -> Dict[str, Any]:
        return {
            "network": self.network,
            "full_mesh": self.full_mesh,
            "origin": self.origin,
            "target_mode": self.target_mode,
            "status": self.status.value,
            "edge_count": len(self.edges),
            "counts": self.counts(),
            "failures": [e.as_dict() for e in self.failures],
            "mtu_failures": [e.as_dict() for e in self.mtu_failures],
            "untested": [e.as_dict() for e in self.untested],
            "isolated_nodes": self.isolated_nodes(),
            "unreachable_targets": self.unreachable_targets(),
            "unreachable_sources": self.unreachable_sources(),
            "uncovered_targets": self.uncovered_targets(),
            "notes": list(self.notes),
            "sources": list(self.sources),
            "targets": list(self.targets),
            "skipped": list(self.skipped),
            "edges": [e.as_dict() for e in self.edges],
        }


#: (source name, transport, target interface names) -- one probe session.
_Plan = Tuple[str, Any, List[str]]


class MeshProbe:
    """Runs the phase-3 probes.

    One SSH session per (source, batch of targets): the whole probe list for a
    source is packed into a single remote shell invocation.  A full mesh over
    30 data-network nodes is 870 pairs, and doing that as 870 ssh sessions
    would take longer than the rest of the recovery put together.
    """

    ORIGINS = ("nodes", "gateways")
    TARGET_MODES = ("all", "anchors")

    def __init__(self, ssh_factory: Any, checks_config: Dict[str, Any],
                 max_workers: int = 8, topology: Any = None):
        self.ssh_factory = ssh_factory
        self.config = checks_config.get("mesh", {}) or {}
        self.thresholds = checks_config.get("thresholds", {}) or {}
        self.max_workers = max_workers
        self.topology = topology if topology is not None \
            else getattr(ssh_factory, "topology", None)

    # -- target selection --------------------------------------------------

    def _anchor_names(self, location: str) -> Optional[set]:
        """The anchor hostnames for *location*; None when anchors are a flat list.

        ``anchors:`` is either a list (one set for every location, as before)
        or a mapping ``{location: [hosts]}``.
        """
        anchors = self.config.get("anchors", []) or []
        if isinstance(anchors, dict):
            return set(anchors.get(location, []) or [])
        return None

    def _targets(self, nodes: Sequence[Any], network: str,
                 full_mesh: bool) -> List[Any]:
        on_net = [n for n in nodes if n.has_network(network)]
        if full_mesh:
            return on_net
        anchors = self.config.get("anchors", []) or []
        if isinstance(anchors, dict):
            # Per-location anchors, each location with its own fallback.
            selected: List[Any] = []
            for loc in _locations_of(on_net):
                here = [n for n in on_net if n.location == loc]
                names = self._anchor_names(loc) or set()
                picked = [n for n in here if n.hostname in names]
                selected.extend(picked or here[:3])
            return selected
        flat = set(anchors)
        selected = [n for n in on_net if n.hostname in flat]
        # An anchor that is not in this run's node set would leave the probe
        # with nothing to aim at; fall back to the first few nodes so the
        # network still gets tested.
        if not selected:
            selected = on_net[:3]
        return selected

    # -- probing -----------------------------------------------------------

    def _script(self, targets: Sequence[str], network: str,
                mtu_probe: bool) -> str:
        """One shell script probing every target, printing parseable blocks.

        Each target's output is bracketed by markers so the results can be
        split apart reliably even when ping's own output format varies between
        the RHEL and AlmaLinux images in the cluster.

        Every interpolated string -- marker text and ping target -- is
        shell-quoted.  Targets have already passed ``valid_hostname`` at
        topology load or ``resolve()``, so quoting is a second boundary, and a
        valid name is left unchanged by it (the markers still read
        ``===BEGIN <name>===``).
        """
        count = int(self.config.get("count", self.thresholds.get("ping_count", 3)))
        timeout = int(self.config.get("timeout_s", self.thresholds.get("ping_timeout_s", 5)))
        payload = int(self.config.get("mtu_payload_bytes", 8972))
        lines = []
        for target in targets:
            lines.append("echo " + shlex.quote(f"===BEGIN {target}==="))
            lines.append(_ping_command(target, count, timeout) + " 2>&1 || true")
            if mtu_probe:
                lines.append("echo " + shlex.quote(f"---MTU {target}---"))
                # One large, do-not-fragment packet.  Success means every hop
                # on the path carries a full jumbo frame.
                lines.append(_ping_command(target, 1, timeout, payload=payload)
                             + " 2>&1 || true")
            lines.append("echo " + shlex.quote(f"===END {target}==="))
        return "\n".join(lines)

    @staticmethod
    def _split(output: str, target: str) -> Optional[Tuple[str, str]]:
        """Return (ping block, mtu block) for one target, or None if absent.

        None -- the BEGIN or END marker is missing -- means the script did not
        get as far as this target (killed by the timeout, a shell that died),
        which is an untested path, not a failed one.
        """
        begin, end = f"===BEGIN {target}===", f"===END {target}==="
        if begin not in output:
            return None
        rest = output.split(begin, 1)[1]
        if end not in rest:
            return None
        body = rest.split(end, 1)[0]
        marker = f"---MTU {target}---"
        if marker in body:
            ping_part, mtu_part = body.split(marker, 1)
            return ping_part, mtu_part
        return body, ""

    def _budget(self, n_targets: int, mtu_probe: bool) -> int:
        return 20 + n_targets * (
            int(self.config.get("count", 3)) * int(self.config.get("timeout_s", 5))
            + (5 if mtu_probe else 0))

    def _probe_source(self, source: str, transport: Any, target_names: Sequence[str],
                      network: str, mtu_probe: bool) -> List[MeshEdge]:
        if not target_names:
            return []
        script = self._script(target_names, network, mtu_probe)
        try:
            res = transport.run(["/bin/sh", "-c", script],
                                timeout=self._budget(len(target_names), mtu_probe))
        except TransportError as exc:
            # The source itself is unreachable: every edge from it is unknown,
            # not failed.  Reported as one edge per target so the matrix stays
            # rectangular and the report can colour the whole row.
            return [_untested(source, name, network, f"source unreachable: {exc}")
                    for name in target_names]

        edges: List[MeshEdge] = []
        for name in target_names:
            blocks = self._split(res.output, name)
            if blocks is None:
                edges.append(_untested(
                    source, name, network,
                    f"no result for this target in the probe output "
                    f"(exit {res.rc}): " + (res.output.strip()[-200:] or "(empty)")))
                continue
            ping_block, mtu_block = blocks
            stats = parse_ping(ping_block)
            if not stats.transmitted and not _answered(ping_block, _PATH_ANSWERS):
                # ping never ran here; nothing was learned about the path.
                edges.append(_untested(
                    source, name, network,
                    "ping produced no result on the source: "
                    + _first_line(ping_block)))
                continue
            mtu_ok: Optional[bool] = None
            if mtu_probe and mtu_block:
                mtu_stats = parse_ping(mtu_block)
                # The same rule for the jumbo probe: BusyBox rejecting -M do
                # says nothing about the path's MTU, so mtu_ok stays None.
                if mtu_stats.transmitted or _answered(mtu_block, _MTU_ANSWERS):
                    mtu_ok = mtu_stats.alive
            edges.append(MeshEdge(
                source=source, target=name, network=network,
                ok=stats.alive, loss_pct=stats.loss_pct,
                rtt_avg_ms=stats.rtt_avg_ms, mtu_ok=mtu_ok,
                detail="" if stats.alive else ping_block.strip()[-200:],
            ))
        return edges

    def _plan_nodes(self, sources: Sequence[Any], targets: Sequence[Any],
                    network: str, per_location: bool) -> List[_Plan]:
        """origin: nodes -- each node probes the targets, never itself."""
        plans: List[_Plan] = []
        for src in sources:
            names = [t.networks[network] for t in targets
                     if t.hostname != src.hostname
                     and (not per_location or t.location == src.location)]
            if names:
                plans.append((src.hostname, lambda s=src: self.ssh_factory.for_node(s),
                              names))
        return plans

    def _plan_gateways(self, targets: Sequence[Any], network: str
                       ) -> Tuple[List[_Plan], List[str], Dict[str, List[str]]]:
        """origin: gateways -- each location's gateways probe its targets.

        Gateways are contacted directly, as the IPMI client does.  There is no
        self-exclusion: a gateway pinging its own BMC is a real path.

        Returns (plans, gateway names, {location: targets} for the locations
        that have targets but no gateway -- those are never probed, and the
        caller must say so rather than leave them out).
        """
        plans: List[_Plan] = []
        gateways: List[str] = []
        orphaned: Dict[str, List[str]] = {}
        for loc in _locations_of(targets):
            names = [t.networks[network] for t in targets if t.location == loc]
            gws = []
            if self.topology is not None and loc != "unknown":
                gws = self.topology.ipmi_gateways(loc) if network == "ipmi" \
                    else self.topology.gateways(loc)
            if not gws:
                log.warning("mesh %s: location %s has no gateway; %d target(s) "
                            "not probed", network, loc, len(names))
                orphaned[loc] = names
                continue
            for gw in gws:
                if gw not in gateways:
                    # The teststand's ipmi_gateways are MC-2's: one host,
                    # two plans, but one source.
                    gateways.append(gw)
                plans.append((gw, lambda g=gw: self.ssh_factory.for_host(g, direct=True),
                              names))
        return plans, gateways, orphaned

    def run(self, nodes: Sequence[Any], network: str,
            full_mesh: bool = True, mtu_probe: bool = False,
            origin: str = "nodes", targets: Optional[str] = None,
            deadline: Any = None, cross_location: bool = False) -> MeshResult:
        """Probe *network* across *nodes*.

        *targets* is ``all`` or ``anchors``; when omitted it follows
        *full_mesh* (``all`` for a full mesh, ``anchors`` otherwise), which is
        the behaviour before the key existed.

        *deadline* is the phase's run.phase_timeout Deadline: a source whose
        probe has not started when it expires is not probed, and its edges
        are UNKNOWN (untested) with :data:`TIMEOUT_DETAIL`.
        """
        target_mode = targets or ("all" if full_mesh else "anchors")
        if origin not in self.ORIGINS:
            raise ValueError(f"mesh {network}: origin must be one of "
                             f"{', '.join(self.ORIGINS)}, not {origin!r}")
        if target_mode not in self.TARGET_MODES:
            raise ValueError(f"mesh {network}: targets must be one of "
                             f"{', '.join(self.TARGET_MODES)}, not {target_mode!r}")

        skipped = [n.hostname for n in nodes if not n.has_network(network)]
        target_nodes = self._targets(nodes, network, target_mode == "all")
        # A full mesh stays inside each location: the private networks are
        # separate segments per site, and MC-2 and the teststand reuse
        # 10.226.9.0/24, so a cross-site pair is not a path at all (verified
        # live: every one fails with ARP "host unreachable").
        # cross_location: true restores the whole-run mesh for a network that
        # really spans sites.
        per_location = (target_mode == "all" and not cross_location) or \
            (isinstance(self.config.get("anchors"), dict)
             and target_mode == "anchors")
        orphaned: Dict[str, List[str]] = {}
        if origin == "gateways":
            plans, source_names, orphaned = self._plan_gateways(target_nodes,
                                                                network)
        else:
            sources = [n for n in nodes if n.has_network(network)]
            source_names = [n.hostname for n in sources]
            plans = self._plan_nodes(sources, target_nodes, network, per_location)

        out = MeshResult(network=network, full_mesh=full_mesh,
                         sources=source_names,
                         targets=[n.hostname for n in target_nodes],
                         skipped=skipped, origin=origin, target_mode=target_mode)
        out.target_hosts = {t.networks[network]: t.hostname for t in target_nodes}
        for loc, names in orphaned.items():
            pseudo = f"(no gateway: {loc})"
            out.pseudo_sources.append(pseudo)
            out.edges.extend(_untested(pseudo, name, network,
                                       f"location {loc} has no gateway in the "
                                       f"topology to probe from")
                             for name in names)
            out.notes.append(
                f"{network}: location {loc} has {len(names)} target(s) but no "
                f"gateway in the topology to probe them from -- those paths are "
                f"UNKNOWN (untested), not OK")
        if not plans:
            log.warning("mesh %s: %d source(s), %d target(s) -- nothing to probe",
                        network, len(source_names), len(target_nodes))
            return out

        log.info("mesh %s: probing from %d %s against %d target(s)%s",
                 network, len(plans),
                 "gateway(s)" if origin == "gateways" else "source(s)",
                 len(target_nodes), " with MTU probe" if mtu_probe else "")

        def job(plan: _Plan) -> List[MeshEdge]:
            name, make_transport, target_names = plan
            if deadline is not None and deadline.expired():
                return [_untested(name, t, network, TIMEOUT_DETAIL)
                        for t in target_names]
            return self._probe_source(name, make_transport(), target_names,
                                      network, mtu_probe)

        # Not a `with` block: its __exit__ would wait for every queued source
        # after a Ctrl-C/SIGTERM (KeyboardInterrupt) before cleanup could run.
        pool = ThreadPoolExecutor(max_workers=self.max_workers)
        try:
            futures = {pool.submit(job, plan): plan for plan in plans}
            for future in as_completed(futures):
                name, _, target_names = futures[future]
                try:
                    out.edges.extend(future.result())
                except Exception as exc:  # noqa: BLE001 - one bad source must
                    # not abandon the other 40; record it and carry on.  It
                    # tested nothing, so its edges are UNKNOWN.
                    log.exception("mesh probe from %s failed", name)
                    out.edges.extend(
                        _untested(name, t, network,
                                  f"probe raised {type(exc).__name__}: {exc}")
                        for t in target_names)
        except BaseException:
            log.warning("mesh %s interrupted: cancelling queued probes", network)
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        pool.shutdown(wait=True)
        out.edges.sort(key=lambda e: (e.source, e.target))
        if origin == "gateways":
            self._note_gateway_coverage(out)
        return out

    @staticmethod
    def _note_gateway_coverage(out: MeshResult) -> None:
        """Say when a dark gateway's targets were covered by its partner."""
        dark = set(out.unreachable_sources())
        if not dark:
            return
        covered_by: Dict[str, set] = {}
        for edge in out.tested:
            covered_by.setdefault(edge.target, set()).add(edge.source)
        for gw in sorted(dark):
            mine = [e.target for e in out.edges if e.source == gw]
            partners = sorted({s for t in mine for s in covered_by.get(t, ())})
            uncovered = [t for t in mine if t not in covered_by]
            short = gw.split(".")[0]
            if not uncovered:
                out.notes.append(
                    f"{out.network}: gateway {short} could not be used, but "
                    f"{', '.join(p.split('.')[0] for p in partners)} tested all "
                    f"{len(mine)} of its target(s); the result stands on those "
                    f"probes")
            else:
                out.notes.append(
                    f"{out.network}: gateway {short} could not be used and "
                    f"{len(uncovered)} of its {len(mine)} target(s) were tested "
                    f"by no other gateway -- those are UNKNOWN")

    def run_all(self, nodes: Sequence[Any], deadline: Any = None,
                exclude: Collection[str] = ()) -> List[MeshResult]:
        """Probe every network listed in checks.yaml's mesh section.

        *exclude* names hosts (phase 3: those that failed an earlier phase)
        left out of ``origin: nodes`` networks only.  An ``origin: gateways``
        network still probes every node's BMC: a BMC does not depend on the
        host OS, and the BMCs of the nodes that did not come back are the
        ones the operator needs next.
        """
        excluded = set(exclude)
        kept = [n for n in nodes if n.hostname not in excluded]
        results: List[MeshResult] = []
        for entry in self.config.get("networks", []) or []:
            origin = str(entry.get("origin", "nodes"))
            results.append(self.run(
                nodes if origin == "gateways" else kept,
                network=entry["name"],
                full_mesh=bool(entry.get("full_mesh", True)),
                mtu_probe=bool(entry.get("mtu_probe", False)),
                origin=origin,
                cross_location=bool(entry.get("cross_location", False)),
                targets=entry.get("targets"),
                deadline=deadline,
            ))
        return results


def _answered(block: str, markers: Sequence[str]) -> bool:
    """True if a ping block with no summary still carries an answer in *markers*."""
    return any(m in block for m in markers)


def _first_line(block: str) -> str:
    """The first non-blank line of *block* -- ping's own reason it did not run."""
    for line in block.splitlines():
        if line.strip():
            return line.strip()[:200]
    return "(empty)"


def _locations_of(nodes: Sequence[Any]) -> List[str]:
    """Distinct locations of *nodes*, in first-seen order."""
    seen: List[str] = []
    for n in nodes:
        if n.location not in seen:
            seen.append(n.location)
    return seen
