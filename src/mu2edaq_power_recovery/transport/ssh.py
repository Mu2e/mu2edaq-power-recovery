"""SSH execution against DAQ nodes, through a gateway when required.

Two facts drive the design:

*  The operator is outside the DAQ networks.  Only the gateways are reachable
   directly; everything else is reached with ``-J <gateway>`` (ProxyJump),
   which keeps the hop in ssh rather than in a nest of ``ssh host1 ssh host2``
   quoting.
*  Authentication is Kerberos (GSSAPI) with delegation, so the ticket that
   opened the gateway session is what authenticates the second hop.  There is
   no password path: ``BatchMode=yes`` makes a missing ticket a fast, legible
   failure instead of a worker thread blocked on a prompt nobody can see.

Root access uses a separate principal (``kerberos.root_principal``) and a
separate ssh login (``ssh.root_user``); the two are independent because the
site's root principal is not necessarily the root account name.

Credential chains
-----------------
No single identity can log in to every DAQ node. The operator's own principal
covers the machines they have an account on; the Mu2e service identities --
``mu2edaq``, ``mu2eshift`` and the rest, whose keytabs mu2edaq-kerberos pulls
from Vault -- cover the others, and which one works varies by host.

So a transport is given an ordered *chain* of credentials rather than one, and
tries them in turn until a session opens. Two things keep that from being
expensive:

* Only an *authentication* failure advances the chain. A refused connection or
  a timeout means the host is down, and trying six more identities against a
  dead machine would cost six more connect timeouts and learn nothing.
* The credential that worked is remembered per host, so the remaining twenty
  commands against that node go straight to it, and is promoted in the chain
  for every later node -- on a cluster, the identity that opened one node very
  probably opens the next.
"""
from __future__ import annotations

import logging
import shlex
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

from .base import Command, CommandResult, TimeoutExpired, Transport, TransportError, as_string
from .local import LocalTransport

log = logging.getLogger(__name__)


class SSHError(TransportError):
    """The SSH connection itself failed (255, or the client could not start)."""


#: ssh exits 255 for its own errors; a remote command exiting 255 is
#: indistinguishable, but no check here runs anything that legitimately does.
SSH_FAILURE_RC = 255

#: Messages that mean "this identity may not log in here" -- worth trying the
#: next credential.
_AUTH_FAILURE_PATTERNS = (
    "permission denied",
    "no kerberos credentials",
    "credentials cache",
    "gssapi",
    "server not found in kerberos database",
    "authentication failed",
    "no supported authentication methods",
    "user does not exist",
    "invalid user",
)

#: Messages that mean the host itself is unreachable. Trying another identity
#: would cost another full connect timeout and could not possibly help, so the
#: chain stops here.
_UNREACHABLE_PATTERNS = (
    "connection refused",
    "connection timed out",
    "connection closed",
    # sshd dropping the connection during key exchange means MaxStartups or a
    # rate limiter, not a credential problem. Advancing the chain here would
    # send six more connections at a server that is already refusing them.
    "connection reset by peer",
    "kex_exchange_identification",
    "too many authentication failures",
    "no route to host",
    "network is unreachable",
    "host is down",
    "name or service not known",
    "could not resolve hostname",
    "operation timed out",
)


def classify_ssh_failure(stderr: str) -> str:
    """'auth', 'unreachable' or 'unknown' for a failed ssh invocation.

    Unreachability is tested first: a jump host that is down reports both
    "Connection refused" and, further down, a message about credentials, and
    reading that as an authentication problem would send the chain through
    every identity against a machine that is simply off.

    An unrecognised failure is 'unknown', which advances the chain -- the cost
    is one extra attempt, and the alternative is refusing to try an identity
    that might have worked because ssh phrased its complaint unexpectedly.
    """
    lowered = (stderr or "").lower()
    if any(pattern in lowered for pattern in _UNREACHABLE_PATTERNS):
        return "unreachable"
    if any(pattern in lowered for pattern in _AUTH_FAILURE_PATTERNS):
        return "auth"
    return "unknown"


class SSHTransport(Transport):
    """Commands on one remote host.

    Instances are cheap and stateless -- each :meth:`run` is one ssh
    invocation.  A persistent ControlMaster would be faster, but a recovery
    run touches a host a handful of times over minutes, and a shared control
    socket turns one wedged connection into a stuck fleet.
    """

    def __init__(self, host: str,
                 user: Optional[str] = None,
                 jump: Optional[str] = None,
                 options: Optional[Sequence[str]] = None,
                 connect_timeout: int = 10,
                 command_timeout: int = 120,
                 local: Optional[LocalTransport] = None,
                 credentials: Optional[Sequence[Any]] = None,
                 on_success: Optional[Callable[[Any], None]] = None,
                 max_capture: int = 65536):
        self.host = host
        self.user = user
        self.jump = jump
        self.options = list(options or [])
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout
        self.local = local or LocalTransport(default_timeout=command_timeout)
        self.max_capture = max_capture
        #: Ordered credentials to try. Empty means "use whatever ssh and the
        #: ambient credential cache would do", which is the right behaviour for
        #: a run where no principal was designated.
        self.credentials: List[Any] = list(credentials or [])
        #: Called with the credential that first worked, so the factory can
        #: promote it for subsequent hosts.
        self.on_success = on_success
        #: The credential that opened a session here. Once set, every later
        #: command on this host uses it directly.
        self.working: Optional[Any] = None
        self._runners: Dict[str, LocalTransport] = {}
        #: What each credential was tried and rejected for, for the report.
        self.attempts: List[Dict[str, Any]] = []

    # -- command construction ---------------------------------------------

    def _target(self, user: Optional[str]) -> str:
        login = user or self.user
        return f"{login}@{self.host}" if login else self.host

    def _runner(self, credential: Optional[Any]) -> LocalTransport:
        """A local transport whose environment selects *credential*'s cache.

        This is what actually makes a designated principal take effect: without
        KRB5CCNAME in the ssh process's environment, a ticket minted into a
        private cache is invisible to it and ssh silently uses the ambient one.
        """
        if credential is None or not getattr(credential, "cache", None):
            return self.local
        key = str(credential.cache)
        if key not in self._runners:
            self._runners[key] = LocalTransport(
                default_timeout=self.command_timeout,
                max_capture=self.max_capture,
                env=credential.environ(),
            )
        return self._runners[key]

    def argv(self, command: Command, user: Optional[str] = None,
             credential: Optional[Any] = None) -> List[str]:
        """The full local ssh argument vector for *command*.

        Exposed (and tested) separately from :meth:`run` so that the
        ``mu2e-ssh-probe`` helper can print exactly what would be executed
        without executing it.
        """
        argv: List[str] = ["ssh"]
        for opt in self.options:
            # Options are written in config as "-o Key=value"; split so that
            # each token is its own argv entry rather than one quoted blob.
            argv.extend(shlex.split(opt))
        argv.extend(["-o", f"ConnectTimeout={self.connect_timeout}"])
        if self.jump:
            argv.extend(["-J", self.jump])
        login = user
        if login is None and credential is not None:
            login = getattr(credential, "login", None)
        argv.append(self._target(login))
        # The remote side runs this through its login shell, so it must be a
        # single string; as_string() quotes a sequence safely.
        argv.append(as_string(command))
        return argv

    # -- execution ---------------------------------------------------------

    def _run_once(self, command: Command, timeout: Optional[float],
                  user: Optional[str], input_text: Optional[str],
                  credential: Optional[Any]) -> CommandResult:
        """One ssh invocation under one credential."""
        argv = self.argv(command, user=user, credential=credential)
        started = time.monotonic()
        try:
            result = self._runner(credential).run(
                argv, timeout=timeout or self.command_timeout,
                input_text=input_text)
        except TimeoutExpired as exc:
            raise TimeoutExpired(f"{self.host}: {exc}") from exc

        result.command = as_string(command)
        result.host = self.host
        result.duration = time.monotonic() - started
        result.meta.update({
            "jump": self.jump,
            "user": user or getattr(credential, "login", None) or self.user,
            "credential": getattr(credential, "name", None),
            "via": "ssh",
        })
        return result

    def run(self, command: Command, timeout: Optional[float] = None,
            user: Optional[str] = None, input_text: Optional[str] = None,
            check: bool = False) -> CommandResult:
        """Run *command*, trying each credential until a session opens.

        An explicit *user* pins the login and disables the chain: a caller that
        names a user means that user, not "whoever can get in".
        """
        chain: List[Optional[Any]] = [None]
        if self.credentials and user is None:
            # Once something has worked here, go straight to it.
            chain = [self.working] if self.working is not None else list(self.credentials)

        last_error = "connection error"
        for index, credential in enumerate(chain):
            result = self._run_once(command, timeout, user, input_text, credential)

            if result.rc != SSH_FAILURE_RC:
                if credential is not None and self.working is None:
                    self.working = credential
                    if self.on_success:
                        self.on_success(credential)
                    if index:
                        log.info("%s: logged in as %s after %d earlier "
                                 "identity/identities were refused",
                                 self.host, credential, index)
                if check and not result.ok:
                    raise TransportError(
                        f"{self.host}: {result.command} exited {result.rc}")
                return result

            detail = result.stderr.strip().splitlines()
            last_error = detail[-1] if detail else "connection error"
            reason = classify_ssh_failure(result.stderr)
            if credential is not None:
                self.attempts.append({"credential": getattr(credential, "name", "?"),
                                      "reason": reason, "detail": last_error})
            if reason == "unreachable":
                # The host is down. Another identity cannot change that, and
                # each further attempt costs a full connect timeout.
                break
            if credential is not None and index + 1 < len(chain):
                log.debug("%s: %s refused (%s); trying the next identity",
                          self.host, credential, reason)

        tried = ", ".join(str(getattr(c, "name", "default")) for c in chain)
        raise SSHError(f"ssh to {self.host} failed after trying {tried}: {last_error}")

    # -- convenience -------------------------------------------------------

    def alive(self, timeout: Optional[float] = None) -> bool:
        """True when an SSH session can be opened and a trivial command runs."""
        try:
            return self.run("true", timeout=timeout or self.connect_timeout + 5).ok
        except TransportError:
            return False

    def wait_for_ssh(self, budget: float, interval: float = 15.0,
                     user: Optional[str] = None) -> bool:
        """Poll until the host answers SSH or *budget* seconds elapse.

        Used after an IPMI power-on.  Returns True as soon as a session opens;
        the caller is responsible for the post-boot settle wait, because
        "sshd is listening" is earlier than "the machine has finished booting".
        """
        deadline = time.monotonic() + budget
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            try:
                if self.run("true", timeout=self.connect_timeout + 5, user=user).ok:
                    log.info("%s answered ssh after %d attempt(s)", self.host, attempt)
                    return True
            except TransportError:
                pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(interval, remaining))
        log.warning("%s did not answer ssh within %.0fs", self.host, budget)
        return False


class SSHFactory:
    """Builds :class:`SSHTransport` objects with the run's ssh settings applied.

    Centralising this is what makes the jump-host rule a single decision:
    a gateway is contacted directly, everything else through the first gateway
    of its location that answered, and the choice is recorded so the report can
    say which path a result came over.
    """

    def __init__(self, settings: Any, topology: Any,
                 local: Optional[LocalTransport] = None,
                 kerberos: Any = None):
        self.settings = settings
        self.topology = topology
        self.local = local or LocalTransport(
            default_timeout=settings.get("ssh.command_timeout", 120),
            max_capture=settings.get("logging.max_capture_bytes", 65536),
        )
        #: The KerberosManager, when one exists. It owns the credential chain:
        #: the operator's ticket, then the Mu2e service identities that
        #: mu2edaq-kerberos can mint from Vault.
        self.kerberos = kerberos
        self._gateway_cache: Dict[str, Optional[str]] = {}
        #: Serialises gateway selection. Nodes are assessed a thread apiece,
        #: and every one of them asks for its location's gateway before its
        #: first command, so an unguarded cache miss means sixteen threads
        #: probing the same two gateways simultaneously -- each a TCP sweep
        #: plus a full handshake per credential in the chain. Against a
        #: gateway that is refusing logins, that is a burst of a couple of
        #: hundred connections at the very start of a phase.
        self._gateway_lock = threading.Lock()
        #: host -> the credential that worked there, so a transport rebuilt for
        #: the same node does not repeat the search.
        self._working: Dict[str, Any] = {}

    # -- gateway selection -------------------------------------------------

    def gateway_for(self, location: str) -> Optional[str]:
        """First responsive gateway for *location*, or None if none answer.

        The result is cached for the life of the run: re-probing the gateways
        before every one of several hundred node commands would dominate the
        runtime, and a gateway that dies mid-run surfaces as SSHError on the
        next command anyway.

        The probe is serialised, so the first caller does it and the rest wait
        for the answer. Without that, the whole worker pool arrives here at
        once on a cold cache and every thread probes the gateways for itself.
        """
        configured = self.settings.get("ssh.proxy", "auto")
        if configured and configured not in ("auto", "none"):
            return configured
        if configured == "none":
            return None
        if location in self._gateway_cache:
            return self._gateway_cache[location]

        with self._gateway_lock:
            # Re-check: another thread may have filled it while we queued.
            if location in self._gateway_cache:
                return self._gateway_cache[location]
            return self._select_gateway(location)

    def _select_gateway(self, location: str) -> Optional[str]:
        """Probe *location*'s gateways and cache the first that answers."""
        candidates = self.topology.gateways(location)
        # Pre-filter on TCP/22 before attempting a full SSH handshake.  A
        # gateway whose chassis is dark costs the whole ssh ConnectTimeout to
        # discover, and during an outage that is the likely case for at least
        # one of them; the sweep answers in one bounded connect.  A gateway
        # that passes the sweep still has to pass the handshake -- an open port
        # is not a working login.
        try:
            from ..sweep import sweep as tcp_sweep
            answered = {r.host for r in tcp_sweep(
                candidates, port=22,
                timeout_ms=int(self.settings.get("ssh.connect_timeout", 10)) * 1000)
                if r.reachable}
            if answered and len(answered) < len(candidates):
                log.info("gateway sweep: %s answered TCP/22, %s did not",
                         ", ".join(sorted(answered)),
                         ", ".join(sorted(set(candidates) - answered)))
                candidates = [g for g in candidates if g in answered]
        except Exception as exc:  # noqa: BLE001 - an optimisation, never a gate
            log.debug("gateway TCP sweep skipped: %s", exc)

        chosen: Optional[str] = None
        for gw in candidates:
            probe = self.for_host(gw, jump=None, direct=True)
            if probe.alive():
                # Remember which identity opened the gateway; it is very often
                # the one that opens the nodes behind it.
                chosen = gw
                break
            log.warning("gateway %s did not answer ssh", gw)
        if chosen is None:
            log.error("no gateway answered for location %s", location)
        self._gateway_cache[location] = chosen
        return chosen

    # -- transports --------------------------------------------------------

    def credentials_for(self, host: str, root: bool = False) -> List[Any]:
        """The ordered credential chain to try for *host*.

        The operator's own principal stays at the front. A service identity
        known to work for this host is promoted ahead of the *other* fallbacks,
        never ahead of the personal ticket: the run belongs to that identity,
        and one node having needed a service account is no reason to stop
        offering it everywhere else.
        """
        if self.kerberos is None:
            return []
        chain = self.kerberos.chain(root=root)
        known = self._working.get(host)
        if known is None or getattr(known, "primary", False):
            return chain

        primary = [c for c in chain if getattr(c, "primary", False)]
        rest = [c for c in chain if not getattr(c, "primary", False)
                and getattr(c, "name", None) != getattr(known, "name", None)]
        return primary + [known] + rest

    def _note_success(self, host: str, credential: Any) -> None:
        self._working[host] = credential
        if self.kerberos is not None:
            self.kerberos.note_success(credential)

    def for_host(self, host: str, jump: Optional[str] = None,
                 user: Optional[str] = None, direct: bool = False,
                 root: bool = False,
                 credentials: Optional[Sequence[Any]] = None) -> SSHTransport:
        chain = list(credentials) if credentials is not None \
            else self.credentials_for(host, root=root)
        return SSHTransport(
            host=host,
            user=user if user is not None else self.settings.get("ssh.user"),
            jump=None if direct else jump,
            options=self.settings.get("ssh.options", []),
            connect_timeout=self.settings.get("ssh.connect_timeout", 10),
            command_timeout=self.settings.get("ssh.command_timeout", 120),
            local=self.local,
            max_capture=self.settings.get("logging.max_capture_bytes", 65536),
            credentials=chain,
            on_success=lambda credential: self._note_success(host, credential),
        )

    def for_node(self, node: Any, user: Optional[str] = None,
                 root: bool = False) -> SSHTransport:
        """Transport for a :class:`~mu2edaq_power_recovery.topology.Node`.

        Gateways are contacted directly; every other node is proxied through
        its location's gateway.

        ``root=True`` pins the login to ``ssh.root_user`` and uses the root
        credential alone -- the service identities are ordinary accounts, so
        cycling through them for a root session would add an authentication
        round trip per identity and could not succeed.
        """
        login = self.settings.get("ssh.root_user", "root") if root else user
        direct = node.node_class == "gateway"
        jump = None if direct else (
            self.gateway_for(node.location) if node.location != "unknown" else None)
        return self.for_host(node.hostname, jump=jump, user=login,
                             direct=direct, root=root)
