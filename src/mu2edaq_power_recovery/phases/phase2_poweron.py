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

Nothing here issues a power command directly: every one goes through
:meth:`IPMIClient.ensure_on`, which enforces the protected-host list and the
dry-run gate, and every attempt is written to the run store before it is made.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from ..checks import Status
from ..orchestrator import NodeAssessment, Orchestrator
from ..topology import Node, expand_entries
from .base import PhaseResult, overall_status

log = logging.getLogger(__name__)

PHASE_NAME = "poweron"
PHASE_NUMBER = 2
PHASE_TITLE = "Power on"


class StageOutcome(dict):
    """One stage's result -- a dict so it serialises straight into the store."""


def _stage_nodes(orch: Orchestrator, stage: Dict[str, Any],
                 defaults: Dict[str, Any]) -> List[Node]:
    """Expand a stage's node list against the topology."""
    location = stage.get("location", defaults.get("location", "mc2"))
    names = expand_entries(stage.get("nodes", []), orch.topology.domain,
                           orch.topology.default_prefix)
    return orch.topology.resolve(names, [location])


def _requirement_met(requirement: str, assessments: Sequence[NodeAssessment]) -> bool:
    """Whether a stage satisfied its require: setting."""
    if not assessments:
        return False
    good = [a for a in assessments if not a.status.is_bad]
    if requirement == "any":
        return bool(good)
    if requirement == "majority":
        return len(good) * 2 > len(assessments)
    return len(good) == len(assessments)


def _power_stage(orch: Orchestrator, nodes: Sequence[Node],
                 stage: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Ensure every node in the stage is powered on.

    Returns hostname -> the dict from ``ensure_on``.  A node whose BMC is not
    in the topology is recorded as 'no_bmc' rather than skipped silently: on a
    post-outage checklist, "we could not power this one on" must be visible.
    """
    outcomes: Dict[str, Dict[str, Any]] = {}
    dry_run = bool(orch.settings.get("run.dry_run", True))

    if not stage.get("power_on", True):
        for node in nodes:
            outcomes[node.hostname] = {"action": "verify_only", "ok": True,
                                       "detail": "stage is verify-only"}
        return outcomes

    if orch.ipmi is None:
        for node in nodes:
            outcomes[node.hostname] = {
                "action": "unavailable", "ok": False,
                "detail": "no IPMI client: credentials or gateway unavailable"}
            orch.store.record_action(node.hostname, "power_on",
                                     node.ipmi_host or "(none)", "unavailable",
                                     dry_run, outcomes[node.hostname]["detail"])
        return outcomes

    for node in nodes:
        if not node.ipmi_host:
            outcomes[node.hostname] = {"action": "no_bmc", "ok": False,
                                       "detail": "no BMC listed in the topology"}
            orch.store.record_action(node.hostname, "power_on", "(none)",
                                     "no_bmc", dry_run, outcomes[node.hostname]["detail"])
            continue
        # Recorded before the command is issued, so a tool that dies mid-action
        # still leaves evidence of what it was attempting.
        orch.store.record_action(node.hostname, "power_on", node.ipmi_host,
                                 "attempting", dry_run, "")
        outcome = orch.ipmi.ensure_on(node.ipmi_host, node_host=node.hostname)
        outcomes[node.hostname] = outcome
        orch.store.record_action(node.hostname, "power_on", node.ipmi_host,
                                 outcome["action"], dry_run, outcome["detail"])
        log.info("%s: %s (%s)", node.short, outcome["action"], outcome["detail"])
    return outcomes


def _wait_for_nodes(orch: Orchestrator, nodes: Sequence[Node],
                    stage: Dict[str, Any], defaults: Dict[str, Any],
                    powered: Dict[str, Dict[str, Any]]) -> Dict[str, bool]:
    """Wait for each node that was just switched on to answer SSH."""
    budget = float(stage.get("boot_timeout", defaults.get("boot_timeout", 600)))
    delay = float(orch.settings.get("ipmi.power_on_delay", 20))
    answered: Dict[str, bool] = {}

    needs_wait = [n for n in nodes
                  if powered.get(n.hostname, {}).get("action") in
                  ("power_on", "dry_run")]
    if needs_wait and not orch.simulate and \
            any(powered[n.hostname]["action"] == "power_on" for n in needs_wait):
        log.info("waiting %.0fs for the BMCs to release power", delay)
        time.sleep(delay)

    for node in nodes:
        action = powered.get(node.hostname, {}).get("action")
        if action == "dry_run":
            answered[node.hostname] = True          # nothing was switched on
            continue
        transport = orch.ssh_factory.for_node(node)
        if action == "none":
            # Already on: one quick probe rather than the full boot budget.
            answered[node.hostname] = bool(getattr(transport, "alive", lambda: True)())
            continue
        waiter = getattr(transport, "wait_for_ssh", None)
        if waiter is None:                            # simulated transport
            answered[node.hostname] = True
            continue
        log.info("waiting up to %.0fs for %s to answer ssh", budget, node.short)
        answered[node.hostname] = waiter(budget)
    return answered


def run_stage(orch: Orchestrator, stage: Dict[str, Any], defaults: Dict[str, Any],
              progress: Optional[Any] = None) -> StageOutcome:
    """Power on, wait for, and verify one stage."""
    name = stage.get("name", "unnamed")
    title = stage.get("title", name)
    nodes = _stage_nodes(orch, stage, defaults)
    requirement = stage.get("require", defaults.get("require", "all"))
    concurrency = int(stage.get("concurrency", defaults.get("concurrency", 8)))
    settle = float(stage.get("settle", defaults.get("settle", 30)))
    started = time.monotonic()

    log.info("stage %s (%s): %d node(s)", name, title, len(nodes))
    orch.store.record_event(f"stage '{name}' started over {len(nodes)} node(s)")

    if not nodes:
        return StageOutcome(name=name, title=title, status=Status.SKIP.value,
                            summary="no nodes resolved for this stage",
                            nodes=[], assessments=[], power={}, answered={},
                            met=True, duration=0.0)

    powered = _power_stage(orch, nodes, stage)
    answered = _wait_for_nodes(orch, nodes, stage, defaults, powered)

    # Settle only if something actually booted -- there is no reason to wait 30
    # seconds for a stage whose nodes were all already running.
    if settle and not orch.simulate and \
            any(powered.get(n.hostname, {}).get("action") == "power_on" for n in nodes):
        log.info("settling for %.0fs before checking stage %s", settle, name)
        time.sleep(settle)

    # A node that has just been switched on should show a short uptime; telling
    # host.uptime so lets it flag a chassis that never actually rebooted.
    for node in nodes:
        if powered.get(node.hostname, {}).get("action") == "power_on":
            orch.baselines.setdefault(node.hostname, {})["expect_recent_boot"] = True

    assessments = orch.assess_nodes(nodes, profile=stage.get("checks"),
                                    concurrency=concurrency, progress=progress)
    for a in assessments:
        a.power_action = powered.get(a.node.hostname)

    met = _requirement_met(requirement, assessments)
    status = overall_status(assessments)
    good = [a for a in assessments if not a.status.is_bad]
    summary = (f"{len(good)}/{len(assessments)} node(s) verified"
               f" (require: {requirement})")
    no_ssh = [h for h, ok in answered.items() if not ok]
    if no_ssh:
        summary += f"; {len(no_ssh)} never answered ssh"

    orch.record(assessments)
    orch.store.record_event(
        f"stage '{name}' {'passed' if met else 'FAILED'}: {summary}",
        level="info" if met else "error")

    return StageOutcome(
        name=name, title=title, status=status.value, summary=summary,
        require=requirement, met=met,
        nodes=[n.hostname for n in nodes],
        power={h: v for h, v in powered.items()},
        answered=answered,
        assessments=assessments,
        duration=time.monotonic() - started,
    )


def run(orch: Orchestrator, progress: Optional[Any] = None,
        from_stage: Optional[str] = None,
        until_stage: Optional[str] = None) -> PhaseResult:
    """Walk the power-on sequence."""
    started = time.monotonic()
    result = PhaseResult(name=PHASE_NAME, number=PHASE_NUMBER, title=PHASE_TITLE,
                         started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    config = orch.sequence_config or {}
    defaults = config.get("defaults", {}) or {}
    stages = list(config.get("stages", []) or [])
    if not stages:
        result.status = Status.UNKNOWN
        result.summary = "no stages defined in the power sequence"
        return result

    from_stage = from_stage or orch.settings.get("run.from_stage")
    until_stage = until_stage or orch.settings.get("run.until_stage")
    stages = _slice_stages(stages, from_stage, until_stage)
    if not stages:
        result.status = Status.UNKNOWN
        result.summary = (f"stage selection --from {from_stage} --until "
                          f"{until_stage} matched nothing")
        return result

    orch.store.start_phase(PHASE_NAME, PHASE_NUMBER)
    dry_run = bool(orch.settings.get("run.dry_run", True))
    if dry_run:
        result.notes.append(
            "DRY RUN: power states were read but no chassis was switched on. "
            "Re-run with --execute to perform the sequence.")
    orch.store.record_event(f"phase 2 (power on) started, {len(stages)} stage(s)"
                            + (" [dry run]" if dry_run else " [LIVE]"))

    outcomes: List[StageOutcome] = []
    stop_on_failure = bool(orch.settings.get("run.stop_on_stage_failure", True))
    aborted_at: Optional[str] = None

    for stage in stages:
        outcome = run_stage(orch, stage, defaults, progress=progress)
        outcomes.append(outcome)
        result.assessments.extend(outcome.get("assessments", []))
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
    passed = sum(1 for o in outcomes if o["met"])
    result.summary = (f"{passed}/{len(outcomes)} stage(s) met their requirement"
                      + (f"; aborted at '{aborted_at}'" if aborted_at else ""))
    result.data = {
        "dry_run": dry_run,
        "stages": [_stage_summary(o) for o in outcomes],
        "aborted_at": aborted_at,
        "powered_on": [h for o in outcomes for h, v in o.get("power", {}).items()
                       if v.get("action") == "power_on"],
        "already_on": [h for o in outcomes for h, v in o.get("power", {}).items()
                       if v.get("action") == "none"],
        "power_failures": [h for o in outcomes for h, v in o.get("power", {}).items()
                           if not v.get("ok", True)],
    }
    result.notes.extend(orch.notes)

    orch.store.finish_phase(
        "complete" if aborted_at is None else "aborted",
        result.summary, result.data)
    orch.store.record_event(f"phase 2 complete: {result.summary}")
    result.duration = time.monotonic() - started
    result.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return result


def _slice_stages(stages: List[Dict[str, Any]], from_stage: Optional[str],
                  until_stage: Optional[str]) -> List[Dict[str, Any]]:
    """Apply --from / --until by stage name, inclusive at both ends."""
    names = [s.get("name") for s in stages]
    start = names.index(from_stage) if from_stage in names else 0
    end = names.index(until_stage) + 1 if until_stage in names else len(stages)
    return stages[start:end]


def _stage_summary(outcome: StageOutcome) -> Dict[str, Any]:
    """The serialisable part of a stage outcome (assessments live elsewhere)."""
    return {k: v for k, v in outcome.items() if k != "assessments"} | {
        "node_status": {a.node.hostname: a.status.value
                        for a in outcome.get("assessments", [])}}
