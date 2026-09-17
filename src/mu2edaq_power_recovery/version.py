"""Version and provenance banner.

Printed at the top of every run (Project-Description.md: "They should then
print information about the version being run").  During an outage the first
question asked of any result is "which version produced this", so the banner
carries the package version, the git revision, whether the working tree was
modified, and a digest of the configuration actually in force -- enough to
reproduce the run exactly.
"""
from __future__ import annotations

import hashlib
import platform
import subprocess
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__

#: src/mu2edaq_power_recovery/version.py -> project root is three up.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _git(*args: str, cwd: Optional[Path] = None) -> Optional[str]:
    """Run a git command in the project tree, returning stdout or None.

    Every failure mode -- git absent, not a repository, command error -- is
    folded into None.  Provenance is nice to have; it must never be the
    reason a recovery run refuses to start.
    """
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(cwd or PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.decode("utf-8", "replace").strip() or None


@dataclass
class VersionInfo:
    """Everything needed to identify the code and config behind a run."""

    package_version: str
    git_describe: Optional[str]
    git_commit: Optional[str]
    git_branch: Optional[str]
    git_dirty: bool
    git_remote: Optional[str]
    config_digest: Optional[str]
    config_files: List[str]
    python_version: str
    platform: str
    hostname: str
    generated_at: str
    #: 'libmu2eprobe' when the C++ extension is built and importable, else
    #: 'python'.  Recorded because it changes how long a sweep takes, and a
    #: run that took four times as long as yesterday's should say why.
    sweep_backend: str = "python"
    sweep_openmp: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def banner(self) -> str:
        """Multi-line banner for stdout."""
        rev = self.git_describe or self.git_commit or "unknown"
        dirty = "  [LOCALLY MODIFIED]" if self.git_dirty else ""
        lines = [
            "=" * 72,
            f"  mu2edaq-power-recovery {self.package_version}",
            f"  revision   : {rev}{dirty}",
        ]
        if self.git_branch:
            lines.append(f"  branch     : {self.git_branch}")
        if self.git_remote:
            lines.append(f"  remote     : {self.git_remote}")
        if self.config_digest:
            lines.append(f"  config     : {self.config_digest}  "
                         f"({len(self.config_files)} file(s))")
        backend = self.sweep_backend
        if self.sweep_openmp:
            backend += " (OpenMP)"
        lines += [
            f"  probe      : {backend}",
            f"  python     : {self.python_version}  on {self.platform}",
            f"  running on : {self.hostname}",
            f"  started    : {self.generated_at}",
            "=" * 72,
        ]
        return "\n".join(lines)


def config_digest(paths: List[Path]) -> Optional[str]:
    """SHA-256 over the concatenated bytes of the configuration in force.

    Files are hashed in the order given, each prefixed by its name, so that
    renaming or reordering configuration changes the digest.  Missing files
    are hashed as their name plus an empty body rather than skipped -- the
    absence of a config file is itself part of the configuration.
    """
    if not paths:
        return None
    h = hashlib.sha256()
    for p in paths:
        h.update(p.name.encode("utf-8"))
        h.update(b"\0")
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"<missing>")
        h.update(b"\0")
    return h.hexdigest()[:16]


def _sweep_backend() -> Dict[str, Any]:
    """Which reachability backend is available, without importing it eagerly."""
    try:
        from .sweep import backend, has_openmp
        return {"sweep_backend": backend(), "sweep_openmp": has_openmp()}
    except Exception:  # noqa: BLE001 - provenance must never break a run
        return {"sweep_backend": "python", "sweep_openmp": False}


def collect(config_paths: Optional[List[Path]] = None) -> VersionInfo:
    """Gather provenance for the current process."""
    config_paths = config_paths or []
    status = _git("status", "--porcelain")
    return VersionInfo(
        package_version=__version__,
        git_describe=_git("describe", "--tags", "--always", "--dirty"),
        git_commit=_git("rev-parse", "--short", "HEAD"),
        git_branch=_git("rev-parse", "--abbrev-ref", "HEAD"),
        git_dirty=bool(status),
        git_remote=_git("config", "--get", "remote.origin.url"),
        config_digest=config_digest(config_paths),
        config_files=[str(p) for p in config_paths],
        python_version=platform.python_version(),
        platform=f"{platform.system()} {platform.release()} ({platform.machine()})",
        hostname=platform.node(),
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **_sweep_backend(),
    )


def main(argv: Optional[List[str]] = None) -> int:
    """`python -m mu2edaq_power_recovery.version` -- print the banner."""
    print(collect().banner())
    return 0


if __name__ == "__main__":
    sys.exit(main())
