"""Check framework: results, context, and the registry.

A check is a function ``(CheckContext) -> CheckResult`` registered under a
dotted id (``disk.local``, ``net.data``).  It gets everything it needs from the
context -- transports, thresholds, the node's topology entry -- and returns a
structured result.  It must not print, must not raise for an ordinary failure,
and must not decide on its own to change anything on the node.

That shape is what makes the same check bodies usable from phase 1 (read-only
assessment), phase 2 (post-power-on verification) and the standalone
diagnostics helpers, and testable against a :class:`FakeTransport`.
"""
from __future__ import annotations

import logging
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..transport.base import Command, CommandResult, Transport, TransportError

log = logging.getLogger(__name__)


class Status(str, Enum):
    """Outcome of one check.

    The distinction that matters operationally is FAIL vs UNKNOWN: FAIL means
    we looked and it is wrong; UNKNOWN means we could not look.  Collapsing
    them would make a machine that is merely unreachable indistinguishable from
    one that is broken, and those need different responses during a recovery.
    """

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"
    UNKNOWN = "unknown"

    @property
    def is_bad(self) -> bool:
        return self in (Status.FAIL, Status.UNKNOWN)

    @property
    def rank(self) -> int:
        """Severity order, used to reduce many results to one node verdict."""
        return {Status.OK: 0, Status.SKIP: 1, Status.WARN: 2,
                Status.UNKNOWN: 3, Status.FAIL: 4}[self]


def worst(statuses: Sequence["Status"]) -> Status:
    """The most severe status in *statuses*; OK for an empty sequence."""
    return max(statuses, key=lambda s: s.rank) if statuses else Status.OK


def rollup(statuses: Sequence["Status"]) -> Status:
    """Reduce a node's check results to one verdict.

    SKIP is dropped first.  A skipped check means "this does not apply to this
    host" -- a node with no BMC, a host with no PCIe card -- and a node whose
    every applicable check passed is healthy, not "not applicable".  Ranking
    SKIP above OK (which :func:`worst` does, correctly, for a single check) and
    then reducing with it would report most of the cluster as n/a.

    When *everything* was skipped the answer really is SKIP: nothing was
    examined, and saying OK would be a claim we did not test.
    """
    applicable = [s for s in statuses if s is not Status.SKIP]
    if not applicable:
        return Status.SKIP if statuses else Status.OK
    return max(applicable, key=lambda s: s.rank)


@dataclass
class CheckResult:
    """One check, on one node."""

    node: str
    check_id: str
    status: Status
    summary: str = ""
    detail: str = ""
    #: Raw command evidence, kept for the phase-4 report and the logbook entry.
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    #: Parsed values a report can tabulate (link speed, free bytes, ...).
    data: Dict[str, Any] = field(default_factory=dict)
    duration: float = 0.0
    started_at: str = ""

    @property
    def ok(self) -> bool:
        return self.status is Status.OK

    def as_dict(self) -> Dict[str, Any]:
        return {
            "node": self.node,
            "check_id": self.check_id,
            "status": self.status.value,
            "summary": self.summary,
            "detail": self.detail,
            "data": dict(self.data),
            "evidence": list(self.evidence),
            "duration": round(self.duration, 3),
            "started_at": self.started_at,
        }


class CheckContext:
    """Everything a check is allowed to touch.

    Transports are created lazily and cached: a node that never needs a root
    session should not have one opened for it, and a node that needs ten
    commands should not open ten sessions' worth of setup.
    """

    def __init__(self, node: Any, settings: Any, topology: Any,
                 ssh_factory: Any = None, ipmi: Any = None,
                 checks_config: Optional[Dict[str, Any]] = None,
                 local: Optional[Transport] = None,
                 baseline: Optional[Dict[str, Any]] = None):
        self.node = node
        self.settings = settings
        self.topology = topology
        self.ssh_factory = ssh_factory
        self.ipmi = ipmi
        self.checks_config = checks_config or {}
        self.local = local
        #: Values recorded earlier in the run that a check may compare against
        #: (e.g. the SEL entry count seen in phase 1, so phase 2 can report
        #: only the events the power-on itself produced).
        self.baseline = baseline or {}
        self._user_ssh: Optional[Transport] = None
        self._root_ssh: Optional[Transport] = None
        self._prober: Optional[Transport] = None
        self.evidence: List[Dict[str, Any]] = []

    # -- transports --------------------------------------------------------

    @property
    def ssh(self) -> Transport:
        if self._user_ssh is None:
            if self.ssh_factory is None:
                raise TransportError("no ssh factory configured for this context")
            self._user_ssh = self.ssh_factory.for_node(self.node)
        return self._user_ssh

    @property
    def root(self) -> Transport:
        if self._root_ssh is None:
            if self.ssh_factory is None:
                raise TransportError("no ssh factory configured for this context")
            self._root_ssh = self.ssh_factory.for_node(self.node, root=True)
        return self._root_ssh

    @property
    def prober(self) -> Transport:
        """Where network probes against this node are launched *from*.

        The operator's workstation is outside the DAQ networks, so pinging a
        node from here would test the site firewall rather than the cluster.
        Probes therefore run on the node's gateway -- which is also what
        Project-Description.md specifies ("From the gateway machines check that
        other machines are responding").  For a gateway node itself there is
        nothing closer, so the local transport is used and the probe measures
        the path an off-site operator actually has.
        """
        if self._prober is None:
            if self.node.node_class == "gateway" or self.ssh_factory is None:
                self._prober = self.local or self.ssh
            else:
                gw = self.ssh_factory.gateway_for(self.node.location)
                self._prober = (self.ssh_factory.for_host(gw, direct=True)
                                if gw else (self.local or self.ssh))
        return self._prober

    def probe(self, command: Command, timeout: Optional[float] = None) -> CommandResult:
        """Run a probe command from :attr:`prober`, recording it as evidence."""
        res = self.prober.run(command, timeout=timeout)
        self.evidence.append(res.as_dict())
        return res

    # -- command helpers ---------------------------------------------------

    def run(self, command: Command, root: bool = False,
            timeout: Optional[float] = None) -> CommandResult:
        """Run a command on the node, recording it as evidence."""
        transport = self.root if root else self.ssh
        result = transport.run(command, timeout=timeout)
        self.evidence.append(result.as_dict())
        return result

    def threshold(self, name: str, default: Any = None) -> Any:
        return self.checks_config.get("thresholds", {}).get(name, default)

    def config(self, section: str, default: Any = None) -> Any:
        return self.checks_config.get(section, default if default is not None else {})

    def take_evidence(self) -> List[Dict[str, Any]]:
        """Drain and return the evidence accumulated since the last drain."""
        out, self.evidence = self.evidence, []
        return out


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

CheckFn = Callable[[CheckContext], CheckResult]

#: check id -> implementation
REGISTRY: Dict[str, CheckFn] = {}
#: check id -> one-line description, for --list-checks and the API page
DESCRIPTIONS: Dict[str, str] = {}
#: check ids that need a root session, so a run without a root principal can
#: report them as SKIP up front instead of failing one ssh at a time.
NEEDS_ROOT: set = set()


def register(check_id: str, description: str = "", needs_root: bool = False):
    """Decorator registering a check implementation under *check_id*."""

    def decorator(fn: CheckFn) -> CheckFn:
        if check_id in REGISTRY:
            raise RuntimeError(f"duplicate check id {check_id!r}")
        REGISTRY[check_id] = fn
        DESCRIPTIONS[check_id] = description or (fn.__doc__ or "").strip().splitlines()[0:1] and \
            (fn.__doc__ or "").strip().splitlines()[0] or check_id
        if needs_root:
            NEEDS_ROOT.add(check_id)
        return fn

    return decorator


# ---------------------------------------------------------------------------
# Result constructors -- checks build results through these so that node id,
# timing and evidence handling are identical everywhere.
# ---------------------------------------------------------------------------


def result(ctx: CheckContext, check_id: str, status: Status, summary: str,
           detail: str = "", data: Optional[Dict[str, Any]] = None,
           started: Optional[float] = None) -> CheckResult:
    return CheckResult(
        node=ctx.node.hostname,
        check_id=check_id,
        status=status,
        summary=summary,
        detail=detail,
        data=data or {},
        evidence=ctx.take_evidence(),
        duration=(time.monotonic() - started) if started else 0.0,
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def run_check(check_id: str, ctx: CheckContext) -> CheckResult:
    """Run one registered check, converting every escape into a result.

    A check that raises is a bug, but a bug in one check must not abort the
    assessment of a 50-node cluster during an outage -- so the traceback lands
    in the result's detail and the run continues.  A transport failure is
    UNKNOWN (we could not look), not FAIL (it is broken).
    """
    fn = REGISTRY.get(check_id)
    started = time.monotonic()
    if fn is None:
        return CheckResult(node=ctx.node.hostname, check_id=check_id,
                           status=Status.SKIP,
                           summary="no implementation for this check id",
                           detail=f"{check_id} is listed in checks.yaml but is not "
                                  f"registered in mu2edaq_power_recovery.checks",
                           started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    try:
        res = fn(ctx)
    except TransportError as exc:
        return result(ctx, check_id, Status.UNKNOWN,
                      "could not reach the node to run this check", str(exc),
                      started=started)
    except Exception as exc:  # noqa: BLE001 - deliberate catch-all, see docstring
        log.exception("check %s raised on %s", check_id, ctx.node.hostname)
        return result(ctx, check_id, Status.UNKNOWN,
                      f"check raised {type(exc).__name__}",
                      traceback.format_exc(limit=5), started=started)
    if res.duration == 0.0:
        res.duration = time.monotonic() - started
    return res


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------


def profile_checks(checks_config: Dict[str, Any], profile: str) -> List[str]:
    """Check ids for *profile*, with 'base' merged in and order preserved.

    'base' comes first because its members (reachability, login) are the ones
    whose failure explains every later failure; running them first means the
    operator reading down a node's result list sees the cause before the
    symptoms.
    """
    profiles = checks_config.get("profiles", {}) or {}
    ids: List[str] = list(profiles.get("base", []) or [])
    for check_id in profiles.get(profile, []) or []:
        if check_id not in ids:
            ids.append(check_id)
    return ids


def profile_for_node(checks_config: Dict[str, Any], node: Any) -> str:
    """The profile name to use for *node*, falling back to 'other'."""
    profiles = checks_config.get("profiles", {}) or {}
    return node.node_class if node.node_class in profiles else "other"
