"""Layered configuration.

Priority, lowest first::

    built-in defaults  <  config/power-recovery.yaml  <  config/.env
                       <  process environment        <  command line

That order is fixed by policy (CLAUDE.md) and is implemented literally:
:func:`load` applies each layer in turn to a single nested dict, and the CLI
applies the last one with :meth:`Settings.apply_cli`.

Configuration is addressed by dotted path (``ssh.connect_timeout``), and the
environment-variable spelling of a path is mechanical -- upper-case, dots to
underscores, prefixed with ``MU2E_POWER_RECOVERY_``.  Nothing has to be
registered in a table for an override to work, so a key added to the YAML is
immediately overridable from the environment with no code change.
"""
from __future__ import annotations

import copy
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import yaml

#: src/mu2edaq_power_recovery/settings.py -> project root is three up.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DEFAULT_CONFIG = CONFIG_DIR / "power-recovery.yaml"
DEFAULT_ENV_FILE = CONFIG_DIR / ".env"

ENV_PREFIX = "MU2E_POWER_RECOVERY_"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigError(ValueError):
    """A configuration file could not be read or a value could not be coerced."""


# ---------------------------------------------------------------------------
# Built-in defaults
#
# These exist so the tools still run with config/ deleted -- a recovery must
# not be blocked by a missing config file.  They mirror the shipped YAML;
# the YAML is the documentation, this is the floor.
# ---------------------------------------------------------------------------

DEFAULTS: Dict[str, Any] = {
    "run": {
        "label": None,
        "dry_run": True,
        "stop_on_stage_failure": True,
        "phase_timeout": 7200,
        "from_stage": None,
        "until_stage": None,
    },
    "topology": {
        "file": "topology.yaml",
        "sequence_file": "power-sequence.yaml",
        "checks_file": "checks.yaml",
        "locations": ["mc2", "teststand"],
    },
    "selfupdate": {
        "enabled": True,
        "remote": "origin",
        "branch": None,
        "rebuild_globs": ["pyproject.toml", "requirements.txt", "CMakeLists.txt",
                          "src/cpp/**", "src/include/**"],
        "allow_dirty": False,
        "timeout": 60,
    },
    "ssh": {
        "user": None,
        "root_user": "root",
        "proxy": "auto",
        "connect_timeout": 10,
        "command_timeout": 120,
        "options": ["-o BatchMode=yes", "-o StrictHostKeyChecking=accept-new",
                    "-o GSSAPIAuthentication=yes", "-o GSSAPIDelegateCredentials=yes",
                    "-o LogLevel=ERROR"],
        "max_sessions": 16,
    },
    "ipmi": {
        "execute_on": "gateway",
        "tool": "ipmitool",
        "interface": "lanplus",
        "privilege": "Operator",
        "cipher_suite": 3,
        "timeout": 10,
        "retries": 2,
        "power_on_delay": 20,
        # ipmitool's own -N/-R. None means "do not pass the flag", leaving
        # ipmitool's defaults (4 retries) in force -- forcing a single attempt
        # made BMCs that need a retry fail to establish a session at all.
        "message_timeout": None,
        "tool_retries": None,
        "extra_args": [],
    },
    "kerberos": {
        "principal": None,
        "root_principal": None,
        "min_lifetime": 3600,
        "prompt": True,
        "verify_users": ["mu2edaq", "mu2eshift"],
        "use_service_keytabs": True,
        "service_identities": ["mu2edaq", "mu2eshift"],
        "discover_identities": True,
        "get_kerberos_ticket_command": None,
        "vault_client_command": None,
        "vault_client_args": [],
    },
    "vault": {
        "addr": "https://ssivault.fnal.gov:8200",
        "kv_mount": "td",
        "base_path": "scd/experiments/mu2e",
        "ipmi_path": "ipmi/config",
        "ipmi_user_field": "username",
        "ipmi_password_field": "password",
        "allow_file_fallback": True,
        "fallback_password_file": "~/.ipmipasswd",
        "fallback_user": "MU2E",
        "auto_login": True,
        "timeout": 30,
    },
    "database": {"url": None, "path": "data/power-recovery.db"},
    "report": {
        "output_dir": "html",
        "title": "Mu2e DAQ Power Outage Recovery",
        "keep_runs": 30,
        "publish": {"enabled": False, "method": "rsync", "target": None,
                    "options": ["-az", "--delete"]},
    },
    "ecl": {
        "enabled": False,
        "url": "https://dbweb8.fnal.gov:8443/ECL/mu2e",
        "category": "DAQ/Recovery",
        "credential_path": "ecl",
        "attach_html": True,
    },
    "logging": {
        "level": "INFO",
        "file": "logs/power-recovery.log",
        "capture_command_output": True,
        "max_capture_bytes": 65536,
    },
}


# ---------------------------------------------------------------------------
# dict helpers
# ---------------------------------------------------------------------------


def deep_merge(base: Dict[str, Any], overlay: Mapping[str, Any]) -> Dict[str, Any]:
    """Recursively merge *overlay* into a copy of *base*.

    Lists replace wholesale rather than concatenating: an operator who writes
    ``ssh.options`` in their config means "these options", not "these as well
    as the built-in ones".
    """
    out = copy.deepcopy(base)
    for key, value in (overlay or {}).items():
        if isinstance(value, Mapping) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def flatten(data: Mapping[str, Any], prefix: str = "") -> Dict[str, Any]:
    """{'a': {'b': 1}} -> {'a.b': 1}."""
    out: Dict[str, Any] = {}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            out.update(flatten(value, path))
        else:
            out[path] = value
    return out


def env_name(path: str) -> str:
    """'report.publish.target' -> 'MU2E_POWER_RECOVERY_REPORT_PUBLISH_TARGET'."""
    return ENV_PREFIX + re.sub(r"[.\-]", "_", path).upper()


def coerce(raw: str, template: Any) -> Any:
    """Coerce a string from .env/environ to the type of the existing value.

    *template* is the value the config already holds, which is what fixes the
    target type.  When the existing value is None the type is unknown, so the
    string is passed through -- better a string than a wrong guess.
    """
    if isinstance(template, bool):
        low = raw.strip().lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise ConfigError(f"expected a boolean, got {raw!r}")
    if isinstance(template, int) and not isinstance(template, bool):
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigError(f"expected an integer, got {raw!r}") from exc
    if isinstance(template, float):
        try:
            return float(raw)
        except ValueError as exc:
            raise ConfigError(f"expected a number, got {raw!r}") from exc
    if isinstance(template, list):
        # Comma-separated, with surrounding whitespace stripped; empty -> [].
        return [item.strip() for item in raw.split(",") if item.strip()]
    return raw


def set_path(data: Dict[str, Any], path: str, value: Any) -> None:
    """Assign into a nested dict by dotted path, creating levels as needed."""
    parts = path.split(".")
    node = data
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def get_path(data: Mapping[str, Any], path: str, default: Any = None) -> Any:
    node: Any = data
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return default
        node = node[part]
    return node


# ---------------------------------------------------------------------------
# .env parsing
# ---------------------------------------------------------------------------


def parse_env_file(path: Path) -> Dict[str, str]:
    """Minimal dotenv reader: KEY=VALUE, '#' comments, optional 'export '.

    Deliberately not python-dotenv -- one function against a two-line file
    beats a dependency (CLAUDE.md: minimal dependencies).  Values may be
    single- or double-quoted; no interpolation is performed, because a
    password containing '$' must survive the round trip intact.
    """
    out: Dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass
class Layer:
    """Provenance record: where one effective value came from."""
    path: str
    value: Any
    source: str


class Settings:
    """Fully resolved configuration, addressed by dotted path.

    Keeps the origin of every overridden value (:attr:`overrides`) so the run
    banner and the report can state exactly which layer set a setting -- during
    an outage "why did it use a 5 second timeout" needs an answer that does not
    involve reading four files.
    """

    def __init__(self, data: Dict[str, Any], sources: Optional[List[Path]] = None):
        self.data = data
        self.sources: List[Path] = sources or []
        self.overrides: List[Layer] = []

    # -- access -----------------------------------------------------------

    def get(self, path: str, default: Any = None) -> Any:
        return get_path(self.data, path, default)

    def __getitem__(self, path: str) -> Any:
        value = get_path(self.data, path, _MISSING)
        if value is _MISSING:
            raise KeyError(path)
        return value

    def set(self, path: str, value: Any, source: str = "runtime") -> None:
        set_path(self.data, path, value)
        self.overrides.append(Layer(path, value, source))

    def section(self, name: str) -> Dict[str, Any]:
        value = self.get(name, {})
        return value if isinstance(value, dict) else {}

    # -- path helpers -----------------------------------------------------

    def resolve_path(self, path_value: Optional[str],
                     base: Optional[Path] = None) -> Optional[Path]:
        """Expand ~ and make relative paths absolute against the project root."""
        if not path_value:
            return None
        p = Path(str(path_value)).expanduser()
        if p.is_absolute():
            return p
        return (base or PROJECT_ROOT) / p

    def config_path(self, key: str) -> Path:
        """A path under config/, e.g. settings.config_path('topology.file')."""
        return CONFIG_DIR / str(self.get(key))

    # -- CLI layer --------------------------------------------------------

    def apply_cli(self, mapping: Mapping[str, Any]) -> None:
        """Apply the highest-priority layer.

        *mapping* is {dotted path: value}; a None value means "the flag was not
        given" and is skipped, so an absent flag never clobbers a config file.
        """
        for path, value in mapping.items():
            if value is None:
                continue
            self.set(path, value, source="command line")

    # -- reporting --------------------------------------------------------

    def provenance(self) -> List[Dict[str, Any]]:
        return [{"path": l.path, "value": l.value, "source": l.source}
                for l in self.overrides]

    def as_dict(self) -> Dict[str, Any]:
        return copy.deepcopy(self.data)

    def redacted(self) -> Dict[str, Any]:
        """A copy safe to write into an HTML report or a logbook entry.

        Anything whose key looks like a credential is replaced, not omitted --
        the report should show that a token was configured without showing it.
        """
        data = copy.deepcopy(self.data)
        pattern = re.compile(r"(pass|passwd|password|token|secret|key)$", re.I)
        for path, value in flatten(data).items():
            if value and pattern.search(path.split(".")[-1]):
                set_path(data, path, "<redacted>")
        return data


_MISSING = object()


def load(config_file: Optional[Path] = None,
         env_file: Optional[Path] = None,
         environ: Optional[Mapping[str, str]] = None,
         cli: Optional[Mapping[str, Any]] = None) -> Settings:
    """Build a :class:`Settings` by applying every layer in priority order.

    A missing config file is not an error (the built-in defaults stand); an
    unparseable one is, because silently running an outage recovery with
    default thresholds after a typo in the YAML would be worse than stopping.
    """
    environ = os.environ if environ is None else environ
    config_file = config_file or DEFAULT_CONFIG
    env_file = env_file if env_file is not None else DEFAULT_ENV_FILE

    data = copy.deepcopy(DEFAULTS)
    sources: List[Path] = []
    settings = Settings(data, sources)

    # --- layer 2: YAML config file ---------------------------------------
    if config_file and Path(config_file).exists():
        try:
            with open(config_file) as fh:
                file_data = yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{config_file} is not valid YAML: {exc}") from exc
        if not isinstance(file_data, dict):
            raise ConfigError(f"{config_file} must contain a mapping at the top level")
        settings.data = deep_merge(settings.data, file_data)
        sources.append(Path(config_file))
        for path, value in flatten(file_data).items():
            settings.overrides.append(Layer(path, value, str(config_file)))

    # --- layers 3 and 4: .env then the environment -----------------------
    known = flatten(settings.data)
    lookup: Dict[str, Tuple[str, Any]] = {env_name(p): (p, v) for p, v in known.items()}

    def _apply_env(pairs: Mapping[str, str], source: str) -> None:
        for name, raw in pairs.items():
            if not name.startswith(ENV_PREFIX):
                continue
            entry = lookup.get(name)
            if entry is None:
                # An override for a key the config does not define.  Accept it
                # as a string rather than rejecting it: a newer config file may
                # define it, and refusing would strand the operator.
                path = name[len(ENV_PREFIX):].lower().replace("_", ".")
                settings.set(path, raw, source=source)
                continue
            path, template = entry
            settings.set(path, coerce(raw, template), source=source)

    if env_file and Path(env_file).exists():
        _apply_env(parse_env_file(Path(env_file)), source=str(env_file))
        sources.append(Path(env_file))
    _apply_env(environ, source="environment")

    # VAULT_ADDR / VAULT_TOKEN are the conventional names the vault CLI and
    # hvac already honour; accept them rather than forcing a second spelling.
    if environ.get("VAULT_ADDR"):
        settings.set("vault.addr", environ["VAULT_ADDR"], source="environment (VAULT_ADDR)")

    # --- layer 5: command line -------------------------------------------
    if cli:
        settings.apply_cli(cli)

    return settings
