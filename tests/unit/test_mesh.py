"""Phase-3 mesh probe: UNKNOWN vs FAIL (#20), IPMI origin (#19), quoting (#9).

The fake here models *routing*, not a universally successful network: a node's
OS answers "Network is unreachable" for any BMC name, because the IPMI subnet
is routed only to the gateways.  A universally successful fake is what hid
#19 -- every scripted ping answered, so probing BMCs from node OSes looked fine.
"""
from __future__ import annotations

import re
import shlex

import pytest

from mu2edaq_power_recovery.checks import Status
from mu2edaq_power_recovery.checks.mesh import MeshEdge, MeshProbe, MeshResult
from mu2edaq_power_recovery.checks.reachability import _ping_command
from mu2edaq_power_recovery.transport import FakeTransport, ScriptedResponse
from mu2edaq_power_recovery.transport.ssh import SSHTransport

OK = ("3 packets transmitted, 3 received, 0% packet loss, time 2003ms\n"
      "rtt min/avg/max/mdev = 0.112/0.147/0.201/0.031 ms")
LOST = "3 packets transmitted, 0 received, 100% packet loss, time 2040ms"
UNROUTABLE = "ping: connect: Network is unreachable"


def _answer(command, reply_for):
    parts = []
    for target in re.findall(r"===BEGIN (\S+)===", command):
        parts += [f"===BEGIN {target}===", reply_for(target), f"===END {target}==="]
    return ScriptedResponse(stdout="\n".join(parts))


def _node_os(command):
    """A host OS: lab/data targets answer; BMCs are not routable from here."""
    return _answer(command, lambda t: UNROUTABLE if "-ipmi." in t else OK)


def _gateway(command):
    """A gateway: routes every network, BMCs included."""
    return _answer(command, lambda t: OK)


class RoutingFactory:
    """Hands out transports whose answers depend on where the probe runs."""

    def __init__(self, topology, dead=(), dead_raises=None):
        self.topology = topology
        self.dead = set(dead)
        self.dead_raises = dead_raises
        self.calls = []            # (host, direct, command)
        self.gateways = set()
        for loc in topology.locations:
            self.gateways.update(topology.gateways(loc))

    def gateway_for(self, location, role="ssh"):
        gws = self.topology.gateways(location)
        return gws[0] if gws else None

    def _make(self, host, direct):
        transport = FakeTransport(host)
        if host in self.dead:
            if self.dead_raises is not None:
                raise self.dead_raises
            transport.expect("", ScriptedResponse(raises="ssh: connect: No route to host"))
        else:
            transport.expect("===BEGIN ", _gateway if host in self.gateways else _node_os)
        original = transport.run

        def run(command, **kwargs):
            self.calls.append((host, direct, " ".join(command)))
            return original(command, **kwargs)
        transport.run = run
        return transport

    def for_host(self, host, jump=None, user=None, direct=False):
        return self._make(host, direct)

    def for_node(self, node, user=None, root=False):
        return self._make(node.hostname, node.node_class == "gateway")


def _nodes(topology, *names):
    return topology.resolve(list(names), ["mc2"])


IPMI = {"name": "ipmi", "origin": "gateways", "targets": "all"}


def _probe(factory, checks_config, networks):
    cfg = dict(checks_config)
    cfg["mesh"] = dict(checks_config["mesh"], networks=networks)
    return MeshProbe(factory, cfg, max_workers=4, topology=factory.topology)


# ---------------------------------------------------------------------------
# #19 -- IPMI probes originate from the gateways
# ---------------------------------------------------------------------------


def test_ipmi_is_probed_only_from_gateways_and_passes(topology, checks_config):
    factory = RoutingFactory(topology)
    nodes = _nodes(topology, "mu2e-dl-01", "mu2e-dl-02", "mu2e-trk-01",
                   "mu2egateway01")
    (res,) = _probe(factory, checks_config, [IPMI]).run_all(nodes)

    assert res.status is Status.OK
    gateways = set(topology.gateways("mc2"))
    assert {e.source for e in res.edges} == gateways
    assert set(res.sources) == gateways
    # Every BMC of the probed nodes, from every gateway -- including the
    # gateway's own BMC: no self-exclusion for gateway origins.
    bmcs = {n.networks["ipmi"] for n in nodes}
    assert {e.target for e in res.edges} == bmcs
    assert len(res.edges) == len(gateways) * len(bmcs)
    # Nothing was asked of a node OS, and gateways were reached directly.
    assert {h for h, _, _ in factory.calls} == gateways
    assert all(direct for _, direct, _ in factory.calls)


def test_ipmi_from_node_oses_is_what_fails_on_a_healthy_network(topology, checks_config):
    """The old behaviour, against the routing model: a false FAIL.  Kept so the
    fake is shown to distinguish the two origins."""
    factory = RoutingFactory(topology)
    nodes = _nodes(topology, "mu2e-dl-01", "mu2e-dl-02")
    (res,) = _probe(factory, checks_config,
                    [{"name": "ipmi", "origin": "nodes", "targets": "all"}]).run_all(nodes)
    assert res.status is Status.FAIL


def test_a_dead_gateway_gives_unknown_edges_not_failures(topology, checks_config):
    gw1, gw2 = topology.gateways("mc2")
    factory = RoutingFactory(topology, dead={gw1})
    nodes = _nodes(topology, "mu2e-dl-01", "mu2e-dl-02", "mu2e-cfo-01")
    (res,) = _probe(factory, checks_config, [IPMI]).run_all(nodes)

    assert res.failures == []
    # Coverage is per target: gw2 tested every BMC, so the verdict stands on
    # its probes -- OK, not UNKNOWN -- with gw1 still named as unusable.
    assert res.status is Status.OK
    assert res.uncovered_targets() == []
    assert any(gw1.split(".")[0] in n and "tested all 3" in n for n in res.notes)
    assert res.unreachable_sources() == [gw1]
    assert res.isolated_nodes() == []
    assert res.unreachable_targets() == []
    dead = [e for e in res.edges if e.source == gw1]
    assert dead and all(not e.tested and e.status is Status.UNKNOWN for e in dead)
    assert all(e.tested and e.ok for e in res.edges if e.source == gw2)
    c = res.counts()
    assert c == {"edges": 6, "tested": 3, "ok": 3, "failed": 0, "unknown": 3,
                 "unreachable_sources": 1, "uncovered_targets": 0}


def test_every_gateway_dead_is_unknown(topology, checks_config):
    factory = RoutingFactory(topology, dead=set(topology.gateways("mc2")))
    (res,) = _probe(factory, checks_config, [IPMI]).run_all(
        _nodes(topology, "mu2e-dl-01", "mu2e-dl-02"))
    assert res.status is Status.UNKNOWN
    assert res.failures == [] and res.isolated_nodes() == []
    assert res.as_dict()["counts"]["tested"] == 0


def test_a_gateway_whose_transport_cannot_be_built_is_unknown(topology, checks_config):
    gw1, _ = topology.gateways("mc2")
    factory = RoutingFactory(topology, dead={gw1}, dead_raises=RuntimeError("boom"))
    (res,) = _probe(factory, checks_config, [IPMI]).run_all(
        _nodes(topology, "mu2e-dl-01", "mu2e-dl-02"))
    raised = [e for e in res.edges if e.source == gw1]
    # One UNKNOWN edge per target, not a single "(all)" failure.
    assert sorted(e.target for e in raised) == ["mu2e-dl-01-ipmi.fnal.gov",
                                                "mu2e-dl-02-ipmi.fnal.gov"]
    assert all(e.status is Status.UNKNOWN for e in raised)
    assert "RuntimeError" in raised[0].detail
    # The other gateway covered both targets.
    assert res.status is Status.OK


def test_other_networks_keep_node_origins_and_self_exclusion(topology, checks_config):
    factory = RoutingFactory(topology)
    nodes = _nodes(topology, "mu2e-dl-01", "mu2e-dl-02")
    (res,) = _probe(factory, checks_config,
                    [{"name": "data", "full_mesh": True}]).run_all(nodes)
    assert res.origin == "nodes" and res.target_mode == "all"
    assert {e.source for e in res.edges} == {"mu2e-dl-01.fnal.gov", "mu2e-dl-02.fnal.gov"}
    assert all(e.source.split(".")[0] not in e.target for e in res.edges)
    assert res.status is Status.OK


def test_unknown_origin_or_target_mode_is_refused(topology, checks_config):
    probe = _probe(RoutingFactory(topology), checks_config, [])
    nodes = _nodes(topology, "mu2e-dl-01")
    with pytest.raises(ValueError, match="origin"):
        probe.run(nodes, "ipmi", origin="bmc")
    with pytest.raises(ValueError, match="targets"):
        probe.run(nodes, "lab", targets="some")


def test_anchors_keyed_per_location(topology, checks_config):
    cfg = dict(checks_config)
    cfg["mesh"] = dict(checks_config["mesh"],
                       anchors={"mc2": ["mu2e-cfo-01.fnal.gov"]})
    probe = MeshProbe(RoutingFactory(topology), cfg, topology=topology)
    nodes = _nodes(topology, "mu2e-dl-01", "mu2e-dl-02", "mu2e-cfo-01")
    res = probe.run(nodes, "lab", full_mesh=False)
    assert res.targets == ["mu2e-cfo-01.fnal.gov"]
    assert {e.target for e in res.edges} == {"mu2e-cfo-01.fnal.gov"}
    # A location with no anchors in the run falls back to its first nodes.
    cfg["mesh"]["anchors"] = {"teststand": ["mu2edaq07.fnal.gov"]}
    res = MeshProbe(RoutingFactory(topology), cfg, topology=topology).run(
        nodes, "lab", full_mesh=False)
    assert len(res.targets) == 3


def test_flat_anchor_list_still_works(topology, checks_config):
    probe = MeshProbe(RoutingFactory(topology), checks_config, topology=topology)
    res = probe.run(_nodes(topology, "mu2e-dl-01", "mu2e-dl-02", "mu2e-trk-01"),
                    "lab", full_mesh=False)
    assert set(res.targets) == {"mu2e-dl-01.fnal.gov"}


# ---------------------------------------------------------------------------
# #20 -- UNKNOWN is not FAIL
# ---------------------------------------------------------------------------


def test_edge_status_distinguishes_untested():
    assert MeshEdge("a", "b", "lab", ok=False, tested=False).status is Status.UNKNOWN
    assert MeshEdge("a", "b", "lab", ok=False).status is Status.FAIL
    assert MeshEdge("a", "b", "lab", ok=True, loss_pct=0).status is Status.OK
    assert MeshEdge("a", "b", "lab", ok=True, loss_pct=33.3).status is Status.WARN
    assert MeshEdge("a", "b", "lab", ok=True, loss_pct=0, mtu_ok=False).status is Status.WARN


def test_tested_failure_outranks_untested():
    res = MeshResult(network="lab", full_mesh=True, edges=[
        MeshEdge("a", "x", "lab", ok=False, tested=False),
        MeshEdge("b", "x", "lab", ok=False),
        MeshEdge("b", "y", "lab", ok=False),
        MeshEdge("c", "y", "lab", ok=True, loss_pct=0),
    ])
    assert res.status is Status.FAIL
    assert res.unreachable_sources() == ["a"]
    # a tested nothing, so it is not "isolated"; b reached nothing it tried.
    assert res.isolated_nodes() == ["b"]
    # x was only tested by b (failed): unreachable. y answered c.
    assert res.unreachable_targets() == ["x"]
    assert res.counts() == {"edges": 4, "tested": 3, "ok": 1, "failed": 2,
                            "unknown": 1, "unreachable_sources": 1,
                            "uncovered_targets": 0}
    d = res.as_dict()
    assert [e["source"] for e in d["untested"]] == ["a"]
    assert all(e["tested"] for e in d["failures"])


def test_source_transport_failure_vs_target_packet_loss(topology, checks_config):
    """A dead source is UNKNOWN; a live source whose pings get nothing is FAIL."""
    base = FakeTransport("x")
    factory = RoutingFactory(topology, dead={"mu2e-dl-01.fnal.gov"})
    lossy = lambda command: _answer(command, lambda t: LOST)  # noqa: E731

    def for_node(node, user=None, root=False):
        if node.hostname == "mu2e-dl-02.fnal.gov":
            return base.clone(node.hostname)
        return factory._make(node.hostname, False)
    base.expect("===BEGIN ", lossy)
    factory.for_node = for_node

    (res,) = _probe(factory, checks_config,
                    [{"name": "data", "full_mesh": True}]).run_all(
        _nodes(topology, "mu2e-dl-01", "mu2e-dl-02"))
    by_source = {e.source: e for e in res.edges}
    assert by_source["mu2e-dl-01.fnal.gov"].status is Status.UNKNOWN
    assert "source unreachable" in by_source["mu2e-dl-01.fnal.gov"].detail
    assert by_source["mu2e-dl-02.fnal.gov"].status is Status.FAIL
    assert res.status is Status.FAIL
    assert res.isolated_nodes() == ["mu2e-dl-02.fnal.gov"]
    assert res.unreachable_sources() == ["mu2e-dl-01.fnal.gov"]


def test_missing_end_marker_is_untested(checks_config):
    probe = MeshProbe(None, checks_config)
    out = f"===BEGIN a===\n{OK}\n===END a===\n===BEGIN b===\n{OK}\n"
    assert probe._split(out, "a") is not None
    assert probe._split(out, "b") is None
    assert probe._split(out, "c") is None

    transport = FakeTransport("src")
    transport.expect("===BEGIN ", ScriptedResponse(stdout=out))
    edges = probe._probe_source("src", transport, ["a", "b", "c"], "lab", False)
    status = {e.target: e.status for e in edges}
    assert status == {"a": Status.OK, "b": Status.UNKNOWN, "c": Status.UNKNOWN}


# ---------------------------------------------------------------------------
# #9 -- quoting of the generated scripts
# ---------------------------------------------------------------------------


def test_valid_names_are_unchanged_by_quoting(checks_config):
    probe = MeshProbe(None, checks_config)
    script = probe._script(["mu2e-dl-01-data.fnal.gov", "10.226.9.5", "fe80::1"],
                           "data", mtu_probe=True)
    assert "echo '===BEGIN mu2e-dl-01-data.fnal.gov==='" in script
    assert "echo '---MTU fe80::1---'" in script
    assert "ping -c 3 -W 5 -q 10.226.9.5 2>&1 || true" in script
    # The fake's marker regex still finds every target.
    assert re.findall(r"===BEGIN (\S+)===", script) == [
        "mu2e-dl-01-data.fnal.gov", "10.226.9.5", "fe80::1"]


@pytest.mark.parametrize("evil", [
    "a;touch /tmp/pwned", "$(touch /tmp/pwned)", "`touch /tmp/pwned`",
    "a b", "a'b", '-oProxyCommand=id', "a===\necho x"])
def test_hostile_names_stay_literal_in_the_script(checks_config, evil):
    """Even past validation, a name is one literal word in every line."""
    probe = MeshProbe(None, checks_config)
    script = probe._script([evil], "data", mtu_probe=True)
    lines = script.split("\n") if "\n" not in evil else None
    if lines is None:
        # A newline inside a quoted word keeps the shell tokens intact.
        tokens = shlex.split(script)
        assert tokens.count(f"===BEGIN {evil}===") == 1
        return
    for line in lines:
        words = shlex.split(line)
        if words[0] == "echo":
            assert len(words) == 2 and evil in words[1]
        else:
            assert words[:2] == ["ping", "-c"]
            assert evil in words
            assert words[-3:] == ["2>&1", "||", "true"]
            assert words[words.index(evil) - 1] in ("-q", "8972")


def test_ping_command_quotes_its_target():
    assert _ping_command("mu2e-trk-01.fnal.gov", 3, 5) == \
        "ping -c 3 -W 5 -q mu2e-trk-01.fnal.gov"
    cmd = _ping_command("x; id", 3, 5)
    assert shlex.split(cmd)[-1] == "x; id"
    assert shlex.split(_ping_command("$(id)", 1, 5, dialect="bsd"))[-1] == "$(id)"


# ---------------------------------------------------------------------------
# #9 -- ssh argv ends option parsing before the destination
# ---------------------------------------------------------------------------


def test_ssh_argv_puts_every_option_before_the_double_dash():
    t = SSHTransport("mu2e-trk-01.fnal.gov", user="mu2edaq",
                     jump="mu2egateway01.fnal.gov",
                     options=["-o BatchMode=yes", "-o GSSAPIAuthentication=yes"],
                     connect_timeout=7)
    argv = t.argv(["id", "-un"])
    dd = argv.index("--")
    assert argv[dd + 1] == "mu2edaq@mu2e-trk-01.fnal.gov"
    assert argv[dd + 2] == "id -un"
    assert len(argv) == dd + 3
    before = argv[:dd]
    assert before[before.index("-J") + 1] == "mu2egateway01.fnal.gov"
    assert "ConnectTimeout=7" in before and "BatchMode=yes" in before


def test_ssh_argv_without_jump_or_user():
    argv = SSHTransport("mu2egateway01.fnal.gov").argv(["true"])
    assert argv[-3:] == ["--", "mu2egateway01.fnal.gov", "true"]
    assert "-J" not in argv


# ---------------------------------------------------------------------------
# per-target gateway coverage (S4)
# ---------------------------------------------------------------------------


def test_a_dark_gateway_and_a_failing_bmc_is_fail_from_the_tested_gateway(
        topology, checks_config):
    gw1, gw2 = topology.gateways("mc2")
    factory = RoutingFactory(topology, dead={gw1})
    nodes = _nodes(topology, "mu2e-dl-01", "mu2e-dl-02")
    dl2 = nodes[1].networks["ipmi"]
    make = factory._make

    def partner_misses_dl2(host, direct):
        transport = make(host, direct)
        if host == gw2:
            # The partner completed its probe; one BMC did not answer it.
            transport.expect_first("===BEGIN ", lambda c: _answer(
                c, lambda t: LOST if t == dl2 else OK))
        return transport

    factory._make = partner_misses_dl2
    (res,) = _probe(factory, checks_config, [IPMI]).run_all(nodes)
    assert res.status is Status.FAIL
    assert [e.target for e in res.failures] == [dl2]
    assert res.unreachable_sources() == [gw1]


def test_a_target_neither_gateway_tested_is_unknown(topology, checks_config):
    factory = RoutingFactory(topology, dead=set(topology.gateways("mc2")))
    (res,) = _probe(factory, checks_config, [IPMI]).run_all(
        _nodes(topology, "mu2e-dl-01"))
    assert res.status is Status.UNKNOWN
    assert res.uncovered_targets() == ["mu2e-dl-01-ipmi.fnal.gov"]
    assert any("tested by no other gateway" in n for n in res.notes)


def test_a_location_with_bmcs_but_no_gateway_is_unknown_not_ok(
        topology, checks_config, monkeypatch):
    nodes = _nodes(topology, "mu2e-dl-01", "mu2e-dl-02")
    real = topology.gateways
    monkeypatch.setattr(topology, "gateways",
                        lambda loc: [] if loc == "mc2" else real(loc))
    (res,) = _probe(RoutingFactory(topology), checks_config, [IPMI]).run_all(nodes)

    assert res.status is Status.UNKNOWN
    assert res.edges and all(not e.tested for e in res.edges)
    assert {e.source for e in res.edges} == {"(no gateway: mc2)"}
    assert res.uncovered_targets() == sorted(n.networks["ipmi"] for n in nodes)
    # Not a host that could not be reached: there was no host at all.
    assert res.unreachable_sources() == []
    assert any("no gateway" in n and "UNKNOWN" in n for n in res.notes)


def test_a_gatewayless_location_does_not_hide_behind_a_healthy_one(
        topology, checks_config, monkeypatch):
    nodes = _nodes(topology, "mu2e-dl-01")
    ts = [n for n in topology.all_nodes(["teststand"]) if n.has_network("ipmi")][:1]
    assert ts, "the topology has a teststand BMC"
    real = topology.ipmi_gateways
    monkeypatch.setattr(topology, "ipmi_gateways",
                        lambda loc: [] if loc == "teststand" else real(loc))
    (res,) = _probe(RoutingFactory(topology), checks_config, [IPMI]).run_all(
        nodes + ts)
    # MC-2's BMC was tested and answered; the teststand's was never looked at.
    assert res.status is Status.UNKNOWN
    assert res.uncovered_targets() == [ts[0].networks["ipmi"]]



@pytest.mark.skipif(not hasattr(__import__("signal"), "pthread_kill"),
                    reason="POSIX only")
def test_sigterm_mid_mesh_cancels_the_queued_sources(topology, checks_config):
    import signal
    import threading

    from mu2edaq_power_recovery.cli import install_sigterm_handler

    nodes = _nodes(topology, "mu2e-dl-01", "mu2e-dl-02", "mu2e-dl-03",
                   "mu2e-dl-04")
    started, running, release = [], threading.Event(), threading.Event()
    factory = RoutingFactory(topology)

    def slow(node, user=None, root=False):
        started.append(node.hostname)
        running.set()
        release.wait(10)
        return factory._make(node.hostname, False)

    factory.for_node = slow
    probe = MeshProbe(factory, dict(checks_config, mesh=dict(
        checks_config["mesh"], networks=[{"name": "data"}])), max_workers=1,
        topology=topology)

    def fire():
        if running.wait(10):
            signal.pthread_kill(threading.main_thread().ident, signal.SIGTERM)

    previous = signal.getsignal(signal.SIGTERM)
    install_sigterm_handler()
    threading.Thread(target=fire, daemon=True).start()
    try:
        with pytest.raises(KeyboardInterrupt):
            probe.run_all(nodes)
    finally:
        release.set()
        signal.signal(signal.SIGTERM, previous)
    assert len(started) == 1, "a queued mesh source was started"


# ---------------------------------------------------------------------------
# Found on the live cluster, 2026-10-01
# ---------------------------------------------------------------------------

DATA = {"name": "data", "full_mesh": True, "mtu_probe": False}


def _two_sites(topology):
    return (topology.resolve(["mu2e-trk-01", "mu2e-dl-01"], ["mc2"])
            + topology.resolve(["mu2edaq07", "mu2edaq13"], ["teststand"]))


def test_a_full_mesh_stays_inside_each_location(topology, checks_config):
    """MC-2 and the teststand both use 10.226.9.0/24 on separate segments:
    a cross-site pair is not a path (live: every one ARP-failed)."""
    nodes = _two_sites(topology)
    loc = {n.networks["data"]: n.location for n in nodes}
    loc.update({n.hostname: n.location for n in nodes})
    (res,) = _probe(RoutingFactory(topology), checks_config, [DATA]).run_all(nodes)
    assert res.edges
    assert all(loc[e.source] == loc[e.target] for e in res.edges)
    assert len(res.edges) == 2 + 2      # 2 ordered pairs per two-node site


def test_cross_location_true_restores_the_whole_run_mesh(topology, checks_config):
    nodes = _two_sites(topology)
    (res,) = _probe(RoutingFactory(topology), checks_config,
                    [dict(DATA, cross_location=True)]).run_all(nodes)
    assert len(res.edges) == 4 * 3


def test_target_hosts_maps_network_names_back_to_nodes(topology, checks_config):
    nodes = _two_sites(topology)
    (res,) = _probe(RoutingFactory(topology), checks_config, [DATA]).run_all(nodes)
    assert res.target_hosts == {n.networks["data"]: n.hostname for n in nodes}


def test_a_lost_path_is_a_failure_not_a_jumbo_failure():
    """Live run: 487 paths lost every packet and 9 had an MTU problem; the
    summary said "496 jumbo-frame failure(s)"."""
    lost = MeshEdge("a", "b-data", "data", ok=False, tested=True, loss_pct=100.0,
                    mtu_ok=False)
    small_only = MeshEdge("a", "c-data", "data", ok=True, tested=True, loss_pct=0.0,
                          mtu_ok=False)
    res = MeshResult(network="data", full_mesh=True, edges=[lost, small_only])
    assert res.failures == [lost]
    assert res.mtu_failures == [small_only]


def test_ipmi_gateways_override_the_ssh_gateways(topology, checks_config, monkeypatch):
    """The teststand's BMCs are on the MC-2 IPMI segment; mu2edaq-gateway has
    no interface there, so they are probed (and driven) from MC-2's gateways."""
    mc2_gws = topology.gateways("mc2")
    monkeypatch.setitem(topology.location_info("teststand"), "ipmi_gateways", mc2_gws)
    assert topology.ipmi_gateways("teststand") == mc2_gws
    assert topology.gateways("teststand") == ["mu2edaq-gateway.fnal.gov"]
    nodes = topology.resolve(["mu2edaq07"], ["teststand"])
    (res,) = _probe(RoutingFactory(topology), checks_config, [IPMI]).run_all(nodes)
    assert {e.source for e in res.edges} == set(mc2_gws)


def test_ipmi_gateways_default_to_the_location_gateways(topology):
    for loc in topology.locations:
        if not topology.location_info(loc).get("ipmi_gateways"):
            assert topology.ipmi_gateways(loc) == topology.gateways(loc)
    assert topology.ipmi_gateways("mc2") == topology.gateways("mc2")
    assert topology.ipmi_gateways("teststand") == topology.gateways("mc2")


def test_an_unresolvable_target_is_named_as_such():
    """Live: mu2edaq10/11/14/22-ipmi are in the topology but not in DNS."""
    nx = MeshEdge("gw", "mu2edaq10-ipmi.fnal.gov", "ipmi", ok=False, tested=True,
                  detail="ping: mu2edaq10-ipmi.fnal.gov: Name or service not known")
    dark = MeshEdge("gw", "mu2edaq07-ipmi.fnal.gov", "ipmi", ok=False, tested=True,
                    detail="3 packets transmitted, 0 received, +3 errors, 100% packet loss")
    res = MeshResult(network="ipmi", full_mesh=True, edges=[nx, dark], origin="gateways")
    assert res.unresolved_targets() == ["mu2edaq10-ipmi.fnal.gov"]
    assert res.unreachable_targets() == sorted([nx.target, dark.target])


def test_a_shared_ipmi_gateway_is_one_source(topology, checks_config, monkeypatch):
    monkeypatch.setitem(topology.location_info("teststand"), "ipmi_gateways",
                        topology.gateways("mc2"))
    nodes = _nodes(topology, "mu2e-dl-01") + topology.resolve(["mu2edaq07"], ["teststand"])
    (res,) = _probe(RoutingFactory(topology), checks_config, [IPMI]).run_all(nodes)
    assert sorted(res.sources) == sorted(topology.gateways("mc2"))


# ---------------------------------------------------------------------------
# PR #28 review -- a ping that never ran is UNKNOWN, not FAIL
# ---------------------------------------------------------------------------


def _probe_with(checks_config, block, mtu_block=None, targets=("a", "b")):
    """Run _probe_source with every target answering *block* (and *mtu_block*)."""
    def reply(command):
        parts = []
        for target in re.findall(r"===BEGIN (\S+)===", command):
            parts += [f"===BEGIN {target}===", block]
            if mtu_block is not None:
                parts += [f"---MTU {target}---", mtu_block]
            parts.append(f"===END {target}===")
        return ScriptedResponse(stdout="\n".join(parts))
    transport = FakeTransport("src")
    transport.expect("===BEGIN ", reply)
    probe = MeshProbe(None, checks_config)
    edges = probe._probe_source("src", transport, list(targets), "data",
                                mtu_block is not None)
    return MeshResult(network="data", full_mesh=True, edges=edges)


def test_ping_not_found_is_untested_not_failed(checks_config):
    """Reviewer's stub: with `ping` missing every source was 'isolated'."""
    res = _probe_with(checks_config, "sh: ping: not found")
    assert all(e.status is Status.UNKNOWN for e in res.edges)
    assert res.failures == [] and res.isolated_nodes() == []
    assert res.status is Status.UNKNOWN
    assert all("sh: ping: not found" in e.detail for e in res.edges)


def test_ping_not_permitted_is_untested_not_failed(checks_config):
    res = _probe_with(checks_config, "ping: socket: Operation not permitted")
    assert res.status is Status.UNKNOWN
    assert res.failures == []
    assert "Operation not permitted" in res.edges[0].detail


def test_an_unresolvable_name_is_still_a_tested_failure(checks_config):
    """Not ping failing to run: an inventory error the operator must see."""
    res = _probe_with(checks_config, "ping: a: Name or service not known",
                      targets=("a",))
    (edge,) = res.edges
    assert edge.tested and edge.status is Status.FAIL
    assert res.unresolved_targets() == ["a"]


def test_no_route_is_still_a_tested_failure(checks_config):
    res = _probe_with(checks_config, UNROUTABLE)
    assert res.status is Status.FAIL
    assert all(e.tested for e in res.edges)


def test_real_packet_loss_is_still_a_failure(checks_config):
    res = _probe_with(checks_config, LOST)
    assert res.status is Status.FAIL
    assert res.isolated_nodes() == ["src"]
    assert res.unresolved_targets() == []


def test_an_mtu_probe_that_never_ran_leaves_mtu_unset(checks_config):
    """BusyBox ping rejects -M do: nothing was learned about the MTU."""
    res = _probe_with(checks_config, OK,
                      mtu_block="ping: unrecognized option: M")
    assert all(e.status is Status.OK and e.mtu_ok is None for e in res.edges)
    assert res.mtu_failures == []


def test_an_mtu_probe_that_ran_and_lost_is_an_mtu_failure(checks_config):
    lost_jumbo = "1 packets transmitted, 0 received, 100% packet loss, time 0ms"
    res = _probe_with(checks_config, OK, mtu_block=lost_jumbo)
    assert all(e.mtu_ok is False for e in res.edges)
    assert res.status is Status.WARN
