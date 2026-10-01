"""Parsers for the command output the checks read.

Kept apart from the checks themselves so that each format has exactly one
parser with its own tests -- the failure mode these guard against is a check
that silently passes because it misread the output it was given.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# df
# ---------------------------------------------------------------------------


@dataclass
class Filesystem:
    """One row of ``df -lP -T``."""

    source: str
    fstype: str
    size_kb: int
    used_kb: int
    available_kb: int
    use_pct: int
    mountpoint: str

    @property
    def is_network(self) -> bool:
        return self.fstype.startswith(("nfs", "cifs", "smb", "fuse.sshfs"))

    def as_dict(self) -> Dict[str, Any]:
        return {"source": self.source, "fstype": self.fstype,
                "size_kb": self.size_kb, "used_kb": self.used_kb,
                "available_kb": self.available_kb, "use_pct": self.use_pct,
                "mountpoint": self.mountpoint}


def parse_df(text: str) -> List[Filesystem]:
    """Parse ``df -lP -T`` output.

    POSIX (-P) output keeps each entry on one line even for long device names,
    which is why the checks always pass -P; without it a wrapped line would be
    silently dropped here.
    """
    rows: List[Filesystem] = []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 7:
            continue
        try:
            rows.append(Filesystem(
                source=fields[0],
                fstype=fields[1],
                size_kb=int(fields[2]),
                used_kb=int(fields[3]),
                available_kb=int(fields[4]),
                use_pct=int(fields[5].rstrip("%")),
                mountpoint=" ".join(fields[6:]),
            ))
        except ValueError:
            continue
    return rows


# ---------------------------------------------------------------------------
# ip
# ---------------------------------------------------------------------------


@dataclass
class Interface:
    """One network interface, merged from ``ip -o link`` and ``ip -o -4 addr``."""

    name: str
    state: str = "UNKNOWN"
    flags: List[str] = None            # type: ignore[assignment]
    mtu: Optional[int] = None
    addresses: List[str] = None        # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.flags = self.flags or []
        self.addresses = self.addresses or []

    @property
    def up(self) -> bool:
        # A NIC with a cable in it reports both UP (admin) and LOWER_UP
        # (carrier).  Requiring only 'state UP' would pass an interface that
        # was configured but has no link -- the exact condition a post-outage
        # check has to catch.
        return "UP" in self.flags and "LOWER_UP" in self.flags

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "state": self.state, "flags": list(self.flags),
                "mtu": self.mtu, "addresses": list(self.addresses), "up": self.up}


_LINK_RE = re.compile(r"^\d+:\s+([^:@]+)[@:]?\S*:\s+<([^>]*)>(.*)$")
_ADDR_RE = re.compile(r"^\d+:\s+(\S+)\s+inet\s+(\S+)")


def parse_ip_link(text: str) -> Dict[str, Interface]:
    out: Dict[str, Interface] = {}
    for line in text.splitlines():
        m = _LINK_RE.match(line.strip())
        if not m:
            continue
        name = m.group(1).strip()
        flags = [f.strip() for f in m.group(2).split(",") if f.strip()]
        rest = m.group(3)
        mtu = None
        mtu_m = re.search(r"\bmtu\s+(\d+)", rest)
        if mtu_m:
            mtu = int(mtu_m.group(1))
        state_m = re.search(r"\bstate\s+(\S+)", rest)
        out[name] = Interface(name=name, flags=flags, mtu=mtu,
                              state=state_m.group(1) if state_m else "UNKNOWN")
    return out


def parse_ip_addr(text: str, interfaces: Optional[Dict[str, Interface]] = None
                  ) -> Dict[str, Interface]:
    """Merge ``ip -o -4 addr`` into (or into a fresh copy of) *interfaces*."""
    out = dict(interfaces or {})
    for line in text.splitlines():
        m = _ADDR_RE.match(line.strip())
        if not m:
            continue
        name, cidr = m.group(1), m.group(2)
        iface = out.get(name) or Interface(name=name)
        iface.addresses.append(cidr)
        out[name] = iface
    return out


def address_in_subnet(address: str, cidr: str) -> bool:
    """True when *address* (dotted quad, optionally /len) lies in *cidr*.

    Implemented with the stdlib ipaddress module; a malformed value returns
    False rather than raising, because the strings come from remote output.
    """
    import ipaddress
    try:
        addr = ipaddress.ip_address(address.split("/")[0])
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False
    return addr in net


# ---------------------------------------------------------------------------
# ping
# ---------------------------------------------------------------------------


@dataclass
class PingResult:
    transmitted: int = 0
    received: int = 0
    loss_pct: float = 100.0
    rtt_avg_ms: Optional[float] = None

    @property
    def alive(self) -> bool:
        return self.received > 0

    def as_dict(self) -> Dict[str, Any]:
        return {"transmitted": self.transmitted, "received": self.received,
                "loss_pct": self.loss_pct, "rtt_avg_ms": self.rtt_avg_ms}


_PING_STATS = re.compile(r"(\d+) packets transmitted,\s*(\d+)\s*(?:packets\s*)?received")
_PING_LOSS = re.compile(r"([\d.]+)% (?:packet )?loss")
_PING_RTT = re.compile(r"(?:rtt|round-trip) min/avg/max(?:/mdev|/stddev)?\s*=\s*"
                       r"[\d.]+/([\d.]+)/")
# Windows ping shares no wording with either Unix ping, and the gateways are
# the one class probed from the operator's workstation -- which the install
# docs say may be Windows 11.  Without these an operator driving the recovery
# from Windows would be told both gateways are dead.
_PING_STATS_WIN = re.compile(r"Sent\s*=\s*(\d+),\s*Received\s*=\s*(\d+)")
_PING_RTT_WIN = re.compile(r"Average\s*=\s*(\d+)\s*ms")


def parse_ping(text: str) -> PingResult:
    """Parse the summary block of iputils, BSD or Windows ping."""
    res = PingResult()
    stats = _PING_STATS.search(text) or _PING_STATS_WIN.search(text)
    if stats:
        res.transmitted = int(stats.group(1))
        res.received = int(stats.group(2))
    loss = _PING_LOSS.search(text)
    if loss:
        res.loss_pct = float(loss.group(1))
    elif res.transmitted:
        res.loss_pct = 100.0 * (res.transmitted - res.received) / res.transmitted
    rtt = _PING_RTT.search(text) or _PING_RTT_WIN.search(text)
    if rtt:
        res.rtt_avg_ms = float(rtt.group(1))
    return res


# ---------------------------------------------------------------------------
# uptime / load
# ---------------------------------------------------------------------------


# A load figure is digits with at most one separator; matching [\d.,]+ instead
# would swallow the trailing comma of "0.31, 0.22" and fail the float().
_NUM = r"\d+(?:[.,]\d+)?"
_LOAD_RE = re.compile(rf"load average[s]?:\s*({_NUM})[,\s]+({_NUM})[,\s]+({_NUM})")


def parse_load(text: str) -> Optional[Tuple[float, float, float]]:
    m = _LOAD_RE.search(text)
    if not m:
        return None
    try:
        return tuple(float(g.replace(",", ".")) for g in m.groups())  # type: ignore[return-value]
    except ValueError:
        return None


def parse_proc_uptime(text: str) -> Optional[float]:
    """Seconds of uptime from /proc/uptime -- exact, unlike parsing `uptime`."""
    try:
        return float(text.split()[0])
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# mdstat
# ---------------------------------------------------------------------------


@dataclass
class MdArray:
    name: str
    state: str
    healthy: bool
    detail: str = ""


def parse_mdstat(text: str) -> List[MdArray]:
    """Parse /proc/mdstat.

    An array is healthy when its ``[n/n]`` counts match and its status map has
    no underscores -- ``[UU]`` good, ``[U_]`` degraded.  A resyncing array is
    reported as not-healthy with the progress line as detail, because a rebuild
    started by an unclean power-down is something the operator must know about
    even though the array is technically serving data.
    """
    arrays: List[MdArray] = []
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        m = re.match(r"^(md\d+)\s*:\s*(\S+)\s+(.*)$", line.strip())
        if not m:
            continue
        name, state, _members = m.group(1), m.group(2), m.group(3)
        detail = ""
        healthy = state == "active"
        block = []
        for follow in lines[idx + 1: idx + 4]:
            # Stop at the next array: its counts and resync line are its own.
            if re.match(r"^md\d+\s*:", follow.strip()):
                break
            block.append(follow)
        if any(re.search(r"super external:(imsm|ddf)\b", f) for f in block):
            # An Intel RST / DDF metadata container (members marked (S)) is
            # always "inactive"; the arrays it holds are listed separately with
            # "super external:/mdNNN/N". Live: dl-01's healthy RAID1 was
            # reported degraded through its container.
            arrays.append(MdArray(name=name, state=state, healthy=True,
                                  detail="metadata container"))
            continue
        for follow in block:
            # mdstat writes [total/active], e.g. "[2/1] [U_]" for a two-disk
            # mirror with one disk missing.  The detail string echoes that
            # ordering verbatim -- printing it the other way round would have
            # an operator comparing this report against /proc/mdstat and seeing
            # two different numbers.
            counts = re.search(r"\[(\d+)/(\d+)\]\s*\[([U_]+)\]", follow)
            if counts:
                total, active, flags = (int(counts.group(1)),
                                        int(counts.group(2)), counts.group(3))
                if active != total or "_" in flags:
                    healthy = False
                    detail = f"degraded: [{total}/{active}] [{flags}]"
            if "recovery" in follow or "resync" in follow:
                healthy = False
                detail = follow.strip()
        arrays.append(MdArray(name=name, state=state, healthy=healthy, detail=detail))
    return arrays


# ---------------------------------------------------------------------------
# ipmitool sel list
# ---------------------------------------------------------------------------


@dataclass
class SelEntry:
    """One row of ``ipmitool sel list``.

    The layout is ``id | date | time | sensor | event | direction``, where the
    record id is hexadecimal (``%4x``, so ``   1``, ``  1a``) and the date is
    ``Pre-Init`` with the time column a raw counter for events logged before
    the BMC's clock was set. Some records (OEM, some discrete sensors) carry
    no direction column.
    """

    record_id: str
    date: str
    time: str
    sensor: str
    event: str = ""
    direction: str = ""
    raw: str = ""

    @property
    def fingerprint(self) -> str:
        """What identifies this event, apart from its id -- no timestamp.

        The BMC's clock is exactly what an outage resets, and a timestamp is
        rendered by the gateway's ipmitool, so it is not an identity: two
        readings of one unchanged record must fingerprint identically.
        """
        return " | ".join(" ".join(part.lower().split())
                          for part in (self.sensor, self.event, self.direction))

    @property
    def is_clear(self) -> bool:
        """The record a BMC writes when its event log is cleared."""
        return "log area reset/cleared" in " ".join(self.event.lower().split())

    def as_dict(self) -> Dict[str, Any]:
        return {"id": self.record_id, "date": self.date, "time": self.time,
                "sensor": self.sensor, "event": self.event,
                "direction": self.direction}


def parse_sel_list(text: str) -> List[SelEntry]:
    """Parse ``ipmitool sel list [last N]`` output, oldest first.

    ``SEL has no entries`` (an empty log) and anything whose first column is
    not a hexadecimal record id are skipped. Record ids are normalised to
    lower-case hex without leading zeros, so ``001A`` and ``1a`` are one id.
    """
    rows: List[SelEntry] = []
    for line in (text or "").splitlines():
        fields = [f.strip() for f in line.split("|")]
        if len(fields) < 4:
            continue
        try:
            record = int(fields[0], 16)
        except ValueError:
            continue
        rows.append(SelEntry(
            record_id=format(record, "x"),
            date=fields[1], time=fields[2], sensor=fields[3],
            event=fields[4] if len(fields) > 4 else "",
            direction=" | ".join(fields[5:]),
            raw=line.strip()))
    return rows


def sel_baseline(entries: List[SelEntry]) -> Dict[str, str]:
    """``{record_id: fingerprint}``: what a later reading is compared to."""
    return {e.record_id: e.fingerprint for e in entries}


@dataclass
class SelDiff:
    """A SEL reading compared against a baseline taken earlier."""

    new: List[SelEntry]
    #: Ids in both readings whose event differs: the log was cleared and the
    #: BMC started numbering again.
    reused: List[str]
    #: The log was cleared since the baseline: a new 'Log area reset/cleared'
    #: record, or a reused id.
    cleared: bool
    #: Every row returned is new and the reading was a full tail, so more new
    #: events may have scrolled out of it.
    possibly_truncated: bool


def diff_sel(baseline: Dict[str, str], entries: List[SelEntry],
             tail: int = 20) -> SelDiff:
    """Which of *entries* were not in *baseline*.

    New means the record id is absent from the baseline, or present with a
    different fingerprint. Identity is the record id, never the position in
    the tail and never a timestamp: with a full log both readings are *tail*
    rows long, and rotation moves every position while ids stay put.
    """
    new: List[SelEntry] = []
    reused: List[str] = []
    for entry in entries:
        seen = baseline.get(entry.record_id)
        if seen is None:
            new.append(entry)
        elif seen != entry.fingerprint:
            new.append(entry)
            reused.append(entry.record_id)
    cleared = bool(reused) or any(e.is_clear for e in new)
    truncated = bool(entries) and len(new) == len(entries) and len(entries) >= tail
    return SelDiff(new=new, reused=reused, cleared=cleared,
                   possibly_truncated=truncated)


# ---------------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------------


def smart_unreadable(text: str) -> bool:
    """smartctl answered but could not read the verdict (``result: UNKNOWN!``).

    Live on trk-11: "SMART Status command failed: scsi error aborted command".
    That is "we could not look", not a SMART failure.
    """
    return bool(re.search(r"self-assessment test result:\s*UNKNOWN", text or "", re.I))


def parse_proc_mounts(text: str) -> Dict[str, Tuple[str, str]]:
    """``{mountpoint: (source, fstype)}`` from /proc/mounts.

    Read instead of ``df`` for network mounts: ``df -l`` omits them by
    definition, and plain ``df`` stats every mount, which hangs on a stale
    NFS handle -- the failure the NFS check exists to find. Octal escapes
    (``\\040`` for a space) are decoded.
    """
    out: Dict[str, Tuple[str, str]] = {}
    for line in (text or "").splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        unescape = lambda f: re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), f)  # noqa: E731
        out[unescape(fields[1])] = (unescape(fields[0]), fields[2])
    return out


def parse_smart_health(text: str) -> Optional[bool]:
    """True/False from smartctl's overall-health line; None if not present."""
    m = re.search(r"overall-health self-assessment test result:\s*(\S+)", text, re.I)
    if m:
        return m.group(1).upper() == "PASSED"
    m = re.search(r"SMART Health Status:\s*(\S+)", text, re.I)
    if m:
        return m.group(1).upper() == "OK"
    return None


#: Kernel messages that indicate storage or filesystem trouble.  Deliberately
#: narrow: a post-outage journal is full of noise, and a check that cries wolf
#: on every boot message is one an operator learns to ignore.
DISK_ERROR_PATTERNS = [
    r"I/O error",
    r"critical medium error",
    r"Medium Error",
    r"failed command: (READ|WRITE)",
    r"EXT4-fs error",
    r"XFS \(.*\): (metadata I/O error|Corruption)",
    r"md/raid.*: Disk failure",
    r"Buffer I/O error",
    r"nvme.*: I/O \d+ QID \d+ timeout",
]


def find_disk_errors(text: str, patterns: Optional[List[str]] = None) -> List[str]:
    compiled = [re.compile(p, re.I) for p in (patterns or DISK_ERROR_PATTERNS)]
    hits: List[str] = []
    for line in text.splitlines():
        if any(rx.search(line) for rx in compiled):
            hits.append(line.strip())
    return hits
