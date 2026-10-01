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

Credential circuit breaker
--------------------------
One BMC account serves every controller in the cluster, so one rejection means
every later attempt will be rejected too, and each one counts towards the
BMC's lockout. :class:`CredentialBreaker` holds the run's answer to "has this
credential been refused?" and is shared by every :class:`IPMIClient` of the
run. Until a first successful invocation proves the credential, it admits
:data:`AUTH_PROBE_CONCURRENCY` (one) invocation at a time; once proven, calls
run concurrently; once refused, no further invocation is made and every caller
receives the same diagnosis as :class:`IPMICredentialsRefused`.

Two refinements keep the gate from costing more than it saves:

* **Reachability pre-check.** While the credential is unproven, each
  invocation is preceded by one ``ping -c 1 -W 1 <bmc>`` from the same
  gateway, outside the gate. A BMC that does not answer is reported
  UNREACHABLE at once, without ipmitool and without queueing behind the
  gate -- after an outage most BMCs may be dark, and serialising a full
  ipmitool timeout for each one would stall the whole assessment.
  ``ipmi.reachability_precheck: false`` turns it off for BMCs that filter
  ICMP.
* **"Unable to establish" from a live BMC.** That message alone cannot be
  read as a refusal -- a dark BMC says it too. But from a BMC that has just
  answered the ping, it is most likely a wrong username (see
  ``_DIAGNOSES``). :data:`ESTABLISH_FAILURE_LIMIT` distinct BMCs doing so
  while the credential is unproven trip the breaker. RAKP / "unauthorized
  name" still trip it on the first occurrence.

A credential refusal is *not* the protected-host refusal. That one is a
deliberate decision of this module (``meta["reason"] == "protected"``); this
one is the BMC telling us we could not look, and the checks report it as
UNKNOWN.
"""
from __future__ import annotations

import logging
import re
import shlex
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

from .base import CommandResult, Transport, TransportError

log = logging.getLogger(__name__)


class IPMIError(TransportError):
    """The BMC could not be reached or refused the command."""


class IPMICredentialsRefused(IPMIError):
    """No invocation was made: the run's IPMI credential has been refused.

    Raised to every caller once :class:`CredentialBreaker` has tripped, with
    the shared diagnosis as its message, so that "the credential is wrong" is
    never read as "the BMC does not answer".
    """


class IPMIUnreachable(IPMIError):
    """No invocation was made: the BMC did not answer the reachability ping."""


class PowerState(str, Enum):
    """Chassis power state as reported by ``chassis power status``."""

    ON = "on"
    OFF = "off"
    UNKNOWN = "unknown"
    UNREACHABLE = "unreachable"
    #: The BMC rejected the credential, or an earlier BMC did and the breaker
    #: stopped this one being asked. Either way the state was not read.
    REFUSED = "refused"

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

#: Authentication-sensitive invocations admitted at once while the credential
#: is unproven. One: a wrong credential then reaches exactly one BMC (with that
#: invocation's own ipmitool retries) before the breaker trips. Deliberately a
#: constant, not a configuration key -- there is no safe reason to raise it.
AUTH_PROBE_CONCURRENCY = 1

#: Distinct BMCs that answered the reachability pre-check and then failed with
#: "Unable to establish ... session" while the credential was unproven, before
#: the breaker trips. Two, not one: a single BMC wanting another cipher suite
#: must not stop the run, but two live BMCs refusing the session to a
#: credential no BMC has accepted is the wrong-username signature.
ESTABLISH_FAILURE_LIMIT = 2

#: The ipmitool text that means the RMCP+ session never opened.
#: ipmitool's message when the BMC name does not resolve on the gateway.
UNRESOLVED = "address lookup for"
UNESTABLISHED = "unable to establish"

#: Rows ``sel()`` asks for. The baseline comparison in power.sel needs to know
#: when a reading came back full, because a full tail may have lost rows.
SEL_TAIL = 20


class CredentialBreaker:
    """Shared "has the BMC credential been refused?" state.

    One instance is shared by every :class:`IPMIClient` that uses the same BMC
    account -- in practice the whole run, whichever gateway each client runs
    ipmitool on. All state changes happen under ``lock``:

    * ``gate`` admits :data:`AUTH_PROBE_CONCURRENCY` invocation at a time
      until ``proven`` is set;
    * ``proven`` is set by the first successful invocation, after which the
      gate is not taken and calls run concurrently;
    * ``refused`` holds the diagnosis once a BMC has rejected the credential;
      it is set once and never cleared.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.gate = threading.BoundedSemaphore(AUTH_PROBE_CONCURRENCY)
        self.proven = threading.Event()
        self._refused: Optional[str] = None
        #: BMCs that answered the pre-check and still would not open a session.
        self._unestablished: set = set()

    @property
    def refused(self) -> Optional[str]:
        with self.lock:
            return self._refused

    def refuse(self, message: str) -> str:
        """Trip the breaker; the first message wins. Returns the one in force."""
        with self.lock:
            if self._refused is None:
                self._refused = message
            return self._refused

    def record_unestablished(self, bmc_host: str) -> int:
        """Count a live BMC that would not open a session; return the count."""
        with self.lock:
            self._unestablished.add(bmc_host)
            return len(self._unestablished)

    def prove(self) -> None:
        """Record that the credential has worked against a BMC."""
        self.proven.set()

    def check(self) -> None:
        """Raise the shared diagnosis if the breaker has tripped."""
        message = self.refused
        if message:
            raise IPMICredentialsRefused(message)


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
                 stop_on_auth_failure: bool = True,
                 breaker: Optional[CredentialBreaker] = None,
                 reachability_precheck: bool = True):
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
        #: False also bypasses the breaker's gate: calls are neither
        #: serialised nor stopped.
        self.stop_on_auth_failure = stop_on_auth_failure
        #: Shared with every other client using the same BMC account.
        self.breaker = breaker if breaker is not None else CredentialBreaker()
        #: Ping each BMC from the gateway before an unproven invocation.
        self.reachability_precheck = reachability_precheck
        #: bmc -> why power_status() found it UNREACHABLE: "unresolved" (no
        #: DNS on the gateway), "no_session" (answers ping, no IPMI session),
        #: "dark" (answers nothing) or "gateway" (the gateway itself failed).
        self.unreachable_reason: Dict[str, str] = {}

    @property
    def credentials_refused(self) -> Optional[str]:
        """The shared diagnosis once a BMC has rejected the credentials."""
        if not self.stop_on_auth_failure:
            return None
        return self.breaker.refused

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

        With ``stop_on_auth_failure`` set, the call goes through the shared
        :class:`CredentialBreaker`: refused already -> raise without invoking
        anything; credential not yet proven -> wait for the gate, then look
        again, because the call that held it may just have been rejected.
        """
        if not self.stop_on_auth_failure:
            return self._invoke(bmc_host, args)

        breaker = self.breaker
        # A BMC has already told us the username or password is wrong, and
        # one credential set is used for every BMC in the cluster. Carrying on
        # would put the same bad credentials to another sixty-four of them,
        # three times each, with ipmitool retrying four times inside every one
        # of those. That is the IPMI version of the refused-ssh burst, and it
        # is a lot of failed authentications to send at a controller you are
        # trying to recover.
        breaker.check()
        if breaker.proven.is_set():
            return self._invoke(bmc_host, args)

        # Unproven. First, outside the gate: does the BMC answer at all? A
        # dark one gets no ipmitool invocation and does not queue behind the
        # gate for a full ipmitool timeout.
        answered = False
        if self.reachability_precheck:
            answered = self._precheck(bmc_host)

        # One authentication attempt at a time, so a wrong credential reaches
        # one BMC, not max_sessions of them at once.
        breaker.gate.acquire()
        try:
            # Look again: the call that held the gate may have just been
            # rejected -- or have just proven the credential, in which case
            # this one need not hold everyone else up.
            breaker.check()
            if not breaker.proven.is_set():
                return self._invoke(bmc_host, args, answered_ping=answered)
        finally:
            breaker.gate.release()
        return self._invoke(bmc_host, args)

    def _ping_command(self, bmc_host: str) -> str:
        """One echo request, one second's wait, in the gateway's dialect."""
        target = shlex.quote(bmc_host)
        platform = getattr(self.gateway, "platform", "linux")
        if platform.startswith("win") or platform == "cygwin":
            return f"ping -n 1 -w 1000 {target}"
        # BSD/macOS ping reads -W as milliseconds; iputils as seconds.
        wait = 1 if platform.startswith("linux") else 1000
        return f"ping -c 1 -W {wait} -q {target}"

    def _precheck(self, bmc_host: str) -> bool:
        """Ping *bmc_host* from the gateway; raise IPMIUnreachable if silent.

        Returns True when the BMC answered, False when the pre-check could not
        be made (no ping on the gateway: rc 126/127), in which case the
        invocation proceeds as it would without one. Takes no gate: nothing
        here authenticates.
        """
        try:
            result = self.gateway.run(self._ping_command(bmc_host), timeout=10)
        except TransportError as exc:
            raise IPMIError(f"cannot reach gateway to talk to {bmc_host}: {exc}") from exc
        if result.ok:
            return True
        if result.rc in (126, 127):
            log.warning("reachability pre-check unavailable on %s (rc=%s); "
                        "asking %s directly", self.gateway.host, result.rc,
                        bmc_host)
            return False
        log.info("%s does not answer ping from %s; not invoking ipmitool",
                 bmc_host, self.gateway.host)
        raise IPMIUnreachable(f"BMC {bmc_host} does not answer ping from "
                              f"{self.gateway.host}")

    def _invoke(self, bmc_host: str, args: List[str],
                answered_ping: bool = False) -> CommandResult:
        """The retry loop around one ipmitool invocation.

        *answered_ping* says this invocation was made under the gate, with the
        credential unproven, to a BMC that answered the pre-check -- the
        condition under which "Unable to establish" counts towards
        :data:`ESTABLISH_FAILURE_LIMIT`.
        """
        guarded = self.stop_on_auth_failure
        command = self._remote_command(bmc_host, args)
        script = (
            "IFS= read -r IPMI_PASSWORD || exit 97; "
            "export IPMI_PASSWORD; "
            f"{command}"
        )
        last: Optional[CommandResult] = None
        for attempt in range(1, self.retries + 2):
            if guarded and attempt > 1:
                # Once proven, calls run concurrently, so another thread (or
                # another client sharing the breaker) may have been refused
                # while this one slept.
                self.breaker.check()
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
                if guarded:
                    self.breaker.prove()
                return result
            last = result
            if UNRESOLVED in (result.stderr + result.stdout).lower():
                # A name that does not resolve will not resolve on retry.
                break
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
        if self._rejected_credentials(last):
            last.meta["credentials_refused"] = True
            if guarded:
                message = self.breaker.refuse(
                    f"{bmc_host} rejected the IPMI credentials for user "
                    f"{self.username!r}. The same credentials are used for "
                    f"every BMC, so no further IPMI command will be issued this "
                    f"run. {last.meta.get('diagnosis', '')} Re-run with "
                    f"ipmi.username set, or 'mu2e-ipmi-tool --diagnose' to find "
                    f"the combination that works.".strip())
                log.error("%s", message)
        elif guarded and answered_ping and not self.breaker.proven.is_set() \
                and UNESTABLISHED in (last.stderr + last.stdout).lower() \
                and UNRESOLVED not in (last.stderr + last.stdout).lower():
            last.meta["answered_ping"] = True
            count = self.breaker.record_unestablished(bmc_host)
            if count >= ESTABLISH_FAILURE_LIMIT:
                last.meta["credentials_refused"] = True
                message = self.breaker.refuse(
                    f"{count} BMCs, most recently {bmc_host}, answered ping but "
                    f"would not open an IPMI session for user "
                    f"{self.username!r}, and no BMC has accepted it this run. "
                    f"The likeliest cause is a wrong username; no further IPMI "
                    f"command will be issued this run. "
                    f"{last.meta.get('diagnosis', '')} Re-run with "
                    f"ipmi.username set, or 'mu2e-ipmi-tool --diagnose' to find "
                    f"the combination that works.".strip())
                log.error("%s", message)
        return last

    @staticmethod
    def _rejected_credentials(result: CommandResult) -> bool:
        """True when the BMC answered and refused the username or password."""
        text = (result.stderr + result.stdout).lower()
        return any(needle in text for needle in CREDENTIAL_REJECTIONS)

    #: Substrings of ipmitool failures that mean the session never opened, and
    #: what an operator should actually check for each.
    _DIAGNOSES = (
        # Before "unable to establish": ipmitool prints that too after a failed
        # lookup, which read as a refused credential on the live teststand.
        ("address lookup for",
         "the BMC name does not resolve on the gateway: fix the topology "
         "(an entry for a host that no longer exists) or DNS. No session was "
         "attempted, so this says nothing about the credentials."),
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
        """Current chassis power state.

        UNREACHABLE when the BMC does not answer; REFUSED when it (or, through
        the breaker, an earlier BMC) rejected the credential -- the BMC may be
        perfectly healthy, we just could not look.
        """
        try:
            result = self._run(bmc_host, ["chassis", "power", "status"])
        except IPMICredentialsRefused:
            return PowerState.REFUSED
        except IPMIUnreachable:
            self.unreachable_reason[bmc_host] = "dark"
            return PowerState.UNREACHABLE
        except IPMIError:
            self.unreachable_reason[bmc_host] = "gateway"
            return PowerState.UNREACHABLE
        if not result.ok:
            if result.meta.get("credentials_refused"):
                return PowerState.REFUSED
            self.unreachable_reason[bmc_host] = self._why_unreachable(bmc_host, result)
            return PowerState.UNREACHABLE
        return PowerState.parse(result.output)

    def _why_unreachable(self, bmc_host: str, result: CommandResult) -> str:
        """Tell a missing name and a BMC that will not talk from a dark one.

        All three end in ipmitool's "Unable to establish" line. On the live
        teststand they were all reported as "does not answer ... no standby
        power", which is true only of the last. One ping from the gateway, on
        failure only, separates a BMC that answers but refuses a session.
        """
        text = (result.stderr + result.stdout).lower()
        if UNRESOLVED in text:
            return "unresolved"
        if result.meta.get("answered_ping"):
            return "no_session"
        if UNESTABLISHED in text and self.reachability_precheck:
            try:
                if self._precheck(bmc_host):
                    return "no_session"
            except IPMIError:
                pass
        return "dark"

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

    def sel(self, bmc_host: str, since: Optional[float] = None) -> Optional[List[str]]:
        """The last :data:`SEL_TAIL` lines of ``sel list``, newest last.

        Returns None when the log could not be read -- distinct from ``[]``,
        an empty log -- so a failed read is never taken for a clean one, and
        never recorded as a baseline.

        *since* is accepted for symmetry with the checks but not used to filter
        here: BMC clocks drift badly across a power outage -- that is precisely
        when they lose time -- so filtering on the BMC's own timestamps would
        silently discard real events.  The caller compares record identities
        against a baseline instead (``parsers.diff_sel``).
        """
        try:
            result = self._run(bmc_host, ["sel", "list", "last", str(SEL_TAIL)])
        except IPMIError:
            return None
        return result.lines() if result.ok else None

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
        'power_on', 'dry_run', 'failed', 'unreachable', 'credentials_refused')
        and the state after.  Phase 2
        records this verbatim, which is what lets the final report say which
        machines actually had to be switched on versus which were already up.
        """
        before = self.power_status(bmc_host)
        if before is PowerState.REFUSED:
            # Not a deliberate refusal (that is meta reason 'protected', from
            # power()) and not a dark BMC: the credential was rejected, so the
            # state is unknown and nothing was attempted.
            return {"before": before.value, "action": "credentials_refused",
                    "after": before.value, "ok": False,
                    "detail": self.credentials_refused or
                    f"BMC {bmc_host} rejected the IPMI credentials"}
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
