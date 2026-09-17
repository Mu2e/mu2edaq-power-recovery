"""mu2e-vault-ipmi -- verify the Vault path that holds the BMC credentials.

The IPMI secret lives at ``td/scd/experiments/mu2e/ipmi``.  Its exact field
names could not be confirmed from the repositories, so
``vault.ipmi_user_field`` / ``vault.ipmi_password_field`` are configurable and
this tool exists to check what is actually there -- before an outage, not
during one.

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

notes
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
    relative = args.path or settings.get("vault.ipmi_path", "ipmi")
    full_path = f"{settings.get('vault.kv_mount')}/{vault.base_path}/{relative}"

    report = {
        "addr": vault.addr,
        "path": full_path,
        "fields": [],
        "reachable": False,
        "credentials_usable": False,
        "source": None,
    }

    try:
        data = vault.read(relative)
        report["reachable"] = True
        report["fields"] = sorted(data)
    except VaultError as exc:
        report["error"] = str(exc)

    if args.fields:
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print("\n".join(report["fields"]) if report["fields"]
                  else f"(no fields: {report.get('error', 'unknown error')})")
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
    return 1


if __name__ == "__main__":
    sys.exit(main())
