"""Service tickets, obtained through the mu2edaq-kerberos package.

The Mu2e DAQ service keytabs live in Vault under
``td/scd/experiments/mu2e/kerberos/<identity>``, and ``mu2edaq-kerberos``
already knows how to turn one into a Kerberos ticket: it reads the secret,
writes the keytab to a private temporary file, runs ``kinit -kt`` into a named
credential cache, and deletes the keytab again. That is a non-trivial amount of
careful handling, and it is the package's job.

So this module does not reimplement any of it. It is a thin adapter that
locates that package's commands and calls them:

* ``vault-client identities`` -- which identities exist. It reads the package's
  own identity-set configuration and needs no Vault token, so discovery works
  before anything has authenticated.
* ``get-kerberos-ticket <identity> --cache <path>`` -- mint a ticket for one
  identity into a cache of our choosing, which is exactly the interface a
  per-identity credential chain needs.

Keeping the boundary here means that if the keytab layout in Vault changes,
this project needs no change at all -- only the package that owns that layout.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Sequence

log = logging.getLogger(__name__)

#: Commands provided by mu2edaq-kerberos that this project uses.
TICKET_COMMAND = "get-kerberos-ticket"
IDENTITIES_COMMAND = "vault-client"

#: Where mu2edaq-kerberos is checked out relative to this project, when both
#: are submodules of mu2edaq-main. Tried after PATH so an installed copy always
#: wins over a sibling checkout.
SIBLING_CHECKOUT = Path("..") / "mu2edaq-kerberos"


class TicketSourceError(RuntimeError):
    """mu2edaq-kerberos is unavailable, or could not mint a ticket."""


class DefaultCacheUnreadable(TicketSourceError):
    """The operator's default credential cache could not be inspected.

    Distinct from "there is no default cache", which is an answer.  This means
    we could not look, and a check that cannot be made must never be mistaken
    for one that passed -- ``None == None`` would have silently switched the
    clobber guard off on exactly the sort of unusual Kerberos installation
    that makes it necessary.
    """


class DefaultCacheClobbered(TicketSourceError):
    """Minting a service ticket disturbed the operator's own credentials.

    A separate type because the *decision* it drives -- abandon the service
    identities for the whole run -- is too important to hang on a substring of
    an error message, which the next person to reword the message would break
    without noticing.
    """


@dataclass
class ServiceTicket:
    """A ticket minted for one service identity."""

    identity: str
    cache: Path
    principal: Optional[str] = None

    def as_dict(self) -> dict:
        return {"identity": self.identity, "cache": str(self.cache),
                "principal": self.principal}


class TicketSource:
    """Adapter over the mu2edaq-kerberos command-line tools."""

    def __init__(self, settings: Any, project_root: Optional[Path] = None):
        self.settings = settings
        self.project_root = project_root or Path(__file__).resolve().parents[3]
        self._resolved: dict = {}
        self._available: Optional[bool] = None

    # -- locating the package -------------------------------------------------

    def _candidates(self, command: str) -> List[Path]:
        """Places to look for one of the package's commands, best first."""
        configured = self.settings.get(f"kerberos.{command.replace('-', '_')}_command")
        out: List[Path] = []
        if configured:
            out.append(Path(str(configured)).expanduser())
        found = shutil.which(command)
        if found:
            out.append(Path(found))
        sibling = (self.project_root / SIBLING_CHECKOUT).resolve()
        # An editable install of the sibling checkout puts its entry points in
        # its own venv, which is not on our PATH when we are running from ours.
        out.append(sibling / "venv" / "bin" / command)
        out.append(sibling / "venv" / "Scripts" / f"{command}.exe")
        if command == TICKET_COMMAND:
            # Last resort: the shell script itself, which is what the console
            # entry point execs anyway.
            out.append(sibling / "src" / "mu2edaq_kerberos" / "get-kerberos-ticket.sh")
        return out

    def resolve(self, command: str) -> Optional[Path]:
        """Path to one of the package's commands, or None if it is not installed."""
        if command in self._resolved:
            return self._resolved[command]
        chosen: Optional[Path] = None
        for candidate in self._candidates(command):
            if candidate.exists() and os.access(candidate, os.X_OK):
                chosen = candidate
                break
        if chosen is None:
            log.debug("%s not found; service identities are unavailable", command)
        else:
            log.debug("using %s from %s", command, chosen)
        self._resolved[command] = chosen
        return chosen

    @property
    def available(self) -> bool:
        """True when mu2edaq-kerberos can be used to mint service tickets."""
        if self._available is None:
            self._available = self.resolve(TICKET_COMMAND) is not None
        return self._available

    def unavailable_reason(self) -> str:
        sibling = (self.project_root / SIBLING_CHECKOUT).resolve()
        return (
            f"the mu2edaq-kerberos package was not found, so the Mu2e service "
            f"identities cannot be used. Install it, put its '{TICKET_COMMAND}' "
            f"on PATH, set kerberos.get_kerberos_ticket_command to its path, or "
            f"check it out at {sibling}."
        )

    # -- running the commands -------------------------------------------------

    def _run(self, command: Path, args: Sequence[str], timeout: int = 120,
             env: Optional[dict] = None) -> subprocess.CompletedProcess:
        argv = [str(command), *args]
        log.debug("running %s", " ".join(argv))
        return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout, check=False, env=env)

    @staticmethod
    def default_principal() -> Optional[str]:
        """The principal in the *default* credential cache, whatever it is.

        Read with KRB5CCNAME removed from the environment, so this reports the
        operator's own cache rather than whichever one a caller has selected.

        Returns None when ``klist`` ran and found no principal -- an empty or
        absent default cache, which is a legitimate state and a real answer.
        Raises :class:`DefaultCacheUnreadable` when klist could not be run at
        all: missing, unexecutable, or hung.  That is *not* an answer, and
        returning None for it would make the before/after comparison in
        :meth:`ticket` compare None with None and pass, disarming the one
        check standing between a run and the operator's own ticket.
        """
        env = {k: v for k, v in os.environ.items() if k != "KRB5CCNAME"}
        try:
            result = subprocess.run(["klist"], env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=20,
                                    check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise DefaultCacheUnreadable(
                f"could not run klist to read the default credential cache: "
                f"{exc}") from exc
        for line in result.stdout.decode("utf-8", "replace").splitlines():
            if "principal:" in line.lower():
                return line.split(":", 1)[1].strip()
        # klist exits non-zero for an absent or empty cache and says so on
        # stderr.  That is an answer -- there is nothing there -- so it is
        # reported as None rather than as a failure to look.
        return None

    def identities(self) -> List[str]:
        """Every service identity mu2edaq-kerberos knows about.

        ``vault-client identities`` reads the package's own identity-set
        configuration, so this works with no Vault token and before anything
        has authenticated -- which is what makes it usable for building the
        credential chain up front.
        """
        command = self.resolve(IDENTITIES_COMMAND)
        if command is None:
            return []
        extra = list(self.settings.get("kerberos.vault_client_args", []) or [])
        try:
            result = self._run(command, ["identities", *extra], timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("could not list the Mu2e service identities: %s", exc)
            return []
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
            log.warning("vault-client identities exited %s: %s", result.returncode,
                        detail[-1] if detail else "")
            return []
        return [line.strip() for line
                in result.stdout.decode("utf-8", "replace").splitlines()
                if line.strip() and not line.startswith(" ")]

    def ticket(self, identity: str, cache: Path,
               timeout: int = 120) -> ServiceTicket:
        """Mint a ticket for *identity* into *cache*.

        Delegates entirely to ``get-kerberos-ticket``, which handles the keytab
        -- fetching it, writing it out with restrictive permissions, running
        kinit, and removing it. Nothing in this project ever sees the keytab
        bytes, which is the point of going through the package.
        """
        command = self.resolve(TICKET_COMMAND)
        if command is None:
            raise TicketSourceError(self.unavailable_reason())

        # 0700: this directory holds TGTs.  mkdtemp already makes it so; the
        # mode matters for a cache_dir supplied by a caller.
        cache.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        extra = list(self.settings.get("kerberos.vault_client_args", []) or [])

        # An explicit FILE: type, not a bare path. macOS ships Heimdal, whose
        # default cache type is API:, and which does not read a bare path as a
        # file cache -- so a bare KRB5CCNAME sends the ticket to the operator's
        # *default* cache and overwrites their own credentials. That is exactly
        # what happened before this was fixed: seven service identities in a
        # row each clobbered the personal ticket.
        target = f"FILE:{cache}"

        # Belt and braces. --cache is what the tool documents, and KRB5CCNAME
        # in its environment catches any path through it that does not honour
        # the flag. Neither alone is enough to guarantee the default cache is
        # left alone, and that guarantee is the point.
        env = {**os.environ, "KRB5CCNAME": target}
        try:
            before = self.default_principal()
        except DefaultCacheUnreadable as exc:
            # Refusing here costs the operator the service-identity fallbacks
            # and nothing else -- their own principal is position 0 of every
            # chain and is unaffected. Minting anyway would spend their TGT on
            # a guarantee we cannot make.
            raise TicketSourceError(
                f"refusing to mint a ticket for {identity}: the default "
                f"credential cache cannot be read ({exc}), so the before/after "
                f"comparison that protects your own ticket cannot be made. "
                f"This is the operation that destroyed it once already. Set "
                f"kerberos.use_service_keytabs: false to run without the "
                f"service-identity fallbacks.") from exc

        timed_out: Optional[TicketSourceError] = None
        try:
            result = self._run(command, [identity, "--cache", target, *extra],
                               timeout=timeout, env=env)
        except subprocess.TimeoutExpired as exc:
            # Held back rather than raised: the command *ran*, so it may have
            # replaced the default cache before it hung. Reporting a timeout
            # while the operator's ticket is quietly gone -- and then trying
            # the next six identities -- is how one bad mint became seven.
            timed_out = TicketSourceError(
                f"get-kerberos-ticket timed out after {timeout}s for {identity}")
            timed_out.__cause__ = exc
            result = None
        except OSError as exc:
            # Never started, so nothing can have been touched.
            raise TicketSourceError(
                f"could not run {command} for {identity}: {exc}") from exc

        self._assert_default_cache_survived(identity, before)
        if timed_out is not None:
            raise timed_out

        if result.returncode != 0:
            detail = (result.stderr or result.stdout).decode("utf-8", "replace")
            lines = [l for l in detail.strip().splitlines() if l.strip()]
            raise TicketSourceError(
                f"get-kerberos-ticket failed for {identity}: "
                f"{lines[-1] if lines else f'exit {result.returncode}'}")
        if not cache.exists():
            raise TicketSourceError(
                f"get-kerberos-ticket reported success for {identity} but wrote "
                f"no credential cache at {cache}. The ticket went somewhere "
                f"else -- check that kinit honours KRB5CCNAME={target}.")
        return ServiceTicket(identity=identity, cache=cache,
                             principal=self._principal_of(cache))

    def _assert_default_cache_survived(self, identity: str,
                                       before: Optional[str]) -> None:
        """Raise unless the operator's default cache is as it was.

        Both failure directions count as a clobber.  A cache that can no
        longer be read is not evidence that it survived -- it is the absence
        of evidence -- and the cost of guessing wrong is the operator's TGT
        and every login that follows.
        """
        try:
            after = self.default_principal()
        except DefaultCacheUnreadable as exc:
            raise DefaultCacheClobbered(
                f"after minting a ticket for {identity} the default "
                f"credential cache could no longer be read ({exc}). Treating "
                f"that as a clobber and refusing to use service identities. "
                f"Check 'klist'; if your own ticket is gone, run "
                f"'kinit {before or '<your principal>'}'.") from exc
        if before != after:
            # Refuse to carry on quietly: the operator's own ticket has just
            # been replaced, every later login would run as the wrong identity,
            # and no amount of retrying fixes a destroyed TGT.
            raise DefaultCacheClobbered(
                f"minting a ticket for {identity} replaced the default "
                f"credential cache ({before!r} -> {after!r}). Refusing to use "
                f"service identities. Run 'kinit {before or '<your principal>'}' "
                f"to restore your own ticket, and report this -- "
                f"get-kerberos-ticket is not honouring --cache.")

    @staticmethod
    def _principal_of(cache: Path) -> Optional[str]:
        """Read the principal back out of a credential cache, for the report."""
        try:
            result = subprocess.run(
                ["klist"], env={**os.environ, "KRB5CCNAME": f"FILE:{cache}"},  # noqa: E501
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=20, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        for line in result.stdout.decode("utf-8", "replace").splitlines():
            if "Default principal:" in line:
                return line.split(":", 1)[1].strip()
        return None
