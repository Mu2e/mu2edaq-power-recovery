"""Post the recovery report to the Fermilab Electronic Logbook.

The posting itself is done by ecl-client (https://github.com/normanajn/ecl-client),
which already implements the ECL 8.x XML posting API for C, C++ and Python.
Reimplementing that here would be a second copy of a protocol that is already
maintained, so this module only builds the entry body and hands it over.

ecl-client is an optional dependency: a recovery must not fail because the
logbook is unreachable or the package is not installed on the workstation.  When
it is missing, :meth:`ECLPoster.post` raises :class:`ECLError`, phase 4 records
that in its notes, and the HTML report -- which is the durable artefact -- is
unaffected.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger(__name__)


class ECLError(RuntimeError):
    """The logbook entry could not be posted."""


class ECLPoster:
    """Builds and submits the phase-4 logbook entry."""

    def __init__(self, settings: Any, vault: Optional[Any] = None):
        self.settings = settings
        self.vault = vault
        self.url: str = settings.get("ecl.url")
        self.category: str = settings.get("ecl.category", "DAQ/Recovery")

    # -- credentials -------------------------------------------------------

    def credentials(self) -> Dict[str, str]:
        """ECL API user and key, from Vault under ``ecl.credential_path``."""
        if self.vault is None:
            raise ECLError("no Vault client available to read the ECL credentials")
        data = self.vault.ecl()
        user = data.get("user") or data.get("username")
        key = data.get("key") or data.get("password") or data.get("api_key")
        if not (user and key):
            raise ECLError(
                "the ECL credential secret has no user/key pair; "
                f"fields present: {', '.join(sorted(data)) or '(none)'}")
        return {"user": str(user), "key": str(key)}

    # -- entry construction ------------------------------------------------

    def subject(self, narrative: Dict[str, Any]) -> str:
        run = narrative.get("run", {})
        label = run.get("label") or "DAQ power recovery"
        prefix = "[DRY RUN] " if narrative.get("dry_run") else ""
        return f"{prefix}{label} -- {narrative.get('headline', '')}"

    def body(self, narrative: Dict[str, Any]) -> str:
        """The entry text.

        Plain text with a fixed section order, not HTML: the logbook renders
        entries in a narrow column, and a reader skimming it months later wants
        the outcome, the outstanding problems and the follow-ups -- the full
        evidence stays in the linked report pages.
        """
        run = narrative.get("run", {})
        counts = narrative.get("counts", {})
        lines: List[str] = []

        lines.append(narrative.get("headline", ""))
        lines.append("")
        lines.append(f"Run          : {run.get('id')}  ({run.get('label') or 'unlabelled'})")
        lines.append(f"Operator     : {run.get('operator')} on {run.get('workstation')}")
        lines.append(f"Started      : {run.get('started_at')}")
        lines.append(f"Finished     : {run.get('finished_at') or '(still running)'}")
        lines.append(f"Mode         : {'DRY RUN -- nothing was switched on' if narrative.get('dry_run') else 'live'}")
        version = (run.get("version") or {})
        lines.append(f"Tool version : {version.get('package_version')} "
                     f"({version.get('git_describe') or version.get('git_commit')})")
        lines.append("")

        lines.append("Phases")
        lines.append("-" * 60)
        for phase in narrative.get("phases", []):
            lines.append(f"  {phase['number']}. {phase['name']:<9} "
                         f"{phase['status']:<22} {phase.get('summary', '')}")
        lines.append("")

        lines.append("Node totals")
        lines.append("-" * 60)
        lines.append(f"  total {counts.get('total', 0)}   ok {counts.get('ok', 0)}   "
                     f"warn {counts.get('warn', 0)}   fail {counts.get('fail', 0)}   "
                     f"unreachable {counts.get('unknown', 0)}")
        lines.append("")

        if narrative.get("powered_on"):
            lines.append("Powered on by this run")
            lines.append("-" * 60)
            for host in narrative["powered_on"]:
                lines.append(f"  {host}")
            lines.append("")

        outstanding = narrative.get("outstanding", [])
        if outstanding:
            lines.append(f"Outstanding problems ({len(outstanding)})")
            lines.append("-" * 60)
            for item in outstanding[:60]:
                lines.append(f"  {item['node']:<28} {item['check']:<18} "
                             f"{item['status']:<8} {item['summary']}")
            if len(outstanding) > 60:
                lines.append(f"  ... and {len(outstanding) - 60} more "
                             f"(see the attached report)")
            lines.append("")

        if narrative.get("power_problems"):
            lines.append("Power actions that did not complete")
            lines.append("-" * 60)
            for item in narrative["power_problems"]:
                lines.append(f"  {item['node']:<28} {item['outcome']:<12} {item['detail']}")
            lines.append("")

        lines.append("Next steps")
        lines.append("-" * 60)
        for step in narrative.get("next_steps", []):
            lines.append(f"  * {step}")
        lines.append("")
        lines.append(f"Generated by mu2edaq-power-recovery at "
                     f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}")
        return "\n".join(lines)

    def tags(self, narrative: Dict[str, Any]) -> List[str]:
        tags = ["power-recovery"]
        if narrative.get("dry_run"):
            tags.append("dry-run")
        if narrative.get("failed") or narrative.get("unreachable"):
            tags.append("needs-attention")
        return tags

    # -- posting -----------------------------------------------------------

    def post(self, narrative: Dict[str, Any],
             attachments: Sequence[str] = ()) -> Dict[str, Any]:
        """Submit the entry; returns whatever the client reports about it."""
        try:
            import ecl_client  # type: ignore[import]
        except ImportError as exc:
            raise ECLError(
                "the ecl-client package is not installed.\n"
                "Install it from https://github.com/normanajn/ecl-client "
                "(pip install .) or run with ecl.enabled: false."
            ) from exc

        creds = self.credentials()
        subject = self.subject(narrative)
        body = self.body(narrative)
        files = list(attachments) if self.settings.get("ecl.attach_html", True) else []

        log.info("posting to the ECL at %s (category %s) with %d attachment(s)",
                 self.url, self.category, len(files))
        try:
            # ecl-client's Python surface has moved between releases; try the
            # documented entry point first and fall back to the class API
            # rather than pinning a version an operator may not have.
            if hasattr(ecl_client, "post"):
                response = ecl_client.post(
                    url=self.url, user=creds["user"], key=creds["key"],
                    subject=subject, text=body, category=self.category,
                    tags=self.tags(narrative), files=files)
            else:
                client = ecl_client.ECLClient(self.url, creds["user"], creds["key"])
                response = client.post(subject=subject, text=body,
                                       category=self.category,
                                       tags=self.tags(narrative), files=files)
        except Exception as exc:  # noqa: BLE001 - client raises its own types
            raise ECLError(f"posting to {self.url} failed: {exc}") from exc

        return {"url": self.url, "category": self.category, "subject": subject,
                "attachments": files, "response": str(response)[:500]}
