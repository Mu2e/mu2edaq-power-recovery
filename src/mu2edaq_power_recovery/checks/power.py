"""IPMI-side checks: chassis power, sensors, and the system event log.

These run against the BMC, not the operating system, so they work on a node
that is powered off or hung -- which is the whole reason they exist.  Phase 1
uses them to find out what survived the outage before anything is touched.
"""
from __future__ import annotations

import time
from typing import Optional

from ..transport.ipmi import SEL_TAIL, PowerState
from .base import CheckContext, CheckResult, Status, register, result
from .parsers import diff_sel, parse_sel_list, sel_baseline

#: Words that make an event-log entry worth an operator's attention.
SEL_SEVERE_WORDS = ("critical", "non-recoverable", "failure", "fault",
                    "correctable ecc", "uncorrectable", "predictive")


def _no_ipmi(ctx: CheckContext, check_id: str, started: float) -> CheckResult:
    if ctx.ipmi is None:
        return result(ctx, check_id, Status.SKIP,
                      "no IPMI client configured for this run", "", {}, started)
    if not ctx.node.ipmi_host:
        return result(ctx, check_id, Status.SKIP,
                      f"{ctx.node.short} has no BMC in the topology", "", {}, started)
    return None  # type: ignore[return-value]


def _refusal(ctx: CheckContext) -> Optional[str]:
    """The run's shared credential diagnosis, if the breaker has tripped."""
    return getattr(ctx.ipmi, "credentials_refused", None)


def _refused_result(ctx: CheckContext, check_id: str, what: str, data: dict,
                    started: float) -> CheckResult:
    """UNKNOWN: we could not look. Not FAIL, and not a protected refusal."""
    return result(ctx, check_id, Status.UNKNOWN,
                  f"IPMI credentials refused; {what} not read",
                  _refusal(ctx) or f"BMC {ctx.node.ipmi_host} rejected the IPMI "
                  "credentials", data, started)


@register("power.status", "IPMI chassis power state")
def power_status(ctx: CheckContext) -> CheckResult:
    started = time.monotonic()
    skip = _no_ipmi(ctx, "power.status", started)
    if skip is not None:
        return skip

    state = ctx.ipmi.power_status(ctx.node.ipmi_host)
    data = {"bmc": ctx.node.ipmi_host, "state": state.value}
    if state is PowerState.REFUSED:
        return _refused_result(ctx, "power.status", "power state", data, started)
    reason = (getattr(ctx.ipmi, "unreachable_reason", {}) or {}).get(ctx.node.ipmi_host)
    if state is PowerState.UNREACHABLE and reason == "unresolved":
        # UNKNOWN: nothing was asked of any BMC. The inventory names a BMC
        # that DNS does not know.
        return result(ctx, "power.status", Status.UNKNOWN,
                      f"BMC name {ctx.node.ipmi_host} does not resolve on the gateway",
                      "fix the topology entry or DNS; this says nothing about the "
                      "machine", data, started)
    if state is PowerState.UNREACHABLE and reason == "no_session":
        return result(ctx, "power.status", Status.UNKNOWN,
                      f"BMC {ctx.node.ipmi_host} answers ping but will not open an "
                      f"IPMI session",
                      "the BMC has standby power; its account or cipher suite differs "
                      "from the one this run uses (mu2e-ipmi-tool --diagnose finds "
                      "the working combination, at the cost of failed logins)",
                      data, started)
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
    if not readings and _refusal(ctx):
        return _refused_result(ctx, "power.sensors", "sensors",
                               {"bmc": ctx.node.ipmi_host}, started)
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
    """Read the tail of the system event log and compare it with the survey.

    BMC clocks routinely lose time across a power outage, so neither filtering
    nor comparing by the BMC's own timestamps is safe. The first successful
    reading is kept as ``{record_id: fingerprint}`` in the context baseline
    (key ``sel``); a later reading reports as new every record whose id is not
    in it, or whose event changed under the same id. Ids are what survive
    rotation: once the log is full both readings are twenty rows long, and a
    length comparison sees nothing.
    """
    started = time.monotonic()
    skip = _no_ipmi(ctx, "power.sel", started)
    if skip is not None:
        return skip

    lines = ctx.ipmi.sel(ctx.node.ipmi_host)
    if lines is None:
        # Not an empty log: we could not read it. No 'records' key, so no
        # baseline is taken from this reading.
        data = {"bmc": ctx.node.ipmi_host}
        if _refusal(ctx):
            return _refused_result(ctx, "power.sel", "event log", data, started)
        return result(ctx, "power.sel", Status.UNKNOWN,
                      "the BMC event log could not be read", "", data, started)

    entries = parse_sel_list("\n".join(lines))
    baseline = ctx.baseline.get("sel")

    def severe(entry) -> bool:
        return any(word in entry.raw.lower() for word in SEL_SEVERE_WORDS)

    interesting = [e.raw for e in entries if severe(e)]
    data = {"bmc": ctx.node.ipmi_host, "count": len(entries),
            "entries": [e.raw for e in entries][-SEL_TAIL:],
            "interesting": interesting,
            "records": sel_baseline(entries),
            "baseline_count": len(baseline) if baseline is not None else None}

    if baseline is not None:
        diff = diff_sel(baseline, entries, tail=SEL_TAIL)
        data.update({"new": [e.raw for e in diff.new], "cleared": diff.cleared,
                     "reused_ids": diff.reused,
                     "possibly_truncated": diff.possibly_truncated})
        notes = []
        if diff.cleared:
            notes.append("the event log was cleared after the survey, so events "
                         "logged before the clear may be gone")
        if diff.possibly_truncated:
            notes.append(f"every one of the {len(entries)} rows read is new: more "
                         f"new events may have scrolled out of the last "
                         f"{SEL_TAIL}; read the full log with 'mu2e-ipmi-tool "
                         f"-n {ctx.node.short} sel list'")
        tail = ("\n(" + "; ".join(notes) + ")") if notes else ""
        bad = [e.raw for e in diff.new if severe(e)]
        if bad:
            return result(ctx, "power.sel", Status.FAIL,
                          f"{len(bad)} new critical event(s) since the survey",
                          "\n".join(bad) + tail, data, started)
        if diff.cleared:
            return result(ctx, "power.sel", Status.WARN,
                          "SEL cleared since survey",
                          "\n".join(e.raw for e in diff.new[:10]) + tail,
                          data, started)
        if diff.new:
            return result(ctx, "power.sel", Status.WARN,
                          f"{len(diff.new)} new event(s) since the survey",
                          "\n".join(e.raw for e in diff.new[:10]) + tail,
                          data, started)
        return result(ctx, "power.sel", Status.OK,
                      f"no new events since the survey ({len(entries)} recent "
                      f"entries)",
                      (f"{len(interesting)} pre-existing critical entry/entries, "
                       "reported by the survey") if interesting else "",
                      data, started)

    if interesting:
        return result(ctx, "power.sel", Status.WARN,
                      f"{len(interesting)} critical entry/entries in the event log",
                      "\n".join(interesting[:10]) +
                      "\n(pre-existing: recorded as the baseline for later phases)",
                      data, started)
    return result(ctx, "power.sel", Status.OK,
                  f"event log clean ({len(entries)} recent entries)", "", data, started)
