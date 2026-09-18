"""Credential chains: trying each identity until one can log in.

No single identity can log in to every DAQ node, so a transport is given an
ordered chain -- the operator's principal, then the Mu2e service identities
whose tickets mu2edaq-kerberos mints from Vault keytabs. What these tests pin
down is when the chain advances, when it stops, and that a chosen credential
actually reaches ssh.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mu2edaq_power_recovery.creds.kerberos import (Credential, KerberosManager,
                                                   TicketInfo)
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

    @staticmethod
    def collection():
        return {}

    def restore_default(self, principal):
        return True


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


def test_the_login_is_left_to_ssh_by_default(manager):
    """The account is NOT derived from the principal.

    Verified against the live cluster: with a valid anorman@FNAL.GOV ticket,
    logging in to mu2egateway01 as `mu2edaq` succeeds (that account's .k5login
    authorises the personal principal) and as `root` succeeds, while `anorman`
    is refused -- there is no such account. Deriving the login from the
    principal broke every host whose account name is not the principal's first
    component, which is all of them here.
    """
    assert manager.operator_login() is None
    assert manager.chain()[0].login is None, "ssh_config must decide the account"


def test_an_explicit_ssh_user_wins(manager):
    manager.settings.set("ssh.user", "mu2edaq")
    assert manager.operator_login() == "mu2edaq"
    assert manager.chain()[0].login == "mu2edaq"


def test_the_root_login_is_still_explicit(manager):
    # root is named outright, not inferred: ssh_config's User would otherwise
    # send a root session to the service account.
    assert manager.chain(root=True)[0].login == "root"


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


# ---------------------------------------------------------------------------
# macOS: caches live in an API: collection, not in files
# ---------------------------------------------------------------------------


def test_ccache_name_passes_through_a_collection_name():
    from mu2edaq_power_recovery.creds.kerberos import ccache_name
    # A ticket minted on macOS has a ccache NAME, not a path; assuming FILE:
    # would point ssh at something that does not exist.
    assert ccache_name("API:ABC-123") == "API:ABC-123"
    assert ccache_name("KCM:1000") == "KCM:1000"


def test_ccache_name_adds_the_file_type_to_a_bare_path():
    from mu2edaq_power_recovery.creds.kerberos import ccache_name
    # And a bare path must never go to Heimdal unqualified -- that is what
    # sent seven service tickets into the operator's default cache.
    assert ccache_name("/tmp/krb5cc_x") == "FILE:/tmp/krb5cc_x"


def test_a_collection_credential_selects_itself_by_name():
    c = Credential(name="mu2edaq", login="mu2edaq", cache="API:ABC-123",
                   principal="mu2edaq/mu2e@FNAL.GOV")
    assert c.environ() == {"KRB5CCNAME": "API:ABC-123"}
    assert "API:ABC-123" in c.describe()


def test_the_default_cache_pointer_is_restored_after_a_mint(settings, tmp_path,
                                                            monkeypatch):
    """Heimdal makes a freshly minted cache the collection default.

    The operator's ticket is not destroyed, only displaced, so the fix is to
    put the pointer back rather than to abandon service identities.
    """
    from mu2edaq_power_recovery.creds import ticketsource as ts_module

    state = {"default": "anorman@FNAL.GOV", "restored": False}

    def fake_run(self, command, args, timeout=120, env=None):
        state["default"] = "mu2edaq/mu2e@FNAL.GOV"     # the mint moves it
        return type("R", (), {"returncode": 0, "stdout": b"", "stderr": b""})()

    def fake_restore(self, principal):
        state["default"] = principal
        state["restored"] = True
        return True

    source = ts_module.TicketSource(settings)
    monkeypatch.setattr(ts_module.TicketSource, "_run", fake_run)
    monkeypatch.setattr(ts_module.TicketSource, "resolve",
                        lambda self, c: Path("/bin/true"))
    monkeypatch.setattr(ts_module.TicketSource, "default_principal",
                        staticmethod(lambda: state["default"]))
    monkeypatch.setattr(ts_module.TicketSource, "restore_default", fake_restore)
    monkeypatch.setattr(ts_module.TicketSource, "collection",
                        staticmethod(lambda: {"mu2edaq/mu2e@FNAL.GOV": "API:XYZ"}))

    ticket = source.ticket("mu2edaq", tmp_path / "nonexistent")
    assert state["restored"], "the default pointer must be put back"
    assert state["default"] == "anorman@FNAL.GOV"
    # ...and the ticket is still usable, by its collection name.
    assert ticket.cache == "API:XYZ"


def test_an_unrestorable_default_aborts_rather_than_continuing(settings, tmp_path,
                                                               monkeypatch):
    from mu2edaq_power_recovery.creds import ticketsource as ts_module
    principals = iter(["anorman@FNAL.GOV", "mu2eraw/mu2e@FNAL.GOV"])

    source = ts_module.TicketSource(settings)
    monkeypatch.setattr(ts_module.TicketSource, "_run",
                        lambda self, c, a, timeout=120, env=None:
                        type("R", (), {"returncode": 0, "stdout": b"", "stderr": b""})())
    monkeypatch.setattr(ts_module.TicketSource, "resolve",
                        lambda self, c: Path("/bin/true"))
    monkeypatch.setattr(ts_module.TicketSource, "default_principal",
                        staticmethod(lambda: next(principals)))
    monkeypatch.setattr(ts_module.TicketSource, "restore_default",
                        lambda self, p: False)

    with pytest.raises(TicketSourceError) as excinfo:
        source.ticket("mu2eraw", tmp_path / "cache")
    assert "kswitch -p anorman@FNAL.GOV" in str(excinfo.value)


def test_one_unrecoverable_clobber_stops_the_whole_chain(manager):
    """Not just that identity -- the remaining six would do the same damage."""
    class Clobbering:
        available = True
        calls = []
        def identities(self): return ["mu2edaq", "mu2eshift", "mu2edcs"]
        def unavailable_reason(self): return ""
        @staticmethod
        def default_principal(): return "anorman@FNAL.GOV"
        def ticket(self, identity, cache, timeout=120):
            Clobbering.calls.append(identity)
            raise TicketSourceError(
                "repointed the default credential cache and it could not be "
                "restored")

    manager.tickets = Clobbering()
    # The first failure disables service identities for the run...
    manager.service_credential("mu2edaq")
    manager.settings.set("kerberos.use_service_keytabs", False)
    chain = manager.chain()
    assert [c.name for c in chain] == ["general"]


def test_the_real_tool_error_is_surfaced_not_the_usage_hint():
    from mu2edaq_power_recovery.creds.ticketsource import summarise_tool_error
    # get-kerberos-ticket appends a "Known identities:" listing after a
    # failure, so the last line of its output is an indented org name and the
    # actual cause is buried above it.
    output = ("RuntimeError: The 'hvac' package is required.\n"
              "Failed to fetch identity 'mu2edaq' from Vault.\n"
              "  Known identities:\n    mu2e: \n    nova: ")
    assert "hvac" in summarise_tool_error(output)
    assert "nova" not in summarise_tool_error(output)


# ---------------------------------------------------------------------------
# the other places a ticket is minted, or destroyed
# ---------------------------------------------------------------------------


def test_a_timed_out_mint_still_restores_the_default_pointer(settings, tmp_path,
                                                             monkeypatch):
    """get-kerberos-ticket ran, so it may have moved the pointer and then hung.

    Returning a bare timeout leaves the operator's ticket displaced and lets
    the chain work through the next six identities under the wrong identity --
    which is the shape of the original incident.
    """
    from mu2edaq_power_recovery.creds import ticketsource as ts_module

    state = {"default": "anorman@FNAL.GOV", "restored": False}

    def hang(self, command, args, timeout=120, env=None):
        state["default"] = "mu2edaq/mu2e@FNAL.GOV"      # moved, then hangs
        raise subprocess.TimeoutExpired(cmd="get-kerberos-ticket", timeout=timeout)

    def fake_restore(self, principal):
        state["default"] = principal
        state["restored"] = True
        return True

    source = ts_module.TicketSource(settings)
    monkeypatch.setattr(ts_module.TicketSource, "_run", hang)
    monkeypatch.setattr(ts_module.TicketSource, "resolve",
                        lambda self, c: Path("/bin/true"))
    monkeypatch.setattr(ts_module.TicketSource, "default_principal",
                        staticmethod(lambda: state["default"]))
    monkeypatch.setattr(ts_module.TicketSource, "restore_default", fake_restore)

    with pytest.raises(TicketSourceError, match="timed out"):
        source.ticket("mu2edaq", tmp_path / "cache")
    assert state["restored"], "the pointer must be put back even on a timeout"
    assert state["default"] == "anorman@FNAL.GOV"


def test_kinit_names_the_cache_and_restores_a_displaced_default(manager, monkeypatch):
    """_kinit is the other place this project runs kinit.

    The service mint was hardened against Heimdal repointing the default; this
    one was still steered by KRB5CCNAME alone -- and it mints the designated,
    root-capable principal.
    """
    import getpass as getpass_module

    from mu2edaq_power_recovery.transport import local as local_module

    seen = {}
    state = {"default": "anorman@FNAL.GOV", "restored": None}

    def fake_run(self, command, timeout=None, user=None, input_text=None,
                 check=False):
        seen["argv"] = list(command)
        seen["env"] = dict(self.env or {})
        seen["stdin"] = input_text
        state["default"] = "mu2edaq/mu2e@FNAL.GOV"      # the mint moves it
        return CommandResult(command=" ".join(command), rc=0)

    def restore(principal):
        state["default"] = principal
        state["restored"] = principal
        return True

    monkeypatch.setattr(getpass_module, "getpass", lambda prompt="": "not-a-password")
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(local_module.LocalTransport, "run", fake_run)
    manager.tickets.default_principal = lambda: state["default"]
    manager.tickets.restore_default = restore

    cache = manager.cache_for("anorman@FNAL.GOV")
    manager._kinit("anorman@FNAL.GOV", cache, "general")

    assert seen["argv"] == ["kinit", "-c", f"FILE:{cache}", "anorman@FNAL.GOV"]
    assert seen["env"].get("KRB5CCNAME") == f"FILE:{cache}", \
        "KRB5CCNAME must also be set, for any kinit that ignores -c"
    assert state["restored"] == "anorman@FNAL.GOV"
    # The prompt path itself is covered by getting this far: reaching kinit
    # means getpass was importable, which it briefly was not.
    assert seen["stdin"] == "not-a-password"
    assert "not-a-password" not in " ".join(seen["argv"])
    assert "not-a-password" not in "".join(seen["env"].values())


def test_a_designated_principal_is_found_in_the_collection(manager, monkeypatch):
    """Heimdal's kinit does not honour a FILE: cache name.

    The ticket is perfectly good; it just has a ccache name rather than a
    path. Without this fallback --principal and --root-principal do not work
    at all on macOS, which is the platform the recovery is driven from.
    """
    monkeypatch.setattr(KerberosManager, "_kinit",
                        lambda self, principal, cache, role: None)

    def fake_klist(self, cache=None):
        found = str(cache) == "API:ABC123"
        return TicketInfo(principal="anorman@FNAL.GOV" if found else None,
                          cache=str(cache), valid=found)

    monkeypatch.setattr(KerberosManager, "_klist", fake_klist)
    manager.tickets.collection = lambda: {"anorman@FNAL.GOV": "API:ABC123"}

    info = manager.ensure("anorman@FNAL.GOV", role="general")
    assert info.valid
    assert manager._caches["general"] == "API:ABC123"
    # ...and that name is what reaches ssh, not FILE:API:ABC123.
    assert manager.environ_for("general") == {"KRB5CCNAME": "API:ABC123"}


def test_cleanup_names_each_cache_and_never_runs_a_bare_kdestroy(manager, monkeypatch):
    """A cache here may be a collection name, and FILE:API:<uuid> names nothing.

    Unnamed, kdestroy either destroyed nothing -- so every service ticket
    survived the run, sitting in the operator's collection where it can become
    the collection default and make the *next* run log in as a service
    identity -- or destroyed the default cache, according to whether
    KRB5CCNAME happened to be honoured.
    """
    from mu2edaq_power_recovery.transport import local as local_module

    calls = []

    def fake_run(self, command, timeout=None, user=None, input_text=None,
                 check=False):
        calls.append(list(command))
        return CommandResult(command=" ".join(command), rc=0)

    monkeypatch.setattr(local_module.LocalTransport, "run", fake_run)

    path_cache = manager.cache_for("anorman@FNAL.GOV")
    path_cache.parent.mkdir(parents=True, exist_ok=True)
    path_cache.write_text("stand-in for a credential cache")
    manager._caches["general"] = path_cache
    manager._caches["svc-mu2edaq"] = "API:8E3F"

    manager.cleanup()

    assert calls == [["kdestroy", "-c", f"FILE:{path_cache}"],
                     ["kdestroy", "-c", "API:8E3F"]]
    assert not path_cache.exists()


def test_cleanup_refuses_a_file_cache_outside_the_runs_own_directory(manager, tmp_path,
                                                                     monkeypatch):
    """Nothing puts a foreign path in _caches today.

    If something ever does, kdestroy must not be what discovers it: the
    plausible foreign path is the operator's own cache.
    """
    from mu2edaq_power_recovery.transport import local as local_module

    calls = []
    monkeypatch.setattr(local_module.LocalTransport, "run",
                        lambda self, command, **kwargs: calls.append(list(command)))

    foreign = tmp_path / "krb5cc_1000"
    foreign.write_text("the operator's own ticket")
    manager._caches["general"] = foreign

    manager.cleanup()
    assert calls == []
    assert foreign.exists()


def test_the_gateways_are_probed_once_even_if_every_worker_asks_at_once(
        settings, topology, monkeypatch):
    """Nodes are assessed a thread apiece, and each asks for its gateway first.

    On a cold cache that meant the whole worker pool probing the same two
    gateways simultaneously -- a TCP sweep plus a full handshake per credential
    in the chain, times sixteen, in the first second of a phase. Against a
    gateway that is refusing logins that is a burst of a couple of hundred
    connections, which is how a rate limiter starts refusing everything else
    too.
    """
    import concurrent.futures
    import time

    from mu2edaq_power_recovery.transport import ssh as ssh_module

    probes = []
    factory = SSHFactory(settings, topology)

    def slow_probe(self, location):
        probes.append(location)
        time.sleep(0.05)          # widen the window a real probe leaves open
        self._gateway_cache[location] = "mu2egateway01.fnal.gov"
        return self._gateway_cache[location]

    monkeypatch.setattr(ssh_module.SSHFactory, "_select_gateway", slow_probe)
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        futures = [pool.submit(factory.gateway_for, "mc2") for _ in range(16)]
        answers = [f.result() for f in futures]

    assert probes == ["mc2"], f"the gateways were probed {len(probes)} times"
    assert set(answers) == {"mu2egateway01.fnal.gov"}
