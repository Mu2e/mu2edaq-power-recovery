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


def bootstrap(args: argparse.Namespace,
              cli: Optional[Dict[str, Any]] = None):
    """Load settings and the topology the way a real run would."""
    settings = load_settings(
        config_file=Path(args.config) if args.config else None,
        env_file=Path(args.env_file) if args.env_file else None,
        cli=cli or {},
    )
    configure_logging(settings, verbose=args.verbose, quiet=args.quiet)
    topology = Topology.load(settings.config_path("topology.file"))
    return settings, topology
