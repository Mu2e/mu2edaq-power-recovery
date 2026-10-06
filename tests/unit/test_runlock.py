"""The run lock and the stop script that trusts it (#12).

What these protect: two runs that can act on hardware never proceed at once,
and a stale record -- the old PID file's failure -- never causes a signal to
an unrelated process, including one that has reused the recorded pid.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mu2edaq_power_recovery import cli, runlock
from mu2edaq_power_recovery.runlock import LockBusy, RunLock

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX lock and shell")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC = PROJECT_ROOT / "src"
STOP = PROJECT_ROOT / "stop-mu2edaq-power-recovery.sh"

HOLDER = """
import sys, time
sys.path.insert(0, {src!r})
from mu2edaq_power_recovery.runlock import RunLock
RunLock({path!r}).acquire(cmdline=["mu2e-power-on", "--execute"])
print("locked", flush=True)
time.sleep(120)
"""


def _env():
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    return env


def start_holder(tmp_path, lock_path, script_name="holder.py"):
    """A child process holding *lock_path*; returns once it has the lock."""
    script = tmp_path / script_name
    script.write_text(HOLDER.format(src=str(SRC), path=str(lock_path)))
    proc = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE,
                            env=_env())
    assert proc.stdout.readline().strip() == b"locked"
    return proc


def stop(proc):
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)


def helper(*args):
    return subprocess.run([sys.executable, "-m", "mu2edaq_power_recovery.runlock",
                           *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, env=_env(), timeout=30)


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# the lock
# ---------------------------------------------------------------------------


def test_acquire_writes_the_holder_record(tmp_path):
    path = tmp_path / "sub" / "run.lock"
    lock = RunLock(path).acquire(cmdline=["mu2e-power-state"])
    try:
        record = json.loads(path.read_text())
        assert record["pid"] == os.getpid()
        assert record["cmdline"] == ["mu2e-power-state"]
        assert record["host"] and record["started_at"]
        assert runlock.probe(path)["held"]
    finally:
        lock.release()
    assert path.exists(), "the file stays; only the lock is released"
    assert not runlock.probe(path)["held"]


def test_a_second_acquire_is_busy_and_names_the_holder(tmp_path):
    path = tmp_path / "run.lock"
    holder = start_holder(tmp_path, path)
    try:
        with pytest.raises(LockBusy) as info:
            RunLock(path).acquire()
        assert info.value.holder["pid"] == holder.pid
        message = str(info.value)
        assert f"pid {holder.pid}" in message
        assert "mu2e-power-on --execute" in message
        assert "started" in message
    finally:
        stop(holder)
    # The kernel released it with the process: no cleanup was needed.
    RunLock(path).acquire().release()


def test_release_then_reacquire_in_process(tmp_path):
    path = tmp_path / "run.lock"
    lock = RunLock(path)
    lock.acquire()
    with pytest.raises(LockBusy):
        RunLock(path).acquire()      # a second descriptor contends too
    lock.release()
    lock.acquire()
    assert lock.held
    lock.release()
    lock.release()                   # idempotent


def test_status_and_pid_while_held(tmp_path):
    path = tmp_path / "run.lock"
    holder = start_holder(tmp_path, path)
    try:
        status = helper("status", "--lock-file", str(path))
        assert status.returncode == 0
        assert f"running: pid {holder.pid}" in status.stdout
        pid = helper("pid", "--lock-file", str(path))
        assert pid.returncode == 0
        assert pid.stdout.strip() == str(holder.pid)
    finally:
        stop(holder)


def test_a_stale_record_naming_a_live_unrelated_pid_is_not_reported(tmp_path):
    # The PID-reuse case: the record names a process that is alive, but the
    # lock is free, so the record is stale and that pid must not come back.
    path = tmp_path / "run.lock"
    sleeper = subprocess.Popen(["sleep", "60"])
    try:
        path.write_text(json.dumps({"pid": sleeper.pid, "started_at": "then",
                                    "cmdline": ["mu2e-power-on"], "host": "x"}))
        status = helper("status", "--lock-file", str(path))
        assert status.returncode == 1
        assert "not running (stale record:" in status.stdout
        pid = helper("pid", "--lock-file", str(path))
        assert pid.returncode == 1
        assert pid.stdout == ""
        assert alive(sleeper.pid)
    finally:
        sleeper.kill()
        sleeper.wait()


def test_a_missing_lock_file_is_not_running_and_is_not_created(tmp_path):
    path = tmp_path / "absent.lock"
    assert helper("pid", "--lock-file", str(path)).returncode == 1
    assert "no record" in helper("status", "--lock-file", str(path)).stdout
    assert not path.exists()


# ---------------------------------------------------------------------------
# the driver
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("argv,expected", [
    (["--phase", "assess"], True),
    (["--phase", "poweron"], True),
    (["--phase", "network"], True),
    (["--phase", "all"], True),
    (["--phase", "all", "--simulate"], False),
    (["--phase", "report"], False),
    (["--list-nodes"], False),
    (["--list-checks"], False),
])
def test_only_hardware_facing_invocations_lock(argv, expected):
    args = cli.build_parser().parse_args(argv)
    phases = cli.PHASE_ORDER if args.phase == "all" else [args.phase]
    assert cli.needs_run_lock(args, phases) is expected


def test_a_busy_lock_stops_the_driver_with_exit_2(tmp_path, capsys, monkeypatch):
    path = tmp_path / "run.lock"
    monkeypatch.setenv("MU2E_POWER_RECOVERY_RUN_LOCK_FILE", str(path))
    holder = start_holder(tmp_path, path)
    try:
        code = cli.main(["--phase", "assess", "--no-self-update", "-q",
                         "--database-url", f"sqlite:///{tmp_path / 'x.db'}"])
    finally:
        stop(holder)
    assert code == 2
    err = capsys.readouterr().err
    assert f"pid {holder.pid}" in err and "run lock" in err
    assert not (tmp_path / "x.db").exists()   # nothing was started


def test_the_driver_releases_the_lock_when_it_returns(tmp_path, monkeypatch):
    path = tmp_path / "run.lock"
    monkeypatch.setenv("MU2E_POWER_RECOVERY_RUN_LOCK_FILE", str(path))
    seen = {}

    def fake_run(*args, **kwargs):
        seen["held"] = runlock.probe(path)["held"]
        return 0

    monkeypatch.setattr(cli, "_run", fake_run)
    assert cli.main(["--phase", "assess", "--no-self-update", "-q"]) == 0
    assert seen["held"] is True
    assert not runlock.probe(path)["held"]


def test_the_lock_is_released_before_the_reexec(tmp_path, monkeypatch, settings):
    from mu2edaq_power_recovery import selfupdate
    path = tmp_path / "run.lock"
    lock = RunLock(path).acquire()
    result = selfupdate.UpdateResult(checked=True, updated=True, needs_reexec=True)
    monkeypatch.setattr(selfupdate.SelfUpdater, "run", lambda self: result)
    seen = {}

    def fake_reexec(self):
        seen["held_during_exec"] = lock.held or runlock.probe(path)["held"]
        raise SystemExit(0)

    monkeypatch.setattr(selfupdate.SelfUpdater, "reexec", fake_reexec)
    with pytest.raises(SystemExit):
        cli.do_self_update(settings, lock=lock)
    assert seen["held_during_exec"] is False


def test_the_default_lock_file_is_under_logs(settings):
    assert settings.get("run.lock_file") == "logs/power-recovery.lock"


# ---------------------------------------------------------------------------
# the stop script
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_venv(tmp_path):
    """VENV_DIR whose bin/python is this interpreter."""
    bindir = tmp_path / "venv" / "bin"
    bindir.mkdir(parents=True)
    python = bindir / "python"
    python.write_text(f"#!/bin/sh\nexec {sys.executable} \"$@\"\n")
    python.chmod(0o755)
    return tmp_path / "venv"


def run_stop(stub_venv, lock_path, *args, grace="5"):
    env = dict(os.environ)
    env.update(VENV_DIR=str(stub_venv), LOCK_FILE=str(lock_path), GRACE=grace)
    return subprocess.run(["sh", str(STOP), *args], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, env=env, timeout=60)


def test_stop_never_signals_a_stale_record(tmp_path, stub_venv):
    path = tmp_path / "run.lock"
    sleeper = subprocess.Popen(["sleep", "60"])
    try:
        path.write_text(json.dumps({"pid": sleeper.pid, "started_at": "then",
                                    "cmdline": ["mu2e-power-recovery"],
                                    "host": "here"}))
        for args in ((), ("--force",)):
            done = run_stop(stub_venv, path, *args)
            assert done.returncode == 0, done.stderr
            assert "no recovery run is active" in done.stdout
            time.sleep(0.2)
            assert alive(sleeper.pid), "a stale record's pid was signalled"
        status = run_stop(stub_venv, path, "--status")
        assert status.returncode == 0 and "not running" in status.stdout
    finally:
        sleeper.kill()
        sleeper.wait()
    assert path.exists()


def test_stop_refuses_a_holder_that_is_not_the_driver(tmp_path, stub_venv):
    # The lock is held, but by a process whose command line is not the
    # driver's: the identity check must refuse it.
    path = tmp_path / "run.lock"
    holder = start_holder(tmp_path, path, script_name="unrelated.py")
    try:
        done = run_stop(stub_venv, path)
        assert done.returncode == 1
        assert "refusing to signal" in done.stderr
        assert holder.poll() is None
    finally:
        stop(holder)


def test_stop_terminates_the_driver_holding_the_lock(tmp_path, stub_venv):
    path = tmp_path / "run.lock"
    holder = start_holder(tmp_path, path, script_name="fake_mu2edaq_power_recovery.py")
    try:
        done = run_stop(stub_venv, path)
        assert done.returncode == 0, done.stderr
        assert f"asking pid {holder.pid} to stop" in done.stdout
        assert "stopped cleanly" in done.stdout
        holder.wait(timeout=10)
        assert holder.returncode != 0      # SIGTERM
    finally:
        stop(holder)


def test_the_lock_file_is_ignored_by_git():
    """PR #32 review: untracked, it made phase 0 see a dirty tree forever."""
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    for name in ("logs/power-recovery.lock", "logs/start.pid"):
        res = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", name])
        assert res.returncode == 0, f"{name} is not ignored"


def test_phase_0_runs_only_under_the_lock(monkeypatch, tmp_path):
    """A report regeneration must not update a checkout a live run uses."""
    from mu2edaq_power_recovery import cli
    calls = []
    monkeypatch.setattr(cli, "do_self_update",
                        lambda *a, **k: calls.append(k.get("lock")) or None)
    monkeypatch.setenv("MU2E_POWER_RECOVERY_DATABASE_PATH", str(tmp_path / "r.db"))
    monkeypatch.setenv("MU2E_POWER_RECOVERY_REPORT_OUTPUT_DIR", str(tmp_path / "out"))
    cli.main_report(["--no-report"])
    assert calls == [], "phase 0 ran without the run lock"
