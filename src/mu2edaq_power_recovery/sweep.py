"""Fast reachability sweep, with a pure-Python fallback.

Phase 1 opens by asking "which of these sixty hosts are up".  Done one at a
time that is sixty timeouts; done with one subprocess per host the fork/exec
cost dominates the measurement.  The C++ extension (:mod:`mu2eprobe`, built by
CMake) does the sweep with one non-blocking socket per host under OpenMP and
releases the GIL while it runs.

The extension is optional.  When it is not built -- no compiler on the
workstation, no pybind11, a platform CMake has not been run on -- this module
does the same thing with a thread pool and :mod:`socket`.  It is slower, and it
is never the reason a recovery cannot start.  :data:`BACKEND` says which one is
in use, and the report records it, so a timing difference between two runs has
an explanation rather than being a mystery.
"""
from __future__ import annotations

import logging
import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from time import monotonic
from typing import Dict, List, Optional, Sequence

log = logging.getLogger(__name__)

try:
    import mu2eprobe as _native  # type: ignore[import]
    BACKEND = "libmu2eprobe"
except ImportError:      # pragma: no cover - depends on whether CMake has run
    _native = None
    BACKEND = "python"


@dataclass
class SweepResult:
    """One host's reachability, in the same shape from either backend."""

    host: str
    reachable: bool
    outcome: str = "unknown"
    address: str = ""
    elapsed_ms: float = 0.0
    detail: str = ""

    def as_dict(self) -> Dict[str, object]:
        return {"host": self.host, "reachable": self.reachable,
                "outcome": self.outcome, "address": self.address,
                "elapsed_ms": round(self.elapsed_ms, 1), "detail": self.detail}


def backend() -> str:
    """Which implementation a sweep will use: 'libmu2eprobe' or 'python'."""
    return BACKEND


def has_openmp() -> bool:
    """True when the native extension is present and was built with OpenMP."""
    return bool(_native is not None and _native.has_openmp())


def _probe_python(host: str, port: int, timeout_ms: int) -> SweepResult:
    """One host, using the standard library.

    A refused connection counts as reachable, exactly as the C++ path treats
    it: the machine answered, sshd simply is not listening yet -- which during
    a power-on is the normal intermediate state, not a failure.
    """
    started = monotonic()
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        return SweepResult(host, False, "unresolved", "",
                           (monotonic() - started) * 1000, str(exc))

    address = infos[0][4][0] if infos else ""
    for family, socktype, proto, _canon, sockaddr in infos:
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(timeout_ms / 1000.0)
        try:
            sock.connect(sockaddr)
            return SweepResult(host, True, "open", address,
                               (monotonic() - started) * 1000)
        except ConnectionRefusedError:
            return SweepResult(host, True, "refused", address,
                               (monotonic() - started) * 1000)
        except socket.timeout:
            continue
        except OSError as exc:
            return SweepResult(host, False, "error", address,
                               (monotonic() - started) * 1000, str(exc))
        finally:
            sock.close()
    return SweepResult(host, False, "timeout", address,
                       (monotonic() - started) * 1000)


def sweep(hosts: Sequence[str], port: int = 22, timeout_ms: int = 2000,
          threads: int = 0) -> List[SweepResult]:
    """Probe every host, returning results in input order."""
    hosts = list(hosts)
    if not hosts:
        return []

    if _native is not None:
        native_results = _native.probe_many(hosts, port=port,
                                            timeout_ms=timeout_ms,
                                            threads=threads)
        return [SweepResult(host=r.host, reachable=r.reachable,
                            outcome=r.outcome_name, address=r.address,
                            elapsed_ms=r.elapsed_ms, detail=r.detail)
                for r in native_results]

    # Bounded: a thread per host would open sixty sockets and sixty threads at
    # once on a workstation, and the win over a sensible pool is negligible
    # because the cost here is waiting, not computing.
    workers = threads if threads > 0 else min(32, max(4, len(hosts)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda h: _probe_python(h, port, timeout_ms), hosts))


def reachable(hosts: Sequence[str], port: int = 22,
              timeout_ms: int = 2000) -> List[str]:
    """Just the hosts that answered, in input order."""
    return [r.host for r in sweep(hosts, port, timeout_ms) if r.reachable]
