"""mu2e-vault-ipmi -- verify the Vault path that holds the BMC credentials.

The IPMI secret lives at ``td/scd/experiments/mu2e/ipmi/config`` -- note
that ``ipmi`` is a folder in the KV tree, not the secret -- with the fields
``username`` and ``password``.

This tool exists to confirm that before an outage rather than during one: the
secret is maintained outside this repository, so its path and fields can change
without anything here noticing until a recovery needs them.

It never prints a password.  It prints which fields exist, which one was
selected, and whether the value is non-empty, which is everything needed to
confirm the configuration is right and nothing that needs protecting.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence

from ..creds import VaultCredentials, VaultError
from ..transport import LocalTransport
from ._common import add_common_arguments, bootstrap


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mu2e-vault-ipmi",
        description="Check the Vault secret holding the Mu2e DAQ BMC credentials.",
        epilog="""
examples
  mu2e-vault-ipmi                  # check the configured ipmi path
  mu2e-vault-ipmi --path ecl       # check another secret under the base path
  mu2e-vault-ipmi --fields         # list the field names only
  mu2e-vault-ipmi --list           # browse the KV tree under the path

notes
  KV v2 paths look like directories. 'ipmi' is a folder holding 'config',
  so the secret is at ipmi/config -- reading the folder returns nothing,
  which looks identical to an empty secret. When a path holds no secret
  this tool lists what is under it and says so.

  No secret value is ever printed. If Vault is unreachable and
  vault.allow_file_fallback is set, the fallback file is reported instead.
  To obtain a token:  vault login -method=ldap -address=<vault.addr>
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--path", metavar="REL",
                        help="secret path relative to vault.base_path "
                             "(default: vault.ipmi_path)")
    parser.add_argument("--addr", metavar="URL", help="override vault.addr")
    parser.add_argument("--fields", action="store_true",
                        help="print only the field names present in the secret")
    parser.add_argument("--list", dest="list_tree", action="store_true",
                        help="list the secrets under the path instead of "
                             "reading it -- use this when a path turns out to "
                             "be a folder rather than a secret")
    parser.add_argument("--no-fallback", action="store_true",
                        help="fail rather than falling back to the local "
                             "password file")
    return add_common_arguments(parser)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    cli = {"vault.addr": args.addr}
    if args.no_fallback:
        cli["vault.allow_file_fallback"] = False
    settings, _topology = bootstrap(args, cli)

    local = LocalTransport(default_timeout=60)
    vault = VaultCredentials(settings, local=local)
    relative = args.path or settings.get("vault.ipmi_path", "ipmi/config")
    full_path = f"{settings.get('vault.kv_mount')}/{vault.base_path}/{relative}"

    report = {
        "addr": vault.addr,
        "path": full_path,
        "fields": [],
        "reachable": False,
        "credentials_usable": False,
        "source": None,
    }

    if args.list_tree:
        # Browse mode: what is actually under this path?
        found = vault.find_secrets(relative)
        siblings = vault.find_secrets("")
        if args.json:
            print(json.dumps({"path": full_path, "secrets": sorted(found),
                              "base_path_tree": sorted(siblings)}, indent=2))
            return 0 if found else 1
        if found:
            print(f"  secrets under {full_path}:")
            for entry in sorted(found):
                print(f"    {entry}")
        else:
            print(f"  nothing under {full_path}")
            if siblings:
                print(f"\n  under {settings.get('vault.kv_mount')}/"
                      f"{vault.base_path} there are:")
                for entry in sorted(siblings):
                    print(f"    {entry}")
        return 0 if found else 1

    try:
        data = vault.read(relative)
        report["reachable"] = True
        report["fields"] = sorted(data)
    except VaultError as exc:
        report["error"] = str(exc)
        # A path with no secret is most often a folder. Say what is in it
        # rather than leaving the operator to guess -- an empty result and a
        # wrong path look identical otherwise.
        report["secrets_under_path"] = sorted(vault.find_secrets(relative))
        report["secrets_under_base"] = sorted(vault.find_secrets(""))

    if args.fields:
        if args.json:
            print(json.dumps(report, indent=2))
        elif report["fields"]:
            print("\n".join(report["fields"]))
        else:
            print(f"no fields at {full_path}")
            print(f"  {report.get('error', 'unknown error')}")
            under = report.get("secrets_under_path") or []
            if under:
                print(f"\n  that path is a folder. Secrets inside it:")
                for entry in under:
                    print(f"    {entry}")
                print(f"\n  set vault.ipmi_path to one of those, e.g.:")
                print(f"    mu2e-vault-ipmi --path {under[0]}")
            elif report.get("secrets_under_base"):
                print(f"\n  secrets under {settings.get('vault.kv_mount')}/"
                      f"{vault.base_path}:")
                for entry in report["secrets_under_base"]:
                    print(f"    {entry}")
        return 0 if report["fields"] else 1

    # Resolve the credentials the way a run would, including the fallback.
    try:
        creds = vault.ipmi()
        report["credentials_usable"] = bool(creds.password)
        report["source"] = creds.source
        report["username"] = creds.username
        report["password_length"] = len(creds.password)
    except VaultError as exc:
        report["error"] = str(exc)

    if args.json:
        print(json.dumps(report, indent=2))
        return 0 if report["credentials_usable"] else 1

    print(f"  Vault address     : {report['addr']}")
    print(f"  Secret path       : {report['path']}")
    print(f"  Reachable         : {'yes' if report['reachable'] else 'NO'}")
    if report["fields"]:
        print(f"  Fields present    : {', '.join(report['fields'])}")
        configured_user = settings.get("vault.ipmi_user_field")
        configured_pass = settings.get("vault.ipmi_password_field")
        print(f"  Configured fields : user={configured_user!r} "
              f"password={configured_pass!r}")
        missing = [f for f in (configured_user, configured_pass)
                   if f not in report["fields"]]
        if missing:
            print(f"  ** the configured field(s) {', '.join(missing)} are not in "
                  f"the secret; the tools fell back to a synonym. Set "
                  f"vault.ipmi_user_field / vault.ipmi_password_field in "
                  f"config/power-recovery.yaml to match.")
    if report["credentials_usable"]:
        print(f"  Credentials       : usable  (user {report['username']}, "
              f"{report['password_length']} character password)")
        print(f"  Source            : {report['source']}")
        print("\n  OK -- a recovery run will be able to reach the BMCs.")
        return 0
    print(f"  Credentials       : NOT usable")
    print(f"  Error             : {report.get('error', 'unknown')}")
    under = report.get("secrets_under_path") or []
    if under:
        print(f"\n  ** {report['path']} is a folder, not a secret.")
        print(f"     Secrets inside it: {', '.join(under)}")
        print(f"     Set vault.ipmi_path accordingly, e.g. "
              f"'{under[0]}' in config/power-recovery.yaml.")
    elif report.get("secrets_under_base"):
        print(f"\n  Secrets under {settings.get('vault.kv_mount')}/"
              f"{vault.base_path}: {', '.join(report['secrets_under_base'])}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
