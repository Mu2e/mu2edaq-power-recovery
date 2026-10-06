"""Phase-2 planning (#1, #2) and the time budget primitives (#13).

plan_sequence() decides, before anything is contacted, which stages run over
which nodes and which hosts may be sent a power command. These tests pin the
scope rules against the shipped power-sequence.yaml and topology.yaml.
"""
from __future__ import annotations

import threading

import pytest
import yaml

from mu2edaq_power_recovery.phases.base import Deadline
from mu2edaq_power_recovery.phases.phase2_poweron import (
    ROLE_FULL, ROLE_OUT_OF_SCOPE, ROLE_PREDECESSOR, ROLE_TARGET,
    SequenceSelectionError, _slice_stages, plan_sequence)
from mu2edaq_power_recovery.topology import TopologyError

ORDER = ["gateways", "manager", "dataloggers", "dcs", "cfo", "readout"]


@pytest.fixture(scope="module")
def sequence(config_dir):
    with open(config_dir / "power-sequence.yaml") as fh:
        return yaml.safe_load(fh)


def roles(plan):
    return {s.name: s.role for s in plan.stages}


class FakeClock:
    """A thread-safe monotonic clock whose sleep advances it instead of waiting."""

    def __init__(self, start: float = 1000.0):
        self.now = start
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.now += max(0.0, float(seconds))


# ---------------------------------------------------------------------------
# --from / --until (#2)
# ---------------------------------------------------------------------------


STAGES = [{"name": n} for n in ORDER]


@pytest.mark.parametrize("from_stage, until_stage, expected", [
    (None, None, ORDER),
    ("dcs", "cfo", ["dcs", "cfo"]),
    ("manager", "manager", ["manager"]),
    ("readout", None, ["readout"]),
    (None, "gateways", ["gateways"]),
])
def test_valid_slices_are_inclusive(from_stage, until_stage, expected):
    assert [s["name"] for s in _slice_stages(STAGES, from_stage, until_stage)] \
        == expected


@pytest.mark.parametrize("from_stage, until_stage, needle", [
    ("manger", None, "--from 'manger' is not a stage"),
    (None, "readuot", "--until 'readuot' is not a stage"),
    ("typo", "cfo", "--from 'typo'"),
    ("cfo", "manager", "comes after --until"),
    ("readout", "gateways", "comes after --until"),
    ("", None, "--from ''"),
])
def test_bad_slices_are_errors_listing_the_valid_names(from_stage, until_stage,
                                                       needle):
    with pytest.raises(SequenceSelectionError) as info:
        _slice_stages(STAGES, from_stage, until_stage)
    assert needle in str(info.value)
    assert "gateways, manager, dataloggers, dcs, cfo, readout" in str(info.value)


def test_duplicate_stage_names_are_an_error():
    with pytest.raises(SequenceSelectionError, match="more than once: dcs"):
        _slice_stages([{"name": "dcs"}, {"name": "cfo"}, {"name": "dcs"}],
                      None, None)


def test_an_unnamed_stage_is_an_error():
    with pytest.raises(SequenceSelectionError, match="no name"):
        _slice_stages([{"name": "dcs"}, {"title": "x"}], None, None)


def test_a_selection_error_is_a_value_error():
    assert issubclass(SequenceSelectionError, ValueError)


# ---------------------------------------------------------------------------
# scope (#1)
# ---------------------------------------------------------------------------


def test_unscoped_plan_is_the_sequence_unchanged(sequence, topology):
    plan = plan_sequence(sequence, topology, ["mc2", "teststand"])
    assert [s.name for s in plan.stages] == ORDER
    assert set(roles(plan).values()) == {ROLE_FULL}
    assert not plan.notices
    # Everything that may be powered: every node of every power_on stage,
    # never the verify-only gateway stage.
    assert "mu2e-trk-14.fnal.gov" in plan.allowed_power
    assert "mu2egateway01.fnal.gov" not in plan.allowed_power
    assert len(plan.stages[-1].nodes) == 28


def test_one_node_targets_its_stage_and_verifies_the_rest(sequence, topology):
    plan = plan_sequence(sequence, topology, ["mc2"], ["mu2e-trk-01"])
    assert roles(plan) == {"gateways": ROLE_PREDECESSOR,
                           "manager": ROLE_PREDECESSOR,
                           "dataloggers": ROLE_PREDECESSOR,
                           "dcs": ROLE_PREDECESSOR,
                           "cfo": ROLE_PREDECESSOR,
                           "readout": ROLE_TARGET}
    readout = plan.stages[-1]
    assert [n.hostname for n in readout.nodes] == ["mu2e-trk-01.fnal.gov"]
    # Only the named node may be powered; predecessors never.
    assert plan.allowed_power == frozenset({"mu2e-trk-01.fnal.gov"})
    assert any("VERIFY-ONLY" in n for n in plan.notices)
    assert any("1 of its 28" in n for n in plan.notices)


def test_fully_qualified_and_short_names_agree(sequence, topology):
    a = plan_sequence(sequence, topology, ["mc2"], ["mu2e-trk-01"])
    b = plan_sequence(sequence, topology, ["mc2"], ["mu2e-trk-01.fnal.gov"])
    assert a.allowed_power == b.allowed_power


def test_a_middle_node_leaves_later_stages_out(sequence, topology):
    plan = plan_sequence(sequence, topology, ["mc2"], ["mu2e-dl-01"])
    r = roles(plan)
    assert r["gateways"] == r["manager"] == ROLE_PREDECESSOR
    assert r["dataloggers"] == ROLE_TARGET
    assert r["dcs"] == r["cfo"] == r["readout"] == ROLE_OUT_OF_SCOPE
    assert all(not s.nodes for s in plan.stages if s.role == ROLE_OUT_OF_SCOPE)
    assert any("will not be run" in n for n in plan.notices)
    assert plan.allowed_power == frozenset({"mu2e-dl-01.fnal.gov"})


def test_nodes_in_two_stages_are_both_targets(sequence, topology):
    plan = plan_sequence(sequence, topology, ["mc2"],
                         ["mu2e-mgr-01", "mu2e-trk-01"])
    r = roles(plan)
    assert r["manager"] == r["readout"] == ROLE_TARGET
    assert r["dataloggers"] == ROLE_PREDECESSOR
    assert plan.allowed_power == frozenset({"mu2e-mgr-01.fnal.gov",
                                            "mu2e-trk-01.fnal.gov"})


def test_from_bounds_the_predecessors(sequence, topology):
    plan = plan_sequence(sequence, topology, ["mc2"], ["mu2e-trk-01"],
                         from_stage="readout")
    assert [s.name for s in plan.stages] == ["readout"]
    assert [n.short for n in plan.stages[0].nodes] == ["mu2e-trk-01"]
    assert plan.allowed_power == frozenset({"mu2e-trk-01.fnal.gov"})


def test_a_gateway_named_alone_is_verify_only(sequence, topology):
    # The gateway stage is power_on: false, so even as a target nothing may be
    # powered.
    plan = plan_sequence(sequence, topology, ["mc2"], ["mu2egateway01"])
    assert roles(plan)["gateways"] == ROLE_TARGET
    assert plan.allowed_power == frozenset()


def test_a_node_in_no_stage_is_an_error(sequence, topology):
    # mu2e-trk-15 is in the topology inventory but not in the readout stage.
    assert topology.node("mu2e-trk-15") is not None
    with pytest.raises(SequenceSelectionError,
                       match="mu2e-trk-15 is not in any stage"):
        plan_sequence(sequence, topology, ["mc2"], ["mu2e-trk-15"])


def test_a_node_outside_until_is_an_error_naming_its_stage(sequence, topology):
    with pytest.raises(SequenceSelectionError) as info:
        plan_sequence(sequence, topology, ["mc2"], ["mu2e-trk-01"],
                      until_stage="cfo")
    assert "stage 'readout', outside --from gateways --until cfo" in str(info.value)


def test_a_node_outside_location_is_an_error(sequence, topology):
    # A custom sequence with a teststand stage; the node is asked for with
    # --location mc2 only.
    config = {"defaults": {"location": "mc2"}, "stages": [
        {"name": "mgr", "nodes": ["mu2e-mgr-01.fnal.gov"]},
        {"name": "ts", "location": "teststand", "nodes": ["mu2edaq04.fnal.gov"]}]}
    with pytest.raises(SequenceSelectionError, match="outside --location mc2"):
        plan_sequence(config, topology, ["mc2"], ["mu2edaq04"])


def test_teststand_only_drops_the_mc2_sequence(sequence, topology):
    # Every shipped stage is in MC-2, so a teststand-only power-on has nothing
    # to run and says so rather than running MC-2.
    with pytest.raises(SequenceSelectionError,
                       match="requested location\\(s\\) teststand"):
        plan_sequence(sequence, topology, ["teststand"])


def test_location_filter_drops_other_sites_with_a_notice(topology):
    config = {"defaults": {"location": "mc2"}, "stages": [
        {"name": "mgr", "nodes": ["mu2e-mgr-01.fnal.gov"]},
        {"name": "ts", "location": "teststand", "nodes": ["mu2edaq04.fnal.gov"]}]}
    plan = plan_sequence(config, topology, ["teststand"])
    assert roles(plan) == {"mgr": ROLE_OUT_OF_SCOPE, "ts": ROLE_FULL}
    assert plan.allowed_power == frozenset({"mu2edaq04.fnal.gov"})
    assert any("another location" in n for n in plan.notices)


def test_location_aliases_are_canonicalised(sequence, topology):
    plan = plan_sequence(sequence, topology, ["MC-2"])
    assert plan.locations == ["mc2"]
    assert [s.name for s in plan.stages] == ORDER


def test_a_bad_name_in_the_sequence_fails_before_anything_runs(topology):
    # Validated when the plan is made, for every stage, not when run_stage
    # reaches it after the earlier stages have been powered.
    config = {"stages": [{"name": "ok", "nodes": ["mu2e-mgr-01.fnal.gov"]},
                         {"name": "bad", "nodes": ["mu2e-x;reboot"]}]}
    with pytest.raises(SequenceSelectionError, match="stage 'bad'"):
        plan_sequence(config, topology, ["mc2"], until_stage="ok")


def test_a_bad_node_name_is_a_topology_error(sequence, topology):
    with pytest.raises(TopologyError):
        plan_sequence(sequence, topology, ["mc2"], ["-oProxyCommand=x"])


def test_config_typos_are_errors_too(sequence, topology):
    with pytest.raises(SequenceSelectionError, match="valid stages"):
        plan_sequence(sequence, topology, ["mc2"], from_stage="manger")


# ---------------------------------------------------------------------------
# Deadline (#13)
# ---------------------------------------------------------------------------


def test_deadline_counts_down_on_its_clock():
    clock = FakeClock()
    d = Deadline(100, clock)
    assert d.remaining() == 100 and not d.expired()
    clock.sleep(60)
    assert d.remaining() == 40
    assert d.cap(120) == 40 and d.cap(10) == 10
    clock.sleep(40)
    assert d.expired() and d.remaining() == 0 and d.cap(10) == 0


@pytest.mark.parametrize("budget", [None, 0, -5])
def test_a_missing_or_zero_budget_is_unbounded(budget):
    clock = FakeClock()
    d = Deadline(budget, clock)
    clock.sleep(1e9)
    assert not d.expired()
    assert d.cap(30) == 30
    assert d.remaining() == float("inf")


def test_a_child_never_outlives_its_parent():
    clock = FakeClock()
    phase = Deadline(100, clock)
    clock.sleep(70)
    stage = phase.child(600)
    assert stage.remaining() == 30
    assert phase.child(10).remaining() == 10
    clock.sleep(30)
    assert stage.expired()
    assert phase.child(600).expired()          # spent parent -> spent child
    assert Deadline(None, clock).child(5).remaining() == 5


# ---------------------------------------------------------------------------
# wait_for_ssh on a fake clock (#13)
# ---------------------------------------------------------------------------


class NeverAnswers:
    """Stands in for LocalTransport under SSHTransport: every ssh times out.

    Each attempt costs its (capped) timeout on the fake clock, as a real
    connect timeout would.
    """

    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.timeouts = []

    def run(self, argv, timeout=None, input_text=None, **kwargs):
        from mu2edaq_power_recovery.transport import CommandResult
        self.timeouts.append(timeout)
        self.clock.sleep(timeout)
        return CommandResult(command=" ".join(argv), rc=255,
                             stderr="ssh: connect to host x port 22: "
                                    "Connection timed out")


def test_wait_for_ssh_stops_at_its_budget_without_sleeping():
    from mu2edaq_power_recovery.transport.ssh import SSHTransport
    clock = FakeClock()
    local = NeverAnswers(clock)
    t = SSHTransport("mu2e-trk-01.fnal.gov", local=local, connect_timeout=10)
    start = clock()
    assert t.wait_for_ssh(600, clock=clock, sleep=clock.sleep) is False
    assert clock() - start <= 600 + 15
    assert local.timeouts and all(x <= 15 for x in local.timeouts)


def test_wait_for_ssh_honours_a_shorter_shared_deadline():
    from mu2edaq_power_recovery.transport.ssh import SSHTransport
    clock = FakeClock()
    local = NeverAnswers(clock)
    t = SSHTransport("mu2e-trk-01.fnal.gov", local=local, connect_timeout=10)
    stage = Deadline(100, clock)
    start = clock()
    assert t.wait_for_ssh(600, deadline=stage, clock=clock,
                          sleep=clock.sleep) is False
    assert clock() - start <= 100 + 1


def test_ssh_calls_are_capped_at_the_phase_budget():
    from mu2edaq_power_recovery.transport import TimeoutExpired
    from mu2edaq_power_recovery.transport.ssh import SSHTransport
    clock = FakeClock()
    local = NeverAnswers(clock)
    phase = Deadline(50, clock)
    t = SSHTransport("h.fnal.gov", local=local, command_timeout=120,
                     deadline_source=lambda: phase)
    with pytest.raises(Exception):
        t.run("true")
    assert local.timeouts == [50]
    # Spent: the next call is refused without starting ssh at all.
    with pytest.raises(TimeoutExpired):
        t.run("true")
    assert len(local.timeouts) == 1
