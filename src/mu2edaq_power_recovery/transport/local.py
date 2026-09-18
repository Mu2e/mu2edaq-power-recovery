"""Run a command on the workstation driving the recovery."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from typing import Any, Optional, Sequence

from .base import Command, CommandResult, TimeoutExpired, Transport, TransportError, as_string

log = logging.getLogger(__name__)

#: Default cap on captured output.  Overridden from logging.max_capture_bytes.
DEFAULT_MAX_CAPTURE = 65536


class LocalTransport(Transport):
    """Local execution: ping, ssh, rsync, kinit, klist, vault, git.

    Commands are run without a shell when given as a sequence, which is the
    normal case; a string command is passed to ``/bin/sh -c`` because some
    probes genuinely need a pipeline.  Nothing here ever interpolates a
    credential into a command line -- secrets go through *input_text*.
    """

    host = "localhost"

    #: Unlike every other transport, this one does not reach a RHEL node: the
    #: operator drives the recovery from Linux, macOS or Windows.  A command
    #: built for this transport may not assume the GNU/iputils spelling.
    platform = sys.platform

    def __init__(self, default_timeout: float = 120.0,
                 max_capture: int = DEFAULT_MAX_CAPTURE,
                 env: Optional[dict] = None):
        self.default_timeout = default_timeout
        self.max_capture = max_capture
        self.env = dict(env) if env else None

    def _truncate(self, raw: bytes) -> tuple:
        text = raw.decode("utf-8", "replace")
        if len(text) <= self.max_capture:
            return text, False
        half = self.max_capture // 2
        return (text[:half] + "\n...[output truncated]...\n" + text[-half:]), True

    def run(self, command: Command, timeout: Optional[float] = None,
            user: Optional[str] = None, input_text: Optional[str] = None,
            check: bool = False) -> CommandResult:
        rendered = as_string(command)
        shell = isinstance(command, str)
        argv: Any = command if not shell else ["/bin/sh", "-c", command]
        env = dict(os.environ)
        if self.env:
            env.update(self.env)

        started = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                input=input_text.encode() if input_text is not None else None,
                timeout=timeout or self.default_timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutExpired(
                f"local command timed out after {timeout or self.default_timeout}s: {rendered}"
            ) from exc
        except OSError as exc:
            raise TransportError(f"could not run {rendered!r}: {exc}") from exc

        stdout, t1 = self._truncate(proc.stdout or b"")
        stderr, t2 = self._truncate(proc.stderr or b"")
        result = CommandResult(
            command=rendered,
            rc=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            host=self.host,
            duration=time.monotonic() - started,
            truncated=t1 or t2,
        )
        log.debug("local rc=%s (%.2fs): %s", result.rc, result.duration, rendered)
        if check and not result.ok:
            raise TransportError(f"{rendered} exited {result.rc}: {result.stderr.strip()}")
        return result
