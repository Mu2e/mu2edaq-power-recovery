"""Reachability and login checks -- the ones whose failure explains the rest."""
from __future__ import annotations

import time
from typing import Optional

from ..transport.base import TransportError
from .base import CheckContext, CheckResult, Status, register, result
from .parsers import parse_ping


def _ping_command(target: str, count: int, timeout: int,
                  payload: Optional[int] = None) -> str:
    """A ping command that works on both iputils (Linux) and BSD ping.

    ``-c`` and ``-W`` are common; ``-W`` means seconds on Linux and is a
    per-packet timeout, which is what we want.  The payload/DF options are only
    added for the MTU probe, where they are Linux-specific -- the gateways this
    runs on are Linux, so that is safe.
    """
    cmd = f"ping -c {count} -W {timeout} -q"
    if payload is not None:
        cmd += f" -M do -s {payload}"
    return f"{cmd} {target}"


@register("ping.lab", "ICMP reachability on the lab network, probed from the gateway")
def ping_lab(ctx: CheckContext) -> CheckResult:
    started = time.monotonic()
    target = ctx.node.networks.get("lab", ctx.node.hostname)
    count = int(ctx.threshold("ping_count", 3))
    timeout = int(ctx.threshold("ping_timeout_s", 5))

    res = ctx.probe(_ping_command(target, count, timeout), timeout=count * timeout + 10)
    stats = parse_ping(res.output)
    data = {"target": target, "probed_from": ctx.prober.host, **stats.as_dict()}

    if not stats.alive:
        return result(ctx, "ping.lab", Status.FAIL,
                      f"{target} does not answer ICMP",
                      f"{stats.transmitted} sent, 0 received "
                      f"(probed from {ctx.prober.host})", data, started)
    if stats.loss_pct > 0:
        return result(ctx, "ping.lab", Status.WARN,
                      f"{target} answers with {stats.loss_pct:.0f}% packet loss",
                      f"rtt avg {stats.rtt_avg_ms} ms", data, started)
    return result(ctx, "ping.lab", Status.OK,
                  f"{target} answers ({stats.rtt_avg_ms} ms avg)", "", data, started)


@register("ssh.login", "an ordinary SSH session can be opened")
def ssh_login(ctx: CheckContext) -> CheckResult:
    started = time.monotonic()
    try:
        res = ctx.run(["id", "-un"])
    except TransportError as exc:
        return result(ctx, "ssh.login", Status.FAIL,
                      "SSH login failed", str(exc), {"user": None}, started)
    who = res.output.strip()
    if not res.ok:
        return result(ctx, "ssh.login", Status.FAIL,
                      f"SSH session opened but 'id' exited {res.rc}",
                      res.stderr.strip(), {}, started)
    return result(ctx, "ssh.login", Status.OK, f"logged in as {who}", "",
                  {"user": who, "jump": res.meta.get("jump")}, started)


@register("ssh.login_root", "a root SSH session can be opened", needs_root=True)
def ssh_login_root(ctx: CheckContext) -> CheckResult:
    started = time.monotonic()
    try:
        res = ctx.run(["id", "-u"], root=True)
    except TransportError as exc:
        return result(ctx, "ssh.login_root", Status.FAIL,
                      "root SSH login failed", str(exc), {}, started)
    uid = res.output.strip()
    if uid != "0":
        return result(ctx, "ssh.login_root", Status.FAIL,
                      f"root session has uid {uid or '(unknown)'}, expected 0",
                      res.stderr.strip(), {"uid": uid}, started)
    return result(ctx, "ssh.login_root", Status.OK, "root login works", "",
                  {"uid": uid}, started)


@register("login.users", "the mu2edaq and mu2eshift accounts can log in", needs_root=True)
def login_users(ctx: CheckContext) -> CheckResult:
    """Verify the service accounts can actually start a session.

    Project-Description.md calls this out for mu2e-mgr-01 specifically, but it
    applies to every node that mounts /home: after an outage the usual failure
    is not a broken account, it is that /home is not mounted, so the account
    exists, authenticates, and then has no home directory to land in.  Running
    the login through ``su - <user>`` (from root, so no password is needed)
    exercises that whole path rather than just asking whether the account is
    in passwd.
    """
    started = time.monotonic()
    users = ctx.settings.get("kerberos.verify_users", ["mu2edaq", "mu2eshift"])
    outcomes = {}
    failures = []
    for user in users:
        try:
            res = ctx.run(["su", "-", user, "-c", "pwd && id -un"], root=True, timeout=45)
        except TransportError as exc:
            outcomes[user] = f"error: {exc}"
            failures.append(user)
            continue
        lines = res.lines()
        if res.ok and len(lines) >= 2 and lines[0].startswith("/"):
            outcomes[user] = f"ok (home {lines[0]})"
        else:
            outcomes[user] = (res.stderr.strip() or res.stdout.strip()
                              or f"exited {res.rc}")
            failures.append(user)

    data = {"users": outcomes}
    if failures:
        return result(ctx, "login.users", Status.FAIL,
                      f"login failed for {', '.join(failures)}",
                      "\n".join(f"{u}: {v}" for u, v in outcomes.items()), data, started)
    return result(ctx, "login.users", Status.OK,
                  f"{', '.join(users)} can log in", "", data, started)
