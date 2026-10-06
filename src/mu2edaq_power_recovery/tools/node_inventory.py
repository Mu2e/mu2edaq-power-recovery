"""mu2e-node-inventory -- print the node inventory the tools will use.

The first question when a run reports something odd is "did it even know about
that machine".  This answers it without contacting anything, and it reads the
same topology file the phases do, so its answer is the phases' answer.
"""
from __future__ import annotations

import argparse
import json
import sys
import textwrap
from typing import List, Optional, Sequence

from .. import console
import yaml

from ..topology import LEVELS, Finding, TopologyError
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
  mu2e-node-inventory --validate               # gaps and inconsistencies

--validate exits 1 when there is an error finding, 0 when there are only
warnings or nothing; it checks every location, not only topology.locations.
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
    parser.add_argument("--validate", action="store_true",
                        help="check the inventory and power sequence (empty "
                             "locations, BMCs with no lab host, invalid names, "
                             "subnets shared between locations, gateways or "
                             "protected hosts missing from the inventory, nodes "
                             "in no power-sequence stage). Exit 1 on errors.")
    return add_common_arguments(parser)


def validate(args: argparse.Namespace) -> int:
    """--validate: findings as a table or JSON; exit 1 on any error."""
    findings: List[Finding] = []
    source = None
    try:
        settings, topology = bootstrap(args)
        source = str(topology.source)
    except TopologyError as exc:
        findings.append(Finding("error", f"the topology does not load: {exc}"))
    else:
        sequence = None
        path = settings.config_path("topology.sequence_file")
        try:
            with open(path) as fh:
                sequence = yaml.safe_load(fh) or {}
        except FileNotFoundError:
            findings.append(Finding("info", f"no power sequence at {path}; stage "
                                            f"checks skipped"))
        except yaml.YAMLError as exc:
            findings.append(Finding("error", f"{path} is not valid YAML: {exc}"))
        findings.extend(topology.validate(sequence))

    counts = {level: sum(1 for f in findings if f.level == level)
              for level in LEVELS}
    code = 1 if counts["error"] else 0
    if args.json:
        print(json.dumps({"topology": source, "findings":
                          [f.as_dict() for f in findings],
                          "counts": counts, "ok": code == 0}, indent=2))
        return code
    # One finding per wrapped paragraph: the messages are sentences, and a
    # table column would truncate the part that names the hosts.
    for finding in findings:
        print(textwrap.fill(finding.message, width=100,
                            initial_indent=f"  {finding.level.upper():<8} ",
                            subsequent_indent=" " * 11))
    print(f"\n  {counts['error']} error(s), {counts['warning']} warning(s), "
          f"{counts['info']} note(s) in {source or 'the topology'}")
    return code


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.validate:
        return validate(args)
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
