"""Copy the generated site to wherever it is meant to be read from.

Project-Description.md allows the report location to be local or a remote
upload address, so this supports three methods: leaving the files where they
are, ``rsync``, and ``scp``.  rsync is the default because it is incremental
and can delete files that no longer exist on the source, which keeps a stale
page from a previous run from lingering on the web server.

Publication failure is never fatal.  The report on disk is the artefact; the
copy on a web server is a convenience, and an outage is a plausible reason for
that server to be unreachable.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..transport.base import TransportError
from ..transport.local import LocalTransport

log = logging.getLogger(__name__)


class PublishError(RuntimeError):
    """The site could not be copied to its destination."""


class Publisher:
    """Pushes ``report.output_dir`` to ``report.publish.target``."""

    def __init__(self, settings: Any, local: Optional[LocalTransport] = None,
                 simulate: bool = False):
        self.settings = settings
        self.local = local or LocalTransport(default_timeout=600)
        #: A rehearsal must not touch the live web area.  The 'copy' method
        #: calls shutil.copytree directly rather than going through *local*,
        #: so scripting the transport is not enough to hold it back.
        self.simulate = simulate
        self.enabled: bool = bool(settings.get("report.publish.enabled", False))
        self.method: str = str(settings.get("report.publish.method", "rsync")).lower()
        self.target: Optional[str] = settings.get("report.publish.target")
        self.options: List[str] = list(settings.get("report.publish.options", []) or [])
        self.source: Path = settings.resolve_path(
            settings.get("report.output_dir", "html"))

    def publish(self) -> Dict[str, Any]:
        """Copy the site; returns what happened, never raises for a bad target."""
        if self.simulate:
            return {"published": False,
                    "reason": "simulated run: the report was written locally "
                              "but nothing was published"}
        if not self.enabled or self.method == "none":
            return {"published": False, "reason": "publication not enabled"}
        if not self.target:
            return {"published": False,
                    "reason": "report.publish.enabled is true but no target is set"}
        if not self.source.exists():
            return {"published": False,
                    "reason": f"nothing to publish: {self.source} does not exist"}

        try:
            if self.method == "rsync":
                result = self._rsync()
            elif self.method == "scp":
                result = self._scp()
            elif self.method == "copy":
                result = self._copy()
            else:
                return {"published": False,
                        "reason": f"unknown publish method {self.method!r}"}
        except (TransportError, OSError) as exc:
            log.error("publication failed: %s", exc)
            return {"published": False, "reason": str(exc), "method": self.method,
                    "target": self.target}

        result.update({"method": self.method, "target": self.target,
                       "source": str(self.source)})
        return result

    # -- methods -----------------------------------------------------------

    def _rsync(self) -> Dict[str, Any]:
        # The trailing slash on the source is load-bearing: without it rsync
        # creates a nested html/ directory inside the target.
        argv = ["rsync", *self.options, f"{self.source}/", self.target]
        res = self.local.run(argv, timeout=900)
        if not res.ok:
            return {"published": False,
                    "reason": f"rsync exited {res.rc}: {res.stderr.strip()[:300]}"}
        log.info("published %s to %s with rsync", self.source, self.target)
        return {"published": True, "detail": res.stdout.strip()[-500:]}

    def _scp(self) -> Dict[str, Any]:
        argv = ["scp", "-r", *self.options, f"{self.source}/.", self.target]
        res = self.local.run(argv, timeout=900)
        if not res.ok:
            return {"published": False,
                    "reason": f"scp exited {res.rc}: {res.stderr.strip()[:300]}"}
        log.info("published %s to %s with scp", self.source, self.target)
        return {"published": True, "detail": res.stdout.strip()[-500:]}

    def _copy(self) -> Dict[str, Any]:
        """Local filesystem copy -- for a target that is a mounted web area."""
        destination = Path(self.target).expanduser()
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.source, destination, dirs_exist_ok=True)
        log.info("copied %s to %s", self.source, destination)
        return {"published": True, "detail": f"copied to {destination}"}
