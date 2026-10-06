"""Node inventory: locations, networks, host classes and IPMI mapping.

The entry syntax is deliberately identical to
``mu2edaq-operations/scripts/nodes_config.yaml`` -- a network's node list is a
sequence whose elements are either a fully-qualified hostname or a NodeRange
mapping that expands to a numbered sequence.  Lists can therefore be copied
between the two files unchanged, which matters because that repository stays
the authoritative inventory and this one has to track it.

What this module adds on top of the upstream expansion:

* ``Node`` objects that know their location, class, subnet, and the hostnames
  of their sibling interfaces on the data and IPMI networks;
* protection flags, so a destructive IPMI verb can be refused for the
  gateways, the manager and dcs-01 before it is ever built into a command;
* alias handling (``heerc`` -> ``teststand``) and the ``mc-2``/``mc2`` spelling
  tolerance the upstream CLI already had.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

import yaml

DEFAULT_PREFIX = "mu2e"
DEFAULT_DOMAIN = "fnal.gov"


class TopologyError(ValueError):
    """The topology file is missing, malformed, or was queried for something
    it does not define."""


# ---------------------------------------------------------------------------
# Hostname validation
# ---------------------------------------------------------------------------

#: One DNS label: letters, digits and hyphens, 1-63 characters, neither
#: starting nor ending with a hyphen (RFC 1123).
_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
#: The only characters an IP literal may contain.  Checked before handing the
#: string to ipaddress, so an IPv6 zone id ("fe80::1%eth0") -- whose text is
#: not constrained -- is refused rather than passed through.
_IP_CHARS = re.compile(r"^[0-9A-Fa-f:.]+\Z")


def valid_hostname(name: Any) -> bool:
    """True when *name* is a DNS hostname or an IPv4/IPv6 literal.

    This is the boundary between configuration or command-line text and the
    shell scripts phase 3 builds: a name that passes contains only
    ``[A-Za-z0-9.-:]``, never starts with ``-`` (so it cannot be read as an
    option by ping or ssh), and never contains whitespace, quotes or any shell
    metacharacter.  The callers still shell-quote -- validation and quoting
    are independent defences -- but a valid name comes out of ``shlex.quote``
    unchanged, which the mesh markers rely on.

    Accepted: ``mu2e-trk-01.fnal.gov``, ``mu2e-trk-01``, ``mu2edaq07``,
    ``131.225.245.10``, ``fe80::1``, ``::1``, and an FQDN with a trailing dot.

    The one function every hostname check uses -- load-time topology
    validation, ``Topology.resolve()`` for command-line names, and any later
    validator -- so the rule cannot drift between them.
    """
    if not isinstance(name, str) or not name or len(name) > 253:
        return False
    if _IP_CHARS.match(name) and ":" in name:
        try:
            ipaddress.IPv6Address(name)
            return True
        except ValueError:
            return False
    if _IP_CHARS.match(name) and re.match(r"^[0-9.]+\Z", name):
        # All digits and dots: an IPv4 literal or nothing.  "1.2.3" would
        # otherwise pass as three numeric DNS labels.
        try:
            ipaddress.IPv4Address(name)
            return True
        except ValueError:
            return False
    labels = name[:-1].split(".") if name.endswith(".") else name.split(".")
    return all(_LABEL.match(label) for label in labels)


def _is_ip_literal(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def require_hostname(name: Any, where: str) -> str:
    """Return *name* if :func:`valid_hostname` accepts it, else raise."""
    if not valid_hostname(name):
        raise TopologyError(
            f"invalid hostname {name!r} in {where}: a hostname must be DNS "
            f"labels (letters, digits, '-', not starting or ending with '-') "
            f"separated by '.', or an IPv4/IPv6 address; whitespace, quotes and "
            f"shell metacharacters are not allowed")
    return name


# ---------------------------------------------------------------------------
# NodeRange -- compact node-sequence notation (same semantics as upstream)
# ---------------------------------------------------------------------------


@dataclass
class NodeRange:
    """A run of consecutively numbered hosts.

    Expands to ``<prefix>-<category>-<fmt % n><suffix>.<domain>`` when
    *category* is set, and ``<prefix><fmt % n><suffix>.<domain>`` when it is
    empty (the ``mu2edaq07.fnal.gov`` teststand form, where the prefix already
    encodes the host type).
    """

    start: int
    end: int
    category: str = ""
    prefix: str = DEFAULT_PREFIX
    suffix: str = ""
    excludes: List[int] = field(default_factory=list)
    fmt: str = "%02d"
    domain: str = DEFAULT_DOMAIN

    def hostnames(self) -> List[str]:
        out: List[str] = []
        skip = set(self.excludes)
        for i in range(self.start, self.end + 1):
            if i in skip:
                continue
            num = self.fmt % i
            if self.category:
                out.append(f"{self.prefix}-{self.category}-{num}{self.suffix}.{self.domain}")
            else:
                out.append(f"{self.prefix}{num}{self.suffix}.{self.domain}")
        return out

    @classmethod
    def from_dict(cls, entry: Dict[str, Any], domain: str, default_prefix: str) -> "NodeRange":
        try:
            start, end = entry["start"], entry["end"]
        except KeyError as exc:  # pragma: no cover - config authoring error
            raise TopologyError(f"NodeRange entry missing {exc}: {entry!r}") from exc
        return cls(
            start=start,
            end=end,
            category=entry.get("category", ""),
            prefix=entry.get("prefix", default_prefix),
            suffix=entry.get("suffix", ""),
            excludes=list(entry.get("excludes", [])),
            fmt=entry.get("fmt", "%02d"),
            domain=domain,
        )


def expand_entries(entries: Sequence[Any], domain: str = DEFAULT_DOMAIN,
                   default_prefix: str = DEFAULT_PREFIX) -> List[str]:
    """Flatten a mixed list of hostnames and NodeRange mappings.

    Exported because config/power-sequence.yaml reuses the same syntax for its
    stage node lists.
    """
    out: List[str] = []
    for entry in entries or []:
        if isinstance(entry, str):
            out.append(entry)
        elif isinstance(entry, dict):
            out.extend(NodeRange.from_dict(entry, domain, default_prefix).hostnames())
        else:  # pragma: no cover - config authoring error
            raise TopologyError(f"unexpected topology entry {entry!r} ({type(entry).__name__})")
    return out


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------


@dataclass
class Node:
    """One physical host, keyed by its lab-network FQDN.

    ``hostname`` is always the lab-network name; the data and IPMI names are
    derived and only populated when the topology actually lists that interface
    for the host, so a check can distinguish "no data NIC by design" (dcs, the
    gateways) from "data NIC missing".
    """

    hostname: str
    location: str
    node_class: str = "other"
    networks: Dict[str, str] = field(default_factory=dict)   # network -> iface hostname
    subnets: Dict[str, str] = field(default_factory=dict)    # network -> CIDR
    protected: bool = False

    # -- convenience ------------------------------------------------------
    @property
    def short(self) -> str:
        """Hostname without the domain, as the on-node scripts print it."""
        return self.hostname.split(".")[0]

    @property
    def ipmi_host(self) -> Optional[str]:
        return self.networks.get("ipmi")

    @property
    def data_host(self) -> Optional[str]:
        return self.networks.get("data")

    def has_network(self, network: str) -> bool:
        return network in self.networks

    def as_dict(self) -> Dict[str, Any]:
        return {
            "hostname": self.hostname,
            "short": self.short,
            "location": self.location,
            "class": self.node_class,
            "networks": dict(self.networks),
            "subnets": dict(self.subnets),
            "protected": self.protected,
        }


# ---------------------------------------------------------------------------
# Topology
# ---------------------------------------------------------------------------


class Topology:
    """The parsed contents of config/topology.yaml."""

    def __init__(self, data: Dict[str, Any], source: Optional[Path] = None):
        self._data = data
        self.source = source
        self.domain: str = data.get("domain", DEFAULT_DOMAIN)
        self.default_prefix: str = data.get("default_prefix", DEFAULT_PREFIX)
        self.ipmi_suffix: str = data.get("ipmi_suffix", "-ipmi")
        self.data_suffix: str = data.get("data_suffix", "-data")
        self._protected: Set[str] = set(data.get("protected", []) or [])
        self._roles: List[Any] = [
            (re.compile(r["pattern"]), r["class"]) for r in data.get("roles", []) or []
        ]
        self._locations: Dict[str, Dict[str, Any]] = data.get("locations", {}) or {}
        # alias -> canonical location name (plus the mc-2/mc2 spelling fold)
        self._aliases: Dict[str, str] = {}
        for name, loc in self._locations.items():
            self._aliases[name] = name
            self._aliases[name.replace("-", "")] = name
            for alias in loc.get("aliases", []) or []:
                self._aliases[alias] = name
                self._aliases[alias.replace("-", "")] = name
        self._node_cache: Dict[str, Dict[str, Node]] = {}
        self._validate_hostnames()

    def _validate_hostnames(self) -> None:
        """Reject any expanded name that is not a hostname, at load.

        Every name here ends up as an ssh target or inside a phase-3 ping
        script, so a stray space or metacharacter in the inventory is refused
        once, with the location and network it came from, rather than
        surfacing as a baffling probe failure (or a command) mid-recovery.
        """
        for host in self._protected:
            require_hostname(host, "protected:")
        for loc, info in self._locations.items():
            for key in ("gateways", "ipmi_gateways"):
                for gw in (info or {}).get(key, []) or []:
                    require_hostname(gw, f"locations.{loc}.{key}")
            for network, entries in ((info or {}).get("networks", {}) or {}).items():
                for host in expand_entries(entries, self.domain, self.default_prefix):
                    require_hostname(host, f"locations.{loc}.networks.{network}")

    # -- construction -----------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "Topology":
        try:
            with open(path) as fh:
                data = yaml.safe_load(fh) or {}
        except FileNotFoundError as exc:
            raise TopologyError(f"topology file not found: {path}") from exc
        except yaml.YAMLError as exc:
            raise TopologyError(f"topology file {path} is not valid YAML: {exc}") from exc
        if not isinstance(data, dict) or "locations" not in data:
            raise TopologyError(f"topology file {path} has no 'locations:' section")
        return cls(data, source=path)

    # -- locations and networks -------------------------------------------

    @property
    def locations(self) -> List[str]:
        return list(self._locations)

    def canonical_location(self, name: str) -> str:
        """Resolve 'mc-2', 'MC2', 'heerc' to the canonical location key."""
        key = (name or "").strip().lower()
        resolved = self._aliases.get(key) or self._aliases.get(key.replace("-", ""))
        if resolved is None:
            raise TopologyError(
                f"unknown location {name!r}; known: {', '.join(sorted(self._locations))}"
            )
        return resolved

    def location_info(self, location: str) -> Dict[str, Any]:
        return self._locations[self.canonical_location(location)]

    def label(self, location: str) -> str:
        info = self.location_info(location)
        return info.get("label", self.canonical_location(location).upper())

    def networks(self, location: str) -> List[str]:
        return list(self.location_info(location).get("networks", {}))

    def subnet(self, location: str, network: str) -> Optional[str]:
        return self.location_info(location).get("subnets", {}).get(network)

    def gateways(self, location: str) -> List[str]:
        return list(self.location_info(location).get("gateways", []) or [])

    def ipmi_gateways(self, location: str) -> List[str]:
        """Hosts that run ipmitool for *location*'s BMCs.

        ``ipmi_gateways:`` when the location sets it, else ``gateways:``. They
        differ at the teststand: its BMCs sit on the MC-2 IPMI segment
        (192.168.157.0/24), which mu2edaq-gateway has no interface on, so they
        are driven from the MC-2 gateways (verified live 2026-10-01).
        """
        explicit = self.location_info(location).get("ipmi_gateways")
        return list(explicit) if explicit else self.gateways(location)

    def hostnames(self, location: str, network: str) -> List[str]:
        """Raw expanded hostnames for one location/network, upstream-compatible."""
        loc = self.canonical_location(location)
        nets = self._locations[loc].get("networks", {})
        if network not in nets:
            raise TopologyError(
                f"network {network!r} is not defined for location {loc!r}; "
                f"defined: {', '.join(sorted(nets)) or '(none)'}"
            )
        return expand_entries(nets[network], self.domain, self.default_prefix)

    # -- host classification ----------------------------------------------

    def classify(self, hostname: str) -> str:
        """Map a hostname to its class via the first matching role pattern."""
        short = hostname.split(".")[0]
        for pattern, cls in self._roles:
            if pattern.search(short):
                return cls
        return "other"

    def is_protected(self, hostname: str) -> bool:
        """True when destructive IPMI verbs must be refused for this host."""
        if hostname in self._protected:
            return True
        short = hostname.split(".")[0]
        return any(p.split(".")[0] == short for p in self._protected)

    # -- node assembly -----------------------------------------------------

    def _base_name(self, iface_host: str, suffix: str) -> str:
        """'mu2e-trk-01-ipmi.fnal.gov' + '-ipmi' -> 'mu2e-trk-01.fnal.gov'."""
        short, _, domain = iface_host.partition(".")
        if suffix and short.endswith(suffix):
            short = short[: -len(suffix)]
        return f"{short}.{domain}" if domain else short

    def nodes(self, location: str) -> Dict[str, Node]:
        """Every node at *location*, keyed by lab-network FQDN.

        A node is created from whichever network mentions it first; the other
        networks then attach their interface names to the existing node.  A
        host that appears only on, say, the IPMI network (its BMC answers but
        the machine has no lab entry) still gets a Node, so that phase 1 can
        report "powered but not in the lab inventory" rather than losing it.
        """
        loc = self.canonical_location(location)
        if loc in self._node_cache:
            return self._node_cache[loc]

        info = self._locations[loc]
        subnets = info.get("subnets", {}) or {}
        nets = info.get("networks", {}) or {}
        # lab first so that the canonical key is the lab name wherever possible
        ordered = ["lab"] + [n for n in nets if n != "lab"]

        nodes: Dict[str, Node] = {}
        for network in ordered:
            if network not in nets:
                continue
            suffix = {"ipmi": self.ipmi_suffix, "data": self.data_suffix}.get(
                network, f"-{network}" if network not in ("lab", "pcie") else ""
            )
            for iface_host in self.hostnames(loc, network):
                base = self._base_name(iface_host, suffix)
                node = nodes.get(base)
                if node is None:
                    node = Node(
                        hostname=base,
                        location=loc,
                        node_class=self.classify(base),
                        protected=self.is_protected(base),
                    )
                    nodes[base] = node
                node.networks[network] = iface_host
                if network in subnets:
                    node.subnets[network] = subnets[network]

        self._node_cache[loc] = nodes
        return nodes

    def all_nodes(self, locations: Optional[Iterable[str]] = None) -> List[Node]:
        """Nodes across several locations, de-duplicated and ordered.

        Ordering is by location, then class, then hostname with any embedded
        run of digits compared numerically -- so trk-02 sorts before trk-10 in
        every report table.
        """
        locs = [self.canonical_location(l) for l in (locations or self.locations)]
        seen: Dict[str, Node] = {}
        for loc in locs:
            for host, node in self.nodes(loc).items():
                seen.setdefault(host, node)
        return sorted(seen.values(), key=lambda n: (n.location, n.node_class, natural_key(n.hostname)))

    def node(self, hostname: str,
             locations: Optional[Iterable[str]] = None) -> Optional[Node]:
        """Look a node up by full or short hostname."""
        target = hostname.split(".")[0]
        for n in self.all_nodes(locations):
            if n.hostname == hostname or n.short == target:
                return n
        return None

    def by_class(self, locations: Optional[Iterable[str]] = None) -> Dict[str, List[Node]]:
        """Nodes grouped by class, which is how the report tables are built."""
        grouped: Dict[str, List[Node]] = {}
        for n in self.all_nodes(locations):
            grouped.setdefault(n.node_class, []).append(n)
        return grouped

    def validate(self, sequence: Optional[Dict[str, Any]] = None) -> List[Finding]:
        """Check the inventory for gaps and inconsistencies (#25).

        Returns :class:`Finding` records -- ``error`` for what would break a
        run (an invalid hostname or subnet, a stage naming an unknown
        location), ``warning`` for what makes it incomplete or ambiguous (an
        empty location, a BMC with no lab host, a subnet shared between
        locations, a gateway or protected host missing from the inventory, a
        node no power-sequence stage covers), ``info`` otherwise. *sequence*
        is the parsed power-sequence.yaml; without it the stage checks are
        skipped. Deliberately not called at load: an incomplete inventory is
        normal (MC-1), and the load-time hostname rule already refuses what
        cannot be run. Exposed as ``mu2e-node-inventory --validate``.
        """
        return _validate(self, sequence)

    def resolve(self, names: Sequence[str],
                locations: Optional[Iterable[str]] = None) -> List[Node]:
        """Turn a list of hostnames (short or full) into Nodes.

        Names not present in the topology are still returned as Nodes, with
        location 'unknown' -- an operator naming a host explicitly on the
        command line should get it probed, not silently dropped, even if the
        inventory has not caught up with the machine room.

        Every name must pass :func:`valid_hostname` first, or TopologyError is
        raised: these come from ``--node`` and end up as ssh destinations and
        inside phase-3 shell scripts.  An IP literal is used as given.
        """
        out: List[Node] = []
        for name in names:
            require_hostname(name, "the requested node list")
            found = self.node(name, locations)
            if found is None:
                literal = _is_ip_literal(name)
                host = name if ("." in name or literal) else f"{name}.{self.domain}"
                found = Node(
                    hostname=host,
                    location="unknown",
                    node_class=self.classify(host),
                    networks={"lab": host},
                    protected=self.is_protected(host),
                )
            out.append(found)
        return out


# ---------------------------------------------------------------------------
# Validation (mu2e-node-inventory --validate; never called at load)
# ---------------------------------------------------------------------------


#: Finding levels, most severe first.
LEVELS = ("error", "warning", "info")


@dataclass
class Finding:
    """One observation about the inventory. Only ``error`` fails validation."""

    level: str
    message: str

    def as_dict(self) -> Dict[str, str]:
        return {"level": self.level, "message": self.message}


def _compress(names: Sequence[str], limit: int = 20) -> str:
    shorts = sorted({n.split(".")[0] for n in names}, key=natural_key)
    text = ", ".join(shorts[:limit])
    return text + (f" and {len(shorts) - limit} more" if len(shorts) > limit else "")


def _validate(topology: "Topology",
              sequence: Optional[Dict[str, Any]] = None) -> List[Finding]:
    findings: List[Finding] = []
    add = lambda level, message: findings.append(Finding(level, message))  # noqa: E731

    # -- hostnames (the load-time rule, by the same function) --------------
    raw_names: List[Any] = []
    for host in topology._protected:
        raw_names.append((host, "protected:"))
    for loc, info in topology._locations.items():
        for key in ("gateways", "ipmi_gateways"):
            for gw in (info or {}).get(key, []) or []:
                raw_names.append((gw, f"locations.{loc}.{key}"))
        for network, entries in ((info or {}).get("networks", {}) or {}).items():
            try:
                hosts = expand_entries(entries, topology.domain, topology.default_prefix)
            except (TopologyError, TypeError, ValueError) as exc:
                add("error", f"locations.{loc}.networks.{network}: {exc}")
                continue
            raw_names.extend((h, f"locations.{loc}.networks.{network}") for h in hosts)
    for host, where in raw_names:
        if not valid_hostname(host):
            add("error", f"invalid hostname {host!r} in {where}")

    # -- per location --------------------------------------------------------
    inventory: Set[str] = set()
    nodes_by_loc: Dict[str, Dict[str, Node]] = {}
    for loc in topology.locations:
        info = topology._locations.get(loc) or {}
        try:
            nodes = topology.nodes(loc)
        except TopologyError as exc:
            add("error", f"location {loc}: {exc}")
            nodes = {}
        nodes_by_loc[loc] = nodes
        inventory.update(nodes)
        if not nodes:
            meta = ", ".join(f"{key}: {info[key]}" for key in
                             ("status", "owner", "inventory_source") if info.get(key))
            add("warning", f"location {loc} has no nodes configured"
                + (f" ({meta})" if meta else "")
                + "; every phase reports it as 'no nodes configured'")
        orphans = [n.networks["ipmi"] for n in nodes.values()
                   if "ipmi" in n.networks and "lab" not in n.networks]
        if orphans:
            add("warning", f"location {loc}: {len(orphans)} BMC(s) with no lab "
                           f"host: {_compress(orphans)}")

    # -- subnets -------------------------------------------------------------
    parsed: List[Any] = []
    for loc in topology.locations:
        subnets = (topology._locations.get(loc) or {}).get("subnets", {}) or {}
        for network, cidr in subnets.items():
            try:
                parsed.append((loc, network, cidr,
                               ipaddress.ip_network(str(cidr), strict=False)))
            except ValueError as exc:
                add("error", f"locations.{loc}.subnets.{network}: {cidr!r} is not "
                             f"a network ({exc})")
    for i, (loc_a, net_a, cidr_a, a) in enumerate(parsed):
        for loc_b, net_b, cidr_b, b in parsed[i + 1:]:
            if loc_a == loc_b or a.version != b.version or not a.overlaps(b):
                continue
            how = "is the same subnet as" if a == b else "overlaps"
            add("warning", f"subnet {loc_a}.{net_a} {cidr_a} {how} "
                           f"{loc_b}.{net_b} {cidr_b}: a failed interface in it "
                           f"cannot be attributed to one location")

    # -- gateways and protected hosts ----------------------------------------
    for loc in topology.locations:
        for gw in topology.gateways(loc):
            if gw not in inventory:
                add("warning", f"gateway {gw} of location {loc} is not a lab host "
                               f"in any location's inventory")
    for host in sorted(topology._protected):
        if host not in inventory:
            add("warning", f"protected host {host} is not in the inventory")

    # -- the power sequence --------------------------------------------------
    if sequence is not None:
        defaults = sequence.get("defaults", {}) or {}
        staged: Dict[str, Set[str]] = {}
        for stage in sequence.get("stages", []) or []:
            name = stage.get("name", "?")
            loc_name = stage.get("location", defaults.get("location", "mc2"))
            try:
                loc = topology.canonical_location(loc_name)
            except TopologyError:
                add("error", f"power-sequence stage {name}: unknown location "
                             f"{loc_name!r}")
                continue
            try:
                hosts = expand_entries(stage.get("nodes", []) or [],
                                       topology.domain, topology.default_prefix)
            except (TopologyError, TypeError, ValueError) as exc:
                add("error", f"power-sequence stage {name}: {exc}")
                continue
            staged.setdefault(loc, set()).update(hosts)
            missing = [h for h in hosts if h not in nodes_by_loc.get(loc, {})]
            if missing:
                add("warning", f"power-sequence stage {name}: {len(missing)} "
                               f"node(s) not in the {loc} inventory: "
                               f"{_compress(missing)}")
        for loc, nodes in nodes_by_loc.items():
            if not nodes:
                continue
            if loc not in staged:
                add("info", f"location {loc} has no power-sequence stage; "
                            f"phase 2 powers nothing there")
                continue
            unstaged = [h for h in nodes if h not in staged[loc]]
            if unstaged:
                add("warning", f"location {loc}: {len(unstaged)} inventory "
                               f"node(s) in no power-sequence stage, so phase 2 "
                               f"never powers them: {_compress(unstaged)}")
    order = {level: i for i, level in enumerate(LEVELS)}
    findings.sort(key=lambda f: order.get(f.level, len(LEVELS)))
    return findings


def natural_key(text: str):
    """Sort key that compares embedded digit runs numerically."""
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]
