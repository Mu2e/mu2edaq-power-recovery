"""Transport interface and the result object every check reasons about."""
from __future__ import annotations

import abc
import shlex
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Union

Command = Union[str, Sequence[str]]


class TransportError(RuntimeError):
    """The command could not be run at all (host unreachable, ssh refused).

    Distinct from a command that ran and exited non-zero: that is a
    :class:`CommandResult` with ``rc != 0``, which is information.  A
    TransportError means we learned nothing about the node.
    """


class TimeoutExpired(TransportError):
    """The command was started but did not finish inside its budget."""


@dataclass
class CommandResult:
    """Outcome of one command, on one host.

    ``stdout``/``stderr`` are decoded text, truncated to the configured
    capture limit; ``truncated`` says whether that happened, so a report never
    presents a clipped log as if it were complete.
    """

    command: str
    rc: int
    stdout: str = ""
    stderr: str = ""
    host: str = "localhost"
    duration: float = 0.0
    truncated: bool = False
    # Free-form transport annotations (jump host used, retry count, ...).
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.rc == 0

    @property
    def output(self) -> str:
        """stdout, falling back to stderr when the command wrote only there."""
        return self.stdout if self.stdout.strip() else self.stderr

    def lines(self) -> List[str]:
        return [l for l in self.output.splitlines() if l.strip()]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "command": self.command,
            "host": self.host,
            "rc": self.rc,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration": round(self.duration, 3),
            "truncated": self.truncated,
            "meta": dict(self.meta),
        }


def as_string(command: Command) -> str:
    """Render a command for logging and for shell execution on a remote host."""
    if isinstance(command, str):
        return command
    return " ".join(shlex.quote(str(part)) for part in command)


class Transport(abc.ABC):
    """Somewhere a command can be run."""

    #: Host this transport addresses, for logging and CommandResult.host.
    host: str = "localhost"

    @abc.abstractmethod
    def run(self, command: Command, timeout: Optional[float] = None,
            user: Optional[str] = None, input_text: Optional[str] = None,
            check: bool = False) -> CommandResult:
        """Run *command* and return its result.

        Parameters
        ----------
        timeout:
            Seconds, or None for the transport's configured default.
        user:
            Run as this user.  For SSH this selects the login; for the local
            transport it is ignored (we never sudo on the operator's laptop).
        input_text:
            Written to the command's stdin.  This is how secrets reach a
            remote command without ever appearing in an argument vector.
        check:
            Raise :class:`TransportError` on a non-zero exit instead of
            returning the result.  Off by default -- most checks want to
            inspect the failure rather than have it raised.
        """

    def run_ok(self, command: Command, **kwargs: Any) -> bool:
        """True when the command exits zero; transport failures count as False."""
        try:
            return self.run(command, **kwargs).ok
        except TransportError:
            return False

    def close(self) -> None:
        """Release anything held open.  Idempotent; the default does nothing."""

    def __enter__(self) -> "Transport":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
