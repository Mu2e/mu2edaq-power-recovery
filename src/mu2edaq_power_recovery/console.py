"""Terminal output.

Project-Description.md asks for an on-screen report as well as the web pages.
This module is that on-screen report: plain ANSI, no curses, no progress bars
that break when the output is piped to a file -- which it will be, because an
operator running a recovery keeps a transcript.

Colour is used only for status and is suppressed automatically when stdout is
not a terminal or when NO_COLOR is set.
"""
from __future__ import annotations

import os
import shutil
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .checks import Status

_COLORS = {
    Status.OK: "\033[32m",
    Status.WARN: "\033[33m",
    Status.FAIL: "\033[31m",
    Status.UNKNOWN: "\033[35m",
    Status.SKIP: "\033[90m",
}
_RESET = "\033[0m"
_BOLD = "\033[1m"

_LABELS = {
    Status.OK: "OK",
    Status.WARN: "WARN",
    Status.FAIL: "FAIL",
    Status.UNKNOWN: "UNREACH",
    Status.SKIP: "n/a",
}


def use_color(stream=None) -> bool:
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return bool(getattr(stream, "isatty", lambda: False)())


def width(default: int = 100) -> int:
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except OSError:
        return default


def paint(text: str, status: Status, enabled: Optional[bool] = None) -> str:
    if enabled is None:
        enabled = use_color()
    if not enabled:
        return text
    return f"{_COLORS.get(status, '')}{text}{_RESET}"


def status_label(status: Status, enabled: Optional[bool] = None) -> str:
    return paint(f"{_LABELS.get(status, str(status)):<8}", status, enabled)


def rule(char: str = "-", title: str = "") -> str:
    cols = min(width(), 100)
    if not title:
        return char * cols
    prefix = f"{char * 3} {title} "
    return prefix + char * max(0, cols - len(prefix))


def heading(text: str, enabled: Optional[bool] = None) -> str:
    if enabled is None:
        enabled = use_color()
    line = rule("=")
    body = f"{_BOLD}{text}{_RESET}" if enabled else text
    return f"\n{line}\n{body}\n{line}"


def table(rows: Sequence[Sequence[str]], headers: Sequence[str],
          aligns: Optional[Sequence[str]] = None) -> str:
    """A fixed-width text table.

    Column widths are computed from the content and then capped so that a long
    summary cannot push the status column off the right edge of an 80-column
    terminal -- the status is the part the operator is scanning for.
    """
    if not rows:
        return "  (nothing to show)"
    columns = len(headers)
    aligns = list(aligns or ["<"] * columns)
    widths = [len(str(h)) for h in headers]
    for row in rows:
        for i in range(columns):
            widths[i] = max(widths[i], len(str(row[i]) if i < len(row) else ""))
    budget = min(width(), 160)
    fixed = sum(widths[:-1]) + 2 * columns
    widths[-1] = max(12, min(widths[-1], budget - fixed))

    def render(values: Iterable[Any]) -> str:
        cells = []
        for i, value in enumerate(values):
            text = str(value)
            if len(text) > widths[i]:
                text = text[: widths[i] - 1] + "…"
            cells.append(f"{text:{aligns[i]}{widths[i]}}")
        return "  " + "  ".join(cells).rstrip()

    out = [render(headers), "  " + "  ".join("-" * w for w in widths)]
    out.extend(render(list(r) + [""] * (columns - len(r))) for r in rows)
    return "\n".join(out)


def node_table(assessments: Sequence[Any], show_power: bool = False,
               enabled: Optional[bool] = None) -> str:
    headers = ["NODE", "CLASS", "STATUS"] + (["POWER"] if show_power else []) + ["SUMMARY"]
    rows: List[List[str]] = []
    for a in assessments:
        row = [a.node.short, a.node.node_class, status_label(a.status, enabled)]
        if show_power:
            action = (a.power_action or {}).get("action")
            row.append(f"{a.power_state or '-'}"
                       + (f" [{action}]" if action and action != "none" else ""))
        row.append(a.summary())
        rows.append(row)
    return table(rows, headers)


def failure_detail(assessments: Sequence[Any], limit: int = 40) -> str:
    """The failing checks, node by node -- what the operator acts on."""
    lines: List[str] = []
    shown = 0
    for a in assessments:
        bad = [r for r in a.results if r.status.is_bad]
        if not bad:
            continue
        lines.append(f"\n  {a.node.short}  ({a.node.node_class}, {a.node.location})")
        for r in bad:
            if shown >= limit:
                lines.append(f"    ... more failures suppressed; see the report page")
                return "\n".join(lines)
            lines.append(f"    {status_label(r.status)} {r.check_id:<18} {r.summary}")
            if r.detail:
                for detail_line in r.detail.strip().splitlines()[:3]:
                    lines.append(f"             {detail_line}")
            shown += 1
    return "\n".join(lines) if lines else "  (no failures)"


def counts_line(counts: Dict[str, int], enabled: Optional[bool] = None) -> str:
    parts = []
    for status in (Status.OK, Status.WARN, Status.FAIL, Status.UNKNOWN, Status.SKIP):
        value = counts.get(status.value, 0)
        # Skips are only worth a column when there are some; every other
        # status is shown even at zero so the line has a constant shape.
        if status is Status.SKIP and not value:
            continue
        parts.append(paint(f"{value} {_LABELS[status].lower()}", status, enabled))
    return f"  {counts.get('total', 0)} node(s): " + ", ".join(parts)


def phase_banner(result: Any, enabled: Optional[bool] = None) -> str:
    """Phase heading, plus the summary when no node table will follow it.

    With nodes, :func:`counts_line` says the same thing in colour immediately
    below, so printing both is just noise on an already busy console.
    """
    banner = heading(f"Phase {result.number}: {result.title}", enabled)
    if getattr(result, "assessments", None):
        return banner
    return f"{banner}\n  {result.summary}"
