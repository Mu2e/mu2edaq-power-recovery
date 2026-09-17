"""Run assembly: builds the context every phase works in, and runs checks.

The orchestrator owns the expensive, shared things -- the Kerberos tickets, the
Vault-sourced BMC credentials, the SSH factory with its gateway choice, the run
store -- and hands each phase a ready-made environment.  It also owns the
concurrency policy, because "how many SSH sessions may exist at once" is a
property of the run, not of any one phase.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

import yaml

from .checks import (CheckContext, CheckResult, NEEDS_ROOT, Status,
                     profile_checks, profile_for_node, rollup, run_check)
from .creds import KerberosError, KerberosManager, VaultCredentials, VaultError
from .state import RunStore
from .topology import Node, Topology
from .transport import (FakeTransport, IPMIClient, LocalTransport, SSHFactory,
                        healthy_node_rules)
from .version import VersionInfo, collect as collect_version

log = logging.getLogger(__name__)


@dataclass
class NodeAssessment:
    """Every check run against one node in one phase, plus the verdict."""

    node: Node
    results: List[CheckResult] = field(default_factory=list)
    power_state: Optional[str] = None
    #: Filled in by phase 2: what it had to do to bring the node up.
    power_action: Optional[Dict[str, Any]] = None
    duration: float = 0.0
    #: Set when neither ICMP nor SSH got an answer, so the remaining checks
    #: were never run.
    unreachable: bool = False

    @property
    def status(self) -> Status:
        """This node's verdict.

        A node nothing could reach is UNKNOWN, not FAIL, even though its ping
        and ssh checks individually failed.  The report draws exactly this
        distinction for the operator -- FAIL means we looked and it is wrong,
        UNREACHABLE means we could not look -- and the two need different
        responses during a recovery.  The individual check results keep their
        own FAIL status; only the roll-up changes.
        """
        if self.unreachable:
            return Status.UNKNOWN
        return rollup([r.status for r in self.results])

    @property
    def failures(self) -> List[CheckResult]:
        return [r for r in self.results if r.status.is_bad]

    @property
    def warnings(self) -> List[CheckResult]:
        return [r for r in self.results if r.status is Status.WARN]

    @property
    def skipped(self) -> List[CheckResult]:
        return [r for r in self.results if r.status is Status.SKIP]

    def summary(self) -> str:
        if not self.results:
            return "no checks ran"
        if self.unreachable:
            return ("did not answer ICMP or SSH; its remaining checks were "
                    "not run")
        applicable = len(self.results) - len(self.skipped)
        suffix = f" ({len(self.skipped)} n/a)" if self.skipped else ""
        if self.status is Status.SKIP:
            return f"no applicable checks ({len(self.results)} not applicable)"
        if self.status is Status.OK:
            return f"all {applicable} checks passed{suffix}"
        bad = self.failures
        if bad:
            return f"{len(bad)} of {applicable} checks failed: " + \
                   ", ".join(r.check_id for r in bad[:4]) + \
                   (" ..." if len(bad) > 4 else "")
        return (f"{len(self.warnings)} warning(s) of {applicable} checks"
                + suffix)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "node": self.node.as_dict(),
            "status": self.status.value,
            "summary": self.summary(),
            "power_state": self.power_state,
            "power_action": self.power_action,
            "unreachable": self.unreachable,
            "duration": round(self.duration, 2),
            "results": [r.as_dict() for r in self.results],
        }


class SimulatedSSHFactory:
    """Stands in for :class:`SSHFactory` under ``--simulate``.

    Hands every node a :class:`FakeTransport` sharing one rule set and one call
    log, so a simulated run exercises the real check bodies, the real parsers
    and the real report generator against synthetic command output.
    """

    def __init__(self, topology: Topology, rules: Optional[Sequence] = None):
        self.topology = topology
        self.base = FakeTransport("simulated")
        for pattern, response in (rules if rules is not None else healthy_node_rules()):
            self.base.expect(pattern, response)

    def gateway_for(self, location: str) -> Optional[str]:
        gateways = self.topology.gateways(location)
        return gateways[0] if gateways else None

    def for_host(self, host: str, jump: Optional[str] = None,
                 user: Optional[str] = None, direct: bool = False) -> FakeTransport:
        return self.base.clone(host)

    def for_node(self, node: Node, user: Optional[str] = None,
                 root: bool = False) -> FakeTransport:
        return self.base.clone(node.hostname)


class Orchestrator:
    """Shared run state and the check-execution engine."""

    def __init__(self, settings: Any, simulate: bool = False):
        self.settings = settings
        self.simulate = simulate
        self.topology = Topology.load(settings.config_path("topology.file"))
        self.checks_config = self._load_yaml(settings.config_path("topology.checks_file"))
        self.sequence_config = self._load_yaml(
            settings.config_path("topology.sequence_file"))
        self.local = LocalTransport(
            default_timeout=settings.get("ssh.command_timeout", 120),
            max_capture=settings.get("logging.max_capture_bytes", 65536))
        self.store = RunStore.from_settings(settings)
        self.version: VersionInfo = collect_version([
            settings.config_path("topology.file"),
            settings.config_path("topology.checks_file"),
            settings.config_path("topology.sequence_file"),
        ])
        self.kerberos: Optional[KerberosManager] = None
        self.vault: Optional[VaultCredentials] = None
        self.ipmi: Optional[IPMIClient] = None
        self.ssh_factory: Any = None
        #: Per-node values carried between phases (SEL baselines, boot flags).
        self.baselines: Dict[str, Dict[str, Any]] = {}
        self._have_root: bool = True
        self.notes: List[str] = []

    # -- configuration -----------------------------------------------------

    @staticmethod
    def _load_yaml(path: Path) -> Dict[str, Any]:
        try:
            with open(path) as fh:
                return yaml.safe_load(fh) or {}
        except FileNotFoundError:
            log.warning("configuration file %s not found; using built-in defaults", path)
            return {}
        except yaml.YAMLError as exc:
            raise SystemExit(f"error: {path} is not valid YAML: {exc}")

    @property
    def locations(self) -> List[str]:
        return list(self.settings.get("topology.locations", ["mc2"]))

    def nodes(self, names: Optional[Sequence[str]] = None) -> List[Node]:
        """The nodes this run operates on."""
        if names:
            return self.topology.resolve(names, self.locations)
        return self.topology.all_nodes(self.locations)

    # -- credentials -------------------------------------------------------

    def prepare_credentials(self) -> Dict[str, Any]:
        """Acquire tickets and BMC credentials before any phase starts.

        Front-loaded deliberately: an operator should answer every password
        prompt at the beginning, not two hours in when a worker thread needs
        root on a node that has just booted and the prompt is interleaved with
        forty lines of progress output.
        """
        info: Dict[str, Any] = {"kerberos": {}, "ipmi": None, "notes": []}

        if self.simulate:
            self.ssh_factory = SimulatedSSHFactory(self.topology)
            # A simulated IPMI client too, driven by the same scripted
            # transport: without it every power.* check would report SKIP and
            # the rehearsal would exercise neither the BMC parsing nor the
            # phase-2 power path, which are the parts most worth rehearsing.
            # dry_run is forced on, so even the simulated client refuses to
            # pretend it switched anything.
            self.ipmi = IPMIClient(
                gateway=self.ssh_factory.for_host("simulated-gateway"),
                username="simulated", password="simulated",
                dry_run=True, protected=self.topology.is_protected)
            info["notes"].append("simulated run: no credentials acquired, no "
                                 "host contacted; all command output is scripted")
            self.notes.extend(info["notes"])
            return info

        self.kerberos = KerberosManager(self.settings, local=self.local)
        try:
            tickets = self.kerberos.prepare()
            info["kerberos"] = {k: v.as_dict() for k, v in tickets.items()}
        except KerberosError as exc:
            raise SystemExit(f"error: {exc}")

        identities = self.kerberos.available_identities()
        if identities:
            info["service_identities"] = identities
            log.info("service identities available as fallbacks: %s",
                     ", ".join(identities))
        elif self.settings.get("kerberos.use_service_keytabs", True):
            info["notes"].append(self.kerberos.tickets.unavailable_reason())

        if not self.settings.get("kerberos.root_principal") and \
                not self.settings.get("kerberos.principal"):
            info["notes"].append(
                "no root principal designated; root checks will run under the "
                "ambient ticket and will be reported as failures if it is not "
                "root-capable")

        # The factory needs the Kerberos manager: it is what supplies the
        # credential chain, and -- more basically -- what puts KRB5CCNAME into
        # the ssh environment, without which a designated principal is minted
        # into a private cache that ssh never looks at.
        self.ssh_factory = SSHFactory(self.settings, self.topology,
                                      local=self.local, kerberos=self.kerberos)

        # BMC credentials.  A failure here is not fatal for phase 1 -- the
        # OS-level checks still work -- so it degrades to "no IPMI" with a note
        # rather than stopping the run.
        self.vault = VaultCredentials(self.settings, local=self.local)
        try:
            creds = self.vault.ipmi()
            info["ipmi"] = creds.redacted()
            self.ipmi = self._make_ipmi_client(creds)
        except VaultError as exc:
            info["notes"].append(f"IPMI unavailable: {exc}")
            log.warning("IPMI credentials unavailable: %s", exc)

        self.notes.extend(info["notes"])
        return info

    def _make_ipmi_client(self, creds: Any) -> Optional[IPMIClient]:
        """Build an IPMI client that runs ipmitool on a responsive gateway."""
        gateway_host = None
        for location in self.locations:
            gateway_host = self.ssh_factory.gateway_for(location)
            if gateway_host:
                break
        if not gateway_host:
            log.error("no gateway is reachable; IPMI commands cannot be issued")
            self.notes.append("no gateway reachable -- IPMI is unavailable, so "
                              "power state cannot be read or changed")
            return None
        gateway = self.ssh_factory.for_host(gateway_host, direct=True)
        log.info("IPMI commands will be issued from %s", gateway_host)
        return IPMIClient(
            gateway=gateway,
            username=self.settings.get("ipmi.username") or creds.username,
            password=creds.password,
            tool=self.settings.get("ipmi.tool", "ipmitool"),
            interface=self.settings.get("ipmi.interface", "lanplus"),
            privilege=self.settings.get("ipmi.privilege", "Operator"),
            cipher_suite=self.settings.get("ipmi.cipher_suite", 3),
            timeout=self.settings.get("ipmi.timeout", 10),
            retries=self.settings.get("ipmi.retries", 2),
            dry_run=bool(self.settings.get("run.dry_run", True)),
            protected=self.topology.is_protected,
            message_timeout=self.settings.get("ipmi.message_timeout"),
            tool_retries=self.settings.get("ipmi.tool_retries"),
            extra_args=self.settings.get("ipmi.extra_args", []),
        )

    # -- check execution ---------------------------------------------------

    def context_for(self, node: Node,
                    baseline: Optional[Dict[str, Any]] = None) -> CheckContext:
        merged = dict(self.baselines.get(node.hostname, {}))
        merged.update(baseline or {})
        return CheckContext(
            node=node,
            settings=self.settings,
            topology=self.topology,
            ssh_factory=self.ssh_factory,
            ipmi=self.ipmi,
            checks_config=self.checks_config,
            local=self.local,
            baseline=merged,
        )

    def checks_for(self, node: Node, profile: Optional[str] = None) -> List[str]:
        name = profile or profile_for_node(self.checks_config, node)
        ids = profile_checks(self.checks_config, name)
        if not self._have_root:
            ids = [c for c in ids if c not in NEEDS_ROOT]
        return ids

    def assess_node(self, node: Node, profile: Optional[str] = None,
                    baseline: Optional[Dict[str, Any]] = None,
                    only: Optional[Sequence[str]] = None) -> NodeAssessment:
        """Run a node's whole check profile and roll the results up.

        Reachability is treated as a gate: if ``ping.lab`` and ``ssh.login``
        both fail there is no point running twenty more checks that will each
        take an SSH timeout to fail, so the rest are recorded as UNKNOWN with
        the reason.  That keeps a dead node's assessment fast without hiding
        which checks were never run.
        """
        started = time.monotonic()
        ctx = self.context_for(node, baseline)
        ids = list(only) if only else self.checks_for(node, profile)
        assessment = NodeAssessment(node=node)

        reachable = True
        for check_id in ids:
            if not reachable and check_id not in ("ping.lab", "power.status",
                                                  "power.sensors", "power.sel"):
                assessment.results.append(CheckResult(
                    node=node.hostname, check_id=check_id, status=Status.UNKNOWN,
                    summary="not run: the node could not be reached",
                    detail="skipped after ping and ssh both failed"))
                continue
            res = run_check(check_id, ctx)
            assessment.results.append(res)
            if check_id == "power.status":
                assessment.power_state = res.data.get("state")
                # Phase 2 compares against the event-log length seen here.
                self.baselines.setdefault(node.hostname, {})
            if check_id == "power.sel" and res.data.get("count") is not None:
                self.baselines.setdefault(node.hostname, {})["sel_count"] = \
                    res.data["count"]
            if check_id == "ssh.login" and res.status is Status.FAIL:
                ping = next((r for r in assessment.results
                             if r.check_id == "ping.lab"), None)
                if ping is not None and ping.status is Status.FAIL:
                    reachable = False
                    assessment.unreachable = True
                    log.info("%s is unreachable; skipping its remaining checks",
                             node.hostname)

        if self.kerberos is not None:
            # Back to the operator's own principal. Nothing here mutates the
            # ambient environment or the default credential cache, so this
            # asserts the invariant rather than repairing anything -- but it
            # makes "a service identity never becomes the run's identity"
            # something the code states, not something you have to infer.
            self.kerberos.restore_primary()

        assessment.duration = time.monotonic() - started
        return assessment

    def assess_nodes(self, nodes: Sequence[Node], profile: Optional[str] = None,
                     baseline: Optional[Dict[str, Any]] = None,
                     concurrency: Optional[int] = None,
                     only: Optional[Sequence[str]] = None,
                     progress: Optional[Callable[[NodeAssessment], None]] = None
                     ) -> List[NodeAssessment]:
        """Assess many nodes in parallel, bounded by ``ssh.max_sessions``."""
        workers = concurrency or int(self.settings.get("ssh.max_sessions", 16))
        workers = max(1, min(workers, len(nodes) or 1))
        out: List[NodeAssessment] = []
        log.info("assessing %d node(s) with %d worker(s)", len(nodes), workers)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self.assess_node, n, profile, baseline, only): n
                       for n in nodes}
            for future in as_completed(futures):
                node = futures[future]
                try:
                    assessment = future.result()
                except Exception as exc:  # noqa: BLE001 - see run_check's rationale
                    log.exception("assessment of %s failed outright", node.hostname)
                    assessment = NodeAssessment(node=node, results=[CheckResult(
                        node=node.hostname, check_id="internal",
                        status=Status.UNKNOWN,
                        summary=f"assessment raised {type(exc).__name__}",
                        detail=str(exc))])
                out.append(assessment)
                if progress:
                    progress(assessment)
        out.sort(key=lambda a: (a.node.location, a.node.node_class, a.node.hostname))
        return out

    # -- persistence -------------------------------------------------------

    def record(self, assessments: Sequence[NodeAssessment]) -> None:
        """Write a phase's assessments into the run store."""
        for a in assessments:
            self.store.record_checks(a.results)
            self.store.record_node(
                hostname=a.node.hostname, location=a.node.location,
                node_class=a.node.node_class, status=a.status.value,
                summary=a.summary(), power_state=a.power_state,
                data={"power_action": a.power_action,
                      "duration": round(a.duration, 2),
                      "networks": a.node.networks})

    def close(self) -> None:
        if self.kerberos:
            self.kerberos.cleanup()
        self.store.close()


# ---------------------------------------------------------------------------
# Roll-ups shared by the phases and the report
# ---------------------------------------------------------------------------


def tally(assessments: Sequence[NodeAssessment]) -> Dict[str, int]:
    """Count nodes by verdict."""
    counts = {s.value: 0 for s in Status}
    for a in assessments:
        counts[a.status.value] += 1
    counts["total"] = len(assessments)
    return counts


def group_by_class(assessments: Sequence[NodeAssessment]
                   ) -> Dict[str, List[NodeAssessment]]:
    grouped: Dict[str, List[NodeAssessment]] = {}
    for a in assessments:
        grouped.setdefault(a.node.node_class, []).append(a)
    return grouped


def group_by_location(assessments: Sequence[NodeAssessment]
                      ) -> Dict[str, List[NodeAssessment]]:
    grouped: Dict[str, List[NodeAssessment]] = {}
    for a in assessments:
        grouped.setdefault(a.node.location, []).append(a)
    return grouped
