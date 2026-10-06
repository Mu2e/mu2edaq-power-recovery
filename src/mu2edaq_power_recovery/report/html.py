"""Static report site.

Plain files, written with Jinja2 and styled with Tailwind from a CDN, with a
little vanilla JavaScript for filtering and sorting.  There is deliberately no
server and no build step: the report has to be readable from a laptop during an
outage, copied to a web area, or attached to a logbook entry, and any of those
rules out a running application.

Every run is rendered into ``runs/<id>/`` from its own stored rows, so a
run's pages can be rebuilt at any time and never contain another run's
evidence; the top level is the newest run's view plus the run history.
"""
from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from dataclasses import dataclass, field
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


#: Pages rendered from one run's data. about/api/sitemap document the tool,
#: but are rendered with the run's own version and settings; runs.html is the
#: only cross-run page and exists at the top level alone.
PHASE_PAGES = ("assess", "poweron", "network", "report")
ALWAYS_PRESENT = ("index", "runs", "about", "api", "sitemap")

#: Lifecycle status of a stored phase row -> verdict, for rows written before
#: the phase's own verdict was persisted in its data (``data._result``).
_LIFECYCLE = {"complete": "ok", "complete_with_failures": "fail",
              "failed": "fail", "aborted": "fail", "timed_out": "unknown",
              "running": "unknown"}
_RANK = {"ok": 0, "skip": 1, "warn": 2, "unknown": 3, "fail": 4}


@dataclass
class Bundle:
    """The files rendered for one run under ``runs/<id>/``.

    ``paths`` are the HTML pages -- the set attached to a logbook entry --
    and ``data`` the JSON companions. Every one of them is derived from the
    run's stored rows and nothing else.
    """

    run_id: int
    directory: Path
    present: List[str]
    paths: List[str] = field(default_factory=list)
    data: List[str] = field(default_factory=list)
    #: Whether the top-level latest view was refreshed from this run.
    latest: bool = False
    latest_dir: Optional[Path] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"run_id": self.run_id, "directory": str(self.directory),
                "present": list(self.present), "pages": list(self.paths),
                "data": list(self.data), "latest": self.latest}


class ReportWriter:
    """Renders the site into ``report.output_dir``.

    Layout: ``runs/<id>/`` holds one run's pages and ``data/``, rendered only
    from that run's export (:meth:`render_run`). The top level is the *latest
    view*: the newest run's bundle, rendered again with the site-wide run
    history link, plus ``runs.html``. Regenerating an older run rewrites its
    bundle and leaves the latest view alone.
    """

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
               subdir: Optional[Path] = None,
               present: Optional[Sequence[str]] = None,
               root: str = "") -> Path:
        """Render one page into *subdir* (default: the top level).

        *present* is the set of page ids the navigation may link: a phase the
        run does not contain is never linked. *root* is the path from the
        page to the site root, for the one cross-run link (run history).
        """
        filename, _label = PAGES[page_id]
        template = self.env.get_template(f"{page_id}.html")
        target_dir = subdir or self.output_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / filename
        context = {
            "page_id": page_id,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "present": list(present) if present is not None else list(PAGES),
            "root": root,
            **context,
        }
        path.write_text(template.render(**context), encoding="utf-8")
        log.info("wrote %s", path)
        return path

    @staticmethod
    def _write_json(directory: Path, name: str, payload: Any) -> Path:
        data_dir = directory / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        path = data_dir / f"{name}.json"
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return path

    # -- one run -----------------------------------------------------------

    def render_run(self, export: Dict[str, Any], narrative: Dict[str, Any],
                   version: Optional[Dict[str, Any]] = None,
                   latest: bool = False,
                   checks: Optional[Dict[str, str]] = None,
                   runs: Optional[Sequence[Dict[str, Any]]] = None,
                   report: Optional[Dict[str, Any]] = None) -> Bundle:
        """Render ``runs/<id>/`` from *export* alone, and the latest view.

        Only the phases present in the run get a page and a data file, and
        the navigation links only those. The bundle directory is emptied
        first, so nothing rendered for another run -- or copied there by an
        older release -- can survive into it. With *latest* (the run is the
        newest in the store) the same pages are rendered at the top level and
        the top-level pages of phases this run lacks are removed; otherwise
        the top level is left as it is. *report* is the phase-4 result as a
        dict (for data/report.json); without it one is built from the stored
        report phase. ``runs.html`` is refreshed from *runs* either way, and
        ``report.keep_runs`` is applied -- never to the bundle just rendered,
        which the caller is about to attach to a logbook entry.
        """
        run = export.get("run") or {}
        rid = int(run["id"])
        version = version or run.get("version") or {}
        present_phases = [name for name in PHASE_PAGES
                          if _latest_phase(export, name) is not None]
        present = list(ALWAYS_PRESENT) + present_phases

        target = self.output_dir / "runs" / str(rid)
        if target.exists():
            shutil.rmtree(target)
        bundle = Bundle(run_id=rid, directory=target, present=present)
        pages, data = self._render_set(target, "../../", export, narrative,
                                       version, present, checks, report)
        bundle.paths, bundle.data = pages, data

        if latest:
            self._render_set(self.output_dir, "", export, narrative, version,
                             present, checks, report)
            self._remove_absent(present)
            bundle.latest, bundle.latest_dir = True, self.output_dir
        if runs is not None:
            self.write_runs(runs)
        self._prune_runs(protect=rid)
        return bundle

    def _render_set(self, directory: Path, root: str, export: Dict[str, Any],
                    narrative: Dict[str, Any], version: Dict[str, Any],
                    present: Sequence[str], checks: Optional[Dict[str, str]],
                    report: Optional[Dict[str, Any]]):
        run = export.get("run") or {}
        pages: List[str] = []
        data: List[str] = []
        kw = {"subdir": directory, "present": present, "root": root}

        phase_rows = []
        for stored in export.get("phases", []):
            view = stored_phase_view(stored)
            phase_rows.append({k: view[k] for k in (
                "name", "number", "title", "status", "summary", "duration",
                "counts", "lifecycle", "started_at", "finished_at")})

        for name in PHASE_PAGES:
            stored = _latest_phase(export, name)
            if stored is None:
                continue
            view = stored_phase_view(stored)
            if name == "report":
                context = {"phase": view, "run": run, "version": version,
                           "narrative": narrative}
                payload = report or {**{k: v for k, v in view.items()
                                        if k != "assessments"},
                                     "data": {"run_id": run.get("id"),
                                              "narrative": narrative}}
                payload = {**payload, "run_id": run.get("id")}
            else:
                context = {"phase": view, "run": run, "version": version,
                           "grouped": _group_dicts(view["assessments"]),
                           "locations": _location_dicts(view["assessments"])}
                if name == "network":
                    context["networks"] = view["data"].get("networks", [])
                if name == "poweron":
                    context["stages"] = view["data"].get("stages", [])
                payload = {**view, "run_id": run.get("id")}
            pages.append(str(self._write(name, context, **kw)))
            data.append(str(self._write_json(directory, name, payload)))

        notes = [e.get("message") for e in export.get("events", [])
                 if e.get("level") == "note"]
        pages.append(str(self._write("index", {
            "run": run, "phases": phase_rows, "version": version,
            "notes": notes, "counts": narrative.get("counts") or {},
            "narrative": narrative}, **kw)))
        pages.append(str(self._write("about", {
            "version": version, "settings": run.get("settings") or {},
            "dependencies": DEPENDENCIES}, **kw)))
        pages.append(str(self._write("api", {
            "checks": checks or {}, "endpoints": DATA_FILES,
            "version": version}, **kw)))
        pages.append(str(self._write("sitemap", {"version": version}, **kw)))

        data.append(str(self._write_json(directory, "summary", {
            "run_id": run.get("id"), "run": run, "phases": phase_rows,
            "version": version, "notes": notes,
            "status": narrative.get("status"),
            "counts": narrative.get("counts") or {}})))
        data.append(str(self._write_json(directory, "run-export", export)))
        data.append(str(self._write_json(directory, "inventory",
                                         _inventory(export))))
        return pages, data

    def _remove_absent(self, present: Sequence[str]) -> None:
        """Drop top-level pages and data of phases the newest run lacks."""
        for name in PHASE_PAGES:
            if name in present:
                continue
            for stale in (self.output_dir / PAGES[name][0],
                          self.output_dir / "data" / f"{name}.json"):
                if stale.exists():
                    stale.unlink()
                    log.info("removed %s: the newest run has no %s phase",
                             stale, name)

    def record_ecl(self, bundle: Bundle, ecl: Dict[str, Any]) -> None:
        """Write the logbook outcome into the bundle's data/report.json.

        Also into the latest view's copy when this bundle is the latest. A
        run without a report phase has no report.json and is left alone.
        """
        dirs = [bundle.directory] + ([bundle.latest_dir] if bundle.latest_dir else [])
        for directory in dirs:
            path = directory / "data" / "report.json"
            if not path.exists():
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["ecl"] = ecl
            path.write_text(json.dumps(payload, indent=2, default=str),
                            encoding="utf-8")

    # -- the top level -----------------------------------------------------

    def write_runs(self, runs: Sequence[Dict[str, Any]]) -> Path:
        return self._write("runs", {"runs": list(runs)})

    def write_phase(self, phase_result: Any, run: Dict[str, Any],
                    version: Dict[str, Any],
                    subdir: Optional[Path] = None) -> Path:
        """Render one live phase result (the driver uses :meth:`render_run`)."""
        page_id = phase_result.name
        if page_id not in PAGES:
            raise KeyError(f"no page defined for phase {page_id!r}")
        phase = phase_result.as_dict()
        context = {
            "phase": phase,
            "run": run,
            "version": version,
            "grouped": _group_dicts(phase["assessments"]),
            "locations": _location_dicts(phase["assessments"]),
        }
        if page_id == "network":
            context["networks"] = phase_result.data.get("networks", [])
        if page_id == "poweron":
            context["stages"] = phase_result.data.get("stages", [])
        if page_id == "report":
            context["narrative"] = phase_result.data.get("narrative", {})
        return self._write(page_id, context, subdir=subdir)

    def write_static_pages(self, version: Dict[str, Any],
                           checks: Optional[Dict[str, str]] = None,
                           settings_dump: Optional[Dict[str, Any]] = None) -> List[Path]:
        """About, API and sitemap -- the pages that document the tool itself."""
        return [
            self._write("about", {"version": version,
                                  "settings": settings_dump or {},
                                  "dependencies": DEPENDENCIES}),
            self._write("api", {"checks": checks or {},
                                "endpoints": DATA_FILES,
                                "version": version}),
            self._write("sitemap", {"version": version}),
        ]

    def _prune_runs(self, protect: Optional[int] = None) -> None:
        """Keep the newest ``report.keep_runs`` bundles, and *protect*.

        *protect* is the run just rendered. Regenerating an old run used to
        render its bundle and then prune it straight away as one of the
        oldest, so ``--post-ecl`` attached files that no longer existed. It
        is kept in addition to the newest *keep*, so regenerating an old run
        never costs a newer one its bundle either; the extra one goes at the
        next render of a newer run.
        """
        keep = int(self.settings.get("report.keep_runs", 30))
        runs_dir = self.output_dir / "runs"
        if keep <= 0 or not runs_dir.exists():
            return
        # Numeric sort, so run 9 is not pruned before run 10.
        existing = sorted((d for d in runs_dir.iterdir() if d.is_dir()),
                          key=lambda d: int(d.name) if d.name.isdigit() else -1)
        for stale in existing[:-keep]:
            if protect is not None and stale.name == str(protect):
                continue
            shutil.rmtree(stale, ignore_errors=True)
            log.info("pruned archived run %s", stale.name)


# ---------------------------------------------------------------------------
# Views of stored rows
# ---------------------------------------------------------------------------


def _latest_phase(export: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    """The most recent stored phase of *name* (re-runs append)."""
    rows = [p for p in export.get("phases", []) if p.get("name") == name]
    return rows[-1] if rows else None


def _assessment_view(node: Dict[str, Any],
                     checks: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """A stored node row and its checks, in NodeAssessment.as_dict() shape."""
    data = node.get("data") or {}
    hostname = node.get("hostname", "")
    return {
        "node": {"hostname": hostname, "short": hostname.split(".")[0],
                 "location": node.get("location"),
                 "class": node.get("node_class"),
                 "networks": data.get("networks") or {}},
        "status": node.get("status"),
        "summary": node.get("summary") or "",
        "power_state": node.get("power_state"),
        "power_action": data.get("power_action"),
        "unreachable": bool(data.get("unreachable")),
        "timed_out": bool(data.get("timed_out")),
        "duration": data.get("duration") or 0.0,
        "results": [{"node": c.get("hostname"), "check_id": c.get("check_id"),
                     "status": c.get("status"), "summary": c.get("summary") or "",
                     "detail": c.get("detail") or "", "data": c.get("data") or {},
                     "evidence": c.get("evidence") or [],
                     "duration": c.get("duration") or 0.0,
                     "started_at": c.get("recorded_at")}
                    for c in checks],
    }


def stored_phase_view(stored: Dict[str, Any]) -> Dict[str, Any]:
    """A stored phase row in PhaseResult.as_dict() shape.

    The verdict, title, notes and duration come from ``data._result``, which
    the driver persists when a phase returns; older rows fall back to the
    worst node verdict, or to their lifecycle status.
    """
    data = dict(stored.get("data") or {})
    result = data.pop("_result", None) or {}
    by_host: Dict[str, List[Dict[str, Any]]] = {}
    for c in stored.get("checks", []):
        by_host.setdefault(c.get("hostname"), []).append(c)
    assessments = [_assessment_view(n, by_host.get(n.get("hostname"), []))
                   for n in stored.get("nodes", [])]
    assessments.sort(key=lambda a: (str(a["node"]["location"]),
                                    str(a["node"]["class"]),
                                    a["node"]["hostname"]))
    counts = {s: 0 for s in ("ok", "warn", "fail", "unknown", "skip")}
    for a in assessments:
        counts[a["status"]] = counts.get(a["status"], 0) + 1
    counts["total"] = len(assessments)

    status = result.get("status")
    if status is None:
        if assessments:
            status = max((a["status"] for a in assessments),
                         key=lambda s: _RANK.get(s, 3))
        else:
            status = _LIFECYCLE.get(stored.get("status"), "unknown")
    return {
        "name": stored.get("name"),
        "number": stored.get("number"),
        "title": result.get("title") or PHASE_TITLES.get(stored.get("name"),
                                                         stored.get("name")),
        "status": status,
        "lifecycle": stored.get("status"),
        "summary": stored.get("summary") or "",
        "counts": counts,
        "notes": list(result.get("notes") or []),
        "data": data,
        "started_at": stored.get("started_at"),
        "finished_at": stored.get("finished_at"),
        "duration": result.get("duration") or 0.0,
        "assessments": assessments,
    }


def _inventory(export: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The nodes this run recorded, latest row per host."""
    nodes: Dict[str, Dict[str, Any]] = {}
    for phase in export.get("phases", []):
        for n in phase.get("nodes", []):
            data = n.get("data") or {}
            nodes[n["hostname"]] = {"hostname": n["hostname"],
                                    "short": n["hostname"].split(".")[0],
                                    "location": n.get("location"),
                                    "class": n.get("node_class"),
                                    "networks": data.get("networks") or {}}
    return [nodes[h] for h in sorted(nodes)]


PHASE_TITLES = {
    "assess": "Initial state",
    "poweron": "Power on",
    "network": "Network connectivity",
    "report": "Recovery report",
}


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


def _group_dicts(assessments: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for a in assessments:
        grouped.setdefault(a["node"].get("class") or "other", []).append(a)
    return dict(sorted(grouped.items()))


def _location_dicts(assessments: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for a in assessments:
        entry = out.setdefault(a["node"].get("location") or "unknown",
                               {"total": 0, "ok": 0, "warn": 0, "fail": 0,
                                "unknown": 0, "skip": 0})
        entry["total"] += 1
        entry[a["status"]] = entry.get(a["status"], 0) + 1
    return out


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
    {"path": "data/summary.json", "content": "run metadata, phase status and the reconciled node counts"},
    {"path": "data/assess.json", "content": "every phase-1 check result, with evidence"},
    {"path": "data/poweron.json", "content": "phase-2 stage outcomes and power actions"},
    {"path": "data/network.json", "content": "phase-3 connectivity matrices per network"},
    {"path": "data/report.json", "content": "the phase-4 narrative: headline, outstanding and resolved problems, next steps, logbook outcome"},
    {"path": "data/run-export.json", "content": "the complete run export -- every phase, node, check and command"},
    {"path": "data/inventory.json", "content": "the nodes this run recorded"},
]
