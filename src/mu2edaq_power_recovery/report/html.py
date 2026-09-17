"""Static report site.

Plain files, written with Jinja2 and styled with Tailwind from a CDN, with a
little vanilla JavaScript for filtering and sorting.  There is deliberately no
server and no build step: the report has to be readable from a laptop during an
outage, copied to a web area, or attached to a logbook entry, and any of those
rules out a running application.

Re-running a phase rewrites that phase's page and refreshes the index; the
per-run archive under ``runs/<id>/`` keeps the history, so a second assessment
after a repair does not erase the evidence that the repair was needed.
"""
from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .. import __version__

log = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"

#: Page id -> (file name, nav label).  The sitemap page is generated from this,
#: so adding a page here is all it takes for it to appear in the navigation and
#: in the site map.
PAGES = {
    "index": ("index.html", "Overview"),
    "assess": ("initial-state.html", "Initial state"),
    "poweron": ("power-on.html", "Power on"),
    "network": ("network.html", "Network"),
    "report": ("detail.html", "Detailed report"),
    "runs": ("runs.html", "Run history"),
    "about": ("about.html", "About"),
    "api": ("api.html", "API"),
    "sitemap": ("sitemap.html", "Site map"),
}

STATUS_ORDER = ["fail", "unknown", "warn", "ok", "skip"]


class ReportWriter:
    """Renders the site into ``report.output_dir``."""

    def __init__(self, settings: Any, topology: Any = None):
        self.settings = settings
        self.topology = topology
        self.output_dir: Path = settings.resolve_path(
            settings.get("report.output_dir", "html"))
        self.title: str = settings.get("report.title", "Mu2e DAQ Power Outage Recovery")
        self.env = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            autoescape=select_autoescape(["html"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self.env.filters["status_class"] = status_class
        self.env.filters["status_badge"] = status_badge
        self.env.filters["duration"] = format_duration
        self.env.filters["shorthost"] = lambda h: str(h).split(".")[0]
        self.env.globals.update({
            "pages": PAGES,
            "site_title": self.title,
            "tool_version": __version__,
        })

    # -- helpers -----------------------------------------------------------

    def _write(self, page_id: str, context: Dict[str, Any],
               subdir: Optional[Path] = None) -> Path:
        filename, _label = PAGES[page_id]
        template = self.env.get_template(f"{page_id}.html")
        target_dir = subdir or self.output_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / filename
        context = {
            "page_id": page_id,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "prefix": "../../" if subdir else "",
            **context,
        }
        path.write_text(template.render(**context), encoding="utf-8")
        log.info("wrote %s", path)
        return path

    # -- pages -------------------------------------------------------------

    def write_phase(self, phase_result: Any, run: Dict[str, Any],
                    version: Dict[str, Any]) -> Path:
        """Render the page belonging to one phase."""
        page_id = phase_result.name
        if page_id not in PAGES:
            raise KeyError(f"no page defined for phase {page_id!r}")
        context = {
            "phase": phase_result.as_dict(),
            "run": run,
            "version": version,
            "grouped": _group_assessments(phase_result.assessments),
            "locations": _group_locations(phase_result.assessments),
        }
        if page_id == "network":
            context["networks"] = phase_result.data.get("networks", [])
        if page_id == "poweron":
            context["stages"] = phase_result.data.get("stages", [])
        if page_id == "report":
            context["narrative"] = phase_result.data.get("narrative", {})
        return self._write(page_id, context)

    def write_index(self, run: Dict[str, Any], phases: Sequence[Dict[str, Any]],
                    version: Dict[str, Any],
                    notes: Optional[List[str]] = None) -> Path:
        """The landing page: current state, phase status, quick links."""
        return self._write("index", {
            "run": run,
            "phases": list(phases),
            "version": version,
            "notes": notes or [],
            "counts": _combined_counts(phases),
        })

    def write_runs(self, runs: Sequence[Dict[str, Any]]) -> Path:
        return self._write("runs", {"runs": list(runs)})

    def write_static_pages(self, version: Dict[str, Any],
                           checks: Optional[Dict[str, str]] = None,
                           settings_dump: Optional[Dict[str, Any]] = None) -> List[Path]:
        """About, API and sitemap -- the pages that document the tool itself."""
        written = [
            self._write("about", {"version": version,
                                  "settings": settings_dump or {},
                                  "dependencies": DEPENDENCIES}),
            self._write("api", {"checks": checks or {},
                                "endpoints": DATA_FILES,
                                "version": version}),
            self._write("sitemap", {"version": version}),
        ]
        return written

    def write_data(self, name: str, payload: Any) -> Path:
        """Write a machine-readable companion file next to the pages.

        Every page's underlying data is also published as JSON, so the report
        is consumable by something other than a browser -- which is what makes
        it usable as an input to the next tool rather than only as a document.
        """
        data_dir = self.output_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        path = data_dir / f"{name}.json"
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return path

    def archive_run(self, run_id: int) -> Optional[Path]:
        """Copy the current pages into ``runs/<id>/`` for the history.

        Done by copying rather than by rendering twice: the archived copy is
        then byte-identical to what the operator looked at during the recovery,
        which matters when the report is the record of what was decided.
        """
        target = self.output_dir / "runs" / str(run_id)
        target.mkdir(parents=True, exist_ok=True)
        copied = 0
        for page_id, (filename, _label) in PAGES.items():
            source = self.output_dir / filename
            if source.exists():
                shutil.copy2(source, target / filename)
                copied += 1
        data_dir = self.output_dir / "data"
        if data_dir.exists():
            shutil.copytree(data_dir, target / "data", dirs_exist_ok=True)
        log.info("archived %d page(s) to %s", copied, target)
        self._prune_runs()
        return target if copied else None

    def _prune_runs(self) -> None:
        keep = int(self.settings.get("report.keep_runs", 30))
        runs_dir = self.output_dir / "runs"
        if keep <= 0 or not runs_dir.exists():
            return
        # Numeric sort, so run 9 is not pruned before run 10.
        existing = sorted((d for d in runs_dir.iterdir() if d.is_dir()),
                          key=lambda d: int(d.name) if d.name.isdigit() else -1)
        for stale in existing[:-keep]:
            shutil.rmtree(stale, ignore_errors=True)
            log.info("pruned archived run %s", stale.name)

    def page_paths(self) -> List[str]:
        """Existing page files, for attaching to a logbook entry."""
        return [str(self.output_dir / filename)
                for filename, _label in PAGES.values()
                if (self.output_dir / filename).exists()]


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------


def status_class(status: str) -> str:
    """Tailwind classes for a status pill."""
    return {
        "ok": "bg-emerald-100 text-emerald-800 ring-emerald-600/20 "
              "dark:bg-emerald-950 dark:text-emerald-200 dark:ring-emerald-400/30",
        "warn": "bg-amber-100 text-amber-900 ring-amber-600/20 "
                "dark:bg-amber-950 dark:text-amber-200 dark:ring-amber-400/30",
        "fail": "bg-rose-100 text-rose-800 ring-rose-600/20 "
                "dark:bg-rose-950 dark:text-rose-200 dark:ring-rose-400/30",
        "unknown": "bg-slate-200 text-slate-800 ring-slate-500/20 "
                   "dark:bg-slate-800 dark:text-slate-200 dark:ring-slate-400/30",
        "skip": "bg-slate-100 text-slate-500 ring-slate-400/20 "
                "dark:bg-slate-900 dark:text-slate-400 dark:ring-slate-600/30",
    }.get(str(status).lower(), "bg-slate-100 text-slate-700 ring-slate-400/20")


def status_badge(status: str) -> str:
    return {"ok": "OK", "warn": "WARN", "fail": "FAIL",
            "unknown": "UNREACHABLE", "skip": "n/a"}.get(str(status).lower(),
                                                         str(status).upper())


def format_duration(seconds: Any) -> str:
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return "-"
    if value < 60:
        return f"{value:.1f} s"
    if value < 3600:
        return f"{value / 60:.1f} min"
    return f"{value / 3600:.2f} h"


def _group_assessments(assessments: Sequence[Any]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for a in assessments:
        grouped.setdefault(a.node.node_class, []).append(a.as_dict())
    return dict(sorted(grouped.items()))


def _group_locations(assessments: Sequence[Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for a in assessments:
        entry = out.setdefault(a.node.location,
                               {"total": 0, "ok": 0, "warn": 0, "fail": 0,
                                "unknown": 0, "skip": 0})
        entry["total"] += 1
        entry[a.status.value] = entry.get(a.status.value, 0) + 1
    return out


def _combined_counts(phases: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """Totals from the most recent phase that assessed nodes.

    Summing across phases would double-count every node that appears in both
    the assessment and the power-on, so the index shows the latest picture
    rather than an arithmetic artefact.
    """
    for phase in reversed(list(phases)):
        counts = phase.get("counts") or {}
        if counts.get("total"):
            return counts
    return {"total": 0, "ok": 0, "warn": 0, "fail": 0, "unknown": 0, "skip": 0}


#: Shown on the About page (CLAUDE.md requires an About page documenting the
#: project's dependencies).
DEPENDENCIES = [
    {"name": "PyYAML", "use": "configuration and topology files"},
    {"name": "Jinja2", "use": "rendering these report pages"},
    {"name": "SQLAlchemy", "use": "the run store (SQLite by default, Postgres optional)"},
    {"name": "hvac", "use": "reading IPMI credentials from HashiCorp Vault"},
    {"name": "Tailwind CSS (CDN)", "use": "page styling; no build step"},
    {"name": "ipmitool", "use": "BMC access, executed on a DAQ gateway"},
    {"name": "OpenSSH", "use": "node access with Kerberos/GSSAPI, ProxyJump via a gateway"},
    {"name": "Kerberos client (kinit/klist)", "use": "ticket acquisition"},
    {"name": "ecl-client (optional)", "use": "posting the phase-4 report to the logbook"},
    {"name": "libmu2eprobe (optional C++)",
     "use": "OpenMP-parallel reachability sweep; the pure-Python path is used when absent"},
]

#: Documented on the API page.
DATA_FILES = [
    {"path": "data/summary.json", "content": "run metadata, phase status and node counts"},
    {"path": "data/assess.json", "content": "every phase-1 check result, with evidence"},
    {"path": "data/poweron.json", "content": "phase-2 stage outcomes and power actions"},
    {"path": "data/network.json", "content": "phase-3 connectivity matrices per network"},
    {"path": "data/report.json", "content": "the phase-4 narrative: headline, outstanding problems, next steps"},
    {"path": "data/run-export.json", "content": "the complete run export -- every phase, node, check and command"},
    {"path": "data/inventory.json", "content": "the node inventory this run used"},
]
