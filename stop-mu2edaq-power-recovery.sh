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

set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PID_FILE="${PID_FILE:-$PROJECT_DIR/logs/power-recovery.pid}"
GRACE="${GRACE:-30}"
FORCE=0
STATUS_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --status) STATUS_ONLY=1 ;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "stop-mu2edaq-power-recovery.sh: unknown option $arg" >&2; exit 2 ;;
  esac
done

if [ ! -f "$PID_FILE" ]; then
  echo "no recovery run is recorded as active (no $PID_FILE)"
  exit 0
fi

pid=$(cat "$PID_FILE" 2>/dev/null || echo "")
if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
  echo "stale pid file for pid ${pid:-unknown}; cleaning up"
  rm -f "$PID_FILE"
  exit 0
fi

if [ "$STATUS_ONLY" -eq 1 ]; then
  echo "a recovery run is active: pid $pid"
  exit 0
fi

if [ "$FORCE" -eq 1 ]; then
  echo "==> killing pid $pid immediately (--force)"
  kill -KILL "$pid" 2>/dev/null || true
  rm -f "$PID_FILE"
  echo "    note: the run store may not have recorded the interruption."
  exit 0
fi

echo "==> asking pid $pid to stop (SIGTERM), waiting up to ${GRACE}s"
kill -TERM "$pid" 2>/dev/null || true

waited=0
while [ "$waited" -lt "$GRACE" ]; do
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "    stopped cleanly after ${waited}s"
    exit 0
  fi
  sleep 1
  waited=$((waited + 1))
done

echo "==> still running after ${GRACE}s; sending SIGKILL"
kill -KILL "$pid" 2>/dev/null || true
sleep 1
rm -f "$PID_FILE"

# A run killed mid-phase leaves partial pages; say so rather than let the
# operator read a half-written report as a complete one.
echo "    killed. The report pages may be incomplete -- re-run"
echo "    mu2e-power-report to regenerate them from the run store."
