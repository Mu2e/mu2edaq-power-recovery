"""mu2e-ssh-probe -- show, or run, the exact ssh command used for a node.

When a check reports "could not reach the node", the next question is always
what command was actually issued, and through which gateway.  This prints that
command verbatim, so it can be copied into a shell and debugged by hand -- and
with --run, executes it and shows what came back.

Credentials come from the same bootstrap the run uses (creds/bootstrap.py):
with --run the designated principals are acquired into private caches and the
service fallbacks minted before any node is contacted, and every private cache
is destroyed on the way out. Without --run nothing is acquired or minted; the
candidates a run would try are listed, and any not yet acquired says so.
"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
from typing import List, Optional, Sequence

from .. import console
from ..cli import install_sigterm_handler
from ..creds import KerberosError
from ..creds.bootstrap import CredentialSession, credential_session
from ..topology import TopologyError
from ..transport import LocalTransport
from ..transport.base import TransportError
from ._common import add_common_arguments, add_credential_arguments, bootstrap


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
    add_credential_arguments(parser)
    return add_common_arguments(parser)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    # SIGTERM takes the same path as Ctrl-C, so the private caches are
    # destroyed by credential_session's finally rather than left behind.
    install_sigterm_handler()
    try:
        return probe(args)
    except KerberosError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n  interrupted; the private Kerberos caches have been "
              "destroyed.", file=sys.stderr)
        return 3


def probe(args: argparse.Namespace) -> int:
    settings, topology = bootstrap(args)

    locations = [args.location] if args.location else \
        settings.get("topology.locations", topology.locations)
    if args.nodes:
        try:
            nodes = topology.resolve(args.nodes, locations)
        except TopologyError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    elif args.node_class:
        wanted = {c.lower() for c in args.node_class}
        nodes = [n for n in topology.all_nodes(locations)
                 if n.node_class.lower() in wanted]
    else:
        print("error: name at least one node, or use --class", file=sys.stderr)
        return 2

    local = LocalTransport(default_timeout=settings.get("ssh.command_timeout", 120))
    if args.no_chain and (settings.get("kerberos.principal") or
                          settings.get("kerberos.root_principal")):
        print("  note: --no-chain uses the ambient ticket only; the designated "
              "principal(s) are ignored.\n")
    # With the credential chain, because the point of this tool is to reproduce
    # what a real run does. Without it the probe tests only the ambient ticket
    # and ssh_config's login, which is how a probe can succeed against a host
    # the actual run cannot reach -- exactly the confusion it exists to prevent.
    # Show-only acquires nothing: describing a command is no reason to prompt
    # for a password or mint seven service tickets.
    with credential_session(settings, topology, local,
                            chain=not args.no_chain,
                            prepare=bool(args.run)) as session:
        if session.warning:
            print(f"  note: {session.warning}\n")
        for note in session.notes:
            if note != session.warning:
                print(f"  note: {note}\n")
        return probe_nodes(args, nodes, session)


def probe_nodes(args: argparse.Namespace, nodes: Sequence,
                session: CredentialSession) -> int:
    """Show, or run, the command for each node under *session*."""
    factory = session.factory
    kerberos = session.kerberos
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
            # Candidates, not the transport's chain: nothing was minted, so
            # the transport holds only what exists, and the listing should
            # show what a run would try -- each unacquired one marked as such.
            candidates = kerberos.chain(root=args.root, mint=False,
                                        candidates=True) \
                if kerberos is not None else []
            entry["would_try"] = [c.as_dict() for c in candidates]
            for credential in candidates:
                # The acquired/not-acquired mark leads, because the table
                # truncates the end of the line on a narrow terminal.
                mark = "would try (not acquired)" if credential.pending \
                    else "would try"
                rows.append(["", "", "", f"  {mark}: {credential.describe()}"])
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
