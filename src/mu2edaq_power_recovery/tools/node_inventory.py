"""mu2e-node-inventory -- print the node inventory the tools will use.

The first question when a run reports something odd is "did it even know about
that machine".  This answers it without contacting anything, and it reads the
same topology file the phases do, so its answer is the phases' answer.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional, Sequence

from .. import console
from ..topology import TopologyError
from ._common import add_common_arguments, bootstrap


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mu2e-node-inventory",
        description="Print the Mu2e DAQ node inventory used by the power-recovery tools.",
        epilog="""
examples
  mu2e-node-inventory                          # every configured location
  mu2e-node-inventory -l mc2 -c tracker        # MC-2 tracker nodes only
  mu2e-node-inventory -n ipmi --hostnames      # BMC names, one per line
  mu2e-node-inventory --json > inventory.json  # machine readable
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-l", "--location", action="append", metavar="NAME",
                        help="limit to this location (mc2, mc1, teststand). "
                             "Repeatable.")
    parser.add_argument("-c", "--class", dest="node_class", action="append",
                        metavar="CLASS",
                        help="limit to this host class (gateway, tracker, ...). "
                             "Repeatable.")
    parser.add_argument("-n", "--network", metavar="NAME",
                        help="print this network's interface names rather than "
                             "the lab names")
    parser.add_argument("--hostnames", action="store_true",
                        help="print bare hostnames, one per line, for piping "
                             "into pssh or a shell loop")
    parser.add_argument("--protected", action="store_true",
                        help="list only the hosts protected from destructive "
                             "IPMI commands")
    return add_common_arguments(parser)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings, topology = bootstrap(args)
    except TopologyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    locations = args.location or settings.get("topology.locations", topology.locations)
    try:
        nodes = topology.all_nodes(locations)
    except TopologyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.node_class:
        wanted = {c.lower() for c in args.node_class}
        nodes = [n for n in nodes if n.node_class.lower() in wanted]
    if args.protected:
        nodes = [n for n in nodes if n.protected]
    if args.network:
        nodes = [n for n in nodes if n.has_network(args.network)]

    if args.json:
        print(json.dumps([n.as_dict() for n in nodes], indent=2))
        return 0

    if args.hostnames:
        for node in nodes:
            print(node.networks.get(args.network, node.hostname)
                  if args.network else node.hostname)
        return 0

    rows: List[List[str]] = []
    for node in nodes:
        rows.append([
            node.short,
            node.node_class,
            node.location,
            ",".join(sorted(node.networks)),
            node.ipmi_host or "-",
            "protected" if node.protected else "",
        ])
    print(console.table(rows, ["NODE", "CLASS", "LOCATION", "NETWORKS", "BMC", ""]))
    print(f"\n  {len(nodes)} node(s) from {topology.source}")
    for location in locations:
        try:
            if not topology.nodes(location):
                print(f"  note: location '{location}' has no nodes configured")
        except TopologyError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
