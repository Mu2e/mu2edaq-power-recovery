"""Configuration layering and precedence."""
from __future__ import annotations

from pathlib import Path

import pytest

from mu2edaq_power_recovery import settings as S


def test_defaults_stand_alone(tmp_path):
    # config/ could be missing entirely on a host; that must not stop a run.
    s = S.load(config_file=tmp_path / "absent.yaml",
               env_file=tmp_path / "absent.env", environ={})
    assert s.get("run.dry_run") is True
    assert s.get("vault.addr") == "https://ssivault.fnal.gov:8200"


def test_yaml_overrides_defaults(config_dir):
    s = S.load(config_file=config_dir / "power-recovery.yaml",
               env_file=Path("/nonexistent"), environ={})
    assert s.get("vault.base_path") == "scd/experiments/mu2e"


def test_environment_beats_the_config_file(config_dir):
    s = S.load(config_file=config_dir / "power-recovery.yaml",
               env_file=Path("/nonexistent"),
               environ={"MU2E_POWER_RECOVERY_SSH_CONNECT_TIMEOUT": "42"})
    assert s.get("ssh.connect_timeout") == 42
    assert isinstance(s.get("ssh.connect_timeout"), int)


def test_env_file_beats_the_config_file_and_loses_to_the_environment(tmp_path, config_dir):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "MU2E_POWER_RECOVERY_SSH_CONNECT_TIMEOUT=11\n"
        "MU2E_POWER_RECOVERY_RUN_LABEL='from the env file'\n")
    s = S.load(config_file=config_dir / "power-recovery.yaml", env_file=env_file,
               environ={"MU2E_POWER_RECOVERY_SSH_CONNECT_TIMEOUT": "99"})
    assert s.get("ssh.connect_timeout") == 99          # environment wins
    assert s.get("run.label") == "from the env file"   # .env still applied


def test_command_line_beats_everything(config_dir):
    s = S.load(config_file=config_dir / "power-recovery.yaml",
               env_file=Path("/nonexistent"),
               environ={"MU2E_POWER_RECOVERY_RUN_DRY_RUN": "true"},
               cli={"run.dry_run": False})
    assert s.get("run.dry_run") is False


def test_absent_flags_do_not_clobber_configuration(config_dir):
    # A None in the CLI layer means "the flag was not given".
    s = S.load(config_file=config_dir / "power-recovery.yaml",
               env_file=Path("/nonexistent"), environ={},
               cli={"vault.addr": None})
    assert s.get("vault.addr") == "https://ssivault.fnal.gov:8200"


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("yes", True), ("1", True), ("ON", True),
    ("false", False), ("no", False), ("0", False), ("off", False),
])
def test_boolean_coercion(raw, expected):
    assert S.coerce(raw, True) is expected


def test_bad_boolean_is_rejected_rather_than_guessed():
    with pytest.raises(S.ConfigError):
        S.coerce("maybe", True)


def test_list_coercion_from_a_comma_separated_string():
    assert S.coerce("mc2, teststand", ["x"]) == ["mc2", "teststand"]


def test_env_name_mapping():
    assert S.env_name("report.publish.target") == \
        "MU2E_POWER_RECOVERY_REPORT_PUBLISH_TARGET"


def test_env_file_parsing_handles_quotes_exports_and_comments(tmp_path):
    path = tmp_path / ".env"
    path.write_text('# comment\nexport FOO="a b"\nBAR=\'c\'\nEMPTY=\nBAD LINE\n')
    parsed = S.parse_env_file(path)
    assert parsed == {"FOO": "a b", "BAR": "c", "EMPTY": ""}


def test_env_file_does_not_interpolate(tmp_path):
    # A BMC password containing '$' must survive the round trip untouched.
    path = tmp_path / ".env"
    path.write_text("SECRET=a$bc${notavar}\n")
    assert S.parse_env_file(path)["SECRET"] == "a$bc${notavar}"


def test_lists_replace_rather_than_merge():
    merged = S.deep_merge({"a": {"b": [1, 2, 3]}}, {"a": {"b": [9]}})
    assert merged["a"]["b"] == [9]


def test_redaction_hides_secrets_but_keeps_their_presence(config_dir):
    s = S.load(config_file=config_dir / "power-recovery.yaml",
               env_file=Path("/nonexistent"), environ={})
    s.set("ecl.token", "super-secret")
    redacted = s.redacted()
    assert redacted["ecl"]["token"] == "<redacted>"
    assert "token" in redacted["ecl"]        # presence is still visible


def test_malformed_yaml_is_an_error_not_a_silent_default(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("run:\n  dry_run: [unclosed\n")
    with pytest.raises(S.ConfigError):
        S.load(config_file=bad, env_file=Path("/nonexistent"), environ={})


def test_provenance_records_where_a_value_came_from(config_dir):
    s = S.load(config_file=config_dir / "power-recovery.yaml",
               env_file=Path("/nonexistent"),
               environ={"MU2E_POWER_RECOVERY_SSH_CONNECT_TIMEOUT": "42"})
    sources = {entry["path"]: entry["source"] for entry in s.provenance()}
    assert sources["ssh.connect_timeout"] == "environment"
