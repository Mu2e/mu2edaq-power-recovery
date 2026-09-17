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
"""
from __future__ import annotations

import getpass
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..transport.base import TransportError
from ..transport.local import LocalTransport
from .ticketsource import TicketSource, TicketSourceError

log = logging.getLogger(__name__)


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

    def environ(self) -> Dict[str, str]:
        """Environment additions selecting this credential's cache."""
        return {"KRB5CCNAME": f"FILE:{self.cache}"} if self.cache else {}

    def for_login(self, login: Optional[str]) -> "Credential":
        """This credential's ticket, used to log in as a different account.

        Which ticket authenticates and which account is logged into are
        separate things. Root access through a service identity is exactly
        this: authenticate as, say, ``mu2edaq``, and log in to the ``root``
        account, which the node's ``root/.k5login`` may authorise.
        """
        return replace(self, login=login)

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "login": self.login,
                "principal": self.principal, "source": self.source}

    def __str__(self) -> str:
        return f"{self.name} ({self.principal or self.login or 'ambient'})"


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
_PRINCIPAL_RE = re.compile(r"Default principal:\s*(\S+)", re.I)
_CACHE_RE = re.compile(r"Ticket cache:\s*(\S+)", re.I)


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
        self._caches: Dict[str, Path] = {}
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

    # -- inspection --------------------------------------------------------

    def _klist(self, cache: Optional[Path] = None) -> TicketInfo:
        env = {"KRB5CCNAME": f"FILE:{cache}"} if cache else {}
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
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 4 and "/" in parts[0] and ":" in parts[1]:
                for fmt in ("%m/%d/%y %H:%M:%S", "%m/%d/%Y %H:%M:%S",
                            "%m/%d/%y %H:%M", "%m/%d/%Y %H:%M"):
                    try:
                        import datetime as _dt
                        return _dt.datetime.strptime(f"{parts[2]} {parts[3]}", fmt).timestamp()
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
            if not info.valid:
                raise KerberosError(
                    "no valid Kerberos ticket in the default credential cache.\n"
                    "Run 'kinit <your principal>' first, or pass "
                    "--principal <principal> to have this tool acquire one."
                )
            log.info("using ambient Kerberos ticket for %s", info.principal)
            return info

        cache = self.cache_for(principal)
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
            raise KerberosError(f"kinit appeared to succeed but no valid ticket "
                                f"is present for {principal}")
        self._acquired.append(principal)
        return info

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

        cache.parent.mkdir(parents=True, exist_ok=True)
        print(f"\nA Kerberos ticket is needed for the {role} principal.")
        try:
            password = getpass.getpass(f"  Password for {principal}: ")
        except (EOFError, KeyboardInterrupt) as exc:
            raise KerberosError(f"password entry cancelled for {principal}") from exc
        if not password:
            raise KerberosError(f"empty password given for {principal}")

        runner = LocalTransport(default_timeout=60,
                                env={"KRB5CCNAME": f"FILE:{cache}"})
        try:
            result = runner.run(["kinit", principal], timeout=60, input_text=password)
        finally:
            del password
        if not result.ok:
            detail = (result.stderr or result.stdout).strip().splitlines()
            raise KerberosError(f"kinit failed for {principal}: "
                                f"{detail[-1] if detail else 'unknown error'}")
        log.info("acquired %s ticket for %s", role, principal)

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
            discovered = self.tickets.identities()
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
        """
        if identity in self._service:
            return self._service[identity]

        credential: Optional[Credential] = None
        if not self.tickets.available:
            log.debug("%s", self.tickets.unavailable_reason())
        else:
            cache = self.cache_for(f"svc-{identity}")
            try:
                ticket = self.tickets.ticket(identity, cache)
            except TicketSourceError as exc:
                log.warning("cannot use the %s service identity: %s", identity, exc)
            else:
                credential = Credential(
                    name=identity, login=identity, cache=ticket.cache,
                    principal=ticket.principal,
                    source="mu2edaq-kerberos (Vault keytab)")
                self._caches[f"svc-{identity}"] = ticket.cache
                self._acquired.append(ticket.principal or identity)
                log.info("acquired a ticket for the %s service identity (%s)",
                         identity, ticket.principal or "principal unknown")

        self._service[identity] = credential
        return credential

    # -- credential chains ----------------------------------------------------

    def operator_credential(self, root: bool = False) -> Credential:
        """The operator's own credential, for the general or root role."""
        role = "root" if root else "general"
        principal = self.settings.get(
            "kerberos.root_principal" if root else "kerberos.principal")
        login = self.settings.get("ssh.root_user", "root") if root else \
            self.settings.get("ssh.user")
        return Credential(name=role, login=login,
                          cache=self._caches.get(role), principal=principal,
                          source="operator ticket", primary=True)

    def chain(self, root: bool = False) -> List[Credential]:
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
        """
        primary = self.operator_credential(root=root)
        chain = [primary]
        if root and not self.settings.get("kerberos.root_fallback", True):
            return chain

        for identity in self.order_chain(self.available_identities()):
            credential = self.service_credential(identity)
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
        promoted = [i for i in self._successful if i in identities]
        return promoted + [i for i in identities if i not in promoted]

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
        return {"KRB5CCNAME": f"FILE:{cache}"} if cache else {}

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
        return tickets

    def cleanup(self) -> None:
        """Destroy the private caches created by this run.

        Tickets acquired on the operator's behalf should not outlive the run --
        they are, after all, root-capable.
        """
        for cache in self._caches.values():
            try:
                LocalTransport(default_timeout=15,
                               env={"KRB5CCNAME": f"FILE:{cache}"}).run(["kdestroy"], timeout=15)
            except TransportError:
                pass
            try:
                Path(cache).unlink(missing_ok=True)
            except OSError:
                pass
        try:
            self._cache_dir.rmdir()
        except OSError:
            pass
