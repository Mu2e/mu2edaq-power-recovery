"""A scripted transport, for pytest and for ``--simulate``.

Every check in this package is a pure function of a :class:`Transport`, which
is only useful if there is a transport that answers without a cluster.  This
one matches each command against an ordered list of regular expressions and
returns the first scripted response, recording everything it was asked to run.

It is also what ``--simulate`` uses, so an operator can rehearse the whole
four-phase run -- including the report pages -- on a laptop, weeks before the
outage, and see exactly which commands would be issued.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Pattern, Sequence, Union

from .base import Command, CommandResult, Transport, TransportError, as_string

Responder = Union[str, "ScriptedResponse", Callable[[str], "ScriptedResponse"]]


@dataclass
class ScriptedResponse:
    """What a matched command should return."""

    stdout: str = ""
    stderr: str = ""
    rc: int = 0
    #: Seconds of simulated latency, so concurrency behaviour can be exercised.
    delay: float = 0.0
    #: Raise TransportError instead of returning -- the "host unreachable" case.
    raises: Optional[str] = None
    #: Consume this response once, then fall through to later rules.  Lets a
    #: test script "first call fails, second succeeds" for the reboot path.
    once: bool = False


@dataclass
class _Rule:
    pattern: Pattern
    response: Responder
    host: Optional[str] = None
    used: bool = False


class FakeTransport(Transport):
    """Answers commands from a script; records every call.

    Rules are tried in order and the first whose pattern searches the rendered
    command (and whose host matches, when given) wins.  With no rule matching,
    the transport returns ``default``, which defaults to a non-zero exit -- an
    unmatched command must not look like a success, or a test will pass because
    the check never really ran.
    """

    def __init__(self, host: str = "fake",
                 rules: Optional[Sequence[Any]] = None,
                 default: Optional[ScriptedResponse] = None):
        self.host = host
        self._rules: List[_Rule] = []
        self.calls: List[Dict[str, Any]] = []
        self.default = default if default is not None else ScriptedResponse(
            rc=127, stderr="fake transport: no rule matched")
        for rule in rules or []:
            if isinstance(rule, _Rule):
                self._rules.append(rule)
            else:
                self.expect(*rule) if isinstance(rule, tuple) else None

    # -- scripting ---------------------------------------------------------

    def expect(self, pattern: str, response: Responder,
               host: Optional[str] = None) -> "FakeTransport":
        """Add a rule; returns self so rules can be chained."""
        if isinstance(response, str):
            response = ScriptedResponse(stdout=response)
        self._rules.append(_Rule(re.compile(pattern), response, host))
        return self

    def expect_first(self, pattern: str, response: Responder,
                     host: Optional[str] = None) -> "FakeTransport":
        """Add a rule ahead of the existing ones.

        The normal way to inject a fault into an otherwise healthy node: the
        baseline rule set stays intact and one command starts answering
        differently.  Rules are shared with any clones taken from this
        transport, so a fault added here applies to every host in a simulated
        sweep as well.
        """
        if isinstance(response, str):
            response = ScriptedResponse(stdout=response)
        self._rules.insert(0, _Rule(re.compile(pattern), response, host))
        return self

    def clone(self, host: str) -> "FakeTransport":
        """A transport for another host sharing this one's rules and call log.

        The factory hands one of these out per node; sharing the call log keeps
        a simulated run's full command history in one place.
        """
        other = FakeTransport(host=host, default=self.default)
        other._rules = self._rules
        other.calls = self.calls
        return other

    # -- Transport ---------------------------------------------------------

    def run(self, command: Command, timeout: Optional[float] = None,
            user: Optional[str] = None, input_text: Optional[str] = None,
            check: bool = False) -> CommandResult:
        rendered = as_string(command)
        self.calls.append({"host": self.host, "command": rendered, "user": user,
                           "stdin": bool(input_text), "timeout": timeout})

        response = self.default
        for rule in self._rules:
            if rule.used:
                continue
            if rule.host is not None and rule.host != self.host:
                continue
            if rule.pattern.search(rendered):
                candidate = rule.response
                if callable(candidate) and not isinstance(candidate, ScriptedResponse):
                    candidate = candidate(rendered)
                response = candidate
                if getattr(candidate, "once", False):
                    rule.used = True
                break

        if response.delay:
            time.sleep(response.delay)
        if response.raises:
            raise TransportError(f"{self.host}: {response.raises}")

        result = CommandResult(command=rendered, rc=response.rc,
                               stdout=response.stdout, stderr=response.stderr,
                               host=self.host, duration=response.delay,
                               meta={"via": "fake"})
        if check and not result.ok:
            raise TransportError(f"{self.host}: {rendered} exited {result.rc}")
        return result

    # -- assertions for tests ---------------------------------------------

    def ran(self, pattern: str, host: Optional[str] = None) -> bool:
        rx = re.compile(pattern)
        return any(rx.search(c["command"]) and (host is None or c["host"] == host)
                   for c in self.calls)

    def commands(self, host: Optional[str] = None) -> List[str]:
        return [c["command"] for c in self.calls if host is None or c["host"] == host]


def _mesh_script_response(command: str) -> ScriptedResponse:
    """Answer a phase-3 mesh probe script.

    The probe packs every target for one source into a single shell script and
    splits the output back apart on ``===BEGIN <target>===`` markers.  A flat
    canned ping summary would therefore parse as "nothing answered", so this
    reconstructs the marker blocks from the script it was handed -- which also
    means a simulated phase 3 exercises the real splitter, not a shortcut past
    it.
    """
    targets = re.findall(r"===BEGIN (\S+)===", command)
    ping_ok = ("3 packets transmitted, 3 received, 0% packet loss, time 2003ms\n"
               "rtt min/avg/max/mdev = 0.112/0.147/0.201/0.031 ms")
    mtu_ok = ("1 packets transmitted, 1 received, 0% packet loss, time 0ms\n"
              "rtt min/avg/max/mdev = 0.210/0.210/0.210/0.000 ms")
    parts = []
    for target in targets:
        parts.append(f"===BEGIN {target}===")
        parts.append(ping_ok)
        if f"---MTU {target}---" in command:
            parts.append(f"---MTU {target}---")
            parts.append(mtu_ok)
        parts.append(f"===END {target}===")
    return ScriptedResponse(stdout="\n".join(parts))


def healthy_node_rules() -> List[tuple]:
    """A rule set describing a fully healthy MC-2 node.

    Used as the baseline for ``--simulate`` and as the starting point for
    tests, which then override individual rules to inject a fault.  The output
    strings are the real formats the checks parse, not placeholders: a
    simulation that exercised a different parser than production would be
    worse than no simulation.

    Order matters -- the first matching rule wins -- so the specific patterns
    come before the general ones (``cat /proc/uptime`` before ``uptime``).

    The address list deliberately carries an interface on every DAQ subnet at
    once, across all three sites.  No real node looks like that, but one rule
    set has to satisfy checks for gateways, MC-2 nodes and teststand nodes
    alike, and the alternative -- a per-class script -- would mean the
    simulation and the topology could disagree about which node is which.
    """
    return [
        # --- trivial / identity -------------------------------------------
        # First: a mesh probe script, which must be answered per target.
        (r"===BEGIN ", _mesh_script_response),
        (r"^true$", ScriptedResponse()),
        # Before the bare id rules: `su - user -c 'pwd && id -un'` contains
        # "id -un" in its -c argument and would otherwise match that instead.
        (r"\bsu\s+-\s", ScriptedResponse(stdout="/home/mu2edaq\nmu2edaq")),
        (r"\bid -un\b", ScriptedResponse(stdout="root")),
        (r"\bid -u\b", ScriptedResponse(stdout="0")),

        # --- host ----------------------------------------------------------
        (r"cat /proc/uptime", ScriptedResponse(stdout="252.31 3921.55")),
        (r"\buptime\b",
         ScriptedResponse(stdout=" 10:42:01 up 4 min,  1 user,  "
                                 "load average: 0.31, 0.22, 0.09")),
        (r"\bnproc\b", ScriptedResponse(stdout="32")),
        (r"uname -r", ScriptedResponse(stdout="5.14.0-503.el9.x86_64")),
        (r"/proc/sys/kernel/tainted", ScriptedResponse(stdout="0")),

        # --- disks ----------------------------------------------------------
        (r"\bdf\b", ScriptedResponse(stdout=(
            "Filesystem                 Type  1024-blocks      Used Available Capacity Mounted on\n"
            "/dev/mapper/rhel-root      xfs      52403200  18321408  34081792      35% /\n"
            "/dev/sda1                  xfs       1038336    329216    709120      32% /boot\n"
            "/dev/mapper/rhel-scratch   xfs     943718400 372834304 570884096      40% /scratch\n"
            "mu2e-mgr-01:/home          nfs4   2147483648 429496730 1717986918     20% /home\n"
            "mu2e-mgr-01:/daqlogs       nfs4   1073741824 107374182  966367642     10% /daqlogs"))),
        (r"\bmountpoint\b", ScriptedResponse()),
        (r"\bls\b.*/(scratch|mu2e|home|daqlogs)", ScriptedResponse(stdout="data\nlogs")),
        (r"ls /dev/mu2e", ScriptedResponse(stdout="/dev/mu2e0\n/dev/mu2e1")),
        (r"lsblk", ScriptedResponse(stdout="sda      disk\nnvme0n1  disk")),
        (r"smartctl", ScriptedResponse(
            stdout="SMART overall-health self-assessment test result: PASSED")),
        (r"/proc/mdstat", ScriptedResponse(stdout=(
            "Personalities : [raid1]\n"
            "md0 : active raid1 sda1[0] sdb1[1]\n"
            "      1048512 blocks [2/2] [UU]\n"))),
        (r"journalctl|dmesg", ScriptedResponse(stdout="")),
        (r"exportfs|showmount|/proc/fs/nfsd/exports",
         ScriptedResponse(stdout="/home       *\n/daqlogs    *")),

        # --- network --------------------------------------------------------
        (r"ip -o -4 addr", ScriptedResponse(stdout=(
            "1: lo    inet 127.0.0.1/8 scope host lo\n"
            "2: eno1  inet 131.225.245.51/24 brd 131.225.245.255 scope global eno1\n"
            "3: eno2  inet 131.225.237.51/24 brd 131.225.237.255 scope global eno2\n"
            "4: eno3  inet 131.225.246.51/24 brd 131.225.246.255 scope global eno3\n"
            "5: ens1f0 inet 10.226.9.51/24 brd 10.226.9.255 scope global ens1f0\n"
            "6: ens2f0 inet 192.168.157.51/24 brd 192.168.157.255 scope global ens2f0\n"
            "7: ens2f1 inet 192.168.150.51/24 brd 192.168.150.255 scope global ens2f1"))),
        (r"ip -o link", ScriptedResponse(stdout=(
            "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 state UNKNOWN\n"
            "2: eno1: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 state UP\n"
            "3: eno2: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 state UP\n"
            "4: eno3: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 state UP\n"
            "5: ens1f0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 9000 state UP\n"
            "6: ens2f0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 state UP\n"
            "7: ens2f1: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 state UP"))),
        (r"ip -o route", ScriptedResponse(stdout=(
            "default via 131.225.245.1 dev eno1\n"
            "131.225.245.0/24 dev eno1 scope link\n"
            "131.225.237.0/24 dev eno2 scope link\n"
            "131.225.246.0/24 dev eno3 scope link\n"
            "10.226.9.0/24 dev ens1f0 scope link\n"
            "192.168.157.0/24 dev ens2f0 scope link\n"
            "192.168.150.0/24 dev ens2f1 scope link"))),
        (r"/sys/class/net/.*/speed", ScriptedResponse(stdout="10000")),
        (r"ip_forward", ScriptedResponse(stdout="1")),
        (r"\bping\b", ScriptedResponse(stdout=(
            "3 packets transmitted, 3 received, 0% packet loss, time 2003ms\n"
            "rtt min/avg/max/mdev = 0.112/0.147/0.201/0.031 ms"))),
        (r"getent hosts|nslookup|\bhost\b",
         ScriptedResponse(stdout="131.225.245.51  mu2e-node.fnal.gov")),
        (r"\bnft\b|\biptables\b", ScriptedResponse(stdout=(
            "table inet filter {\n"
            "  chain input { type filter hook input priority 0; policy drop; }\n"
            "  chain forward { type filter hook forward priority 0; policy accept; }\n"
            "}"))),

        # --- services and PCIe ----------------------------------------------
        (r"systemctl is-active", ScriptedResponse(stdout="active")),
        (r"pgrep|ps -C", ScriptedResponse(stdout="2417")),
        (r"lspci|xilinx", ScriptedResponse(stdout=(
            "03:00.0 Signal processing controller: Xilinx Corporation Device 7038"))),
        (r"lsmod", ScriptedResponse(stdout="mu2e                   61440  0")),
        (r"puppet", ScriptedResponse(stdout="Currently applying a catalog: false")),

        # --- IPMI (matched inside the gateway shell wrapper) -----------------
        (r"chassis power status", ScriptedResponse(stdout="Chassis Power is on")),
        (r"chassis power on", ScriptedResponse(stdout="Chassis Power Control: Up/On")),
        (r"sdr elist", ScriptedResponse(stdout=(
            "CPU1 Temp        | 01h | ok  |  3.1 | 41 degrees C\n"
            "CPU2 Temp        | 02h | ok  |  3.2 | 39 degrees C\n"
            "FAN1             | 41h | ok  | 29.1 | 5200 RPM\n"
            "PSU1 Status      | 70h | ok  | 10.1 | Presence detected"))),
        (r"sel list", ScriptedResponse(stdout="")),
        (r"fru print", ScriptedResponse(stdout=("Product Name          : SYS-1029U\n"
                                                "Product Serial        : S123456"))),
        (r"lan print", ScriptedResponse(stdout=("IP Address            : 192.168.157.51\n"
                                                "MAC Address           : 0c:c4:7a:00:00:01"))),
    ]
