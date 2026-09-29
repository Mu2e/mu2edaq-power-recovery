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
#
# There is no PID file and no trap. The driver itself takes an exclusive OS
# lock on run.lock_file (logs/power-recovery.lock) for every run that can act
# on hardware, so a second concurrent run exits 2 naming the first, and the
# kernel releases the lock however the run ends. See
#   python -m mu2edaq_power_recovery.runlock status

set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$PROJECT_DIR"

VENV_DIR="${VENV_DIR:-$PROJECT_DIR/venv}"

if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "==> no virtual environment found; bootstrapping"
  "$PROJECT_DIR/bootstrap.sh"
fi

if [ $# -eq 0 ]; then
  set -- --phase assess
  echo "==> no arguments given; running the read-only survey (phase 1)"
fi

exec "$VENV_DIR/bin/mu2e-power-recovery" "$@"
