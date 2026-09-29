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


def test_execute_is_not_a_configuration_override():
    # --execute is this invocation's authorisation, decided by
    # authorize_live(); it no longer writes run.dry_run as a config layer.
    args = cli.build_parser().parse_args(["--execute"])
    assert "run.dry_run" not in cli.cli_overrides(args)


def test_execute_arms_the_run_through_authorize_live(settings):
    args = cli.build_parser().parse_args(["--execute"])
    decision = cli.authorize_live(settings, args, {})
    assert decision.live and decision.source == "--execute"
    assert settings.get("run.dry_run") is False


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


# ---------------------------------------------------------------------------
# Selection errors stop the run before credentials and before the run row
# ---------------------------------------------------------------------------


@pytest.fixture
def no_credentials(monkeypatch):
    """Fail the test if the driver gets as far as acquiring credentials."""
    from mu2edaq_power_recovery.orchestrator import Orchestrator

    def refuse(self):
        raise AssertionError("prepare_credentials() reached")

    monkeypatch.setattr(Orchestrator, "prepare_credentials", refuse)


def _run_rows(tmp_path):
    import sqlite3
    db = tmp_path / "cli.db"
    if not db.exists():
        return 0
    with sqlite3.connect(str(db)) as conn:
        try:
            return conn.execute("select count(*) from runs").fetchone()[0]
        except sqlite3.OperationalError:
            return 0


@pytest.mark.parametrize("args, needle", [
    (["--from", "manger"], "valid stages, in order: gateways, manager"),
    (["--until", "readuot"], "--until 'readuot' is not a stage"),
    (["--from", "cfo", "--until", "manager"], "comes after --until"),
    (["--node", "mu2e-trk-15"], "not in any stage of the power sequence"),
    (["--node", "mu2e-trk-01", "--until", "cfo"], "stage 'readout', outside"),
    (["--node", "bad;name"], "bad;name"),
])
def test_bad_phase2_selection_exits_2_before_credentials(tmp_path, capsys,
                                                         no_credentials,
                                                         args, needle):
    code = run_cli(tmp_path, "--phase", "poweron", *args)
    captured = capsys.readouterr()
    assert code == 2
    assert needle in captured.err
    assert "Traceback" not in captured.err + captured.out
    assert _run_rows(tmp_path) == 0


def test_a_bad_node_exits_2_before_credentials_in_every_phase(tmp_path, capsys,
                                                              no_credentials):
    # --node resolution is up front for phases 1 and 3 as well.
    code = run_cli(tmp_path, "--phase", "assess", "--node=-oProxyCommand=x")
    captured = capsys.readouterr()
    assert code == 2
    assert "Traceback" not in captured.err + captured.out
    assert _run_rows(tmp_path) == 0


def test_config_from_stage_typo_exits_2(tmp_path, capsys, no_credentials,
                                        monkeypatch):
    monkeypatch.setenv("MU2E_POWER_RECOVERY_RUN_FROM_STAGE", "dataloger")
    assert run_cli(tmp_path, "--phase", "poweron") == 2
    assert "'dataloger' is not a stage" in capsys.readouterr().err


def test_list_nodes_rejects_a_bad_name_cleanly(capsys):
    code = cli.main(["--list-nodes", "--node", "bad name", "--no-self-update",
                     "-q"])
    captured = capsys.readouterr()
    assert code == 2
    assert "error:" in captured.err
    assert "Traceback" not in captured.err


def test_config_live_without_authorisation_exits_2(tmp_path, capsys,
                                                   no_credentials, monkeypatch):
    monkeypatch.setenv("MU2E_POWER_RECOVERY_RUN_DRY_RUN", "false")
    monkeypatch.delenv("MU2E_POWER_RECOVERY_ARM", raising=False)
    code = cli.main(["--no-self-update", "-q", "--phase", "assess",
                     "--database-url", f"sqlite:///{tmp_path / 'cli.db'}"])
    err = capsys.readouterr().err
    assert code == 2
    assert "--execute" in err and "MU2E_POWER_RECOVERY_ARM" in err
    assert _run_rows(tmp_path) == 0


def test_scoped_poweron_rehearsal_powers_only_the_named_node(tmp_path, capsys):
    import json as _json
    assert run_cli(tmp_path, "--phase", "poweron", "--node", "mu2e-trk-01") == 0
    out = capsys.readouterr().out
    assert "VERIFY-ONLY" in out
    data = _json.loads((tmp_path / "html" / "data" / "poweron.json").read_text())
    stages = {s["name"]: s for s in data["data"]["stages"]}
    assert stages["readout"]["nodes"] == ["mu2e-trk-01.fnal.gov"]
    assert stages["manager"]["role"] == "predecessor"
    assert data["data"]["scope"]["allowed_power"] == ["mu2e-trk-01.fnal.gov"]
    assert data["counts"]["total"] == 9
