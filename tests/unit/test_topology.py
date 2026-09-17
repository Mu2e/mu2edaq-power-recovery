"""Topology expansion, classification and protection."""
from __future__ import annotations

import pytest

from mu2edaq_power_recovery.topology import (NodeRange, Topology, TopologyError,
                                             expand_entries, natural_key)


def test_noderange_expands_with_category_and_suffix():
    rng = NodeRange(start=1, end=3, category="trk", suffix="-data")
    assert rng.hostnames() == ["mu2e-trk-01-data.fnal.gov",
                               "mu2e-trk-02-data.fnal.gov",
                               "mu2e-trk-03-data.fnal.gov"]


def test_noderange_without_category_uses_the_bare_prefix():
    # The teststand form: the prefix already encodes the host type.
    rng = NodeRange(start=7, end=8, category="", prefix="mu2edaq")
    assert rng.hostnames() == ["mu2edaq07.fnal.gov", "mu2edaq08.fnal.gov"]


def test_noderange_honours_excludes():
    rng = NodeRange(start=1, end=5, category="calo", excludes=[2, 4])
    assert [h.split(".")[0] for h in rng.hostnames()] == [
        "mu2e-calo-01", "mu2e-calo-03", "mu2e-calo-05"]


def test_expand_entries_mixes_strings_and_ranges():
    hosts = expand_entries(["mu2egateway01.fnal.gov",
                            {"category": "stm", "start": 1, "end": 2}])
    assert hosts == ["mu2egateway01.fnal.gov", "mu2e-stm-01.fnal.gov",
                     "mu2e-stm-02.fnal.gov"]


def test_expand_entries_rejects_nonsense():
    with pytest.raises(TopologyError):
        expand_entries([42])


def test_location_aliases_and_spelling(topology):
    assert topology.canonical_location("heerc") == "teststand"
    assert topology.canonical_location("MC-2") == "mc2"
    assert topology.canonical_location("mc2") == "mc2"
    with pytest.raises(TopologyError):
        topology.canonical_location("nowhere")


def test_nodes_merge_interfaces_onto_one_node(topology):
    node = topology.nodes("mc2")["mu2e-trk-01.fnal.gov"]
    assert node.networks["lab"] == "mu2e-trk-01.fnal.gov"
    assert node.networks["ipmi"] == "mu2e-trk-01-ipmi.fnal.gov"
    assert node.networks["data"] == "mu2e-trk-01-data.fnal.gov"
    assert node.node_class == "tracker"


def test_a_node_without_a_bmc_has_no_ipmi_host(topology):
    # trk-05 is in the ipmi excludes list in config/topology.yaml.
    node = topology.nodes("mc2")["mu2e-trk-05.fnal.gov"]
    assert node.ipmi_host is None
    assert node.has_network("lab")


def test_gateways_and_manager_are_protected(topology):
    assert topology.is_protected("mu2egateway01.fnal.gov")
    assert topology.is_protected("mu2e-mgr-01.fnal.gov")
    assert topology.is_protected("mu2e-dcs-01.fnal.gov")
    assert not topology.is_protected("mu2e-trk-01.fnal.gov")


def test_dcs_hosts_have_no_data_network(topology):
    # The DCS servers are on lab/mgmt only; a check must be able to tell that
    # apart from a data NIC that failed to come up.
    node = topology.nodes("mc2")["mu2e-dcs-01.fnal.gov"]
    assert not node.has_network("data")


def test_mc1_is_defined_but_empty(topology):
    # MC-1's inventory is not yet known; it must be present and empty rather
    # than absent, so the tools report "no nodes configured" for it.
    assert "mc1" in topology.locations
    assert topology.nodes("mc1") == {}
    assert topology.subnet("mc1", "lab") == "131.225.246.0/24"


def test_resolve_keeps_unknown_hosts(topology):
    nodes = topology.resolve(["mu2e-not-in-inventory"], ["mc2"])
    assert len(nodes) == 1
    assert nodes[0].location == "unknown"
    assert nodes[0].hostname.endswith(".fnal.gov")


def test_natural_key_orders_numerically():
    hosts = ["mu2e-trk-10", "mu2e-trk-02", "mu2e-trk-01"]
    assert sorted(hosts, key=natural_key) == ["mu2e-trk-01", "mu2e-trk-02",
                                              "mu2e-trk-10"]


def test_subnets_differ_between_sites(topology):
    assert topology.subnet("mc2", "lab") == "131.225.245.0/24"
    assert topology.subnet("teststand", "lab") == "131.225.237.0/24"
    assert topology.subnet("teststand", "ipmi") == "192.168.150.0/24"
