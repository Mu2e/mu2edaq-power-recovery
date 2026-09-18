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


#: Substrings that mean the BMC *answered* and rejected our identity, as
#: opposed to not answering at all.  "Unable to establish ... session" is
#: deliberately absent: a dark BMC says exactly the same thing, and during a
#: power outage a dark BMC is the expected case.  These two are unambiguous --
#: the BMC completed enough of RMCP+ to tell us the username or password is
#: wrong.
CREDENTIAL_REJECTIONS = ("rakp", "unauthorized name")

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
                 protected: Optional[Any] = None,
                 message_timeout: Optional[int] = None,
                 tool_retries: Optional[int] = None,
                 extra_args: Optional[List[str]] = None,
                 stop_on_auth_failure: bool = True):
        self.gateway = gateway
        self.username = username
        self._password = password
        self.tool = tool
        self.interface = interface
        self.privilege = privilege
        self.cipher_suite = cipher_suite
        self.timeout = timeout
        #: Our own retry loop around the whole invocation.
        self.retries = retries
        #: ipmitool's -N and -R. None means "do not pass the flag", which
        #: leaves ipmitool's defaults in force -- see _remote_command.
        self.message_timeout = message_timeout
        self.tool_retries = tool_retries
        #: Extra ipmitool arguments from configuration, e.g. ['-e', '^'].
        self.extra_args = list(extra_args or [])
        self.dry_run = dry_run
        #: Callable(hostname) -> bool, normally Topology.is_protected.
        self.protected = protected or (lambda host: False)
        #: Stop issuing IPMI commands once a BMC has rejected the credentials.
        self.stop_on_auth_failure = stop_on_auth_failure
        #: Set when that happens: the message, for every later caller.
        self.credentials_refused: Optional[str] = None

    # -- command construction ---------------------------------------------

    def _remote_command(self, bmc_host: str, args: List[str]) -> str:
        """The shell line executed on the gateway.

        Deliberately the same invocation as the known-working
        mu2edaq-operations script::

            ipmitool -I lanplus -H <bmc> -U <user> -L Operator -C 3 <args>

        with one difference: ``-E`` instead of ``-P <password>``, so the
        password is read from the environment rather than the command line.
        ``IPMI_PASSWORD`` is exported inside the remote shell from a value that
        arrives on stdin (see :meth:`_run`).

        ``-N`` (per-message timeout) and ``-R`` (retry count) are *not* sent
        unless configured. They were, once, at ``-N 5 -R 1``, and that single
        attempt was enough to turn a BMC that needs a retry to establish its
        RMCP+ session into an outright failure. ipmitool's own defaults -- four
        retries -- are what the upstream script relies on and what works.

        ``timeout`` on the gateway still bounds the whole invocation, so a BMC
        that accepts the session and then never answers cannot hang a stage.
        """
        parts = [
            "timeout", str(self.tool_timeout()),
            self.tool,
            "-I", self.interface,
            "-H", bmc_host,
            "-U", self.username,
            "-L", self.privilege,
            "-C", str(self.cipher_suite),
            "-E",
        ]
        if self.message_timeout:
            parts += ["-N", str(self.message_timeout)]
        if self.tool_retries is not None:
            parts += ["-R", str(self.tool_retries)]
        parts += [str(a) for a in self.extra_args]
        return " ".join(shlex.quote(p) for p in parts + [str(a) for a in args])

    def tool_timeout(self) -> int:
        """Wall-clock bound applied on the gateway.

        Generous relative to the per-message timeout, because ipmitool retries
        internally: cutting it to roughly one attempt is what broke this
        before.
        """
        return max(self.timeout * 3, 20)

    def describe(self, bmc_host: str, args: List[str]) -> str:
        """The invocation as it would be run, for the operator to compare.

        Contains no secret -- the password is passed by environment -- so this
        is safe to print and to put in the report.
        """
        return self._remote_command(bmc_host, args)

    def _run(self, bmc_host: str, args: List[str]) -> CommandResult:
        """Run one ipmitool invocation on the gateway, with retries.

        The password reaches the gateway on stdin: the remote shell reads one
        line into IPMI_PASSWORD, exports it, and runs ipmitool.  `read -r`
        keeps backslashes intact, and the variable is never echoed.
        """
        if self.credentials_refused:
            # A BMC has already told us the username or password is wrong, and
            # one credential set is used for every BMC in the cluster. Carrying
            # on would put the same bad credentials to another sixty-four of
            # them, three times each, with ipmitool retrying four times inside
            # every one of those. That is the IPMI version of the refused-ssh
            # burst, and it is a lot of failed authentications to send at a
            # controller you are trying to recover.
            raise IPMIError(self.credentials_refused)

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
            if self._rejected_credentials(result):
                # Retrying a wrong username is wrong the second and third time
                # too. All it adds is two more failed authentications against
                # this BMC -- and a BMC that answered RAKP is one that is
                # counting them.
                log.error("%s rejected the IPMI credentials; not retrying",
                          bmc_host)
                break
            if attempt <= self.retries:
                log.debug("ipmi %s %s rc=%s, retrying", bmc_host, args, result.rc)
                time.sleep(1.0)
        assert last is not None
        self._annotate_failure(last, bmc_host)
        if self.stop_on_auth_failure and self._rejected_credentials(last):
            self.credentials_refused = (
                f"{bmc_host} rejected the IPMI credentials for user "
                f"{self.username!r}. The same credentials are used for every "
                f"BMC, so no further IPMI command will be issued this run. "
                f"{last.meta.get('diagnosis', '')} Re-run with ipmi.username "
                f"set, or 'mu2e-ipmi-tool --diagnose' to find the combination "
                f"that works.").strip()
            log.error("%s", self.credentials_refused)
        return last

    @staticmethod
    def _rejected_credentials(result: CommandResult) -> bool:
        """True when the BMC answered and refused the username or password."""
        text = (result.stderr + result.stdout).lower()
        return any(needle in text for needle in CREDENTIAL_REJECTIONS)

    #: Substrings of ipmitool failures that mean the session never opened, and
    #: what an operator should actually check for each.
    _DIAGNOSES = (
        ("unable to establish",
         "the BMC refused the session. In order of likelihood: the username is "
         "wrong (upstream mu2e_ipmi.sh hard-codes 'MU2E' -- compare it against "
         "vault.ipmi_user_field), the password is wrong, or this BMC wants a "
         "different cipher suite (try ipmi.cipher_suite: 17, or 0)."),
        ("rakp", "the BMC rejected the credentials during RMCP+ authentication: "
                 "the username or password is wrong."),
        ("unauthorized name",
         "the BMC does not have an account with this username."),
        ("privilege level",
         "the account exists but may not use ipmi.privilege "
         f"-- try Administrator."),
        ("no route to host",
         "the gateway cannot reach the IPMI segment; check net.ipmi_reach."),
        ("timed out",
         "the BMC did not answer. If it answers intermittently, raise "
         "ipmi.timeout or set ipmi.tool_retries."),
    )

    def _annotate_failure(self, result: CommandResult, bmc_host: str) -> None:
        """Attach a plain-language cause to a failed invocation.

        "Unable to establish IPMI v2 / RMCP+ session" is the same message for a
        wrong username, a wrong password and an unsupported cipher suite, so
        the raw output alone does not tell an operator what to change.
        """
        text = (result.stderr + result.stdout).lower()
        for needle, diagnosis in self._DIAGNOSES:
            if needle in text:
                result.meta["diagnosis"] = diagnosis
                result.meta["invocation"] = self.describe(bmc_host, [])
                log.error("%s: %s", bmc_host, diagnosis)
                return

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
