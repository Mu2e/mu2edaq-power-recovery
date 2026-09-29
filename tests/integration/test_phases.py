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
    # The simulated log is empty: an empty baseline, which is not "none".
    assert orch.baselines["mu2e-trk-01.fnal.gov"]["sel"] == {}


def _sel_rows(first, last, event="Power Supply AC lost"):
    return "\n".join(f"{i:4x} | 09/18/2026 | 14:{i % 60:02d}:00 | "
                     f"Power Supply #0x51 | {event} | Asserted"
                     for i in range(first, last + 1))


def test_a_rotated_full_sel_still_shows_the_new_critical_event(orch):
    """Both readings are twenty rows long; only the record ids tell them apart."""
    node = _nodes(orch, "mu2e-trk-01")[0]
    base = orch.ssh_factory.base
    base.expect_first(r"sel list", ScriptedResponse(stdout=_sel_rows(0x41, 0x54),
                                                    once=True))
    phase1_assess.run(orch, [node])
    assert len(orch.baselines[node.hostname]["sel"]) == 20

    rotated = _sel_rows(0x42, 0x54) + ("\n  55 | 09/18/2026 | 15:10:02 | "
                                       "Processor #0x04 | IERR | Asserted"
                                       " | Critical")
    base.expect_first(r"sel list", ScriptedResponse(stdout=rotated, once=True))
    res = orch.assess_node(node, only=["power.sel"]).results[0]
    assert res.status is Status.FAIL
    assert "1 new critical event" in res.summary
    # The survey stays the baseline; the later reading does not replace it.
    assert "55" not in orch.baselines[node.hostname]["sel"]


def test_a_failed_sel_read_records_no_baseline(orch):
    node = _nodes(orch, "mu2e-trk-01")[0]
    orch.ssh_factory.base.expect_first(r"sel list", ScriptedResponse(
        stderr="Error: timed out", rc=1))
    phase1_assess.run(orch, [node])
    assert "sel" not in orch.baselines.get(node.hostname, {})


def test_sensors_and_sel_are_not_asked_after_a_dark_bmc(orch):
    node = _nodes(orch, "mu2e-trk-01")[0]
    orch.ssh_factory.base.expect_first(
        r"^ping -c 1 -W 1 -q " + node.ipmi_host.replace(".", r"\."),
        ScriptedResponse(rc=1, stdout="1 packets transmitted, 0 received"))
    result = phase1_assess.run(orch, [node])
    by_id = {r.check_id: r for r in result.assessments[0].results}
    assert by_id["power.status"].status is Status.FAIL      # it is dark
    for check_id in ("power.sensors", "power.sel"):
        assert by_id[check_id].status is Status.UNKNOWN     # we did not look
        assert "power.status" in by_id[check_id].summary
    asked = [c["command"] for c in orch.ssh_factory.base.calls
             if node.ipmi_host in c["command"]]
    # One pre-check ping; no ipmitool at all.
    assert len(asked) == 1 and asked[0].startswith("ping ")


def test_sensors_and_sel_are_not_asked_after_a_refused_credential(orch):
    node = _nodes(orch, "mu2e-trk-01")[0]
    orch.ssh_factory.base.expect_first(r"ipmitool", ScriptedResponse(
        stderr="RAKP 2 HMAC is invalid", rc=1))
    result = phase1_assess.run(orch, [node])
    by_id = {r.check_id: r for r in result.assessments[0].results}
    assert by_id["power.status"].status is Status.UNKNOWN
    assert by_id["power.sensors"].status is Status.UNKNOWN
    assert by_id["power.sel"].status is Status.UNKNOWN
    assert "credential was refused" in by_id["power.sel"].summary


def test_a_refused_credential_is_unknown_and_blocks_phase2(orch):
    orch.ssh_factory.base.expect_first(r"ipmitool", ScriptedResponse(
        stderr="RAKP 2 message indicates an error : unauthorized name", rc=1))
    result = phase1_assess.run(orch, _nodes(orch, "mu2e-trk-01", "mu2e-trk-02"))
    power = [r for a in result.assessments for r in a.results
             if r.check_id.startswith("power.")]
    assert power and all(r.status is Status.UNKNOWN for r in power)
    assert not any("does not answer" in r.summary for r in power)
    readiness = result.data["ready_for_phase2"]
    assert readiness["ready"] is False
    assert "rejected the IPMI credentials" in readiness["credentials_refused"]
    assert any("IPMI credentials refused" in n for n in result.notes)
    # One BMC was asked, once; everything else was stopped by the breaker.
    asked = [c for c in orch.ssh_factory.base.calls if "ipmitool" in c["command"]]
    assert len(asked) == 1


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


def _no_replies(command):
    """A completed probe in which every ping got nothing back."""
    import re
    parts = []
    for target in re.findall(r"===BEGIN (\S+)===", command):
        parts += [f"===BEGIN {target}===",
                  "3 packets transmitted, 0 received, 100% packet loss, time 2040ms",
                  f"===END {target}==="]
    return ScriptedResponse(stdout="\n".join(parts))


def test_an_isolated_node_is_identified(orch):
    # Marker-wrapped "0 received": the probe ran and the pings got no reply.
    # Empty output would be an untested path (UNKNOWN), not an isolated node.
    orch.ssh_factory.base.expect_first(r"===BEGIN ", _no_replies)
    result = phase3_network.run(orch, _nodes(orch, "mu2e-dl-01", "mu2e-dl-02"))
    assert result.status is Status.FAIL
    assert result.data["isolated_nodes"]
    # Notes are aggregated, not one per host.
    assert len(result.notes) < 10


def test_a_probe_that_returns_nothing_is_unknown_not_failed(orch):
    orch.ssh_factory.base.expect_first(r"===BEGIN ", ScriptedResponse(stdout=""))
    result = phase3_network.run(orch, _nodes(orch, "mu2e-dl-01", "mu2e-dl-02"))
    assert result.status is Status.UNKNOWN
    assert result.data["isolated_nodes"] == []
    for net in result.data["networks"]:
        assert net["failures"] == []
        assert net["counts"]["tested"] == 0
        assert net["counts"]["unknown"] == net["edge_count"] > 0


def test_ipmi_is_probed_from_the_gateways(orch):
    result = phase3_network.run(orch, _nodes(orch, "mu2e-dl-01", "mu2e-dl-02"))
    ipmi = next(n for n in result.data["networks"] if n["network"] == "ipmi")
    assert ipmi["origin"] == "gateways"
    assert {e["source"] for e in ipmi["edges"]} == set(
        orch.topology.gateways("mc2"))
    assert {e["target"] for e in ipmi["edges"]} == {
        "mu2e-dl-01-ipmi.fnal.gov", "mu2e-dl-02-ipmi.fnal.gov"}
    assert "[from gateways]" in result.summary


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


# ---------------------------------------------------------------------------
# an interrupt mid-phase does not wait for the queued nodes (S1)
# ---------------------------------------------------------------------------

import signal as _signal  # noqa: E402
import threading as _threading  # noqa: E402
import time  # noqa: E402

from mu2edaq_power_recovery.orchestrator import NodeAssessment  # noqa: E402


def _sigterm_main_when(event):
    """Deliver a real SIGTERM to the main thread once *event* is set."""
    def fire():
        if event.wait(10):
            _signal.pthread_kill(_threading.main_thread().ident, _signal.SIGTERM)
    t = _threading.Thread(target=fire, daemon=True)
    t.start()
    return t


@pytest.fixture
def sigterm_handler():
    from mu2edaq_power_recovery.cli import install_sigterm_handler
    previous = _signal.getsignal(_signal.SIGTERM)
    install_sigterm_handler()
    yield
    _signal.signal(_signal.SIGTERM, previous)


@pytest.mark.skipif(not hasattr(_signal, "pthread_kill"), reason="POSIX only")
def test_sigterm_mid_phase_cancels_the_queued_nodes(orch, monkeypatch,
                                                    sigterm_handler):
    nodes = orch.nodes()[:6]
    started, running, release = [], _threading.Event(), _threading.Event()

    def assess_node(node, *args, **kwargs):
        started.append(node.hostname)
        running.set()
        release.wait(10)             # a slow node, still running at SIGTERM
        return NodeAssessment(node=node)

    monkeypatch.setattr(orch, "assess_node", assess_node)
    _sigterm_main_when(running)
    try:
        with pytest.raises(KeyboardInterrupt):
            orch.assess_nodes(nodes, concurrency=1)
    finally:
        release.set()
    assert started == [nodes[0].hostname], "a queued node was started"


def test_an_interrupt_from_a_worker_is_not_swallowed_or_waited_out(orch,
                                                                   monkeypatch):
    nodes = orch.nodes()[:8]
    started = []

    def assess_node(node, *args, **kwargs):
        started.append(node.hostname)
        if node is nodes[0]:
            raise KeyboardInterrupt
        time.sleep(0.2)          # busy enough for the main thread to cancel
        return NodeAssessment(node=node)

    monkeypatch.setattr(orch, "assess_node", assess_node)
    with pytest.raises(KeyboardInterrupt):
        orch.assess_nodes(nodes, concurrency=1)
    # The one worker may already have taken the next node off the queue
    # before the main thread saw the interrupt; nothing after that runs.
    assert len(started) <= 2
