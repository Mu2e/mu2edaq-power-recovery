#!/bin/sh
# Stop a running recovery.
#
# SIGTERM first, SIGKILL only after a grace period and only if needed.
#
# The driver installs a SIGTERM handler that routes into the same path as
# Ctrl-C: it records the interruption, marks the run 'interrupted' in the run
# store, and destroys the run's private Kerberos caches -- which can hold
# root-capable service tickets.
#
# --force skips the wait and sends SIGKILL, which none of that survives: the
# store is left saying 'running' and the caches remain. Check with `klist -l`
# and destroy them by name if you use it.
#
#   ./stop-mu2edaq-power-recovery.sh            # 30-second grace period
#   ./stop-mu2edaq-power-recovery.sh --force    # kill immediately
#   ./stop-mu2edaq-power-recovery.sh --status   # report, change nothing
#
# The target comes from the driver's run lock, never from a bare PID file:
# `python -m mu2edaq_power_recovery.runlock pid` prints a pid only while the
# lock is held, and a free lock means any recorded pid is stale. Before every
# signal the pid's command line is checked again (`ps -o args=`) for
# mu2edaq_power_recovery or mu2e-power, so a recycled pid is never signalled.
#
# Environment: VENV_DIR (default ./venv), LOCK_FILE (default run.lock_file
# from the configuration), GRACE (seconds, default 30).

set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VENV_DIR="${VENV_DIR:-$PROJECT_DIR/venv}"
LOCK_FILE="${LOCK_FILE:-}"
GRACE="${GRACE:-30}"
FORCE=0
STATUS_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --status) STATUS_ONLY=1 ;;
    -h|--help) sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "stop-mu2edaq-power-recovery.sh: unknown option $arg" >&2; exit 2 ;;
  esac
done

PYTHON="$VENV_DIR/bin/python"
if [ ! -x "$PYTHON" ]; then
  echo "error: no python at $PYTHON (set VENV_DIR, or run ./bootstrap.sh)" >&2
  exit 2
fi

# The helper from this checkout, whatever the venv has installed.
runlock() {
  if [ -n "$LOCK_FILE" ]; then
    PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" \
      "$PYTHON" -m mu2edaq_power_recovery.runlock "$@" --lock-file "$LOCK_FILE"
  else
    PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" \
      "$PYTHON" -m mu2edaq_power_recovery.runlock "$@"
  fi
}

# True while $1 is alive AND is the recovery driver. Checked before every
# signal, not once: the pid could exit and be reused while we wait.
is_driver() {
  args=$(ps -o args= -p "$1" 2>/dev/null || true)
  case "$args" in
    *mu2edaq_power_recovery*|*mu2e-power*) return 0 ;;
    *) return 1 ;;
  esac
}

# Still the lock holder? (A run that exited released it.)
holds_lock() {
  current=$(runlock pid 2>/dev/null || true)
  [ "$current" = "$1" ]
}

if [ "$STATUS_ONLY" -eq 1 ]; then
  rc=0
  runlock status || rc=$?
  [ "$rc" -le 1 ] && exit 0
  exit "$rc"
fi

rc=0
pid=$(runlock pid) || rc=$?
if [ "$rc" -eq 1 ]; then
  echo "no recovery run is active (the run lock is free); nothing signalled"
  exit 0
elif [ "$rc" -ne 0 ] || [ -z "$pid" ]; then
  echo "error: could not determine the run lock holder (exit $rc)" >&2
  exit 2
fi

if ! is_driver "$pid"; then
  echo "error: the run lock names pid $pid, but that process is not the" >&2
  echo "       recovery driver ($(ps -o args= -p "$pid" 2>/dev/null || echo 'gone'));" >&2
  echo "       refusing to signal it." >&2
  exit 1
fi

if [ "$FORCE" -eq 1 ]; then
  echo "==> killing pid $pid immediately (--force)"
  is_driver "$pid" && kill -KILL "$pid" 2>/dev/null || true
  echo "    note: the run store may not have recorded the interruption."
  exit 0
fi

echo "==> asking pid $pid to stop (SIGTERM), waiting up to ${GRACE}s"
is_driver "$pid" && kill -TERM "$pid" 2>/dev/null || true

waited=0
while [ "$waited" -lt "$GRACE" ]; do
  if ! kill -0 "$pid" 2>/dev/null || ! is_driver "$pid" || ! holds_lock "$pid"; then
    echo "    stopped cleanly after ${waited}s"
    exit 0
  fi
  sleep 1
  waited=$((waited + 1))
done

if ! is_driver "$pid" || ! holds_lock "$pid"; then
  echo "    stopped after ${GRACE}s"
  exit 0
fi
echo "==> still running after ${GRACE}s; sending SIGKILL"
kill -KILL "$pid" 2>/dev/null || true

# The lock file is left in place on purpose: the lock, not the file, is
# authoritative, and the kernel released it with the process.
#
# A run killed mid-phase leaves partial pages; say so rather than let the
# operator read a half-written report as a complete one.
echo "    killed. The report pages may be incomplete -- re-run"
echo "    mu2e-power-report to regenerate them from the run store."
