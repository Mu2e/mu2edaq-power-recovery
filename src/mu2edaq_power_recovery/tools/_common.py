"""Shared argument handling for the diagnostics helpers."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from ..logsetup import configure as configure_logging
from ..settings import load as load_settings
from ..topology import Topology


def add_common_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--config", metavar="FILE",
                        help="main configuration file "
                             "(default: config/power-recovery.yaml)")
    parser.add_argument("--env-file", metavar="FILE",
                        help="dotenv file (default: config/.env)")
    parser.add_argument("--json", action="store_true",
                        help="emit JSON instead of a table")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="warnings and errors only")
    return parser


def add_credential_arguments(parser: argparse.ArgumentParser
                             ) -> argparse.ArgumentParser:
    """--principal and --root-principal, as mu2e-power-recovery spells them.

    For the helpers that open ssh sessions: they reproduce a run only if they
    can be pointed at the same principals the run would use.
    """
    group = parser.add_argument_group("credentials")
    group.add_argument("--principal", metavar="PRINCIPAL",
                       help="Kerberos principal for ordinary logins "
                            "(overrides kerberos.principal). A password is "
                            "prompted for if no valid ticket exists.")
    group.add_argument("--root-principal", metavar="PRINCIPAL",
                       help="Kerberos principal with root access on the nodes "
                            "(overrides kerberos.root_principal)")
    return parser


def credential_overrides(args: argparse.Namespace) -> Dict[str, Any]:
    """Settings overrides from add_credential_arguments(), where given."""
    out: Dict[str, Any] = {}
    if getattr(args, "principal", None):
        out["kerberos.principal"] = args.principal
    if getattr(args, "root_principal", None):
        out["kerberos.root_principal"] = args.root_principal
    return out


def bootstrap(args: argparse.Namespace,
              cli: Optional[Dict[str, Any]] = None):
    """Load settings and the topology the way a real run would.

    Command-line values win over every other layer, as in the main driver.
    """
    overrides = credential_overrides(args)
    overrides.update(cli or {})
    settings = load_settings(
        config_file=Path(args.config) if args.config else None,
        env_file=Path(args.env_file) if args.env_file else None,
        cli=overrides,
    )
    configure_logging(settings, verbose=args.verbose, quiet=args.quiet)
    topology = Topology.load(settings.config_path("topology.file"))
    return settings, topology
