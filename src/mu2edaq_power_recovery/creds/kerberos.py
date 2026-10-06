"""Kerberos ticket management for the recovery run.

Project-Description.md asks for three things, and this module is each of them:

* a set of principals can be designated, one general and one root-capable;
* the tools prompt for a password when a principal has no usable ticket;
* logins to the gateways -- and, by delegation, onward to the nodes -- use
  those tickets.

Everything is done by driving ``klist``/``kinit``, not by linking a GSSAPI
binding.  The site's krb5 configuration, whatever it is, is then automatically
the one in force, and the tool has no build-time dependency on python-krb5 on
a host that may be recovering from an outage.

Separate credential caches
--------------------------
The general and root principals are kept in *separate* caches
(``KRB5CCNAME=FILE:...``) rather than a cache collection, so acquiring the root
ticket cannot silently replace the ordinary one, and an SSH command can select
which identity it runs under simply by which environment it is given.

Concurrency
-----------
Nodes are assessed a thread apiece, and every transport asks for a credential
chain. Minting is therefore one serialised transaction under ``_mint_lock``:
read the default cache, mint, look the ticket up in the collection, restore
the default. Two of those interleaved can restore each other's "before" and
leave the operator's default displaced, which is the failure the whole guard
exists to prevent. A mint's results are published to the shared maps last, so
the lock-free fast path never sees a half-initialised credential.
"""
from __future__ import annotations

import getpass
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..transport.base import TransportError
from ..transport.local import LocalTransport
from .ticketsource import (DefaultCacheDisplaced, DefaultCacheGuardError,
                           DefaultCacheUnverifiable, NoDefaultCache,
                           TicketSource, TicketSourceError, TicketTimeout)

log = logging.getLogger(__name__)

#: Sentinel for "not looked up yet", so a genuine None is cached too.
_UNSET = object()

#: Credential-cache types that KRB5CCNAME understands. A value already
#: carrying one of these is a complete ccache name and must be passed through
#: untouched -- on macOS a service ticket lands in the API: collection, not in
#: a file, so assuming FILE: would point ssh at a path that does not exist.
CCACHE_TYPES = ("FILE:", "API:", "DIR:", "KEYRING:", "KCM:", "MEMORY:")


def ccache_name(cache: Any) -> str:
    """The KRB5CCNAME value for *cache*, which may be a path or a ccache name."""
    text = str(cache)
    return text if text.startswith(CCACHE_TYPES) else f"FILE:{text}"


class KerberosError(RuntimeError):
    """No usable ticket could be obtained for a required principal."""


@dataclass
class Credential:
    """One identity an SSH session can be attempted under.

    A recovery has several to choose from: the operator's own principal, their
    root principal, and the Mu2e service identities whose keytabs live in
    Vault. Which of them can log in to a given node varies -- that is the
    point of trying them in turn -- so a credential carries both the ssh login
    to use and the credential cache that authenticates it.
    """

    #: Short name for logs and the report: 'operator', 'root', or the identity.
    name: str
    #: SSH login. None means "whatever ssh would use by default".
    login: Optional[str] = None
    #: KRB5CCNAME target. None means the ambient credential cache.
    cache: Optional[Path] = None
    #: Kerberos principal, when known.
    principal: Optional[str] = None
    #: 'operator ticket' or 'vault keytab' -- shown in the report.
    source: str = "operator ticket"

    #: True for the operator's own principal. It is always tried first, and
    #: the run returns to it; the service identities are only ever fallbacks.
    primary: bool = False

    #: Why this credential's ticket has not been acquired, or None when it
    #: has (or when it is the ambient cache, which needs no acquiring). Set
    #: only on credentials built for *describing* a chain -- see
    #: KerberosManager.chain(candidates=True) -- so a display never claims a
    #: cache will be used that does not exist yet.
    pending: Optional[str] = None

    def environ(self) -> Dict[str, str]:
        """Environment additions selecting this credential's cache."""
        return {"KRB5CCNAME": ccache_name(self.cache)} if self.cache else {}

    def for_login(self, login: Optional[str]) -> "Credential":
        """This credential's ticket, used to log in as a different account.

        Which ticket authenticates and which account is logged into are
        separate things. Root access through a service identity is exactly
        this: authenticate as, say, ``mu2edaq``, and log in to the ``root``
        account, which the node's ``root/.k5login`` may authorise.
        """
        return replace(self, login=login)

    def as_dict(self) -> Dict[str, Any]:
        out = {"name": self.name, "login": self.login,
               "principal": self.principal, "source": self.source}
        if self.pending:
            out["pending"] = self.pending
        return out

    def __str__(self) -> str:
        return f"{self.name} ({self.principal or self.login or 'ambient'})"

    def describe(self) -> str:
        """One line naming the login, the ticket and where the ticket lives.

        This is the pair that actually decides whether a login succeeds, and
        the pair that is invisible in an ordinary ssh failure -- "Permission
        denied (gssapi)" says nothing about which principal was offered to
        which account.
        """
        login = self.login or "(ssh default)"
        principal = self.principal or "(principal unknown)"
        if self.pending:
            cache = f"not acquired: {self.pending}"
        else:
            cache = ccache_name(self.cache) if self.cache else "ambient cache"
        return f"login {login:<16} ticket {principal:<34} [{cache}]"


@dataclass
class FallbacksDisabled:
    """Why the service identities were abandoned for the rest of a run.

    Carried as data, not inferred from a message: the decision is made on the
    exception *type* (:class:`DefaultCacheGuardError`), and this records which
    kind it was and what the operator should do about it.
    """

    #: 'displaced' or 'unverifiable' -- DefaultCacheGuardError.kind.
    kind: str
    #: The identity whose mint raised it.
    identity: str
    #: The exception's own message.
    detail: str
    #: What the operator should do.
    guidance: str

    def note(self) -> str:
        """One note for the phase report, the run store and the console."""
        return (f"service identities disabled for the rest of this run "
                f"({self.kind}, while minting {self.identity}): {self.detail}. "
                f"{self.guidance}")

    def as_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "identity": self.identity,
                "detail": self.detail, "guidance": self.guidance}


def _guidance(exc: DefaultCacheGuardError) -> str:
    """What to do about a guard failure, by type."""
    if isinstance(exc, DefaultCacheDisplaced):
        before = exc.before or "<you>@FNAL.GOV"
        return (f"The operator's ticket is still in the collection: run "
                f"'kswitch -p {before}' (or 'kinit {before}') before the next "
                f"run, and set kerberos.use_service_keytabs: false if it "
                f"recurs. This run continues on the operator credential alone.")
    if isinstance(exc, DefaultCacheUnverifiable):
        return ("Run 'kinit <you>@FNAL.GOV' so the default cache holds your own "
                "ticket (check with 'klist'), then re-run to use the service "
                "identities. This run continues on the operator credential "
                "alone.")
    return "This run continues on the operator credential alone."


@dataclass
class TicketInfo:
    """What ``klist`` says about one credential cache."""

    principal: Optional[str]
    cache: Optional[str]
    expires: Optional[float] = None      # epoch seconds, None if unparsed
    valid: bool = False
    raw: str = ""

    @property
    def remaining(self) -> Optional[float]:
        if self.expires is None:
            return None
        return self.expires - time.time()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "principal": self.principal,
            "cache": self.cache,
            "valid": self.valid,
            "remaining_s": int(self.remaining) if self.remaining is not None else None,
        }


#: klist -s exits 0 when the cache holds a ticket that has not expired.  That
#: is the authoritative validity test; the text parse below is only for the
#: human-facing detail (which principal, how much longer).
#: MIT prints "Ticket cache:" / "Default principal:"; Heimdal (macOS) prints
#: "Credentials cache:" / "Principal:". The ticket table's "Principal" column
#: heading has no colon, so it does not match.
_PRINCIPAL_RE = re.compile(r"^\s*(?:Default\s+)?principal:\s*(\S+)", re.I | re.M)
_CACHE_RE = re.compile(r"^\s*(?:Ticket|Credentials)\s+cache:\s*(\S+)", re.I | re.M)


class KerberosManager:
    """Acquires and tracks the tickets a run needs."""

    def __init__(self, settings: Any, local: Optional[LocalTransport] = None,
                 cache_dir: Optional[Path] = None):
        self.settings = settings
        self.local = local or LocalTransport(default_timeout=60)
        self.prompt_allowed: bool = bool(settings.get("kerberos.prompt", True))
        self.min_lifetime: int = int(settings.get("kerberos.min_lifetime", 3600))
        self._cache_dir = cache_dir or Path(
            tempfile.mkdtemp(prefix="mu2e-recovery-krb5-"))
        #: role -> KRB5CCNAME target: a path under _cache_dir normally, or a
        #: collection name when Heimdal put the ticket in the API: collection.
        self._caches: Dict[str, Any] = {}
        self._acquired: List[str] = []
        #: Service tickets come from the mu2edaq-kerberos package, which owns
        #: the keytab-in-Vault layout; this project never handles a keytab.
        self.tickets = TicketSource(settings)
        #: identity -> Credential, or None once an identity is known to be
        #: unusable. Cached so that forty nodes do not each re-fetch a keytab
        #: from Vault and re-run kinit for the same identity.
        self._service: Dict[str, Optional[Credential]] = {}
        #: Identities that have successfully logged in somewhere, most recent
        #: first. Used to reorder the chain -- see order_chain().
        self._successful: List[str] = []
        #: Cached principal of the default credential cache.
        self._ambient: Any = _UNSET
        #: Serialises every mint -- service identities and the designated
        #: principals alike -- as one read/mint/lookup/restore transaction.
        #: Re-entrant because warm_fallbacks() and chain() reach
        #: service_credential() while a caller may already hold it.
        self._mint_lock = threading.RLock()
        #: Guards _successful. Separate from the mint lock on purpose: an ssh
        #: success callback must not wait behind a two-minute mint.
        self._state_lock = threading.Lock()
        #: Set once a default-cache guard failure has abandoned the service
        #: identities; None while they are usable.
        self.fallbacks_disabled: Optional[FallbacksDisabled] = None
        #: False for a describe-only session (mu2e-ssh-probe without --run):
        #: chain() then never mints, it only reports what would be tried.
        self.minting: bool = True
        #: Set by cleanup(); no mint is started after it.
        self._closed = False
        #: `vault-client identities`, run once per manager: every chain() --
        #: one per transport, so per node -- asks for the identity list, and
        #: the answer does not change during a run.
        self._discovered: Optional[List[str]] = None
        self._discover_lock = threading.Lock()

    # -- inspection --------------------------------------------------------

    def _klist(self, cache: Optional[Any] = None) -> TicketInfo:
        # ccache_name, not FILE:, because a cache we hold may be a collection
        # name (API:<uuid>) rather than a path -- see ensure().
        env = {"KRB5CCNAME": ccache_name(cache)} if cache else {}
        runner = LocalTransport(default_timeout=20, env=env)
        try:
            listed = runner.run(["klist"], timeout=20)
        except TransportError as exc:
            return TicketInfo(principal=None, cache=str(cache) if cache else None,
                              valid=False, raw=str(exc))
        text = listed.stdout + listed.stderr
        principal = _PRINCIPAL_RE.search(text)
        cache_name = _CACHE_RE.search(text)
        # klist -s is the real test; it is silent and exits non-zero when the
        # cache is missing, empty, or holds only expired tickets.
        valid = runner.run_ok(["klist", "-s"], timeout=20)
        return TicketInfo(
            principal=principal.group(1) if principal else None,
            cache=cache_name.group(1) if cache_name else (str(cache) if cache else None),
            expires=self._parse_expiry(text),
            valid=valid,
            raw=text.strip(),
        )

    @staticmethod
    def _parse_expiry(text: str) -> Optional[float]:
        """Best-effort parse of the first ticket's expiry from klist output.

        klist's date format is locale-dependent, so a failure here is expected
        and harmless -- validity comes from ``klist -s``, and this only feeds
        the "renew early" convenience check.
        """
        import datetime as _dt
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 4 and "/" in parts[0] and ":" in parts[1]:
                # MIT: "10/01/26 13:24:02  10/02/26 15:24:02  krbtgt/..."
                for fmt in ("%m/%d/%y %H:%M:%S", "%m/%d/%Y %H:%M:%S",
                            "%m/%d/%y %H:%M", "%m/%d/%Y %H:%M"):
                    try:
                        return _dt.datetime.strptime(f"{parts[2]} {parts[3]}", fmt).timestamp()
                    except ValueError:
                        continue
            elif len(parts) >= 9 and ":" in parts[2] and ":" in parts[6]:
                # Heimdal: "Oct  1 13:24:02 2026  Oct  2 15:24:02 2026  krbtgt/..."
                try:
                    return _dt.datetime.strptime(" ".join(parts[4:8]),
                                                 "%b %d %H:%M:%S %Y").timestamp()
                except ValueError:
                    continue
        return None

    def current(self) -> TicketInfo:
        """The ticket in the operator's ambient credential cache."""
        return self._klist(None)

    # -- acquisition -------------------------------------------------------

    def cache_for(self, principal: str) -> Path:
        """Path of the private credential cache used for *principal*."""
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", principal)
        return self._cache_dir / f"krb5cc_{safe}"

    def ensure(self, principal: Optional[str], role: str = "general") -> TicketInfo:
        """Return a usable ticket for *principal*, acquiring one if needed.

        With *principal* None the ambient cache is used as-is: an operator who
        already ran ``kinit`` should not be asked again.  Otherwise a private
        cache is used, and ``kinit`` is run into it -- prompting for the
        password when allowed, and failing with instructions when not.
        """
        if not principal:
            info = self.current()
            principal = info.principal or self.ambient_principal()
            if not info.valid:
                raise KerberosError(
                    "no valid Kerberos ticket in the default credential cache.\n"
                    "Run 'kinit <your principal>' first, or pass "
                    "--principal <principal> to have this tool acquire one."
                )
            log.info("using the ambient Kerberos ticket: %s",
                     principal or "(principal unknown)")
            return info

        cache: Any = self.cache_for(principal)
        self._caches[role] = cache
        info = self._klist(cache)
        if info.valid and (info.remaining is None or info.remaining >= self.min_lifetime):
            log.info("reusing %s ticket for %s", role, principal)
            return info
        if info.valid:
            log.info("%s ticket for %s expires in %ss; renewing up front",
                     role, principal, int(info.remaining or 0))

        self._kinit(principal, cache, role)
        info = self._klist(cache)
        if not info.valid:
            # No file, but kinit reported success: on macOS Heimdal does not
            # honour a FILE: cache name and put the ticket in the API:
            # collection instead. It is perfectly usable, it just has a ccache
            # name rather than a path. Same fallback TicketSource.ticket()
            # makes for the service identities -- without it, --principal and
            # --root-principal simply do not work on the operator's own laptop.
            name = self._collection_cache_for(principal)
            if name:
                log.debug("%s is in the credential collection as %s",
                          principal, name)
                cache = name
                self._caches[role] = name
                info = self._klist(name)
        if not info.valid:
            raise KerberosError(f"kinit appeared to succeed but no valid ticket "
                                f"is present for {principal}")
        self._acquired.append(principal)
        return info

    def _collection_cache_for(self, principal: str) -> Optional[str]:
        """The ccache name holding *principal*, from the collection."""
        caches = self.tickets.collection()
        if principal in caches:
            return caches[principal]
        # A principal given without a realm still names one cache.
        wanted = principal.split("@")[0]
        for held, name in caches.items():
            if held.split("@")[0] == wanted:
                return name
        return None

    def _kinit(self, principal: str, cache: Path, role: str) -> None:
        """Run kinit into *cache*, reading the password from stdin.

        The password never becomes an argument, never enters the environment,
        and is not retained after this call.
        """
        if not self.prompt_allowed:
            raise KerberosError(
                f"no valid ticket for {principal} and interactive prompting is "
                f"disabled (kerberos.prompt: false).\n"
                f"Acquire one first, e.g.:\n"
                f"    KRB5CCNAME=FILE:{cache} kinit {principal}"
            )
        if not shutil.which("kinit"):
            raise KerberosError("kinit is not on PATH; install the Kerberos client tools")
        # Before the prompt, so an operator is not asked for a password only
        # to be refused; checked again under the lock below.
        self._guard_kinit(principal, self._default_status())

        cache.parent.mkdir(parents=True, exist_ok=True)
        print(f"\nA Kerberos ticket is needed for the {role} principal.")
        try:
            password = getpass.getpass(f"  Password for {principal}: ")
        except (EOFError, KeyboardInterrupt) as exc:
            raise KerberosError(f"password entry cancelled for {principal}") from exc
        if not password:
            raise KerberosError(f"empty password given for {principal}")

        target = ccache_name(cache)
        runner = LocalTransport(default_timeout=60,
                                env={"KRB5CCNAME": target})
        # The same displacement TicketSource.ticket() guards against, in the
        # one other place this project mints a ticket -- and the riskier one,
        # because this is the operator's own, root-capable principal. -c is
        # what kinit documents; KRB5CCNAME alone is what Heimdal ignores.
        # Under the mint lock like every other mint, so it cannot interleave
        # with a service mint's read-before/restore.
        with self._mint_lock:
            before, why = self._default_status()
            self._guard_kinit(principal, (before, why))
            try:
                result = runner.run(["kinit", "-c", target, principal],
                                    timeout=60, input_text=password)
            except BaseException:
                # Interrupted (Ctrl-C, or SIGTERM raised as KeyboardInterrupt)
                # or timed out with kinit running: it may already have minted,
                # and on macOS that ticket is in the API: collection as the
                # new default while _caches[role] still holds the FILE: path.
                # Same as TicketSource.ticket(): settle, then re-raise.
                self._settle_interrupted_kinit(principal, role, before)
                raise
            finally:
                del password
            self._restore_default_if_displaced(principal, before)
            if not before:
                # A default may exist now (this principal's); re-read it.
                self._ambient = _UNSET
        if not result.ok:
            detail = (result.stderr or result.stdout).strip().splitlines()
            raise KerberosError(f"kinit failed for {principal}: "
                                f"{detail[-1] if detail else 'unknown error'}")
        log.info("acquired %s ticket for %s", role, principal)

    def _settle_interrupted_kinit(self, principal: str, role: str,
                                  before: Optional[str]) -> None:
        """After a kinit that did not return: restore, and record its cache.

        Called with the mint lock held and an exception in flight. Puts the
        default back if the kinit displaced it, then looks *principal* up in
        the collection and records the cache it names for :meth:`cleanup` --
        otherwise a root ticket minted into API:<uuid> outlives the run as the
        operator's default. Each step runs only ``kswitch``/``klist``, and
        either can itself fail or be interrupted; both are logged and dropped
        so the caller's exception is the one that propagates.
        """
        try:
            self._restore_default_if_displaced(principal, before)
        except BaseException as exc:            # noqa: BLE001 -- see above
            log.error("could not restore the default credential cache after "
                      "an interrupted kinit for %s: %r", principal, exc)
        finally:
            self._ambient = _UNSET
        self._record_collection_cache(
            f"{role}@collection", principal, before,
            lambda: self._collection_cache_for(principal))

    def _record_collection_cache(self, key: str, principal: str,
                                 before: Optional[str], lookup) -> None:
        """Record the collection cache an unfinished mint may have left.

        *lookup* returns the ccache name (or ``(principal, name)``) holding
        *principal*, or None. Recorded under its own *key*, beside the FILE:
        path already in ``_caches``, so cleanup() destroys whichever exists.
        Only a non-file name is recorded -- a FILE: cache is the pre-recorded
        path or nothing of ours -- and never the cache of *before*, the
        principal that was the default when the mint began: that one was the
        operator's before this run touched it. Failures are logged, never
        raised: this runs while another exception is propagating.
        """
        try:
            found = lookup()
        except BaseException as exc:            # noqa: BLE001 -- bookkeeping only
            log.error("could not look %s up in the credential collection "
                      "after an unfinished mint; if a ticket was minted it "
                      "may survive the run (klist -l; kdestroy -c <name>): %r",
                      principal, exc)
            return
        if isinstance(found, tuple):
            held, name = found
        else:
            held, name = principal, found
        if not name or str(name).startswith("FILE:"):
            return
        if before and held.split("@")[0] == before.split("@")[0]:
            log.debug("%s is the cache that was the default before the mint; "
                      "not recording it for cleanup", name)
            return
        self._caches[key] = name
        log.debug("unfinished mint for %s: recorded %s for cleanup",
                  principal, name)

    def _default_status(self):
        """``(principal, reason)`` for the default cache, via the ticket source."""
        status = getattr(self.tickets, "default_principal_status", None)
        if status is not None:
            return status()
        principal = self.tickets.default_principal()
        return principal, (None if principal else "default cache not readable")

    def _primaries_private(self, planned: bool = False) -> bool:
        """True when every primary role (general *and* root) has a private cache.

        Then nothing the run does depends on the default credential cache,
        which is what makes minting from "no default at all" safe. Root with no
        root principal designated runs on the ambient cache (see
        operator_credential), so it needs one -- or the same principal as
        general, which prepare() gives the general cache. *planned* answers
        from the configuration alone, for use while prepare() is still
        acquiring them; otherwise both caches must exist too.
        """
        general = self.settings.get("kerberos.principal")
        root = self.settings.get("kerberos.root_principal")
        if not (general and root):
            return False
        if planned:
            return True
        return all(self._caches.get(role) for role in ("general", "root"))

    def _guard_kinit(self, principal: str, status) -> None:
        """The service-mint rule, for kinit: never mint unguarded.

        A readable default can be compared and restored afterwards. When klist
        says there simply is no default (``NoDefaultCache`` -- a fresh login),
        kinit is allowed: what it mints is the *operator's* designated
        principal, and that principal becoming the default is exactly what the
        guard exists to preserve, not a displacement. (Service mints are held
        to the stricter rule in ``TicketSource.ticket``.) A default that exists
        but cannot be read is refused, before the password prompt, because a
        kinit could then displace an identity the run cannot restore.
        """
        before, why = status
        if before or isinstance(why, NoDefaultCache):
            return
        raise KerberosError(
            f"cannot acquire a ticket for {principal}: the default credential "
            f"cache could not be read ({why or 'no principal reported'}), so "
            f"the kinit could not be checked for displacing it, and this run "
            f"uses the default cache for "
            f"{'root sessions' if self.settings.get('kerberos.principal') else 'its sessions'}.\n"
            f"Run 'klist' to see what is wrong with the default cache, or "
            f"'kinit <you>@FNAL.GOV' to replace it, then re-run.")

    def _restore_default_if_displaced(self, principal: str,
                                      before: Optional[str]) -> None:
        """Put the default credential cache back if minting moved it.

        Unlike the service-identity path this does not abort the run when the
        restore fails, and the asymmetry is deliberate: a service identity is
        an optional fallback, so refusing it costs the operator nothing they
        need, whereas a designated principal *is* the run. Refusing to acquire
        it mid-outage would be worse than the displacement, which ``kswitch``
        undoes in a second and which the startup guard names on the next run.
        """
        if not before:
            return
        after = self.tickets.default_principal()
        if after == before:
            return
        if self.tickets.restore_default(before):
            log.debug("default credential cache moved to %s while acquiring "
                      "%s; restored to %s", after, principal, before)
            return
        log.error(
            "acquiring a ticket for %s repointed the default credential cache "
            "from %r to %r and it could not be restored. Every later login "
            "will run as the wrong identity -- run 'kswitch -p %s' (or "
            "'kinit %s') before continuing.",
            principal, before, after, before, before)
        self._ambient = _UNSET

    # -- service identities, via mu2edaq-kerberos -----------------------------

    def available_identities(self) -> List[str]:
        """Service identities to try, in the order they should be tried.

        The configured preferences come first -- mu2edaq and mu2eshift by
        default, because between them they cover most of the cluster -- and
        every other identity mu2edaq-kerberos knows about follows, so an
        identity added to Vault becomes usable here with no change to this
        project. Identities that have already worked in this run are promoted
        to the front by :meth:`order_chain`.
        """
        if not self.settings.get("kerberos.use_service_keytabs", True):
            return []
        preferred = list(self.settings.get("kerberos.service_identities",
                                           ["mu2edaq", "mu2eshift"]) or [])
        discovered: List[str] = []
        if self.settings.get("kerberos.discover_identities", True):
            with self._discover_lock:
                if self._discovered is None:
                    self._discovered = list(self.tickets.identities())
                discovered = list(self._discovered)
        if not discovered:
            return preferred
        ordered = [i for i in preferred if i in discovered]
        ordered += [i for i in discovered if i not in ordered]
        return ordered

    def service_credential(self, identity: str) -> Optional[Credential]:
        """A credential for one service identity, or None if unusable.

        The keytab never passes through this process: ``get-kerberos-ticket``
        reads it from Vault, uses it, and removes it, and all we receive is the
        path of a credential cache.

        A failure is cached as None. An identity whose keytab is missing, or
        whose kinit fails, will fail identically for every other node, and
        rediscovering that once per node would dominate a fifty-node run.

        Thread-safe. The fast path reads the published result without the
        lock; a miss takes the lock and checks again, so concurrent cold
        requests for one identity mint it once, and mints of different
        identities never interleave their default-cache restores.
        """
        if identity in self._service:
            return self._service[identity]

        with self._mint_lock:
            if identity in self._service:
                return self._service[identity]
            if self._closed or self.fallbacks_disabled is not None:
                # Cleaned up, or the default cache can no longer be trusted:
                # a thread that queued behind the failing mint must not start
                # another one.
                return None

            credential: Optional[Credential] = None
            cache_key = f"svc-{identity}"
            ticket_cache: Any = None
            if not self.tickets.available:
                log.debug("%s", self.tickets.unavailable_reason())
            else:
                cache = self.cache_for(cache_key)
                private = self._primaries_private()
                # Recorded for cleanup() *before* the mint, so a mint that is
                # interrupted (SIGTERM -> KeyboardInterrupt) after writing the
                # cache still has it destroyed. cleanup() tolerates a cache
                # that was never created. Replaced below by the ticket's real
                # cache, which on macOS may be a collection name instead.
                self._caches[cache_key] = cache
                try:
                    ticket = self.tickets.ticket(identity, cache,
                                                 private_primary=private)
                except DefaultCacheGuardError as exc:
                    # Decided on the type, never the wording. Either the
                    # operator's default has been displaced and could not be
                    # put back, or it could not be read so a mint could not be
                    # checked at all; in both cases every further mint carries
                    # the same risk, so stop them all.
                    if isinstance(exc, DefaultCacheDisplaced):
                        # Raised after the mint ran: the displacing default
                        # is this identity's cache, and ours to destroy.
                        self._record_collection_cache(
                            f"{cache_key}@collection", identity, None,
                            lambda: self.tickets.collection_cache_for(identity))
                    self._disable_fallbacks(identity, exc)
                    self._service[identity] = None
                    return None
                except TicketTimeout as exc:
                    # The tool ran and may have minted before it hung; on
                    # macOS into API:<uuid>, which only a lookup can name.
                    self._record_collection_cache(
                        f"{cache_key}@collection", identity, None,
                        lambda: self.tickets.collection_cache_for(identity))
                    log.warning("cannot use the %s service identity: %s",
                                identity, exc)
                except TicketSourceError as exc:
                    log.warning("cannot use the %s service identity: %s",
                                identity, exc)
                except BaseException:
                    # Interrupted mid-mint. ticket() has already put the
                    # default back; the service cache it may have made -- root
                    # capable through root's .k5login -- must still be named
                    # for cleanup() before the interrupt propagates.
                    self._record_collection_cache(
                        f"{cache_key}@collection", identity, None,
                        lambda: self.tickets.collection_cache_for(identity))
                    raise
                else:
                    credential = Credential(
                        name=identity, login=identity, cache=ticket.cache,
                        principal=ticket.principal,
                        source="mu2edaq-kerberos (Vault keytab)")
                    ticket_cache = ticket.cache
                    log.info("acquired a ticket for the %s service identity (%s)",
                             identity, ticket.principal or "principal unknown")

            # Published last, _service last of all: a reader on the fast path
            # that sees the credential also sees its cache recorded for
            # cleanup. (A failed mint keeps the pre-recorded path: whatever
            # it left there is ours to destroy.)
            if credential is not None:
                self._caches[cache_key] = ticket_cache
                self._acquired.append(credential.principal or identity)
            self._service[identity] = credential
            return credential

    def _disable_fallbacks(self, identity: str,
                           exc: DefaultCacheGuardError) -> None:
        """Abandon the service identities for the rest of the run."""
        if self.fallbacks_disabled is None:
            self.fallbacks_disabled = FallbacksDisabled(
                kind=getattr(exc, "kind", "guard"), identity=identity,
                detail=str(exc), guidance=_guidance(exc))
            log.error("%s", self.fallbacks_disabled.note())
        self.settings.set("kerberos.use_service_keytabs", False,
                          source=f"runtime (default-cache guard: "
                                 f"{self.fallbacks_disabled.kind})")

    def warm_fallbacks(self) -> List[str]:
        """Mint every fallback identity once, before any worker starts.

        The lock already makes a lazy mint safe; doing them here as well means
        no mint -- and so no moment in which the default cache may be
        displaced -- overlaps worker threads running ssh under the ambient
        cache. Only called when kerberos.use_service_keytabs is on, and never
        for a simulated or describe-only session. Returns the identities that
        are now usable.
        """
        if not self.settings.get("kerberos.use_service_keytabs", True):
            return []
        usable: List[str] = []
        with self._mint_lock:
            for identity in self.available_identities():
                if self.fallbacks_disabled is not None:
                    break
                if self.service_credential(identity) is not None:
                    usable.append(identity)
        return usable

    # -- credential chains ----------------------------------------------------

    def operator_login(self) -> Optional[str]:
        """The account the operator's own principal logs in as.

        ``ssh.user`` when set, and otherwise **None** -- meaning "let ssh
        decide", from ssh_config and then its own default of the local
        username.

        This deliberately does NOT derive the account from the principal. That
        was tried, on the reasoning that ssh_config's ``User mu2edaq`` for the
        DAQ hosts was sending the personal ticket into the service account. The
        cluster disagrees: with a valid ``anorman@FNAL.GOV`` ticket, logging in
        as ``mu2edaq`` succeeds (that account's ``.k5login`` authorises the
        personal principal) and so does ``root``, while ``anorman`` is refused
        because no such account exists there. Deriving the login broke every
        host whose account name is not the principal's first component -- which
        is all of them here.

        The original symptom that prompted the derivation was the clobbered
        credential cache, nothing to do with the login.
        """
        return self.settings.get("ssh.user")

    def ambient_principal(self) -> Optional[str]:
        """The principal in the operator's default credential cache."""
        if self._ambient is _UNSET:
            self._ambient = self.tickets.default_principal()
        return self._ambient

    def operator_credential(self, root: bool = False) -> Credential:
        """The operator's own credential, for the general or root role."""
        role = "root" if root else "general"
        principal = self.settings.get(
            "kerberos.root_principal" if root else "kerberos.principal")
        if not principal:
            # No principal designated for this role, so the ambient ticket is
            # what will actually authenticate -- name it, rather than printing
            # "(principal unknown)" for the credential the run leads with.
            principal = self.ambient_principal()
        login = self.settings.get("ssh.root_user", "root") if root else \
            self.operator_login()
        cache = self._caches.get(role)
        designated = self.settings.get(
            "kerberos.root_principal" if root else "kerberos.principal")
        # A designated principal whose ticket has not been acquired would
        # otherwise print as "[ambient cache]" -- a claim about a cache that
        # may hold somebody else entirely.
        pending = "designated principal, not yet acquired" \
            if designated and not cache else None
        return Credential(name=role, login=login,
                          cache=cache, principal=principal,
                          source="operator ticket", primary=True,
                          pending=pending)

    def chain(self, root: bool = False, mint: Optional[bool] = None,
              candidates: bool = False) -> List[Credential]:
        """Credentials to try for a node, in order.

        **The operator's own principal is always first**, for root sessions as
        much as for ordinary ones. It is the identity the run belongs to: when
        it works, which is the normal case, nothing else is touched, and no
        service credential is used where a personal one would have done. The
        service identities are only ever fallbacks, and the run returns to the
        personal ticket afterwards -- see :meth:`restore_primary`.

        For a root session the fallbacks keep the ``root`` login and change
        only the *ticket*: authenticating as ``mu2edaq`` and logging in to the
        root account is a thing a node's ``root/.k5login`` can authorise, and
        it is the reason root has fallbacks at all.

        *mint* (default: :attr:`minting`) False never mints: identities not
        already acquired are left out, or -- with *candidates* -- included as
        :attr:`Credential.pending` placeholders, which is how a describe-only
        diagnostic shows what a run would try without acquiring any of it.
        """
        if mint is None:
            mint = self.minting
        primary = self.operator_credential(root=root)
        chain = [primary]
        if root and not self.settings.get("kerberos.root_fallback", True):
            return chain

        for identity in self.order_chain(self.available_identities()):
            if not self.settings.get("kerberos.use_service_keytabs", True):
                # An earlier identity repointed the default cache and could not
                # be put back. Stop the whole chain rather than working through
                # the remaining six doing the same damage.
                break
            if mint:
                credential = self.service_credential(identity)
            elif identity in self._service:
                credential = self._service[identity]
            elif candidates:
                credential = Credential(
                    name=identity, login=identity,
                    source="mu2edaq-kerberos (Vault keytab)",
                    pending="would be minted from Vault by a real run")
            else:
                credential = None
            if credential is None:
                continue
            # Root: same login, different ticket. Ordinary: the identity's own
            # account.
            chain.append(credential.for_login(primary.login) if root else credential)
        return chain

    def order_chain(self, identities: List[str]) -> List[str]:
        """Order the *fallback* identities, best guess first.

        On a fifty-node cluster the service identity that opened node 1 very
        probably opens node 2, so trying it before the others turns a walk down
        the whole list into a single attempt.

        This reorders the fallbacks among themselves only. The operator's own
        principal is prepended by :meth:`chain` afterwards and is never
        displaced -- a service identity having worked somewhere is not a reason
        to stop offering the personal ticket first.
        """
        with self._state_lock:
            successful = list(self._successful)
        promoted = [i for i in successful if i in identities]
        return promoted + [i for i in identities if i not in promoted]

    #: Principals that are service identities rather than a person. Used only
    #: to warn: a run driven by one of these is almost always an accident.
    SERVICE_PREFIXES = ("mu2edaq", "mu2eshift", "mu2edcs", "mu2edqm", "mu2eraw",
                        "mu2e-controlroom", "mu2e-teststand")

    def ambient_warning(self) -> Optional[str]:
        """A warning when the default credential cache is not the operator's.

        On macOS the credential cache is a *collection*, and a ticket minted
        for a service identity can become its default -- so a later run picks
        up that identity silently and every login is refused as the wrong
        principal. That is unreadable from the ssh error alone, so say it
        plainly before any connection is attempted.
        """
        ambient = self.ambient_principal()
        if not ambient:
            return None
        configured = self.settings.get("kerberos.principal")
        short = ambient.split("@")[0].split("/")[0]
        if short in self.SERVICE_PREFIXES:
            return (f"the default Kerberos cache holds the SERVICE identity "
                    f"{ambient!r}, not a personal principal. Every login will "
                    f"be attempted as that identity and will almost certainly "
                    f"be refused.\n"
                    f"    Fix it with:  kswitch -p <you>@FNAL.GOV   "
                    f"(or kdestroy --all && kinit <you>@FNAL.GOV)")
        if configured and ambient != configured:
            return (f"the default Kerberos cache holds {ambient!r} but "
                    f"kerberos.principal is {configured!r}; the run will use "
                    f"the configured one.")
        return None

    def restore_primary(self) -> Credential:
        """Return to the operator's own principal.

        Nothing in this project ever mutates the process environment or the
        default credential cache -- each ssh invocation is given its own
        KRB5CCNAME, and tickets are minted into private caches with
        ``get-kerberos-ticket --cache`` -- so the personal ticket is never
        displaced in the first place. This makes that explicit, and is called
        after each node so a service identity cannot become the run's working
        identity by accident.
        """
        return self.operator_credential()

    def note_success(self, credential: Credential) -> None:
        """Record that *credential* logged in somewhere."""
        name = credential.name
        if name in ("general", "root"):
            return
        # Called from every worker's ssh success callback, so the
        # remove-then-insert must be atomic: two threads interleaving it can
        # duplicate a name or raise ValueError on the remove.
        with self._state_lock:
            if name in self._successful:
                self._successful.remove(name)
            self._successful.insert(0, name)

    # -- use ---------------------------------------------------------------

    def environ_for(self, role: str = "general") -> Dict[str, str]:
        """Environment additions selecting this role's credential cache.

        An empty dict means "use the ambient cache", which is correct when no
        principal was designated for the role.
        """
        cache = self._caches.get(role)
        return {"KRB5CCNAME": ccache_name(cache)} if cache else {}

    def prepare(self) -> Dict[str, TicketInfo]:
        """Acquire every ticket the run will need, before any phase starts.

        Front-loading this is deliberate: an operator should be asked for their
        passwords once, at the start, not two hours in when phase 2 first needs
        root on a node that has just booted.
        """
        tickets: Dict[str, TicketInfo] = {}
        general = self.settings.get("kerberos.principal")
        root = self.settings.get("kerberos.root_principal")
        tickets["general"] = self.ensure(general, role="general")
        if root and root != general:
            tickets["root"] = self.ensure(root, role="root")
        elif general:
            # One principal doing both jobs; record it under both roles so the
            # report does not imply a root identity that was never used.
            tickets["root"] = tickets["general"]
            if root == general and "general" in self._caches:
                # And select its cache for root sessions too: without this a
                # root session named that principal while ssh was handed the
                # ambient cache, which may hold somebody else.
                self._caches["root"] = self._caches["general"]
        return tickets

    def cleanup(self) -> None:
        """Destroy the private caches created by this run.

        Tickets acquired on the operator's behalf should not outlive the run --
        they are, after all, root-capable.

        Each cache is named to kdestroy with ``-c``, not steered at it through
        KRB5CCNAME.  Two reasons.  A cache here may be a collection name
        (``API:<uuid>``) rather than a path, and ``FILE:API:<uuid>`` names
        nothing -- so on macOS every service ticket used to survive the run,
        sitting in the operator's collection where it can become the
        collection default and make the *next* run log in as a service
        identity.  And a bare kdestroy steered only by the environment
        destroys the *default* cache the moment that variable is not honoured
        the way we assume, which is the assumption this module has already
        been caught making once.

        A file cache outside the run's own directory is refused outright: the
        plausible foreign path is the operator's own.
        """
        # Under the mint lock: a mint in flight finishes (and records its cache)
        # before the sweep, and none starts after it.
        with self._mint_lock:
            self._closed = True
            caches = list(self._caches.values())
        cache_dir = self._cache_dir.resolve()
        seen = set()
        for cache in caches:
            target = ccache_name(cache)
            if target in seen:
                continue      # root and general sharing one principal's cache
            seen.add(target)
            if target.startswith("FILE:"):
                path = Path(target[len("FILE:"):])
                try:
                    ours = path.resolve().parent == cache_dir
                except OSError:
                    ours = False
                if not ours:
                    log.error("refusing to destroy %s: not a credential cache "
                              "this run created", path)
                    continue
            else:
                path = None
            try:
                LocalTransport(default_timeout=15,
                               env={"KRB5CCNAME": target}).run(
                    ["kdestroy", "-c", target], timeout=15)
            except TransportError as exc:
                log.debug("kdestroy %s: %s", target, exc)
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        # Sweep whatever kinit or kdestroy left beside the caches, so no ticket
        # material survives the run. Files only, and only in our own directory:
        # this is a clean-up, not a licence to delete a tree.
        try:
            for leftover in cache_dir.iterdir():
                if leftover.is_file():
                    leftover.unlink()
        except OSError:
            pass
        try:
            cache_dir.rmdir()
        except OSError:
            log.debug("left %s in place; it is not empty", cache_dir)
