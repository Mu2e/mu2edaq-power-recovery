"""Phase 0: check GitHub for a newer revision, fast-forward, rebuild if needed.

"When running the tools the first thing they should do is check for updates
from the github repo and update if needed and rebuild if needed"
(Project-Description.md).

Three constraints shape the implementation:

* An outage is the worst possible time for a surprise.  The update is a
  *fast-forward only*; if the local branch has diverged, or the working tree is
  dirty, the tool says so and runs the code that is checked out rather than
  reconciling anything by itself.
* The network may be part of what is broken.  Every git call is bounded by
  ``selfupdate.timeout`` and a failure is a warning, never a stop.
* After an update the process is running the *old* code.  Python has already
  imported the modules that just changed on disk, so the driver re-executes
  itself once (with a guard variable to make that exactly once) instead of
  pretending the update took effect.
"""
from __future__ import annotations

import fnmatch
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .version import PROJECT_ROOT

log = logging.getLogger(__name__)

#: Set in the environment of the re-executed process so a bad loop is
#: impossible even if something goes wrong with the revision comparison.
REEXEC_GUARD = "MU2E_POWER_RECOVERY_UPDATED"


@dataclass
class UpdateResult:
    """What phase 0 did."""

    checked: bool = False
    updated: bool = False
    rebuilt: bool = False
    before: Optional[str] = None
    after: Optional[str] = None
    behind: int = 0
    ahead: int = 0
    dirty: bool = False
    changed_files: List[str] = field(default_factory=list)
    messages: List[str] = field(default_factory=list)
    needs_reexec: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"checked": self.checked, "updated": self.updated,
                "rebuilt": self.rebuilt, "before": self.before, "after": self.after,
                "behind": self.behind, "ahead": self.ahead, "dirty": self.dirty,
                "changed_files": self.changed_files[:50],
                "messages": self.messages}

    def summary(self) -> str:
        if not self.checked:
            return "update check skipped"
        if self.updated:
            return (f"updated {self.before} -> {self.after}"
                    + (" and rebuilt" if self.rebuilt else ""))
        if self.behind:
            return f"{self.behind} commit(s) behind but not updated"
        return "already up to date"


class SelfUpdater:
    """Runs the phase-0 update against the project's own git checkout."""

    def __init__(self, settings: Any, root: Optional[Path] = None):
        self.settings = settings
        self.root = root or PROJECT_ROOT
        self.timeout = int(settings.get("selfupdate.timeout", 60))
        self.remote = settings.get("selfupdate.remote", "origin")

    # -- git ---------------------------------------------------------------

    def _git(self, *args: str, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=str(self.root),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout or self.timeout, check=False,
        )

    def _out(self, *args: str) -> str:
        try:
            proc = self._git(*args)
        except (OSError, subprocess.SubprocessError):
            return ""
        return proc.stdout.decode("utf-8", "replace").strip() if proc.returncode == 0 else ""

    # -- the update --------------------------------------------------------

    def run(self) -> UpdateResult:
        out = UpdateResult()
        if not self.settings.get("selfupdate.enabled", True):
            out.messages.append("self-update disabled by configuration")
            return out
        if os.environ.get(REEXEC_GUARD):
            out.messages.append("already updated and re-executed in this invocation")
            return out
        if not (self.root / ".git").exists():
            out.messages.append(f"{self.root} is not a git checkout; skipping update")
            return out

        out.checked = True
        out.before = self._out("rev-parse", "--short", "HEAD") or None
        branch = (self.settings.get("selfupdate.branch")
                  or self._out("rev-parse", "--abbrev-ref", "HEAD"))
        if not branch or branch == "HEAD":
            out.messages.append("detached HEAD; not updating")
            return out

        status = self._out("status", "--porcelain")
        out.dirty = bool(status)
        if out.dirty and not self.settings.get("selfupdate.allow_dirty", False):
            out.messages.append(
                "working tree has local modifications -- running the code that is "
                "checked out and not pulling. Commit or stash to enable updates.")
            log.warning(out.messages[-1])
            return out

        try:
            fetched = self._git("fetch", "--quiet", self.remote, branch)
        except subprocess.TimeoutExpired:
            out.messages.append(f"git fetch timed out after {self.timeout}s; "
                                "continuing with the local revision")
            log.warning(out.messages[-1])
            return out
        except OSError as exc:
            out.messages.append(f"git is not available ({exc}); skipping update")
            return out
        if fetched.returncode != 0:
            detail = fetched.stderr.decode("utf-8", "replace").strip().splitlines()
            out.messages.append(f"git fetch failed: {detail[-1] if detail else '?'}; "
                                "continuing with the local revision")
            log.warning(out.messages[-1])
            return out

        counts = self._out("rev-list", "--left-right", "--count",
                           f"HEAD...{self.remote}/{branch}")
        if counts:
            try:
                out.ahead, out.behind = (int(x) for x in counts.split())
            except ValueError:
                pass
        if out.behind == 0:
            out.messages.append("already at the remote revision")
            return out
        if out.ahead:
            out.messages.append(
                f"local branch has {out.ahead} commit(s) the remote does not; "
                "refusing to fast-forward. Push or rebase first.")
            log.warning(out.messages[-1])
            return out

        out.changed_files = [f for f in self._out(
            "diff", "--name-only", "HEAD", f"{self.remote}/{branch}").splitlines() if f]

        merged = self._git("merge", "--ff-only", f"{self.remote}/{branch}")
        if merged.returncode != 0:
            detail = merged.stderr.decode("utf-8", "replace").strip().splitlines()
            out.messages.append(f"fast-forward failed: {detail[-1] if detail else '?'}")
            log.warning(out.messages[-1])
            return out

        out.updated = True
        out.after = self._out("rev-parse", "--short", "HEAD") or None
        out.messages.append(f"fast-forwarded {out.before} -> {out.after} "
                            f"({out.behind} commit(s))")
        log.info(out.messages[-1])

        if self.needs_rebuild(out.changed_files):
            out.rebuilt = self.rebuild()
        out.needs_reexec = True
        return out

    # -- rebuild -----------------------------------------------------------

    def needs_rebuild(self, changed: List[str]) -> bool:
        """True when the pull touched anything that is not pure Python source."""
        globs = self.settings.get("selfupdate.rebuild_globs", []) or []
        for path in changed:
            for pattern in globs:
                # fnmatch does not treat '/' specially, so 'src/cpp/**' matches
                # any depth, which is what the config means by it.
                if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, pattern + "/*"):
                    log.info("rebuild needed: %s matches %s", path, pattern)
                    return True
        return False

    def rebuild(self) -> bool:
        """Re-run the project's bootstrap script.

        bootstrap.sh is idempotent -- it creates the venv if missing and
        upgrades the dependencies otherwise -- so this is safe to call whenever
        a dependency or a C++ source changed.
        """
        script = self.root / ("bootstrap.ps1" if os.name == "nt" else "bootstrap.sh")
        if not script.exists():
            log.warning("no bootstrap script at %s; skipping rebuild", script)
            return False
        cmd = (["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script)]
               if os.name == "nt" else [str(script)])
        log.info("rebuilding: %s", " ".join(cmd))
        try:
            proc = subprocess.run(cmd, cwd=str(self.root), timeout=900, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            log.error("rebuild failed to start: %s", exc)
            return False
        if proc.returncode != 0:
            log.error("rebuild exited %s -- continuing with the existing build",
                      proc.returncode)
            return False
        return True

    # -- re-exec -----------------------------------------------------------

    def reexec(self) -> None:
        """Replace this process with the updated code.

        Never returns.  The guard variable stops the new process from checking
        for updates again, so this can happen at most once per invocation.
        """
        env = dict(os.environ)
        env[REEXEC_GUARD] = "1"
        log.info("restarting with the updated code")
        sys.stdout.flush()
        sys.stderr.flush()
        os.execve(sys.executable, [sys.executable, "-m", "mu2edaq_power_recovery",
                                   *sys.argv[1:]], env)
