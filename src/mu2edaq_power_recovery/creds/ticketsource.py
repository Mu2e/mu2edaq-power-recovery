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
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

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


class DefaultCacheGuardError(TicketSourceError):
    """The operator's default credential cache cannot be protected.

    Distinct from an ordinary per-identity failure (no keytab, a failed kinit)
    because it is a statement about the *run*, not about one identity: every
    further mint would carry the same risk. Callers decide what to do on the
    exception type, never on its message -- the message is for the operator
    and may be reworded freely.
    """

    #: Short machine-readable kind, for notes and events.
    kind = "guard"


class DefaultCacheDisplaced(DefaultCacheGuardError):
    """A mint repointed the default cache and it could not be put back."""

    kind = "displaced"

    def __init__(self, before: Optional[str], after: Optional[str],
                 message: Optional[str] = None):
        self.before = before
        self.after = after
        super().__init__(message or (
            f"minting a service ticket repointed the default credential "
            f"cache from {before!r} to {after!r}, and it could not be "
            f"restored"))


class DefaultCacheUnverifiable(DefaultCacheGuardError):
    """The default cache could not be read, so a mint could not be guarded.

    Raised *before* any mint command runs: without the principal that was in
    the default cache beforehand there is nothing to compare against
    afterwards and nothing to restore, so the mint would be unguarded in
    exactly the case the guard exists for.
    """

    kind = "unverifiable"

    def __init__(self, reason: str, message: Optional[str] = None):
        self.reason = reason
        super().__init__(message or (
            f"the default credential cache could not be read ({reason}), so "
            f"a service-ticket mint could not be checked for displacing it"))


class NoDefaultCache(str):
    """A :meth:`TicketSource.default_principal_status` reason meaning "klist
    ran, and there is simply no default credential cache".

    A ``str`` so it reads as the reason it is, and a distinct *type* so a
    caller can tell it apart from "klist is missing" or "klist output did not
    parse" without matching on the wording. It matters because it is the one
    unreadable state a mint can safely proceed from -- when the run's primary
    credentials do not use the default at all (see ``ticket(private_primary=)``).
    """


#: Lines from here on are a usage hint, not the failure. get-kerberos-ticket
#: appends a "Known identities:" listing after an error, so the last line of
#: its output is typically an indented organisation name -- which is what a
#: naive tail of the output reports as the cause.
_HINT_MARKERS = ("Known identities", "Usage:", "usage:")

#: Lines that actually say what went wrong, preferred over any other.
_ERROR_MARKERS = ("Error", "error", "Exception", "Traceback", "required",
                  "failed", "denied", "not found", "No such")


def summarise_tool_error(output: str) -> str:
    """The most informative line of a failed tool's output.

    Taking the last line is wrong here: the tool appends a hint listing after
    its error, so the tail is an indented organisation name and the real cause
    -- "The 'hvac' package is required" -- is buried above it.
    """
    lines = [l.rstrip() for l in (output or "").splitlines()]
    kept = []
    for line in lines:
        if any(marker in line for marker in _HINT_MARKERS):
            break
        if line.strip():
            kept.append(line.strip())
    if not kept:
        return ""
    for line in reversed(kept):
        if any(marker in line for marker in _ERROR_MARKERS):
            return line
    return kept[-1]


@dataclass
class ServiceTicket:
    """A ticket minted for one service identity."""

    identity: str
    #: A KRB5CCNAME value: a file path, or a collection name like API:<uuid>.
    cache: Any
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
        None when it cannot be read; :meth:`default_principal_status` says why.
        """
        return TicketSource.default_principal_status()[0]

    @staticmethod
    def default_principal_status() -> Tuple[Optional[str], Optional[str]]:
        """``(principal, None)``, or ``(None, reason)`` when it cannot be read.

        The reason distinguishes the three ways the read fails, because they
        need different fixes: klist is missing (install the Kerberos client
        tools), there is no default cache (kinit), or klist answered in a form
        this parser does not recognise (a bug here, to be reported).
        """
        env = {k: v for k, v in os.environ.items() if k != "KRB5CCNAME"}
        try:
            result = subprocess.run(["klist"], env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=20,
                                    check=False)
        except FileNotFoundError:
            return None, "klist is not installed or not on PATH"
        except subprocess.TimeoutExpired:
            return None, "klist did not answer within 20s"
        except (OSError, subprocess.SubprocessError) as exc:
            return None, f"klist could not be run: {exc}"
        text = result.stdout.decode("utf-8", "replace")
        for line in text.splitlines():
            if "principal:" in line.lower():
                principal = line.split(":", 1)[1].strip()
                if principal:
                    return principal, None
        if result.returncode != 0 or not text.strip():
            detail = result.stderr.decode("utf-8", "replace").strip()
            detail = detail.splitlines()[-1] if detail else \
                f"klist exited {result.returncode}"
            return None, NoDefaultCache(
                f"there is no default credential cache ({detail})")
        return None, ("klist listed a default cache but named no principal "
                      "in a form this tool can parse")

    @staticmethod
    def _python_env() -> dict:
        """Pin the interpreter mu2edaq-kerberos runs its Python helpers with.

        Its shell script does ``PYTHON=${PYTHON:-python3}``, so without this it
        picks up whatever ``python3`` the ambient PATH happens to give -- which
        is the right venv only when one is activated, and the system Python
        (with no hvac, so no Vault access) otherwise. Our own interpreter is
        guaranteed to have hvac, since this project depends on it.
        """
        return {"PYTHON": sys.executable}

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
            result = self._run(command, ["identities", *extra], timeout=60,
                               env={**os.environ, **self._python_env()})
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

    def ticket(self, identity: str, cache: Path, timeout: int = 120,
               private_primary: bool = False) -> ServiceTicket:
        """Mint a ticket for *identity* into *cache*.

        Delegates entirely to ``get-kerberos-ticket``, which handles the keytab
        -- fetching it, writing it out with restrictive permissions, running
        kinit, and removing it. Nothing in this project ever sees the keytab
        bytes, which is the point of going through the package.

        The default credential cache must be readable first, so a displacement
        can be detected and undone -- with one exception. *private_primary*
        says every primary role of the run (general and root) is served by a
        private cache the run created, so nothing of the run's depends on the
        default. Then, and only when klist reports that there is *no* default
        (:class:`NoDefaultCache`, not a tool or parse failure), the mint may
        proceed; afterwards a default that has appeared naming this service
        identity is destroyed, and :class:`DefaultCacheDisplaced` raised if it
        cannot be. That is the fresh macOS login whose operator ticket was
        minted by ``--principal`` into a FILE: cache, so the collection has no
        default at all.
        """
        command = self.resolve(TICKET_COMMAND)
        if command is None:
            raise TicketSourceError(self.unavailable_reason())

        cache.parent.mkdir(parents=True, exist_ok=True)
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
        env = {**os.environ, "KRB5CCNAME": target, **self._python_env()}
        # The guard's precondition. Without the principal that is in the
        # default cache now, a displacement can be neither detected nor
        # undone -- so refuse before get-kerberos-ticket runs, rather than
        # minting unguarded in exactly the case the guard exists for.
        before, why = self.default_principal_status()
        if before is None:
            if not (private_primary and isinstance(why, NoDefaultCache)):
                raise DefaultCacheUnverifiable(why or "no principal reported")
            log.info("no default credential cache, and every primary "
                     "credential of this run is a private cache: minting %s, "
                     "then checking that no service default was left behind",
                     identity)

        def settle() -> None:
            if before:
                self._restore_default_if_displaced(identity, before)
            else:
                self._clear_service_default(identity)

        timed_out: Optional[TicketSourceError] = None
        try:
            result = self._run(command, [identity, "--cache", target, *extra],
                               timeout=timeout, env=env)
        except subprocess.TimeoutExpired as exc:
            # Held back rather than raised here. The command *ran*, so it may
            # have repointed the default cache before it hung -- and returning
            # a bare timeout would leave the pointer moved and let the chain
            # try the next six identities under the wrong identity.
            timed_out = TicketSourceError(
                f"get-kerberos-ticket timed out after {timeout}s for {identity}")
            timed_out.__cause__ = exc
            result = None
        except OSError as exc:
            # Never started, so nothing can have been repointed.
            raise TicketSourceError(
                f"could not run {command} for {identity}: {exc}") from exc
        except BaseException:
            # Interrupted mid-mint -- in practice KeyboardInterrupt, which is
            # also what the SIGTERM handler raises. The command was running,
            # so it may already have repointed the default: put it back before
            # the interrupt propagates. A failed restore is logged, never
            # raised, so it cannot replace the interrupt the caller is
            # unwinding for.
            try:
                settle()
            except DefaultCacheGuardError as exc:
                log.error("%s", exc)
            raise

        settle()
        if timed_out is not None:
            raise timed_out

        if result.returncode != 0:
            detail = (result.stderr or result.stdout).decode("utf-8", "replace")
            raise TicketSourceError(
                f"get-kerberos-ticket failed for {identity}: "
                f"{summarise_tool_error(detail) or f'exit {result.returncode}'}")
        if cache.exists():
            return ServiceTicket(identity=identity, cache=str(cache),
                                 principal=self._principal_of(cache))

        # No file, but the mint reported success -- on macOS the ticket went
        # into the API: collection instead, because Heimdal's kinit does not
        # honour a FILE: KRB5CCNAME. It is still perfectly usable; it just has
        # a ccache name rather than a path.
        #
        # Looked up by identity, not by diffing the collection before and
        # after: a cache for this identity very likely already exists from an
        # earlier run, in which case the mint refreshes it in place and nothing
        # "appears".
        for principal, name in self.collection().items():
            if principal.split("/")[0] == identity:
                log.debug("%s is in the credential collection as %s",
                          identity, name)
                return ServiceTicket(identity=identity, cache=name,
                                     principal=principal)

        raise TicketSourceError(
            f"get-kerberos-ticket reported success for {identity} but the "
            f"ticket is neither at {cache} nor in the credential collection. "
            f"Check that kinit honours KRB5CCNAME={target}.")

    def _restore_default_if_displaced(self, identity: str,
                                      before: Optional[str]) -> None:
        """Put the default credential cache back if minting moved it.

        Raises :class:`DefaultCacheDisplaced` when it cannot. Expected on
        macOS. Heimdal keeps credential caches in a *collection*
        and makes a freshly minted one the collection default, whatever
        KRB5CCNAME or --cache said -- so minting a service ticket silently
        repoints "the default ticket" at it and every later login runs as that
        identity.

        The operator's ticket is not destroyed, only displaced: it is still in
        the collection. So put the pointer back rather than giving up on
        service identities altogether.
        """
        if not before:
            return
        after = self.default_principal()
        if after == before:
            return
        if self.restore_default(before):
            log.debug("default credential cache moved to %s while minting "
                      "%s; restored to %s", after, identity, before)
            return
        raise DefaultCacheDisplaced(before, after,
            f"minting a ticket for {identity} repointed the default "
            f"credential cache from {before!r} to {after!r}, and it "
            f"could not be restored. Every later login would run as "
            f"the wrong identity. Run 'kswitch -p {before}' (or "
            f"'kinit {before}') and re-run with "
            f"kerberos.use_service_keytabs: false.")

    def _clear_service_default(self, identity: str) -> None:
        """After a mint from no default: remove a service default it created.

        There was no default before, so there is nothing to switch back to;
        undoing the mint's side effect means destroying the cache it made the
        default. Only a default naming *identity* is touched -- anything else
        that appeared (the operator running kinit in another terminal) is not
        ours. Raises :class:`DefaultCacheDisplaced` when it cannot be undone.
        """
        after = self.default_principal()
        if not after:
            return
        if after.split("@")[0].split("/")[0] != identity:
            log.info("a default credential cache for %s appeared while minting "
                     "%s; not ours, left alone", after, identity)
            return
        if self.destroy_default(after):
            log.info("minting %s made it the default credential cache where "
                     "there was none; destroyed it", identity)
            return
        raise DefaultCacheDisplaced(None, after,
            f"minting a ticket for {identity} left {after!r} as the default "
            f"credential cache where there was none, and it could not be "
            f"destroyed. Anything using the default cache would run as that "
            f"identity. Run 'kdestroy -p {after}' and re-run with "
            f"kerberos.use_service_keytabs: false.")

    def destroy_default(self, principal: str) -> bool:
        """Destroy the collection cache holding *principal*, by name.

        Always ``kdestroy -c <name>``, never a bare kdestroy: naming the cache
        is what makes this unable to hit the operator's. Returns whether the
        default no longer names *principal*.
        """
        name = self.collection().get(principal)
        if not name:
            log.warning("cannot find %s in the credential collection to "
                        "destroy it", principal)
            return False
        env = {k: v for k, v in os.environ.items() if k != "KRB5CCNAME"}
        try:
            subprocess.run(["kdestroy", "-c", name], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=20, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("kdestroy -c %s failed: %s", name, exc)
            return False
        return self.default_principal() != principal

    @staticmethod
    def collection() -> dict:
        """principal -> ccache name for every cache in the collection.

        macOS keeps credential caches in an API: collection rather than as
        files, so a ticket minted there has no path to point at -- but it does
        have a ccache name, which is what KRB5CCNAME actually wants.
        """
        env = {k: v for k, v in os.environ.items() if k != "KRB5CCNAME"}
        try:
            result = subprocess.run(["klist", "-l"], env=env,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, timeout=20,
                                    check=False)
        except (OSError, subprocess.SubprocessError):
            return {}
        caches = {}
        for line in result.stdout.decode("utf-8", "replace").splitlines():
            parts = line.split()
            # "<principal> <CACHETYPE:name> <expiry...>"; the header row and
            # any blank lines have no ':' in the second column.
            if len(parts) >= 2 and "@" in parts[0] and ":" in parts[1]:
                caches[parts[0]] = parts[1]
        return caches

    def restore_default(self, principal: str) -> bool:
        """Point the default credential cache back at *principal*.

        ``kswitch -p`` selects an existing cache within the collection; it
        mints nothing and needs no credentials. Returns whether the default
        actually came back, rather than trusting the exit status -- the whole
        reason this code exists is a tool that reported success while moving
        the default.
        """
        kswitch = shutil.which("kswitch")
        if not kswitch:
            log.warning("kswitch is not available; cannot restore the default "
                        "credential cache to %s", principal)
            return False
        try:
            subprocess.run([kswitch, "-p", principal],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=20, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("kswitch failed: %s", exc)
            return False
        return self.default_principal() == principal

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
