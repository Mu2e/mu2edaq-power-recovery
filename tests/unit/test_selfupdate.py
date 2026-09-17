"""Phase 0: the self-update check.

The behaviour being protected here is conservatism.  During an outage the tool
must never rewrite the operator's working tree, never reconcile a divergent
branch, and never let a network problem stop the run.
"""
from __future__ import annotations

import subprocess

import pytest

from mu2edaq_power_recovery.selfupdate import REEXEC_GUARD, SelfUpdater


@pytest.fixture
def repo(tmp_path):
    """A small git repository with an 'origin' it can be behind."""
    origin = tmp_path / "origin"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
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
    subprocess.run(["git", "clone", "-q", str(repo.parent / "origin"), str(clone)],
                   check=True)
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
