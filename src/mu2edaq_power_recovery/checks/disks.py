"""Disk, filesystem and mount checks.

Thresholds and the expected mount list come from config/checks.yaml, whose
values were taken from mu2edaq-operations/scripts/check_functions.sh so that a
node this tool calls healthy is also healthy by the on-node script's standard.
"""
from __future__ import annotations

import time
from typing import Dict, List

from ..transport.base import TransportError
from .base import CheckContext, CheckResult, Status, register, result
from .parsers import (find_disk_errors, parse_df, parse_mdstat,
                      parse_smart_health)

#: -P keeps one filesystem per line, -T shows the type, and the -x exclusions
#: drop pseudo-filesystems that are always "full" and never interesting.
DF_COMMAND = ["df", "-lP", "-T", "-x", "tmpfs", "-x", "devtmpfs",
              "-x", "efivarfs", "-x", "fuse"]


@register("disk.local", "no local filesystem is over its usage threshold")
def disk_local(ctx: CheckContext) -> CheckResult:
    started = time.monotonic()
    res = ctx.run(DF_COMMAND)
    rows = parse_df(res.output)
    if not rows:
        return result(ctx, "disk.local", Status.UNKNOWN,
                      "df returned nothing usable", res.output.strip()[:500],
                      {}, started)

    fail_pct = int(ctx.threshold("disk_full_pct", 90))
    warn_pct = int(ctx.threshold("disk_warn_pct", 80))
    local = [f for f in rows if not f.is_network]
    full = [f for f in local if f.use_pct >= fail_pct]
    warn = [f for f in local if warn_pct <= f.use_pct < fail_pct]
    data = {"filesystems": [f.as_dict() for f in rows],
            "full": [f.mountpoint for f in full],
            "warn": [f.mountpoint for f in warn]}

    if full:
        detail = "\n".join(f"{f.mountpoint}  {f.use_pct}%  ({f.source})" for f in full)
        return result(ctx, "disk.local", Status.FAIL,
                      f"{len(full)} filesystem(s) at or above {fail_pct}%",
                      detail, data, started)
    if warn:
        detail = "\n".join(f"{f.mountpoint}  {f.use_pct}%" for f in warn)
        return result(ctx, "disk.local", Status.WARN,
                      f"{len(warn)} filesystem(s) above {warn_pct}%", detail, data, started)
    busiest = max(local, key=lambda f: f.use_pct) if local else None
    summary = f"{len(local)} local filesystem(s) healthy"
    if busiest:
        summary += f" (busiest {busiest.mountpoint} at {busiest.use_pct}%)"
    return result(ctx, "disk.local", Status.OK, summary, "", data, started)


@register("disk.mounts", "every expected filesystem is mounted")
def disk_mounts(ctx: CheckContext) -> CheckResult:
    """Check the configured mount list, plus the plain path-existence tests.

    The NFS entries are skipped on the host that exports them -- mu2e-mgr-01
    does not mount its own /home -- which is the same exemption check_nfs()
    makes upstream.
    """
    started = time.monotonic()
    mounts_cfg = ctx.config("mounts", {})
    expected: List[Dict] = list(mounts_cfg.get("common", []) or [])
    expected += list(mounts_cfg.get(ctx.node.location, []) or [])

    missing: List[str] = []
    present: List[str] = []
    skipped: List[str] = []

    for entry in expected:
        path = entry.get("path")
        if not path:
            continue
        server = entry.get("server")
        if server and server.split(".")[0] == ctx.node.short:
            skipped.append(f"{path} (exported by this host)")
            continue
        res = ctx.run(["mountpoint", "-q", path])
        (present if res.ok else missing).append(path)

    missing_paths: List[str] = []
    for path in mounts_cfg.get("paths", []) or []:
        if not ctx.run(["ls", "-d", path]).ok:
            missing_paths.append(path)

    data = {"present": present, "missing": missing, "skipped": skipped,
            "missing_paths": missing_paths}
    problems = missing + missing_paths
    if problems:
        return result(ctx, "disk.mounts", Status.FAIL,
                      f"not mounted / not present: {', '.join(problems)}",
                      "after an outage this is almost always an NFS server that "
                      "has not come back yet, or an automount that has not been "
                      "triggered", data, started)
    summary = f"{len(present)} expected mount(s) present"
    if skipped:
        summary += f", {len(skipped)} not applicable"
    return result(ctx, "disk.mounts", Status.OK, summary, "", data, started)


@register("disk.nfs_from_mgr", "shared areas are mounted from mu2e-mgr-01")
def disk_nfs_from_mgr(ctx: CheckContext) -> CheckResult:
    """Confirm the NFS mounts really come from the manager node.

    Project-Description.md asks for this specifically on the DCS hosts.  It is
    not the same test as disk.mounts: a stale entry can leave /home mounted
    from an address that no longer serves it, which passes 'mountpoint' and
    fails the moment anything reads a file.  So the source is compared, and
    then the mount is actually read.
    """
    started = time.monotonic()
    mounts_cfg = ctx.config("mounts", {})
    expected = [e for e in (list(mounts_cfg.get("common", []) or []) +
                            list(mounts_cfg.get(ctx.node.location, []) or []))
                if e.get("server")]
    if not expected:
        return result(ctx, "disk.nfs_from_mgr", Status.SKIP,
                      "no NFS mounts configured for this location", "", {}, started)

    rows = {f.mountpoint: f for f in parse_df(ctx.run(DF_COMMAND).output)}
    wrong: List[str] = []
    unreadable: List[str] = []
    checked: Dict[str, str] = {}

    for entry in expected:
        path, server = entry["path"], entry["server"]
        if server.split(".")[0] == ctx.node.short:
            continue
        row = rows.get(path)
        if row is None:
            wrong.append(f"{path}: not mounted")
            continue
        checked[path] = row.source
        if server.split(".")[0] not in row.source:
            wrong.append(f"{path}: mounted from {row.source}, expected {server}")
        # A stale NFS handle hangs rather than failing, so bound this hard.
        try:
            if not ctx.run(["ls", "-d", path], timeout=20).ok:
                unreadable.append(path)
        except TransportError:
            unreadable.append(f"{path} (timed out -- stale NFS handle?)")

    data = {"sources": checked, "wrong": wrong, "unreadable": unreadable}
    if wrong or unreadable:
        return result(ctx, "disk.nfs_from_mgr", Status.FAIL,
                      "NFS mounts from the manager are wrong or unreadable",
                      "\n".join(wrong + unreadable), data, started)
    return result(ctx, "disk.nfs_from_mgr", Status.OK,
                  f"{len(checked)} NFS mount(s) served correctly", "", data, started)


@register("disk.smart", "SMART overall health for every physical device", needs_root=True)
def disk_smart(ctx: CheckContext) -> CheckResult:
    """Ask every block device for its SMART verdict.

    Devices that do not support SMART, or that sit behind a RAID controller
    that hides it, report nothing -- counted as 'unsupported' rather than as a
    failure, because a false alarm on every RAID node would make the check
    useless.
    """
    started = time.monotonic()
    listing = ctx.run(["lsblk", "-dn", "-o", "NAME,TYPE"], root=True)
    devices = [line.split()[0] for line in listing.lines()
               if len(line.split()) >= 2 and line.split()[1] == "disk"]
    if not devices:
        # lsblk without -o TYPE support, or an unusual layout: fall back to
        # whatever lsblk did print rather than reporting no disks at all.
        devices = [line.split()[0] for line in listing.lines() if line.split()]
    if not devices:
        return result(ctx, "disk.smart", Status.UNKNOWN,
                      "no block devices found", listing.output.strip(), {}, started)

    failed: List[str] = []
    unsupported: List[str] = []
    passed: List[str] = []
    for dev in devices:
        res = ctx.run(["smartctl", "-H", f"/dev/{dev}"], root=True, timeout=45)
        verdict = parse_smart_health(res.output)
        if verdict is None:
            unsupported.append(dev)
        elif verdict:
            passed.append(dev)
        else:
            failed.append(dev)

    data = {"passed": passed, "failed": failed, "unsupported": unsupported}
    if failed:
        return result(ctx, "disk.smart", Status.FAIL,
                      f"SMART reports failure on {', '.join(failed)}",
                      "replace before returning the node to service", data, started)
    summary = f"{len(passed)} device(s) pass SMART"
    if unsupported:
        summary += f", {len(unsupported)} without SMART data"
    return result(ctx, "disk.smart", Status.OK, summary, "", data, started)


@register("disk.errors", "no storage errors in the kernel log since boot", needs_root=True)
def disk_errors(ctx: CheckContext) -> CheckResult:
    """Scan this boot's kernel log for I/O and filesystem errors.

    ``journalctl -b`` limits the scan to the current boot, which after a
    power-on is exactly the window that matters: errors from before the outage
    are history, errors since the machine came back are the ones that say a
    disk did not survive it.
    """
    started = time.monotonic()
    res = ctx.run(["journalctl", "-k", "-b", "--no-pager", "-p", "err"],
                  root=True, timeout=60)
    if not res.ok:
        res = ctx.run(["dmesg", "-l", "err,crit,alert,emerg"], root=True, timeout=60)
    hits = find_disk_errors(res.output)
    data = {"errors": hits[:50], "count": len(hits)}
    if hits:
        return result(ctx, "disk.errors", Status.FAIL,
                      f"{len(hits)} storage error(s) in this boot's kernel log",
                      "\n".join(hits[:10]), data, started)
    return result(ctx, "disk.errors", Status.OK,
                  "no storage errors since boot", "", data, started)


@register("disk.raid", "md/RAID arrays are complete and not rebuilding", needs_root=True)
def disk_raid(ctx: CheckContext) -> CheckResult:
    started = time.monotonic()
    res = ctx.run(["cat", "/proc/mdstat"], root=True)
    arrays = parse_mdstat(res.output)
    if not arrays:
        return result(ctx, "disk.raid", Status.SKIP,
                      "no md arrays on this host", "", {"arrays": []}, started)
    bad = [a for a in arrays if not a.healthy]
    data = {"arrays": [{"name": a.name, "state": a.state, "healthy": a.healthy,
                        "detail": a.detail} for a in arrays]}
    if bad:
        return result(ctx, "disk.raid", Status.FAIL,
                      f"{len(bad)} of {len(arrays)} array(s) degraded or rebuilding",
                      "\n".join(f"{a.name}: {a.state} {a.detail}" for a in bad),
                      data, started)
    return result(ctx, "disk.raid", Status.OK,
                  f"{len(arrays)} array(s) healthy", "", data, started)
