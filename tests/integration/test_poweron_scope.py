"""Phase 2 against the scripted transport, with the IPMI clients *armed*.

The dry-run gate is switched off on the simulated clients, so these tests see
exactly which ``chassis power on`` commands a live run would send: only to the
hosts the plan allows, never to a predecessor, each through a gateway of the
BMC's own location. Also the phase_timeout budget and the concurrent boot
wait (#13), on a fake clock.
"""
from __future__ import annotations

import re
import threading

import pytest

from mu2edaq_power_recovery.checks import Status
from mu2edaq_power_recovery.orchestrator import Orchestrator
from mu2edaq_power_recovery.phases import phase1_assess, phase2_poweron, phase3_network
from mu2edaq_power_recovery.phases.base import TIMEOUT_SUMMARY, Deadline
from mu2edaq_power_recovery.phases.phase2_poweron import plan_sequence
from mu2edaq_power_recovery.transport import CommandResult, ScriptedResponse


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.now += max(0.0, float(seconds))


@pytest.fixture
def clock():
    return FakeClock()


def build(settings, clock, locations=("mc2",), sequence=None):
    settings.set("topology.locations", list(locations))
    o = Orchestrator(settings, simulate=True, clock=clock, sleep=clock.sleep)
    if sequence is not None:
        o.sequence_config = sequence
    o.prepare_credentials()
    o.store.start_run("test", False, o.version.as_dict(), {})
    return o


@pytest.fixture
def orch(settings, clock):
    o = build(settings, clock)
    yield o
    o.close()


def arm(orch):
    """Take the dry-run gate off every IPMI client (the protected list stays)."""
    orch.settings.set("run.dry_run", False)
    for client in orch.ipmi_clients.values():
        client.dry_run = False


def off_for(*bmcs):
    """chassis power status: 'off' for these BMCs, 'on' for every other."""
    def respond(command):
        if any(f"-H {re.escape(b)} " in command or f"-H {b} " in command
               for b in bmcs):
            return ScriptedResponse(stdout="Chassis Power is off")
        return ScriptedResponse(stdout="Chassis Power is on")
    return respond


def power_on_targets(orch):
    """BMC host of every 'chassis power on' sent, with the gateway it ran on."""
    out = []
    for call in orch.ssh_factory.base.calls:
        if "chassis power on" in call["command"]:
            bmc = re.search(r"-H (\S+)", call["command"]).group(1)
            out.append((bmc, call["host"]))
    return out


def plan(orch, nodes=None, **kw):
    return plan_sequence(orch.sequence_config, orch.topology, orch.locations,
                         nodes, kw.get("from_stage"), kw.get("until_stage"))


# ---------------------------------------------------------------------------
# #1: power commands only where the plan allows
# ---------------------------------------------------------------------------


def test_a_scoped_live_run_powers_only_the_named_node(orch):
    arm(orch)
    # Everything off except the predecessors, which are on (else the run
    # stops before readout -- tested below).
    orch.ssh_factory.base.expect_first(
        r"chassis power status",
        off_for("mu2e-trk-01-ipmi.fnal.gov", "mu2e-trk-02-ipmi.fnal.gov"))
    result = phase2_poweron.run(orch, plan=plan(orch, ["mu2e-trk-01"]))

    sent = power_on_targets(orch)
    assert [bmc for bmc, _ in sent] == ["mu2e-trk-01-ipmi.fnal.gov"]
    actions = orch.store.get_actions()
    live = [a for a in actions if a["outcome"] == "power_on"]
    assert [a["hostname"] for a in live] == ["mu2e-trk-01.fnal.gov"]
    assert all(a["dry_run"] == 0 for a in actions)
    # Predecessors were read, never powered, and not recorded as actions.
    assert not any(a["hostname"].startswith("mu2e-mgr") for a in actions)
    stages = {s["name"]: s for s in result.data["stages"]}
    assert stages["manager"]["role"] == "predecessor"
    assert all(v["action"] == "verify" for v in stages["cfo"]["power"].values())
    assert stages["readout"]["nodes"] == ["mu2e-trk-01.fnal.gov"]


def test_an_unscoped_live_run_is_unchanged(orch):
    arm(orch)
    orch.ssh_factory.base.expect_first(
        r"chassis power status", off_for("mu2e-mgr-01-ipmi.fnal.gov"))
    phase2_poweron.run(orch, from_stage="manager", until_stage="manager")
    assert [b for b, _ in power_on_targets(orch)] == ["mu2e-mgr-01-ipmi.fnal.gov"]


def test_power_stage_refuses_a_host_outside_the_plan(orch):
    # Defence in depth: even handed a node the plan does not allow,
    # _power_stage records a refusal and sends nothing.
    arm(orch)
    orch.ssh_factory.base.expect_first(r"chassis power status",
                                       ScriptedResponse(stdout="Chassis Power is off"))
    nodes = orch.topology.resolve(["mu2e-trk-01", "mu2e-trk-02"], ["mc2"])
    outcomes = phase2_poweron._power_stage(
        orch, nodes, {"name": "readout"},
        allowed=frozenset({"mu2e-trk-01.fnal.gov"}))
    assert outcomes["mu2e-trk-02.fnal.gov"]["action"] == "out_of_scope"
    assert [b for b, _ in power_on_targets(orch)] == ["mu2e-trk-01-ipmi.fnal.gov"]
    refused = [a for a in orch.store.get_actions() if a["outcome"] == "out_of_scope"]
    assert [a["hostname"] for a in refused] == ["mu2e-trk-02.fnal.gov"]


def test_an_off_predecessor_stops_before_the_target(orch):
    arm(orch)
    orch.ssh_factory.base.expect_first(
        r"chassis power status",
        off_for("mu2e-mgr-01-ipmi.fnal.gov", "mu2e-trk-01-ipmi.fnal.gov"))
    result = phase2_poweron.run(orch, plan=plan(orch, ["mu2e-trk-01"]))

    assert power_on_targets(orch) == []           # nothing, anywhere
    assert result.data["blocked_at"] == "manager"
    assert result.status is Status.FAIL
    assert [s["name"] for s in result.data["stages"]] == ["gateways", "manager"]
    note = next(n for n in result.notes if n.startswith("stopped before 'readout'"))
    assert "mu2e-mgr-01 is off" in note
    assert "--from manager --until manager" in note


def test_an_off_predecessor_stops_even_with_continue_on_error(orch):
    orch.settings.set("run.stop_on_stage_failure", False)
    orch.ssh_factory.base.expect_first(
        r"chassis power status", off_for("mu2e-dl-02-ipmi.fnal.gov"))
    result = phase2_poweron.run(orch, plan=plan(orch, ["mu2e-trk-01"]))
    assert result.data["blocked_at"] == "dataloggers"


def test_a_predecessor_that_never_answers_ssh_stops_the_run(orch, monkeypatch):
    orch.ssh_factory.base.expect_first(
        r"chassis power status", ScriptedResponse(stdout="Chassis Power is on"))
    monkeypatch.setattr(phase2_poweron, "_wait_for_nodes",
                        lambda o, nodes, *a, **k: {n.hostname: n.short != "mu2e-cfo-01"
                                                   for n in nodes})
    result = phase2_poweron.run(orch, plan=plan(orch, ["mu2e-trk-01"]))
    assert result.data["blocked_at"] == "cfo"
    assert "did not answer ssh" in " ".join(result.notes)


def test_scope_notices_reach_notes_and_the_store(orch):
    result = phase2_poweron.run(orch, plan=plan(orch, ["mu2e-dl-01"]))
    assert any("VERIFY-ONLY" in n for n in result.notes)
    events = [e["message"] for e in orch.store.get_events()]
    assert any(e.startswith("scope: ") and "VERIFY-ONLY" in e for e in events)
    stages = {s["name"]: s for s in result.data["stages"]}
    assert stages["readout"]["status"] == "skip"
    assert stages["readout"]["summary"].startswith("outside requested scope")


def test_a_typo_raises_before_the_phase_starts(orch):
    with pytest.raises(phase2_poweron.SequenceSelectionError):
        phase2_poweron.run(orch, from_stage="manger")
    assert orch.store.get_phases() == []


# ---------------------------------------------------------------------------
# per-location IPMI clients
# ---------------------------------------------------------------------------


MIXED = {"defaults": {"location": "mc2", "boot_timeout": 60, "settle": 0},
         "stages": [
             {"name": "mc2", "checks": "readout", "nodes": ["mu2e-trk-01.fnal.gov"]},
             {"name": "ts", "location": "teststand", "checks": "readout",
              "nodes": ["mu2edaq04.fnal.gov"]}]}


def test_each_bmc_is_driven_through_its_own_locations_gateway(settings, clock):
    orch = build(settings, clock, locations=("mc2", "teststand"), sequence=MIXED)
    try:
        assert set(orch.ipmi_clients) == {"mc2", "teststand"}
        # One breaker for every client: one BMC account.
        assert {id(c.breaker) for c in orch.ipmi_clients.values()} == \
            {id(orch.ipmi_breaker)}
        assert all(c.protected == orch.topology.is_protected
                   for c in orch.ipmi_clients.values())
        arm(orch)
        orch.ssh_factory.base.expect_first(
            r"chassis power status",
            off_for("mu2e-trk-01-ipmi.fnal.gov", "mu2edaq04-ipmi.fnal.gov"))
        orch.settings.set("run.stop_on_stage_failure", False)
        phase2_poweron.run(orch)
        assert sorted(power_on_targets(orch)) == [
            ("mu2e-trk-01-ipmi.fnal.gov", "mu2egateway01.fnal.gov"),
            # The teststand's BMCs sit on the MC-2 IPMI segment, so its
            # ipmi_gateways are MC-2's gateways -- still a client of its own.
            ("mu2edaq04-ipmi.fnal.gov", "mu2egateway01.fnal.gov")]
        assert orch.ipmi_clients["teststand"] is not orch.ipmi_clients["mc2"]
    finally:
        orch.close()


def test_a_location_with_no_gateway_gets_no_client(settings, clock, monkeypatch):
    from mu2edaq_power_recovery.orchestrator import SimulatedSSHFactory
    monkeypatch.setattr(SimulatedSSHFactory, "gateway_for",
                        lambda self, loc, role="ssh": None if loc == "teststand"
                        else self.topology.gateways(loc)[0])
    orch = build(settings, clock, locations=("mc2", "teststand"), sequence=MIXED)
    try:
        ts = orch.topology.node("mu2edaq04")
        assert orch.ipmi_for(ts) is None          # never borrows MC-2's gateway
        out = phase2_poweron._power_stage(orch, [ts], {"name": "ts"})
        assert out[ts.hostname]["action"] == "unavailable"
    finally:
        orch.close()


# ---------------------------------------------------------------------------
# #13: concurrent boot waits and the phase budget, on a fake clock
# ---------------------------------------------------------------------------


class NeverAnswers:
    """LocalTransport stand-in under SSHTransport: every ssh times out, and
    each attempt costs its (capped) timeout on the fake clock."""

    def __init__(self, clock):
        self.clock = clock
        self.hosts = set()
        self._lock = threading.Lock()

    def run(self, argv, timeout=None, input_text=None, **kwargs):
        with self._lock:
            self.hosts.add(argv[-2])
        self.clock.sleep(timeout)
        return CommandResult(command=" ".join(argv), rc=255,
                             stderr="ssh: connect to host port 22: Connection timed out")


class DeadFactory:
    def __init__(self, local):
        self.local = local
        self.deadline = None

    def for_node(self, node, user=None, root=False):
        from mu2edaq_power_recovery.transport.ssh import SSHTransport
        return SSHTransport(node.hostname, local=self.local, connect_timeout=10)


def test_28_dead_nodes_cost_one_boot_timeout_not_28(orch, clock):
    nodes = plan(orch).stages[-1].nodes
    assert len(nodes) == 28
    local = NeverAnswers(clock)
    orch.ssh_factory = DeadFactory(local)
    powered = {n.hostname: {"action": "power_on"} for n in nodes}
    start = clock()
    answered = phase2_poweron._wait_for_nodes(
        orch, nodes, {"boot_timeout": 600}, {}, powered, Deadline(7200, clock))
    elapsed = clock() - start
    assert answered == {n.hostname: False for n in nodes}
    # Serially this would be 28 x 600 s = 16800 s. Concurrently, under one
    # stage deadline, the stage ends at one boot_timeout plus the attempts in
    # flight when it expired: at most ssh.max_sessions of them, each capped at
    # connect_timeout + 5. (On this shared fake clock those in-flight attempts
    # add up; in wall time they overlap and cost one attempt.)
    sessions = int(orch.settings.get("ssh.max_sessions", 16))
    assert elapsed <= 600 + sessions * 15
    assert elapsed < 2 * 600
    assert local.hosts                              # it really did try


def test_the_boot_wait_is_capped_by_the_phase_budget(orch, clock):
    nodes = plan(orch, from_stage="cfo", until_stage="cfo").stages[0].nodes
    orch.ssh_factory = DeadFactory(NeverAnswers(clock))
    start = clock()
    phase2_poweron._wait_for_nodes(
        orch, nodes, {"boot_timeout": 600}, {},
        {n.hostname: {"action": "power_on"} for n in nodes}, Deadline(90, clock))
    assert clock() - start <= 90 + 1


def _jump_after_first(orch, clock, monkeypatch, seconds=10_000):
    """Make the first assess_nodes call cost *seconds* on the fake clock."""
    original = orch.assess_nodes
    calls = []

    def slow(*args, **kwargs):
        out = original(*args, **kwargs)
        if not calls:
            clock.sleep(seconds)
        calls.append(1)
        return out
    monkeypatch.setattr(orch, "assess_nodes", slow)


def test_phase_timeout_marks_the_stages_it_never_reached(orch, clock, monkeypatch):
    orch.settings.set("run.phase_timeout", 100)
    _jump_after_first(orch, clock, monkeypatch)
    result = phase2_poweron.run(orch)
    stages = {s["name"]: s for s in result.data["stages"]}
    assert stages["gateways"]["met"] is True
    for name in ("manager", "dataloggers", "dcs", "cfo", "readout"):
        assert stages[name]["status"] == "unknown"
        assert stages[name]["summary"] == TIMEOUT_SUMMARY
    assert result.data["timed_out"] is True
    assert result.status is Status.UNKNOWN            # not FAIL: nothing looked at
    readout = [a for a in result.assessments if a.node.node_class == "tracker"]
    assert readout and all(a.summary() == TIMEOUT_SUMMARY for a in readout)
    assert any("Resume with --from manager" in n for n in result.notes)
    assert orch.store.get_phases()[-1]["status"] == "timed_out"


def test_phase1_timeout_leaves_later_nodes_unknown(orch, clock, monkeypatch):
    orch.settings.set("run.phase_timeout", 100)
    _jump_after_first(orch, clock, monkeypatch)
    nodes = orch.topology.resolve(["mu2egateway01", "mu2e-trk-01", "mu2e-trk-02"],
                                  ["mc2"])
    result = phase1_assess.run(orch, nodes)
    gateway, *rest = sorted(result.assessments, key=lambda a: a.node.node_class != "gateway")
    assert gateway.status is Status.OK
    assert rest and all(a.timed_out and a.status is Status.UNKNOWN for a in rest)
    assert all(r.summary == TIMEOUT_SUMMARY for a in rest for r in a.results)
    assert sorted(result.data["timed_out"]) == ["mu2e-trk-01.fnal.gov",
                                                "mu2e-trk-02.fnal.gov"]
    # Phase 1 is read-only regardless.
    assert not orch.store.get_actions()


def test_phase3_timeout_leaves_unprobed_paths_unknown(orch, clock):
    from mu2edaq_power_recovery.transport.fake import _mesh_script_response
    orch.settings.set("run.phase_timeout", 100)

    def slow(command):
        clock.sleep(10_000)
        return _mesh_script_response(command)
    orch.ssh_factory.base.expect_first(r"===BEGIN ", slow)
    result = phase3_network.run(orch, orch.topology.resolve(
        ["mu2e-dl-01", "mu2e-dl-02", "mu2e-cfo-01"], ["mc2"]))
    assert result.data["timed_out"] is True
    unprobed = [e for net in result.data["networks"] for e in net["edges"]
                if e["detail"] == TIMEOUT_SUMMARY]
    assert unprobed and all(e["status"] == "unknown" for e in unprobed)


def test_an_ipmi_error_on_one_node_does_not_abandon_the_stage(orch, monkeypatch):
    """PR #30 review: an IPMIError from ensure_on crashed the run, so the rest
    of the stage got no command and the phase was never finished."""
    from mu2edaq_power_recovery.transport.ipmi import IPMIError
    nodes = plan(orch, from_stage="cfo", until_stage="readout").stages[-1].nodes[:3]
    calls = []

    class Flaky:
        def ensure_on(self, bmc, node_host=None):
            calls.append(node_host)
            if node_host == nodes[0].hostname:
                raise IPMIError("cannot reach gateway: run.phase_timeout expired")
            return {"action": "none", "ok": True, "before": "on", "after": "on",
                    "detail": "already powered on"}

    monkeypatch.setattr(orch, "ipmi_for", lambda node: Flaky())
    out = phase2_poweron._power_stage(orch, nodes, {"name": "readout"})
    assert calls == [n.hostname for n in nodes]
    assert out[nodes[0].hostname]["action"] == "failed"
    assert "IPMI error" in out[nodes[0].hostname]["detail"]
    assert all(out[n.hostname]["ok"] for n in nodes[1:])
