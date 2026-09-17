"""Network interface, DNS and segment checks."""
from __future__ import annotations

import time
from typing import Dict, List, Optional

from .base import CheckContext, CheckResult, Status, register, result
from .parsers import (Interface, address_in_subnet, parse_ip_addr,
                      parse_ip_link, parse_ping)
from .reachability import _ping_command


def _interfaces(ctx: CheckContext) -> Dict[str, Interface]:
    """Merged view of ``ip -o link`` and ``ip -o -4 addr``."""
    links = parse_ip_link(ctx.run(["ip", "-o", "link", "show"]).output)
    return parse_ip_addr(ctx.run(["ip", "-o", "-4", "addr", "show"]).output, links)


def _iface_on_subnet(ifaces: Dict[str, Interface], cidr: str) -> Optional[Interface]:
    for iface in ifaces.values():
        if any(address_in_subnet(addr, cidr) for addr in iface.addresses):
            return iface
    return None


@register("net.interfaces", "every network the node belongs to has an interface up")
def net_interfaces(ctx: CheckContext) -> CheckResult:
    """Match the node's configured networks against its live interfaces.

    The topology says which segments a host is *supposed* to be on, so this
    check does not have to guess from the interface names -- which differ
    between chassis generations.  It looks for an interface carrying an
    address in each expected subnet, which is both name-independent and the
    thing that actually matters.
    """
    started = time.monotonic()
    ifaces = _interfaces(ctx)
    expected = {net: cidr for net, cidr in ctx.node.subnets.items()
                if net in ctx.node.networks and net not in ("ipmi", "pcie")}
    if not expected:
        return result(ctx, "net.interfaces", Status.SKIP,
                      "no subnets recorded for this node in the topology", "",
                      {"interfaces": {n: i.as_dict() for n, i in ifaces.items()}}, started)

    missing: List[str] = []
    down: List[str] = []
    found: Dict[str, str] = {}
    for net, cidr in expected.items():
        iface = _iface_on_subnet(ifaces, cidr)
        if iface is None:
            missing.append(f"{net} ({cidr})")
            continue
        found[net] = iface.name
        if not iface.up:
            down.append(f"{net}: {iface.name} is {iface.state} "
                        f"(flags {','.join(iface.flags)})")

    data = {"expected": expected, "found": found, "missing": missing, "down": down,
            "interfaces": {n: i.as_dict() for n, i in ifaces.items()}}
    if missing:
        return result(ctx, "net.interfaces", Status.FAIL,
                      f"no interface on {', '.join(missing)}",
                      "the NIC did not come back, or its configuration did not "
                      "apply on boot", data, started)
    if down:
        return result(ctx, "net.interfaces", Status.FAIL,
                      f"{len(down)} expected interface(s) not carrying link",
                      "\n".join(down), data, started)
    return result(ctx, "net.interfaces", Status.OK,
                  "interfaces up on " + ", ".join(f"{n} ({i})" for n, i in found.items()),
                  "", data, started)


@register("net.data", "the 10 GbE data-network interface is up at link speed")
def net_data(ctx: CheckContext) -> CheckResult:
    """Data-network NIC presence, link state and negotiated speed.

    Renegotiating at 1 Gb/s after a switch reboot is a real and quiet failure:
    everything works, and the DAQ runs ten times too slowly.  check_functions.sh
    tests the same thing on-node; this does it from outside.
    """
    started = time.monotonic()
    if not ctx.node.has_network("data"):
        return result(ctx, "net.data", Status.SKIP,
                      "this host has no data-network interface by design", "",
                      {}, started)
    cidr = ctx.node.subnets.get("data")
    if not cidr:
        return result(ctx, "net.data", Status.SKIP,
                      "no data subnet recorded for this location", "", {}, started)

    ifaces = _interfaces(ctx)
    iface = _iface_on_subnet(ifaces, cidr)
    if iface is None:
        return result(ctx, "net.data", Status.FAIL,
                      f"no interface holds an address in the data network ({cidr})",
                      "\n".join(f"{i.name}: {', '.join(i.addresses) or 'no address'}"
                                for i in ifaces.values()),
                      {"expected_subnet": cidr}, started)

    speed_res = ctx.run(["cat", f"/sys/class/net/{iface.name}/speed"])
    try:
        speed = int(speed_res.output.strip())
    except ValueError:
        speed = -1
    expected_speed = int(ctx.threshold("data_link_speed_mbps", 10000))
    data = {"interface": iface.as_dict(), "speed_mbps": speed,
            "expected_speed_mbps": expected_speed, "subnet": cidr}

    if not iface.up:
        return result(ctx, "net.data", Status.FAIL,
                      f"data interface {iface.name} has no link",
                      f"state {iface.state}, flags {','.join(iface.flags)}",
                      data, started)
    if speed < 0:
        return result(ctx, "net.data", Status.WARN,
                      f"data interface {iface.name} is up but its speed is unreadable",
                      speed_res.output.strip(), data, started)
    if speed < expected_speed:
        return result(ctx, "net.data", Status.FAIL,
                      f"data interface {iface.name} negotiated {speed} Mb/s, "
                      f"expected {expected_speed}",
                      "a link that came back at the wrong speed will not be "
                      "noticed by anything except throughput", data, started)
    return result(ctx, "net.data", Status.OK,
                  f"{iface.name} up at {speed} Mb/s on {cidr}", "", data, started)


@register("net.dns", "forward and reverse name resolution work")
def net_dns(ctx: CheckContext) -> CheckResult:
    """Resolve a known name and this host's own name, both ways.

    DNS is checked on the node rather than from the gateway because what
    matters is whether *this* host can resolve -- a node that boots before its
    resolver is reachable often ends up with an empty or stale resolv.conf.
    """
    started = time.monotonic()
    probe_name = "mu2e-dl-01"
    forward = ctx.run(["getent", "hosts", probe_name])
    self_lookup = ctx.run(["getent", "hosts", ctx.node.hostname])
    data = {"forward": forward.output.strip(), "self": self_lookup.output.strip()}
    if not forward.ok:
        return result(ctx, "net.dns", Status.FAIL,
                      f"cannot resolve {probe_name}",
                      "check /etc/resolv.conf and reachability of the site resolvers",
                      data, started)
    if not self_lookup.ok:
        return result(ctx, "net.dns", Status.WARN,
                      f"cannot resolve its own name {ctx.node.hostname}", "",
                      data, started)
    return result(ctx, "net.dns", Status.OK, "name resolution works", "", data, started)


@register("net.forwarding", "the gateway is routing between the DAQ segments",
          needs_root=True)
def net_forwarding(ctx: CheckContext) -> CheckResult:
    """Gateway-only: IP forwarding enabled and a route to each segment.

    If forwarding came back off -- a fresh sysctl default, a partially applied
    configuration -- the gateway answers perfectly well itself while nothing
    behind it is reachable, which is a confusing failure to debug by hand.
    """
    started = time.monotonic()
    fwd = ctx.run(["cat", "/proc/sys/net/ipv4/ip_forward"], root=True)
    enabled = fwd.output.strip() == "1"
    routes = ctx.run(["ip", "-o", "route", "show"], root=True)

    subnets = ctx.topology.location_info(ctx.node.location).get("subnets", {}) or {}
    unrouted = [f"{net} ({cidr})" for net, cidr in subnets.items()
                if cidr.split("/")[0].rsplit(".", 1)[0] not in routes.output]
    data = {"ip_forward": enabled, "unrouted": unrouted,
            "routes": routes.output.strip().splitlines()[:40]}

    if not enabled:
        return result(ctx, "net.forwarding", Status.FAIL,
                      "IP forwarding is disabled on this gateway",
                      "nothing behind the gateway is reachable until "
                      "net.ipv4.ip_forward is 1", data, started)
    if unrouted:
        return result(ctx, "net.forwarding", Status.WARN,
                      f"no route covering {', '.join(unrouted)}", "", data, started)
    return result(ctx, "net.forwarding", Status.OK,
                  f"forwarding enabled, routes present for {len(subnets)} segment(s)",
                  "", data, started)


@register("net.ipmi_reach", "the IPMI segment is reachable from this gateway")
def net_ipmi_reach(ctx: CheckContext) -> CheckResult:
    """Gateway-only: can this gateway actually reach the BMC network?

    Phase 2 drives every IPMI command from a gateway, so this is a
    prerequisite rather than a nicety -- if it fails, power-on will fail for
    every node and the operator should know before the sequence starts.
    """
    started = time.monotonic()
    cidr = ctx.node.subnets.get("ipmi")
    if not cidr:
        return result(ctx, "net.ipmi_reach", Status.SKIP,
                      "no IPMI subnet recorded for this location", "", {}, started)

    ifaces = _interfaces(ctx)
    iface = _iface_on_subnet(ifaces, cidr)
    # Pick a BMC at this location to probe: the gateway's own is guaranteed to
    # exist and is the least disruptive thing to poke.
    target = ctx.node.networks.get("ipmi")
    count = int(ctx.threshold("ping_count", 3))
    timeout = int(ctx.threshold("ping_timeout_s", 5))
    reachable = None
    if target:
        stats = parse_ping(ctx.run(_ping_command(target, count, timeout),
                                   timeout=count * timeout + 10).output)
        reachable = stats.alive

    data = {"subnet": cidr, "interface": iface.name if iface else None,
            "probe_target": target, "reachable": reachable}
    if iface is None:
        return result(ctx, "net.ipmi_reach", Status.FAIL,
                      f"this gateway has no interface on the IPMI network ({cidr})",
                      "IPMI commands cannot be issued from here", data, started)
    if reachable is False:
        return result(ctx, "net.ipmi_reach", Status.FAIL,
                      f"IPMI interface {iface.name} is configured but {target} "
                      f"does not answer", "", data, started)
    return result(ctx, "net.ipmi_reach", Status.OK,
                  f"IPMI segment reachable via {iface.name}", "", data, started)
