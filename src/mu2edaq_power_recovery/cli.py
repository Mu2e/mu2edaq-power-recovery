"""Command-line driver.

One argument parser serves the general driver and the four single-phase
entry points; the specialised ones simply pin ``--phase`` and adjust the help
text, so their options can never drift apart from the driver's.

Option precedence is the project-wide rule: command line beats environment,
which beats ``config/.env``, which beats the YAML config.  That is implemented
by collecting the flags into a dotted-path mapping and handing it to
:func:`settings.load` as the last layer, rather than by reading flags at the
point of use.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from . import __version__, console
from .checks import DESCRIPTIONS, Status
from .logsetup import configure as configure_logging
from .orchestrator import Orchestrator
from .phases import phase1_assess, phase2_poweron, phase3_network, phase4_report
from .report import Publisher, ReportWriter
from .runlock import DEFAULT_LOCK_FILE, LockError, RunLock
from .selfupdate import SelfUpdater, UpdateResult
from .phases.phase2_poweron import SequenceSelectionError, plan_sequence
from .settings import ARM_ENV, ConfigError, load as load_settings
from .topology import TopologyError

log = logging.getLogger(__name__)

PHASE_ORDER = ["assess", "poweron", "network", "report"]

EPILOG = """
examples
  # read-only survey of MC-2 and the teststand, refreshing the report pages
  mu2e-power-recovery --phase assess

  # rehearse the whole four-phase run with no cluster attached
  mu2e-power-recovery --phase all --simulate

  # the real thing: survey, then power on for real, then check the fabric
  mu2e-power-recovery --phase all --execute \\
      --principal anorman@FNAL.GOV --root-principal anorman/root@FNAL.GOV \\
      --label "Sept 2026 planned outage"

  # resume a power-on that stopped at the dcs stage, after fixing it
  mu2e-power-on --execute --from dcs

  # regenerate and post the report for an earlier run (no new run is made)
  mu2e-power-report --run-id 17 --post-ecl

  # machine-readable: stdout is one JSON object
  mu2e-power-recovery --phase all --simulate --json | jq .status

live power commands
  A run is a dry run unless this invocation authorises it, in one of two ways:
    --execute                    on the command line; or
    MU2E_POWER_RECOVERY_ARM=<run.label>
                                 in the process environment, together with
                                 run.dry_run: false in the configuration and a
                                 run.label equal to the token.
  run.dry_run: false on its own (YAML, config/.env or environment) does not
  arm anything: it is refused with exit 2. --simulate always wins.

scope in phase 2
  --location drops stages in other locations. --node cuts the stages holding
  the named nodes down to them; earlier stages are VERIFY-ONLY (never sent a
  power command) and a predecessor that is not up stops the run before the
  requested nodes. Later stages are not run. A name, stage or range that
  cannot be honoured exactly is an error (exit 2) before any credential.

exit status
  0  every phase completed and nothing failed
  1  a phase completed but one or more nodes failed their checks
  2  the run could not start (configuration, credentials, no gateway,
     live-run authorisation, stage or node selection, a --run-id that is
     not in the store, another run holding the run lock, a failed update
     that could not be rolled back), or it stopped on an internal error
  3  interrupted by the operator

run lock
  Phases 1-3 run for real (not --simulate) take an exclusive lock on
  run.lock_file (logs/power-recovery.lock) before phase 0; a second such run
  exits 2 naming the holder. --simulate, --list-* and report-only runs do not.
  python -m mu2edaq_power_recovery.runlock status   shows the holder.
"""


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_parser(prog: Optional[str] = None, description: Optional[str] = None,
                 fixed_phase: Optional[str] = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description=description or
        "Assess, power on, verify and report on the Mu2e DAQ clusters after a "
        "power outage.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version",
                        version=f"mu2edaq-power-recovery {__version__}")

    if fixed_phase is None:
        parser.add_argument("--phase", default="assess",
                            choices=PHASE_ORDER + ["all"],
                            help="phase to run (default: assess). 'all' runs "
                                 "1-4 in order.")

    run = parser.add_argument_group("run control")
    run.add_argument("--label", metavar="TEXT",
                     help="label for this recovery, shown in the report and the "
                          "logbook entry")
    run.add_argument("--execute", action="store_true",
                     help="authorise live power commands for this invocation. "
                          "Without it (or the MU2E_POWER_RECOVERY_ARM token) the "
                          "run is a dry run: states are read, nothing is switched "
                          "on. run.dry_run: false in configuration alone is "
                          "refused.")
    run.add_argument("--simulate", action="store_true",
                     help="contact nothing at all; answer every command from a "
                          "built-in script. For rehearsal and for testing the "
                          "report off site.")
    run.add_argument("--location", action="append", metavar="NAME",
                     help="limit the run to this location (mc2, mc1, teststand). "
                          "Repeatable; defaults to topology.locations.")
    run.add_argument("--node", action="append", metavar="HOST",
                     help="limit the run to these nodes. Repeatable. Accepts "
                          "short or fully-qualified names. In phase 2 only these "
                          "nodes are powered; earlier stages are verified, never "
                          "powered.")
    run.add_argument("--continue-on-error", action="store_true",
                     help="in phase 2, carry on to the next stage even when a "
                          "stage does not meet its requirement")
    run.add_argument("--from", dest="from_stage", metavar="STAGE",
                     help="start the power-on sequence at this stage (an "
                          "unknown name is an error)")
    run.add_argument("--until", dest="until_stage", metavar="STAGE",
                     help="stop the power-on sequence after this stage (an "
                          "unknown name is an error)")
    run.add_argument("--include-failed", action="store_true",
                     help="in phase 3, probe nodes that failed an earlier phase "
                          "instead of skipping them")

    creds = parser.add_argument_group("credentials")
    creds.add_argument("--principal", metavar="PRINCIPAL",
                       help="Kerberos principal for ordinary logins. A password "
                            "is prompted for if there is no usable ticket.")
    creds.add_argument("--root-principal", metavar="PRINCIPAL",
                       help="Kerberos principal with root access on the nodes")
    creds.add_argument("--no-prompt", action="store_true",
                       help="never prompt for a password; fail with instructions "
                            "instead (for unattended runs)")
    creds.add_argument("--vault-addr", metavar="URL", help="HashiCorp Vault address")

    report = parser.add_argument_group("report")
    report.add_argument("--output-dir", metavar="DIR",
                        help="where the report pages are written (default: html/)")
    report.add_argument("--publish", action="store_true",
                        help="copy the report to report.publish.target afterwards")
    report.add_argument("--publish-target", metavar="DEST",
                        help="override the publication target "
                             "(e.g. user@host:/web/recovery/)")
    report.add_argument("--no-report", action="store_true",
                        help="do not write the HTML pages (console output only)")
    report.add_argument("--post-ecl", action="store_true",
                        help="post the phase-4 report to the electronic logbook")
    report.add_argument("--run-id", type=int, metavar="N",
                        help="regenerate the report of stored run N (default: "
                             "the latest). Only with --phase report; attaches "
                             "to run N, creates no run, needs no credentials "
                             "unless posting")

    general = parser.add_argument_group("general")
    general.add_argument("--config", metavar="FILE",
                         help="main configuration file "
                              "(default: config/power-recovery.yaml)")
    general.add_argument("--env-file", metavar="FILE",
                         help="dotenv file (default: config/.env)")
    general.add_argument("--database-url", metavar="URL",
                         help="SQLAlchemy URL for the run store "
                              "(default: sqlite in data/)")
    general.add_argument("--no-self-update", action="store_true",
                         help="skip the phase-0 check for a newer revision")
    general.add_argument("--list-checks", action="store_true",
                         help="print the registered checks and exit")
    general.add_argument("--list-nodes", action="store_true",
                         help="print the node inventory and exit")
    general.add_argument("--json", action="store_true",
                         help="stdout carries exactly one JSON document (the "
                              "run result, or the --list-* listing); every "
                              "human-readable line goes to stderr")
    general.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    general.add_argument("-q", "--quiet", action="store_true",
                         help="warnings and errors only")
    return parser


def cli_overrides(args: argparse.Namespace) -> Dict[str, Any]:
    """Map parsed flags onto dotted configuration paths.

    Only flags the operator actually gave appear here: a None value is dropped
    by :meth:`Settings.apply_cli`, so an absent flag never overrides a config
    file.  The two exceptions are the store-true flags, which are only sent
    when true for the same reason.

    ``--execute`` is deliberately absent: it is not a configuration value but
    this invocation's authorisation, decided by :func:`authorize_live`, which
    then sets ``run.dry_run`` itself.
    """
    overrides: Dict[str, Any] = {
        "run.label": args.label,
        "run.from_stage": args.from_stage,
        "run.until_stage": args.until_stage,
        "kerberos.principal": args.principal,
        "kerberos.root_principal": args.root_principal,
        "vault.addr": args.vault_addr,
        "report.output_dir": args.output_dir,
        "report.publish.target": args.publish_target,
        "database.url": args.database_url,
    }
    if args.simulate:
        # A simulated run must never be able to touch anything, whatever else
        # was asked for -- including --execute on the same command line.
        overrides["run.dry_run"] = True
    if args.continue_on_error:
        overrides["run.stop_on_stage_failure"] = False
    if args.no_prompt:
        overrides["kerberos.prompt"] = False
    if args.no_self_update:
        overrides["selfupdate.enabled"] = False
    if args.publish or args.publish_target:
        overrides["report.publish.enabled"] = True
    if args.post_ecl:
        overrides["ecl.enabled"] = True
    if args.location:
        overrides["topology.locations"] = list(args.location)
    return overrides


# ---------------------------------------------------------------------------
# Live-run authorisation
# ---------------------------------------------------------------------------


class LiveAuthorizationError(ValueError):
    """The configuration asks for live mode, or an ARM token was given, but
    this invocation's authorisation does not hold. Exit 2."""


@dataclass
class Authorization:
    """The decision :func:`authorize_live` made, and what it rests on."""

    live: bool
    source: str


def _dry_run_source(settings: Any) -> str:
    """Which layer last set run.dry_run (for the error message)."""
    for layer in reversed(getattr(settings, "overrides", [])):
        if layer.path == "run.dry_run":
            return layer.source
    return "built-in default"


def authorize_live(settings: Any, args: argparse.Namespace,
                   environ: Mapping[str, str]) -> Authorization:
    """Decide whether this invocation may issue state-changing IPMI commands.

    Two independent conditions, one of which must be given *per invocation*:

    * ``--simulate``: never live, whatever else is set.
    * ``--execute``: live. The shipped configuration is ``run.dry_run: true``
      and every documented example uses the bare flag, so the flag alone
      authorises; a stray ARM token beside it is ignored with a warning.
    * no flag, ``MU2E_POWER_RECOVERY_ARM`` in the process environment: live
      only if the configuration permits it (``run.dry_run: false``) *and* the
      token equals the configured, non-empty ``run.label``. Any other
      combination -- a null label, a mismatch, ``run.dry_run: true`` -- is an
      error, because a token that does not do what it says is a mistake.
    * no flag, no token, ``run.dry_run: false`` from YAML, ``config/.env`` or
      the environment: an error. That key alone only *permits* the token path;
      it never arms a run, so a persistent file cannot turn a bare invocation
      into a live one.
    * otherwise: dry run.

    On return ``run.dry_run`` has been set to ``not live`` (source recorded),
    so everything downstream -- the IPMI clients, the banner, the run store --
    reads one resolved value exactly as before. Raises
    :class:`LiveAuthorizationError`.
    """
    token = environ.get(ARM_ENV)
    label = settings.get("run.label")
    config_live = settings.get("run.dry_run", True) is False
    how = (f"pass --execute on the command line, or set {ARM_ENV}=<run.label> "
           f"in the environment of this invocation together with "
           f"run.dry_run: false and a run.label")

    if args.simulate:
        decision = Authorization(False, "--simulate")
    elif args.execute:
        if token is not None:
            log.warning("%s is set but ignored: --execute already authorises "
                        "this run", ARM_ENV)
        decision = Authorization(True, "--execute")
    elif token is not None:
        if not config_live:
            raise LiveAuthorizationError(
                f"{ARM_ENV} is set but run.dry_run is true "
                f"({_dry_run_source(settings)}); the token only arms a run whose "
                f"configuration sets run.dry_run: false. Unset it for a dry run, "
                f"or use --execute.")
        if not label:
            raise LiveAuthorizationError(
                f"{ARM_ENV} is set but run.label is not; the token must equal a "
                f"configured run.label, so with no label it can match nothing.")
        if token != str(label):
            raise LiveAuthorizationError(
                f"{ARM_ENV}={token!r} does not match run.label {label!r}; "
                f"refusing to arm the run.")
        decision = Authorization(True, f"{ARM_ENV} (run.label {label!r})")
    elif config_live:
        raise LiveAuthorizationError(
            f"run.dry_run is false ({_dry_run_source(settings)}) but this "
            f"invocation did not authorise live power commands. The "
            f"configuration only permits a live run; to authorise one, {how}. "
            f"For a dry run, set run.dry_run back to true.")
    else:
        decision = Authorization(False, "default (dry run)")

    settings.set("run.dry_run", not decision.live,
                 source=f"live-run authorisation: {decision.source}")
    if decision.live:
        log.warning("LIVE run authorised by %s: power commands will be issued",
                    decision.source)
    return decision


# ---------------------------------------------------------------------------
# Informational modes
# ---------------------------------------------------------------------------


def print_checks(as_json: bool = False, out: Any = None) -> int:
    out = out or sys.stdout
    if as_json:
        print(json.dumps(DESCRIPTIONS, indent=2), file=out)
        return 0
    print(console.heading("Registered checks", console.use_color(out)), file=out)
    rows = [[cid, DESCRIPTIONS[cid]] for cid in sorted(DESCRIPTIONS)]
    print(console.table(rows, ["CHECK ID", "VERIFIES"]), file=out)
    print(f"\n  {len(rows)} check(s). Profiles that use them are in "
          f"config/checks.yaml.", file=out)
    return 0


def print_nodes(orch: Orchestrator, names: Optional[Sequence[str]] = None,
                as_json: bool = False, out: Any = None) -> int:
    out = out or sys.stdout
    nodes = orch.nodes(names)
    if as_json:
        print(json.dumps([n.as_dict() for n in nodes], indent=2), file=out)
        return 0
    print(console.heading("Node inventory", console.use_color(out)), file=out)
    rows = [[n.short, n.node_class, n.location,
             ",".join(sorted(n.networks)), n.ipmi_host or "-",
             "protected" if n.protected else ""]
            for n in nodes]
    print(console.table(rows, ["NODE", "CLASS", "LOCATION", "NETWORKS", "BMC", ""]),
          file=out)
    print(f"\n  {len(nodes)} node(s) across "
          f"{', '.join(sorted({n.location for n in nodes}))}", file=out)
    empty = [loc for loc in orch.locations
             if not orch.topology.nodes(loc)]
    if empty:
        print(f"\n  note: no nodes are configured for {', '.join(empty)} "
              f"-- see config/topology.yaml", file=out)
    return 0


# ---------------------------------------------------------------------------
# Phase-0 self update
# ---------------------------------------------------------------------------


def _stderr_target() -> Any:
    """stderr as something subprocess can write to (a descriptor)."""
    try:
        return sys.stderr.fileno()
    except (AttributeError, OSError, ValueError):
        return subprocess.DEVNULL


def do_self_update(settings: Any, out: Any = None, as_json: bool = False,
                   lock: Optional[RunLock] = None) -> UpdateResult:
    """Run the update check and, if it changed anything, restart this process.

    Under --json the messages go to *out* (stderr) and so does the rebuild's
    own output, so stdout stays a single JSON document.

    The run lock is released immediately before the re-exec; the new process
    takes it again through the normal path (pid identity is preserved by
    exec, and the record is rewritten). Returns the result, which the run
    records in its provenance; a result with ``reset_failed`` set means the
    caller must stop (exit 2).
    """
    out = out or sys.stdout
    updater = SelfUpdater(settings, stdout=_stderr_target() if as_json else None)
    result = updater.run()
    for message in result.messages:
        print(f"  update: {message}", file=out)
    if result.needs_reexec:
        if lock is not None:
            lock.release()
        updater.reexec()   # never returns
    return result


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def write_report(orch: Orchestrator, run_id: int, settings: Any,
                 report_result: Optional[Any] = None,
                 post: bool = False) -> Dict[str, Any]:
    """Render run *run_id*'s bundle, post it if asked, then publish.

    Called only once the run is finished (or, for regeneration, on the stored
    run as it is), so nothing here can record a run as still in progress.
    The order is the one #18 needs: render the complete bundle, post it with
    that bundle's pages attached, write the logbook outcome into the bundle's
    report.json, and only then publish -- once, with the bundle final.
    """
    if report_result is not None and report_result.data.get("export"):
        export = report_result.data["export"]
        narrative = report_result.data["narrative"]
    else:
        export = orch.store.export_run(run_id)
        narrative = phase4_report.build_narrative(export)
    payload = None
    if report_result is not None:
        payload = report_result.as_dict()
        payload.pop("assessments", None)
        # The export has its own file, data/run-export.json.
        payload["data"] = {k: v for k, v in payload["data"].items()
                           if k != "export"}

    writer = ReportWriter(settings, orch.topology)
    run = export.get("run") or {}
    bundle = writer.render_run(
        export, narrative,
        version=run.get("version") or orch.version.as_dict(),
        latest=orch.store.latest_run_id() == run_id,
        checks=DESCRIPTIONS,
        runs=orch.store.list_runs(int(settings.get("report.keep_runs", 30))),
        report=payload)

    ecl: Optional[Dict[str, Any]] = None
    if report_result is not None:
        ecl = _post_or_skip(orch, run_id, narrative, bundle.paths, post)
        writer.record_ecl(bundle, ecl)
        _note_ecl(report_result, ecl)

    publication = Publisher(settings, orch.local, simulate=orch.simulate).publish()
    return {"output_dir": str(writer.output_dir),
            "run_dir": str(bundle.directory),
            "pages": bundle.paths, "data": bundle.data,
            "latest": bundle.latest,
            "publication": publication, "ecl": ecl}


def _post_or_skip(orch: Orchestrator, run_id: int, narrative: Dict[str, Any],
                  attachments: Sequence[str], post: bool) -> Dict[str, Any]:
    if not post:
        return {"posted": False,
                "reason": "logbook posting not enabled (ecl.enabled: false)"}
    return phase4_report.post(orch, run_id, narrative, attachments)


def _note_ecl(result: Any, ecl: Dict[str, Any]) -> None:
    result.data["ecl"] = ecl
    if ecl.get("posted"):
        result.notes.append(f"posted to the logbook: {ecl.get('url') or 'ok'}")
    elif ecl.get("error"):
        result.notes.append(f"logbook posting failed: {ecl['error']} (the local "
                            f"report is complete)")
    else:
        result.notes.append(ecl.get("reason") or "not posted to the logbook")


def _render_after_failure(orch: Orchestrator, run_id: Optional[int],
                          settings: Any, args: argparse.Namespace) -> None:
    """Best effort: render the (already finished) run after an error.

    The run's terminal status is in the store by now, so the pages say
    'error' or 'interrupted', not 'in progress'. Never publishes or posts.
    """
    if run_id is None or args.no_report:
        return
    try:
        export = orch.store.export_run(run_id)
        ReportWriter(settings, orch.topology).render_run(
            export, phase4_report.build_narrative(export),
            version=(export.get("run") or {}).get("version"),
            latest=orch.store.latest_run_id() == run_id,
            checks=DESCRIPTIONS,
            runs=orch.store.list_runs(int(settings.get("report.keep_runs", 30))))
    except Exception:  # noqa: BLE001 - the failure being reported matters more
        log.exception("could not render the report after the failure")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def print_phase(result: Any, out: Any, name: Optional[str] = None) -> None:
    """The console view of one phase result, on *out*."""
    color = console.use_color(out)
    name = name or result.name
    print(console.phase_banner(result, color), file=out)
    if result.assessments:
        print(console.counts_line(result.counts, color), file=out)
        print(file=out)
        print(console.node_table(result.assessments,
                                 show_power=(name in ("assess", "poweron")),
                                 enabled=color), file=out)
        failures = [a for a in result.assessments if a.status.is_bad]
        if failures:
            print(console.rule("-", "failures"), file=out)
            print(console.failure_detail(failures, enabled=color), file=out)
    if result.notes:
        print(console.rule("-", "notes"), file=out)
        # Capped: the full set is on the phase's report page, and a console
        # that scrolls a hundred notes past the operator has told them
        # nothing.
        for note in result.notes[:20]:
            print(f"  * {note}", file=out)
        if len(result.notes) > 20:
            print(f"  ... and {len(result.notes) - 20} more; see the "
                  f"report page", file=out)


def _persist_result(orch: Orchestrator, result: Any,
                    phase_id_before: Optional[int]) -> None:
    """Store what a phase returned beside what it stored itself.

    The verdict, title, notes and duration go into the phase row's data as
    ``_result``, so every report page can be rebuilt from the store alone.
    A phase that returned before opening a row (nothing to do) has none,
    and is absent from the run -- and from its report.
    """
    phase_id = getattr(orch.store, "phase_id", None)
    if phase_id is None or phase_id == phase_id_before:
        return
    orch.store.annotate_phase(phase_id, {"_result": {
        "status": result.status.value, "title": result.title,
        "notes": list(result.notes), "counts": result.counts,
        "duration": round(result.duration, 2),
        "started_at": result.started_at, "finished_at": result.finished_at}})


def _record_notes(orch: Orchestrator, seen: set) -> None:
    """Run-level notes (credentials, gateways) into the run as 'note' events."""
    for note in orch.notes:
        if note not in seen:
            seen.add(note)
            orch.store.record_event(note, level="note")


def run_phases(orch: Orchestrator, args: argparse.Namespace,
               phase_names: Sequence[str],
               nodes: Optional[Sequence[Any]] = None,
               plan: Optional[Any] = None,
               out: Any = None) -> List[Any]:
    """Execute phases 1-3 in order, returning their results.

    Phase 4 is not run here: it needs the run finished first, which is
    :func:`main`'s job once these return. *nodes* and *plan* are resolved by
    :func:`main` before credentials are acquired; given neither, they are
    resolved here (--node for phases 1 and 3; phase 2 plans the sequence
    itself).
    """
    out = out or sys.stdout
    results: List[Any] = []
    if nodes is None and args.node:
        nodes = orch.nodes(args.node)
    runnable = [n for n in phase_names if n != "report"]

    for name in runnable:
        before = getattr(orch.store, "phase_id", None)
        if name == "assess":
            result = phase1_assess.run(orch, nodes)
        elif name == "poweron":
            result = phase2_poweron.run(orch, from_stage=args.from_stage,
                                        until_stage=args.until_stage, plan=plan)
        elif name == "network":
            result = phase3_network.run(orch, nodes,
                                        include_failed=args.include_failed)
        else:  # pragma: no cover - argparse restricts this
            continue

        # Service identities abandoned mid-phase (a default-cache guard
        # failure) belong in this phase's notes and the run's events, whichever
        # phase it happened in -- phase 3 does not copy orch.notes itself.
        orch.surface_credential_failure(result.notes)
        _persist_result(orch, result, before)
        results.append(result)
        print_phase(result, out, name)

        # A phase that cannot proceed makes the following phases meaningless:
        # powering on through a gateway that never answered, or probing a mesh
        # of nodes that were never brought up, produces noise, not information.
        if result.status is Status.FAIL and name in ("assess", "poweron") \
                and not args.continue_on_error and len(phase_names) > 1:
            if name == "assess" and result.data.get("ready_for_phase2", {}).get("ready"):
                continue   # failures exist, but phase 2 still has what it needs
            print(f"\n  Stopping after phase '{name}': later phases depend on it. "
                  f"Use --continue-on-error to override.", file=out)
            break
    return results


def _json_phase(result: Any) -> Dict[str, Any]:
    payload = result.as_dict()
    if "export" in payload.get("data", {}):
        # Phase 4 carries the whole run export; it is in data/run-export.json.
        payload["data"] = {k: v for k, v in payload["data"].items()
                           if k != "export"}
    return payload


def main(argv: Optional[Sequence[str]] = None,
         fixed_phase: Optional[str] = None,
         prog: Optional[str] = None,
         description: Optional[str] = None) -> int:
    """The driver.

    With ``--json`` stdout carries exactly one JSON document and nothing
    else: every human-readable line goes to stderr (and anything else that
    prints is redirected there), and the document is written last. For
    --list-checks / --list-nodes it is that listing; otherwise the object
    ``{version, run_id, status, exit_code, phases, report, [error]}``,
    written on the error and interrupt paths too.
    """
    parser = build_parser(prog=prog, description=description, fixed_phase=fixed_phase)
    args = parser.parse_args(argv)
    if fixed_phase:
        args.phase = fixed_phase
    if not args.json:
        return _main(args, sys.stdout, sys.stdout, {})

    data_out = sys.stdout
    doc: Dict[str, Any] = {"version": {"package_version": __version__},
                           "run_id": None, "status": None, "exit_code": None,
                           "phases": [], "report": None}
    with contextlib.redirect_stdout(sys.stderr):
        code = _main(args, sys.stderr, data_out, doc)
    if doc.pop("_emit", True):
        doc["exit_code"] = code
        print(json.dumps(doc, indent=2, default=str), file=data_out)
    return code


def _main(args: argparse.Namespace, out: Any, data_out: Any,
          doc: Dict[str, Any]) -> int:
    phase_names = PHASE_ORDER if args.phase == "all" else [args.phase]
    report_only = phase_names == ["report"]

    def fail(message: str, code: int = 2) -> int:
        print(f"error: {message}", file=sys.stderr)
        doc["error"] = message
        return code

    # --- configuration ----------------------------------------------------
    try:
        settings = load_settings(
            config_file=Path(args.config) if args.config else None,
            env_file=Path(args.env_file) if args.env_file else None,
            cli=cli_overrides(args),
        )
    except ConfigError as exc:
        return fail(str(exc))

    configure_logging(settings, verbose=args.verbose, quiet=args.quiet)

    if args.list_checks:
        doc["_emit"] = False
        return print_checks(args.json, data_out if args.json else out)

    # --run-id names a stored run to report on; with any other phase it would
    # mean "report on N but run phases 1-3 as a new run", which is two runs.
    if args.run_id is not None and not report_only and not args.list_nodes:
        return fail("--run-id selects an existing run to regenerate the report "
                    "for; it is only valid with --phase report "
                    "(mu2e-power-report)")

    # --- live-run authorisation -------------------------------------------
    # Before the self-update and before anything is contacted: a run whose
    # configuration asks for live mode without this invocation's say-so
    # stops here.
    authorization: Optional[Authorization] = None
    if not args.list_nodes:
        try:
            authorization = authorize_live(settings, args, os.environ)
        except LiveAuthorizationError as exc:
            return fail(str(exc))

    # --- the run lock -----------------------------------------------------
    # Only an invocation that can act on hardware takes it: phases 1-3 for
    # real. A rehearsal, a listing or a report regeneration contacts nothing
    # that another run could be driving, and must stay usable while a real
    # run is in progress. Taken before phase 0, so two starts cannot both
    # update the checkout either.
    lock: Optional[RunLock] = None
    if needs_run_lock(args, phase_names):
        lock = RunLock(settings.resolve_path(
            settings.get("run.lock_file") or DEFAULT_LOCK_FILE))
        try:
            lock.acquire()
        except LockError as exc:
            return fail(str(exc))
    try:
        return _run(args, settings, authorization, lock, phase_names,
                    report_only, out, data_out, doc, fail)
    finally:
        if lock is not None:
            lock.release()


def needs_run_lock(args: argparse.Namespace, phase_names: Sequence[str]) -> bool:
    """True for the invocations that can act on hardware (#12)."""
    if args.simulate or args.list_nodes or args.list_checks:
        return False
    return any(name in ("assess", "poweron", "network") for name in phase_names)


def _run(args: argparse.Namespace, settings: Any,
         authorization: Optional[Authorization], lock: Optional[RunLock],
         phase_names: Sequence[str], report_only: bool, out: Any,
         data_out: Any, doc: Dict[str, Any], fail: Any) -> int:
    """Everything after the lock: phase 0, then the run itself."""
    # --- phase 0 ----------------------------------------------------------
    update: Optional[UpdateResult] = None
    # Only under the run lock: a report regeneration or listing that updated
    # the checkout -- fast-forward, venv rebuild, possibly a reset -- would do
    # it under a live --execute run still loading modules and templates from
    # that tree (PR #32 review). Those invocations run the code they started.
    if not args.simulate and lock is not None:
        update = do_self_update(settings, out, args.json, lock=lock)
        if update.reset_failed:
            return fail(update.messages[-1])

    # --- banner -----------------------------------------------------------
    try:
        orch = Orchestrator(settings, simulate=args.simulate)
    except TopologyError as exc:
        return fail(str(exc))
    doc["version"] = orch.version.as_dict()

    if not args.quiet:
        print(orch.version.banner(), file=out)
    if args.list_nodes:
        doc["_emit"] = False
        try:
            return print_nodes(orch, args.node, args.json,
                               data_out if args.json else out)
        except TopologyError as exc:
            return fail(str(exc))
        finally:
            orch.close()

    # --- the run to report on (report-only) -------------------------------
    # Checked before anything touches credentials: a missing run is a usage
    # error, and no row is created for it.
    target_run: Optional[int] = None
    if report_only:
        target_run = (args.run_id if args.run_id is not None
                      else orch.store.latest_run_id())
        if target_run is None or orch.store.get_run(target_run) is None:
            orch.close()
            if args.run_id is not None:
                return fail(f"run {args.run_id} is not in the run store "
                            f"({orch.store.url}); see the run history page or "
                            f"'data/summary.json' for the run ids it holds")
            return fail(f"the run store ({orch.store.url}) holds no run to "
                        f"report on; run a phase first")

    if report_only:
        print(f"  REPORT ONLY -- regenerating the report for run {target_run} "
              f"from the run store; no host is contacted.\n", file=out)
    elif args.simulate:
        print("  SIMULATED RUN -- no host will be contacted; command output is "
              "answered from a built-in script.\n", file=out)
    elif settings.get("run.dry_run", True):
        print("  DRY RUN -- power states will be read but nothing will be "
              "switched on. Pass --execute to act (see 'live power commands' "
              "in --help).\n", file=out)
    else:
        print(f"  LIVE RUN -- power commands WILL be issued "
              f"(authorised by {authorization.source if authorization else '?'}).\n",
              file=out)

    # --- scope ------------------------------------------------------------
    # Resolved before credentials and before the run row exists, so a bad
    # --node, --from/--until or --location costs the operator no password
    # prompt and leaves no half-started run in the store.
    nodes = None
    plan = None
    if not report_only:
        try:
            nodes = orch.nodes(args.node) if args.node else None
            if "poweron" in phase_names:
                plan = plan_sequence(orch.sequence_config, orch.topology,
                                     orch.locations, args.node,
                                     settings.get("run.from_stage"),
                                     settings.get("run.until_stage"))
        except (TopologyError, SequenceSelectionError) as exc:
            orch.close()
            return fail(str(exc))
    if plan is not None:
        for notice in plan.notices:
            print(f"  scope: {notice}", file=out)
        if plan.notices:
            print(file=out)

    # --- run --------------------------------------------------------------
    exit_code = 0
    results: List[Any] = []
    rid: Optional[int] = None
    #: Once True the run's terminal status is in the store, and nothing on
    #: an error path may overwrite it. A regenerated run starts True: its
    #: status is never changed by regenerating it.
    finished = report_only
    post = bool(settings.get("ecl.enabled", False))
    install_sigterm_handler()
    try:
        if report_only:
            orch.store.attach(target_run)
            rid = target_run
        else:
            orch.prepare_credentials()
            rid = orch.store.start_run(
                label=settings.get("run.label") or default_label(),
                dry_run=bool(settings.get("run.dry_run", True)),
                version=dict(orch.version.as_dict(),
                             selfupdate=update.as_dict() if update else None),
                settings=settings.redacted(),
            )
            if authorization is not None and authorization.live:
                orch.store.record_event(
                    f"LIVE run: power commands authorised by {authorization.source}",
                    level="warning")
            seen: set = set()
            _record_notes(orch, seen)
            results = run_phases(orch, args, phase_names, nodes=nodes,
                                 plan=plan, out=out)
            _record_notes(orch, seen)

            # Finish the run before anything is assembled, rendered or
            # posted: the report is the record, and it must not say
            # "in progress" about a run that has ended.
            verdict = phase4_report.overall_status(orch.store.export_run(rid))
            orch.store.finish_run("complete" if verdict is not Status.FAIL
                                  else "complete_with_failures")
            finished = True
        doc["run_id"] = rid

        report_result = None
        if "report" in phase_names:
            report_result = phase4_report.assemble(orch.store, rid)
            results.append(report_result)
            verdict = report_result.status
        else:
            verdict = phase4_report.overall_status(orch.store.export_run(rid))
        exit_code = 1 if verdict.is_bad else 0

        report_info: Optional[Dict[str, Any]] = None
        if not args.no_report:
            report_info = write_report(orch, rid, settings, report_result, post)
        elif report_result is not None:
            ecl = _post_or_skip(orch, rid, report_result.data["narrative"], [], post)
            _note_ecl(report_result, ecl)
            report_info = {"output_dir": None, "pages": [], "data": [],
                           "publication": None, "ecl": ecl}
        if report_result is not None:
            print_phase(report_result, out)
        if report_info is not None and report_info.get("output_dir"):
            print(console.rule("-", "report"), file=out)
            print(f"  run {rid}: pages written to {report_info['run_dir']}"
                  + ("; the top-level view shows this run"
                     if report_info.get("latest") else
                     "; the top-level view still shows the newest run"),
                  file=out)
            publication = report_info["publication"] or {}
            if publication.get("published"):
                print(f"  published to {publication.get('target')}", file=out)
            elif publication.get("reason") and \
                    settings.get("report.publish.enabled"):
                print(f"  publication skipped: {publication['reason']}", file=out)
        doc["report"] = report_info

    except KeyboardInterrupt:
        # Ctrl-C, or SIGTERM routed here by install_sigterm_handler().
        print("\n  interrupted; the run store keeps everything done so far, and "
              "the run's private Kerberos caches have been destroyed.",
              file=sys.stderr)
        doc["error"] = "interrupted"
        if rid is not None:
            orch.store.record_event("run interrupted by the operator"
                                    if not finished else
                                    "report generation interrupted by the operator",
                                    level="error", run_id=rid)
            if not finished:
                orch.store.finish_run("interrupted", run_id=rid)
                finished = True
                _render_after_failure(orch, rid, settings, args)
        exit_code = 3
    except SystemExit as exc:
        # prepare_credentials() and the config loaders stop with
        # SystemExit("error: ..."): a message, not a status.
        if isinstance(exc.code, int):
            exit_code = exc.code
        else:
            exit_code = 2
            if exc.code:
                print(str(exc.code), file=sys.stderr)
        doc["error"] = str(exc.code) if exc.code else f"exit {exit_code}"
        if rid is not None and not finished:
            orch.store.finish_run("error", run_id=rid)
            finished = True
    except Exception as exc:  # noqa: BLE001 - report, do not traceback at the operator
        log.exception("run failed")
        print(f"\nerror: {exc}", file=sys.stderr)
        print("  see the log file for the full traceback.", file=sys.stderr)
        doc["error"] = str(exc)
        if rid is not None and not finished:
            orch.store.finish_run("error", run_id=rid)
            finished = True
            _render_after_failure(orch, rid, settings, args)
        exit_code = 2
    finally:
        try:
            doc["run_id"] = rid
            doc["phases"] = [_json_phase(r) for r in results]
            stored = orch.store.get_run(rid) if rid is not None else None
            doc["status"] = stored.get("status") if stored else None
        except Exception:  # noqa: BLE001 - the JSON document is best effort here
            log.exception("could not read the run back for --json")
        orch.close()

    return exit_code


def install_sigterm_handler() -> None:
    """Route SIGTERM into the same clean path as Ctrl-C.

    Without this the default disposition applies and the process is terminated
    outright: the run store is left saying 'running', the interruption is never
    recorded, and -- worst of the three -- the ``finally`` that calls
    Orchestrator.close() never runs, so the run's private Kerberos caches
    survive it. Those can include root-capable service tickets.

    stop-mu2edaq-power-recovery.sh sends SIGTERM and has always described that
    as the clean stop, so it was the documented path that did not do what it
    said. Raising KeyboardInterrupt reuses the handler that already records the
    interruption; the signal arrives in the main thread, which is where the
    phase runner waits on its workers.
    """
    def terminate(signum, frame):  # noqa: ARG001 - signal handler signature
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, terminate)
    except (ValueError, OSError, AttributeError):
        # Not the main thread, or a platform without SIGTERM. The run still
        # works; only the clean-stop path is unavailable.
        log.debug("could not install a SIGTERM handler")


def default_label() -> str:
    import getpass
    import socket
    from datetime import datetime
    return (f"{getpass.getuser()}@{socket.gethostname().split('.')[0]} "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M')}")


# ---------------------------------------------------------------------------
# Single-phase entry points
# ---------------------------------------------------------------------------


def main_state(argv: Optional[Sequence[str]] = None) -> int:
    return main(argv, fixed_phase="assess", prog="mu2e-power-state",
                description="Phase 1: read-only survey of the DAQ clusters.")


def main_poweron(argv: Optional[Sequence[str]] = None) -> int:
    return main(argv, fixed_phase="poweron", prog="mu2e-power-on",
                description="Phase 2: power the cluster on in dependency order. "
                            "Issues no power command unless this invocation "
                            "authorises it (--execute, or the "
                            "MU2E_POWER_RECOVERY_ARM token); --node powers only "
                            "the named nodes and verifies the stages before "
                            "them.")


def main_netcheck(argv: Optional[Sequence[str]] = None) -> int:
    return main(argv, fixed_phase="network", prog="mu2e-power-netcheck",
                description="Phase 3: inter-node connectivity across the DAQ "
                            "network segments.")


def main_report(argv: Optional[Sequence[str]] = None) -> int:
    return main(argv, fixed_phase="report", prog="mu2e-power-report",
                description="Phase 4: regenerate the consolidated report and "
                            "optionally post it to the electronic logbook.")


if __name__ == "__main__":
    sys.exit(main())
