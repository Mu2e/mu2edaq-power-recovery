"""IPMI (BMC) access, executed on a gateway.

The IPMI segments -- 192.168.157.0/24 at MC-2, 192.168.150.0/24 at the
teststand -- are private and not routable from off site, so ``ipmitool`` is
never run locally.  Every command is run *on* a gateway over SSH, which is
exactly what Project-Description.md asks for ("IPMI commands should be able to
be issued from gateway01 or gateway02") and is also the only thing that works.

Credential handling
-------------------
``ipmitool -P <password>`` puts the BMC password in the gateway's process
table for every user on the machine to read.  This module uses ``-E`` instead,
which makes ipmitool read ``IPMI_PASSWORD`` from its environment, and sets
that variable from a here-document fed to the remote shell over stdin.  The
password therefore appears in no argument vector, on either host.

Safety
------
:meth:`IPMIClient.power` refuses a destructive verb for any host the topology
marks ``protected`` (gateways, mu2e-mgr-01, mu2e-dcs-01) and refuses every
state-changing verb unless the client was constructed with ``dry_run=False``.
The refusal happens before the command is built, not inside the remote shell.
"""
from __future__ import annotations

import logging
import re
import shlex
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

from .base import CommandResult, Transport, TransportError

log = logging.getLogger(__name__)


class IPMIError(TransportError):
    """The BMC could not be reached or refused the command."""


class PowerState(str, Enum):
    """Chassis power state as reported by ``chassis power status``."""

    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"
    UNREACHABLE = "unreachable"

    @classmethod
    def parse(cls, text: str) -> "PowerState":
        low = (text or "").strip().lower()
        if "chassis power is on" in low or low.endswith(" on"):
            return cls.ON
        if "chassis power is off" in low or low.endswith(" off"):
            return cls.OFF
        return cls.UNKNOWN


#: Verbs that change the machine's state.  Everything not listed is read-only
#: and is allowed in a dry run, because reading is how phase 1 works.
DESTRUCTIVE_VERBS = {"off", "cycle", "reset", "soft"}
#: 'on' changes state too, but it is the entire point of phase 2 and is not
#: destructive; it is still gated on dry_run, just not on `protected`.
STATE_CHANGING_VERBS = DESTRUCTIVE_VERBS | {"on"}


@dataclass
class SensorReading:
    """One row of ``sdr elist`` / ``sensor``: name, value, unit, status."""

    name: str
    value: str
    unit: str = ""
    status: str = "ok"

    @property
    def critical(self) -> bool:
        return self.status.lower() in ("cr", "nc", "nr", "critical", "lower critical",
                                       "upper critical", "lnr", "unr")

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "value": self.value, "unit": self.unit,
                "status": self.status, "critical": self.critical}


class IPMIClient:
    """Issues ipmitool commands for a set of BMCs, from one gateway."""

    def __init__(self, gateway: Transport,
                 username: str,
                 password: str,
                 tool: str = "ipmitool",
                 interface: str = "lanplus",
                 privilege: str = "Operator",
                 cipher_suite: int = 3,
                 timeout: int = 10,
                 retries: int = 2,
                 dry_run: bool = True,
                 protected: Optional[Any] = None):
        self.gateway = gateway
        self.username = username
        self._password = password
        self.tool = tool
        self.interface = interface
        self.privilege = privilege
        self.cipher_suite = cipher_suite
        self.timeout = timeout
        self.retries = retries
        self.dry_run = dry_run
        #: Callable(hostname) -> bool, normally Topology.is_protected.
        self.protected = protected or (lambda host: False)

    # -- command construction ---------------------------------------------

    def _remote_command(self, bmc_host: str, args: List[str]) -> str:
        """The shell line executed on the gateway.

        ``IPMI_PASSWORD`` is exported inside the remote shell (its value
        arrives via stdin, see :meth:`_run`), and ipmitool's ``-E`` reads it
        from there.  ``timeout`` bounds a BMC that accepts the TCP session and
        then never answers -- a common failure mode for a BMC that has just
        had its power restored.
        """
        parts = [
            "timeout", str(self.timeout + 5),
            self.tool,
            "-I", self.interface,
            "-H", bmc_host,
            "-U", self.username,
            "-L", self.privilege,
            "-C", str(self.cipher_suite),
            "-E",
            "-N", str(max(1, self.timeout // 2)),
            "-R", "1",
        ]
        return " ".join(shlex.quote(p) for p in parts + [str(a) for a in args])

    def _run(self, bmc_host: str, args: List[str]) -> CommandResult:
        """Run one ipmitool invocation on the gateway, with retries.

        The password reaches the gateway on stdin: the remote shell reads one
        line into IPMI_PASSWORD, exports it, and runs ipmitool.  `read -r`
        keeps backslashes intact, and the variable is never echoed.
        """
        command = self._remote_command(bmc_host, args)
        script = (
            "IFS= read -r IPMI_PASSWORD || exit 97; "
            "export IPMI_PASSWORD; "
            f"{command}"
        )
        last: Optional[CommandResult] = None
        for attempt in range(1, self.retries + 2):
            try:
                result = self.gateway.run(
                    ["/bin/sh", "-c", script],
                    timeout=self.timeout + 20,
                    input_text=self._password + "\n",
                )
            except TransportError as exc:
                raise IPMIError(f"cannot reach gateway to talk to {bmc_host}: {exc}") from exc
            result.meta.update({"bmc": bmc_host, "ipmi_args": list(args),
                                "attempt": attempt, "via": "ipmi"})
            # Scrub: the command line is stored in the run database and shown
            # in the report, and the script text contains no password, but the
            # read loop would confuse a reader.  Show the effective command.
            result.command = f"ipmitool [{bmc_host}] {' '.join(str(a) for a in args)}"
            if result.rc == 97:
                raise IPMIError("internal error: no password delivered to the gateway")
            if result.ok:
                return result
            last = result
            if attempt <= self.retries:
                log.debug("ipmi %s %s rc=%s, retrying", bmc_host, args, result.rc)
                time.sleep(1.0)
        assert last is not None
        return last

    # -- read-only operations ---------------------------------------------

    def power_status(self, bmc_host: str) -> PowerState:
        """Current chassis power state; UNREACHABLE when the BMC does not answer."""
        try:
            result = self._run(bmc_host, ["chassis", "power", "status"])
        except IPMIError:
            return PowerState.UNREACHABLE
        if not result.ok:
            return PowerState.UNREACHABLE
        return PowerState.parse(result.output)

    def sensors(self, bmc_host: str) -> List[SensorReading]:
        """Parse ``sdr elist`` into readings.

        ``sdr elist`` is pipe-delimited: name | id | status | entity | reading.
        Sensors the BMC reports as 'ns' (no reading) are dropped rather than
        reported as failures -- an unpopulated slot has no temperature.
        """
        try:
            result = self._run(bmc_host, ["sdr", "elist"])
        except IPMIError:
            return []
        readings: List[SensorReading] = []
        for line in result.lines():
            fields = [f.strip() for f in line.split("|")]
            if len(fields) < 5:
                continue
            name, _sid, status, _entity, reading = fields[0], fields[1], fields[2], fields[3], fields[4]
            if status.lower() in ("ns", "no reading", "disabled"):
                continue
            value, _, unit = reading.partition(" ")
            readings.append(SensorReading(name=name, value=value, unit=unit.strip(),
                                          status=status))
        return readings

    def sel(self, bmc_host: str, since: Optional[float] = None) -> List[str]:
        """System event log entries, newest last.

        *since* is accepted for symmetry with the checks but not used to filter
        here: BMC clocks drift badly across a power outage -- that is precisely
        when they lose time -- so filtering on the BMC's own timestamps would
        silently discard real events.  The caller compares against a baseline
        count instead.
        """
        try:
            result = self._run(bmc_host, ["sel", "list", "last", "20"])
        except IPMIError:
            return []
        return result.lines() if result.ok else []

    def fru(self, bmc_host: str) -> Dict[str, str]:
        """FRU inventory (product name, serial) -- identifies the physical box."""
        try:
            result = self._run(bmc_host, ["fru", "print"])
        except IPMIError:
            return {}
        out: Dict[str, str] = {}
        for line in result.lines():
            key, sep, value = line.partition(":")
            if sep:
                out[key.strip()] = value.strip()
        return out

    def lan_print(self, bmc_host: str) -> Dict[str, str]:
        """BMC LAN configuration -- confirms the BMC kept its address."""
        try:
            result = self._run(bmc_host, ["lan", "print"])
        except IPMIError:
            return {}
        out: Dict[str, str] = {}
        for line in result.lines():
            key, sep, value = line.partition(":")
            if sep:
                out[key.strip()] = value.strip()
        return out

    # -- state-changing operations ----------------------------------------

    def power(self, bmc_host: str, verb: str, node_host: Optional[str] = None,
              force: bool = False) -> CommandResult:
        """Issue ``chassis power <verb>``.

        Refuses, in this order:
          1. a destructive verb against a protected host, always -- ``force``
             does not override this, because the protection list exists to stop
             the operator cutting their own path into the cluster;
          2. any state-changing verb while ``dry_run`` is set.

        A refusal returns a CommandResult with a non-zero rc and an explanation
        rather than raising, so a stage can record "refused" against the node
        and carry on with the rest of the stage.
        """
        verb = verb.lower()
        target = node_host or bmc_host
        if verb in DESTRUCTIVE_VERBS and self.protected(target):
            msg = (f"refusing '{verb}' for protected host {target}: powering it "
                   "down would cut access to the cluster being recovered")
            log.error(msg)
            return CommandResult(command=f"ipmitool [{bmc_host}] chassis power {verb}",
                                 rc=77, stderr=msg, host=bmc_host,
                                 meta={"refused": True, "reason": "protected"})
        if verb in STATE_CHANGING_VERBS and self.dry_run:
            msg = f"dry run: would issue 'chassis power {verb}' to {bmc_host}"
            log.info(msg)
            return CommandResult(command=f"ipmitool [{bmc_host}] chassis power {verb}",
                                 rc=0, stdout=msg, host=bmc_host,
                                 meta={"dry_run": True, "would_run": verb})
        log.warning("IPMI %s -> %s (%s)", verb, bmc_host, target)
        return self._run(bmc_host, ["chassis", "power", verb])

    def power_on(self, bmc_host: str, node_host: Optional[str] = None) -> CommandResult:
        return self.power(bmc_host, "on", node_host=node_host)

    def ensure_on(self, bmc_host: str, node_host: Optional[str] = None) -> Dict[str, Any]:
        """Bring a chassis to the ON state, reporting what had to be done.

        Returns a dict with the state before, the action taken ('none',
        'power_on', 'refused', 'unreachable') and the state after.  Phase 2
        records this verbatim, which is what lets the final report say which
        machines actually had to be switched on versus which were already up.
        """
        before = self.power_status(bmc_host)
        if before is PowerState.UNREACHABLE:
            return {"before": before.value, "action": "unreachable",
                    "after": before.value, "ok": False,
                    "detail": f"BMC {bmc_host} did not answer"}
        if before is PowerState.ON:
            return {"before": before.value, "action": "none", "after": before.value,
                    "ok": True, "detail": "already powered on"}

        result = self.power_on(bmc_host, node_host=node_host)
        if result.meta.get("dry_run"):
            return {"before": before.value, "action": "dry_run", "after": before.value,
                    "ok": True, "detail": result.stdout}
        if not result.ok:
            return {"before": before.value, "action": "failed", "after": before.value,
                    "ok": False, "detail": result.stderr.strip() or result.stdout.strip()}
        after = self.power_status(bmc_host)
        return {"before": before.value, "action": "power_on", "after": after.value,
                "ok": after is PowerState.ON,
                "detail": f"issued chassis power on; now {after.value}"}
