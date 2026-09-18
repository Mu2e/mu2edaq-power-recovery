"""mu2e-ssh-probe -- show, or run, the exact ssh command used for a node.

When a check reports "could not reach the node", the next question is always
what command was actually issued, and through which gateway.  This prints that
command verbatim, so it can be copied into a shell and debugged by hand -- and
with --run, executes it and shows what came back.
"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
from typing import List, Optional, Sequence

from .. import console
from ..creds import KerberosManager
from ..transport import LocalTransport, SSHFactory
from ..transport.base import TransportError
from ._common import add_common_arguments, bootstrap


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mu2e-ssh-probe",
        description="Show or run the ssh command the recovery tools use for a node.",
        epilog="""
examples
  # what would be run, without running it
  mu2e-ssh-probe mu2e-trk-03

  # run a command as root through the gateway, as the tools would
  mu2e-ssh-probe mu2e-trk-03 --root --run 'mountpoint -q /home; echo $?'

  # check every tracker node answers at all
  mu2e-ssh-probe -c tracker --run true
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("nodes", nargs="*", metavar="HOST",
                        help="nodes to probe (short or full names)")
    parser.add_argument("-c", "--class", dest="node_class", action="append",
                        metavar="CLASS", help="probe every node of this class")
    parser.add_argument("-l", "--location", metavar="NAME",
                        help="location to resolve names in")
    parser.add_argument("--root", action="store_true",
                        help="use the root login (ssh.root_user)")
    parser.add_argument("--run", metavar="COMMAND",
                        help="actually run this command and show the result")
    parser.add_argument("--timeout", type=int, metavar="SECONDS",
                        help="override the command timeout")
    parser.add_argument("--no-chain", action="store_true",
                        help="use only the ambient ticket, instead of the full "
                             "credential chain a real run would try")
    return add_common_arguments(parser)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    settings, topology = bootstrap(args)

    locations = [args.location] if args.location else \
        settings.get("topology.locations", topology.locations)
    if args.nodes:
        nodes = topology.resolve(args.nodes, locations)
    elif args.node_class:
        wanted = {c.lower() for c in args.node_class}
        nodes = [n for n in topology.all_nodes(locations)
                 if n.node_class.lower() in wanted]
    else:
        print("error: name at least one node, or use --class", file=sys.stderr)
        return 2

    local = LocalTransport(default_timeout=settings.get("ssh.command_timeout", 120))
    # With the credential chain, because the point of this tool is to reproduce
    # what a real run does. Without it the probe tests only the ambient ticket
    # and ssh_config's login, which is how a probe can succeed against a host
    # the actual run cannot reach -- exactly the confusion it exists to prevent.
    kerberos = None if args.no_chain else KerberosManager(settings, local=local)
    if kerberos is not None:
        warning = kerberos.ambient_warning()
        if warning:
            print(f"  note: {warning}\n")
    factory = SSHFactory(settings, topology, local=local, kerberos=kerberos)
    command = args.run or "true"

    rows: List[List[str]] = []
    payload = []
    failures = 0
    for node in nodes:
        transport = factory.for_node(node, root=args.root)
        argv_list = transport.argv(command)
        entry = {"node": node.hostname, "jump": transport.jump,
                 "user": transport.user, "argv": argv_list,
                 "command": " ".join(shlex.quote(a) for a in argv_list)}
        if args.run:
            try:
                result = transport.run(command, timeout=args.timeout)
                entry.update({"rc": result.rc, "stdout": result.stdout.strip(),
                              "stderr": result.stderr.strip(),
                              "credential": result.meta.get("credential"),
                              "duration": round(result.duration, 2)})
                rows.append([node.short, transport.jump or "(direct)",
                             f"rc={result.rc}",
                             result.meta.get("credential") or "ambient",
                             (result.output.strip().splitlines() or [""])[0][:52]])
                if not result.ok:
                    failures += 1
            except TransportError as exc:
                entry["rc"] = None
                entry["error"] = str(exc)
                entry["attempts"] = list(getattr(transport, "attempts", []))
                rows.append([node.short, transport.jump or "(direct)", "ERROR",
                             "-", str(exc)[:52]])
                failures += 1
                # Say which login/ticket pairs were refused and why, rather
                # than one opaque error for the whole chain.
                for attempt in entry["attempts"]:
                    rows.append(["", "", f"  {attempt['reason']}",
                                 attempt.get("credential", "?"),
                                 (attempt.get("detail") or "")[:52]])
        else:
            rows.append([node.short, transport.jump or "(direct)", "",
                         entry["command"]])
            for credential in getattr(transport, "credentials", []) or []:
                rows.append(["", "", "", f"  would try: {credential.describe()}"])
        payload.append(entry)

    if args.json:
        print(json.dumps(payload, indent=2))
        return 1 if failures else 0

    headers = ["NODE", "VIA", "RESULT", "CREDENTIAL", "OUTPUT"] if args.run \
        else ["NODE", "VIA", "", "COMMAND"]
    print(console.table(rows, headers))
    if args.run:
        # Counted over nodes, not table rows: a failed node contributes extra
        # rows describing each refused credential.
        print(f"\n  {len(payload) - failures}/{len(payload)} succeeded")
    else:
        print("\n  nothing was run. Add --run COMMAND to execute.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
