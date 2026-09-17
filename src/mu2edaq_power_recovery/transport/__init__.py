"""Ways of running a command somewhere other than here.

Three implementations share one interface (:class:`~.base.Transport`):

* :mod:`~.local`  -- run on this workstation (ping, ssh, rsync, kinit)
* :mod:`~.ssh`    -- run on a DAQ node, via a gateway ProxyJump when needed
* :mod:`~.fake`   -- a scripted transport for pytest and for ``--simulate``

Checks are written against the interface only, which is what makes the whole
check suite runnable with no cluster attached.
"""
from .base import CommandResult, Transport, TransportError, TimeoutExpired
from .local import LocalTransport
from .ssh import SSHTransport, SSHError, SSHFactory
from .ipmi import IPMIClient, IPMIError, PowerState
from .fake import FakeTransport, ScriptedResponse, healthy_node_rules

__all__ = [
    "CommandResult", "Transport", "TransportError", "TimeoutExpired",
    "LocalTransport", "SSHTransport", "SSHError", "SSHFactory",
    "IPMIClient", "IPMIError", "PowerState",
    "FakeTransport", "ScriptedResponse", "healthy_node_rules",
]
