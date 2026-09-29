"""The run lock: one hardware-facing recovery run per checkout at a time.

A second concurrent run would drive the same cluster from two directions and
interleave its power commands. The start scripts used to prevent that with a
PID file, which had two defects (#12): ``exec`` discarded the shell trap that
was meant to remove it, so every normal run left it behind; and the start and
stop scripts then trusted the recorded integer, which after PID reuse can name
an unrelated process that ``stop`` would signal.

The driver now owns an operating-system lock instead:

* ``os.open(O_CREAT | O_RDWR)`` then ``fcntl.flock(LOCK_EX | LOCK_NB)`` on
  POSIX, ``msvcrt.locking(LK_NBLCK, 1)`` on Windows. Acquisition is atomic,
  so two simultaneous starts cannot both proceed, and the kernel releases the
  lock when the process ends however it ends -- a crash or SIGKILL leaves the
  file but never a held lock.
* After locking, the holder writes ``{pid, started_at, cmdline, host}`` into
  the file. That record is *information*, not authority: it is only believed
  while the lock is actually held. A free lock means the recorded PID is stale
  and must never be signalled; ``python -m mu2edaq_power_recovery.runlock pid``
  prints a PID only when the lock is held.
* The file is left in place on release. Deleting it would open a race (a
  second process could lock the old inode while a third creates a new one).

The descriptor is not inheritable (``os.open`` default since Python 3.4), so
the ssh/ipmitool children never carry the lock beyond the driver's lifetime.
Before the phase-0 re-exec the driver releases the lock and the re-executed
process takes it again through the normal path.
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

if os.name == "nt":  # pragma: no cover - exercised on Windows only
    import msvcrt
    fcntl = None
else:
    import fcntl
    msvcrt = None

#: Built-in default for ``run.lock_file``, relative to the project root.
DEFAULT_LOCK_FILE = "logs/power-recovery.lock"

#: Byte offset msvcrt locks on Windows. Well past the JSON record, because a
#: Windows byte-range lock is mandatory: locking byte 0 would stop ``status``
#: from reading the holder's record.
_WIN_LOCK_OFFSET = 1 << 30


class LockError(RuntimeError):
    """The lock file could not be opened or locked for a reason other than
    another holder."""


class LockBusy(LockError):
    """Another process holds the lock; :attr:`holder` is its record."""

    def __init__(self, path: Path, holder: Optional[Dict[str, Any]]):
        self.path = path
        self.holder = holder or {}
        super().__init__(
            f"another recovery run holds the run lock {path}: "
            f"{describe(self.holder)}. Wait for it to finish, or stop it with "
            f"./stop-mu2edaq-power-recovery.sh")


def describe(holder: Optional[Dict[str, Any]]) -> str:
    """'pid 123 on host x, started 2026-..., command: mu2e-power-on --execute'."""
    if not holder:
        return "no holder record (the holder may be starting up)"
    cmdline = holder.get("cmdline")
    if isinstance(cmdline, list):
        cmdline = " ".join(str(part) for part in cmdline)
    parts = [f"pid {holder.get('pid', '?')}"]
    if holder.get("host"):
        parts.append(f"on host {holder['host']}")
    parts.append(f"started {holder.get('started_at', '?')}")
    if cmdline:
        parts.append(f"command: {cmdline}")
    return ", ".join(parts)


def _is_contention(exc: OSError) -> bool:
    return exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK,
                         getattr(errno, "EDEADLK", -1))


def _lock_fd(fd: int) -> None:
    """Take the exclusive lock without blocking; OSError on contention."""
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)   # pragma: no cover - Windows
    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)        # pragma: no cover - Windows


def _unlock_fd(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)   # pragma: no cover - Windows
    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)        # pragma: no cover - Windows


def read_record(path: Path) -> Optional[Dict[str, Any]]:
    """The last holder's record, or None when there is none to read."""
    try:
        text = Path(path).read_text()
    except OSError:
        return None
    if not text.strip():
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return {"raw": text.strip()[:200]}
    return data if isinstance(data, dict) else {"raw": text.strip()[:200]}


class RunLock:
    """An exclusive, non-blocking lock on *path*."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.fd: Optional[int] = None

    @property
    def held(self) -> bool:
        return self.fd is not None

    def acquire(self, cmdline: Optional[Sequence[str]] = None) -> "RunLock":
        """Take the lock and write this process's record, or raise.

        :class:`LockBusy` names the current holder; :class:`LockError` is
        anything else (an unwritable directory, say).
        """
        if self.fd is not None:
            return self
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as exc:
            raise LockError(f"cannot open the run lock {self.path}: {exc}") from exc
        try:
            _lock_fd(fd)
        except OSError as exc:
            os.close(fd)
            if _is_contention(exc):
                raise LockBusy(self.path, read_record(self.path)) from None
            raise LockError(f"cannot lock {self.path}: {exc}") from exc
        self.fd = fd
        try:
            self._write_record(cmdline)
        except OSError as exc:
            self.release()
            raise LockError(f"cannot write the run lock record {self.path}: "
                            f"{exc}") from exc
        return self

    def _write_record(self, cmdline: Optional[Sequence[str]]) -> None:
        record = {
            "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "cmdline": list(cmdline if cmdline is not None else sys.argv),
            "host": socket.gethostname(),
        }
        data = (json.dumps(record) + "\n").encode("utf-8")
        assert self.fd is not None
        os.lseek(self.fd, 0, os.SEEK_SET)
        os.ftruncate(self.fd, 0)
        os.write(self.fd, data)
        os.fsync(self.fd)

    def release(self) -> None:
        """Drop the lock; the file (and its now-stale record) stays."""
        if self.fd is None:
            return
        fd, self.fd = self.fd, None
        try:
            _unlock_fd(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    def __enter__(self) -> "RunLock":
        return self.acquire()

    def __exit__(self, *exc: Any) -> None:
        self.release()


def probe(path: Path) -> Dict[str, Any]:
    """Is the lock held, and by whom? Never signals, never deletes.

    Returns ``{"held": bool, "record": dict|None, "path": str}``. A missing
    file is reported free without being created. A free lock is taken for an
    instant to find out, so a start in that instant would see "busy"; the
    caller is a status query, and that window is accepted.
    """
    path = Path(path)
    result: Dict[str, Any] = {"held": False, "record": None, "path": str(path)}
    if not path.exists():
        return result
    try:
        fd = os.open(str(path), os.O_RDWR)
    except OSError as exc:
        raise LockError(f"cannot open the run lock {path}: {exc}") from exc
    try:
        try:
            _lock_fd(fd)
        except OSError as exc:
            if not _is_contention(exc):
                raise LockError(f"cannot test {path}: {exc}") from exc
            result["held"] = True
        else:
            _unlock_fd(fd)
    finally:
        os.close(fd)
    record = read_record(path)
    if result["held"] and not (record and "pid" in record):
        # The holder writes its record immediately after locking; give a
        # starting process a moment rather than report "no record".
        for _ in range(10):
            time.sleep(0.1)
            record = read_record(path)
            if record and "pid" in record:
                break
    result["record"] = record
    return result


def default_path(config: Optional[str] = None,
                 env_file: Optional[str] = None) -> Path:
    """``run.lock_file`` resolved the way the driver resolves it."""
    try:
        from .settings import load as load_settings
        settings = load_settings(
            config_file=Path(config) if config else None,
            env_file=Path(env_file) if env_file else None)
        resolved = settings.resolve_path(
            settings.get("run.lock_file") or DEFAULT_LOCK_FILE)
        if resolved is not None:
            return resolved
    except Exception:  # noqa: BLE001 - a broken config must not hide the lock
        pass
    from .settings import PROJECT_ROOT
    return PROJECT_ROOT / DEFAULT_LOCK_FILE


# ---------------------------------------------------------------------------
# python -m mu2edaq_power_recovery.runlock status|pid
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mu2edaq_power_recovery.runlock",
        description="Report on the power-recovery run lock. Used by the stop "
                    "scripts; never signals anything itself.",
        epilog="""
exit status
  status  0 a run holds the lock; 1 no run holds it; 2 the lock could not be
          examined
  pid     0 and the holder's pid on stdout; 1 the lock is free (any recorded
          pid is stale and must not be signalled) or held from another host;
          2 the lock could not be examined, or is held with no readable record
""",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["status", "pid"])
    parser.add_argument("--lock-file", metavar="PATH",
                        help="lock file (default: run.lock_file from the "
                             "configuration, logs/power-recovery.lock)")
    parser.add_argument("--config", metavar="FILE",
                        help="main configuration file used to find "
                             "run.lock_file")
    parser.add_argument("--env-file", metavar="FILE", help="dotenv file")
    parser.add_argument("--json", action="store_true",
                        help="status as one JSON object")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    path = (Path(args.lock_file).expanduser() if args.lock_file
            else default_path(args.config, args.env_file))
    try:
        state = probe(path)
    except LockError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    record = state["record"]

    if args.command == "pid":
        if not state["held"]:
            if record and record.get("pid"):
                print(f"not running: the lock is free, so pid {record.get('pid')} "
                      f"in {path} is stale and is not reported", file=sys.stderr)
            return 1
        if not (record and isinstance(record.get("pid"), int)):
            print(f"error: {path} is locked but holds no readable record",
                  file=sys.stderr)
            return 2
        host = record.get("host")
        if host and host != socket.gethostname():
            print(f"the lock is held by pid {record['pid']} on host {host}, not "
                  f"this host; it cannot be signalled from here", file=sys.stderr)
            return 1
        print(record["pid"])
        return 0

    if args.json:
        print(json.dumps({"running": state["held"], "lock_file": str(path),
                          "holder": record if state["held"] else None,
                          "stale_record": None if state["held"] else record}))
    elif state["held"]:
        print(f"running: {describe(record)} (lock {path})")
    elif record:
        print(f"not running (stale record: {describe(record)}; lock {path} is "
              f"free)")
    else:
        print(f"not running (no record in {path})")
    return 0 if state["held"] else 1


if __name__ == "__main__":
    sys.exit(main())
