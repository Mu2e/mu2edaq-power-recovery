"""IPMI-side checks: chassis power, sensors, and the system event log.

These run against the BMC, not the operating system, so they work on a node
that is powered off or hung -- which is the whole reason they exist.  Phase 1
uses them to find out what survived the outage before anything is touched.
"""
from __future__ import annotations

import time
from typing import List

from ..transport.ipmi import PowerState
from .base import CheckContext, CheckResult, Status, register, result


def _no_ipmi(ctx: CheckContext, check_id: str, started: float) -> CheckResult:
    if ctx.ipmi is None:
        return result(ctx, check_id, Status.SKIP,
                      "no IPMI client configured for this run", "", {}, started)
    if not ctx.node.ipmi_host:
        return result(ctx, check_id, Status.SKIP,
                      f"{ctx.node.short} has no BMC in the topology", "", {}, started)
    return None  # type: ignore[return-value]


@register("power.status", "IPMI chassis power state")
def power_status(ctx: CheckContext) -> CheckResult:
    started = time.monotonic()
    skip = _no_ipmi(ctx, "power.status", started)
    if skip is not None:
        return skip

    state = ctx.ipmi.power_status(ctx.node.ipmi_host)
    data = {"bmc": ctx.node.ipmi_host, "state": state.value}
    if state is PowerState.UNREACHABLE:
        return result(ctx, "power.status", Status.FAIL,
                      f"BMC {ctx.node.ipmi_host} does not answer",
                      "the BMC has its own power path; if it is dark the chassis "
                      "has no standby power at all", data, started)
    if state is PowerState.OFF:
        return result(ctx, "power.status", Status.WARN,
                      "chassis is powered off",
                      "expected before phase 2; a failure after it", data, started)
    if state is PowerState.UNKNOWN:
        return result(ctx, "power.status", Status.UNKNOWN,
                      "the BMC answered but the power state could not be read",
                      "", data, started)
    return result(ctx, "power.status", Status.OK, "chassis is powered on", "",
                  data, started)


@register("power.sensors", "no IPMI sensor is in a critical state")
def power_sensors(ctx: CheckContext) -> CheckResult:
    """Read the sensor data repository and flag anything critical.

    Temperature and fan sensors are the ones that matter after an outage: a
    chassis that powered on into a room whose cooling has not come back is the
    failure this catches before the hardware does.
    """
    started = time.monotonic()
    skip = _no_ipmi(ctx, "power.sensors", started)
    if skip is not None:
        return skip

    readings = ctx.ipmi.sensors(ctx.node.ipmi_host)
    if not readings:
        return result(ctx, "power.sensors", Status.UNKNOWN,
                      "no sensor data returned by the BMC", "",
                      {"bmc": ctx.node.ipmi_host}, started)
    critical = [r for r in readings if r.critical]
    data = {"bmc": ctx.node.ipmi_host, "count": len(readings),
            "critical": [r.as_dict() for r in critical],
            "readings": [r.as_dict() for r in readings][:60]}
    if critical:
        return result(ctx, "power.sensors", Status.FAIL,
                      f"{len(critical)} sensor(s) critical",
                      "\n".join(f"{r.name}: {r.value} {r.unit} [{r.status}]"
                                for r in critical), data, started)
    return result(ctx, "power.sensors", Status.OK,
                  f"{len(readings)} sensor(s) within thresholds", "", data, started)


@register("power.sel", "no new critical entries in the BMC event log")
def power_sel(ctx: CheckContext) -> CheckResult:
    """Read the tail of the system event log.

    BMC clocks routinely lose time across a power outage, so filtering by the
    BMC's own timestamps would drop real events.  Instead the phase-1 entry
    count is stored in the context baseline and phase 2 reports only what
    appeared since -- which is exactly the set of events the power-on caused.
    """
    started = time.monotonic()
    skip = _no_ipmi(ctx, "power.sel", started)
    if skip is not None:
        return skip

    entries: List[str] = ctx.ipmi.sel(ctx.node.ipmi_host)
    baseline = ctx.baseline.get("sel_count")
    interesting = [e for e in entries
                   if any(word in e.lower() for word in
                          ("critical", "non-recoverable", "failure", "fault",
                           "correctable ecc", "uncorrectable", "predictive"))]
    data = {"bmc": ctx.node.ipmi_host, "count": len(entries),
            "baseline_count": baseline, "entries": entries[-20:],
            "interesting": interesting}

    if baseline is not None and len(entries) > baseline:
        new = entries[baseline:]
        severe = [e for e in new if e in interesting]
        if severe:
            return result(ctx, "power.sel", Status.FAIL,
                          f"{len(severe)} new critical event(s) since the survey",
                          "\n".join(severe), data, started)
        return result(ctx, "power.sel", Status.WARN,
                      f"{len(new)} new event(s) since the survey",
                      "\n".join(new[:10]), data, started)
    if interesting:
        return result(ctx, "power.sel", Status.WARN,
                      f"{len(interesting)} critical entry/entries in the event log",
                      "\n".join(interesting[:10]) +
                      "\n(pre-existing: recorded as the baseline for later phases)",
                      data, started)
    return result(ctx, "power.sel", Status.OK,
                  f"event log clean ({len(entries)} recent entries)", "", data, started)
