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
import json
import logging
import signal
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import __version__, console
from .checks import DESCRIPTIONS, Status
from .logsetup import configure as configure_logging
from .orchestrator import Orchestrator
from .phases import PHASES, phase1_assess, phase2_poweron, phase3_network, phase4_report
from .report import Publisher, ReportWriter
from .selfupdate import SelfUpdater
from .settings import ConfigError, load as load_settings
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

  # regenerate and post the report for an earlier run
  mu2e-power-report --run-id 17 --post-ecl

exit status
  0  every phase completed and nothing failed
  1  a phase completed but one or more nodes failed their checks
  2  the run could not start (configuration, credentials, no gateway)
  3  interrupted by the operator
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
                     help="actually issue power commands. Without it the run is "
                          "a dry run: states are read, nothing is switched on.")
    run.add_argument("--simulate", action="store_true",
                     help="contact nothing at all; answer every command from a "
                          "built-in script. For rehearsal and for testing the "
                          "report off site.")
    run.add_argument("--location", action="append", metavar="NAME",
                     help="limit the run to this location (mc2, mc1, teststand). "
                          "Repeatable; defaults to topology.locations.")
    run.add_argument("--node", action="append", metavar="HOST",
                     help="limit the run to these nodes. Repeatable. Accepts "
                          "short or fully-qualified names.")
    run.add_argument("--continue-on-error", action="store_true",
                     help="in phase 2, carry on to the next stage even when a "
                          "stage does not meet its requirement")
    run.add_argument("--from", dest="from_stage", metavar="STAGE",
                     help="start the power-on sequence at this stage")
    run.add_argument("--until", dest="until_stage", metavar="STAGE",
                     help="stop the power-on sequence after this stage")
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
                        help="operate on an existing run (phase 4 / report "
                             "regeneration)")

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
                         help="print the result as JSON on stdout instead of tables")
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
    if args.execute:
        overrides["run.dry_run"] = False
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
# Informational modes
# ---------------------------------------------------------------------------


def print_checks(as_json: bool = False) -> int:
    if as_json:
        print(json.dumps(DESCRIPTIONS, indent=2))
        return 0
    print(console.heading("Registered checks"))
    rows = [[cid, DESCRIPTIONS[cid]] for cid in sorted(DESCRIPTIONS)]
    print(console.table(rows, ["CHECK ID", "VERIFIES"]))
    print(f"\n  {len(rows)} check(s). Profiles that use them are in "
          f"config/checks.yaml.")
    return 0


def print_nodes(orch: Orchestrator, names: Optional[Sequence[str]] = None,
                as_json: bool = False) -> int:
    nodes = orch.nodes(names)
    if as_json:
        print(json.dumps([n.as_dict() for n in nodes], indent=2))
        return 0
    print(console.heading("Node inventory"))
    rows = [[n.short, n.node_class, n.location,
             ",".join(sorted(n.networks)), n.ipmi_host or "-",
             "protected" if n.protected else ""]
            for n in nodes]
    print(console.table(rows, ["NODE", "CLASS", "LOCATION", "NETWORKS", "BMC", ""]))
    print(f"\n  {len(nodes)} node(s) across "
          f"{', '.join(sorted({n.location for n in nodes}))}")
    empty = [loc for loc in orch.locations
             if not orch.topology.nodes(loc)]
    if empty:
        print(f"\n  note: no nodes are configured for {', '.join(empty)} "
              f"-- see config/topology.yaml")
    return 0


# ---------------------------------------------------------------------------
# Phase-0 self update
# ---------------------------------------------------------------------------


def do_self_update(settings: Any) -> None:
    """Run the update check and, if it changed anything, restart this process."""
    updater = SelfUpdater(settings)
    result = updater.run()
    for message in result.messages:
        print(f"  update: {message}")
    if result.needs_reexec:
        updater.reexec()   # never returns


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def write_report(orch: Orchestrator, results: Sequence[Any],
                 settings: Any) -> Dict[str, Any]:
    """Render every page the run touched, plus the static ones.

    Only the phases that actually ran are re-rendered; the others keep whatever
    a previous invocation wrote, which is what makes "re-run phase 1 and refresh
    its page" work without erasing phase 2's page.
    """
    writer = ReportWriter(settings, orch.topology)
    run = orch.store.get_run() or {}
    version = orch.version.as_dict()
    written: List[str] = []

    for result in results:
        if result.name in ("assess", "poweron", "network", "report"):
            written.append(str(writer.write_phase(result, run, version)))
            payload = result.as_dict()
            if result.name == "report":
                # The phase-4 result carries the entire run export, which the
                # per-phase files already hold check by check.  Split it out so
                # data/report.json stays the readable narrative and the bulk
                # evidence has a file of its own.
                export = payload.get("data", {}).pop("export", None)
                if export is not None:
                    writer.write_data("run-export", export)
            writer.write_data(result.name, payload)

    stored_phases = orch.store.get_phases()
    phase_rows = []
    for stored in stored_phases:
        live = next((r for r in results if r.name == stored["name"]), None)
        phase_rows.append({
            "name": stored["name"],
            "number": stored["number"],
            "title": PHASE_TITLES.get(stored["name"], stored["name"]),
            "status": (live.status.value if live else stored["status"]),
            "summary": stored.get("summary") or (live.summary if live else ""),
            "duration": (live.duration if live else 0.0),
            "counts": (live.counts if live else {}),
        })

    written.append(str(writer.write_index(run, phase_rows, version, orch.notes)))
    written.append(str(writer.write_runs(orch.store.list_runs(
        settings.get("report.keep_runs", 30)))))
    written.extend(str(p) for p in writer.write_static_pages(
        version, DESCRIPTIONS, settings.redacted()))
    writer.write_data("summary", {"run": run, "phases": phase_rows,
                                  "version": version, "notes": orch.notes})
    writer.write_data("inventory", [n.as_dict() for n in orch.nodes()])

    if run.get("id"):
        writer.archive_run(int(run["id"]))

    publication = Publisher(settings, orch.local,
                            simulate=orch.simulate).publish()
    return {"pages": written, "output_dir": str(writer.output_dir),
            "publication": publication, "page_paths": writer.page_paths()}


PHASE_TITLES = {
    "assess": "Initial state",
    "poweron": "Power on",
    "network": "Network connectivity",
    "report": "Recovery report",
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run_phases(orch: Orchestrator, args: argparse.Namespace,
               phase_names: Sequence[str]) -> List[Any]:
    """Execute the requested phases in order, returning their results."""
    results: List[Any] = []
    nodes = orch.nodes(args.node) if args.node else None

    for name in phase_names:
        if name == "assess":
            result = phase1_assess.run(orch, nodes)
        elif name == "poweron":
            result = phase2_poweron.run(orch, from_stage=args.from_stage,
                                        until_stage=args.until_stage)
        elif name == "network":
            result = phase3_network.run(orch, nodes,
                                        include_failed=args.include_failed)
        elif name == "report":
            # Phase 4 needs the pages on disk to attach them, so the report is
            # written for the earlier phases first and the paths handed over.
            html_paths: List[str] = []
            if not args.no_report and results:
                html_paths = write_report(orch, results, orch.settings)["page_paths"]
            result = phase4_report.run(orch, run_id=args.run_id,
                                       post=args.post_ecl or None,
                                       html_paths=html_paths)
        else:  # pragma: no cover - argparse restricts this
            continue

        results.append(result)
        print(console.phase_banner(result))
        if result.assessments:
            print(console.counts_line(result.counts))
            print()
            print(console.node_table(result.assessments,
                                     show_power=(name in ("assess", "poweron"))))
            failures = [a for a in result.assessments if a.status.is_bad]
            if failures:
                print(console.rule("-", "failures"))
                print(console.failure_detail(failures))
        if result.notes:
            print(console.rule("-", "notes"))
            # Capped: the full set is on the phase's report page, and a console
            # that scrolls a hundred notes past the operator has told them
            # nothing.
            for note in result.notes[:20]:
                print(f"  * {note}")
            if len(result.notes) > 20:
                print(f"  ... and {len(result.notes) - 20} more; see the "
                      f"report page")

        # A phase that cannot proceed makes the following phases meaningless:
        # powering on through a gateway that never answered, or probing a mesh
        # of nodes that were never brought up, produces noise, not information.
        if result.status is Status.FAIL and name in ("assess", "poweron") \
                and not args.continue_on_error and len(phase_names) > 1:
            if name == "assess" and result.data.get("ready_for_phase2", {}).get("ready"):
                continue   # failures exist, but phase 2 still has what it needs
            print(f"\n  Stopping after phase '{name}': later phases depend on it. "
                  f"Use --continue-on-error to override.")
            break
    return results


def main(argv: Optional[Sequence[str]] = None,
         fixed_phase: Optional[str] = None,
         prog: Optional[str] = None,
         description: Optional[str] = None) -> int:
    parser = build_parser(prog=prog, description=description, fixed_phase=fixed_phase)
    args = parser.parse_args(argv)
    if fixed_phase:
        args.phase = fixed_phase

    # --- configuration ----------------------------------------------------
    try:
        settings = load_settings(
            config_file=Path(args.config) if args.config else None,
            env_file=Path(args.env_file) if args.env_file else None,
            cli=cli_overrides(args),
        )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    configure_logging(settings, verbose=args.verbose, quiet=args.quiet)

    if args.list_checks:
        return print_checks(args.json)

    # --- phase 0 ----------------------------------------------------------
    if not args.simulate:
        do_self_update(settings)

    # --- banner -----------------------------------------------------------
    try:
        orch = Orchestrator(settings, simulate=args.simulate)
    except TopologyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not args.quiet:
        print(orch.version.banner())
    if args.list_nodes:
        return print_nodes(orch, args.node, args.json)

    if args.simulate:
        print("  SIMULATED RUN -- no host will be contacted; command output is "
              "answered from a built-in script.\n")
    elif settings.get("run.dry_run", True):
        print("  DRY RUN -- power states will be read but nothing will be "
              "switched on. Pass --execute to act.\n")
    else:
        print("  LIVE RUN -- power commands WILL be issued.\n")

    phase_names = PHASE_ORDER if args.phase == "all" else [args.phase]

    # --- run --------------------------------------------------------------
    exit_code = 0
    results: List[Any] = []
    install_sigterm_handler()
    try:
        orch.prepare_credentials()
        orch.store.start_run(
            label=settings.get("run.label") or default_label(),
            dry_run=bool(settings.get("run.dry_run", True)),
            version=orch.version.as_dict(),
            settings=settings.redacted(),
        )
        results = run_phases(orch, args, phase_names)

        if not args.no_report:
            report_info = write_report(orch, results, settings)
            print(console.rule("-", "report"))
            print(f"  pages written to {report_info['output_dir']}")
            publication = report_info["publication"]
            if publication.get("published"):
                print(f"  published to {publication.get('target')}")
            elif publication.get("reason") and \
                    settings.get("report.publish.enabled"):
                print(f"  publication skipped: {publication['reason']}")

        worst = max((r.status for r in results), key=lambda s: s.rank) \
            if results else Status.UNKNOWN
        orch.store.finish_run("complete" if worst is not Status.FAIL
                              else "complete_with_failures")
        exit_code = 1 if worst.is_bad else 0

        if args.json:
            print(json.dumps([r.as_dict() for r in results], indent=2, default=str))

    except KeyboardInterrupt:
        # Ctrl-C, or SIGTERM routed here by install_sigterm_handler().
        print("\n  interrupted; the run store keeps everything done so far, and "
              "the run's private Kerberos caches have been destroyed.",
              file=sys.stderr)
        orch.store.record_event("run interrupted by the operator", level="error")
        orch.store.finish_run("interrupted")
        exit_code = 3
    except SystemExit as exc:
        return int(exc.code or 2)
    except Exception as exc:  # noqa: BLE001 - report, do not traceback at the operator
        log.exception("run failed")
        print(f"\nerror: {exc}", file=sys.stderr)
        print("  see the log file for the full traceback.", file=sys.stderr)
        orch.store.finish_run("error")
        exit_code = 2
    finally:
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
                            "Requires --execute to issue any power command.")


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
