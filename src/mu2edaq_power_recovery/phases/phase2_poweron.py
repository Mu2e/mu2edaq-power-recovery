"""Phase 2 -- bring the cluster up, in order, verifying as it goes.

The sequence itself is data (config/power-sequence.yaml); this module is the
engine that walks it.  For each stage:

    read power state -> power on what is off -> wait for SSH -> settle
      -> run the stage's check profile -> decide whether the stage passed

A stage's ``require:`` (all / majority / any) decides whether the next stage
starts.  The default is ``all``, and ``run.stop_on_stage_failure`` decides
whether a failed stage aborts the sequence or is merely recorded -- an operator
recovering at 3 a.m. usually wants to be stopped; one doing a planned restart
with a known-dead node wants ``--continue-on-error``.

Scope
-----
What the phase may touch is decided up front by :func:`plan_sequence`, from
``--location``, ``--node`` and ``--from``/``--until``, before any credential is
acquired; a selection that cannot be honoured is a
:class:`SequenceSelectionError`, never a silent widening.  With ``--node`` the
stages holding the named nodes are cut down to them, and the stages before
them are *predecessors*: verified (power state read, waited for, checked) but
never sent a power command.  A predecessor stage that is not up stops the run
before the requested nodes' stage.  :func:`_power_stage` refuses any host
outside :attr:`SequencePlan.allowed_power` as a second line of defence.

Time
----
``run.phase_timeout`` is a :class:`~.base.Deadline` over the whole phase,
checked before every stage and every node's power command, and it caps every
ssh call's timeout (see :meth:`Orchestrator.budget`).  Each stage's boot wait
runs concurrently under one stage deadline, ``now + min(boot_timeout, phase
time left)``.  Stages and nodes the budget never reached are UNKNOWN with
``TIMEOUT_SUMMARY``.

Nothing here issues a power command directly: every one goes through
:meth:`IPMIClient.ensure_on`, which enforces the protected-host list and the
dry-run gate, and every attempt is written to the run store before it is made.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, List, Optional, Sequence

from ..checks import CheckResult, Status
from ..orchestrator import NodeAssessment, Orchestrator
from ..topology import Node, Topology, TopologyError, expand_entries
from ..transport.ipmi import IPMIError
from .base import (TIMEOUT_SUMMARY, Deadline, PhaseResult, overall_status,
                   phase_deadline)

log = logging.getLogger(__name__)

PHASE_NAME = "poweron"
PHASE_NUMBER = 2
PHASE_TITLE = "Power on"

#: Seconds between starting successive boot waits in a stage, so a stage's
#: nodes -- all switched on within seconds of each other -- do not all open
#: their first ssh connection at the same instant against sshd instances
#: that have only just started.
WAIT_STAGGER = 0.5

#: Stage roles in a :class:`SequencePlan`.
ROLE_FULL = "full"                  # unscoped: the whole stage, as configured
ROLE_TARGET = "target"              # holds a --node host; cut to those hosts
ROLE_PREDECESSOR = "predecessor"    # before a target stage: verify only
ROLE_OUT_OF_SCOPE = "out_of_scope"  # in the slice, but not run: SKIP


class SequenceSelectionError(ValueError):
    """--from/--until/--node/--location select something that cannot be run.

    Raised before credentials are acquired; the CLI turns it into exit 2.
    """


class StageOutcome(dict):
    """One stage's result -- a dict so it serialises straight into the store."""


@dataclass
class PlannedStage:
    """One stage of the plan: the configured stage, its nodes and its role."""

    stage: Dict[str, Any]
    nodes: List[Node]
    role: str = ROLE_FULL
    location: str = ""
    #: Why an out-of-scope stage is not run, for its SKIP row.
    reason: str = ""

    @property
    def name(self) -> str:
        return str(self.stage.get("name", "unnamed"))

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "role": self.role, "location": self.location,
                "nodes": [n.hostname for n in self.nodes], "reason": self.reason}


@dataclass
class SequencePlan:
    """What phase 2 will do, decided before anything is contacted."""

    stages: List[PlannedStage] = field(default_factory=list)
    notices: List[str] = field(default_factory=list)
    #: Hostnames phase 2 may send ``chassis power on`` to.  Nothing else.
    allowed_power: FrozenSet[str] = frozenset()
    requested: List[str] = field(default_factory=list)
    locations: List[str] = field(default_factory=list)
    from_stage: Optional[str] = None
    until_stage: Optional[str] = None
    defaults: Dict[str, Any] = field(default_factory=dict)

    @property
    def scoped(self) -> bool:
        return bool(self.requested)

    def as_dict(self) -> Dict[str, Any]:
        return {"stages": [s.as_dict() for s in self.stages],
                "notices": list(self.notices),
                "allowed_power": sorted(self.allowed_power),
                "requested": list(self.requested),
                "locations": list(self.locations),
                "from_stage": self.from_stage, "until_stage": self.until_stage}


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _slice_bounds(stages: Sequence[Dict[str, Any]], from_stage: Optional[str],
                  until_stage: Optional[str]) -> range:
    """Indices selected by --from/--until, inclusive at both ends.

    Every problem is an error that lists the valid names: an unknown name, a
    reversed range, and a sequence whose stage names are missing or repeated
    (which would make a name ambiguous).  The old behaviour -- an unknown name
    silently meant "from the start" or "to the end" -- turned a typo in a
    safety bound into a power-on of the whole sequence.
    """
    names = [s.get("name") for s in stages]
    unnamed = [i + 1 for i, n in enumerate(names) if not n]
    if unnamed:
        raise SequenceSelectionError(
            f"power sequence stage(s) at position {', '.join(map(str, unnamed))} "
            f"have no name; every stage needs a unique 'name:'")
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise SequenceSelectionError(
            f"power sequence stage name(s) used more than once: "
            f"{', '.join(duplicates)}; stage names must be unique")
    valid = ", ".join(names)
    for flag, value in (("--from", from_stage), ("--until", until_stage)):
        if value is not None and value not in names:
            raise SequenceSelectionError(
                f"{flag} {value!r} is not a stage of the power sequence; "
                f"valid stages, in order: {valid}")
    start = names.index(from_stage) if from_stage is not None else 0
    end = names.index(until_stage) + 1 if until_stage is not None else len(names)
    if end <= start:
        raise SequenceSelectionError(
            f"--from {from_stage} comes after --until {until_stage} in the power "
            f"sequence; stages run in this order: {valid}")
    return range(start, end)


def _slice_stages(stages: List[Dict[str, Any]], from_stage: Optional[str],
                  until_stage: Optional[str]) -> List[Dict[str, Any]]:
    """Apply --from / --until by stage name, inclusive at both ends.

    Raises :class:`SequenceSelectionError` for anything it cannot honour
    exactly; see :func:`_slice_bounds`.
    """
    if not stages:
        return []
    return [stages[i] for i in _slice_bounds(stages, from_stage, until_stage)]


def _stage_location(topology: Topology, stage: Dict[str, Any],
                    defaults: Dict[str, Any]) -> str:
    raw = stage.get("location", defaults.get("location", "mc2"))
    try:
        return topology.canonical_location(raw)
    except TopologyError as exc:
        raise SequenceSelectionError(
            f"power sequence stage {stage.get('name')!r}: {exc}") from exc


def _expand_stage(topology: Topology, stage: Dict[str, Any], location: str) -> List[Node]:
    """A stage's nodes, every name validated (``valid_hostname``) up front.

    Every stage of the sequence is expanded when the plan is made, not only
    the selected ones, so a bad name anywhere in power-sequence.yaml stops the
    run before anything is contacted -- never mid-sequence, after earlier
    stages have already been powered.
    """
    try:
        names = expand_entries(stage.get("nodes", []) or [], topology.domain,
                               topology.default_prefix)
        return topology.resolve(names, [location])
    except TopologyError as exc:
        raise SequenceSelectionError(
            f"power sequence stage {stage.get('name')!r}: {exc}") from exc


def _power_enabled(stage: Dict[str, Any], defaults: Dict[str, Any]) -> bool:
    return bool(stage.get("power_on", defaults.get("power_on", True)))


def plan_sequence(config: Dict[str, Any], topology: Topology,
                  locations: Sequence[str],
                  node_names: Optional[Sequence[str]] = None,
                  from_stage: Optional[str] = None,
                  until_stage: Optional[str] = None) -> SequencePlan:
    """Decide which stages run, over which nodes, and which may be powered.

    * ``--from``/``--until`` must name stages exactly (see :func:`_slice_bounds`).
    * Stages whose location is outside *locations* are not run, with a notice;
      if none is left the selection is an error.
    * With *node_names*: each must be in a stage of the selected range and
      location, or it is an error that names the stage(s) it is in.  Those
      stages are cut to the named nodes (``target``).  The stages from the
      start of the range up to the last target stage are ``predecessor``s:
      verify only, never powered.  Stages after the last target are not run.
      ``--from`` therefore bounds how far back predecessors reach.
    * :attr:`SequencePlan.allowed_power` is the set of hosts that may be sent
      ``chassis power on``: the nodes of full and target stages whose stage
      has ``power_on`` enabled.

    Without *node_names*, and with *locations* covering the sequence, the plan
    is the sliced sequence unchanged.  Raises :class:`SequenceSelectionError`
    (and :class:`TopologyError` for an invalid hostname).
    """
    config = config or {}
    defaults = dict(config.get("defaults", {}) or {})
    stages = list(config.get("stages", []) or [])
    try:
        wanted = [topology.canonical_location(loc) for loc in locations]
    except TopologyError as exc:
        raise SequenceSelectionError(str(exc)) from exc

    plan = SequencePlan(locations=wanted, from_stage=from_stage,
                        until_stage=until_stage, defaults=defaults)
    if not stages:
        if node_names:
            raise SequenceSelectionError(
                "the power sequence defines no stages, so phase 2 cannot power "
                "on the requested node(s)")
        return plan

    bounds = _slice_bounds(stages, from_stage, until_stage)
    stage_loc = [_stage_location(topology, s, defaults) for s in stages]
    stage_nodes = [_expand_stage(topology, s, loc)
                   for s, loc in zip(stages, stage_loc)]
    names = [str(s.get("name")) for s in stages]
    range_text = f"--from {names[bounds.start]} --until {names[bounds.stop - 1]}"

    located = [i for i in bounds if stage_loc[i] in wanted]
    elsewhere = [i for i in bounds if stage_loc[i] not in wanted]
    if not located:
        raise SequenceSelectionError(
            f"no stage of the selected power sequence ({range_text}) is in the "
            f"requested location(s) {', '.join(wanted)}; its stages are in "
            f"{', '.join(sorted({stage_loc[i] for i in bounds}))}")
    if elsewhere:
        plan.notices.append(
            f"--location {','.join(wanted)}: stage(s) "
            + ", ".join(f"'{names[i]}' ({stage_loc[i]})" for i in elsewhere)
            + " are in another location and will not be run")

    targets: Dict[int, List[str]] = {}
    if node_names:
        for node in topology.resolve(list(node_names), wanted):
            host = node.hostname
            if host in plan.requested:
                continue
            plan.requested.append(host)
            holding = [i for i, nodes in enumerate(stage_nodes)
                       if any(n.hostname == host for n in nodes)]
            if not holding:
                raise SequenceSelectionError(
                    f"{node.short} is not in any stage of the power sequence, so "
                    f"phase 2 has nothing to do for it (stages: "
                    f"{', '.join(names)}); add it to config/power-sequence.yaml "
                    f"or leave it out of --node")
            usable = [i for i in holding if i in located]
            if not usable:
                why = []
                for i in holding:
                    if i not in bounds:
                        why.append(f"stage '{names[i]}', outside {range_text}")
                    else:
                        why.append(f"stage '{names[i]}', in location "
                                   f"{stage_loc[i]}, outside --location "
                                   f"{','.join(wanted)}")
                raise SequenceSelectionError(
                    f"{node.short} is only in {'; '.join(why)}; widen the "
                    f"selection or leave it out of --node")
            for i in usable:
                targets.setdefault(i, []).append(host)

    last_target = max(targets) if targets else None
    for i in bounds:
        planned = PlannedStage(stage=stages[i], nodes=list(stage_nodes[i]),
                               location=stage_loc[i])
        if i in elsewhere:
            planned.role, planned.nodes = ROLE_OUT_OF_SCOPE, []
            planned.reason = (f"location {stage_loc[i]} is outside --location "
                              f"{','.join(wanted)}")
        elif last_target is None:
            planned.role = ROLE_FULL
        elif i in targets:
            planned.role = ROLE_TARGET
            planned.nodes = [n for n in stage_nodes[i] if n.hostname in targets[i]]
            if len(planned.nodes) < len(stage_nodes[i]):
                plan.notices.append(
                    f"stage '{names[i]}' is limited to {len(planned.nodes)} of its "
                    f"{len(stage_nodes[i])} node(s): "
                    + ", ".join(n.short for n in planned.nodes))
        elif i < last_target:
            planned.role = ROLE_PREDECESSOR
        else:
            planned.role, planned.nodes = ROLE_OUT_OF_SCOPE, []
            planned.reason = "after the last stage holding a --node host"
        plan.stages.append(planned)

    predecessors = [p.name for p in plan.stages if p.role == ROLE_PREDECESSOR]
    if predecessors and last_target is not None:
        plan.notices.append(
            f"--node: stage(s) {', '.join(predecessors)} precede "
            f"'{names[last_target]}' and are included VERIFY-ONLY -- power state "
            f"read, ssh waited for, checks run, but no power command is sent. If "
            f"one of them is not up, the run stops before the requested "
            f"node(s); power it on by running that stage explicitly "
            f"(--from <stage> --until <stage>)")
    later = [p.name for p in plan.stages
             if p.role == ROLE_OUT_OF_SCOPE and p.reason.startswith("after")]
    if later:
        plan.notices.append(
            f"--node: stage(s) {', '.join(later)} come after the requested "
            f"node(s) and will not be run")

    plan.allowed_power = frozenset(
        n.hostname for p in plan.stages if p.role in (ROLE_FULL, ROLE_TARGET)
        and _power_enabled(p.stage, defaults) for n in p.nodes)
    return plan


# ---------------------------------------------------------------------------
# Stage execution
# ---------------------------------------------------------------------------


def _stage_nodes(orch: Orchestrator, stage: Dict[str, Any],
                 defaults: Dict[str, Any]) -> List[Node]:
    """Expand a stage's node list against the topology."""
    location = stage.get("location", defaults.get("location", "mc2"))
    names = expand_entries(stage.get("nodes", []), orch.topology.domain,
                           orch.topology.default_prefix)
    return orch.topology.resolve(names, [location])


def _requirement_met(requirement: str, assessments: Sequence[NodeAssessment],
                     also_bad: Sequence[str] = ()) -> bool:
    """Whether a stage satisfied its require: setting.

    *also_bad* are hostnames counted as failures whatever their checks said:
    a predecessor that is off or never answered ssh.
    """
    if not assessments:
        return False
    good = [a for a in assessments if not a.status.is_bad
            and not (also_bad and a.node.hostname in also_bad)]
    if requirement == "any":
        return bool(good)
    if requirement == "majority":
        return len(good) * 2 > len(assessments)
    return len(good) == len(assessments)


def _record(orch: Orchestrator, node: Node, outcome: Dict[str, Any],
            dry_run: bool, target: Optional[str] = None) -> None:
    orch.store.record_action(node.hostname, "power_on",
                             target or node.ipmi_host or "(none)",
                             outcome["action"], dry_run, outcome.get("detail", ""))


def _power_stage(orch: Orchestrator, nodes: Sequence[Node],
                 stage: Dict[str, Any],
                 allowed: Optional[FrozenSet[str]] = None,
                 defaults: Optional[Dict[str, Any]] = None
                 ) -> Dict[str, Dict[str, Any]]:
    """Ensure every node in the stage is powered on.

    Returns hostname -> the dict from ``ensure_on``.  A node whose BMC is not
    in the topology is recorded as 'no_bmc' rather than skipped silently: on a
    post-outage checklist, "we could not power this one on" must be visible.

    *allowed* is the plan's :attr:`SequencePlan.allowed_power`.  A host outside
    it is refused -- recorded as action ``out_of_scope`` and never passed to
    ``ensure_on`` -- whatever the stage list says.  The plan already keeps such
    hosts out; this is the second of two independent checks.

    Each node is driven through its own location's IPMI client
    (:meth:`Orchestrator.ipmi_for`).  Commands are issued one node at a time:
    the credential breaker serialises them until the BMC account is proven
    anyway, and spacing chassis power-ons spreads the inrush.
    """
    outcomes: Dict[str, Dict[str, Any]] = {}
    dry_run = bool(orch.settings.get("run.dry_run", True))

    if not _power_enabled(stage, defaults or {}):
        for node in nodes:
            outcomes[node.hostname] = {"action": "verify_only", "ok": True,
                                       "detail": "stage is verify-only"}
        return outcomes

    for node in nodes:
        if allowed is not None and node.hostname not in allowed:
            outcomes[node.hostname] = {
                "action": "out_of_scope", "ok": False,
                "detail": "refused: not in this run's --node/--location scope"}
            log.error("%s: refusing a power command outside the requested scope",
                      node.hostname)
            _record(orch, node, outcomes[node.hostname], dry_run)
            continue
        if orch.budget_expired():
            outcomes[node.hostname] = {"action": "not_run", "ok": False,
                                       "detail": TIMEOUT_SUMMARY}
            _record(orch, node, outcomes[node.hostname], dry_run)
            continue
        ipmi = orch.ipmi_for(node)
        if ipmi is None:
            outcomes[node.hostname] = {
                "action": "unavailable", "ok": False,
                "detail": f"no IPMI client for {node.location}: credentials or "
                          f"gateway unavailable"}
            _record(orch, node, outcomes[node.hostname], dry_run)
            continue
        if not node.ipmi_host:
            outcomes[node.hostname] = {"action": "no_bmc", "ok": False,
                                       "detail": "no BMC listed in the topology"}
            _record(orch, node, outcomes[node.hostname], dry_run, "(none)")
            continue
        # Recorded before the command is issued, so a tool that dies mid-action
        # still leaves evidence of what it was attempting.
        orch.store.record_action(node.hostname, "power_on", node.ipmi_host,
                                 "attempting", dry_run, "")
        try:
            outcome = ipmi.ensure_on(node.ipmi_host, node_host=node.hostname)
        except IPMIError as exc:
            # One node's IPMI failure is that node's outcome, not the run's:
            # the rest of the stage still gets its commands and the phase is
            # still finished and recorded (PR #30 review).
            outcome = {"action": "failed", "ok": False,
                       "detail": f"IPMI error: {exc}"}
            log.error("%s: %s", node.hostname, outcome["detail"])
        outcomes[node.hostname] = outcome
        _record(orch, node, outcome, dry_run)
        log.info("%s: %s (%s)", node.short, outcome["action"], outcome["detail"])
    return outcomes


def _verify_power(orch: Orchestrator, nodes: Sequence[Node]
                  ) -> Dict[str, Dict[str, Any]]:
    """A predecessor's power state, read and never changed.

    Only ``chassis power status`` is sent.  A node that is off is marked
    ``predecessor_off``, which blocks the target stage.
    """
    outcomes: Dict[str, Dict[str, Any]] = {}
    for node in nodes:
        ipmi = orch.ipmi_for(node)
        state = None
        if ipmi is not None and node.ipmi_host and not orch.budget_expired():
            state = ipmi.power_status(node.ipmi_host).value
        if state == "off":
            outcomes[node.hostname] = {
                "action": "predecessor_off", "ok": False, "before": state,
                "after": state,
                "detail": "off; a predecessor is verified, never powered"}
        else:
            outcomes[node.hostname] = {
                "action": "verify", "ok": True, "before": state, "after": state,
                "detail": f"predecessor, verify only (power {state or 'not read'})"}
    return outcomes


#: Actions after which a node is not waited for at all.
_NO_WAIT = ("dry_run", "predecessor_off", "out_of_scope", "not_run")


def _wait_for_nodes(orch: Orchestrator, nodes: Sequence[Node],
                    stage: Dict[str, Any], defaults: Dict[str, Any],
                    powered: Dict[str, Dict[str, Any]],
                    deadline: Optional[Deadline] = None) -> Dict[str, bool]:
    """Wait, concurrently, for the stage's nodes to answer SSH.

    Every node is waited for at once under one stage deadline, ``now +
    min(boot_timeout, phase time left)``, so a stage of N nodes that never
    answer costs one boot_timeout, not N of them.  Concurrent ssh *attempts*
    are bounded by a semaphore of ``ssh.max_sessions``; the sleeps between a
    node's attempts hold nothing, so a node queued behind a slow one still
    gets polled before the deadline.  Starts are staggered by
    :data:`WAIT_STAGGER`, so freshly started sshds do not all see their first
    connection in the same instant.

    A node already on gets one quick probe; one that was just switched on, or
    whose state is uncertain, is polled until the deadline.
    """
    budget = float(stage.get("boot_timeout", defaults.get("boot_timeout", 600)))
    delay = float(orch.settings.get("ipmi.power_on_delay", 20))
    phase = deadline if deadline is not None else Deadline(None, orch.clock)
    answered: Dict[str, bool] = {}

    if not orch.simulate and \
            any(powered.get(n.hostname, {}).get("action") == "power_on" for n in nodes):
        pause = min(delay, phase.remaining())
        log.info("waiting %.0fs for the BMCs to release power", pause)
        orch.sleep(pause)

    stage_deadline = phase.child(budget)
    jobs: List[Any] = []
    for node in nodes:
        action = powered.get(node.hostname, {}).get("action")
        if action in _NO_WAIT:
            # dry_run: nothing was switched on; the others were never meant to
            # come up in this run.
            answered[node.hostname] = action == "dry_run"
            continue
        jobs.append((node, action))
    if not jobs:
        return answered

    sessions = threading.BoundedSemaphore(
        max(1, int(orch.settings.get("ssh.max_sessions", 16))))

    def wait(node: Node, action: Optional[str]) -> bool:
        transport = orch.ssh_factory.for_node(node)
        if action == "none":
            # Already on: one quick probe rather than the full boot budget.
            probe = getattr(transport, "alive", None)
            if probe is None:
                return True
            with sessions:
                return bool(probe())
        waiter = getattr(transport, "wait_for_ssh", None)
        if waiter is None:                            # simulated transport
            return True
        log.info("waiting up to %.0fs for %s to answer ssh",
                 min(budget, stage_deadline.remaining()), node.short)
        return bool(waiter(budget, deadline=stage_deadline, clock=orch.clock,
                           sleep=orch.sleep, gate=sessions))

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = []
        for index, (node, action) in enumerate(jobs):
            if index and not orch.simulate:
                orch.sleep(WAIT_STAGGER)
            futures.append((node, pool.submit(wait, node, action)))
        for node, future in futures:
            try:
                answered[node.hostname] = future.result()
            except Exception as exc:  # noqa: BLE001 - one node must not sink the stage
                log.warning("waiting for %s failed: %s", node.short, exc)
                answered[node.hostname] = False
    return answered


def _timed_out_assessment(node: Node) -> NodeAssessment:
    return NodeAssessment(node=node, timed_out=True, results=[CheckResult(
        node=node.hostname, check_id="phase.timeout", status=Status.UNKNOWN,
        summary=TIMEOUT_SUMMARY,
        detail="run.phase_timeout ran out before this node's stage started")])


def _timed_out_stage(orch: Orchestrator, planned: PlannedStage) -> StageOutcome:
    assessments = [_timed_out_assessment(n) for n in planned.nodes]
    orch.record(assessments)
    return StageOutcome(
        name=planned.name, title=planned.stage.get("title", planned.name),
        role=planned.role, status=Status.UNKNOWN.value, summary=TIMEOUT_SUMMARY,
        require=planned.stage.get("require", "all"), met=False, timed_out=True,
        nodes=[n.hostname for n in planned.nodes], power={}, answered={},
        assessments=assessments, duration=0.0)


def run_stage(orch: Orchestrator, stage: Any, defaults: Dict[str, Any],
              progress: Optional[Any] = None,
              plan: Optional[SequencePlan] = None,
              deadline: Optional[Deadline] = None) -> StageOutcome:
    """Power on (or, for a predecessor, verify), wait for, and check one stage.

    *stage* is a :class:`PlannedStage`, or a raw stage dict (planned in full).
    """
    if not isinstance(stage, PlannedStage):
        stage = PlannedStage(stage=stage, nodes=_stage_nodes(orch, stage, defaults))
    planned = stage
    raw = planned.stage
    name = planned.name
    title = raw.get("title", name)
    nodes = planned.nodes
    requirement = raw.get("require", defaults.get("require", "all"))
    concurrency = int(raw.get("concurrency", defaults.get("concurrency", 8)))
    settle = float(raw.get("settle", defaults.get("settle", 30)))
    started = orch.clock()
    base = dict(name=name, title=title, role=planned.role)

    if planned.role == ROLE_OUT_OF_SCOPE:
        orch.store.record_event(f"stage '{name}' not run: outside requested "
                                f"scope ({planned.reason})")
        return StageOutcome(**base, status=Status.SKIP.value,
                            summary=f"outside requested scope: {planned.reason}",
                            nodes=[], assessments=[], power={}, answered={},
                            met=True, duration=0.0)

    verify_only = planned.role == ROLE_PREDECESSOR
    log.info("stage %s (%s): %d node(s)%s", name, title, len(nodes),
             " [predecessor: verify only]" if verify_only else "")
    orch.store.record_event(
        f"stage '{name}' started over {len(nodes)} node(s)"
        + (" [predecessor: verify only]" if verify_only else ""))

    if not nodes:
        return StageOutcome(**base, status=Status.SKIP.value,
                            summary="no nodes resolved for this stage",
                            nodes=[], assessments=[], power={}, answered={},
                            met=True, duration=0.0)

    if verify_only:
        powered = _verify_power(orch, nodes)
    else:
        powered = _power_stage(orch, nodes, raw,
                               plan.allowed_power if plan is not None else None,
                               defaults)
    answered = _wait_for_nodes(orch, nodes, raw, defaults, powered, deadline)

    # Settle only if something actually booted -- there is no reason to wait 30
    # seconds for a stage whose nodes were all already running.
    if settle and not orch.simulate and \
            any(powered.get(n.hostname, {}).get("action") == "power_on" for n in nodes):
        pause = settle if deadline is None else min(settle, deadline.remaining())
        log.info("settling for %.0fs before checking stage %s", pause, name)
        orch.sleep(pause)

    # A node that has just been switched on should show a short uptime; telling
    # host.uptime so lets it flag a chassis that never actually rebooted.
    for node in nodes:
        if powered.get(node.hostname, {}).get("action") == "power_on":
            orch.baselines.setdefault(node.hostname, {})["expect_recent_boot"] = True

    assessments = orch.assess_nodes(nodes, profile=raw.get("checks"),
                                    concurrency=concurrency, progress=progress)
    for a in assessments:
        a.power_action = powered.get(a.node.hostname)

    blocked: Dict[str, str] = {}
    if verify_only:
        for node in nodes:
            if powered[node.hostname]["action"] == "predecessor_off":
                blocked[node.hostname] = f"{node.short} is off"
            elif not answered.get(node.hostname, True):
                blocked[node.hostname] = (f"{node.short} did not answer ssh "
                                          f"within the stage's boot wait")

    met = _requirement_met(requirement, assessments, also_bad=list(blocked))
    status = overall_status(assessments)
    if blocked and status.rank < Status.FAIL.rank:
        status = Status.FAIL          # looked, and a dependency is down
    good = [a for a in assessments
            if not a.status.is_bad and a.node.hostname not in blocked]
    summary = (f"{len(good)}/{len(assessments)} node(s) verified"
               f" (require: {requirement})")
    if verify_only:
        summary += "; predecessor, verify only"
    no_ssh = [h for h, ok in answered.items() if not ok]
    if no_ssh:
        summary += f"; {len(no_ssh)} never answered ssh"
    if blocked:
        summary += f"; {len(blocked)} not up"
    timed_out = [a.node.hostname for a in assessments if a.timed_out]
    if timed_out:
        summary += f"; {len(timed_out)} not fully checked (phase_timeout)"

    orch.record(assessments)
    orch.store.record_event(
        f"stage '{name}' {'passed' if met else 'FAILED'}: {summary}",
        level="info" if met else "error")

    return StageOutcome(
        **base, status=status.value, summary=summary,
        require=requirement, met=met,
        nodes=[n.hostname for n in nodes],
        power={h: v for h, v in powered.items()},
        answered=answered,
        blocked=blocked,
        assessments=assessments,
        duration=orch.clock() - started,
    )


def run(orch: Orchestrator, progress: Optional[Any] = None,
        from_stage: Optional[str] = None,
        until_stage: Optional[str] = None,
        plan: Optional[SequencePlan] = None) -> PhaseResult:
    """Walk the power-on sequence.

    *plan* is normally made by the CLI with :func:`plan_sequence` before any
    credential is acquired; without one the whole sequence is planned here,
    sliced by *from_stage*/*until_stage* (or ``run.from_stage`` /
    ``run.until_stage``) over the run's locations, and a bad selection raises
    :class:`SequenceSelectionError`.
    """
    started = orch.clock()
    result = PhaseResult(name=PHASE_NAME, number=PHASE_NUMBER, title=PHASE_TITLE,
                         started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    config = orch.sequence_config or {}
    result.notes.extend(orch.empty_location_notes())
    if not (config.get("stages") or []):
        result.status = Status.UNKNOWN
        result.summary = "no stages defined in the power sequence"
        return result

    if plan is None:
        plan = plan_sequence(
            config, orch.topology, orch.locations, None,
            from_stage or orch.settings.get("run.from_stage"),
            until_stage or orch.settings.get("run.until_stage"))
    defaults = plan.defaults

    orch.store.start_phase(PHASE_NAME, PHASE_NUMBER)
    dry_run = bool(orch.settings.get("run.dry_run", True))
    if dry_run:
        result.notes.append(
            "DRY RUN: power states were read but no chassis was switched on. "
            "Re-run with --execute to perform the sequence.")
    for notice in plan.notices:
        result.notes.append(notice)
        orch.store.record_event(f"scope: {notice}", level="warning")
    orch.store.record_event(f"phase 2 (power on) started, {len(plan.stages)} stage(s)"
                            + (" [dry run]" if dry_run else " [LIVE]"))

    outcomes: List[StageOutcome] = []
    stop_on_failure = bool(orch.settings.get("run.stop_on_stage_failure", True))
    aborted_at: Optional[str] = None
    blocked_at: Optional[str] = None
    timed_out = False
    deadline = phase_deadline(orch)

    with orch.budget(deadline):
        for index, planned in enumerate(plan.stages):
            if deadline.expired():
                timed_out = True
                for rest in plan.stages[index:]:
                    if rest.role == ROLE_OUT_OF_SCOPE:
                        continue
                    outcome = _timed_out_stage(orch, rest)
                    outcomes.append(outcome)
                    result.assessments.extend(outcome["assessments"])
                note = (f"run.phase_timeout ({deadline.budget:.0f}s) expired before "
                        f"stage '{planned.name}'; it and every later stage are "
                        f"UNKNOWN ({TIMEOUT_SUMMARY}). Resume with --from "
                        f"{planned.name}.")
                result.notes.append(note)
                log.error(note)
                orch.store.record_event(note, level="error")
                break

            outcome = run_stage(orch, planned, defaults, progress=progress,
                                plan=plan, deadline=deadline)
            outcomes.append(outcome)
            result.assessments.extend(outcome.get("assessments", []))
            if planned.role == ROLE_PREDECESSOR and not outcome["met"]:
                # Always stop, --continue-on-error or not: powering the
                # requested nodes with a dependency down is what a scoped run
                # must not do, and the predecessor was deliberately not
                # powered, so only the operator can fix it.
                blocked_at = aborted_at = outcome["name"]
                target = next((p.name for p in plan.stages
                               if p.role == ROLE_TARGET), "the requested stage")
                problems = "; ".join(outcome.get("blocked", {}).values()) or \
                    outcome["summary"]
                result.notes.append(
                    f"stopped before '{target}': predecessor stage "
                    f"'{outcome['name']}' is not up ({problems}). It was only "
                    f"verified -- a --node run never powers a predecessor -- so "
                    f"nothing in '{target}' was switched on. Bring it up by "
                    f"running that stage explicitly, e.g. mu2e-power-on "
                    f"--execute --from {outcome['name']} --until "
                    f"{outcome['name']}, then re-run this command.")
                log.error(result.notes[-1])
                orch.store.record_event(result.notes[-1], level="error")
                break
            if not outcome["met"] and stop_on_failure:
                aborted_at = outcome["name"]
                result.notes.append(
                    f"sequence stopped after stage '{outcome['name']}' did not meet "
                    f"its '{outcome['require']}' requirement. Later stages depend on "
                    f"it, so continuing would produce failures that say nothing new. "
                    f"Fix and re-run with --from {outcome['name']}, or use "
                    f"--continue-on-error.")
                log.error(result.notes[-1])
                orch.store.record_event(result.notes[-1], level="error")
                break

    result.status = overall_status(result.assessments)
    if blocked_at is not None:
        result.status = Status.FAIL
    elif timed_out and result.status.rank < Status.UNKNOWN.rank:
        result.status = Status.UNKNOWN
    passed = sum(1 for o in outcomes if o["met"])
    result.summary = (f"{passed}/{len(outcomes)} stage(s) met their requirement"
                      + (f"; aborted at '{aborted_at}'" if aborted_at else "")
                      + ("; phase_timeout expired" if timed_out else ""))
    result.data = {
        "dry_run": dry_run,
        "stages": [_stage_summary(o) for o in outcomes],
        "aborted_at": aborted_at,
        "blocked_at": blocked_at,
        "timed_out": timed_out,
        "scope": plan.as_dict(),
        "powered_on": [h for o in outcomes for h, v in o.get("power", {}).items()
                       if v.get("action") == "power_on"],
        "already_on": [h for o in outcomes for h, v in o.get("power", {}).items()
                       if v.get("action") == "none"],
        "power_failures": [h for o in outcomes for h, v in o.get("power", {}).items()
                           if not v.get("ok", True)],
    }
    result.notes.extend(orch.notes)

    if timed_out:
        final = "timed_out"
    elif aborted_at is not None:
        final = "aborted"
    else:
        final = "complete"
    orch.store.finish_phase(final, result.summary, result.data)
    orch.store.record_event(f"phase 2 complete: {result.summary}")
    result.duration = orch.clock() - started
    result.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return result


def _stage_summary(outcome: StageOutcome) -> Dict[str, Any]:
    """The serialisable part of a stage outcome (assessments live elsewhere)."""
    out = {k: v for k, v in outcome.items() if k != "assessments"}
    out["node_status"] = {a.node.hostname: a.status.value
                          for a in outcome.get("assessments", [])}
    return out
