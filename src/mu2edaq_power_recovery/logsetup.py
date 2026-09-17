"""Logging configuration.

Two sinks with different jobs: the console shows what an operator needs to
follow along (INFO and worse, no timestamps -- the console output is already
interleaved with the on-screen report), and the file keeps the full record with
timestamps and module names, which is what gets attached to a ticket when
something behaved unexpectedly.
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from typing import Any, Optional


def configure(settings: Any, verbose: bool = False, quiet: bool = False) -> Path:
    """Set up logging and return the log file path (which may not be writable)."""
    level_name = str(settings.get("logging.level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    if verbose:
        level = logging.DEBUG
    if quiet:
        level = logging.WARNING

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    root.addHandler(console)

    log_path = settings.resolve_path(settings.get("logging.file", "logs/power-recovery.log"))
    if log_path:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            # Rotate rather than truncate: several recovery runs in one evening
            # is normal, and the earlier ones are exactly what you want to read
            # when the later one behaves oddly.
            file_handler = logging.handlers.RotatingFileHandler(
                log_path, maxBytes=10 * 1024 * 1024, backupCount=5)
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
            root.addHandler(file_handler)
        except OSError as exc:
            logging.getLogger(__name__).warning(
                "cannot write the log file %s (%s); console logging only",
                log_path, exc)

    # These are chatty at DEBUG and say nothing about the cluster.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return log_path
