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


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch, request):
    """Fail any test that tries to shell out to ssh, ping, ipmitool or kinit.

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

    Mark a test with @pytest.mark.allow_network to opt out.
    """
    if request.node.get_closest_marker("allow_network"):
        return

    from mu2edaq_power_recovery.transport import local as local_module

    blocked = ("ssh", "scp", "rsync", "ping", "ping6", "ipmitool", "kinit",
               "klist", "kdestroy", "vault", "get-kerberos-ticket",
               "vault-client")
    original = local_module.LocalTransport.run

    def guarded(self, command, *args, **kwargs):
        rendered = command if isinstance(command, str) else " ".join(
            str(part) for part in command)
        first = rendered.strip().split()[0].rsplit("/", 1)[-1] if rendered.strip() else ""
        if first in blocked:
            raise AssertionError(
                f"test tried to run {first!r} for real: {rendered[:120]}\n"
                f"Stub the transport, or mark the test @pytest.mark.allow_network.")
        return original(self, command, *args, **kwargs)

    monkeypatch.setattr(local_module.LocalTransport, "run", guarded)
