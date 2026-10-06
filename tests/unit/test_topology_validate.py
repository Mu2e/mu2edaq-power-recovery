"""Topology.validate() and the MC-1 scaffolding (#25).

The inventory data for MC-1 is still pending; what is tested here is that the
tools say so, that the shipped configuration's known ambiguities are reported,
and that an MC-1 inventory, once written, is picked up by every path with no
code change.
"""
from __future__ import annotations

import json

import pytest
import yaml

from mu2edaq_power_recovery import cli
from mu2edaq_power_recovery.topology import Finding, Topology
from mu2edaq_power_recovery.tools import node_inventory


@pytest.fixture(scope="module")
def sequence(config_dir):
    with open(config_dir / "power-sequence.yaml") as fh:
        return yaml.safe_load(fh)


def test_the_shipped_configuration_findings(topology, sequence):
    found = [(f.level, f.message) for f in topology.validate(sequence)]
    assert not [m for level, m in found if level == "error"]
    warnings = [m for level, m in found if level == "warning"]
    assert len(warnings) == 6, warnings
    # The teststand BMCs genuinely share MC-2's IPMI segment (and so mc1's
    # placeholder): reported, because a dead /25 is still ambiguous.
    assert sum("overlaps teststand.ipmi 192.168.157.128/25" in m for m in warnings) == 2
    assert any(m.startswith("location mc1 has no nodes configured")
               and "status: pending" in m for m in warnings)
    assert any("mc2.ipmi 192.168.157.0/24 is the same subnet as mc1.ipmi "
               "192.168.157.0/24" in m for m in warnings)
    assert any("mc2.data 10.226.9.0/24 is the same subnet as teststand.data "
               "10.226.9.0/24" in m for m in warnings)
    unstaged = [m for m in warnings if "in no power-sequence stage" in m]
    assert len(unstaged) == 1 and unstaged[0].startswith("location mc2: 12 ")
    for short in ("mu2e-trk-15", "mu2e-trk-16", "mu2e-trk-17", "mu2e-trk-18"):
        assert short in unstaged[0]
    infos = [m for level, m in found if level == "info"]
    assert infos == ["location teststand has no power-sequence stage; phase 2 "
                     "powers nothing there"]


def test_validate_is_not_called_at_load(tmp_path):
    # An incomplete inventory must still load: MC-1 is normal, not an error.
    path = tmp_path / "t.yaml"
    path.write_text(yaml.safe_dump({"locations": {"x": {"networks": {"lab": []}}}}))
    topology = Topology.load(path)
    assert [f.level for f in topology.validate()] == ["warning"]


def _topology(tmp_path, data):
    path = tmp_path / "t.yaml"
    path.write_text(yaml.safe_dump(data))
    return Topology.load(path)


def test_orphan_bmcs_missing_gateways_and_protected_hosts(tmp_path):
    topology = _topology(tmp_path, {
        "protected": ["mu2e-gone-01.fnal.gov"],
        "locations": {"a": {
            "gateways": ["mu2e-gw-09.fnal.gov"],
            "subnets": {"lab": "10.0.0.0/24", "ipmi": "not-a-cidr"},
            "networks": {"lab": ["mu2e-x-01.fnal.gov"],
                         "ipmi": ["mu2e-x-01-ipmi.fnal.gov",
                                  "mu2e-x-02-ipmi.fnal.gov"]}}}})
    found = topology.validate()
    by_level = {level: [f.message for f in found if f.level == level]
                for level in ("error", "warning", "info")}
    assert any("not-a-cidr" in m for m in by_level["error"])
    assert any("1 BMC(s) with no lab host: mu2e-x-02-ipmi" in m
               for m in by_level["warning"])
    assert any("gateway mu2e-gw-09.fnal.gov" in m for m in by_level["warning"])
    assert any("protected host mu2e-gone-01.fnal.gov" in m
               for m in by_level["warning"])
    assert found[0].level == "error", "errors sort first"


def test_overlapping_but_unequal_subnets(tmp_path):
    topology = _topology(tmp_path, {"locations": {
        "a": {"subnets": {"lab": "10.0.0.0/16"},
              "networks": {"lab": ["h1.fnal.gov"]}},
        "b": {"subnets": {"lab": "10.0.5.0/24"},
              "networks": {"lab": ["h2.fnal.gov"]}}}})
    assert any("a.lab 10.0.0.0/16 overlaps b.lab 10.0.5.0/24" in f.message
               for f in topology.validate())


def test_invalid_names_use_the_load_time_rule(tmp_path, monkeypatch):
    # validate() reuses topology.valid_hostname; a name the loader would have
    # refused is an error finding when the data is examined directly.
    from mu2edaq_power_recovery import topology as topo_mod
    topology = _topology(tmp_path, {"locations": {"a": {
        "networks": {"lab": ["h1.fnal.gov"]}}}})
    topology._locations["a"]["gateways"] = ["bad name"]
    calls = []
    real = topo_mod.valid_hostname
    monkeypatch.setattr(topo_mod, "valid_hostname",
                        lambda n: calls.append(n) or real(n))
    found = topology.validate()
    assert "bad name" in calls
    assert any(f.level == "error" and "'bad name'" in f.message for f in found)


def test_stage_problems(tmp_path):
    topology = _topology(tmp_path, {"locations": {"a": {
        "networks": {"lab": ["h1.fnal.gov", "h2.fnal.gov"]}}}})
    found = topology.validate({"defaults": {"location": "a"}, "stages": [
        {"name": "one", "nodes": ["h1.fnal.gov", "h9.fnal.gov"]},
        {"name": "two", "location": "nowhere", "nodes": []}]})
    messages = [(f.level, f.message) for f in found]
    assert ("error", "power-sequence stage two: unknown location 'nowhere'") in messages
    assert any("stage one: 1 node(s) not in the a inventory: h9" in m
               for _, m in messages)
    assert any("a: 1 inventory node(s) in no power-sequence stage" in m
               and "h2" in m for _, m in messages)


def test_node_inventory_validate_exit_status_and_json(capsys, tmp_path):
    assert node_inventory.main(["--validate", "-q"]) == 0      # warnings only
    assert "6 warning(s)" in capsys.readouterr().out
    assert node_inventory.main(["--validate", "--json", "-q"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["counts"]["warning"] == 6

    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"locations": {"a": {
        "subnets": {"lab": "nope"}, "networks": {"lab": ["h.fnal.gov"]}}}}))
    config = tmp_path / "pr.yaml"
    config.write_text(yaml.safe_dump({"topology": {"file": str(bad)}}))
    assert node_inventory.main(["--validate", "--json", "-q",
                                "--config", str(config)]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert any(f["level"] == "error" and "'nope'" in f["message"]
               for f in payload["findings"])


def test_mc1_metadata_is_accepted_by_the_loader(topology):
    info = topology.location_info("mc1")
    assert info["status"] == "pending"
    assert "inventory_source" in info and "owner" in info
    assert topology.nodes("mc1") == {}


# ---------------------------------------------------------------------------
# a synthetic MC-1 inventory, end to end in simulation
# ---------------------------------------------------------------------------


@pytest.fixture
def mc1_topology(tmp_path, config_dir, monkeypatch):
    """The shipped topology with an MC-1 inventory filled in."""
    with open(config_dir / "topology.yaml") as fh:
        data = yaml.safe_load(fh)
    data["locations"]["mc1"]["networks"] = {
        "lab": [{"category": "mc1node", "start": 1, "end": 3}],
        "ipmi": [{"category": "mc1node", "start": 1, "end": 3, "suffix": "-ipmi"}],
    }
    data["locations"]["mc1"]["status"] = "populated"
    path = tmp_path / "topology-mc1.yaml"
    path.write_text(yaml.safe_dump(data))
    monkeypatch.setenv("MU2E_POWER_RECOVERY_TOPOLOGY_FILE", str(path))
    return path


def test_list_nodes_for_a_populated_mc1(mc1_topology, capsys):
    assert cli.main(["--list-nodes", "--location", "mc1", "--no-self-update",
                     "-q", "--json"]) == 0
    nodes = json.loads(capsys.readouterr().out)
    assert [n["short"] for n in nodes] == ["mu2e-mc1node-01", "mu2e-mc1node-02",
                                           "mu2e-mc1node-03"]
    assert all(n["location"] == "mc1" and n["networks"]["ipmi"] for n in nodes)


def test_a_simulated_assess_of_a_populated_mc1(mc1_topology, tmp_path, capsys):
    code = cli.main(["--simulate", "--no-self-update", "--phase", "assess",
                     "--location", "mc1", "--json",
                     "--database-url", f"sqlite:///{tmp_path / 'mc1.db'}",
                     "--output-dir", str(tmp_path / "html")])
    doc = json.loads(capsys.readouterr().out)
    assert code in (0, 1), doc.get("error")
    (phase,) = doc["phases"]
    hosts = {a["node"]["hostname"] for a in phase["assessments"]}
    assert hosts == {f"mu2e-mc1node-0{i}.fnal.gov" for i in (1, 2, 3)}
    assert not any("no nodes configured" in n for n in phase["notes"])


def test_an_empty_mc1_is_noted_in_the_phase(tmp_path, capsys):
    cli.main(["--simulate", "--no-self-update", "--phase", "assess",
              "--location", "mc1", "--location", "mc2", "--node", "mu2e-trk-01",
              "--json", "--database-url", f"sqlite:///{tmp_path / 'e.db'}",
              "--no-report"])
    doc = json.loads(capsys.readouterr().out)
    # --node given: the run is scoped to that node, so no location note.
    assert not any("no nodes configured" in n for n in doc["phases"][0]["notes"])

    cli.main(["--simulate", "--no-self-update", "--phase", "network",
              "--location", "mc1", "--json",
              "--database-url", f"sqlite:///{tmp_path / 'e2.db'}", "--no-report"])
    doc = json.loads(capsys.readouterr().out)
    notes = doc["phases"][0]["notes"]
    assert any(n.startswith("no nodes configured for mc1 (inventory status: "
                            "pending)") for n in notes), notes


def test_finding_as_dict():
    assert Finding("info", "x").as_dict() == {"level": "info", "message": "x"}
