"""Credential chains: trying each identity until one can log in.

No single identity can log in to every DAQ node, so a transport is given an
ordered chain -- the operator's principal, then the Mu2e service identities
whose tickets mu2edaq-kerberos mints from Vault keytabs. What these tests pin
down is when the chain advances, when it stops, and that a chosen credential
actually reaches ssh.
"""
from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from mu2edaq_power_recovery.creds.kerberos import (Credential, KerberosError,
                                                   KerberosManager,
                                                   TicketInfo)
from mu2edaq_power_recovery.creds.ticketsource import (DefaultCacheDisplaced,
                                                       DefaultCacheUnverifiable,
                                                       ServiceTicket, TicketSource,
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

    def ticket(self, identity, cache, timeout=120, **kwargs):
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
    # operator_credential() reads the ambient default; without this stub the
    # test ran the developer's real klist (caught by the #22 Popen guard).
    m.tickets.default_principal = lambda *args, **kwargs: None
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
    monkeypatch.setattr(ts_module.TicketSource, "default_principal_status",
                        staticmethod(lambda: ("anorman@FNAL.GOV", None)))
    monkeypatch.setattr(ts_module.TicketSource, "_principal_of",
                        staticmethod(lambda cache: "mu2edaq/mu2e@FNAL.GOV"))

    cache = tmp_path / "krb5cc_svc-mu2edaq"
    source.ticket("mu2edaq", cache)

    assert f"FILE:{cache}" in seen["args"], "--cache must carry an explicit type"
    assert seen["env"].get("KRB5CCNAME") == f"FILE:{cache}", \
        "KRB5CCNAME must also be set, for any path that ignores --cache"


class Clobbering:
    """A ticket source whose every mint raises *error* and records the call."""

    available = True

    def __init__(self, error, identities=("mu2edaq", "mu2eshift", "mu2edcs")):
        self.error = error
        self._identities = list(identities)
        self.minted = []

    def identities(self):
        return list(self._identities)

    def unavailable_reason(self):
        return ""

    @staticmethod
    def default_principal():
        return "anorman@FNAL.GOV"

    def ticket(self, identity, cache, timeout=120, **kwargs):
        self.minted.append(identity)
        raise self.error


@pytest.mark.parametrize("error", [
    DefaultCacheDisplaced("anorman@FNAL.GOV", "mu2edaq/mu2e@FNAL.GOV"),
    # The wording is for the operator; the type is the signal.
    DefaultCacheDisplaced("anorman@FNAL.GOV", "mu2edaq/mu2e@FNAL.GOV",
                          "something reworded entirely"),
    DefaultCacheUnverifiable("klist is not installed", "unrelated text"),
])
def test_a_guard_failure_disables_service_identities_for_the_run(manager, error):
    manager.tickets = Clobbering(error)
    assert manager.service_credential("mu2edaq") is None
    # ...and no further identity is attempted, because the damage is done.
    assert manager.settings.get("kerberos.use_service_keytabs") is False
    assert [c.name for c in manager.chain()] == ["general"]
    assert manager.service_credential("mu2eshift") is None
    assert manager.tickets.minted == ["mu2edaq"]
    state = manager.fallbacks_disabled
    assert state is not None and state.kind == error.kind
    assert state.identity == "mu2edaq"
    assert "kswitch" in state.note() or "kinit" in state.note()


def test_the_old_substring_no_longer_disables_anything(manager):
    """An ordinary mint failure that happens to mention the default cache.

    The decision used to be ``"default credential cache" in str(exc)``, so a
    reworded guard message silently downgraded to "skip this identity" -- and
    this one, an unrelated failure, would have stopped the whole chain.
    """
    manager.tickets = Clobbering(TicketSourceError(
        "get-kerberos-ticket failed for mu2edaq: could not write the default "
        "credential cache"))
    manager.chain()
    assert manager.fallbacks_disabled is None
    assert manager.settings.get("kerberos.use_service_keytabs") is not False
    # Only the identity that failed is dropped; the others are still tried.
    assert manager.tickets.minted == ["mu2edaq", "mu2eshift", "mu2edcs"]


def test_one_unrecoverable_clobber_stops_the_whole_chain(manager):
    """Not just that identity -- the remaining six would do the same damage."""
    manager.tickets = Clobbering(DefaultCacheDisplaced(
        "anorman@FNAL.GOV", "mu2edaq/mu2e@FNAL.GOV"))
    chain = manager.chain()
    assert [c.name for c in chain] == ["general"]
    assert manager.tickets.minted == ["mu2edaq"]


# ---------------------------------------------------------------------------
# concurrency: workers ask for chains simultaneously on a cold cache
# ---------------------------------------------------------------------------


class SlowTicketSource(FakeTicketSource):
    """Mints slowly and tracks how many mints overlap, to catch interleaving."""

    def __init__(self, identities):
        super().__init__(identities)
        self.active = 0
        self.max_active = 0
        self._count_lock = threading.Lock()

    def ticket(self, identity, cache, timeout=120, **kwargs):
        with self._count_lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.02)
            return super().ticket(identity, cache, timeout)
        finally:
            with self._count_lock:
                self.active -= 1


def test_concurrent_cold_chains_mint_each_identity_exactly_once(manager):
    identities = ["mu2edaq", "mu2eshift", "mu2edcs"]
    manager.tickets = SlowTicketSource(identities)
    workers = 12
    barrier = threading.Barrier(workers)
    chains, errors = [], []

    def worker():
        try:
            barrier.wait()
            chains.append(manager.chain())
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors
    assert sorted(manager.tickets.minted) == sorted(identities)
    # One read/mint/restore transaction at a time.
    assert manager.tickets.max_active == 1
    # Nobody saw a partially initialised credential: every chain is complete
    # and every credential carries its cache.
    assert len(chains) == workers
    for chain in chains:
        assert [c.name for c in chain] == ["general", *identities]
        assert all(c.cache for c in chain[1:])
    assert sorted(k for k in manager._caches if k.startswith("svc-")) == \
        sorted(f"svc-{i}" for i in identities)


def test_concurrent_guard_failure_mints_once_and_disables_once(manager):
    manager.tickets = Clobbering(DefaultCacheDisplaced("a@FNAL.GOV", "b@FNAL.GOV"))
    workers = 8
    barrier = threading.Barrier(workers)

    def worker():
        barrier.wait()
        manager.chain()

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert manager.tickets.minted == ["mu2edaq"]
    assert manager.fallbacks_disabled.identity == "mu2edaq"


def test_concurrent_note_success_keeps_one_entry_per_identity(manager):
    names = ["mu2edaq", "mu2eshift", "mu2edcs", "mu2eraw"]
    workers = 8
    barrier = threading.Barrier(workers)
    errors = []

    def worker(offset):
        try:
            barrier.wait()
            for i in range(400):
                manager.note_success(Credential(name=names[(i + offset) % 4]))
                manager.order_chain(names)
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors
    assert sorted(manager._successful) == sorted(names)


def test_warm_fallbacks_mints_every_identity_up_front(manager):
    usable = manager.warm_fallbacks()
    assert set(usable) == set(manager.tickets.minted)
    before = list(manager.tickets.minted)
    manager.chain()
    assert manager.tickets.minted == before, "chain() must reuse the warm tickets"


def test_warm_fallbacks_does_nothing_with_service_keytabs_off(manager):
    manager.settings.set("kerberos.use_service_keytabs", False)
    assert manager.warm_fallbacks() == []
    assert manager.tickets.minted == []


def test_no_mint_starts_after_cleanup(manager, monkeypatch):
    from mu2edaq_power_recovery.transport import local as local_module
    monkeypatch.setattr(local_module.LocalTransport, "run",
                        lambda self, command, **kwargs: CommandResult(
                            command=" ".join(command), rc=0))
    manager.cleanup()
    assert manager.service_credential("mu2edaq") is None
    assert manager.tickets.minted == []


def test_chain_without_minting_describes_candidates_and_mints_nothing(manager):
    candidates = manager.chain(mint=False, candidates=True)
    assert manager.tickets.minted == []
    assert candidates[0].primary
    assert all(c.pending for c in candidates[1:])
    assert "not acquired" in candidates[1].describe()
    # Without candidates only credentials that exist are offered.
    assert [c.name for c in manager.chain(mint=False)] == ["general"]


def test_an_unacquired_designated_principal_is_not_described_as_ambient(manager):
    manager.settings.set("kerberos.principal", "someone@FNAL.GOV")
    described = manager.operator_credential().describe()
    assert "ambient cache" not in described
    assert "not acquired" in described


def test_root_equal_to_general_uses_the_general_cache(manager, monkeypatch):
    manager.settings.set("kerberos.principal", "anorman@FNAL.GOV")
    manager.settings.set("kerberos.root_principal", "anorman@FNAL.GOV")

    def fake_ensure(principal, role="general"):
        manager._caches[role] = manager.cache_for(principal)
        return TicketInfo(principal=principal, cache=None, valid=True)

    monkeypatch.setattr(manager, "ensure", fake_ensure)
    manager.prepare()
    root = manager.operator_credential(root=True)
    assert root.environ() == manager.operator_credential().environ() != {}


# ---------------------------------------------------------------------------
# the default-cache guard's precondition (#23)
# ---------------------------------------------------------------------------


def _guarded_source(settings, monkeypatch, klist):
    """A TicketSource whose klist is *klist* and whose mint is recorded."""
    from mu2edaq_power_recovery.creds import ticketsource as ts_module

    minted = []

    def fake_run(self, command, args, timeout=120, env=None):
        minted.append(list(args))
        cache = Path(str(args[args.index("--cache") + 1]).replace("FILE:", ""))
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text("ticket")
        return type("R", (), {"returncode": 0, "stdout": b"", "stderr": b""})()

    def fake_subprocess_run(argv, **kwargs):
        if list(argv) != ["klist"]:
            raise AssertionError(f"unexpected command {argv}")
        return klist()

    monkeypatch.setattr(ts_module.TicketSource, "_run", fake_run)
    monkeypatch.setattr(ts_module.TicketSource, "resolve",
                        lambda self, c: Path("/bin/true"))
    monkeypatch.setattr(ts_module.TicketSource, "_principal_of",
                        staticmethod(lambda cache: "mu2edaq/mu2e@FNAL.GOV"))
    monkeypatch.setattr(ts_module.subprocess, "run", fake_subprocess_run)
    return ts_module.TicketSource(settings), minted


def _completed(rc, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess(["klist"], rc, stdout, stderr)


def _klist_missing():
    raise FileNotFoundError(2, "No such file or directory", "klist")


@pytest.mark.parametrize("klist,reason", [
    (_klist_missing, "not installed"),
    # Heimdal with an empty collection; MIT says much the same.
    (lambda: _completed(1, stderr=b"klist: No ticket file: /tmp/krb5cc_501\n"),
     "no default credential cache"),
    (lambda: _completed(0, stdout=b"Credentials cache: API:1234\n  Issued  "
                                  b"Expires  Principal\n"),
     "parse"),
])
def test_an_unreadable_default_refuses_before_any_mint(settings, tmp_path,
                                                      monkeypatch, klist, reason):
    source, minted = _guarded_source(settings, monkeypatch, klist)
    with pytest.raises(DefaultCacheUnverifiable) as excinfo:
        source.ticket("mu2edaq", tmp_path / "cache")
    assert minted == [], "no mint command may run when the guard cannot"
    assert reason in excinfo.value.reason


def test_a_readable_default_lets_the_mint_proceed(settings, tmp_path, monkeypatch):
    source, minted = _guarded_source(
        settings, monkeypatch,
        lambda: _completed(0, stdout=b"Ticket cache: FILE:/tmp/krb5cc_501\n"
                                     b"Default principal: anorman@FNAL.GOV\n"))
    ticket = source.ticket("mu2edaq", tmp_path / "cache")
    assert len(minted) == 1
    assert ticket.principal == "mu2edaq/mu2e@FNAL.GOV"


def test_default_principal_keeps_its_contract(monkeypatch):
    from mu2edaq_power_recovery.creds import ticketsource as ts_module
    monkeypatch.setattr(ts_module.subprocess, "run",
                        lambda argv, **kw: _completed(1, stderr=b"no cache\n"))
    assert ts_module.TicketSource.default_principal() is None
    principal, reason = ts_module.TicketSource.default_principal_status()
    assert principal is None and "no cache" in reason


def test_an_unverifiable_default_through_the_manager_keeps_the_operator(
        manager):
    manager.tickets = Clobbering(DefaultCacheUnverifiable("no default cache"))
    chain = manager.chain()
    assert [c.name for c in chain] == ["general"]
    assert "kinit" in manager.fallbacks_disabled.guidance


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
    monkeypatch.setattr(ts_module.TicketSource, "default_principal_status",
                        staticmethod(lambda: (state["default"], None)))
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
    monkeypatch.setattr(ts_module.TicketSource, "default_principal_status",
                        staticmethod(lambda: (next(principals), None)))
    monkeypatch.setattr(ts_module.TicketSource, "restore_default",
                        lambda self, p: False)

    with pytest.raises(DefaultCacheDisplaced) as excinfo:
        source.ticket("mu2eraw", tmp_path / "cache")
    assert "kswitch -p anorman@FNAL.GOV" in str(excinfo.value)
    assert (excinfo.value.before, excinfo.value.after) == \
        ("anorman@FNAL.GOV", "mu2eraw/mu2e@FNAL.GOV")


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
    monkeypatch.setattr(ts_module.TicketSource, "default_principal_status",
                        staticmethod(lambda: (state["default"], None)))
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


# -------------------------------------------------------------------------
# a changed host key is not a login failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stderr", [
    "@@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@@",
    "Host key verification failed.",
    "POSSIBLE DNS SPOOFING DETECTED!",
])
def test_host_key_failures_are_their_own_category(stderr):
    """Reimaged nodes are exactly what an outage recovery meets.

    ssh aborts on a changed host key before authenticating, so this is not a
    credential problem and must not be reported as one.
    """
    assert classify_ssh_failure(stderr) == "hostkey"


def test_a_host_key_failure_stops_the_chain_immediately():
    # No identity can get past a host key mismatch, and each further attempt
    # is another connection to a host already refusing.
    local = ScriptedLocal(
        allow=set(),
        failure="@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@")
    t = SSHTransport("node", credentials=CHAIN, local=local)
    with pytest.raises(SSHError):
        t.run(["id", "-un"])
    assert local.attempts == ["anorman"], "only the first credential is tried"


def test_the_login_check_says_the_host_key_changed(make_context, monkeypatch):
    from mu2edaq_power_recovery.checks import Status, run_check
    from mu2edaq_power_recovery.transport.base import TransportError

    ctx = make_context()

    def refuse(*args, **kwargs):
        raise TransportError("Host key verification failed.")

    transport = ctx.ssh
    monkeypatch.setattr(type(transport), "run", refuse)
    transport.attempts = [{"credential": "general", "reason": "hostkey",
                           "detail": "REMOTE HOST IDENTIFICATION HAS CHANGED",
                           "described": "login x ticket y"}]

    res = run_check("ssh.login", ctx)
    assert res.status is Status.FAIL
    assert "host key has CHANGED" in res.summary
    assert res.data.get("hostkey_changed") is True
    # It must say how to verify, not invite a blind known_hosts edit.
    assert "ssh-keyscan" in res.detail


# ---------------------------------------------------------------------------
# an interrupted mint (SIGTERM -> KeyboardInterrupt) -- M2
# ---------------------------------------------------------------------------


def test_an_interrupted_mint_still_restores_the_default(settings, tmp_path,
                                                        monkeypatch):
    from mu2edaq_power_recovery.creds import ticketsource as ts_module

    state = {"default": "anorman@FNAL.GOV", "restored": False}

    def interrupted(self, command, args, timeout=120, env=None):
        state["default"] = "mu2edaq/mu2e@FNAL.GOV"      # moved, then SIGTERM
        raise KeyboardInterrupt

    def fake_restore(self, principal):
        state["default"] = principal
        state["restored"] = True
        return True

    source = ts_module.TicketSource(settings)
    monkeypatch.setattr(ts_module.TicketSource, "_run", interrupted)
    monkeypatch.setattr(ts_module.TicketSource, "resolve",
                        lambda self, c: Path("/bin/true"))
    monkeypatch.setattr(ts_module.TicketSource, "default_principal_status",
                        staticmethod(lambda: (state["default"], None)))
    monkeypatch.setattr(ts_module.TicketSource, "restore_default", fake_restore)

    with pytest.raises(KeyboardInterrupt):
        source.ticket("mu2edaq", tmp_path / "cache")
    assert state["restored"], "the default must be put back on an interrupt"
    assert state["default"] == "anorman@FNAL.GOV"


def test_a_failed_restore_does_not_replace_the_interrupt(settings, tmp_path,
                                                         monkeypatch):
    from mu2edaq_power_recovery.creds import ticketsource as ts_module

    principals = iter(["anorman@FNAL.GOV", "mu2edaq/mu2e@FNAL.GOV"])
    source = ts_module.TicketSource(settings)

    def interrupted(self, command, args, timeout=120, env=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(ts_module.TicketSource, "_run", interrupted)
    monkeypatch.setattr(ts_module.TicketSource, "resolve",
                        lambda self, c: Path("/bin/true"))
    monkeypatch.setattr(ts_module.TicketSource, "default_principal_status",
                        staticmethod(lambda: (next(principals), None)))
    monkeypatch.setattr(ts_module.TicketSource, "restore_default",
                        lambda self, p: False)
    with pytest.raises(KeyboardInterrupt):
        source.ticket("mu2edaq", tmp_path / "cache")


def test_an_interrupted_mint_leaves_its_cache_for_cleanup(manager, monkeypatch):
    from mu2edaq_power_recovery.transport import local as local_module

    class Interrupting(FakeTicketSource):
        def ticket(self, identity, cache, timeout=120, **kwargs):
            # The tool wrote the cache, then the run was told to stop.
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text("half a ticket")
            raise KeyboardInterrupt

    manager.tickets = Interrupting(["mu2edaq"])
    with pytest.raises(KeyboardInterrupt):
        manager.service_credential("mu2edaq")
    cache = manager.cache_for("svc-mu2edaq")
    assert manager._caches["svc-mu2edaq"] == cache

    calls = []

    def fake_run(self, command, timeout=None, user=None, input_text=None,
                 check=False):
        calls.append(list(command))
        return CommandResult(command=" ".join(command), rc=0)

    monkeypatch.setattr(local_module.LocalTransport, "run", fake_run)
    manager.cleanup()
    assert ["kdestroy", "-c", f"FILE:{cache}"] in calls
    assert not cache.exists()


def test_cleanup_tolerates_a_cache_the_mint_never_created(manager, monkeypatch):
    from mu2edaq_power_recovery.transport import local as local_module

    class InterruptedEarly(FakeTicketSource):
        def ticket(self, identity, cache, timeout=120, **kwargs):
            raise KeyboardInterrupt          # before anything was written

    manager.tickets = InterruptedEarly(["mu2edaq"])
    with pytest.raises(KeyboardInterrupt):
        manager.service_credential("mu2edaq")

    def missing(self, command, timeout=None, user=None, input_text=None,
                check=False):
        return CommandResult(command=" ".join(command), rc=1,
                             stderr="kdestroy: No credentials cache found")

    monkeypatch.setattr(local_module.LocalTransport, "run", missing)
    manager.cleanup()          # must not raise
    assert not manager.cache_for("svc-mu2edaq").exists()


# ---------------------------------------------------------------------------
# minting from "no default" when the run uses no default cache (S2)
# ---------------------------------------------------------------------------


def _no_default():
    from mu2edaq_power_recovery.creds.ticketsource import NoDefaultCache
    return None, NoDefaultCache("there is no default credential cache "
                                "(klist: No credentials cache found)")


def _patched_source(settings, monkeypatch, statuses, destroyed=None,
                    destroy_ok=True):
    """A TicketSource whose klist answers from *statuses*, in order."""
    from mu2edaq_power_recovery.creds import ticketsource as ts_module

    minted = []
    seq = iter(statuses)
    state = {"last": None}

    def status():
        state["last"] = next(seq, state["last"])
        return state["last"]

    def fake_run(self, command, args, timeout=120, env=None):
        minted.append(list(args))
        cache = Path(str(args[args.index("--cache") + 1]).replace("FILE:", ""))
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text("ticket")
        return type("R", (), {"returncode": 0, "stdout": b"", "stderr": b""})()

    def destroy(self, principal):
        if destroyed is not None:
            destroyed.append(principal)
        if destroy_ok:
            state["last"] = (None, state["last"][1])
        return destroy_ok

    monkeypatch.setattr(ts_module.TicketSource, "_run", fake_run)
    monkeypatch.setattr(ts_module.TicketSource, "resolve",
                        lambda self, c: Path("/bin/true"))
    monkeypatch.setattr(ts_module.TicketSource, "_principal_of",
                        staticmethod(lambda cache: "mu2edaq/mu2e@FNAL.GOV"))
    monkeypatch.setattr(ts_module.TicketSource, "default_principal_status",
                        staticmethod(lambda: (status()[0], status_reason(state))))
    monkeypatch.setattr(ts_module.TicketSource, "destroy_default", destroy)
    return ts_module.TicketSource(settings), minted


def status_reason(state):
    return state["last"][1] if state["last"] else None


def test_private_primaries_and_no_default_let_the_mint_proceed(settings, tmp_path,
                                                              monkeypatch):
    source, minted = _patched_source(settings, monkeypatch,
                                     [_no_default(), _no_default()])
    ticket = source.ticket("mu2edaq", tmp_path / "cache", private_primary=True)
    assert len(minted) == 1
    assert ticket.cache == str(tmp_path / "cache")


def test_a_service_default_left_by_the_mint_is_destroyed(settings, tmp_path,
                                                         monkeypatch):
    no = _no_default()
    destroyed = []
    source, minted = _patched_source(
        settings, monkeypatch,
        [no, ("mu2edaq/mu2e@FNAL.GOV", None)], destroyed=destroyed)
    source.ticket("mu2edaq", tmp_path / "cache", private_primary=True)
    assert destroyed == ["mu2edaq/mu2e@FNAL.GOV"]


def test_a_service_default_that_cannot_be_destroyed_is_displaced(settings, tmp_path,
                                                                 monkeypatch):
    source, _ = _patched_source(
        settings, monkeypatch,
        [_no_default(), ("mu2edaq/mu2e@FNAL.GOV", None)], destroy_ok=False)
    with pytest.raises(DefaultCacheDisplaced) as excinfo:
        source.ticket("mu2edaq", tmp_path / "cache", private_primary=True)
    assert excinfo.value.before is None
    assert excinfo.value.after == "mu2edaq/mu2e@FNAL.GOV"


def test_a_foreign_default_appearing_is_left_alone(settings, tmp_path, monkeypatch):
    destroyed = []
    source, _ = _patched_source(
        settings, monkeypatch,
        [_no_default(), ("anorman@FNAL.GOV", None)], destroyed=destroyed)
    source.ticket("mu2edaq", tmp_path / "cache", private_primary=True)
    assert destroyed == []


def test_an_ambient_primary_with_no_default_is_still_refused(settings, tmp_path,
                                                             monkeypatch):
    source, minted = _patched_source(settings, monkeypatch, [_no_default()])
    with pytest.raises(DefaultCacheUnverifiable):
        source.ticket("mu2edaq", tmp_path / "cache", private_primary=False)
    assert minted == []


def test_private_primaries_do_not_excuse_an_unreadable_klist(settings, tmp_path,
                                                             monkeypatch):
    # Not "no default": klist output that did not parse. Nothing could be
    # checked afterwards either, so the mint is refused regardless.
    source, minted = _patched_source(
        settings, monkeypatch, [(None, "klist listed a default cache but named "
                                       "no principal")])
    with pytest.raises(DefaultCacheUnverifiable):
        source.ticket("mu2edaq", tmp_path / "cache", private_primary=True)
    assert minted == []


def test_the_manager_passes_private_primary_only_when_both_roles_are_private(
        manager):
    seen = []

    class Recording(FakeTicketSource):
        def ticket(self, identity, cache, timeout=120, **kwargs):
            seen.append(kwargs.get("private_primary"))
            return super().ticket(identity, cache, timeout)

    manager.tickets = Recording(["mu2edaq", "mu2eshift"])
    manager.service_credential("mu2edaq")
    assert seen == [False], "the ambient cache is in use: not private"

    manager.settings.set("kerberos.principal", "anorman@FNAL.GOV")
    manager.settings.set("kerberos.root_principal", "anorman/root@FNAL.GOV")
    manager._caches["general"] = manager.cache_for("anorman@FNAL.GOV")
    manager._caches["root"] = manager.cache_for("anorman/root@FNAL.GOV")
    manager.service_credential("mu2eshift")
    assert seen == [False, True]


def test_a_root_role_on_the_ambient_cache_is_not_private(manager):
    manager.settings.set("kerberos.principal", "anorman@FNAL.GOV")
    manager._caches["general"] = manager.cache_for("anorman@FNAL.GOV")
    # No root principal: root sessions use the ambient cache.
    assert not manager._primaries_private()
    assert not manager._primaries_private(planned=True)


def _kinit_harness(manager, monkeypatch, status):
    import getpass as getpass_module

    from mu2edaq_power_recovery.transport import local as local_module

    ran = []
    prompted = []

    def fake_run(self, command, timeout=None, user=None, input_text=None,
                 check=False):
        ran.append(list(command))
        return CommandResult(command=" ".join(command), rc=0)

    monkeypatch.setattr(getpass_module, "getpass",
                        lambda prompt="": prompted.append(prompt) or "pw")
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(local_module.LocalTransport, "run", fake_run)
    manager.tickets.default_principal_status = lambda: status
    return ran, prompted


def test_kinit_on_a_fresh_login_proceeds(manager, monkeypatch):
    # The documented setup: kerberos.principal in config/.env, no ticket yet.
    # kinit mints the operator's own principal; that becoming the default is
    # what the guard protects, not a displacement, so it must not be refused.
    manager.settings.set("kerberos.principal", "anorman@FNAL.GOV")
    ran, prompted = _kinit_harness(manager, monkeypatch, _no_default())
    cache = manager.cache_for("anorman@FNAL.GOV")
    manager._kinit("anorman@FNAL.GOV", cache, "general")
    assert ran == [["kinit", "-c", f"FILE:{cache}", "anorman@FNAL.GOV"]]
    assert prompted, "the operator is prompted for the password"


def test_kinit_with_no_default_and_private_primaries_proceeds(manager, monkeypatch):
    manager.settings.set("kerberos.principal", "anorman@FNAL.GOV")
    manager.settings.set("kerberos.root_principal", "anorman/root@FNAL.GOV")
    ran, _ = _kinit_harness(manager, monkeypatch, _no_default())
    cache = manager.cache_for("anorman@FNAL.GOV")
    manager._kinit("anorman@FNAL.GOV", cache, "general")
    assert ran == [["kinit", "-c", f"FILE:{cache}", "anorman@FNAL.GOV"]]


def test_kinit_with_an_unparsable_default_is_refused_even_when_private(
        manager, monkeypatch):
    manager.settings.set("kerberos.principal", "anorman@FNAL.GOV")
    manager.settings.set("kerberos.root_principal", "anorman/root@FNAL.GOV")
    ran, _ = _kinit_harness(manager, monkeypatch,
                            (None, "klist named no principal"))
    with pytest.raises(KerberosError):
        manager._kinit("anorman@FNAL.GOV",
                       manager.cache_for("anorman@FNAL.GOV"), "general")
    assert ran == []


def test_destroy_default_names_the_cache_never_a_bare_kdestroy(settings,
                                                               monkeypatch):
    from mu2edaq_power_recovery.creds import ticketsource as ts_module

    calls = []
    principals = iter([None])          # after the kdestroy: no default

    def fake_run(argv, **kwargs):
        calls.append((list(argv), "KRB5CCNAME" in (kwargs.get("env") or {})))
        return _completed(0)

    monkeypatch.setattr(ts_module.subprocess, "run", fake_run)
    monkeypatch.setattr(ts_module.TicketSource, "collection",
                        staticmethod(lambda: {"mu2edaq/mu2e@FNAL.GOV": "API:77"}))
    monkeypatch.setattr(ts_module.TicketSource, "default_principal",
                        staticmethod(lambda: next(principals, None)))
    source = ts_module.TicketSource(settings)
    assert source.destroy_default("mu2edaq/mu2e@FNAL.GOV")
    assert calls == [(["kdestroy", "-c", "API:77"], False)]


def test_identity_discovery_runs_once_per_manager(manager):
    calls = []
    original = manager.tickets.identities

    def counting():
        calls.append(1)
        return original()

    manager.tickets.identities = counting
    for _ in range(5):
        manager.chain()
        manager.chain(root=True)
    assert manager.available_identities()[:2] == ["mu2edaq", "mu2eshift"]
    assert len(calls) == 1
    # The live switch still applies: memoised discovery is not memoised policy.
    manager.settings.set("kerberos.use_service_keytabs", False)
    assert manager.available_identities() == []
