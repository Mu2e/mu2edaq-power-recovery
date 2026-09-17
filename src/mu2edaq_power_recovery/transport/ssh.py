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
"""
from __future__ import annotations

import logging
import shlex
import time
from typing import Any, Dict, List, Optional, Sequence

from .base import Command, CommandResult, TimeoutExpired, Transport, TransportError, as_string
from .local import LocalTransport

log = logging.getLogger(__name__)


class SSHError(TransportError):
    """The SSH connection itself failed (255, or the client could not start)."""


#: ssh exits 255 for its own errors; a remote command exiting 255 is
#: indistinguishable, but no check here runs anything that legitimately does.
SSH_FAILURE_RC = 255


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
                 local: Optional[LocalTransport] = None):
        self.host = host
        self.user = user
        self.jump = jump
        self.options = list(options or [])
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout
        self.local = local or LocalTransport(default_timeout=command_timeout)

    # -- command construction ---------------------------------------------

    def _target(self, user: Optional[str]) -> str:
        login = user or self.user
        return f"{login}@{self.host}" if login else self.host

    def argv(self, command: Command, user: Optional[str] = None) -> List[str]:
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
        argv.append(self._target(user))
        # The remote side runs this through its login shell, so it must be a
        # single string; as_string() quotes a sequence safely.
        argv.append(as_string(command))
        return argv

    # -- execution ---------------------------------------------------------

    def run(self, command: Command, timeout: Optional[float] = None,
            user: Optional[str] = None, input_text: Optional[str] = None,
            check: bool = False) -> CommandResult:
        argv = self.argv(command, user=user)
        started = time.monotonic()
        try:
            result = self.local.run(argv, timeout=timeout or self.command_timeout,
                                    input_text=input_text)
        except TimeoutExpired as exc:
            raise TimeoutExpired(f"{self.host}: {exc}") from exc

        result.command = as_string(command)
        result.host = self.host
        result.duration = time.monotonic() - started
        result.meta.update({"jump": self.jump, "user": user or self.user, "via": "ssh"})

        if result.rc == SSH_FAILURE_RC:
            detail = result.stderr.strip().splitlines()
            raise SSHError(f"ssh to {self.host} failed: "
                           f"{detail[-1] if detail else 'connection error'}")
        if check and not result.ok:
            raise TransportError(f"{self.host}: {result.command} exited {result.rc}")
        return result

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
                 local: Optional[LocalTransport] = None):
        self.settings = settings
        self.topology = topology
        self.local = local or LocalTransport(
            default_timeout=settings.get("ssh.command_timeout", 120),
            max_capture=settings.get("logging.max_capture_bytes", 65536),
        )
        self._gateway_cache: Dict[str, Optional[str]] = {}

    # -- gateway selection -------------------------------------------------

    def gateway_for(self, location: str) -> Optional[str]:
        """First responsive gateway for *location*, or None if none answer.

        The result is cached for the life of the run: re-probing the gateways
        before every one of several hundred node commands would dominate the
        runtime, and a gateway that dies mid-run surfaces as SSHError on the
        next command anyway.
        """
        configured = self.settings.get("ssh.proxy", "auto")
        if configured and configured not in ("auto", "none"):
            return configured
        if configured == "none":
            return None
        if location in self._gateway_cache:
            return self._gateway_cache[location]

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
                chosen = gw
                break
            log.warning("gateway %s did not answer ssh", gw)
        if chosen is None:
            log.error("no gateway answered for location %s", location)
        self._gateway_cache[location] = chosen
        return chosen

    # -- transports --------------------------------------------------------

    def for_host(self, host: str, jump: Optional[str] = None,
                 user: Optional[str] = None, direct: bool = False) -> SSHTransport:
        return SSHTransport(
            host=host,
            user=user if user is not None else self.settings.get("ssh.user"),
            jump=None if direct else jump,
            options=self.settings.get("ssh.options", []),
            connect_timeout=self.settings.get("ssh.connect_timeout", 10),
            command_timeout=self.settings.get("ssh.command_timeout", 120),
            local=self.local,
        )

    def for_node(self, node: Any, user: Optional[str] = None,
                 root: bool = False) -> SSHTransport:
        """Transport for a :class:`~mu2edaq_power_recovery.topology.Node`.

        Gateways are contacted directly; every other node is proxied through
        its location's gateway.  ``root=True`` selects ``ssh.root_user``.
        """
        login = self.settings.get("ssh.root_user", "root") if root else user
        if node.node_class == "gateway":
            return self.for_host(node.hostname, user=login, direct=True)
        jump = self.gateway_for(node.location) if node.location != "unknown" else None
        return self.for_host(node.hostname, jump=jump, user=login)
