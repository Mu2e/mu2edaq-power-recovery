"""IPMI client: safety gates, credential handling and parsing.

The safety assertions here are the most important tests in the suite.  A bug
that lets a destructive verb through to a gateway or the NFS server during a
recovery cuts the operator off from the cluster they are recovering.
"""
from __future__ import annotations

import pytest

from mu2edaq_power_recovery.transport import (FakeTransport, IPMIClient,
                                              PowerState, ScriptedResponse,
                                              healthy_node_rules)
from mu2edaq_power_recovery.transport.ipmi import SensorReading


@pytest.fixture
def gateway() -> FakeTransport:
    transport = FakeTransport("mu2egateway01.fnal.gov")
    for pattern, response in healthy_node_rules():
        transport.expect(pattern, response)
    return transport


def make_client(gateway, dry_run=True, protected=None):
    return IPMIClient(gateway=gateway, username="MU2E", password="s3cret",
                      dry_run=dry_run,
                      protected=protected or (lambda host: False))


# ---------------------------------------------------------------------------
# credential handling
# ---------------------------------------------------------------------------


def test_the_password_never_appears_in_a_command_line(gateway):
    client = make_client(gateway)
    client.power_status("mu2e-trk-01-ipmi.fnal.gov")
    for call in gateway.calls:
        assert "s3cret" not in call["command"], \
            "the BMC password reached the gateway's process table"


def test_the_password_is_delivered_on_stdin(gateway):
    client = make_client(gateway)
    client.power_status("mu2e-trk-01-ipmi.fnal.gov")
    assert gateway.calls, "no command was issued"
    assert all(call["stdin"] for call in gateway.calls), \
        "ipmitool was invoked without the password on stdin"


def test_ipmitool_uses_the_environment_password_option(gateway):
    client = make_client(gateway)
    client.power_status("mu2e-trk-01-ipmi.fnal.gov")
    command = gateway.calls[0]["command"]
    assert "-E" in command          # read IPMI_PASSWORD from the environment
    assert "-P" not in command      # never the password-on-the-command-line form


def test_the_recorded_command_is_readable_and_secret_free(gateway):
    client = make_client(gateway)
    result = client._run("mu2e-trk-01-ipmi.fnal.gov", ["chassis", "power", "status"])
    # This string is stored in the run database and shown in the report.
    assert result.command == "ipmitool [mu2e-trk-01-ipmi.fnal.gov] chassis power status"
    assert "s3cret" not in result.command


# ---------------------------------------------------------------------------
# safety gates
# ---------------------------------------------------------------------------


def test_a_protected_host_cannot_be_powered_off(gateway):
    client = make_client(gateway, dry_run=False,
                         protected=lambda host: host == "mu2egateway01.fnal.gov")
    result = client.power("mu2egateway01-ipmi.fnal.gov", "off",
                          node_host="mu2egateway01.fnal.gov")
    assert not result.ok
    assert result.meta["refused"] is True
    assert result.meta["reason"] == "protected"
    assert not gateway.ran("chassis power off")


@pytest.mark.parametrize("verb", ["off", "cycle", "reset"])
def test_every_destructive_verb_is_refused_for_a_protected_host(gateway, verb):
    client = make_client(gateway, dry_run=False, protected=lambda host: True)
    result = client.power("mu2e-mgr-01-ipmi.fnal.gov", verb,
                          node_host="mu2e-mgr-01.fnal.gov")
    assert result.meta.get("refused") is True
    assert not gateway.ran(f"chassis power {verb}")


def test_powering_on_a_protected_host_is_allowed(gateway):
    # The protection list exists to stop the operator cutting their own access,
    # not to stop them restoring it.
    client = make_client(gateway, dry_run=False, protected=lambda host: True)
    result = client.power("mu2e-mgr-01-ipmi.fnal.gov", "on",
                          node_host="mu2e-mgr-01.fnal.gov")
    assert result.ok
    assert gateway.ran("chassis power on")


def test_a_dry_run_issues_nothing(gateway):
    client = make_client(gateway, dry_run=True)
    result = client.power("mu2e-trk-01-ipmi.fnal.gov", "on")
    assert result.ok                       # reported as a success...
    assert result.meta["dry_run"] is True  # ...but nothing happened
    assert not gateway.ran("chassis power on")


def test_a_dry_run_still_reads_state(gateway):
    # Reading is how phase 1 works and must not be gated on --execute.
    client = make_client(gateway, dry_run=True)
    assert client.power_status("mu2e-trk-01-ipmi.fnal.gov") is PowerState.ON
    assert gateway.ran("chassis power status")


# ---------------------------------------------------------------------------
# state reading
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("Chassis Power is on", PowerState.ON),
    ("Chassis Power is off", PowerState.OFF),
    ("something unexpected", PowerState.UNKNOWN),
])
def test_power_state_parsing(text, expected):
    assert PowerState.parse(text) is expected


def test_an_unreachable_bmc_is_distinguished_from_a_powered_off_one(gateway):
    # These need different responses: one is a dead BMC, the other a healthy
    # BMC reporting a chassis that is off as expected before phase 2.
    gateway.expect_first(r"chassis power status",
                         ScriptedResponse(stderr="Unable to establish session", rc=1))
    client = make_client(gateway)
    assert client.power_status("mu2e-trk-01-ipmi.fnal.gov") is PowerState.UNREACHABLE


def test_ensure_on_leaves_a_running_chassis_alone(gateway):
    client = make_client(gateway, dry_run=False)
    outcome = client.ensure_on("mu2e-trk-01-ipmi.fnal.gov")
    assert outcome["action"] == "none" and outcome["ok"]
    assert not gateway.ran("chassis power on")


def test_ensure_on_powers_up_a_chassis_that_is_off(gateway):
    # First status says off, the status read after the power-on says on.
    gateway.expect_first(r"chassis power status",
                         ScriptedResponse(stdout="Chassis Power is off", once=True))
    client = make_client(gateway, dry_run=False)
    outcome = client.ensure_on("mu2e-trk-01-ipmi.fnal.gov")
    assert outcome["action"] == "power_on"
    assert outcome["before"] == "off" and outcome["after"] == "on"
    assert gateway.ran("chassis power on")


def test_ensure_on_reports_an_unreachable_bmc_rather_than_trying(gateway):
    gateway.expect_first(r"chassis power status",
                         ScriptedResponse(stderr="no route to host", rc=1))
    client = make_client(gateway, dry_run=False)
    outcome = client.ensure_on("mu2e-trk-01-ipmi.fnal.gov")
    assert outcome["action"] == "unreachable" and not outcome["ok"]
    assert not gateway.ran("chassis power on")


# ---------------------------------------------------------------------------
# sensors and the event log
# ---------------------------------------------------------------------------


def test_sensor_parsing_skips_unpopulated_slots(gateway):
    gateway.expect_first(r"sdr elist", ScriptedResponse(stdout=(
        "CPU1 Temp   | 01h | ok  |  3.1 | 41 degrees C\n"
        "CPU2 Temp   | 02h | ns  |  3.2 | Disabled\n"
        "FAN3        | 43h | cr  | 29.3 | 300 RPM")))
    readings = make_client(gateway).sensors("mu2e-trk-01-ipmi.fnal.gov")
    names = [r.name for r in readings]
    assert "CPU2 Temp" not in names        # an empty slot is not a failure
    assert [r.name for r in readings if r.critical] == ["FAN3"]


@pytest.mark.parametrize("status,critical", [
    ("ok", False), ("cr", True), ("nc", True), ("nr", True), ("ns", False),
])
def test_sensor_criticality(status, critical):
    assert SensorReading("x", "1", "C", status).critical is critical


def test_retries_are_bounded(gateway):
    gateway.expect_first(r"chassis power status",
                         ScriptedResponse(stderr="timeout", rc=1))
    client = IPMIClient(gateway=gateway, username="MU2E", password="x", retries=2)
    client.power_status("mu2e-trk-01-ipmi.fnal.gov")
    attempts = [c for c in gateway.calls if "chassis power status" in c["command"]]
    assert len(attempts) == 3      # the first try plus two retries, then stop
