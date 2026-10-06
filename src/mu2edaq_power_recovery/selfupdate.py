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
* An update whose required rebuild fails is not an update.  The checkout is
  reset to the revision it started at and the process carries on without
  re-executing (#21); if even the reset fails, the driver stops with exit 2
  rather than run old code against a new tree.
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
    #: The pull touched a build input (selfupdate.rebuild_globs).
    rebuild_required: bool = False
    #: A required rebuild failed, so the update was abandoned (#21).
    update_failed: bool = False
    #: ... and the checkout was reset to :attr:`before`.
    rolled_back: bool = False
    #: ... but the reset itself failed: the checkout is at :attr:`attempted`
    #: while this process runs :attr:`before`'s code. The driver exits 2.
    reset_failed: bool = False
    #: The revision the abandoned update fast-forwarded to.
    attempted: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"checked": self.checked, "updated": self.updated,
                "rebuilt": self.rebuilt, "before": self.before, "after": self.after,
                "behind": self.behind, "ahead": self.ahead, "dirty": self.dirty,
                "rebuild_required": self.rebuild_required,
                "update_failed": self.update_failed,
                "rolled_back": self.rolled_back,
                "reset_failed": self.reset_failed,
                "attempted": self.attempted,
                "needs_reexec": self.needs_reexec,
                "changed_files": self.changed_files[:50],
                "messages": self.messages}

    def summary(self) -> str:
        if not self.checked:
            return "update check skipped"
        if self.update_failed:
            return (f"update to {_short(self.attempted)} abandoned (rebuild "
                    f"failed); "
                    + (f"rolled back to {_short(self.before)}"
                       if self.rolled_back else "ROLLBACK FAILED"))
        if self.updated:
            return (f"updated {_short(self.before)} -> {_short(self.after)}"
                    + (" and rebuilt" if self.rebuilt else ""))
        if self.behind:
            return f"{self.behind} commit(s) behind but not updated"
        return "already up to date"


def _short(sha: Optional[str]) -> str:
    return (sha or "?")[:12]


class SelfUpdater:
    """Runs the phase-0 update against the project's own git checkout."""

    def __init__(self, settings: Any, root: Optional[Path] = None,
                 stdout: Any = None):
        self.settings = settings
        self.root = root or PROJECT_ROOT
        #: Where the rebuild's output goes (a file object or descriptor);
        #: None inherits stdout. The driver passes stderr under --json, so
        #: stdout carries nothing but the JSON document.
        self.stdout = stdout
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
        # The full SHA: it is what a rollback resets to, and what the run's
        # provenance records (a short one can become ambiguous).
        out.before = self._out("rev-parse", "HEAD") or None
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
        out.after = self._out("rev-parse", "HEAD") or None
        out.messages.append(f"fast-forwarded {_short(out.before)} -> "
                            f"{_short(out.after)} ({out.behind} commit(s))")
        log.info(out.messages[-1])

        if self.needs_rebuild(out.changed_files):
            out.rebuild_required = True
            out.rebuilt = self.rebuild()
            if not out.rebuilt:
                # The new code's declared build inputs were not applied, so
                # re-executing into it would run it against the old venv or
                # native extension -- a working installation turned into a
                # startup failure just before a recovery (#21).
                self._roll_back(out)
                return out
        out.needs_reexec = True
        return out

    def _roll_back(self, out: UpdateResult) -> None:
        """Abandon the update: put the checkout back at ``out.before``.

        ``--hard`` on a clean tree; ``--keep`` when the tree was dirty under
        ``allow_dirty``, which keeps the operator's local edits (and refuses
        rather than overwrite one). Either way this process carries on with
        the code it started with. If the reset fails the checkout no longer
        matches the running code -- which still imports modules lazily from
        disk -- so ``reset_failed`` is set and the driver stops.
        """
        out.update_failed = True
        out.attempted = out.after
        out.needs_reexec = False
        mode = "--keep" if out.dirty else "--hard"
        detail = ""
        try:
            proc = self._git("reset", "--quiet", mode, str(out.before))
            ok = proc.returncode == 0 and bool(out.before)
            if not ok:
                lines = proc.stderr.decode("utf-8", "replace").strip().splitlines()
                detail = lines[-1] if lines else f"git exited {proc.returncode}"
        except (OSError, subprocess.SubprocessError) as exc:
            ok = False
            detail = str(exc)
        if ok:
            out.rolled_back = True
            out.after = out.before
            out.messages.append(
                f"the rebuild this update requires failed, so the update to "
                f"{_short(out.attempted)} was abandoned: the checkout was reset "
                f"({mode}) to {_short(out.before)} and this run continues on the "
                f"code it started with, not re-executed. Run ./bootstrap.sh by "
                f"hand and read its output before the next run; a partly run "
                f"bootstrap may already have changed the virtual environment "
                f"(venv/) this run uses.")
            log.error(out.messages[-1])
            return
        out.reset_failed = True
        out.messages.append(
            f"the rebuild this update requires failed, and rolling the checkout "
            f"back to {_short(out.before)} failed too ({detail}). The checkout "
            f"is at {_short(out.attempted)} while this process runs "
            f"{_short(out.before)}'s code, which still imports modules from "
            f"disk; refusing to continue. Restore it by hand (git reset "
            f"{mode} {out.before}) or run ./bootstrap.sh, then start again.")
        log.error(out.messages[-1])

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
            log.warning("no bootstrap script at %s; the required rebuild cannot run", script)
            return False
        cmd = (["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script)]
               if os.name == "nt" else [str(script)])
        log.info("rebuilding: %s", " ".join(cmd))
        try:
            proc = subprocess.run(cmd, cwd=str(self.root), timeout=900,
                                  check=False, stdout=self.stdout)
        except (OSError, subprocess.SubprocessError) as exc:
            log.error("rebuild failed to start: %s", exc)
            return False
        if proc.returncode != 0:
            log.error("rebuild exited %s; the update will be rolled back",
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
