#!/bin/sh
# Start a power-outage recovery run.
#
# Bootstraps the venv if it is missing, then hands every argument straight to
# mu2e-power-recovery.  This is the entry point an operator is expected to use
# from a fresh clone, because it works before the venv exists.
#
#   ./start-mu2edaq-power-recovery.sh --phase assess
#   ./start-mu2edaq-power-recovery.sh --phase all --execute --label "Sept outage"
#   ./start-mu2edaq-power-recovery.sh --phase all --simulate     # rehearsal
#
# With no arguments it runs phase 1 only -- the read-only survey -- because an
# accidental bare invocation should look at the cluster, not change it.

set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$PROJECT_DIR"

VENV_DIR="${VENV_DIR:-$PROJECT_DIR/venv}"
PID_FILE="${PID_FILE:-$PROJECT_DIR/logs/power-recovery.pid}"

if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "==> no virtual environment found; bootstrapping"
  "$PROJECT_DIR/bootstrap.sh"
fi

mkdir -p "$PROJECT_DIR/logs"

# A second concurrent run would drive the same cluster from two directions and
# interleave its power commands, so refuse rather than race.
if [ -f "$PID_FILE" ]; then
  existing=$(cat "$PID_FILE" 2>/dev/null || echo "")
  if [ -n "$existing" ] && kill -0 "$existing" 2>/dev/null; then
    echo "error: a recovery run is already active (pid $existing)." >&2
    echo "       Use ./stop-mu2edaq-power-recovery.sh first, or wait for it." >&2
    exit 1
  fi
  rm -f "$PID_FILE"
fi

if [ $# -eq 0 ]; then
  set -- --phase assess
  echo "==> no arguments given; running the read-only survey (phase 1)"
fi

echo "$$" > "$PID_FILE"
# The pid file must not outlive the run, however it ends.
trap 'rm -f "$PID_FILE"' EXIT INT TERM

exec "$VENV_DIR/bin/mu2e-power-recovery" "$@"
