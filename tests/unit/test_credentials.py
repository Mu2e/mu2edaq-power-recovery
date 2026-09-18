"""Credential chains: trying each identity until one can log in.

No single identity can log in to every DAQ node, so a transport is given an
ordered chain -- the operator's principal, then the Mu2e service identities
whose tickets mu2edaq-kerberos mints from Vault keytabs. What these tests pin
down is when the chain advances, when it stops, and that a chosen credential
actually reaches ssh.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mu2edaq_power_recovery.creds.kerberos import Credential, KerberosManager
from mu2edaq_power_recovery.creds.ticketsource import (ServiceTicket, TicketSource,
                                                       TicketSourceError)
from mu2edaq_power_recovery.transport.base import CommandResult
from mu2edaq_power_recovery.transport.ssh import (SSHError, SSHFactory,
                                                  SSHTransport,
                                                  classify_ssh_failure)


# ---------------------------------------------------------------------------
# failure classification -- this is what decides whether to keep trying
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stderr,expected", [
    ("Permission denied (gssapi-with-mic,publickey).", "auth"),
    ("No Kerberos credentials available", "auth"),
    ("user does not exist", "auth"),
    ("ssh: connect to host h port 22: Connection refused", "unreachable"),
    ("ssh: connect to host h port 22: Operation timed out", "unreachable"),
    ("ssh: Could not resolve hostname h", "unreachable"),
    # sshd dropping us during key exchange is MaxStartups or a rate limiter,
    # not a credential problem: sending six more identities at a server that is
    # already refusing connections makes it worse.
    ("kex_exchange_identification: read: Connection reset by peer", "unreachable"),
    ("Too many authentication failures", "unreachable"),
])
def test_classification(stderr, expected):
    assert classify_ssh_failure(stderr) == expected


def test_unreachability_wins_over_a_credentials_message():
    # A dead jump host produces both: reading it as an auth problem would send
    # the chain through every identity against a machine that is simply off.
    mixed = ("ssh_exchange_identification: Connection closed by remote host\n"
             "No Kerberos credentials available")
    assert classify_ssh_failure(mixed) == "unreachable"


# ---------------------------------------------------------------------------
# the chain
# ---------------------------------------------------------------------------


class ScriptedLocal:
    """A local transport that answers by ssh login name.

    *allow* is the set of logins that may log in; everything else is refused
    the way sshd refuses it. Records every attempt so the order can be checked.
    """

    def __init__(self, allow, failure="Permission denied (gssapi-with-mic)."):
        self.allow = set(allow)
        self.failure = failure
        self.attempts = []

    def run(self, argv, timeout=None, input_text=None, **kwargs):
        target = next(a for a in argv if "@" in a or a.startswith("mu2e"))
        login = target.split("@")[0] if "@" in target else None
        self.attempts.append(login)
        if login in self.allow:
            return CommandResult(command=" ".join(argv), rc=0, stdout=login or "me")
        return CommandResult(command=" ".join(argv), rc=255, stderr=self.failure)


def credential(name, login=None, cache=None):
    return Credential(name=name, login=login or name,
                      cache=Path(cache) if cache else None, principal=f"{name}@FNAL.GOV")


CHAIN = [credential("general", "anorman"), credential("mu2edaq"),
         credential("mu2eshift")]


def test_the_first_credential_that_works_is_used():
    local = ScriptedLocal(allow={"anorman"})
    t = SSHTransport("node", credentials=CHAIN, local=local)
    assert t.run(["id", "-un"]).ok
    assert local.attempts == ["anorman"]          # nothing else was tried


def test_the_chain_advances_past_a_refusal():
    local = ScriptedLocal(allow={"mu2eshift"})
    t = SSHTransport("node", credentials=CHAIN, local=local)
    result = t.run(["id", "-un"])
    assert result.ok
    assert local.attempts == ["anorman", "mu2edaq", "mu2eshift"]
    assert result.meta["credential"] == "mu2eshift"


def test_the_working_credential_is_remembered_for_later_commands():
    # Twenty checks per node must not each walk the chain again.
    local = ScriptedLocal(allow={"mu2eshift"})
    t = SSHTransport("node", credentials=CHAIN, local=local)
    t.run(["id", "-un"])
    local.attempts.clear()
    t.run(["uptime"])
    assert local.attempts == ["mu2eshift"]


def test_exhausting_the_chain_raises_and_names_what_was_tried():
    local = ScriptedLocal(allow=set())
    t = SSHTransport("node", credentials=CHAIN, local=local)
    with pytest.raises(SSHError) as excinfo:
        t.run(["id", "-un"])
    assert local.attempts == ["anorman", "mu2edaq", "mu2eshift"]
    message = str(excinfo.value)
    for name in ("general", "mu2edaq", "mu2eshift"):
        assert name in message


def test_an_unreachable_host_stops_the_chain_immediately():
    # The whole point of classifying the failure: six more identities against a
    # dead machine would cost six more connect timeouts and learn nothing.
    local = ScriptedLocal(allow=set(),
                          failure="ssh: connect to host node port 22: "
                                  "Connection refused")
    t = SSHTransport("node", credentials=CHAIN, local=local)
    with pytest.raises(SSHError):
        t.run(["id", "-un"])
    assert local.attempts == ["anorman"]


def test_an_explicit_user_pins_the_login_and_disables_the_chain():
    # A caller naming a user means that user, not "whoever can get in".
    local = ScriptedLocal(allow={"mu2eshift"})
    t = SSHTransport("node", credentials=CHAIN, local=local)
    with pytest.raises(SSHError):
        t.run(["id", "-un"], user="root")
    assert local.attempts == ["root"]


def test_refusals_are_recorded_for_the_report():
    local = ScriptedLocal(allow={"mu2eshift"})
    t = SSHTransport("node", credentials=CHAIN, local=local)
    t.run(["id", "-un"])
    rejected = [a["credential"] for a in t.attempts]
    assert rejected == ["general", "mu2edaq"]
    assert all(a["reason"] == "auth" for a in t.attempts)


def test_the_success_callback_fires_once(monkeypatch):
    seen = []
    local = ScriptedLocal(allow={"mu2edaq"})
    t = SSHTransport("node", credentials=CHAIN, local=local,
                     on_success=seen.append)
    t.run(["id", "-un"])
    t.run(["uptime"])
    assert [c.name for c in seen] == ["mu2edaq"]


# ---------------------------------------------------------------------------
# the credential cache actually reaches ssh
# ---------------------------------------------------------------------------


def test_the_credential_cache_is_put_into_the_ssh_environment(tmp_path):
    # The bug this covers: a ticket minted into a private cache that ssh never
    # looks at, so a designated principal silently has no effect.
    cache = tmp_path / "krb5cc_mu2edaq"
    cred = credential("mu2edaq", cache=str(cache))
    t = SSHTransport("node", credentials=[cred])
    runner = t._runner(cred)
    assert runner.env["KRB5CCNAME"] == f"FILE:{cache}"


def test_a_credential_without_a_cache_uses_the_ambient_one():
    t = SSHTransport("node")
    assert t._runner(None) is t.local


def test_the_login_comes_from_the_credential():
    cred = credential("mu2eshift")
    t = SSHTransport("node.fnal.gov", credentials=[cred])
    assert "mu2eshift@node.fnal.gov" in t.argv(["true"], credential=cred)


# ---------------------------------------------------------------------------
# KerberosManager chain construction
# ---------------------------------------------------------------------------


class FakeTicketSource:
    def __init__(self, identities, working=None, fail=()):
        self._identities = identities
        self._working = set(working if working is not None else identities)
        self._fail = set(fail)
        self.available = True
        self.minted = []

    def identities(self):
        return list(self._identities)

    def ticket(self, identity, cache, timeout=120):
        self.minted.append(identity)
        if identity in self._fail:
            raise TicketSourceError(f"no keytab for {identity}")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text("x")
        return ServiceTicket(identity=identity, cache=cache,
                             principal=f"{identity}/mu2e@FNAL.GOV")

    def unavailable_reason(self):
        return "not installed"

    @staticmethod
    def default_principal():
        # A stable ambient principal, so operator_login() resolves without
        # shelling out to klist.
        return "anorman@FNAL.GOV"


@pytest.fixture
def manager(settings, tmp_path):
    m = KerberosManager(settings, cache_dir=tmp_path / "caches")
    m.tickets = FakeTicketSource(
        ["mu2e-controlroom", "mu2e-teststand", "mu2edaq", "mu2edcs",
         "mu2edqm", "mu2eraw", "mu2eshift"])
    return m


def test_preferred_identities_come_first_then_the_rest(manager):
    identities = manager.available_identities()
    assert identities[:2] == ["mu2edaq", "mu2eshift"]
    # ...and every other identity is still available to cycle through.
    assert set(identities) == {"mu2edaq", "mu2eshift", "mu2edcs", "mu2edqm",
                               "mu2eraw", "mu2e-controlroom", "mu2e-teststand"}


def test_the_chain_starts_with_the_operator(manager):
    chain = manager.chain()
    assert chain[0].name == "general"
    assert [c.name for c in chain[1:3]] == ["mu2edaq", "mu2eshift"]


def test_root_tries_the_personal_principal_first_then_falls_back(manager):
    """The operator's own principal leads the root chain, and root does fall back.

    The fallbacks keep the root login and change only the ticket:
    authenticating as mu2edaq and logging in to the root account is something
    a node's root/.k5login can authorise, which is why root has fallbacks.
    """
    chain = manager.chain(root=True)
    assert chain[0].name == "root" and chain[0].primary
    assert [c.name for c in chain[1:3]] == ["mu2edaq", "mu2eshift"]
    # Same login throughout; only the credential cache differs.
    assert {c.login for c in chain} == {"root"}


def test_root_fallback_can_be_turned_off(manager):
    manager.settings.set("kerberos.root_fallback", False)
    assert [c.name for c in manager.chain(root=True)] == ["root"]


def test_an_unusable_identity_is_skipped_and_not_retried(manager):
    manager.tickets = FakeTicketSource(["mu2edaq", "mu2eshift"],
                                       fail=["mu2edaq"])
    names = [c.name for c in manager.chain()]
    assert "mu2edaq" not in names and "mu2eshift" in names
    manager.chain()          # second pass: the failure is cached
    assert manager.tickets.minted.count("mu2edaq") == 1


def test_a_ticket_is_minted_once_per_identity(manager):
    manager.chain()
    manager.chain()
    assert manager.tickets.minted.count("mu2edaq") == 1


def test_successful_identities_are_promoted(manager):
    # On a cluster, the identity that opened node 1 very probably opens node 2.
    manager.note_success(Credential(name="mu2eraw", login="mu2eraw"))
    assert manager.order_chain(["mu2edaq", "mu2eshift", "mu2eraw"])[0] == "mu2eraw"


def test_the_operator_roles_are_not_promoted(manager):
    manager.note_success(Credential(name="general", login="anorman"))
    assert manager._successful == []


def test_service_keytabs_can_be_turned_off(manager):
    manager.settings.set("kerberos.use_service_keytabs", False)
    assert manager.available_identities() == []
    assert [c.name for c in manager.chain()] == ["general"]


def test_discovery_can_be_turned_off_leaving_the_configured_two(manager):
    manager.settings.set("kerberos.discover_identities", False)
    assert manager.available_identities() == ["mu2edaq", "mu2eshift"]


def test_no_kerberos_package_means_no_service_identities(settings, tmp_path):
    m = KerberosManager(settings, cache_dir=tmp_path / "caches")
    m.tickets.resolve = lambda command: None
    m.tickets._available = False
    assert m.available_identities() == ["mu2edaq", "mu2eshift"]   # configured
    assert [c.name for c in m.chain()] == ["general"]             # but unusable


# ---------------------------------------------------------------------------
# factory integration
# ---------------------------------------------------------------------------


def test_the_personal_principal_always_leads_the_chain(settings, topology, manager):
    for root in (False, True):
        chain = manager.chain(root=root)
        assert chain[0].primary, f"root={root}: personal ticket must be first"
        assert not any(c.primary for c in chain[1:])


def test_restore_primary_returns_the_personal_credential(manager):
    assert manager.restore_primary().primary


def test_the_factory_passes_the_chain_to_the_transport(settings, topology, manager):
    factory = SSHFactory(settings, topology, kerberos=manager)
    transport = factory.for_host("mu2e-trk-01.fnal.gov", direct=True)
    assert [c.name for c in transport.credentials][:3] == \
        ["general", "mu2edaq", "mu2eshift"]


def test_the_memo_promotes_a_fallback_but_never_past_the_personal_ticket(
        settings, topology, manager):
    """A host that needed a service identity still gets the personal one first.

    The run belongs to the operator's principal. One node having needed
    mu2eshift is no reason to stop offering the personal ticket everywhere --
    including on that node, the next time a transport for it is built.
    """
    factory = SSHFactory(settings, topology, kerberos=manager)
    factory._note_success("mu2e-trk-01.fnal.gov",
                          Credential(name="mu2eshift", login="mu2eshift"))
    chain = factory.credentials_for("mu2e-trk-01.fnal.gov")

    assert chain[0].primary, "the personal ticket must stay first"
    assert chain[1].name == "mu2eshift", "the known-good fallback comes next"
    assert [c.name for c in chain].count("mu2eshift") == 1


def test_a_successful_service_identity_does_not_lead_other_hosts_chains(manager):
    # Promotion orders the fallbacks among themselves, never ahead of the
    # personal principal.
    manager.note_success(Credential(name="mu2eraw", login="mu2eraw"))
    chain = manager.chain()
    assert chain[0].primary
    assert chain[1].name == "mu2eraw"


def test_root_transports_get_the_root_chain(settings, topology, manager,
                                            monkeypatch):
    factory = SSHFactory(settings, topology, kerberos=manager)
    # Resolving a gateway probes it for real; the chain is what is under test.
    monkeypatch.setattr(factory, "gateway_for", lambda location: "gw.fnal.gov")
    node = topology.node("mu2e-trk-01")
    transport = factory.for_node(node, root=True)
    names = [c.name for c in transport.credentials]
    assert names[0] == "root" and transport.credentials[0].primary
    assert "mu2edaq" in names        # root falls back too
    assert all(c.login == "root" for c in transport.credentials)


def test_without_a_kerberos_manager_there_is_no_chain(settings, topology):
    factory = SSHFactory(settings, topology)
    assert factory.credentials_for("any-host") == []


# ---------------------------------------------------------------------------
# the operator's default credential cache must never be touched
# ---------------------------------------------------------------------------


def test_the_login_is_derived_from_the_principal_not_left_to_ssh(manager):
    """ssh_config legitimately sets `User mu2edaq` for the DAQ hosts.

    Leaving the login unset therefore authenticated the *personal* ticket into
    the *service* account, which is refused -- and looks like a bad ticket.
    """
    assert manager.operator_login() == "anorman"          # from anorman@FNAL.GOV
    assert manager.chain()[0].login == "anorman"


def test_an_explicit_ssh_user_still_wins(manager):
    manager.settings.set("ssh.user", "someoneelse")
    assert manager.operator_login() == "someoneelse"


def test_a_root_instance_principal_reduces_to_the_account(manager):
    manager.settings.set("kerberos.principal", "anorman/root@FNAL.GOV")
    assert manager.operator_login() == "anorman"


def test_the_ticket_cache_is_requested_with_an_explicit_FILE_type(settings, tmp_path,
                                                                  monkeypatch):
    """macOS ships Heimdal, whose default cache type is API:.

    A bare path in KRB5CCNAME is not read as a file cache there, so the ticket
    lands in the operator's *default* cache and destroys their credentials --
    which is exactly what happened, seven times in a row.
    """
    from mu2edaq_power_recovery.creds import ticketsource as ts_module

    seen = {}

    def fake_run(self, command, args, timeout=120, env=None):
        seen["args"] = list(args)
        seen["env"] = env or {}
        cache = Path(str(args[args.index("--cache") + 1]).replace("FILE:", ""))
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text("ticket")
        return type("R", (), {"returncode": 0, "stdout": b"", "stderr": b""})()

    source = ts_module.TicketSource(settings)
    monkeypatch.setattr(ts_module.TicketSource, "_run", fake_run)
    monkeypatch.setattr(ts_module.TicketSource, "resolve",
                        lambda self, command: Path("/bin/true"))
    monkeypatch.setattr(ts_module.TicketSource, "default_principal",
                        staticmethod(lambda: "anorman@FNAL.GOV"))
    monkeypatch.setattr(ts_module.TicketSource, "_principal_of",
                        staticmethod(lambda cache: "mu2edaq/mu2e@FNAL.GOV"))

    cache = tmp_path / "krb5cc_svc-mu2edaq"
    source.ticket("mu2edaq", cache)

    assert f"FILE:{cache}" in seen["args"], "--cache must carry an explicit type"
    assert seen["env"].get("KRB5CCNAME") == f"FILE:{cache}", \
        "KRB5CCNAME must also be set, for any path that ignores --cache"


def test_a_clobbered_default_cache_is_refused_loudly(settings, tmp_path, monkeypatch):
    from mu2edaq_power_recovery.creds import ticketsource as ts_module

    principals = iter(["anorman@FNAL.GOV", "mu2eraw/mu2e@FNAL.GOV"])

    def fake_run(self, command, args, timeout=120, env=None):
        return type("R", (), {"returncode": 0, "stdout": b"", "stderr": b""})()

    source = ts_module.TicketSource(settings)
    monkeypatch.setattr(ts_module.TicketSource, "_run", fake_run)
    monkeypatch.setattr(ts_module.TicketSource, "resolve",
                        lambda self, command: Path("/bin/true"))
    monkeypatch.setattr(ts_module.TicketSource, "default_principal",
                        staticmethod(lambda: next(principals)))

    with pytest.raises(TicketSourceError) as excinfo:
        source.ticket("mu2eraw", tmp_path / "cache")
    message = str(excinfo.value)
    assert "default credential cache" in message
    assert "kinit anorman@FNAL.GOV" in message, "must say how to recover"


def test_a_clobbered_cache_disables_service_identities_for_the_run(manager):
    class Clobbering:
        available = True
        def identities(self): return ["mu2edaq", "mu2eshift"]
        def unavailable_reason(self): return ""
        @staticmethod
        def default_principal(): return "anorman@FNAL.GOV"
        def ticket(self, identity, cache, timeout=120):
            raise TicketSourceError(
                "minting a ticket for x replaced the default credential cache")

    manager.tickets = Clobbering()
    assert manager.service_credential("mu2edaq") is None
    # ...and no further identity is attempted, because the damage is done.
    assert manager.settings.get("kerberos.use_service_keytabs") is False
    assert [c.name for c in manager.chain()] == ["general"]


# ---------------------------------------------------------------------------
# credential visibility -- what an operator needs to see when a login fails
# ---------------------------------------------------------------------------


def test_describe_names_the_login_the_ticket_and_the_cache():
    """The login/ticket pair is what decides a GSSAPI login, and an ssh
    "Permission denied (gssapi)" names neither."""
    c = Credential(name="mu2edaq", login="mu2edaq", cache=Path("/tmp/cc"),
                   principal="mu2edaq/mu2e@FNAL.GOV")
    described = c.describe()
    assert "mu2edaq" in described                  # the login
    assert "mu2edaq/mu2e@FNAL.GOV" in described    # the ticket
    assert "/tmp/cc" in described                  # where the ticket lives


def test_describe_marks_the_ambient_cache():
    c = Credential(name="general", login="anorman", principal="anorman@FNAL.GOV")
    assert "ambient cache" in c.describe()


def test_a_service_identity_in_the_default_cache_is_flagged(manager, monkeypatch):
    """The failure mode that cost an evening.

    On macOS the credential cache is a collection, and a service ticket minted
    into it can become the default -- after which every login runs as that
    identity and is refused, with nothing in the ssh error to say why.
    """
    monkeypatch.setattr(manager, "ambient_principal",
                        lambda: "mu2eraw/mu2edaq/mu2e.fnal.gov@FNAL.GOV")
    warning = manager.ambient_warning()
    assert warning and "SERVICE identity" in warning
    assert "kswitch" in warning, "must say how to fix it"


def test_a_personal_principal_in_the_default_cache_is_not_flagged(manager,
                                                                  monkeypatch):
    monkeypatch.setattr(manager, "ambient_principal", lambda: "anorman@FNAL.GOV")
    assert manager.ambient_warning() is None


def test_a_mismatch_with_the_configured_principal_is_flagged(manager, monkeypatch):
    monkeypatch.setattr(manager, "ambient_principal", lambda: "someoneelse@FNAL.GOV")
    manager.settings.set("kerberos.principal", "anorman@FNAL.GOV")
    warning = manager.ambient_warning()
    assert warning and "someoneelse@FNAL.GOV" in warning


def test_no_ambient_ticket_is_not_itself_a_warning(manager, monkeypatch):
    # "No ticket at all" is caught by ensure(), with its own message.
    monkeypatch.setattr(manager, "ambient_principal", lambda: None)
    assert manager.ambient_warning() is None


def test_refused_attempts_record_the_login_and_ticket(monkeypatch):
    """A refused chain must say which pairs were tried, not just that it failed."""
    local = ScriptedLocal(allow=set())
    chain = [credential("general", "anorman"), credential("mu2edaq")]
    t = SSHTransport("gw", credentials=chain, local=local)
    with pytest.raises(SSHError):
        t.run(["true"])
    assert [a["login"] for a in t.attempts] == ["anorman", "mu2edaq"]
    assert all(a["described"] for a in t.attempts), \
        "each attempt must carry a human-readable login/ticket description"
