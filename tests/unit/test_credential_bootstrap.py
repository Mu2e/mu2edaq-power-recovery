"""The shared credential bootstrap, and the two diagnostics that now use it.

mu2e-ipmi-tool used to open its gateway session with no Kerberos manager at
all, and mu2e-ssh-probe reported a configured principal while ssh used the
ambient cache. Both now go through creds/bootstrap.py, as the run does. What
these tests pin down is the login/ticket pair each ssh attempt is actually
given, that show-only acquires nothing, and that the private caches are
destroyed on every exit path.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mu2edaq_power_recovery.creds import bootstrap as bootstrap_module
from mu2edaq_power_recovery.creds.kerberos import KerberosManager, TicketInfo
from mu2edaq_power_recovery.creds.ticketsource import (DefaultCacheDisplaced,
                                                       ServiceTicket)
from mu2edaq_power_recovery.creds.vault import IPMICredentials
from mu2edaq_power_recovery.transport.base import CommandResult
from mu2edaq_power_recovery.transport import local as local_module

GATEWAY = "mu2egateway01.fnal.gov"
AMBIENT = "anorman@FNAL.GOV"


class Tickets:
    """Stand-in for TicketSource: records every mint, never shells out."""

    available = True

    def __init__(self, identities=("mu2edaq", "mu2eshift"), error=None):
        self._identities = list(identities)
        self.error = error
        self.minted = []

    def identities(self):
        return list(self._identities)

    def unavailable_reason(self):
        return "not installed"

    def ticket(self, identity, cache, timeout=120, **kwargs):
        self.minted.append(identity)
        if self.error is not None:
            raise self.error
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text("ticket")
        return ServiceTicket(identity=identity, cache=str(cache),
                             principal=f"{identity}/mu2e@FNAL.GOV")

    @staticmethod
    def default_principal():
        return AMBIENT

    @staticmethod
    def collection():
        return {}

    def restore_default(self, principal):
        return True


class Harness:
    """Everything a tool reaches for, scripted, with a record of what it did."""

    def __init__(self, tmp_path, allow=("mu2edaq",), tickets=None):
        self.tmp_path = tmp_path
        self.allow = set(allow)
        self.tickets = tickets or Tickets()
        self.ssh = []          # (login, KRB5CCNAME or None, remote command)
        self.kinit = []        # (principal, role)
        self.cleanups = 0
        self.managers = []
        self.raise_on_ssh = None

    # -- LocalTransport.run --------------------------------------------------

    def run(self, transport, command, timeout=None, user=None, input_text=None,
            check=False):
        argv = [str(a) for a in command]
        if argv[0] == "ssh":
            if self.raise_on_ssh is not None:
                raise self.raise_on_ssh
            target = next(a for a in argv[1:] if a.endswith(".fnal.gov")
                          or "@" in a)
            login = target.split("@")[0] if "@" in target else None
            cache = (transport.env or {}).get("KRB5CCNAME")
            self.ssh.append((login, cache, argv[-1]))
            if login in self.allow:
                return CommandResult(command=" ".join(argv), rc=0,
                                     stdout="Chassis Power is on\n")
            return CommandResult(command=" ".join(argv), rc=255,
                                 stderr="Permission denied (gssapi-with-mic).")
        if argv[0] == "kdestroy":
            return CommandResult(command=" ".join(argv), rc=0)
        raise AssertionError(f"unexpected local command {argv}")

    # -- KerberosManager ----------------------------------------------------

    def manager_class(self):
        harness = self

        class ScriptedManager(KerberosManager):
            def __init__(self, settings, local=None, cache_dir=None):
                super().__init__(settings, local=local,
                                 cache_dir=harness.tmp_path / "caches")
                self.tickets = harness.tickets
                harness.managers.append(self)

            def _klist(self, cache=None):
                if cache is None:
                    return TicketInfo(principal=AMBIENT, cache="API:ambient",
                                      valid=True)
                path = Path(str(cache))
                return TicketInfo(principal=path.name, cache=str(cache),
                                  valid=path.exists())

            def _kinit(self, principal, cache, role):
                harness.kinit.append((principal, role))
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text("ticket")

            def cleanup(self):
                harness.cleanups += 1
                super().cleanup()

        return ScriptedManager


@pytest.fixture
def harness(tmp_path, monkeypatch):
    h = Harness(tmp_path)
    monkeypatch.setattr(bootstrap_module, "KerberosManager", h.manager_class())
    monkeypatch.setattr(local_module.LocalTransport, "run",
                        lambda self, command, **kw: h.run(self, command, **kw))
    # The tools install the cli's SIGTERM handler; not in the test process.
    from mu2edaq_power_recovery.tools import ipmi_tool, ssh_probe
    monkeypatch.setattr(ipmi_tool, "install_sigterm_handler", lambda: None)
    monkeypatch.setattr(ssh_probe, "install_sigterm_handler", lambda: None)
    monkeypatch.setattr(
        ipmi_tool.VaultCredentials, "ipmi",
        lambda self: IPMICredentials(username="MU2E", password="not-a-secret",
                                     source="test"))
    for key in ("KRB5CCNAME",):
        monkeypatch.delenv(key, raising=False)
    return h


COMMON = ["--env-file", "/nonexistent", "-q"]


def svc(harness, identity):
    return f"FILE:{harness.tmp_path / 'caches' / f'krb5cc_svc-{identity}'}"


# ---------------------------------------------------------------------------
# mu2e-ssh-probe
# ---------------------------------------------------------------------------


def probe(*args):
    from mu2edaq_power_recovery.tools import ssh_probe
    return ssh_probe.main([*args, *COMMON])


def test_show_only_mints_nothing_and_says_so(harness, capsys):
    assert probe(GATEWAY, "--principal", "someone@FNAL.GOV") == 0
    out = capsys.readouterr().out
    assert harness.tickets.minted == []
    assert harness.kinit == []
    assert harness.ssh == [], "show-only must not open a session to a gateway"
    # The designated principal is not claimed to be in the ambient cache, and
    # the fallbacks are listed as candidates, not as acquired.
    assert "someone@FNAL.GOV" in out
    assert "ambient cache" not in out
    assert out.count("would try (not acquired)") == 3
    assert harness.cleanups == 1


def test_run_with_the_ambient_ticket_then_a_fallback(harness):
    assert probe(GATEWAY, "--run", "true") == 0
    # Warmed before the session: both fallbacks minted once, up front.
    assert harness.tickets.minted == ["mu2edaq", "mu2eshift"]
    # Operator first, under the ambient cache and ssh's own login; then the
    # first fallback, under its own cache and account.
    assert [(login, cache) for login, cache, _ in harness.ssh] == \
        [(None, None), ("mu2edaq", svc(harness, "mu2edaq"))]
    assert harness.cleanups == 1


def test_run_with_a_configured_principal_selects_its_private_cache(harness):
    harness.allow = {None}
    assert probe(GATEWAY, "--run", "true", "--principal", AMBIENT) == 0
    assert harness.kinit == [(AMBIENT, "general")]
    manager = harness.managers[0]
    private = f"FILE:{manager.cache_for(AMBIENT)}"
    assert harness.ssh[0][:2] == (None, private)


def test_run_as_root_keeps_the_root_login_and_changes_only_the_ticket(harness):
    harness.allow = {"nobody"}
    assert probe(GATEWAY, "--root", "--run", "true",
                 "--root-principal", "anorman/root@FNAL.GOV") == 1
    manager = harness.managers[0]
    assert [(login, cache) for login, cache, _ in harness.ssh] == [
        ("root", f"FILE:{manager.cache_for('anorman/root@FNAL.GOV')}"),
        ("root", svc(harness, "mu2edaq")),
        ("root", svc(harness, "mu2eshift")),
    ]


def test_no_chain_uses_the_ambient_ticket_alone(harness):
    assert probe(GATEWAY, "--run", "true", "--no-chain") == 1
    assert harness.managers == []
    assert harness.tickets.minted == []
    assert [(login, cache) for login, cache, _ in harness.ssh] == [(None, None)]


def test_a_guard_failure_is_reported_and_leaves_the_operator(harness, capsys):
    harness.tickets.error = DefaultCacheDisplaced(AMBIENT, "mu2edaq/mu2e@FNAL.GOV")
    assert probe(GATEWAY, "--run", "true") == 1
    assert harness.tickets.minted == ["mu2edaq"]
    assert [(login, cache) for login, cache, _ in harness.ssh] == [(None, None)]
    assert "service identities disabled" in capsys.readouterr().out


def test_probe_cleans_up_after_an_exception(harness):
    harness.raise_on_ssh = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        probe(GATEWAY, "--run", "true")
    assert harness.cleanups == 1


def test_probe_cleans_up_after_an_interrupt(harness):
    harness.raise_on_ssh = KeyboardInterrupt()
    assert probe(GATEWAY, "--run", "true") == 3
    assert harness.cleanups == 1


# ---------------------------------------------------------------------------
# mu2e-ipmi-tool
# ---------------------------------------------------------------------------


def ipmi(*args):
    from mu2edaq_power_recovery.tools import ipmi_tool
    return ipmi_tool.main(["-n", "mu2e-trk-01", "--gateway", GATEWAY,
                           *COMMON, *args, "chassis", "power", "status"])


def test_ipmi_tool_leads_with_the_operator_then_the_fallbacks(harness):
    assert ipmi() == 0
    pairs = [(login, cache) for login, cache, _ in harness.ssh]
    # The first gateway command is the reachability ping (ipmi.
    # reachability_precheck): it walks the chain, operator first; ipmitool
    # then goes out on the pair that worked.
    assert pairs == [(None, None), ("mu2edaq", svc(harness, "mu2edaq")),
                     ("mu2edaq", svc(harness, "mu2edaq"))]
    remotes = [remote for _, _, remote in harness.ssh]
    assert all(r.startswith("ping ") for r in remotes[:2])
    assert "ipmitool" in remotes[2]
    assert harness.cleanups == 1


def test_ipmi_tool_uses_a_designated_principal(harness):
    harness.allow = {None}
    assert ipmi("--principal", AMBIENT) == 0
    manager = harness.managers[0]
    assert harness.kinit == [(AMBIENT, "general")]
    # The reachability ping, then ipmitool, both on the designated ticket.
    assert [(login, cache) for login, cache, _ in harness.ssh] == \
        [(None, f"FILE:{manager.cache_for(AMBIENT)}")] * 2
    # The chain is built when the gateway transport is, so the fallbacks are
    # minted then (once each, under the lock) even though none was needed.
    assert harness.tickets.minted == ["mu2edaq", "mu2eshift"]


def test_ipmi_show_command_acquires_nothing(harness):
    assert ipmi("--show-command", "--principal", AMBIENT) == 0
    assert harness.kinit == []
    assert harness.tickets.minted == []
    assert harness.ssh == []
    assert harness.cleanups == 1


def test_ipmi_tool_cleans_up_after_an_exception(harness):
    harness.raise_on_ssh = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        ipmi()
    assert harness.cleanups == 1


def test_ipmi_tool_cleans_up_after_an_interrupt(harness):
    harness.raise_on_ssh = KeyboardInterrupt()
    assert ipmi() == 3
    assert harness.cleanups == 1


# ---------------------------------------------------------------------------
# the bootstrap itself, and the orchestrator's use of it
# ---------------------------------------------------------------------------


def test_cleanup_runs_when_prepare_fails(harness, settings, topology, monkeypatch):
    from mu2edaq_power_recovery.creds import KerberosError

    def refuse(self):
        raise KerberosError("no ticket")

    monkeypatch.setattr(KerberosManager, "prepare", refuse)
    with pytest.raises(KerberosError):
        with bootstrap_module.credential_session(settings, topology):
            pass
    assert harness.cleanups == 1


def test_warm_is_skipped_with_service_keytabs_off(harness, settings, topology):
    settings.set("kerberos.use_service_keytabs", False)
    with bootstrap_module.credential_session(settings, topology) as session:
        assert session.warmed == []
    assert harness.tickets.minted == []


def test_the_orchestrator_surfaces_a_disabled_chain(harness, settings,
                                                    monkeypatch):
    from mu2edaq_power_recovery.creds import VaultError
    from mu2edaq_power_recovery.orchestrator import Orchestrator

    monkeypatch.setattr(
        "mu2edaq_power_recovery.orchestrator.VaultCredentials.ipmi",
        lambda self: (_ for _ in ()).throw(VaultError("no vault in tests")))
    harness.tickets.error = DefaultCacheDisplaced(AMBIENT, "mu2edaq/mu2e@FNAL.GOV",
                                                  "worded any way at all")
    orch = Orchestrator(settings)
    try:
        info = orch.prepare_credentials()
        assert info["fallbacks_disabled"]["kind"] == "displaced"
        assert any("service identities disabled" in n for n in orch.notes)
        orch.store.start_run(label="t", dry_run=True, version={}, settings={})
        phase_notes = []
        orch.surface_credential_failure(phase_notes)
        orch.surface_credential_failure(phase_notes)
        assert len(phase_notes) == 1
        events = [e for e in orch.store.get_events()
                  if "service identities disabled" in e["message"]]
        assert len(events) == 1 and events[0]["level"] == "error"
    finally:
        orch.close()
    assert harness.cleanups == 1
