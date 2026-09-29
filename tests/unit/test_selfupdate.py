"""Phase 0: the self-update check.

The behaviour being protected here is conservatism.  During an outage the tool
must never rewrite the operator's working tree, never reconcile a divergent
branch, and never let a network problem stop the run.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from mu2edaq_power_recovery.selfupdate import REEXEC_GUARD, SelfUpdater


@pytest.fixture(autouse=True)
def hermetic_git(monkeypatch):
    """Keep the developer's git configuration out of these repositories.

    ``init.defaultBranch`` in particular decides what a bare origin's HEAD
    names; on a host where it is unset (git's default is still ``master``)
    the clones below checked out an unborn branch and the pushes were refused.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture
def repo(tmp_path):
    """A small git repository with an 'origin' it can be behind."""
    origin = tmp_path / "origin"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    # Name the branch explicitly rather than rely on init.defaultBranch;
    # symbolic-ref rather than ``init -b`` so git older than 2.28 works too.
    subprocess.run(["git", "-C", str(origin), "symbolic-ref", "HEAD",
                    "refs/heads/main"], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], check=True)
    for key, value in (("user.email", "t@example.invalid"), ("user.name", "Test")):
        subprocess.run(["git", "-C", str(work), "config", key, value], check=True)
    (work / "README.md").write_text("one\n")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-qm", "first"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "HEAD:main"],
                   check=True)
    subprocess.run(["git", "-C", str(work), "branch", "-M", "main"], check=True)
    subprocess.run(["git", "-C", str(work), "branch", "--set-upstream-to",
                    "origin/main", "main"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return work


def advance_origin(repo, message="second"):
    """Add a commit to origin that the working copy does not have."""
    clone = repo.parent / "other"
    subprocess.run(["git", "clone", "-q", "-b", "main", str(repo.parent / "origin"),
                    str(clone)], check=True)
    for key, value in (("user.email", "t@example.invalid"), ("user.name", "Test")):
        subprocess.run(["git", "-C", str(clone), "config", key, value], check=True)
    (clone / "NEW.md").write_text("two\n")
    subprocess.run(["git", "-C", str(clone), "add", "."], check=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-qm", message], check=True)
    subprocess.run(["git", "-C", str(clone), "push", "-q", "origin", "HEAD:main"],
                   check=True)


def test_disabled_by_configuration(settings, repo):
    settings.set("selfupdate.enabled", False)
    result = SelfUpdater(settings, root=repo).run()
    assert not result.checked
    assert "disabled" in result.messages[0]


def test_a_non_git_directory_is_skipped(settings, tmp_path):
    result = SelfUpdater(settings, root=tmp_path / "plain").run()
    assert not result.checked
    assert not result.updated


def test_already_up_to_date(settings, repo):
    result = SelfUpdater(settings, root=repo).run()
    assert result.checked
    assert not result.updated
    assert result.behind == 0


def test_a_dirty_tree_is_reported_not_stashed(settings, repo):
    # An operator's local edit must survive, and they must be told why the
    # update did not happen.
    (repo / "README.md").write_text("locally modified\n")
    advance_origin(repo)
    result = SelfUpdater(settings, root=repo).run()
    assert result.dirty
    assert not result.updated
    assert "local modifications" in result.messages[-1]
    assert (repo / "README.md").read_text() == "locally modified\n"


def test_a_fast_forward_is_applied(settings, repo):
    advance_origin(repo)
    result = SelfUpdater(settings, root=repo).run()
    assert result.updated
    assert result.behind == 1
    assert result.before != result.after
    assert result.needs_reexec
    assert (repo / "NEW.md").exists()


def test_a_divergent_branch_is_refused(settings, repo):
    # Local commits the remote does not have: fast-forward is impossible and
    # this tool will not rebase on the operator's behalf.
    advance_origin(repo)
    (repo / "LOCAL.md").write_text("local work\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "local"], check=True)
    result = SelfUpdater(settings, root=repo).run()
    assert not result.updated
    assert result.ahead == 1
    assert "refusing to fast-forward" in result.messages[-1]


def test_the_reexec_guard_prevents_a_second_check(settings, repo, monkeypatch):
    monkeypatch.setenv(REEXEC_GUARD, "1")
    result = SelfUpdater(settings, root=repo).run()
    assert not result.checked
    assert "already updated" in result.messages[0]


@pytest.mark.parametrize("changed,expected", [
    (["src/mu2edaq_power_recovery/cli.py"], False),   # pure Python: no rebuild
    (["pyproject.toml"], True),
    (["requirements.txt"], True),
    (["src/cpp/probe.cpp"], True),
    (["src/include/mu2eprobe/probe.hpp"], True),
    (["README.md"], False),
])
def test_rebuild_is_triggered_only_by_build_inputs(settings, repo, changed, expected):
    assert SelfUpdater(settings, root=repo).needs_rebuild(changed) is expected


# ---------------------------------------------------------------------------
# #21: a required rebuild that fails abandons the update
# ---------------------------------------------------------------------------


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          stdout=subprocess.PIPE).stdout.decode().strip()


def advance_origin_with(repo, files, message="update"):
    """Commit *files* ({path: text or (text, mode)}) to origin."""
    clone = repo.parent / f"other-{abs(hash(message)) % 10000}"
    subprocess.run(["git", "clone", "-q", "-b", "main", str(repo.parent / "origin"),
                    str(clone)], check=True)
    for key, value in (("user.email", "t@example.invalid"), ("user.name", "Test")):
        subprocess.run(["git", "-C", str(clone), "config", key, value], check=True)
    for name, content in files.items():
        text, mode = content if isinstance(content, tuple) else (content, 0o644)
        target = clone / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        target.chmod(mode)
    subprocess.run(["git", "-C", str(clone), "add", "."], check=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-qm", message], check=True)
    subprocess.run(["git", "-C", str(clone), "push", "-q", "origin", "HEAD:main"],
                   check=True)
    return _git(clone, "rev-parse", "HEAD")


def _assert_rolled_back(result, repo, before, attempted):
    assert _git(repo, "rev-parse", "HEAD") == before
    assert result.update_failed and result.rolled_back
    assert not result.reset_failed
    assert not result.needs_reexec
    assert result.before == before and result.after == before
    assert result.attempted == attempted
    assert "./bootstrap.sh" in result.messages[-1]
    assert "partly run bootstrap" in result.messages[-1]
    data = result.as_dict()
    for key in ("update_failed", "rolled_back", "attempted", "reset_failed",
                "rebuild_required"):
        assert key in data


def test_the_pre_update_sha_is_recorded_in_full(settings, repo):
    advance_origin(repo)
    before = _git(repo, "rev-parse", "HEAD")
    result = SelfUpdater(settings, root=repo).run()
    assert result.before == before and len(before) == 40
    assert result.after == _git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize("changed", ["pyproject.toml", "requirements.txt",
                                     "src/cpp/x.cpp"])
def test_a_failed_rebuild_rolls_the_checkout_back(settings, repo, monkeypatch,
                                                  changed):
    before = _git(repo, "rev-parse", "HEAD")
    attempted = advance_origin_with(repo, {changed: "new\n"}, message=changed)
    monkeypatch.setattr(SelfUpdater, "rebuild", lambda self: False)
    result = SelfUpdater(settings, root=repo).run()
    assert result.rebuild_required
    _assert_rolled_back(result, repo, before, attempted)
    assert not (repo / changed).exists()


def test_a_bootstrap_that_exits_1_rolls_back(settings, repo):
    before = _git(repo, "rev-parse", "HEAD")
    attempted = advance_origin_with(repo, {
        "bootstrap.sh": ("#!/bin/sh\nexit 1\n", 0o755),
        "requirements.txt": "newdep\n"}, message="bootstrap-fails")
    result = SelfUpdater(settings, root=repo, stdout=subprocess.DEVNULL).run()
    _assert_rolled_back(result, repo, before, attempted)


def test_a_missing_bootstrap_rolls_back(settings, repo):
    before = _git(repo, "rev-parse", "HEAD")
    attempted = advance_origin_with(repo, {"requirements.txt": "newdep\n"},
                                    message="no-bootstrap")
    assert not (repo / "bootstrap.sh").exists()
    result = SelfUpdater(settings, root=repo).run()
    _assert_rolled_back(result, repo, before, attempted)


def test_a_dirty_tree_under_allow_dirty_rolls_back_with_keep(settings, repo,
                                                             monkeypatch):
    settings.set("selfupdate.allow_dirty", True)
    (repo / "README.md").write_text("operator's local edit\n")
    before = _git(repo, "rev-parse", "HEAD")
    attempted = advance_origin_with(repo, {"pyproject.toml": "x\n"}, message="dirty")
    monkeypatch.setattr(SelfUpdater, "rebuild", lambda self: False)
    calls = []
    real = SelfUpdater._git

    def spy(self, *args, **kwargs):
        calls.append(args)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(SelfUpdater, "_git", spy)
    result = SelfUpdater(settings, root=repo).run()
    _assert_rolled_back(result, repo, before, attempted)
    assert any(a[:1] == ("reset",) and "--keep" in a for a in calls)
    assert (repo / "README.md").read_text() == "operator's local edit\n"


def test_a_failed_reset_is_reported_for_exit_2(settings, repo, monkeypatch):
    advance_origin_with(repo, {"pyproject.toml": "x\n"}, message="reset-fails")
    monkeypatch.setattr(SelfUpdater, "rebuild", lambda self: False)
    real = SelfUpdater._git

    def failing_reset(self, *args, **kwargs):
        if args[:1] == ("reset",):
            return subprocess.CompletedProcess(args, 128, b"", b"fatal: nope\n")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(SelfUpdater, "_git", failing_reset)
    result = SelfUpdater(settings, root=repo).run()
    assert result.reset_failed and result.update_failed
    assert not result.rolled_back and not result.needs_reexec
    assert "refusing to continue" in result.messages[-1]


def test_a_successful_rebuild_still_reexecs(settings, repo, monkeypatch):
    advance_origin_with(repo, {"pyproject.toml": "x\n"}, message="ok")
    monkeypatch.setattr(SelfUpdater, "rebuild", lambda self: True)
    result = SelfUpdater(settings, root=repo).run()
    assert result.rebuilt and result.needs_reexec and not result.update_failed


def test_the_driver_exits_2_when_the_rollback_failed(monkeypatch, capsys, tmp_path):
    from mu2edaq_power_recovery import cli
    from mu2edaq_power_recovery.selfupdate import UpdateResult
    failed = UpdateResult(checked=True, update_failed=True, reset_failed=True,
                          messages=["rolling back failed; refusing to continue"])
    monkeypatch.setattr(SelfUpdater, "run", lambda self: failed)
    code = cli.main(["--phase", "assess", "-q",
                     "--database-url", f"sqlite:///{tmp_path / 'x.db'}"])
    assert code == 2
    assert "refusing to continue" in capsys.readouterr().err
    assert not (tmp_path / "x.db").exists()


def test_the_update_result_is_recorded_in_run_provenance(monkeypatch, tmp_path):
    """The run row's version carries what phase 0 did (#21)."""
    import sqlite3
    from mu2edaq_power_recovery import cli
    from mu2edaq_power_recovery.orchestrator import Orchestrator
    from mu2edaq_power_recovery.selfupdate import UpdateResult
    rolled = UpdateResult(checked=True, update_failed=True, rolled_back=True,
                          before="a" * 40, after="a" * 40, attempted="b" * 40,
                          messages=["rolled back"])
    monkeypatch.setattr(SelfUpdater, "run", lambda self: rolled)
    # A real (non-simulated) run is needed for phase 0 to happen; answer it
    # from the simulator underneath so nothing is contacted.
    real_init = Orchestrator.__init__

    def simulated(self, settings, simulate=False, **kw):
        real_init(self, settings, simulate=True, **kw)

    monkeypatch.setattr(Orchestrator, "__init__", simulated)
    db = tmp_path / "prov.db"
    cli.main(["--phase", "assess", "-q", "--node", "mu2e-trk-01",
              "--database-url", f"sqlite:///{db}",
              "--output-dir", str(tmp_path / "html"), "--no-report"])
    row = sqlite3.connect(str(db)).execute(
        "select version from runs order by id desc limit 1").fetchone()
    version = json.loads(row[0])
    assert version["selfupdate"]["rolled_back"] is True
    assert version["selfupdate"]["attempted"] == "b" * 40
    assert version["selfupdate"]["after"] == "a" * 40
