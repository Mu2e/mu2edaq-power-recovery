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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..transport.base import TransportError
from ..transport.local import LocalTransport

log = logging.getLogger(__name__)


class KerberosError(RuntimeError):
    """No usable ticket could be obtained for a required principal."""


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
