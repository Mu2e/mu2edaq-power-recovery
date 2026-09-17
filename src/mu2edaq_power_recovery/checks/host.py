"""Whole-host state: uptime, load, kernel."""
from __future__ import annotations

import time

from .base import CheckContext, CheckResult, Status, register, result
from .parsers import parse_load, parse_proc_uptime


@register("host.uptime", "uptime and load average, with a post-power-on sanity test")
def host_uptime(ctx: CheckContext) -> CheckResult:
    """Report uptime and load.

    Two things are being tested.  The obvious one is load: a node whose load is
    far above its core count minutes after boot is doing something unexpected.
    The subtle one is uptime itself -- when phase 2 has just powered a machine
    on, an uptime of days means the IPMI command went to a different chassis
    than the operator thought, or the node never actually went down.  The
    context flags that case by putting 'expect_recent_boot' in the baseline.
    """
    started = time.monotonic()
    up = ctx.run(["cat", "/proc/uptime"])
    seconds = parse_proc_uptime(up.output)
    loads = parse_load(ctx.run(["uptime"]).output)
    cores_res = ctx.run(["nproc"])
    try:
        cores = int(cores_res.output.strip())
    except ValueError:
        cores = 0

    data = {"uptime_s": seconds, "load": list(loads) if loads else None, "cores": cores}
    if seconds is None:
        return result(ctx, "host.uptime", Status.UNKNOWN,
                      "could not read /proc/uptime", up.output.strip(), data, started)

    hours = seconds / 3600.0
    summary = f"up {hours:.1f} h"
    if loads:
        summary += f", load {loads[0]:.2f}"

    if ctx.baseline.get("expect_recent_boot"):
        limit = float(ctx.threshold("max_post_boot_uptime_s", 3600))
        if seconds > limit:
            return result(ctx, "host.uptime", Status.WARN,
                          f"{summary} -- but this node was just powered on",
                          f"uptime of {hours:.1f} h after a power-on means the "
                          f"chassis did not actually reboot; check that the IPMI "
                          f"target matches the node", data, started)

    if loads and cores and loads[0] > cores * float(ctx.threshold("load_per_core_warn", 2.0)):
        return result(ctx, "host.uptime", Status.WARN,
                      f"{summary} on {cores} cores -- load is high",
                      f"1-minute load {loads[0]} against {cores} cores", data, started)
    return result(ctx, "host.uptime", Status.OK, summary, "", data, started)


@register("host.kernel", "kernel release and taint state")
def host_kernel(ctx: CheckContext) -> CheckResult:
    """Kernel version, plus the taint word.

    A non-zero taint after a power event usually means a module failed to load
    or a machine check was logged -- worth a WARN even though the node works.
    """
    started = time.monotonic()
    release = ctx.run(["uname", "-r"]).output.strip()
    tainted_raw = ctx.run(["cat", "/proc/sys/kernel/tainted"]).output.strip()
    try:
        tainted = int(tainted_raw)
    except ValueError:
        tainted = 0
    data = {"kernel": release, "tainted": tainted}
    if not release:
        return result(ctx, "host.kernel", Status.UNKNOWN,
                      "could not read the kernel release", "", data, started)
    if tainted:
        return result(ctx, "host.kernel", Status.WARN,
                      f"kernel {release}, tainted ({tainted})",
                      "a tainted kernel after a power event often means a driver "
                      "failed to load or a machine check was recorded",
                      data, started)
    return result(ctx, "host.kernel", Status.OK, f"kernel {release}", "", data, started)
