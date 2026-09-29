"""Shared fixtures.

Everything here is offline: the tests never touch the DAQ network, never need a
Kerberos ticket, and never read Vault.  That is the point of the transport
abstraction -- if these tests needed a cluster they could not be run before an
outage, which is the only time it matters that they pass.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from mu2edaq_power_recovery.checks import CheckContext           # noqa: E402
from mu2edaq_power_recovery.settings import load as load_settings  # noqa: E402
from mu2edaq_power_recovery.topology import Topology             # noqa: E402
from mu2edaq_power_recovery.transport import (FakeTransport,      # noqa: E402
                                              healthy_node_rules)


@pytest.fixture(scope="session")
def config_dir() -> Path:
    return PROJECT_ROOT / "config"


@pytest.fixture
def settings(tmp_path, config_dir):
    """Real shipped configuration, with the run store pointed at tmp_path.

    Deliberately the real config rather than a fixture copy: a test suite that
    validates a synthetic configuration would not notice the day someone breaks
    the one the tools actually ship with.
    """
    s = load_settings(config_file=config_dir / "power-recovery.yaml",
                      env_file=Path("/nonexistent"),
                      environ={},
                      cli={"database.path": str(tmp_path / "test.db"),
                           "report.output_dir": str(tmp_path / "html")})
    return s


@pytest.fixture(scope="session")
def topology(config_dir) -> Topology:
    return Topology.load(config_dir / "topology.yaml")


@pytest.fixture
def fake_transport() -> FakeTransport:
    """A transport scripted as a fully healthy node."""
    transport = FakeTransport("test-node.fnal.gov")
    for pattern, response in healthy_node_rules():
        transport.expect(pattern, response)
    return transport


class FakeFactory:
    """Minimal SSHFactory stand-in handing out clones of one FakeTransport."""

    def __init__(self, transport: FakeTransport, topology: Topology):
        self.transport = transport
        self.topology = topology

    def gateway_for(self, location):
        gateways = self.topology.gateways(location)
        return gateways[0] if gateways else None

    def for_host(self, host, jump=None, user=None, direct=False):
        return self.transport.clone(host)

    def for_node(self, node, user=None, root=False):
        return self.transport.clone(node.hostname)


@pytest.fixture
def factory(fake_transport, topology) -> FakeFactory:
    return FakeFactory(fake_transport, topology)


@pytest.fixture
def checks_config(config_dir):
    import yaml
    with open(config_dir / "checks.yaml") as fh:
        return yaml.safe_load(fh)


@pytest.fixture
def make_context(settings, topology, factory, checks_config):
    """Build a CheckContext for a named node, optionally with an IPMI client."""
    def _make(hostname="mu2e-trk-01.fnal.gov", ipmi=None, baseline=None):
        node = topology.node(hostname) or topology.resolve([hostname])[0]
        return CheckContext(node=node, settings=settings, topology=topology,
                            ssh_factory=factory, ipmi=ipmi,
                            checks_config=checks_config,
                            local=factory.transport, baseline=baseline or {})
    return _make


#: Commands that reach the DAQ network, Kerberos or Vault.  A test that runs
#: one of these for real is contacting infrastructure, whatever it believes the
#: transport underneath to be.
BLOCKED_COMMANDS = frozenset((
    "ssh", "scp", "rsync", "ping", "ping6", "ipmitool", "kinit", "klist",
    "kdestroy", "kswitch", "vault", "get-kerberos-ticket", "vault-client",
    "mu2e-probe", "curl", "wget", "nc"))

_SHELLS = frozenset(("sh", "bash", "zsh", "dash", "ksh", "csh", "tcsh"))


def _command_words(args, shell: bool) -> list:
    """Every command word a Popen call would execute, as basenames.

    A list whose head is a shell with ``-c`` is looked into, as is a string
    run with ``shell=True``: ``["/bin/sh", "-c", "ping host"]`` is a ping.
    Each ``;``/``&&``/``||``/``|`` segment contributes its first word, after
    any ``env`` and ``VAR=value`` prefixes.
    """
    import re
    import shlex

    def segments(script: str) -> list:
        words = []
        for segment in re.split(r"\|\||&&|[;|&\n()`]|\$\(", script):
            try:
                tokens = shlex.split(segment)
            except ValueError:
                tokens = segment.split()
            while tokens and (tokens[0] == "env" or
                              re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0])):
                tokens.pop(0)
            if tokens:
                words.append(tokens[0].rsplit("/", 1)[-1])
        return words

    if isinstance(args, (str, bytes)):
        text = args.decode() if isinstance(args, bytes) else args
        return segments(text) if shell else [text.rsplit("/", 1)[-1]]
    argv = [a.decode() if isinstance(a, bytes) else str(a) for a in args]
    if not argv:
        return []
    head = argv[0].rsplit("/", 1)[-1]
    if shell:
        return segments(" ".join(argv))
    if head in _SHELLS and "-c" in argv[1:]:
        index = argv.index("-c", 1)
        if index + 1 < len(argv):
            return [head] + segments(argv[index + 1])
    return segments(" ".join(shlex.quote(a) for a in argv))[:1] or [head]


def _offline_address(host) -> bool:
    """True for addresses a test may connect to: loopback and TEST-NET-1.

    192.0.2.0/24 (RFC 5737) is never routed to a live host, which is what
    makes it useful for timeout tests; ``.invalid`` never resolves.
    """
    import ipaddress
    if host in ("localhost", "") or str(host).endswith(".invalid"):
        return True
    try:
        address = ipaddress.ip_address(str(host).split("%", 1)[0])
    except ValueError:
        return False
    return address.is_loopback or address in ipaddress.ip_network("192.0.2.0/24")


class NetworkGuard:
    """What the autouse guard has blocked during one test.

    A block raises :class:`pytest.fail.Exception`, which derives from
    BaseException, so production code's ``except Exception`` -- and there is a
    good deal of it on exactly these paths (the gateway TCP pre-filter, every
    Vault call) -- cannot swallow it.  Each block is also recorded here and the
    test fails at teardown, which catches a ``except BaseException`` too.
    Meta-tests that expect a block call :meth:`expect` to consume it.
    """

    def __init__(self):
        self.violations = []

    def block(self, what: str):
        message = (f"test tried to {what} for real\n"
                   f"Stub it, or mark the test @pytest.mark.allow_network.")
        self.violations.append(message)
        pytest.fail(message, pytrace=False)

    def expect(self, fragment: str) -> str:
        """Consume the recorded block matching *fragment*; fail if there is none."""
        for index, message in enumerate(self.violations):
            if fragment in message:
                return self.violations.pop(index)
        pytest.fail(f"expected the network guard to block {fragment!r}; "
                    f"recorded: {self.violations}")


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch, request):
    """Fail any test that reaches the DAQ network, Kerberos or Vault.

    The suite claims to contact nothing -- no DAQ network, no Kerberos ticket,
    no Vault -- and that claim is the reason it can be run before an outage.
    It is easy to break by accident: a test that builds a transport through
    SSHFactory.for_node() resolves a gateway, and resolving a gateway probes
    it for real. This turns that into a failure instead of a slow test and a
    stray connection to the cluster.

    ``ping`` is on the list because leaving it off cost two days of an
    intermittent failure: a simulated run probed gateway nodes from the
    workstation's own transport, so ping.lab really did ping mu2egateway01,
    and the phase tests passed or failed according to whether that answered.
    A test that pings a DAQ host is a test that contacts the DAQ network,
    whatever the transport underneath claims to be.

    Three layers, because production code reaches the outside three ways:

    * ``subprocess.Popen`` -- every ``run``/``call``/``check_output`` and
      ``LocalTransport`` goes through it, including the credential commands
      ``creds/ticketsource.py`` and ``creds/vault.py`` run directly;
    * ``socket.connect``/``connect_ex`` to anything but loopback or TEST-NET-1
      -- this is what stops ``hvac`` (through requests/urllib3) and the Python
      sweep backend;
    * ``sweep.sweep`` -- the native backend opens its sockets in C++, where
      the socket patch cannot see them.

    Mark a test with @pytest.mark.allow_network to opt out.
    """
    guard = NetworkGuard()
    if request.node.get_closest_marker("allow_network"):
        yield guard
        return

    import socket
    import subprocess

    from mu2edaq_power_recovery import sweep as sweep_module

    original_init = subprocess.Popen.__init__

    def guarded_popen(self, args, *rest, **kwargs):
        shell = bool(kwargs.get("shell", False))
        for word in _command_words(args, shell):
            if word in BLOCKED_COMMANDS:
                rendered = args if isinstance(args, str) else " ".join(
                    str(a) for a in args)
                guard.block(f"run {word!r}: {rendered[:120]}")
        return original_init(self, args, *rest, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", guarded_popen)

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def _check(sock, address):
        if sock.family == getattr(socket, "AF_UNIX", object()):
            return
        host = address[0] if isinstance(address, tuple) else address
        if not _offline_address(host):
            guard.block(f"connect to {address!r}")

    def guarded_connect(self, address):
        _check(self, address)
        return original_connect(self, address)

    def guarded_connect_ex(self, address):
        _check(self, address)
        return original_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)

    original_sweep = sweep_module.sweep

    def guarded_sweep(hosts, *args, **kwargs):
        for host in hosts:
            if not _offline_address(host):
                guard.block(f"sweep {host!r}")
        return original_sweep(hosts, *args, **kwargs)

    monkeypatch.setattr(sweep_module, "sweep", guarded_sweep)

    yield guard

    if guard.violations:
        leftover = list(guard.violations)
        guard.violations.clear()
        pytest.fail("network guard blocked, and the code under test swallowed "
                    "it:\n" + "\n".join(leftover), pytrace=False)


@pytest.fixture(autouse=True)
def private_run_lock(monkeypatch, tmp_path):
    """Point run.lock_file into tmp_path for every test (#12).

    cli.main takes the run lock for every non-simulated phase 1-3 invocation,
    and the default is logs/ under the project root: without this a test run
    would create the checkout's lock file, and two concurrent test runs (or a
    test run beside a real recovery) would contend for it. The environment is
    the layer cli.main reads; tests that build Settings with ``environ={}``
    never take the lock.
    """
    monkeypatch.setenv("MU2E_POWER_RECOVERY_RUN_LOCK_FILE",
                       str(tmp_path / "run.lock"))
