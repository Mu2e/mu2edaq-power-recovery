"""mu2e-ipmi-tool -- issue one IPMI command through a DAQ gateway.

The equivalent of mu2edaq-operations/scripts/mu2e_ipmi.sh, with three
differences that matter during a recovery:

* the BMC password comes from Vault and is delivered on stdin, so it is in no
  process table on either host;
* destructive verbs are refused for the protected hosts (gateways, mu2e-mgr-01,
  mu2e-dcs-01) before the command is built;
* state-changing verbs require --execute, so a mistyped host name in a hurry
  reads a power state instead of changing one.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional, Sequence

from .. import console
from ..creds import VaultCredentials, VaultError
from ..transport import IPMIClient, LocalTransport, SSHFactory
from ..transport.base import TransportError
from ..transport.ipmi import DESTRUCTIVE_VERBS, STATE_CHANGING_VERBS
from ._common import add_common_arguments, bootstrap


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
    parser.add_argument("--show-command", action="store_true",
                        help="print the exact ipmitool invocation and exit, "
                             "for comparison against a known-working one. It "
                             "contains no password.")
    parser.add_argument("--execute", action="store_true",
                        help="really issue a state-changing verb "
                             "(power on/off/cycle/reset)")
    parser.add_argument("--yes", action="store_true",
                        help="do not ask for confirmation before a destructive verb")
    return add_common_arguments(parser)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    settings, topology = bootstrap(args)

    location = args.location or (settings.get("topology.locations") or ["mc2"])[0]
    location = topology.canonical_location(location)

    # --- targets ----------------------------------------------------------
    if args.node:
        nodes = topology.resolve(args.node, [location])
    elif args.node_class:
        wanted = {c.lower() for c in args.node_class}
        nodes = [n for n in topology.all_nodes([location])
                 if n.node_class.lower() in wanted]
    else:
        nodes = [n for n in topology.all_nodes([location]) if n.ipmi_host]
    nodes = [n for n in nodes if n.ipmi_host] or nodes
    if not nodes:
        print("error: no target nodes with a BMC", file=sys.stderr)
        return 2

    verb = args.command[-1].lower() if len(args.command) >= 3 else ""
    destructive = verb in DESTRUCTIVE_VERBS and args.command[0].lower() == "chassis"
    changing = verb in STATE_CHANGING_VERBS and args.command[0].lower() == "chassis"

    if changing and not args.execute:
        print(f"  '{' '.join(args.command)}' changes machine state; re-run with "
              f"--execute to issue it. Showing intent only.\n")
    if destructive and args.execute and not args.yes:
        listed = ", ".join(n.short for n in nodes[:10])
        more = f" and {len(nodes) - 10} more" if len(nodes) > 10 else ""
        answer = input(f"  About to '{' '.join(args.command)}' on {len(nodes)} "
                       f"host(s): {listed}{more}\n  Type 'yes' to proceed: ")
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

    factory = SSHFactory(settings, topology, local=local)
    gateway_host = args.gateway or factory.gateway_for(location)
    if not gateway_host:
        print(f"error: no gateway for {location} answered ssh; ipmitool cannot "
              f"be run", file=sys.stderr)
        return 2
    username = args.user or creds.username
    if not args.quiet:
        # The username is not a secret and is the first thing to check when a
        # BMC refuses the session, so say it rather than making the operator
        # go and look it up.
        print(f"  running ipmitool on {gateway_host} as BMC user "
              f"'{username}' (credentials from {creds.source})\n")

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
    )

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
                        "refused": result.meta.get("refused", False),
                        "dry_run": result.meta.get("dry_run", False),
                        "diagnosis": result.meta.get("diagnosis"),
                        "invocation": result.meta.get("invocation")})
        if not result.ok:
            failures += 1

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print(console.table(rows, ["NODE", "BMC", "RESULT", "OUTPUT"]))
        print(f"\n  {len(rows) - failures}/{len(rows)} succeeded")
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


if __name__ == "__main__":
    sys.exit(main())
