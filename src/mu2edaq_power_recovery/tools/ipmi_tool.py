"""mu2e-ipmi-tool -- issue one IPMI command through a DAQ gateway.

The equivalent of mu2edaq-operations/scripts/mu2e_ipmi.sh, with three
differences that matter during a recovery:

* the BMC password comes from Vault and is delivered on stdin, so it is in no
  process table on either host;
* destructive verbs are refused for the protected hosts (gateways, mu2e-mgr-01,
  mu2e-dcs-01) before the command is built;
* state-changing verbs require --execute, so a mistyped host name in a hurry
  reads a power state instead of changing one.

The gateway session uses the recovery run's own credential bootstrap
(creds/bootstrap.py): the operator's principal first, then the service
fallbacks, with the run's private caches destroyed on every exit path. A
diagnostic that logged in differently from the run could pass or fail where
the run would not.
"""
from __future__ import annotations

import argparse
import json
import sys
from contextlib import ExitStack
from typing import Any, List, Optional, Sequence, Tuple

from .. import console
from ..cli import install_sigterm_handler
from ..creds import KerberosError, VaultCredentials, VaultError
from ..creds.bootstrap import credential_session
from ..transport import IPMIClient, LocalTransport
from ..transport.base import TransportError
from ..transport.ipmi import DESTRUCTIVE_VERBS, STATE_CHANGING_VERBS
from ._common import add_common_arguments, add_credential_arguments, bootstrap

#: Output cap for this tool's own invocations, raised from the run's
#: logging.max_capture_bytes so a full SEL listing arrives whole.
TOOL_MAX_CAPTURE = 8 * 1024 * 1024


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mu2e-ipmi-tool",
        description="Run an ipmitool command against DAQ BMCs, from a gateway.",
        epilog="""
examples
  # read the power state of every tracker node
  mu2e-ipmi-tool -c tracker chassis power status

  # sensors on one node
  mu2e-ipmi-tool -n mu2e-trk-03 sdr elist

  # actually switch a node on (without --execute this only reports intent)
  mu2e-ipmi-tool -n mu2e-trk-03 --execute chassis power on

notes
  ipmitool runs ON the gateway: the IPMI subnets are not routable from off site.
  'chassis power off', 'cycle' and 'reset' are refused for protected hosts
  regardless of --execute; see protected: in config/topology.yaml.
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", nargs="+", metavar="ARG",
                        help="the ipmitool command, e.g. chassis power status")
    parser.add_argument("-n", "--node", action="append", metavar="HOST",
                        help="target node (lab name, short or full). Repeatable.")
    parser.add_argument("-c", "--class", dest="node_class", action="append",
                        metavar="CLASS", help="target every node of this class")
    parser.add_argument("-l", "--location", metavar="NAME", default=None,
                        help="location whose gateway is used and whose nodes "
                             "are targeted (default: the first configured)")
    parser.add_argument("--gateway", metavar="HOST",
                        help="force a particular gateway to run ipmitool on")
    parser.add_argument("--user", metavar="NAME",
                        help="override the BMC username from Vault. The "
                             "upstream mu2e_ipmi.sh hard-codes MU2E; use this "
                             "to test whether the Vault username differs.")
    parser.add_argument("--diagnose", action="store_true",
                        help="find a working username and cipher suite against "
                             "one BMC by trying the plausible combinations, "
                             "read-only, stopping at the first that works")
    parser.add_argument("--show-command", action="store_true",
                        help="print the exact ipmitool invocation and exit, "
                             "for comparison against a known-working one. It "
                             "contains no password.")
    parser.add_argument("--execute", action="store_true",
                        help="really issue a state-changing verb "
                             "(power on/off/cycle/reset)")
    parser.add_argument("--yes", action="store_true",
                        help="do not ask for confirmation before a destructive verb")
    add_credential_arguments(parser)
    return add_common_arguments(parser)


def main(argv: Optional[Sequence[str]] = None) -> int:
    # SIGTERM takes the same path as Ctrl-C, so the private caches are
    # destroyed by credential_session's finally rather than left behind.
    install_sigterm_handler()
    try:
        with ExitStack() as credentials:
            return run(argv, credentials)
    except KerberosError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n  interrupted; the private Kerberos caches have been "
              "destroyed.", file=sys.stderr)
        return 3


def run(argv: Optional[Sequence[str]], credentials: ExitStack) -> int:
    """The tool proper; *credentials* holds the credential session open."""
    args = build_parser().parse_args(argv)
    settings, topology = bootstrap(args)

    location = args.location or (settings.get("topology.locations") or ["mc2"])[0]
    location = topology.canonical_location(location)

    # --- targets ----------------------------------------------------------
    # Everything up to here touches neither Vault nor a gateway, so a
    # selection with nothing valid in it stops before either is contacted.
    from ..topology import TopologyError

    try:
        nodes, skipped = select_targets(topology, location, args.node,
                                        args.node_class)
    except TopologyError as exc:
        # An invalid -n name (one that fails valid_hostname) is an operator
        # typo, not a crash: say so and exit as for any other bad selection.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for node, reason in skipped:
        print(f"  skipping {node.short}: {reason}", file=sys.stderr)
    if not nodes:
        print("error: no target nodes with a BMC", file=sys.stderr)
        return 2

    verb = args.command[-1].lower() if len(args.command) >= 3 else ""
    destructive = verb in DESTRUCTIVE_VERBS and args.command[0].lower() == "chassis"
    changing = verb in STATE_CHANGING_VERBS and args.command[0].lower() == "chassis"

    if changing and not args.execute:
        print(f"  '{' '.join(args.command)}' changes machine state; re-run with "
              f"--execute to issue it. Showing intent only.\n")
    if changing:
        # Every target, by name: an operator confirming a state change must
        # see exactly which machines it will reach, not a count.
        out = sys.stderr if args.json else sys.stdout
        print(f"  targets for '{' '.join(args.command)}' ({len(nodes)}):",
              file=out)
        print(target_listing(nodes, topology, destructive) + "\n", file=out)
    if destructive and args.execute and not args.yes:
        answer = input(f"  About to '{' '.join(args.command)}' on the "
                       f"{len(nodes)} host(s) listed above.\n"
                       f"  Type 'yes' to proceed: ")
        if answer.strip().lower() != "yes":
            print("  aborted")
            return 3

    # --- credentials and gateway -----------------------------------------
    local = LocalTransport(default_timeout=settings.get("ssh.command_timeout", 120))
    try:
        creds = VaultCredentials(settings, local=local).ipmi()
    except VaultError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # The run's own bootstrap. --show-command acquires and mints nothing:
    # printing an invocation is no reason to prompt for a password. warm is
    # off because this is one session, not a worker pool -- a lazy mint under
    # the manager's lock is the same transaction.
    # A full 'sel list' is the read this tool exists for (power.sel tells the
    # operator to run it), and a BMC's log runs to hundreds of KiB: under the
    # run's 64 KiB capture cap the middle was silently cut, splitting a row.
    cap = max(int(settings.get("logging.max_capture_bytes", 65536)),
              TOOL_MAX_CAPTURE)
    settings.set("logging.max_capture_bytes", cap)
    # Also the shared local transport, built before this: an ambient-ticket
    # ssh runs through it, not through a runner sized by the setting.
    local.max_capture = cap
    session = credentials.enter_context(credential_session(
        settings, topology, local, prepare=not args.show_command, warm=False))
    if session.warning and not args.quiet:
        print(f"  note: {session.warning}\n", file=sys.stderr)
    factory = session.factory
    gateway_host = args.gateway or factory.gateway_for(location, role="ipmi")
    if not gateway_host:
        print(f"error: no gateway for {location} answered ssh; ipmitool cannot "
              f"be run", file=sys.stderr)
        return 2
    username = args.user or creds.username
    if not args.quiet:
        # The username is not a secret and is the first thing to check when a
        # BMC refuses the session, so say it rather than making the operator
        # go and look it up.
        # stderr, so --json stdout is one parseable document.
        print(f"  running ipmitool on {gateway_host} as BMC user "
              f"'{username}' (credentials from {creds.source})\n", file=sys.stderr)

    client = IPMIClient(
        gateway=factory.for_host(gateway_host, direct=True),
        username=username, password=creds.password,
        tool=settings.get("ipmi.tool", "ipmitool"),
        interface=settings.get("ipmi.interface", "lanplus"),
        privilege=settings.get("ipmi.privilege", "Operator"),
        cipher_suite=settings.get("ipmi.cipher_suite", 3),
        timeout=settings.get("ipmi.timeout", 10),
        retries=settings.get("ipmi.retries", 2),
        dry_run=not args.execute,
        protected=topology.is_protected,
        message_timeout=settings.get("ipmi.message_timeout"),
        tool_retries=settings.get("ipmi.tool_retries"),
        extra_args=settings.get("ipmi.extra_args", []),
        # As in the run: a refusal from the first BMC stops the rest of a
        # -c/-l sweep being sent the same rejected credential.
        stop_on_auth_failure=bool(
            settings.get("ipmi.stop_on_auth_failure", True)),
        reachability_precheck=bool(
            settings.get("ipmi.reachability_precheck", True)),
    )

    if args.diagnose:
        return diagnose(settings, topology, factory, gateway_host, creds,
                        nodes[0], username)

    if args.show_command:
        print("  the invocation run on the gateway. It carries no password --\n"
              "  ipmitool reads IPMI_PASSWORD from the environment:\n")
        for node in nodes:
            print(f"    {client.describe(node.ipmi_host, list(args.command))}")
        print(f"\n  BMC username in use : {client.username}")
        print(f"  credential source   : {creds.source}")
        print("\n  the known-working upstream form, for comparison:")
        print("    ipmitool -I lanplus -H <bmc> -UMU2E -P<password> "
              "-L Operator -C 3 -e^ <args>")
        return 0

    # --- run --------------------------------------------------------------
    results = []
    rows: List[List[str]] = []
    failures = 0
    for node in nodes:
        try:
            if changing:
                result = client.power(node.ipmi_host, verb, node_host=node.hostname)
            else:
                result = client._run(node.ipmi_host, list(args.command))
        except TransportError as exc:
            rows.append([node.short, node.ipmi_host or "-", "ERROR", str(exc)[:80]])
            failures += 1
            continue
        first_line = (result.output.strip().splitlines() or [""])[0]
        rows.append([node.short, node.ipmi_host or "-",
                     "ok" if result.ok else f"rc={result.rc}",
                     first_line[:100]])
        results.append({"node": node.hostname, "bmc": node.ipmi_host,
                        "rc": result.rc, "output": result.output.strip(),
                        "truncated": result.truncated,
                        "refused": result.meta.get("refused", False),
                        "dry_run": result.meta.get("dry_run", False),
                        "diagnosis": result.meta.get("diagnosis"),
                        "invocation": result.meta.get("invocation")})
        if result.truncated:
            print(f"  warning: output from {node.short} exceeded "
                  f"{settings.get('logging.max_capture_bytes')} bytes and was cut "
                  f"in the middle", file=sys.stderr)
        if not result.ok:
            failures += 1

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print(console.table(rows, ["NODE", "BMC", "RESULT", "OUTPUT"]))
        print(f"\n  {len(rows) - failures}/{len(rows)} succeeded")
        if client.credentials_refused:
            # The rest of the sweep was stopped, not failed: say why once.
            print(f"\n  stopped: {client.credentials_refused}")
        # One diagnosis, not one per node: a credential or cipher-suite problem
        # hits every BMC identically, and repeating it fifty times helps nobody.
        for entry in results:
            if entry.get("diagnosis"):
                print(f"\n  {entry['diagnosis']}")
                print(f"\n  BMC username in use: {client.username}")
                print(f"  invocation: {entry['invocation']}")
                short = entry["node"].split(".")[0]
                print(f"\n  to compare against the upstream form:"
                      f"\n    mu2e-ipmi-tool --show-command -n {short} "
                      f"chassis power status")
                break
    return 1 if failures else 0


def select_targets(topology: Any, location: str,
                   names: Optional[Sequence[str]],
                   classes: Optional[Sequence[str]]
                   ) -> Tuple[List[Any], List[Tuple[Any, str]]]:
    """The nodes to address, and the selected nodes that cannot be.

    Returns ``(valid, skipped)``: *valid* have a BMC; *skipped* pairs each
    other selected node with why. There is no fallback to the unfiltered
    selection -- a node without a BMC has no address to give ipmitool, and
    the old fallback built ``ipmitool -H None``. With neither *names* nor
    *classes*, every node of *location* that has a BMC is selected and none
    is reported as skipped: nobody asked for the others.
    """
    if names:
        selected = topology.resolve(list(names), [location])
    elif classes:
        wanted = {c.lower() for c in classes}
        selected = [n for n in topology.all_nodes([location])
                    if n.node_class.lower() in wanted]
    else:
        return [n for n in topology.all_nodes([location]) if n.ipmi_host], []

    valid: List[Any] = []
    skipped: List[Tuple[Any, str]] = []
    for node in selected:
        if node.ipmi_host:
            valid.append(node)
        elif node.location == "unknown":
            # Topology.resolve() keeps a name it does not know, for the ssh
            # tools' sake; here it has no BMC to address.
            skipped.append((node, f"unknown host: not in the topology for "
                                  f"location {location}"))
        else:
            skipped.append((node, "no BMC: the topology lists no ipmi "
                                  "interface for it"))
    return valid, skipped


def target_listing(nodes: Sequence[Any], topology: Any,
                   destructive: bool) -> str:
    """One line per target: hostname and BMC, marking protected refusals."""
    lines = []
    for node in nodes:
        mark = ""
        if destructive and topology.is_protected(node.hostname):
            mark = "  (protected: will be refused)"
        lines.append(f"    {node.hostname}  [BMC {node.ipmi_host}]{mark}")
    return "\n".join(lines)


def candidate_usernames(configured: str) -> List[str]:
    """Usernames worth trying, most likely first, without duplicates.

    IPMI usernames are case sensitive. The working upstream invocation
    hard-codes ``MU2E`` while the Vault secret has been seen holding ``mu2e``,
    so the case variants of whatever is configured are the first thing to try.
    """
    ordered = [configured, "MU2E", configured.upper(), configured.lower()]
    seen: List[str] = []
    for name in ordered:
        if name and name not in seen:
            seen.append(name)
    return seen


#: Cipher suites worth trying. 3 is what the upstream script uses; 17 is the
#: default on newer Supermicro BMCs; 0 disables authentication of the payload
#: and is a last resort that tells us the rest of the path works.
CANDIDATE_CIPHERS = (3, 17, 0)

#: Hard cap on authentication attempts. BMCs lock an account after a handful
#: of failures, and an exhaustive sweep would be a good way to lock out the
#: account in the middle of a recovery.
MAX_ATTEMPTS = 9


def diagnose(settings: Any, topology: Any, factory: Any, gateway_host: str,
             creds: Any, node: Any, configured_username: str) -> int:
    """Try plausible username and cipher-suite combinations against one BMC.

    Read-only: every attempt is ``chassis power status``. Stops at the first
    combination that works, and at :data:`MAX_ATTEMPTS` regardless, because
    each failure counts towards the BMC's account lockout.
    """
    from ..transport.ipmi import IPMIUnreachable

    if not node.ipmi_host:
        print(f"error: {node.short} has no BMC in the topology", file=sys.stderr)
        return 2

    usernames = candidate_usernames(configured_username)
    print(f"  diagnosing {node.ipmi_host} via {gateway_host}")
    print(f"  password from {creds.source}")
    print(f"  usernames to try : {', '.join(usernames)}")
    print(f"  cipher suites    : {', '.join(str(c) for c in CANDIDATE_CIPHERS)}")
    print(f"\n  read-only ('chassis power status'), stopping at the first that "
          f"works.\n  Capped at {MAX_ATTEMPTS} attempts: BMCs lock an account "
          f"after repeated failures.\n")

    gateway = factory.for_host(gateway_host, direct=True)
    rows: List[List[str]] = []
    attempts = 0
    winner = None

    for cipher in CANDIDATE_CIPHERS:
        for username in usernames:
            if attempts >= MAX_ATTEMPTS:
                break
            attempts += 1
            client = IPMIClient(
                gateway=gateway, username=username, password=creds.password,
                tool=settings.get("ipmi.tool", "ipmitool"),
                interface=settings.get("ipmi.interface", "lanplus"),
                privilege=settings.get("ipmi.privilege", "Operator"),
                cipher_suite=cipher,
                timeout=settings.get("ipmi.timeout", 10),
                retries=0,          # one shot per combination
                dry_run=True,       # read-only anyway, but be explicit
                protected=topology.is_protected,
                extra_args=settings.get("ipmi.extra_args", []),
                reachability_precheck=bool(
                    settings.get("ipmi.reachability_precheck", True)),
            )
            try:
                res = client._run(node.ipmi_host, ["chassis", "power", "status"])
                ok, detail = res.ok, (res.output.strip().splitlines() or [""])[0]
            except IPMIUnreachable as exc:
                # No credential was tried; another combination cannot help.
                print(f"  {exc}. No combination was tried: the BMC is dark or "
                      f"filters ICMP\n  (set ipmi.reachability_precheck: false "
                      f"for the latter).")
                return 1
            except TransportError as exc:
                ok, detail = False, str(exc)[:60]
            rows.append([username, str(cipher), "OK" if ok else "refused",
                         detail[:60]])
            if ok:
                winner = (username, cipher)
                break
        if winner:
            break

    print(console.table(rows, ["USERNAME", "CIPHER", "RESULT", "OUTPUT"]))

    if not winner:
        print(f"\n  No combination worked in {attempts} attempt(s).")
        print("  The password is the remaining likely cause -- check it with"
              "\n    mu2e-vault-ipmi")
        print("  If the BMC has locked the account, wait for its lockout window"
              "\n  to expire before trying again.")
        return 1

    username, cipher = winner
    print(f"\n  Works: username '{username}', cipher suite {cipher}.")
    changes = []
    if username != configured_username:
        changes.append(f"  ipmi:\n    username: {username}")
    if cipher != settings.get("ipmi.cipher_suite", 3):
        changes.append(f"  ipmi:\n    cipher_suite: {cipher}")
    if changes:
        print("\n  Put this in config/power-recovery.yaml:\n")
        print("\n".join(changes))
        print("\n  (Vault is the source of the username by default; setting it"
              "\n  here overrides that without touching the secret.)")
    else:
        print("  That is what is already configured -- the earlier failure was "
              "transient.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
