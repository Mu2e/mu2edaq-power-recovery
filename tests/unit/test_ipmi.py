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


def ipmitool_calls(gateway):
    """The gateway calls that ran ipmitool -- not the reachability ping."""
    return [c for c in gateway.calls if "ipmitool" in c["command"]]


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
    assert ipmitool_calls(gateway), "no command was issued"
    assert all(call["stdin"] for call in ipmitool_calls(gateway)), \
        "ipmitool was invoked without the password on stdin"


def test_ipmitool_uses_the_environment_password_option(gateway):
    client = make_client(gateway)
    client.power_status("mu2e-trk-01-ipmi.fnal.gov")
    command = ipmitool_calls(gateway)[0]["command"]
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


def test_a_rejected_credential_is_not_retried(gateway):
    """A wrong username is still wrong on the second and third attempt.

    All the retries add is two more failed authentications against a BMC that
    has just demonstrated it is counting them.
    """
    gateway.expect_first(r"chassis power status", ScriptedResponse(
        stderr="Error: Unable to establish IPMI v2 / RMCP+ session\n"
               "RAKP 2 HMAC is invalid", rc=1))
    client = IPMIClient(gateway=gateway, username="MU2E", password="x", retries=2)
    client.power_status("mu2e-trk-01-ipmi.fnal.gov")
    attempts = [c for c in gateway.calls if "chassis power status" in c["command"]]
    assert len(attempts) == 1


def test_a_rejected_credential_stops_the_run_talking_to_any_bmc(gateway):
    # One credential set serves all 65 BMCs, so the next one rejects it too.
    # Without this the wrong username reaches every controller in the cluster,
    # three invocations each, with four ipmitool retries inside every one.
    gateway.expect_first(r"chassis power status", ScriptedResponse(
        stderr="RAKP 2 message indicates an error : unauthorized name", rc=1))
    client = IPMIClient(gateway=gateway, username="MU2E", password="x", retries=2)

    # REFUSED, not UNREACHABLE: the BMC answered, and "the credential is
    # wrong" needs a different response from "the BMC is dark".
    assert client.power_status("mu2e-trk-01-ipmi.fnal.gov") is PowerState.REFUSED
    issued = len(gateway.calls)
    assert client.power_status("mu2e-trk-02-ipmi.fnal.gov") is PowerState.REFUSED
    assert client.sensors("mu2e-trk-03-ipmi.fnal.gov") == []
    assert len(gateway.calls) == issued, "a further BMC was contacted"
    assert "rejected the IPMI credentials" in client.credentials_refused
    assert "--diagnose" in client.credentials_refused, "must say how to resolve it"


def test_a_bmc_that_does_not_answer_does_not_stop_the_run(gateway):
    """A dark chassis reports "Unable to establish ... session" too.

    After a power outage that is the expected state of a good part of the
    cluster, so it must not be read as a wrong password and must not stop the
    other BMCs being interrogated.
    """
    gateway.expect_first(r"chassis power status", ScriptedResponse(
        stderr="Error: Unable to establish IPMI v2 / RMCP+ session", rc=1))
    client = IPMIClient(gateway=gateway, username="MU2E", password="x", retries=1)
    client.power_status("mu2e-trk-01-ipmi.fnal.gov")

    assert client.credentials_refused is None
    attempts = [c for c in gateway.calls if "chassis power status" in c["command"]]
    assert len(attempts) == 2, "an unanswered BMC must still be retried"


def test_the_credential_stop_can_be_turned_off(gateway):
    # For deliberately gathering evidence from several BMCs at once.
    gateway.expect_first(r"chassis power status",
                         ScriptedResponse(stderr="RAKP 2 HMAC is invalid", rc=1))
    client = IPMIClient(gateway=gateway, username="MU2E", password="x", retries=0,
                        stop_on_auth_failure=False)
    client.power_status("mu2e-trk-01-ipmi.fnal.gov")
    issued = len(gateway.calls)
    client.power_status("mu2e-trk-02-ipmi.fnal.gov")
    assert len(gateway.calls) > issued
    assert client.credentials_refused is None


# ---------------------------------------------------------------------------
# invocation shape -- this is where a regression breaks the real cluster
# ---------------------------------------------------------------------------


def test_the_invocation_matches_the_known_working_upstream_form(gateway):
    """The upstream mu2e_ipmi.sh command is known to work against these BMCs.

    Deviating from it is how "Unable to establish IPMI v2 / RMCP+ session"
    happened: an added ``-R 1`` cut ipmitool to a single attempt, and a BMC
    that needs a retry then never establishes a session at all.
    """
    client = make_client(gateway)
    command = client.describe("mu2e-crv-01-ipmi.fnal.gov",
                              ["chassis", "power", "status"])
    for expected in ("-I lanplus", "-H mu2e-crv-01-ipmi.fnal.gov", "-U MU2E",
                     "-L Operator", "-C 3", "chassis power status"):
        assert expected in command, f"missing {expected!r}"


def test_ipmitool_retry_flags_are_not_sent_by_default(gateway):
    # ipmitool retries four times by default; forcing fewer is what broke it.
    command = make_client(gateway).describe("bmc", ["chassis", "power", "status"])
    assert " -R " not in command
    assert " -N " not in command


def test_retry_flags_are_sent_when_configured(gateway):
    client = IPMIClient(gateway=gateway, username="MU2E", password="x",
                        message_timeout=3, tool_retries=6)
    command = client.describe("bmc", ["chassis", "power", "status"])
    assert "-N 3" in command and "-R 6" in command


def test_extra_args_are_appended(gateway):
    client = IPMIClient(gateway=gateway, username="MU2E", password="x",
                        extra_args=["-e", "^"])
    # Quoted, because '^' is a shell metacharacter and this string is run by
    # the gateway's shell.
    assert "-e '^'" in client.describe("bmc", ["sol", "activate"])


def test_the_gateway_timeout_allows_for_ipmitool_retries(gateway):
    # The wall-clock bound must not itself cut the retries short.
    client = IPMIClient(gateway=gateway, username="MU2E", password="x", timeout=10)
    assert client.tool_timeout() >= 30
    assert client.describe("bmc", []).startswith("timeout 30 ")


def test_describe_never_contains_the_password(gateway):
    client = IPMIClient(gateway=gateway, username="MU2E", password="s3cret")
    assert "s3cret" not in client.describe("bmc", ["chassis", "power", "status"])


# ---------------------------------------------------------------------------
# failure diagnosis
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stderr,expected", [
    ("Error: Unable to establish IPMI v2 / RMCP+ session", "username"),
    ("Error: RAKP 2 HMAC is invalid", "password is wrong"),
    ("Error: Unauthorized name", "does not have an account"),
    ("Error: Requested privilege level exceeds limit", "Administrator"),
])
def test_session_failures_get_a_plain_language_cause(gateway, stderr, expected):
    # All of these look alike in raw ipmitool output; the operator needs to be
    # told which knob to turn.
    gateway.expect_first(r"chassis power status",
                         ScriptedResponse(stderr=stderr, rc=1))
    client = IPMIClient(gateway=gateway, username="MU2E", password="x", retries=0)
    result = client._run("bmc", ["chassis", "power", "status"])
    assert expected in result.meta.get("diagnosis", "")
    # The invocation is attached so it can be compared with a working one.
    assert "ipmitool" in result.meta.get("invocation", "")


def test_a_successful_call_is_not_annotated(gateway):
    client = make_client(gateway)
    result = client._run("bmc", ["chassis", "power", "status"])
    assert result.ok and "diagnosis" not in result.meta


# ---------------------------------------------------------------------------
# username resolution and the diagnose sweep
# ---------------------------------------------------------------------------


def test_candidate_usernames_tries_case_variants_most_likely_first():
    """IPMI usernames are case sensitive.

    The live Vault secret held 'mu2e' while the working upstream invocation
    hard-codes 'MU2E', which is exactly the failure this ordering targets.
    """
    from mu2edaq_power_recovery.tools.ipmi_tool import candidate_usernames

    assert candidate_usernames("mu2e") == ["mu2e", "MU2E"]
    # The configured value is always tried first, whatever it is.
    assert candidate_usernames("admin")[0] == "admin"
    assert "MU2E" in candidate_usernames("admin")
    # No duplicates, however the cases collide.
    assert len(candidate_usernames("MU2E")) == len(set(candidate_usernames("MU2E")))


def test_the_diagnose_sweep_is_capped():
    # Every failed attempt counts towards the BMC's account lockout, so an
    # exhaustive sweep is a good way to lock the account mid-recovery.
    from mu2edaq_power_recovery.tools.ipmi_tool import (CANDIDATE_CIPHERS,
                                                        MAX_ATTEMPTS)

    assert MAX_ATTEMPTS <= 9
    assert CANDIDATE_CIPHERS[0] == 3      # what upstream uses, tried first


def test_the_configured_username_overrides_vault(settings):
    # Vault is the source by default, but the two can disagree and the secret
    # is maintained elsewhere -- so there has to be a local override.
    assert settings.get("ipmi.username") is None      # default: use Vault
    settings.set("ipmi.username", "MU2E")
    assert settings.get("ipmi.username") == "MU2E"


# ---------------------------------------------------------------------------
# the credential circuit breaker under concurrency (#8)
# ---------------------------------------------------------------------------

import threading  # noqa: E402
import time  # noqa: E402

from mu2edaq_power_recovery.transport import (CredentialBreaker,  # noqa: E402
                                              IPMICredentialsRefused)
from mu2edaq_power_recovery.transport.base import (CommandResult,  # noqa: E402
                                                   Transport)

REJECTION = "Error: Unable to establish IPMI v2 / RMCP+ session\nRAKP 2 HMAC is invalid"


class CountingGateway(Transport):
    """A gateway that holds each invocation open and counts the overlap.

    FakeTransport answers under its rule lock, which would itself serialise
    the calls this test is trying to see overlap, so this one answers with no
    lock around the delay.
    """

    def __init__(self, stdout="Chassis Power is on", stderr="", rc=0, delay=0.2,
                 ping_rc=0, ping_delay=0.0):
        self.host = "mu2egateway01.fnal.gov"
        self.stdout, self.stderr, self.rc, self.delay = stdout, stderr, rc, delay
        #: The reachability pre-check's answer; counted apart from ipmitool.
        self.ping_rc, self.ping_delay = ping_rc, ping_delay
        self._lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.invocations = 0
        self.pings = 0
        self.ping_in_flight = 0
        self.max_ping_in_flight = 0

    def run(self, command, timeout=None, user=None, input_text=None, check=False):
        if str(command).startswith("ping "):
            with self._lock:
                self.pings += 1
                self.ping_in_flight += 1
                self.max_ping_in_flight = max(self.max_ping_in_flight,
                                              self.ping_in_flight)
            try:
                time.sleep(self.ping_delay)
            finally:
                with self._lock:
                    self.ping_in_flight -= 1
            return CommandResult(command=str(command), rc=self.ping_rc,
                                 host=self.host)
        with self._lock:
            self.invocations += 1
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            time.sleep(self.delay)
        finally:
            with self._lock:
                self.in_flight -= 1
        return CommandResult(command=str(command), rc=self.rc, stdout=self.stdout,
                             stderr=self.stderr, host=self.host)


def _concurrently(calls):
    """Start every call at the same instant; return their results in order."""
    barrier = threading.Barrier(len(calls))
    results = [None] * len(calls)

    def worker(i, fn):
        barrier.wait()
        try:
            results[i] = fn()
        except Exception as exc:  # noqa: BLE001 - the result under test
            results[i] = exc

    threads = [threading.Thread(target=worker, args=(i, fn))
               for i, fn in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results


def test_a_concurrent_start_puts_a_rejected_credential_to_one_bmc_only():
    # Sixteen workers is ssh.max_sessions: every one of them reaches the
    # breaker before the first rejection could have been seen.
    gateway = CountingGateway(stderr=REJECTION, rc=1)
    client = IPMIClient(gateway=gateway, username="MU2E", password="x", retries=2)
    calls = [lambda i=i: client.power_status(f"bmc-{i:02d}") for i in range(16)]
    states = _concurrently(calls)

    assert gateway.invocations == 1, "a wrong credential reached a second BMC"
    assert all(s is PowerState.REFUSED for s in states)
    assert "rejected the IPMI credentials" in client.credentials_refused


def test_waiting_callers_get_the_shared_diagnosis_without_invoking_ipmitool():
    gateway = CountingGateway(stderr=REJECTION, rc=1)
    client = IPMIClient(gateway=gateway, username="MU2E", password="x", retries=0)
    calls = [lambda i=i: client._run(f"bmc-{i}", ["chassis", "power", "status"])
             for i in range(8)]
    results = _concurrently(calls)

    raised = [r for r in results if isinstance(r, IPMICredentialsRefused)]
    answered = [r for r in results if isinstance(r, CommandResult)]
    assert len(answered) == 1 and not answered[0].ok
    assert len(raised) == 7
    assert {str(r) for r in raised} == {client.credentials_refused}
    assert gateway.invocations == 1


def test_mixed_status_sensor_and_sel_requests_share_one_breaker():
    gateway = CountingGateway(stderr=REJECTION, rc=1)
    client = IPMIClient(gateway=gateway, username="MU2E", password="x")
    calls = []
    for i in range(4):
        calls += [lambda i=i: client.power_status(f"bmc-{i}"),
                  lambda i=i: client.sensors(f"bmc-{i}"),
                  lambda i=i: client.sel(f"bmc-{i}")]
    results = _concurrently(calls)

    assert gateway.invocations == 1
    assert [r for r in results[0::3]] == [PowerState.REFUSED] * 4
    assert results[1::3] == [[]] * 4          # no sensor data
    assert results[2::3] == [None] * 4        # an unread log, not an empty one


def test_one_breaker_serves_several_clients():
    # One BMC account, whichever gateway ipmitool runs on: clients sharing a
    # breaker (those of one location) must all stop together.
    breaker = CredentialBreaker()
    gw_a = CountingGateway(stderr=REJECTION, rc=1)
    gw_b = CountingGateway(stderr=REJECTION, rc=1)
    a = IPMIClient(gateway=gw_a, username="MU2E", password="x", breaker=breaker)
    b = IPMIClient(gateway=gw_b, username="MU2E", password="x", breaker=breaker)
    calls = [lambda i=i: (a if i % 2 else b).power_status(f"bmc-{i}")
             for i in range(10)]
    _concurrently(calls)

    assert gw_a.invocations + gw_b.invocations == 1
    assert a.credentials_refused == b.credentials_refused is not None


def test_separate_breakers_do_not_stop_each_other():
    # The run gives each location its own breaker: on 2026-10-01 the
    # teststand's BMCs refused the account MC-2's accepted.
    teststand = IPMIClient(gateway=CountingGateway(stderr=UNESTABLISHED_TEXT,
                                                   rc=1, delay=0.0),
                           username="MU2E", password="x", retries=0,
                           breaker=CredentialBreaker("teststand"))
    mc2_gateway = CountingGateway(delay=0.0)
    mc2 = IPMIClient(gateway=mc2_gateway, username="MU2E", password="x",
                     retries=0, breaker=CredentialBreaker("mc2"))
    teststand.power_status("mu2edaq04-ipmi")
    assert teststand.power_status("mu2edaq13-ipmi") is PowerState.REFUSED
    assert "BMCs at teststand" in teststand.credentials_refused
    assert mc2.credentials_refused is None
    assert mc2.power_status("mu2e-trk-01-ipmi") is PowerState.ON
    assert mc2_gateway.invocations == 1


def test_a_proven_credential_allows_concurrency():
    gateway = CountingGateway(delay=0.2)
    client = IPMIClient(gateway=gateway, username="MU2E", password="x")
    states = _concurrently([lambda i=i: client.power_status(f"bmc-{i}")
                            for i in range(8)])

    assert all(s is PowerState.ON for s in states)
    assert gateway.invocations == 8
    # The first call runs alone; once it proves the credential the rest are
    # not serialised behind the gate.
    assert client.breaker.proven.is_set()
    assert gateway.max_in_flight > 1


def test_an_unproven_credential_is_tried_one_bmc_at_a_time():
    # With the pre-check off (BMCs that filter ICMP) a dark BMC is
    # indistinguishable from a live one, proves nothing, and the calls stay
    # serialised: the price of never sending a burst of unproven attempts.
    gateway = CountingGateway(stderr="Error: Unable to establish IPMI v2 / "
                                     "RMCP+ session", rc=1, delay=0.05)
    client = IPMIClient(gateway=gateway, username="MU2E", password="x", retries=0,
                        reachability_precheck=False)
    states = _concurrently([lambda i=i: client.power_status(f"bmc-{i}")
                            for i in range(6)])

    assert all(s is PowerState.UNREACHABLE for s in states)
    assert gateway.invocations == 6
    assert gateway.max_in_flight == 1
    assert client.credentials_refused is None


def test_with_the_breaker_off_nothing_is_serialised_or_stopped():
    gateway = CountingGateway(stderr=REJECTION, rc=1)
    client = IPMIClient(gateway=gateway, username="MU2E", password="x",
                        retries=0, stop_on_auth_failure=False)
    states = _concurrently([lambda i=i: client.power_status(f"bmc-{i}")
                            for i in range(6)])

    assert gateway.invocations == 6
    assert gateway.max_in_flight > 1
    assert client.credentials_refused is None
    # Still reported as a refusal, not as a BMC that does not answer.
    assert all(s is PowerState.REFUSED for s in states)


def test_the_breaker_is_checked_again_before_every_retry(gateway, monkeypatch):
    import mu2edaq_power_recovery.transport.ipmi as ipmi_module

    monkeypatch.setattr(ipmi_module.time, "sleep", lambda s: None)
    breaker = CredentialBreaker()
    breaker.prove()

    def timeout_and_trip(command):
        # Another worker is refused while this call waits to retry.
        breaker.refuse("bmc-other rejected the IPMI credentials")
        return ScriptedResponse(stderr="Error: timed out", rc=1)

    gateway.expect_first(r"chassis power status", timeout_and_trip)
    client = IPMIClient(gateway=gateway, username="MU2E", password="x",
                        retries=2, breaker=breaker)
    assert client.power_status("bmc-01") is PowerState.REFUSED
    attempts = [c for c in gateway.calls if "chassis power status" in c["command"]]
    assert len(attempts) == 1, "retried after the breaker tripped"


def test_ensure_on_reports_a_credential_refusal_distinctly(gateway):
    gateway.expect_first(r"chassis power status",
                         ScriptedResponse(stderr=REJECTION, rc=1))
    client = make_client(gateway, dry_run=False)
    outcome = client.ensure_on("mu2e-trk-01-ipmi.fnal.gov")
    assert outcome["action"] == "credentials_refused" and not outcome["ok"]
    assert "rejected the IPMI credentials" in outcome["detail"]
    assert not gateway.ran("chassis power on")


def test_the_protected_refusal_is_unchanged_by_a_tripped_breaker(gateway):
    # The deliberate refusal comes first and reads as one, whatever the
    # credential state: it is decided before any command is built.
    client = make_client(gateway, dry_run=False, protected=lambda host: True)
    client.breaker.refuse("some BMC rejected the IPMI credentials")
    result = client.power("mu2egateway01-ipmi.fnal.gov", "off",
                          node_host="mu2egateway01.fnal.gov")
    assert result.rc == 77
    assert result.meta == {"refused": True, "reason": "protected"}
    assert "protected host" in result.stderr
    assert not gateway.calls


def test_a_gateway_that_cannot_be_reached_is_not_a_credential_refusal(gateway):
    gateway.expect_first(r"chassis power status",
                         ScriptedResponse(raises="connection refused"))
    client = make_client(gateway)
    assert client.power_status("bmc") is PowerState.UNREACHABLE
    assert client.credentials_refused is None


# ---------------------------------------------------------------------------
# reachability pre-check and the live-BMC "Unable to establish" stop
# ---------------------------------------------------------------------------

UNESTABLISHED_TEXT = "Error: Unable to establish IPMI v2 / RMCP+ session"


def test_dark_bmcs_never_take_the_gate_and_are_checked_concurrently():
    gateway = CountingGateway(ping_rc=1, ping_delay=0.2)
    breaker = CredentialBreaker()
    client = IPMIClient(gateway=gateway, username="MU2E", password="x",
                        breaker=breaker)
    # Hold the gate for the whole test: a call that tried to take it would
    # block, and the test would time out instead of passing.
    breaker.gate.acquire()
    try:
        started = time.monotonic()
        states = _concurrently([lambda i=i: client.power_status(f"bmc-{i}")
                                for i in range(8)])
        elapsed = time.monotonic() - started
    finally:
        breaker.gate.release()

    assert all(s is PowerState.UNREACHABLE for s in states)
    assert gateway.invocations == 0, "ipmitool was run against a dark BMC"
    assert gateway.pings == 8
    assert gateway.max_ping_in_flight > 1
    assert elapsed < 8 * 0.2, "the dark BMCs were checked one at a time"
    assert client.credentials_refused is None


def test_the_precheck_is_a_single_quoted_ping_from_the_gateway(gateway):
    gateway.expect_first(r"^ping ", ScriptedResponse(rc=1, stdout=(
        "3 packets transmitted, 0 received, 100% packet loss, time 405ms")))
    client = make_client(gateway)
    assert client.power_status("mu2e-trk-01-ipmi.fnal.gov") is PowerState.UNREACHABLE
    assert gateway.commands() == [
        "ping -c 3 -i 0.2 -W 1 -q mu2e-trk-01-ipmi.fnal.gov"]


def test_the_precheck_ping_follows_the_gateways_dialect(gateway):
    client = make_client(gateway)
    gateway.platform = "darwin"
    assert client._ping_command("bmc") == "ping -c 3 -W 1000 -q bmc"
    gateway.platform = "win32"
    assert client._ping_command("bmc") == "ping -n 3 -w 1000 bmc"


#: iputils summary when the first echo is lost to a cold ARP entry.
FIRST_ECHO_LOST = ("--- mu2e-trk-01-ipmi.fnal.gov ping statistics ---\n"
                   "3 packets transmitted, 2 received, 33.3333% packet loss, "
                   "time 402ms\n")


def test_a_lost_first_echo_does_not_make_a_live_bmc_unreachable(gateway):
    # Right after an outage the gateway's ARP entry for the BMC is cold and
    # the first echo is lost. One echo used to be the whole pre-check, the BMC
    # was UNREACHABLE, and ensure_on never switched its chassis on.
    gateway.expect_first(r"^ping ", ScriptedResponse(rc=0, stdout=FIRST_ECHO_LOST))
    gateway.expect_first(r"chassis power status",
                         ScriptedResponse(stdout="Chassis Power is off"))
    client = make_client(gateway, dry_run=False)
    outcome = client.ensure_on("mu2e-trk-01-ipmi.fnal.gov")
    assert gateway.ran("chassis power on"), outcome
    assert client.unreachable_reason == {}


def test_any_reply_counts_even_if_ping_exits_nonzero_on_partial_loss(gateway):
    gateway.expect_first(r"^ping ", ScriptedResponse(rc=1, stdout=FIRST_ECHO_LOST))
    client = make_client(gateway)
    assert client.power_status("mu2e-trk-01-ipmi.fnal.gov") is PowerState.ON


def test_the_failure_diagnosis_also_tolerates_a_lost_echo(gateway):
    # _why_unreachable reuses the pre-check after an "Unable to establish":
    # a BMC that answered two of three echoes is "no_session", not "dark".
    gateway.expect_first(r"^ping ", ScriptedResponse(rc=1, stdout=FIRST_ECHO_LOST))
    gateway.expect_first(r"chassis power status", ScriptedResponse(
        stderr="Error: Unable to establish IPMI v2 / RMCP+ session", rc=1))
    client = IPMIClient(gateway=gateway, username="MU2E", password="x",
                        retries=0, reachability_precheck=True)
    client.breaker.prove()
    assert client.power_status("bmc-0") is PowerState.UNREACHABLE
    assert client.unreachable_reason["bmc-0"] == "no_session"


def test_two_live_bmcs_that_will_not_open_a_session_trip_the_breaker():
    gateway = CountingGateway(stderr=UNESTABLISHED_TEXT, rc=1, delay=0.0)
    client = IPMIClient(gateway=gateway, username="mu2e", password="x", retries=0)

    first = client.power_status("bmc-0")
    assert first is PowerState.UNREACHABLE
    assert client.credentials_refused is None, "one live BMC must not trip it"
    assert client.power_status("bmc-1") is PowerState.REFUSED
    assert "wrong username" in client.credentials_refused
    assert "'mu2e'" in client.credentials_refused
    # No third BMC is asked.
    assert client.power_status("bmc-2") is PowerState.REFUSED
    assert gateway.invocations == 2


def test_the_same_live_bmc_failing_twice_does_not_trip_the_breaker():
    gateway = CountingGateway(stderr=UNESTABLISHED_TEXT, rc=1, delay=0.0)
    client = IPMIClient(gateway=gateway, username="MU2E", password="x", retries=0)
    client.power_status("bmc-0")
    client.sensors("bmc-0")
    assert client.credentials_refused is None


def test_a_rakp_rejection_still_trips_on_the_first_bmc():
    gateway = CountingGateway(stderr=REJECTION, rc=1, delay=0.0)
    client = IPMIClient(gateway=gateway, username="MU2E", password="x", retries=0)
    assert client.power_status("bmc-0") is PowerState.REFUSED
    assert client.credentials_refused is not None
    assert gateway.invocations == 1


def test_unable_to_establish_after_a_proven_credential_does_not_count():
    gateway = CountingGateway(stderr=UNESTABLISHED_TEXT, rc=1, delay=0.0)
    client = IPMIClient(gateway=gateway, username="MU2E", password="x", retries=0)
    client.breaker.prove()
    for i in range(4):
        assert client.power_status(f"bmc-{i}") is PowerState.UNREACHABLE
    assert client.credentials_refused is None
    # No pre-check before any invocation; one diagnostic ping after each
    # failure, to tell "answers but no session" from dark.
    assert gateway.invocations == 4
    assert gateway.pings == 4


def test_with_the_precheck_off_nothing_is_pinged_and_nothing_trips():
    gateway = CountingGateway(stderr=UNESTABLISHED_TEXT, rc=1, delay=0.0,
                              ping_rc=1)
    client = IPMIClient(gateway=gateway, username="MU2E", password="x", retries=0,
                        reachability_precheck=False)
    for i in range(4):
        assert client.power_status(f"bmc-{i}") is PowerState.UNREACHABLE
    assert gateway.pings == 0
    assert gateway.invocations == 4
    assert client.credentials_refused is None


def test_a_gateway_without_ping_falls_back_to_asking_the_bmc():
    gateway = CountingGateway(ping_rc=127, delay=0.0)
    client = IPMIClient(gateway=gateway, username="MU2E", password="x")
    assert client.power_status("bmc-0") is PowerState.ON
    assert gateway.invocations == 1


def test_the_precheck_setting_reaches_the_run_client(settings):
    from mu2edaq_power_recovery.orchestrator import Orchestrator
    from mu2edaq_power_recovery.creds.vault import IPMICredentials

    settings.set("ipmi.reachability_precheck", False)
    orch = Orchestrator(settings, simulate=True)
    try:
        orch.prepare_credentials()
        client = orch._make_ipmi_client(IPMICredentials(username="u",
                                                        password="p"),
                                        "mu2egateway01.fnal.gov", "mc2")
        assert client.reachability_precheck is False
    finally:
        orch.close()


#: ipmitool on mu2egateway01 for a topology BMC name with no DNS entry,
#: captured live 2026-10-01.
UNRESOLVED_STDERR = ("Address lookup for mu2edaq10-ipmi.fnal.gov failed\n"
                     "Could not open socket!\n"
                     "Error: Unable to establish IPMI v2 / RMCP+ session\n")


def test_an_unresolvable_bmc_name_is_diagnosed_as_such_and_not_retried(gateway):
    gateway.expect_first(r"chassis power status",
                         ScriptedResponse(stderr=UNRESOLVED_STDERR, rc=1))
    client = IPMIClient(gateway=gateway, username="MU2E", password="x", retries=2)
    assert client.power_status("mu2edaq10-ipmi.fnal.gov") is PowerState.UNREACHABLE
    attempts = [c for c in gateway.calls if "chassis power status" in c["command"]]
    assert len(attempts) == 1
    assert client.credentials_refused is None


def test_an_unresolvable_name_never_counts_toward_the_credential_stop(gateway):
    gateway.expect_first(r"chassis power status",
                         ScriptedResponse(stderr=UNRESOLVED_STDERR, rc=1))
    client = IPMIClient(gateway=gateway, username="MU2E", password="x", retries=0)
    result = client._run("mu2edaq10-ipmi.fnal.gov", ["chassis", "power", "status"])
    assert "does not resolve" in result.meta["diagnosis"]
    assert "credential" not in result.meta["diagnosis"].split(".")[0]
    assert not result.meta.get("credentials_refused")


# ---------------------------------------------------------------------------
# PR #30 review: a state-changing call is never cut short by the phase budget
# ---------------------------------------------------------------------------

class ExemptRecordingGateway(Transport):
    """Records, per command, whether it ran exempt from the phase deadline."""

    host = "mu2egateway01.fnal.gov"

    def __init__(self, state="off"):
        self.state, self.seen = state, []

    def run(self, command, timeout=None, user=None, input_text=None, check=False):
        from mu2edaq_power_recovery.transport.base import is_deadline_exempt
        text = str(command)
        self.seen.append((text, is_deadline_exempt()))
        if "power on" in text:
            self.state = "on"
            return CommandResult(command=text, rc=0, stdout="Chassis Power Control: Up/On")
        if text.startswith("ping "):
            return CommandResult(command=text, rc=0, stdout="1 received")
        return CommandResult(command=text, rc=0, stdout=f"Chassis Power is {self.state}")


def test_power_on_and_its_confirmation_run_exempt_from_the_deadline():
    gateway = ExemptRecordingGateway()
    client = IPMIClient(gateway=gateway, username="MU2E", password="x", retries=0,
                        dry_run=False)
    outcome = client.ensure_on("mu2e-trk-01-ipmi.fnal.gov")
    assert outcome["action"] == "power_on" and outcome["ok"]
    exempt = {("power on" in c, "power status" in c): e for c, e in gateway.seen
              if "chassis" in c}
    assert exempt[(True, False)] is True                 # the power command
    statuses = [e for c, e in gateway.seen if "power status" in c]
    assert statuses == [False, True]   # read before: capped; confirm after: exempt


def test_an_exempt_ssh_call_ignores_an_expired_budget():
    from mu2edaq_power_recovery.transport.base import deadline_exempt
    from mu2edaq_power_recovery.transport.ssh import SSHTransport

    class Expired:
        def expired(self):
            return True

        def remaining(self):
            return 0.0

    t = SSHTransport(host="h", deadline_source=lambda: Expired(), command_timeout=30)
    with pytest.raises(Exception):
        t._capped(30)
    with deadline_exempt():
        assert t._capped(30) == 30.0
