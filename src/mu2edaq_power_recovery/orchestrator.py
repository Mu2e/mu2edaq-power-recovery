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
from contextlib import ExitStack, contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

import yaml

from .checks import (CheckContext, CheckResult, NEEDS_ROOT, Status,
                     profile_checks, profile_for_node, rollup, run_check)
from .creds import KerberosError, KerberosManager, VaultCredentials, VaultError
from .creds.bootstrap import credential_session
from .state import RunStore
from .topology import Node, Topology, TopologyError
from .transport import (CredentialBreaker, FakeTransport, IPMIClient,
                        LocalTransport, PowerState, SSHFactory,
                        healthy_node_rules)
from .version import VersionInfo, collect as collect_version

log = logging.getLogger(__name__)

#: The summary given to every node, check, stage or path a phase never reached
#: because ``run.phase_timeout`` ran out. UNKNOWN, never FAIL: nothing was
#: looked at, and "the budget ran out" needs a different response from both
#: "it is broken" and "it did not answer". Re-exported by phases/base.py.
TIMEOUT_SUMMARY = "not run: phase_timeout expired"


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
    #: Set when run.phase_timeout expired before every check had run; the
    #: checks not reached are UNKNOWN with TIMEOUT_SUMMARY.
    timed_out: bool = False

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
        if self.timed_out and all(r.summary == TIMEOUT_SUMMARY
                                  for r in self.results):
            return TIMEOUT_SUMMARY
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
            # is_bad covers FAIL and UNKNOWN; say which. Live: mu2edaq13 read
            # "3 of 17 checks failed" for three checks that could not look.
            failed = [r for r in bad if r.status is Status.FAIL]
            unknown = [r for r in bad if r.status is not Status.FAIL]
            parts = []
            if failed:
                parts.append(f"{len(failed)} of {applicable} checks failed: "
                             + ", ".join(r.check_id for r in failed[:4])
                             + (" ..." if len(failed) > 4 else ""))
            if unknown:
                parts.append(f"{len(unknown)} could not be checked: "
                             + ", ".join(r.check_id for r in unknown[:4])
                             + (" ..." if len(unknown) > 4 else ""))
            return "; ".join(parts)
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
            "timed_out": self.timed_out,
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
        #: Mirrors SSHFactory.deadline; scripted transports ignore it.
        self.deadline: Any = None
        self.base = FakeTransport("simulated")
        for pattern, response in (rules if rules is not None else healthy_node_rules()):
            self.base.expect(pattern, response)

    def gateway_for(self, location: str, role: str = "ssh") -> Optional[str]:
        gateways = self.topology.ipmi_gateways(location) if role == "ipmi" \
            else self.topology.gateways(location)
        return gateways[0] if gateways else None

    def for_host(self, host: str, jump: Optional[str] = None,
                 user: Optional[str] = None, direct: bool = False) -> FakeTransport:
        return self.base.clone(host)

    def for_node(self, node: Node, user: Optional[str] = None,
                 root: bool = False) -> FakeTransport:
        return self.base.clone(node.hostname)


class Orchestrator:
    """Shared run state and the check-execution engine."""

    def __init__(self, settings: Any, simulate: bool = False,
                 clock: Optional[Callable[[], float]] = None,
                 sleep: Optional[Callable[[float], None]] = None):
        self.settings = settings
        self.simulate = simulate
        #: Injectable so a test can drive phase timeouts and boot waits on a
        #: fake clock whose sleep advances it, rather than sleeping for real.
        self.clock: Callable[[], float] = clock or time.monotonic
        self.sleep: Callable[[float], None] = sleep or time.sleep
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
        #: The first location's client, kept for callers that want "the" IPMI
        #: client (phase 1's readiness summary reads the shared breaker
        #: through it). Per-node work goes through :meth:`ipmi_for`.
        self.ipmi: Optional[IPMIClient] = None
        #: location -> IPMI client running ipmitool on a gateway of *that*
        #: location. A BMC is never driven through another site's gateway.
        self.ipmi_clients: Dict[str, IPMIClient] = {}
        #: The running phase's run.phase_timeout Deadline (None between
        #: phases). Every ssh transport the factory builds caps its per-call
        #: timeout at the time this has left; see :meth:`budget`.
        self._deadline: Any = None
        #: location -> its CredentialBreaker, shared by every IPMI client of
        #: that location whichever gateway it runs ipmitool on. Per location,
        #: not per run: the BMC account is not the same everywhere (on
        #: 2026-10-01 the teststand's BMCs refused the account MC-2's accept),
        #: so a refusal at one site must not stop IPMI at another.
        self.ipmi_breakers: Dict[str, CredentialBreaker] = {}
        self.ssh_factory: Any = None
        #: Per-node values carried between phases (SEL baselines, boot flags).
        self.baselines: Dict[str, Dict[str, Any]] = {}
        self._have_root: bool = True
        self.notes: List[str] = []
        #: Holds the credential session open; close() unwinds it, which is
        #: what destroys the run's private Kerberos caches.
        self._credentials = ExitStack()
        #: Whether the fallbacks-disabled event has reached the run store.
        self._fallback_event_recorded = False

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

    def empty_location_notes(self) -> List[str]:
        """One note per requested location that has no nodes configured.

        Phases 1-3 carry these so a run over, say, ``--location mc1`` says
        in its own report that it covered nothing there, instead of an
        empty table that reads as "nothing wrong" (#25).
        """
        notes: List[str] = []
        for location in self.locations:
            try:
                if self.topology.nodes(location):
                    continue
                info = self.topology.location_info(location)
            except TopologyError:
                continue
            status = info.get("status")
            notes.append(
                f"no nodes configured for {location}"
                + (f" (inventory status: {status})" if status else "")
                + " -- nothing there was checked; see config/topology.yaml "
                  "and mu2e-node-inventory --validate")
        return notes

    def nodes(self, names: Optional[Sequence[str]] = None) -> List[Node]:
        """The nodes this run operates on."""
        if names:
            return self.topology.resolve(names, self.locations)
        return self.topology.all_nodes(self.locations)

    # -- time budget -------------------------------------------------------

    @property
    def deadline(self) -> Any:
        return self._deadline

    @deadline.setter
    def deadline(self, value: Any) -> None:
        self._deadline = value
        if self.ssh_factory is not None and hasattr(self.ssh_factory, "deadline"):
            self.ssh_factory.deadline = value

    @contextmanager
    def budget(self, deadline: Any) -> Iterator[Any]:
        """Make *deadline* the running phase's budget for the ``with`` block.

        While it is set, :meth:`assess_nodes` starts no node and
        :meth:`assess_node` runs no check once it has expired, and every ssh
        call (checks, IPMI through a gateway, mesh probes) has its timeout
        capped at the time remaining. So a phase overruns its budget by at
        most the one call in flight when the budget expires -- and that call
        was itself capped at what was left when it started.
        """
        previous = self._deadline
        self.deadline = deadline
        try:
            yield deadline
        finally:
            self.deadline = previous

    def budget_expired(self) -> bool:
        return self._deadline is not None and self._deadline.expired()

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
            # The workstation's own transport has to be scripted as well.  A
            # gateway has no prober closer than the machine driving the run --
            # CheckContext.prober falls back to ctx.local for node_class
            # 'gateway' -- so leaving the real LocalTransport in place meant a
            # *simulated* ping.lab shelled out and pinged mu2egateway01 for
            # real.  That contacted the cluster the simulation promises not to
            # touch, and made the outcome depend on whether the operator's
            # workstation happened to reach Fermilab at that instant, which is
            # what made the phase tests fail intermittently.
            self.local = self.ssh_factory.for_host("localhost")
            # A simulated IPMI client too, driven by the same scripted
            # transport: without it every power.* check would report SKIP and
            # the rehearsal would exercise neither the BMC parsing nor the
            # phase-2 power path, which are the parts most worth rehearsing.
            # dry_run is forced on, so even the simulated client refuses to
            # pretend it switched anything.
            # One per location, like a real run, each on its location's
            # breaker.
            for location in self.locations:
                gateway = self.ssh_factory.gateway_for(location, role="ipmi")
                if not gateway:
                    continue
                self.ipmi_clients[location] = IPMIClient(
                    gateway=self.ssh_factory.for_host(gateway, direct=True),
                    username="simulated", password="simulated",
                    dry_run=True, protected=self.topology.is_protected,
                    breaker=self.ipmi_breaker_for(location))
            self.ipmi = next(iter(self.ipmi_clients.values()), None)
            info["notes"].append("simulated run: no credentials acquired, no "
                                 "host contacted; all command output is scripted")
            self.notes.extend(info["notes"])
            return info

        # The same bootstrap the diagnostics use (creds/bootstrap.py): the
        # ambient-cache warning, the designated principals, the service
        # fallbacks minted before any worker starts, and the ssh factory.
        # Held open until close(), which destroys the private caches.
        try:
            session = self._credentials.enter_context(credential_session(
                self.settings, self.topology, self.local))
        except KerberosError as exc:
            raise SystemExit(f"error: {exc}")
        self.kerberos = session.kerberos
        info["kerberos"] = {k: v.as_dict() for k, v in session.tickets.items()}
        info["notes"].extend(session.notes)

        # Say plainly which login/ticket pair the run will lead with -- it is
        # the pair that decides everything downstream.
        primary = self.kerberos.operator_credential()
        log.info("primary credential: %s", primary.describe())
        info["primary_credential"] = primary.as_dict()

        identities = self.kerberos.available_identities()
        if self.kerberos.fallbacks_disabled is not None:
            info["fallbacks_disabled"] = self.kerberos.fallbacks_disabled.as_dict()
        elif identities:
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

        self.ssh_factory = session.factory

        # BMC credentials.  A failure here is not fatal for phase 1 -- the
        # OS-level checks still work -- so it degrades to "no IPMI" with a note
        # rather than stopping the run.
        self.vault = VaultCredentials(self.settings, local=self.local)
        try:
            creds = self.vault.ipmi()
            info["ipmi"] = creds.redacted()
            self.ipmi_clients = self._make_ipmi_clients(creds)
            self.ipmi = next(iter(self.ipmi_clients.values()), None)
        except VaultError as exc:
            info["notes"].append(f"IPMI unavailable: {exc}")
            log.warning("IPMI credentials unavailable: %s", exc)

        self.notes.extend(info["notes"])
        return info

    def _make_ipmi_clients(self, creds: Any) -> Dict[str, IPMIClient]:
        """One IPMI client per location, each on an IPMI gateway of that location.

        The IPMI gateway is ``ipmi_gateways:`` when the location names one
        (the teststand's BMCs are on the MC-2 segment), else ``gateways:``.

        The IPMI subnets are per site and not routable between them, so a BMC
        must be driven from its own location's gateway; one client for the
        whole run (the first gateway that answered anywhere) would send a
        teststand BMC's commands through MC-2. Each client takes its
        location's :class:`CredentialBreaker` (:meth:`ipmi_breaker_for`) --
        the BMC account differs between sites, so one site's refusal must not
        stop another's IPMI -- and keeps the protected-host refusal.
        """
        clients: Dict[str, IPMIClient] = {}
        for location in self.locations:
            gateway_host = self.ssh_factory.gateway_for(location, role="ipmi")
            if not gateway_host:
                log.error("no gateway is reachable for %s; its BMCs cannot be "
                          "driven", location)
                self.notes.append(
                    f"no gateway reachable for {location} -- IPMI is "
                    f"unavailable there, so its power state cannot be read "
                    f"or changed")
                continue
            clients[location] = self._make_ipmi_client(creds, gateway_host,
                                                       location)
            log.info("IPMI commands for %s will be issued from %s",
                     location, gateway_host)
        return clients

    def ipmi_breaker_for(self, location: str) -> CredentialBreaker:
        """The :class:`CredentialBreaker` shared by *location*'s IPMI clients.

        Created on first use. Every client driving that location's BMCs must
        take this one: a client with its own breaker would present a refused
        credential again.
        """
        breaker = self.ipmi_breakers.get(location)
        if breaker is None:
            breaker = self.ipmi_breakers[location] = CredentialBreaker(location)
        return breaker

    def _make_ipmi_client(self, creds: Any, gateway_host: str,
                          location: str) -> IPMIClient:
        """An IPMI client that runs ipmitool on *gateway_host* for *location*."""
        gateway = self.ssh_factory.for_host(gateway_host, direct=True)
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
            stop_on_auth_failure=bool(
                self.settings.get("ipmi.stop_on_auth_failure", True)),
            breaker=self.ipmi_breaker_for(location),
            reachability_precheck=bool(
                self.settings.get("ipmi.reachability_precheck", True)),
        )

    def ipmi_for(self, node: Node) -> Optional[IPMIClient]:
        """The IPMI client for *node*'s location, or None.

        None when that location has no reachable gateway, or when the node is
        not in the inventory (location 'unknown'): there is no gateway of its
        own to drive it from, and borrowing another site's would be wrong.
        """
        return self.ipmi_clients.get(node.location)

    def surface_credential_failure(self, notes: Optional[List[str]] = None
                                   ) -> Optional[str]:
        """Carry a disabled-fallbacks state into the notes and the run store.

        The decision is made deep in a worker thread, by
        KerberosManager.service_credential(); an operator reading the report
        has to see it. Idempotent: the note appears once in :attr:`notes`
        (which phases 1 and 2 copy into their results), once in *notes* when
        given (a phase's own list), and once in the store as an error event --
        recorded as soon as there is a run to record it against.
        """
        state = self.kerberos.fallbacks_disabled if self.kerberos else None
        if state is None:
            return None
        note = state.note()
        if note not in self.notes:
            self.notes.append(note)
        if notes is not None and note not in notes:
            notes.append(note)
        if not self._fallback_event_recorded and \
                getattr(self.store, "run_id", None) is not None:
            self.store.record_event(note, level="error")
            self._fallback_event_recorded = True
        return note

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
            ipmi=self.ipmi_for(node),
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
        #: Set when power.status found the BMC dark or the credential refused:
        #: power.sensors and power.sel would ask the same BMC the same
        #: question and learn nothing new, so they are UNKNOWN without a call.
        bmc_unread: Optional[CheckResult] = None
        for check_id in ids:
            if self.budget_expired():
                assessment.timed_out = True
                assessment.results.append(CheckResult(
                    node=node.hostname, check_id=check_id, status=Status.UNKNOWN,
                    summary=TIMEOUT_SUMMARY,
                    detail="run.phase_timeout ran out before this check was "
                           "reached; nothing was looked at"))
                continue
            if bmc_unread is not None and check_id in ("power.sensors",
                                                       "power.sel"):
                state = bmc_unread.data.get("state")
                why = ("the BMC did not answer" if state == "unreachable"
                       else "the IPMI credential was refused")
                assessment.results.append(CheckResult(
                    node=node.hostname, check_id=check_id, status=Status.UNKNOWN,
                    summary=f"not read: {why} (power.status)",
                    detail="skipped after power.status could not read the BMC; "
                           "asking it again would cost another IPMI timeout "
                           "and learn nothing",
                    data={"bmc": node.ipmi_host, "state": state}))
                continue
            if not reachable and check_id not in ("ping.lab", "power.status",
                                                  "power.sensors", "power.sel"):
                assessment.results.append(CheckResult(
                    node=node.hostname, check_id=check_id, status=Status.UNKNOWN,
                    summary="not run: no usable SSH session to this node",
                    detail="skipped after the login was refused -- every check "
                           "below needs a session, and retrying each one would "
                           "just be another refused connection"))
                continue
            res = run_check(check_id, ctx)
            assessment.results.append(res)
            if check_id == "power.status":
                assessment.power_state = res.data.get("state")
                if res.data.get("state") in (PowerState.UNREACHABLE.value,
                                             PowerState.REFUSED.value):
                    bmc_unread = res
                self.baselines.setdefault(node.hostname, {})
            if check_id == "power.sel" and res.data.get("records") is not None:
                # The first successful read is the survey every later phase
                # compares against, so it is kept, not overwritten. A failed
                # read carries no 'records' and records no baseline.
                self.baselines.setdefault(node.hostname, {}).setdefault(
                    "sel", res.data["records"])
            if check_id == "ssh.login" and res.status is Status.FAIL:
                # Every remaining check needs an ssh session, so once the login
                # is refused the rest can only fail the same way -- twelve more
                # times, each a fresh connection. On a host that is refusing
                # because of a rate limiter, that makes things worse.
                reachable = False
                ping = next((r for r in assessment.results
                             if r.check_id == "ping.lab"), None)
                # Answering ICMP but refusing ssh is a credential or sshd
                # problem, not a dead machine; only the latter is UNREACHABLE.
                assessment.unreachable = not (ping is not None
                                              and ping.status is Status.OK)
                log.info("%s refused the login; skipping its remaining "
                         "ssh checks", node.hostname)

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
        # Not a `with` block: its __exit__ waits for every queued node, so a
        # Ctrl-C or SIGTERM (raised here as KeyboardInterrupt) would sit
        # through the rest of the phase before cleanup could run.
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
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
        except BaseException:
            # Queued nodes are never started; nodes already running finish
            # their current command in the background.
            shutdown_now(pool)
            raise
        pool.shutdown(wait=True)
        # Before the caller copies self.notes into its phase result.
        self.surface_credential_failure()
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
                      "networks": a.node.networks,
                      # Kept so a page rebuilt from the store, and phase 4's
                      # reconciliation, can tell UNREACHABLE from FAIL.
                      "unreachable": a.unreachable,
                      "timed_out": a.timed_out})

    def close(self) -> None:
        # Unwinds credential_session(), whose finally runs
        # KerberosManager.cleanup().
        self._credentials.close()
        self.store.close()


def shutdown_now(pool: ThreadPoolExecutor) -> None:
    """Stop *pool* without waiting: cancel what is queued, leave what runs.

    For an interrupt (KeyboardInterrupt from Ctrl-C or the SIGTERM handler):
    the caller re-raises, and its ``finally`` -- Orchestrator.close(), which
    destroys the private Kerberos caches -- runs now rather than after every
    queued node has been assessed. ``cancel_futures`` is Python 3.9+.
    """
    log.warning("interrupted: cancelling queued work")
    pool.shutdown(wait=False, cancel_futures=True)


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
