"""Health checks.

Importing this package registers every check implementation; the registry is
then addressed by the dotted ids used in config/checks.yaml.  The imports below
look unused and are not -- each module's @register decorators run on import.
"""
from .base import (CheckContext, CheckResult, DESCRIPTIONS, NEEDS_ROOT, REGISTRY,
                   Status, profile_checks, profile_for_node, register, result,
                   rollup, run_check, worst)

from . import reachability   # noqa: F401  ping.lab, ssh.login*, login.users
from . import host           # noqa: F401  host.uptime, host.kernel
from . import disks          # noqa: F401  disk.*
from . import network        # noqa: F401  net.*
from . import power          # noqa: F401  power.*
from . import services       # noqa: F401  svc.*, pcie.*
from .mesh import MeshProbe, MeshResult, MeshEdge

__all__ = [
    "CheckContext", "CheckResult", "Status", "REGISTRY", "DESCRIPTIONS",
    "NEEDS_ROOT", "register", "result", "run_check", "worst", "rollup",
    "profile_checks", "profile_for_node",
    "MeshProbe", "MeshResult", "MeshEdge",
]
