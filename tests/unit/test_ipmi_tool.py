"""mu2e-ipmi-tool target selection (#16).

A node with no BMC has no address to give ipmitool. The helper used to fall
back to the unfiltered selection when every selected node lacked one, and then
built ``ipmitool -H None``. These tests pin the replacement: skipped nodes are
named on stderr with the reason, nothing is contacted when no valid target
remains, and a state change lists every target by name before it is confirmed.
"""
from __future__ import annotations

import pytest

from mu2edaq_power_recovery.tools import ipmi_tool
from mu2edaq_power_recovery.transport import (FakeTransport, ScriptedResponse,
                                              healthy_node_rules)


class _Creds:
    username = "MU2E"
    password = "x"
    source = "stub"


@pytest.fixture
def contacted(monkeypatch):
    """Stub Vault and the credential session; record whether either was built."""
    seen = {"vault": 0, "factory": 0}
    gateway = FakeTransport("mu2egateway01.fnal.gov")
    for pattern, response in healthy_node_rules():
        gateway.expect(pattern, response)

    class Vault:
        def __init__(self, *args, **kwargs):
            seen["vault"] += 1

        def ipmi(self):
            return _Creds()

    class Factory:
        def __init__(self, *args, **kwargs):
            seen["factory"] += 1

        def gateway_for(self, location):
            return "mu2egateway01.fnal.gov"

        def for_host(self, host, **kwargs):
            return gateway

    from contextlib import contextmanager
    from types import SimpleNamespace

    @contextmanager
    def session(*args, **kwargs):
        # The factory now comes from creds.bootstrap.credential_session (#14);
        # building it there is what "contacting" means for these tests.
        yield SimpleNamespace(factory=Factory(), warning=None, notes=[])

    monkeypatch.setattr(ipmi_tool, "VaultCredentials", Vault)
    monkeypatch.setattr(ipmi_tool, "credential_session", session)
    seen["gateway"] = gateway
    return seen


def test_a_lone_bmc_less_node_contacts_nothing_and_exits_2(contacted, capsys):
    rc = ipmi_tool.main(["-l", "mc2", "-n", "mu2e-dcs-03",
                         "chassis", "power", "status"])
    assert rc == 2
    assert contacted["vault"] == 0 and contacted["factory"] == 0
    err = capsys.readouterr().err
    assert "skipping mu2e-dcs-03: no BMC" in err
    assert "no target nodes with a BMC" in err


def test_an_unknown_host_is_reported_as_unknown_not_as_bmc_less(contacted, capsys):
    rc = ipmi_tool.main(["-l", "mc2", "-n", "mu2e-nosuch-99",
                         "chassis", "power", "status"])
    assert rc == 2 and contacted["vault"] == 0
    assert "skipping mu2e-nosuch-99: unknown host" in capsys.readouterr().err


def test_a_mixed_selection_operates_only_on_the_valid_bmcs(contacted, capsys):
    rc = ipmi_tool.main(["-l", "mc2", "-n", "mu2e-dcs-03", "-n", "mu2e-dcs-01",
                         "chassis", "power", "status"])
    assert rc == 0
    out = capsys.readouterr()
    assert "skipping mu2e-dcs-03: no BMC" in out.err
    commands = [c["command"] for c in contacted["gateway"].calls
                if "ipmitool" in c["command"]]
    assert len(commands) == 1
    assert "mu2e-dcs-01" in commands[0]
    assert "None" not in commands[0]


def test_class_selection_skips_the_bmc_less_members(topology):
    valid, skipped = ipmi_tool.select_targets(topology, "mc2", None, ["dcs"])
    assert [n.short for n, _ in skipped] == ["mu2e-dcs-03"]
    assert "no BMC" in skipped[0][1]
    assert valid and all(n.ipmi_host for n in valid)


def test_default_selection_is_every_bmc_and_reports_nothing(topology):
    valid, skipped = ipmi_tool.select_targets(topology, "mc2", None, None)
    assert skipped == []
    assert len(valid) == sum(1 for n in topology.all_nodes(["mc2"]) if n.ipmi_host)


def test_explicit_selection_never_falls_back_to_the_unfiltered_list(topology):
    valid, skipped = ipmi_tool.select_targets(
        topology, "mc2", ["mu2e-dcs-03", "mu2e-trk-05"], None)
    assert valid == []
    assert [n.short for n, _ in skipped] == ["mu2e-dcs-03", "mu2e-trk-05"]


def test_the_confirmation_lists_every_target_by_hostname(contacted, capsys,
                                                         monkeypatch, topology):
    prompts = []
    monkeypatch.setattr("builtins.input", lambda text: prompts.append(text) or "no")
    rc = ipmi_tool.main(["-l", "mc2", "-c", "tracker", "--execute",
                         "chassis", "power", "cycle"])
    assert rc == 3                                  # declined
    assert contacted["vault"] == 0 and not contacted["gateway"].calls
    out = capsys.readouterr().out
    trackers = [n for n in topology.all_nodes(["mc2"])
                if n.node_class == "tracker" and n.ipmi_host]
    assert len(trackers) > 10                       # the old prompt stopped at 10
    for node in trackers:
        assert node.hostname in out, f"{node.hostname} missing from the listing"
    assert "more" not in prompts[0]
    # The BMC-less trackers are not in the list of what will be changed.
    for node in topology.all_nodes(["mc2"]):
        if node.node_class == "tracker" and not node.ipmi_host:
            assert f"{node.hostname}  [BMC" not in out


def test_a_protected_target_is_marked_in_the_listing(contacted, capsys, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda text: "no")
    ipmi_tool.main(["-l", "mc2", "-n", "mu2e-dcs-01", "--execute",
                    "chassis", "power", "off"])
    out = capsys.readouterr().out
    assert "mu2e-dcs-01.fnal.gov" in out
    assert "protected: will be refused" in out


def test_an_invalid_host_name_is_a_clean_error_not_a_traceback(contacted, capsys):
    rc = ipmi_tool.main(["-l", "mc2", "-n", "mu2e-trk-01;reboot",
                         "chassis", "power", "status"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "error: " in err
    assert "Traceback" not in err
    assert contacted["vault"] == 0 and contacted["factory"] == 0
    assert not contacted["gateway"].calls


def test_a_topology_error_from_target_selection_exits_2(contacted, capsys,
                                                        monkeypatch):
    from mu2edaq_power_recovery.topology import Topology, TopologyError

    def refuse(self, names, locations=None):
        raise TopologyError(f"invalid hostname {names[0]!r}")

    monkeypatch.setattr(Topology, "resolve", refuse)
    rc = ipmi_tool.main(["-l", "mc2", "-n", "bad name",
                         "chassis", "power", "status"])
    assert rc == 2
    err = capsys.readouterr().err
    assert err == "error: invalid hostname 'bad name'\n"
    assert contacted["vault"] == 0 and not contacted["gateway"].calls


def test_ipmi_tool_stops_a_sweep_at_the_first_credential_refusal(contacted, capsys):
    gateway = contacted["gateway"]
    gateway.expect_first(r"ipmitool", ScriptedResponse(
        stderr="RAKP 2 HMAC is invalid", rc=1))
    rc = ipmi_tool.main(["-l", "mc2", "-c", "tracker", "-q",
                         "chassis", "power", "status"])
    assert rc == 1
    asked = [c for c in gateway.calls if "ipmitool" in c["command"]]
    assert len(asked) == 1, "the rejected credential reached a second BMC"
    assert "stopped:" in capsys.readouterr().out


def test_ipmi_tool_honours_stop_on_auth_failure_false(contacted, monkeypatch):
    gateway = contacted["gateway"]
    gateway.expect_first(r"ipmitool", ScriptedResponse(
        stderr="RAKP 2 HMAC is invalid", rc=1))
    monkeypatch.setenv("MU2E_POWER_RECOVERY_IPMI_STOP_ON_AUTH_FAILURE", "false")
    rc = ipmi_tool.main(["-l", "mc2", "-c", "tracker", "-q",
                         "chassis", "power", "status"])
    assert rc == 1
    asked = [c for c in gateway.calls if "ipmitool" in c["command"]]
    assert len(asked) > 1
