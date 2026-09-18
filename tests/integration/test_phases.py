"""The four phases, end to end, against the simulated transport.

These are the tests that would catch a phase wiring mistake -- a stage order
that stops depending on the config, a power-on that runs in a dry run, a report
that disagrees with the store.  They contact nothing.
"""
from __future__ import annotations

import pytest

from mu2edaq_power_recovery.checks import Status
from mu2edaq_power_recovery.orchestrator import Orchestrator
from mu2edaq_power_recovery.phases import (phase1_assess, phase2_poweron,
                                           phase3_network, phase4_report)
from mu2edaq_power_recovery.transport import ScriptedResponse


@pytest.fixture
def orch(settings):
    settings.set("topology.locations", ["mc2"])
    o = Orchestrator(settings, simulate=True)
    o.prepare_credentials()
    o.store.start_run("test", True, o.version.as_dict(), {})
    yield o
    o.close()


def _nodes(orch, *names):
    return orch.topology.resolve(list(names), ["mc2"])


# ---------------------------------------------------------------------------
# phase 1
# ---------------------------------------------------------------------------


def test_assess_reports_every_node_healthy_on_a_healthy_cluster(orch):
    result = phase1_assess.run(orch, _nodes(orch, "mu2egateway01", "mu2e-trk-01"))
    assert result.status is Status.OK
    assert result.counts["total"] == 2
    assert result.counts["ok"] == 2


def test_a_simulated_run_probes_gateways_through_the_script(orch):
    """A simulation must contact nothing -- gateways included.

    CheckContext.prober has nothing closer to a gateway than the machine
    driving the run, so while the orchestrator left the real LocalTransport in
    place a *simulated* ping.lab shelled out and pinged mu2egateway01 for
    real.  These tests then passed or failed according to whether the
    workstation could reach Fermilab at that instant, which is exactly the
    intermittent failure this guards against.
    """
    from mu2edaq_power_recovery.transport import LocalTransport

    assert not isinstance(orch.local, LocalTransport)
    result = phase1_assess.run(orch, _nodes(orch, "mu2egateway01"))
    ping = next(r for r in result.assessments[0].results
                if r.check_id == "ping.lab")
    assert ping.status is Status.OK
    assert ping.data["probed_from"] == "localhost"
    # ... and the probe landed in the scripted call log, not on a socket.
    assert orch.ssh_factory.base.ran(r"\bping\b", host="localhost")


def test_assess_takes_no_corrective_action(orch):
    # The phase is specified as read-only; nothing it runs may change state.
    phase1_assess.run(orch, _nodes(orch, "mu2e-trk-01"))
    issued = orch.ssh_factory.base.commands()
    assert not any("chassis power on" in c for c in issued)
    assert not any("chassis power off" in c for c in issued)
    assert not orch.store.get_actions()


def test_assess_stops_when_no_gateway_can_be_logged_in_to(orch):
    # Everything downstream is probed from a gateway, so this is the one
    # failure that genuinely stops the phase.
    orch.ssh_factory.base.expect_first(r"\bid -un\b",
                                       ScriptedResponse(stderr="denied", rc=255))
    result = phase1_assess.run(orch, _nodes(orch, "mu2egateway01", "mu2e-trk-01"))
    assert result.status is Status.FAIL
    assert "no gateway" in result.summary
    # trk-01 was never assessed, because there was no path to it.
    assert [a.node.short for a in result.assessments] == ["mu2egateway01"]


def test_assess_reports_phase2_readiness(orch):
    result = phase1_assess.run(orch, _nodes(orch, "mu2egateway01", "mu2e-trk-01"))
    readiness = result.data["ready_for_phase2"]
    assert readiness["gateway_usable"] is True
    assert readiness["bmc_answered"] >= 1
    assert readiness["ready"] is True


def test_assess_records_a_sel_baseline_for_later_phases(orch):
    phase1_assess.run(orch, _nodes(orch, "mu2e-trk-01"))
    assert "sel_count" in orch.baselines["mu2e-trk-01.fnal.gov"]


def test_an_unreachable_node_is_unknown_not_failed(orch):
    orch.ssh_factory.base.expect_first(r"\bping\b", ScriptedResponse(
        stdout="3 packets transmitted, 0 received, 100% packet loss"))
    orch.ssh_factory.base.expect_first(r"\bid -un\b",
                                       ScriptedResponse(stderr="timeout", rc=255))
    result = phase1_assess.run(orch, _nodes(orch, "mu2e-trk-01"))
    assessment = result.assessments[0]
    assert assessment.status is Status.UNKNOWN
    # The remaining checks are recorded as not-run, never as passes.
    not_run = [r for r in assessment.results
               if r.status is Status.UNKNOWN and "not run" in r.summary]
    assert not_run


# ---------------------------------------------------------------------------
# phase 2
# ---------------------------------------------------------------------------


def test_poweron_walks_the_configured_stage_order(orch):
    result = phase2_poweron.run(orch)
    names = [stage["name"] for stage in result.data["stages"]]
    assert names == ["gateways", "manager", "dataloggers", "dcs", "cfo", "readout"]


def test_poweron_is_a_dry_run_by_default(orch):
    assert orch.settings.get("run.dry_run") is True
    result = phase2_poweron.run(orch)
    assert result.data["dry_run"] is True
    assert not orch.ssh_factory.base.ran("chassis power on")
    assert any("DRY RUN" in note for note in result.notes)


def test_the_gateway_stage_never_issues_a_power_command(orch):
    # The gateways are the jump hosts; power-cycling them removes the path
    # every other stage depends on.
    result = phase2_poweron.run(orch, until_stage="gateways")
    stage = result.data["stages"][0]
    assert all(entry["action"] == "verify_only" for entry in stage["power"].values())


def test_from_and_until_slice_the_sequence(orch):
    result = phase2_poweron.run(orch, from_stage="dcs", until_stage="cfo")
    assert [s["name"] for s in result.data["stages"]] == ["dcs", "cfo"]


def test_a_failed_stage_stops_the_sequence(orch):
    # The manager exports /home; every later stage fails without it, so
    # continuing would produce noise rather than information.
    orch.settings.set("run.stop_on_stage_failure", True)
    orch.ssh_factory.base.expect_first(
        r"exportfs|showmount|/proc/fs/nfsd/exports", ScriptedResponse(rc=1))
    result = phase2_poweron.run(orch)
    assert result.data["aborted_at"] == "manager"
    assert [s["name"] for s in result.data["stages"]] == ["gateways", "manager"]


def test_continue_on_error_runs_the_whole_sequence(orch):
    orch.settings.set("run.stop_on_stage_failure", False)
    orch.ssh_factory.base.expect_first(
        r"exportfs|showmount|/proc/fs/nfsd/exports", ScriptedResponse(rc=1))
    result = phase2_poweron.run(orch)
    assert result.data["aborted_at"] is None
    assert len(result.data["stages"]) == 6


def test_requirement_modes(orch):
    from mu2edaq_power_recovery.phases.phase2_poweron import _requirement_met

    class FakeAssessment:
        def __init__(self, status):
            self.status = status

    good, bad = FakeAssessment(Status.OK), FakeAssessment(Status.FAIL)
    assert _requirement_met("all", [good, good])
    assert not _requirement_met("all", [good, bad])
    assert _requirement_met("majority", [good, good, bad])
    assert not _requirement_met("majority", [good, bad, bad])
    assert _requirement_met("any", [good, bad])
    assert not _requirement_met("any", [bad, bad])
    assert not _requirement_met("all", [])


def test_power_actions_are_recorded_even_in_a_dry_run(orch):
    phase2_poweron.run(orch, from_stage="manager", until_stage="manager")
    actions = orch.store.get_actions()
    assert any(a["hostname"] == "mu2e-mgr-01.fnal.gov" for a in actions)
    assert all(a["dry_run"] == 1 for a in actions)


# ---------------------------------------------------------------------------
# phase 3
# ---------------------------------------------------------------------------


def test_network_probes_every_configured_segment(orch):
    result = phase3_network.run(orch, _nodes(orch, "mu2e-dl-01", "mu2e-dl-02",
                                             "mu2e-cfo-01"))
    probed = {net["network"] for net in result.data["networks"]}
    assert probed == {"data", "lab", "ipmi"}


def test_a_healthy_fabric_passes(orch):
    result = phase3_network.run(orch, _nodes(orch, "mu2e-dl-01", "mu2e-dl-02"))
    assert result.status is Status.OK
    data = next(n for n in result.data["networks"] if n["network"] == "data")
    assert data["failures"] == []


def test_an_isolated_node_is_identified(orch):
    orch.ssh_factory.base.expect_first(r"===BEGIN ", ScriptedResponse(stdout=""))
    result = phase3_network.run(orch, _nodes(orch, "mu2e-dl-01", "mu2e-dl-02"))
    assert result.status is Status.FAIL
    assert result.data["isolated_nodes"]
    # Notes are aggregated, not one per host.
    assert len(result.notes) < 10


def test_mesh_edges_come_back_in_a_stable_order(orch):
    result = phase3_network.run(orch, _nodes(orch, "mu2e-dl-01", "mu2e-dl-02"))
    data = next(n for n in result.data["networks"] if n["network"] == "data")
    edges = [(e["source"], e["target"]) for e in data["edges"]]
    assert edges == sorted(edges)


# ---------------------------------------------------------------------------
# phase 4
# ---------------------------------------------------------------------------


def test_report_summarises_the_stored_run(orch):
    phase1_assess.run(orch, _nodes(orch, "mu2egateway01", "mu2e-trk-01"))
    result = phase4_report.run(orch, post=False)
    narrative = result.data["narrative"]
    assert narrative["counts"]["total"] == 2
    assert "verified healthy" in narrative["headline"]
    assert narrative["dry_run"] is True


def test_report_lists_outstanding_problems_and_next_steps(orch):
    orch.ssh_factory.base.expect_first(r"mountpoint -q /home",
                                       ScriptedResponse(rc=1))
    phase1_assess.run(orch, _nodes(orch, "mu2e-trk-01", "mu2e-trk-02",
                                   "mu2e-trk-03"))
    narrative = phase4_report.run(orch, post=False).data["narrative"]
    assert narrative["counts"]["fail"] == 3
    outstanding = {item["check"] for item in narrative["outstanding"]}
    assert "disk.mounts" in outstanding
    # A fault on three nodes should be called out as one shared cause.
    assert any("shared cause" in step for step in narrative["next_steps"])


def test_report_probes_nothing(orch):
    phase1_assess.run(orch, _nodes(orch, "mu2e-trk-01"))
    before = len(orch.ssh_factory.base.calls)
    phase4_report.run(orch, post=False)
    assert len(orch.ssh_factory.base.calls) == before


def test_report_does_not_post_unless_asked(orch):
    phase1_assess.run(orch, _nodes(orch, "mu2e-trk-01"))
    result = phase4_report.run(orch, post=False)
    assert any("not enabled" in note for note in result.notes)
    assert "ecl" not in result.data
