"""The manual is checked against the code by the test suite.

tools/generate-docs.py is the same script the ``docs`` build target and the
``docs-check`` ctest entry run.  Exercising it from pytest as well means drift
is caught by ``pytest`` alone, without a CMake build tree -- which is how the
suite is usually run, and the only way it is run on a machine that has no
compiler.

The script is run as a subprocess rather than imported: its contract with the
build is an exit status, and that is what is worth testing.
"""
from __future__ import annotations

import pytest

import hashlib
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
GENERATOR = PROJECT_ROOT / "tools" / "generate-docs.py"


def run_generator(*args):
    return subprocess.run([sys.executable, str(GENERATOR), *args],
                          cwd=str(PROJECT_ROOT), capture_output=True,
                          text=True, timeout=300)


def man_page_digest():
    """Content hash of every man page, for detecting an unwanted write."""
    digest = hashlib.sha256()
    for page in sorted((PROJECT_ROOT / "man").rglob("*")):
        if page.is_file():
            digest.update(page.read_bytes())
    return digest.hexdigest()


def test_generator_is_present():
    assert GENERATOR.exists(), f"{GENERATOR} is missing"


def test_documentation_matches_the_code():
    completed = run_generator("--check")
    assert completed.returncode == 0, (
        "tools/generate-docs.py --check reported documentation drift:\n"
        f"{completed.stdout}{completed.stderr}\n"
        "Run './venv/bin/python tools/generate-docs.py --write' for the "
        "generated sections; the rest needs prose.")


def test_check_changes_nothing():
    """--check must be safe to run against a read-only or checked-out tree."""
    before = man_page_digest()
    run_generator("--check", "--quiet")
    assert man_page_digest() == before


def test_sigterm_reaches_the_clean_shutdown_path():
    """stop-mu2edaq-power-recovery.sh sends SIGTERM and calls it the clean stop.

    Without a handler the default disposition applies: the process dies before
    the `finally` that destroys the run's private Kerberos caches, which can
    hold root-capable service tickets, and the run store is left saying
    'running'. The documented path has to actually be the clean one.
    """
    import signal

    from mu2edaq_power_recovery.cli import install_sigterm_handler

    previous = signal.getsignal(signal.SIGTERM)
    try:
        install_sigterm_handler()
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        assert handler not in (signal.SIG_DFL, signal.SIG_IGN)
        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGTERM, None)
    finally:
        signal.signal(signal.SIGTERM, previous)
