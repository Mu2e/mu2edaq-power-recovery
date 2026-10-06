"""Live-run authorisation (#11).

run.dry_run: false in configuration only *permits* a live run. This
invocation must authorise it: --execute, or MU2E_POWER_RECOVERY_ARM equal to
the configured run.label in the process environment. --simulate always wins.
"""
from __future__ import annotations

import pytest

from mu2edaq_power_recovery import cli
from mu2edaq_power_recovery.settings import ARM_ENV, ConfigError, load

ARM = ARM_ENV


def make(tmp_path, yaml_text="", dotenv="", environ=None, argv=()):
    config = tmp_path / "power-recovery.yaml"
    config.write_text(yaml_text or "run: {}\n")
    env_file = tmp_path / ".env"
    env_file.write_text(dotenv)
    args = cli.build_parser().parse_args(list(argv))
    environ = dict(environ or {})
    settings = load(config_file=config, env_file=env_file, environ=environ,
                    cli=cli.cli_overrides(args))
    return settings, args, environ


def decide(tmp_path, **kwargs):
    settings, args, environ = make(tmp_path, **kwargs)
    return settings, cli.authorize_live(settings, args, environ)


LIVE_YAML = "run:\n  dry_run: false\n  label: outage-2026-09\n"


def test_default_is_a_dry_run(tmp_path):
    settings, d = decide(tmp_path)
    assert not d.live
    assert settings.get("run.dry_run") is True


def test_execute_alone_arms(tmp_path):
    settings, d = decide(tmp_path, argv=["--execute"])
    assert d.live and d.source == "--execute"
    assert settings.get("run.dry_run") is False


def test_execute_with_config_live_arms(tmp_path):
    _, d = decide(tmp_path, yaml_text=LIVE_YAML, argv=["--execute"])
    assert d.live and d.source == "--execute"


@pytest.mark.parametrize("source", ["yaml", "dotenv", "environment"])
def test_config_live_alone_is_refused(tmp_path, source):
    kwargs = {"yaml": {"yaml_text": "run:\n  dry_run: false\n"},
              "dotenv": {"dotenv": "MU2E_POWER_RECOVERY_RUN_DRY_RUN=false\n"},
              "environment": {"environ": {
                  "MU2E_POWER_RECOVERY_RUN_DRY_RUN": "false"}}}[source]
    settings, args, environ = make(tmp_path, **kwargs)
    with pytest.raises(cli.LiveAuthorizationError) as info:
        cli.authorize_live(settings, args, environ)
    message = str(info.value)
    # Explains both ways to authorise, and where the setting came from.
    assert "--execute" in message and ARM in message
    expected = {"yaml": "power-recovery.yaml", "dotenv": ".env",
                "environment": "environment"}[source]
    assert expected in message


def test_arm_matching_the_label_arms(tmp_path):
    settings, d = decide(tmp_path, yaml_text=LIVE_YAML,
                         environ={ARM: "outage-2026-09"})
    assert d.live and ARM in d.source and "outage-2026-09" in d.source
    assert settings.get("run.dry_run") is False


def test_arm_with_a_label_from_the_environment_arms(tmp_path):
    _, d = decide(tmp_path, dotenv="MU2E_POWER_RECOVERY_RUN_DRY_RUN=false\n",
                  environ={"MU2E_POWER_RECOVERY_RUN_LABEL": "planned",
                           ARM: "planned"})
    assert d.live


@pytest.mark.parametrize("yaml_text, token, needle", [
    (LIVE_YAML, "outage-2026-08", "does not match run.label"),
    ("run:\n  dry_run: false\n", "anything", "run.label is not"),
    ("run:\n  dry_run: true\n  label: outage-2026-09\n", "outage-2026-09",
     "run.dry_run is true"),
    ("run:\n  label: outage-2026-09\n", "outage-2026-09", "run.dry_run is true"),
])
def test_an_arm_token_that_does_not_hold_is_refused(tmp_path, yaml_text, token,
                                                   needle):
    settings, args, environ = make(tmp_path, yaml_text=yaml_text,
                                   environ={ARM: token})
    with pytest.raises(cli.LiveAuthorizationError, match=needle):
        cli.authorize_live(settings, args, environ)


def test_arm_in_dotenv_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="per invocation"):
        make(tmp_path, yaml_text=LIVE_YAML, dotenv=f"{ARM}=outage-2026-09\n")


def test_arm_is_never_ingested_as_a_setting(tmp_path):
    settings, _, _ = make(tmp_path, environ={ARM: "x"})
    assert settings.get("arm") is None
    assert not any(layer.path == "arm" for layer in settings.overrides)


@pytest.mark.parametrize("kwargs", [
    {"argv": ["--simulate", "--execute"]},
    {"yaml_text": LIVE_YAML, "environ": {ARM: "outage-2026-09"},
     "argv": ["--simulate"]},
    {"yaml_text": "run:\n  dry_run: false\n", "argv": ["--simulate"]},
    {"environ": {ARM: "mismatch"}, "argv": ["--simulate"]},
])
def test_simulate_always_wins(tmp_path, kwargs):
    settings, d = decide(tmp_path, **kwargs)
    assert not d.live and d.source == "--simulate"
    assert settings.get("run.dry_run") is True


def test_execute_ignores_a_stray_token(tmp_path, caplog):
    _, d = decide(tmp_path, environ={ARM: "whatever"}, argv=["--execute"])
    assert d.live
    assert any("ignored" in r.getMessage() for r in caplog.records)


def test_the_decision_is_recorded_as_a_settings_layer(tmp_path):
    settings, _ = decide(tmp_path, argv=["--execute"])
    last = [l for l in settings.overrides if l.path == "run.dry_run"][-1]
    assert "live-run authorisation" in last.source and "--execute" in last.source


READ_ONLY = [["assess"], ["network"], ["report"]]


@pytest.mark.parametrize("phases", READ_ONLY)
@pytest.mark.parametrize("kwargs", [
    {"yaml_text": "run:\n  dry_run: false\n"},
    {"dotenv": "MU2E_POWER_RECOVERY_RUN_DRY_RUN=false\n"},
    {"yaml_text": LIVE_YAML, "environ": {ARM: "wrong-label"}},
    {"yaml_text": LIVE_YAML, "environ": {ARM: "outage-2026-09"}},
    {"argv": ["--execute"]},
])
def test_a_read_only_invocation_is_a_dry_run_whatever_is_set(tmp_path, kwargs,
                                                             phases, caplog):
    # mu2e-power-state / -netcheck / -report cannot issue a power command, so
    # there is nothing to authorise and nothing to refuse.
    caplog.set_level("INFO")
    settings, args, environ = make(tmp_path, **kwargs)
    d = cli.authorize_live(settings, args, environ, phases=phases)
    assert not d.live and "read-only" in d.source
    assert settings.get("run.dry_run") is True
    assert any("ignored" in r.getMessage() and r.levelname == "INFO"
               for r in caplog.records)


@pytest.mark.parametrize("phases", [["poweron"], cli.PHASE_ORDER])
def test_the_gate_still_holds_for_any_invocation_with_power_on(tmp_path, phases):
    settings, args, environ = make(tmp_path, yaml_text="run:\n  dry_run: false\n")
    with pytest.raises(cli.LiveAuthorizationError):
        cli.authorize_live(settings, args, environ, phases=phases)
    settings, args, environ = make(tmp_path, yaml_text=LIVE_YAML,
                                   environ={ARM: "wrong-label"})
    with pytest.raises(cli.LiveAuthorizationError):
        cli.authorize_live(settings, args, environ, phases=phases)
    settings, args, environ = make(tmp_path, yaml_text=LIVE_YAML,
                                   environ={ARM: "outage-2026-09"})
    assert cli.authorize_live(settings, args, environ, phases=phases).live
    settings, args, environ = make(tmp_path, argv=["--execute"])
    assert cli.authorize_live(settings, args, environ, phases=phases).live
    settings, args, environ = make(tmp_path, argv=["--execute", "--simulate"])
    assert not cli.authorize_live(settings, args, environ, phases=phases).live


def test_without_phases_the_gate_applies(tmp_path):
    # phases=None is the conservative default: "may include power-on".
    settings, args, environ = make(tmp_path, yaml_text="run:\n  dry_run: false\n",
                                   argv=["--phase", "assess"])
    with pytest.raises(cli.LiveAuthorizationError):
        cli.authorize_live(settings, args, environ)


def test_a_live_run_logs_a_warning(tmp_path, caplog):
    decide(tmp_path, yaml_text=LIVE_YAML, environ={ARM: "outage-2026-09"})
    assert any(r.levelname == "WARNING" and "LIVE run authorised" in r.getMessage()
               for r in caplog.records)
