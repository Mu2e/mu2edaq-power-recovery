"""The command-line driver, end to end in simulation."""
from __future__ import annotations

import json

import pytest

from mu2edaq_power_recovery import cli


def run_cli(tmp_path, *args, config_dir=None):
    """Invoke main() with the run store and report redirected into tmp_path."""
    base = [
        "--simulate", "--no-self-update", "-q",
        "--database-url", f"sqlite:///{tmp_path / 'cli.db'}",
        "--output-dir", str(tmp_path / "html"),
        "--location", "mc2",
    ]
    return cli.main(base + list(args))


def test_list_checks_exits_cleanly(capsys):
    assert cli.main(["--list-checks", "--no-self-update", "-q"]) == 0
    assert "ping.lab" in capsys.readouterr().out


def test_list_checks_as_json(capsys):
    cli.main(["--list-checks", "--json", "--no-self-update", "-q"])
    payload = json.loads(capsys.readouterr().out)
    assert "disk.mounts" in payload


def test_list_nodes(capsys):
    assert cli.main(["--list-nodes", "--location", "mc2", "--no-self-update",
                     "-q"]) == 0
    assert "mu2e-trk-01" in capsys.readouterr().out


def test_a_simulated_assess_run_succeeds(tmp_path, capsys):
    code = run_cli(tmp_path, "--phase", "assess", "--node", "mu2egateway01",
                   "--node", "mu2e-trk-01")
    assert code == 0
    out = capsys.readouterr().out
    assert "Phase 1: Initial state" in out
    assert "SIMULATED RUN" in out
    assert (tmp_path / "html" / "initial-state.html").exists()


def test_all_four_phases_run_and_write_every_page(tmp_path):
    assert run_cli(tmp_path, "--phase", "all") == 0
    html = tmp_path / "html"
    for page in ("index.html", "initial-state.html", "power-on.html",
                 "network.html", "detail.html", "about.html", "api.html",
                 "sitemap.html", "runs.html"):
        assert (html / page).exists(), f"{page} was not written"
    for data in ("summary.json", "assess.json", "poweron.json", "network.json",
                 "report.json", "inventory.json"):
        assert (html / "data" / data).exists(), f"{data} was not written"


def test_a_simulated_run_never_arms_the_destructive_path(tmp_path, capsys):
    # --execute must not survive --simulate: a rehearsal has to be inert.
    run_cli(tmp_path, "--phase", "poweron", "--execute")
    assert "SIMULATED RUN" in capsys.readouterr().out


def test_dry_run_is_the_default_and_is_announced(tmp_path, capsys, monkeypatch):
    # Without --simulate and without --execute the banner must say dry run.
    parser = cli.build_parser()
    args = parser.parse_args(["--phase", "assess"])
    overrides = cli.cli_overrides(args)
    assert "run.dry_run" not in overrides      # config default (True) stands


def test_execute_flips_dry_run_off():
    args = cli.build_parser().parse_args(["--execute"])
    assert cli.cli_overrides(args)["run.dry_run"] is False


def test_simulate_forces_dry_run_even_with_execute():
    args = cli.build_parser().parse_args(["--execute", "--simulate"])
    assert cli.cli_overrides(args)["run.dry_run"] is True


def test_absent_flags_are_not_passed_as_overrides():
    args = cli.build_parser().parse_args([])
    assert all(value is None for key, value in cli.cli_overrides(args).items()
               if key in ("run.label", "vault.addr", "report.output_dir"))


def test_publish_flags_enable_publication():
    args = cli.build_parser().parse_args(["--publish-target", "host:/web/"])
    overrides = cli.cli_overrides(args)
    assert overrides["report.publish.enabled"] is True
    assert overrides["report.publish.target"] == "host:/web/"


def test_failures_produce_exit_status_one(tmp_path, monkeypatch):
    # A run that completes but finds broken nodes is exit 1, not 0 -- so a
    # wrapper script can tell "all healthy" from "look at the report".
    from mu2edaq_power_recovery import orchestrator as orch_module
    from mu2edaq_power_recovery.transport import ScriptedResponse
    original = orch_module.healthy_node_rules

    def broken_rules():
        # /home not mounted: the usual post-outage failure.
        return [(r"mountpoint -q /home", ScriptedResponse(rc=1))] + list(original())

    # Patched on the orchestrator, which imported the name directly -- patching
    # the defining module would leave that binding untouched.
    monkeypatch.setattr(orch_module, "healthy_node_rules", broken_rules)
    assert run_cli(tmp_path, "--phase", "assess", "--node", "mu2e-trk-01") == 1


def test_single_phase_entry_points_pin_their_phase():
    for entry, phase in ((cli.main_state, "assess"),
                         (cli.main_poweron, "poweron"),
                         (cli.main_netcheck, "network"),
                         (cli.main_report, "report")):
        with pytest.raises(SystemExit):
            entry(["--help"])       # the parser exists and is phase-specific


def test_json_output_is_machine_readable(tmp_path, capsys):
    run_cli(tmp_path, "--phase", "assess", "--node", "mu2e-trk-01", "--json")
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("["):])
    assert payload[0]["name"] == "assess"
    assert payload[0]["counts"]["total"] == 1
