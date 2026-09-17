"""Service, PCIe and firewall checks."""
from __future__ import annotations

import time
from typing import Dict, List

from .base import CheckContext, CheckResult, Status, register, result


@register("svc.running", "the expected daemons are running")
def svc_running(ctx: CheckContext) -> CheckResult:
    """Check the per-class service list from config/checks.yaml.

    ``systemctl is-active`` is tried first and ``pgrep`` used as the fallback,
    because some of these run from puppet-managed init scripts rather than as
    systemd units and would otherwise be reported missing while running.
    """
    started = time.monotonic()
    services_cfg = ctx.config("services", {})
    wanted: List[str] = list(services_cfg.get("common", []) or [])
    wanted += [s for s in (services_cfg.get(ctx.node.node_class, []) or [])
               if s not in wanted]
    if not wanted:
        return result(ctx, "svc.running", Status.SKIP,
                      "no services configured for this host class", "", {}, started)

    states: Dict[str, str] = {}
    missing: List[str] = []
    for svc in wanted:
        active = ctx.run(["systemctl", "is-active", svc])
        if active.ok and active.output.strip() == "active":
            states[svc] = "active"
            continue
        running = ctx.run(["pgrep", "-x", svc])
        if running.ok and running.output.strip():
            states[svc] = f"running (pid {running.output.split()[0]})"
        else:
            states[svc] = active.output.strip() or "not running"
            missing.append(svc)

    data = {"services": states, "missing": missing}
    if missing:
        return result(ctx, "svc.running", Status.FAIL,
                      f"not running: {', '.join(missing)}",
                      "\n".join(f"{k}: {v}" for k, v in states.items()), data, started)
    return result(ctx, "svc.running", Status.OK,
                  f"{len(wanted)} service(s) running", "", data, started)


@register("svc.nfs_export", "the NFS exports are published", needs_root=True)
def svc_nfs_export(ctx: CheckContext) -> CheckResult:
    """Manager-only: nfsd is up and the export list is non-empty.

    Checked on the server rather than inferred from clients, so that a
    recovery can tell "the server has not exported yet" apart from "the client
    has not mounted yet" -- they need different actions and look identical
    from the client side.
    """
    started = time.monotonic()
    exports = ctx.run(["exportfs", "-s"], root=True)
    if not exports.ok:
        exports = ctx.run(["cat", "/proc/fs/nfsd/exports"], root=True)
    lines = [l for l in exports.lines() if not l.startswith("#")]
    active = ctx.run(["systemctl", "is-active", "nfs-server"], root=True)
    data = {"exports": lines[:40], "count": len(lines),
            "nfs_server": active.output.strip()}

    if not lines:
        return result(ctx, "svc.nfs_export", Status.FAIL,
                      "no filesystems are exported",
                      "every node that mounts /home from here will fail until "
                      "the exports are published", data, started)
    if active.output.strip() not in ("active", "unknown", ""):
        return result(ctx, "svc.nfs_export", Status.WARN,
                      f"{len(lines)} export(s) listed but nfs-server is "
                      f"{active.output.strip()}", "", data, started)
    return result(ctx, "svc.nfs_export", Status.OK,
                  f"{len(lines)} filesystem(s) exported", "", data, started)


@register("svc.firewall", "the packet filter is loaded", needs_root=True)
def svc_firewall(ctx: CheckContext) -> CheckResult:
    """Gateway-only: confirm the filter came back with rules in it.

    A gateway that boots with an empty ruleset is worse than one that boots
    with none at all -- it looks healthy and is wide open -- so an empty
    ruleset is reported as a failure, not as "no firewall configured".
    """
    started = time.monotonic()
    nft = ctx.run(["nft", "list", "ruleset"], root=True)
    iptables = ctx.run(["iptables", "-S"], root=True) if not nft.ok else None
    output = nft.output if nft.ok else (iptables.output if iptables else "")
    rules = [l for l in output.splitlines() if l.strip()]
    data = {"backend": "nft" if nft.ok else "iptables", "rule_count": len(rules)}

    if not rules:
        return result(ctx, "svc.firewall", Status.FAIL,
                      "the packet filter has no rules loaded",
                      "a gateway with an empty ruleset is forwarding everything",
                      data, started)
    # A bare iptables -S on an unconfigured host prints only the three default
    # -P policy lines; that is an empty ruleset wearing a disguise.
    if data["backend"] == "iptables" and len(rules) <= 3 and all(
            l.startswith("-P") for l in rules):
        return result(ctx, "svc.firewall", Status.FAIL,
                      "only default policies are present -- no rules loaded",
                      "\n".join(rules), data, started)
    return result(ctx, "svc.firewall", Status.OK,
                  f"packet filter loaded ({len(rules)} rules, {data['backend']})",
                  "", data, started)


@register("pcie.devices", "the DTC/CFO PCIe cards are enumerated", needs_root=True)
def pcie_devices(ctx: CheckContext) -> CheckResult:
    """Look for the Xilinx readout card on the PCIe bus.

    A card that is not enumerated after a power event is usually a cold-boot
    training failure: the machine is fine, the slot is not, and only a full
    AC power cycle recovers it.  Distinguishing that from a driver problem is
    why this is separate from pcie.driver.
    """
    started = time.monotonic()
    if not ctx.node.has_network("pcie"):
        return result(ctx, "pcie.devices", Status.SKIP,
                      "this host is not listed as carrying a DTC/CFO card", "",
                      {}, started)
    res = ctx.run(["lspci", "-d", "10ee:"], root=True)
    devices = res.lines()
    data = {"devices": devices, "count": len(devices)}
    if not devices:
        # Fall back to a name match, in case the vendor id filter is
        # unsupported by an old lspci.
        broad = ctx.run(["sh", "-c", "lspci | grep -i xilinx"], root=True)
        devices = broad.lines()
        data = {"devices": devices, "count": len(devices)}
    if not devices:
        return result(ctx, "pcie.devices", Status.FAIL,
                      "no Xilinx PCIe device found",
                      "the card did not enumerate; this usually needs a full AC "
                      "power cycle of the chassis, not a warm reboot", data, started)
    return result(ctx, "pcie.devices", Status.OK,
                  f"{len(devices)} PCIe readout device(s) present",
                  "\n".join(devices), data, started)


@register("pcie.driver", "the mu2e PCIe driver is loaded with device nodes",
          needs_root=True)
def pcie_driver(ctx: CheckContext) -> CheckResult:
    started = time.monotonic()
    if not ctx.node.has_network("pcie"):
        return result(ctx, "pcie.driver", Status.SKIP,
                      "this host is not listed as carrying a DTC/CFO card", "",
                      {}, started)
    lsmod = ctx.run(["sh", "-c", "lsmod | grep -w mu2e"], root=True)
    nodes = ctx.run(["sh", "-c", "ls /dev/mu2e* 2>/dev/null"], root=True)
    loaded = bool(lsmod.output.strip())
    devnodes = nodes.lines()
    data = {"module_loaded": loaded, "device_nodes": devnodes}

    if not loaded:
        return result(ctx, "pcie.driver", Status.FAIL,
                      "the mu2e kernel module is not loaded",
                      "run the spack PCIe setup on the node "
                      "(/mu2e/spack_pcie/setup-env.sh)", data, started)
    if not devnodes:
        return result(ctx, "pcie.driver", Status.FAIL,
                      "the module is loaded but there are no /dev/mu2e* nodes",
                      "the driver bound nothing -- see pcie.devices", data, started)
    return result(ctx, "pcie.driver", Status.OK,
                  f"driver loaded, {len(devnodes)} device node(s)", "", data, started)
