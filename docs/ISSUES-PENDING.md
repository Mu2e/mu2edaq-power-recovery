# Staged issues — mu2edaq-power-recovery

Findings from the September 2026 DAQ mega-review (`mu2e-sept-mega-review`),
2026-10-06.

**Why these are staged.** `Mu2e/mu2edaq-power-recovery` is public with
issues enabled. Under CONTRACT.md §9.1 a package agent attempts no GitHub
write. Each finding below is in the §6 body format, ready to file unchanged
with labels `mega-review` plus the category.

**Revision reviewed.** `origin/main` at `dc91bb2` (v0.2.0, PRs #26-#33,
merged 2026-10-06 19:19 UTC). The review branch was checked out at `8f1746f`,
29 commits and 107 files behind. Reviewing that tree would have re-reported
#1-#24, which v0.2.0 closed. Every `file:line` below is against `dc91bb2`.
Where a defect is also present at `8f1746f`, the finding says so.

**Dedup.** `gh issue list --state all --limit 300` on 2026-10-06 returned 25
issues. #1-#24 are closed (fixed by PRs #26-#32) and #25 (MC-1 inventory) is
open. No fingerprint matched and no title overlapped an open issue on two or
more significant tokens. PWR-003 touches #25's subject (inventory coverage)
but is about MC-2 and the teststand, so it is filed separately and
cross-referenced. All 14 findings are new.

**Verification method.** All reading was done in a `git archive origin/main`
export in a scratch directory. The package suite there gave 798 passed and 1
failed out of 799; the failure, `test_the_lock_file_is_ignored_by_git`, needs
a `.git` directory, which the export does not have. PWR-001 was reproduced
with the package's own `test_ipmi_tool.py` fixtures (a `FakeTransport`
gateway, stubbed Vault and credential session). PWR-002 was reproduced with
`--simulate`, and PWR-003 with `mu2e-node-inventory --validate`. No host,
BMC, KDC or Vault server was contacted. The ipmitool verb table comes from
upstream `ipmitool/ipmitool` master `be11d948f8`, read through the GitHub API.

**Fingerprints** are `sha1("Mu2e/mu2edaq-power-recovery:<path>:<rule_id>")[:8]`;
the inputs are tabulated at the end.

14 findings: 1 × S1, 1 × S2, 9 × S3, 3 × S4. Under the 15-issue cap, so
nothing is rolled up.

---

## PWR-001

**Title:** `[ipmi-tool] destructive ipmitool commands not spelled 'chassis power off|cycle|reset|soft' bypass --execute, confirmation and the protected-host refusal`

**Severity:** S1 — data loss, security exposure, or wrong physics
**Category:** bug
**Location:** `src/mu2edaq_power_recovery/tools/ipmi_tool.py:138-140`, `:259-264`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What
`mu2e-ipmi-tool` decides whether a command changes state by comparing the
**last** argument with five canonical `chassis power` verbs. Every command
that fails that test goes to `IPMIClient._run()` unchanged. That path has no
dry-run gate, no confirmation prompt, no run lock and no protected-host
check. ipmitool accepts several other spellings of power-off. With no `-n`
or `-c`, the tool targets every BMC at the location.

## Evidence
```
$ sed -n 138,140p src/mu2edaq_power_recovery/tools/ipmi_tool.py
    verb = args.command[-1].lower() if len(args.command) >= 3 else ""
    destructive = verb in DESTRUCTIVE_VERBS and args.command[0].lower() == "chassis"
    changing = verb in STATE_CHANGING_VERBS and args.command[0].lower() == "chassis"
$ sed -n 260,264p src/mu2edaq_power_recovery/tools/ipmi_tool.py
            if changing:
                result = client.power(node.ipmi_host, verb, node_host=node.hostname)
            else:
                result = client._run(node.ipmi_host, list(args.command))
$ grep -n 'VERBS =' src/mu2edaq_power_recovery/transport/ipmi.py
125:DESTRUCTIVE_VERBS = {"off", "cycle", "reset", "soft"}
128:STATE_CHANGING_VERBS = DESTRUCTIVE_VERBS | {"on"}
```
Upstream ipmitool (`ipmitool/ipmitool` `be11d948f8`) shows why this misses
cases:

- `src/ipmitool.c:92` registers a top-level `power` command ("Shortcut to
  chassis power commands").
- `lib/ipmi_chassis.c:1659-1669` and `:1928-1945` map `down` to POWER_DOWN,
  `up` to POWER_UP, `acpi` to ACPI_SOFT and `diag` to PULSE_DIAG.
- Only `argv[1]` is compared, so trailing arguments are ignored.

All of the following run with the configured `-L Operator`:

| Typed (no `--execute`, no `--yes`) | Tool's classification | ipmitool does |
|---|---|---|
| `power off` | read-only (fewer than 3 args) | chassis power down |
| `chassis power down` | read-only | chassis power down |
| `chassis power acpi` | read-only | ACPI soft off |
| `chassis power diag` | read-only | NMI pulse; crashes the host where NMI panics are enabled |
| `chassis power off now` | read-only (last arg is `now`) | chassis power down |
| `chassis policy always-off` | read-only | host stays off after the next AC loss |
| `raw 0x00 0x02 0x00` | read-only | Chassis Control: power down |
| `sel clear` | read-only | erases the SEL that `power.sel` diffs |

Reproduced with the package's own `tests/unit/test_ipmi_tool.py` fixtures
(fake gateway, nothing contacted). The protected host is `mu2e-mgr-01`:
```
$ mu2e-ipmi-tool -l mc2 -n mu2e-mgr-01 power off      # no --execute
prompts: 0
sent: timeout 30 ipmitool -I lanplus -H mu2e-mgr-01-ipmi.fnal.gov -U MU2E -L Operator -C 3 -E power off
(the same for every row of the table above)
$ mu2e-ipmi-tool -l mc2 power off                     # no -n / -c
reached 37 BMCs, including mu2egateway01-ipmi, mu2egateway02-ipmi,
mu2e-mgr-01-ipmi and mu2e-dcs-01-ipmi -- every protected host at MC-2
```
The same logic is present on the review branch at `8f1746f`
(`tools/ipmi_tool.py:102-104`, `:181`).

The documentation promises the opposite:

- `tools/ipmi_tool.py:10-11` and `man1/mu2e-ipmi-tool.1:33-35` say
  "state-changing verbs require --execute, so a mistyped host name in a hurry
  reads a power state instead of changing one";
- `docs/OPERATIONS.md:578` says "Any power command at all unless this
  invocation authorised it".

## Why it matters
This is the one command that can switch DAQ machines off, and its dry-run
gate and protected list are both bypassed by `power off`. That is ipmitool's
own documented shorthand, and what an operator coming from `mu2e_ipmi.sh`
habits is most likely to type. Typing it to preview the targets, with no
selection, powers off all 37 MC-2 BMC hosts. That includes both gateways,
which cuts off the session that is issuing the commands, and `mu2e-mgr-01`,
which serves `/home` and the shared areas to every other node. During data
taking that is data loss. `chassis policy always-off` silently undermines the
next power recovery. Rated S1 because a destructive path with no dry-run
guard is the rubric's anchor.

## Suggested fix
1. Invert the classification. Keep an allowlist of read-only commands:
   `chassis power status`, `chassis status`, `sdr*`, `sensor list|get`,
   `sel list|elist|info|get`, `fru print`, `lan print`, `mc info`.
   Treat everything else as state-changing. It then needs `--execute`, takes
   the run lock, and (for a destructive command or anything not
   understood) asks for confirmation and gets the protected refusal.
2. Normalise before classifying: `power X` becomes `chassis power X`; `down`
   is `off`, `up` is `on`, `acpi` is `soft`; `diag` is destructive. Take the
   verb from `argv[2]`, not `argv[-1]`. Refuse `raw` unless a flag naming
   it is given.
3. Defence in depth in `IPMIClient.power()` (`transport/ipmi.py:754-755`):
   require `node_host` for a destructive verb, or strip `ipmi_suffix` from
   `bmc_host` before calling `protected()`. A bare BMC name never matches
   `protected:` today.
4. Add one test per spelling in the table above.

## Related
- Mu2e/mu2edaq-power-recovery#16 (target selection in the same tool, closed)
- Mu2e/mu2edaq-power-recovery#11 (per-invocation authorisation for the drivers, closed)
- Mu2e/daq-operations `scripts/mu2e_ipmi.sh:19` (upstream guards only the exact string `chassis power off`)

<!-- mega-review-fingerprint: ebd1006d -->

---

## PWR-002

**Title:** `[selfupdate] the phase-0 re-exec drops the entry point: mu2e-power-on --execute restarts as --phase assess`

**Severity:** S2 — production breakage or silent wrong behaviour; workaround exists
**Category:** bug
**Location:** `src/mu2edaq_power_recovery/selfupdate.py:330-331`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What
After phase 0 fast-forwards the checkout, the process re-executes as
`python -m mu2edaq_power_recovery <original argv[1:]>`. `__main__` calls
`cli.main()` with no `fixed_phase`, so `--phase` falls back to its default,
`assess`. The single-phase drivers pin their phase through `fixed_phase`
(`cli.py:1053-1077`) and reject `--phase`, so their phase is not in argv and
is lost.

## Evidence
```
$ sed -n 330,331p src/mu2edaq_power_recovery/selfupdate.py
        os.execve(sys.executable, [sys.executable, "-m", "mu2edaq_power_recovery",
                                   *sys.argv[1:]], env)
$ cat src/mu2edaq_power_recovery/__main__.py | tail -2
if __name__ == "__main__":
    sys.exit(main())
$ grep -n 'parser.add_argument("--phase", default=' src/mu2edaq_power_recovery/cli.py
120:        parser.add_argument("--phase", default="assess",
# what the re-exec runs for `mu2e-power-on --execute` (rehearsed with --simulate):
$ python -m mu2edaq_power_recovery --execute --simulate --no-self-update --json | jq '[.phases[].name]'
["assess"]
```
The same code is on the review branch (`selfupdate.py:237`).

## Why it matters
Phase 0 runs before every real run of phases 1-3 (`cli.py:790`). The first
`mu2e-power-on --execute` after new commits land does not power anything on.
It runs a read-only phase-1 assessment, logs that `--execute` was ignored, and
prints "DRY RUN -- ... Pass --execute to act" although `--execute` was
passed. `mu2e-power-netcheck` likewise runs phase 1 instead of phase 3. During
an outage this costs a full assessment pass and a confusing banner, at exactly
the moment updates are most likely to have landed. The behaviour is not
escalating: no read-only driver turns into a power-on. That, and the fact
that a second invocation works, is why this is S2 and not S1.

## Suggested fix
Re-exec the same entry point. Either pass `--phase <fixed_phase>` explicitly
(accept it in the single-phase parsers when it equals their own phase), or
carry the entry point in an environment variable next to `REEXEC_GUARD` and
dispatch on it in `__main__`. Add a test that drives `do_self_update` with a
stubbed `execve` from each of the five entry points and asserts the argv.

## Related
- Mu2e/mu2edaq-power-recovery#21 (re-exec after a failed rebuild, closed)

<!-- mega-review-fingerprint: d15a7a54 -->

---

## PWR-003

**Title:** `[config] power-sequence.yaml never powers 12 of the 48 MC-2 inventory nodes or any teststand node`

**Severity:** S3 — correctness or maintainability risk, no current outage
**Category:** bug
**Location:** `config/power-sequence.yaml:90-100`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What
`topology.yaml` lists 48 MC-2 nodes (37 with a BMC) and 17 teststand nodes
(8 with a BMC). The phase-2 sequence stages 36 MC-2 nodes and has no
teststand stage at all. In addition, nine of the 28 `readout` nodes have no
BMC in the topology, so phase 2 can only record `no_bmc` for them.

## Evidence
```
$ mu2e-node-inventory --validate
  WARNING  location mc2: 12 inventory node(s) in no power-sequence stage, so phase 2 never powers
           them: mu2e-calo-12, mu2e-calo-13, mu2e-calo-14, mu2e-dcs-03, mu2e-extmon-01, mu2e-trk-15,
           mu2e-trk-16, mu2e-trk-17, mu2e-trk-18, mu2e-worker-01, mu2e-worker-02, mu2e-worker-03
  INFO     location teststand has no power-sequence stage; phase 2 powers nothing there
$ grep -n 'category: trk\|category: calo' config/topology.yaml config/power-sequence.yaml
config/topology.yaml:95:        - {category: trk,    start: 1, end: 18}
config/topology.yaml:96:        - {category: calo,   start: 1, end: 14}
config/power-sequence.yaml:97:      - {category: trk,  start: 1, end: 14}
config/power-sequence.yaml:98:      - {category: calo, start: 1, end: 11}
```
Readout nodes with no BMC (`topology.yaml:111-114` excludes):
`trk-05, 06, 07, 08, 10, 13`, `calo-08, 09, 10`. Ten of the twelve unstaged
MC-2 nodes do have BMCs (`dcs-03` and `calo-12` do not).

## Why it matters
After an outage these ten BMC-equipped hosts stay off unless someone switches
them on by hand with `mu2e-ipmi-tool`. `--node mu2e-trk-15` on a power-on
exits 2. The teststand cannot be powered by phase 2 at all:
`mu2e-power-on --location teststand` exits 2. CLAUDE.md §7 records
`trk-15..18` as a deliberate decision. Nothing records one for `calo-12..14`,
`dcs-03`, `extmon-01`, `worker-01..03` or the teststand. The validator warns,
but it runs only on request and never during a recovery. This is also one of
the four disagreeing MC-2 topology copies tracked for Wave 2 (18/14 here vs
14/11 in the sequence).

## Suggested fix
For each unstaged node, either add it to a stage or record the decision in a
comment in `power-sequence.yaml`, as was done for `trk-15..18`. Add a
teststand stage, or state in the file and in `man 5
mu2edaq-power-sequence.yaml` that the teststand is power-verified only.
Consider printing the `--validate` coverage warning in phase 2's notes, so
the report says which inventory nodes the sequence did not cover.

## Related
- Mu2e/mu2edaq-power-recovery#25 (MC-1 inventory; same validator, different site)
- mega-review Wave 2 topology table (`docs/mega-review/WAVE2-NOTES.md`)

<!-- mega-review-fingerprint: 11a8569f -->

---

## PWR-004

**Title:** `[topology] mu2e-mgr-01 is the phase-2 NFS stage here but absent from the authoritative nodes_config.yaml`

**Severity:** S3 — correctness or maintainability risk, no current outage
**Category:** bug
**Location:** `config/topology.yaml:88`, `:105`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What
`topology.yaml` describes itself as a copy of
`mu2edaq-operations/scripts/nodes_config.yaml`, "the authoritative upstream
inventory". Network by network, the two agree for MC-2 and HEERC except for
one host. `mu2e-mgr-01` (lab and IPMI) appears only in this package. Here it
is the second stage of the power-on, the NFS and home server that "every
later node's NFS mounts" depend on, and a protected host.

## Evidence
```
# set comparison with this package's own expand_entries, per network:
mc2 data   identical (42)        mc2 mgmt  identical (5)     mc2 pcie identical (36)
mc2 lab    only in power-recovery: mu2e-mgr-01
mc2 ipmi   only in power-recovery: mu2e-mgr-01-ipmi
teststand/heerc ctrl, data, ipmi, lab, pcie: identical
$ grep -n 'mu2e-mgr-01' ../mu2edaq-operations/scripts/nodes_config.yaml
(no output; nodes_config.yaml last changed 2991d05, 2026-09-17)
$ grep -n 'exclude_list=' ../mu2edaq-operations/scripts/mu2e_ipmi.sh
10:exclude_list="mu2egateway01-ipmi.fnal.gov mu2egateway02-ipmi.fnal.gov mu2e-dcs-01-ipmi.fnal.gov mu2e-mgr-01-ipmi.fnal.gov"
```
Upstream's exclude list names `mu2e-mgr-01-ipmi`, but upstream's node list
never yields it. Other packages (`mu2edaq-controlroom-setup`,
`Bootstrap/ssh/SSH-HOWTO.md`) also name `mu2e-mgr-01`.

## Why it matters
Two sources disagree about a host on which the whole power-on order depends.
Either the authoritative inventory is missing the cluster's NFS server, so
operations tools that iterate `nodes_config.yaml` (including `mu2e_ipmi.sh`)
never see it, or this package stages a host that is not where it thinks.
This review could not determine which from the repositories. "Lists can be
copied between the two files unchanged" no longer holds.

## Suggested fix
Confirm `mu2e-mgr-01` and its BMC with the DAQ operations owner. If they are
real, add them to `Mu2e/daq-operations` `scripts/nodes_config.yaml` so the
two copies agree again. In either case, record in `topology.yaml:4-7` which
entries are deliberate additions over upstream.

## Related
- Mu2e/daq-operations `scripts/nodes_config.yaml`
- PWR-003; mega-review Wave 2 topology table

<!-- mega-review-fingerprint: 2b60420b -->

---

## PWR-005

**Title:** `[bootstrap] bootstrap.sh rewrites the tracked requirements.txt, dirtying the checkout and switching off the phase-0 update`

**Severity:** S3 — correctness or maintainability risk, no current outage
**Category:** bug
**Location:** `bootstrap.sh:65-75`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What
Every `bootstrap.sh` run overwrites the tracked `requirements.txt` with
`pip freeze` from the local venv. The result depends on the interpreter
version and platform, so the file changes on any machine whose resolution
differs from the committed one. Phase 0 then sees a modified working tree and
refuses to update (`selfupdate.allow_dirty: false`). Phase 0's own rebuild
step runs `bootstrap.sh`, so a successful update can disable the next one.

## Evidence
```
$ sed -n 65,75p bootstrap.sh
# Regenerate requirements.txt from what was actually resolved, so the file
...
  mv "$PROJECT_DIR/requirements.txt.new" "$PROJECT_DIR/requirements.txt"
$ sed -n 158,165p src/mu2edaq_power_recovery/selfupdate.py
        status = self._out("status", "--porcelain")
        out.dirty = bool(status)
        if out.dirty and not self.settings.get("selfupdate.allow_dirty", False):
            out.messages.append(
                "working tree has local modifications -- running the code that is "
                "checked out and not pulling. ...
# in the superproject's checkout of this package, 2026-10-06:
$ git status --porcelain
 M requirements.txt                  # Python 3.12 resolution: pytest 9.1.1, urllib3 2.8.0, ...
$ git log -1 --format=%s 846853d
Merge main into the v0.2.0 stack; keep main's Python 3.9-compatible requirements.txt
```
The mega-review's Wave 0 also had to stash a bootstrap-regenerated
`requirements.txt` in this package (`docs/mega-review/QUARANTINE.md:11`).

## Why it matters
Phase 0 is the mechanism the project relies on to put fixes in the
operator's hands before a recovery. A routine bootstrap disables it, with
only a warning line in the phase-0 output, and the run continues on whatever
code is checked out. The same rewrite keeps producing merge conflicts on
`requirements.txt` (see `846853d`) and dirty trees in shared checkouts.

## Suggested fix
Write the freeze to an untracked file (`requirements.lock`, or under
`build/`), or regenerate only on request (`bootstrap.sh --freeze`). Keep the
tracked `requirements.txt` as an input that is edited deliberately. If it
must stay generated, have phase 0 ignore a modification to that one path
when it decides whether the tree is dirty.

## Related
- PWR-006; Mu2e/mu2edaq-power-recovery#21 (closed)

<!-- mega-review-fingerprint: 832886f2 -->

---

## PWR-006

**Title:** `[selfupdate] phase 0 fast-forwards to and executes unverified remote code, then installs unpinned dependencies, at the start of every live run`

**Severity:** S3 — correctness or maintainability risk, no current outage
**Category:** enhancement
**Location:** `src/mu2edaq_power_recovery/selfupdate.py:168-228`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What
Before every real run of phases 1-3, phase 0 runs
`git fetch <selfupdate.remote> <branch>` and `git merge --ff-only`. If a
build input changed, it runs `bootstrap.sh`. It then `execve`s into the new
code. No commit or tag signature is checked. `bootstrap.sh` installs
`pip install -e .[dev]` against the `>=` lower bounds in `pyproject.toml`,
not against the pinned `requirements.txt`. The new code then runs with the
operator's Kerberos tickets (including the root principal and the service
identities), their Vault token, and the BMC password.

## Evidence
```
$ grep -n '"fetch"\|"merge", "--ff-only"\|os.execve\|verify' src/mu2edaq_power_recovery/selfupdate.py
168:            fetched = self._git("fetch", "--quiet", self.remote, branch)
204:        merged = self._git("merge", "--ff-only", f"{self.remote}/{branch}")
330:        os.execve(sys.executable, [sys.executable, "-m", "mu2edaq_power_recovery",
$ sed -n 17,21p pyproject.toml
dependencies = [
    "PyYAML>=5.4",
    "Jinja2>=3.0",
    "SQLAlchemy>=1.4",
    "hvac>=1.2",
$ sed -n 63p bootstrap.sh
"$VENV_PY" -m pip install --quiet --editable ".[dev]"
$ gh repo view Mu2e/mu2edaq-power-recovery --json visibility
{"visibility":"PUBLIC"}
```

## Why it matters
Whoever can push to the tracked branch of a public repository gets code
execution on the recovery workstation, holding root-capable credentials, at
the start of the next live run. That includes a compromised contributor
account or token. Unpinned resolution also means an outage-time rebuild can
pull a dependency release nobody has tested. The guard rails that exist are
fast-forward only, refusal of a dirty tree or diverged branch, and rollback
on a failed rebuild (#21). They protect against accidents, not against a
malicious or broken commit. Rated S3 because self-update is a stated
requirement (`Project-Description.md`) and GitHub write access is restricted
to the Mu2e organisation.

## Suggested fix
- Fast-forward only to a signed tag or commit. Use `git merge --ff-only
  --verify-signatures`, or `git verify-tag` on a release tag with the
  maintainers' keys in an allowed-signers file.
- Make phase 0 install from a hash-pinned lock file
  (`pip install --require-hashes -r requirements.lock`), not from `>=` bounds.
- Print the incoming commit range and ask before re-executing under
  `--execute`, or offer `selfupdate.mode: check-only` for live runs.

## Related
- PWR-002, PWR-005; Mu2e/mu2edaq-power-recovery#21 (closed)

<!-- mega-review-fingerprint: fc4d7f3a -->

---

## PWR-007

**Title:** `[vault] the ~/.ipmipasswd fallback has no permission check and is taken on any Vault error, not only an outage`

**Severity:** S3 — correctness or maintainability risk, no current outage
**Category:** bug
**Location:** `src/mu2edaq_power_recovery/creds/vault.py:245-252`, `:285-302`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What
`VaultCredentials.ipmi()` falls back to `~/.ipmipasswd` on every
`VaultError`. That covers Vault being unreachable, which is the documented
intent. It also covers a missing `hvac`, an expired token with a failed login,
"permission denied", and "no secret at path", and the fallback is also taken
when the secret has no password field. The file is read with `read_text()`
and no `stat`: mode and owner are never checked.

## Evidence
```
$ sed -n 245,252p src/mu2edaq_power_recovery/creds/vault.py
        try:
            data = self.read(rel)
        except VaultError as exc:
            log.warning("Vault read failed: %s", exc)
            fallback = self._file_fallback()
            if fallback:
                return fallback
            raise
$ sed -n 290,294p src/mu2edaq_power_recovery/creds/vault.py
        path = Path(str(path_value)).expanduser()
        try:
            password = path.read_text().strip()
        except OSError:
            return None
$ grep -n 'allow_file_fallback\|fallback_user' config/power-recovery.yaml
237:  allow_file_fallback: true
239:  fallback_user: MU2E
```

## Why it matters
A group- or world-readable `~/.ipmipasswd` on a shared workstation exposes
the BMC account for 45 BMCs, and the tool uses it without comment. A
misconfigured Vault path or policy produces a run that "works" on a local
file with the hard-coded user `MU2E`, rather than a clear failure that would
get the Vault configuration fixed. The source is recorded (`file:...`), but
only as a log warning. The docstring (`vault.py:17-22`) justifies the
fallback by Vault being *down*, which is narrower than what the code does.

## Suggested fix
- Refuse (or require a flag to accept) a fallback file that is not a
  regular file owned by the effective uid with `mode & 0o077 == 0`, as ssh
  does for private keys.
- Fall back only for reachability failures: connection, TLS or timeout from
  `client()`. Raise on `Forbidden`, `InvalidPath` and a missing field.
- Put the credential source in the phase-1 notes, so it reaches the report
  and the ECL entry.

## Related
- Mu2e/daq-operations `scripts/mu2e_ipmi.sh:27` (the same file, read with `cat`)

<!-- mega-review-fingerprint: 0ae7f39e -->

---

## PWR-008

**Title:** `[ipmi-tool] mu2e-ipmi-tool ignores ipmi.username, so the fix its own --diagnose recommends does not reach it`

**Severity:** S3 — correctness or maintainability risk, no current outage
**Category:** bug
**Location:** `src/mu2edaq_power_recovery/tools/ipmi_tool.py:205`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What
The recovery run takes the BMC user from `ipmi.username`, falling back to the
Vault secret. `mu2e-ipmi-tool` takes it from `--user`, falling back to the
Vault secret. It never reads `ipmi.username`. `--diagnose` compares the
working username against the Vault value and tells the operator to set
`ipmi.username` in `config/power-recovery.yaml`. After they do, the run
works, but the tool does not, and `--diagnose` gives the same advice again.

## Evidence
```
$ grep -rn 'ipmi.username' src/mu2edaq_power_recovery/*.py src/mu2edaq_power_recovery/tools/*.py
src/mu2edaq_power_recovery/orchestrator.py:447:            username=self.settings.get("ipmi.username") or creds.username,
$ sed -n 205p src/mu2edaq_power_recovery/tools/ipmi_tool.py
    username = args.user or creds.username
$ sed -n 465,471p src/mu2edaq_power_recovery/tools/ipmi_tool.py
    if username != configured_username:
        changes.append(f"  ipmi:\n    username: {username}")
    ...
        print("\n  Put this in config/power-recovery.yaml:\n")
```
The live case this was built for: Vault holds `mu2e`, and the BMCs want
`MU2E` (`config/power-recovery.yaml:109-115`).

## Why it matters
The tool exists to reproduce what the run does ("a diagnostic that logged
in differently from the run could pass or fail where the run would not",
`tools/ipmi_tool.py:13-17`). Here it logs in differently. Each failed attempt
counts towards BMC account lockout, so an operator who retries the tool after
applying the recommended fix spends lockout budget on a known-bad username.

## Suggested fix
`username = args.user or settings.get("ipmi.username") or creds.username`.
Compare `--diagnose`'s result against that same value. Add a test that sets
`ipmi.username` and asserts the `-U` the tool sends.

## Related
- Mu2e/mu2edaq-power-recovery#14 (the tool's ssh credential chain, closed)

<!-- mega-review-fingerprint: f982c0cd -->

---

## PWR-009

**Title:** `[publish] rsync --delete runs against an unvalidated report.publish.target`

**Severity:** S3 — correctness or maintainability risk, no current outage
**Category:** bug
**Location:** `src/mu2edaq_power_recovery/report/publish.py:84-93`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What
Publication runs `rsync -az --delete html/ <report.publish.target>` after
every run that writes a report, whenever publishing is enabled. The target is
not checked in any way: no marker file, no expected suffix, no dry run, no
`--` before it. `--publish-target` on the command line enables publishing by
itself (`cli.py:247-248`).

## Evidence
```
$ sed -n 87p src/mu2edaq_power_recovery/report/publish.py
        argv = ["rsync", *self.options, f"{self.source}/", self.target]
$ grep -n 'options:' -A0 config/power-recovery.yaml | tail -1
265:    options: [-az, --delete]
$ grep -n 'target:' config/power-recovery.yaml
263:  # e.g. mu2edaq@mu2egateway01.fnal.gov:/web/sites/mu2e/recovery/
264:    target: null
```

## Why it matters
One path component short (`.../web/sites/mu2e/` for `.../recovery/`), and
the first publish deletes everything on the web area that is not part of the
report. It happens at the end of a recovery run, when the operator is least
likely to notice. The same applies to a mistyped `--publish-target` given on
the command line.

## Suggested fix
Write a marker file (for example `.mu2edaq-power-recovery-report`) into the
target on first publish. Refuse `--delete` unless the marker is already
there, or use `--delete` only inside a dedicated subdirectory the tool owns.
Put `--` before the source and target. Log the target and the rsync
`--itemize-changes` deletions into the run's events.

<!-- mega-review-fingerprint: 40ea2585 -->

---

## PWR-010

**Title:** `[ssh] ssh.options do not reach the -J hop, and GSSAPIDelegateCredentials forwards root-capable tickets to every node`

**Severity:** S3 — correctness or maintainability risk, no current outage
**Category:** bug
**Location:** `config/power-recovery.yaml:92-97`; `src/mu2edaq_power_recovery/transport/ssh.py:210-217`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What
Two properties of the one `ssh.options` list:

1. **The options do not apply to the jump host.** Nodes are reached with
   `ssh <options> -J <gateway> -- host`. ssh(1) on `-J`: "configuration
   directives supplied on the command-line generally apply to the
   destination host and not any specified jump hosts. Use ~/.ssh/config to
   specify configuration for jump hosts." So `BatchMode=yes`,
   `StrictHostKeyChecking=accept-new` and `GSSAPIAuthentication=yes` hold for
   the node but not for the gateway hop. That hop runs on whatever the
   operator's `ssh_config` says.
2. **Delegation is on for every session**, root sessions and service-identity
   sessions included. The stated reason, "the ticket that opened the gateway
   session authenticates the second hop" (`docs/DESIGN.md:36-37`,
   `creds/kerberos.py:7-8`, `transport/ssh.py:9-10`), describes nested ssh.
   With ProxyJump the workstation authenticates to the node itself, through
   a TCP forward. No check found in `checks/` uses Kerberos on the node.

## Evidence
```
$ sed -n 92,97p config/power-recovery.yaml
  options:
    - -o BatchMode=yes
    - -o StrictHostKeyChecking=accept-new
    - -o GSSAPIAuthentication=yes
    - -o GSSAPIDelegateCredentials=yes
    - -o LogLevel=ERROR
$ grep -rn 'klist\|kinit\|aklog' src/mu2edaq_power_recovery/checks/
(no output)
$ man ssh | grep -A3 'apply to the destination'
     supplied on the command-line generally apply to the destination
     host and not any specified jump hosts.  Use ~/.ssh/config to
     specify configuration for jump hosts.
```
Not verified against the live cluster. The 2026-10-01 read-only runs worked,
which suggests the author's `ssh_config` already sets GSSAPI for the
gateways.

## Why it matters
- On a workstation whose `ssh_config` does not enable GSSAPI for the
  gateways, the hop fails with an auth message. `classify_ssh_failure`
  classifies it as `auth`, and the run walks the whole seven-identity chain
  for nothing, which is the failure mode CLAUDE.md §6 warns about. Without
  `BatchMode`, the hop can also prompt on `/dev/tty` from a worker thread.
- Delegation copies a forwardable TGT onto each of about 48 nodes for the
  life of each session. With `--root-principal`, that is the operator's
  root-capable principal. Anyone with root on one recovering node, possibly
  one just reimaged, can use it while the session lasts.

## Suggested fix
Make the gateway hop explicit: `-o ProxyCommand="ssh <same options> -W %h:%p
<gateway>"`, or generate a per-run `ssh_config` with a `Host` block per
gateway and pass `-F`. Drop `GSSAPIDelegateCredentials=yes`, or set it only
for non-root logins if a check is found that needs it. Test the change with
`mu2e-ssh-probe --root --run true` on one node first. Correct the three
docstrings.

<!-- mega-review-fingerprint: 447b548d -->

---

## PWR-011

**Title:** `[ci] 799 pytest tests and a CTest suite with no CI workflow`

**Severity:** S3 — correctness or maintainability risk, no current outage
**Category:** enhancement
**Location:** `.github/workflows` (absent)
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What
The suite is large and hermetic. Its autouse `no_real_network` guard blocks
ssh, ipmitool, kinit, Vault, sockets and the sweep, so it needs no cluster
and no credentials. It also has a CTest target. The repository has no
`.github/` directory.

## Evidence
```
$ ls -a | grep -c '^\.github$'
0
$ pytest -o addopts= -q        # origin/main dc91bb2, scratch export, Python 3.12
1 failed, 798 passed in 75.71s   # the failure needs a .git directory (export artefact)
```

## Why it matters
The package's safety properties are carried by tests:
`tests/unit/test_ipmi.py` ("the safety assertions"), the live-run gate and
the network guard. Nothing runs them on a pull request. The suite targets
Python 3.9 on Linux (CLAUDE.md), and nothing checks that either.

## Suggested fix
Add a GitHub Actions workflow: `pytest` on Python 3.9 and 3.12 on
ubuntu-latest and macos-latest, plus `cmake -S . -B build && cmake --build
build && ctest --test-dir build`. Add `tools/generate-docs.py --check` if it
offers one.

## Related
- mega-review suite-wide "no CI" tracking item (`docs/mega-review/WAVE2-NOTES.md`)

<!-- mega-review-fingerprint: 9cbf95ee -->

---

## PWR-012

**Title:** `[kerberos] the mu2edaq-kerberos organisation is not pinned; VAULT_ORG in the operator's environment changes the fallback identities`

**Severity:** S4 — cosmetic, stale, hygiene
**Category:** bug
**Location:** `config/power-recovery.yaml:216-217`; `src/mu2edaq_power_recovery/creds/ticketsource.py:306-309`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What
`kerberos.vault_client_args` is empty by default, so neither `vault-client
identities` nor `get-kerberos-ticket` is given `--org mu2e`. Both inherit the
whole process environment. mu2edaq-kerberos resolves the organisation as
`--org`, then `VAULT_ORG`, then its own `vault.default_organization`, which
is `mu2e` today.

## Evidence
```
$ sed -n 216,217p config/power-recovery.yaml
  # Extra arguments passed through to vault-client, e.g. ['--org', 'mu2e'].
  vault_client_args: []
$ sed -n 306,309p src/mu2edaq_power_recovery/creds/ticketsource.py
        extra = list(self.settings.get("kerberos.vault_client_args", []) or [])
        try:
            result = self._run(command, ["identities", *extra], timeout=60,
                               env={**os.environ, **self._python_env()})
$ grep -n "org = " ../mu2edaq-kerberos/src/mu2edaq_kerberos/vault_client.py
287:    org = getattr(args, 'org', None) or env.get('VAULT_ORG') or file_cfg.get('default_organization')
```

## Why it matters
An operator who also works on NOvA and has `VAULT_ORG=nova` exported gets a
fallback chain made of NOvA identities. Every one of them is refused by Mu2e
hosts, and the only symptom is "refused every credential". A change of
mu2edaq-kerberos's default would have the same effect. Rated S4, not S3: the
effect is a failed fallback, never a wrong action, and the operator's own
principal is always tried first.

## Suggested fix
Ship `vault_client_args: [--org, mu2e]` in `config/power-recovery.yaml`, or
strip `VAULT_ORG` from the child environment unless configured.

## Related
- Mu2e/mu2edaq-kerberos (`config/vault.yaml:11` `default_organization: mu2e`)

<!-- mega-review-fingerprint: 9544a926 -->

---

## PWR-013

**Title:** `[docs] the teststand IPMI subnet is still given as 192.168.150.0/24 in six places`

**Severity:** S4 — cosmetic, stale, hygiene
**Category:** documentation
**Location:** `src/mu2edaq_power_recovery/transport/ipmi.py:3-4`
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What
`topology.yaml` was corrected after the 2026-10-01 live run. The teststand
BMCs are on the upper half of MC-2's segment, `192.168.157.128/25`, and are
driven from the MC-2 gateways. Six places still describe the old
`192.168.150.0/24`.

## Evidence
```
$ grep -rn '192\.168\.150' --include='*.py' --include='*.yaml' --include='*.md' --include='*.[0-9]' . | grep -v 'fake.py\|Project-Description'
src/mu2edaq_power_recovery/transport/ipmi.py:3:The IPMI segments -- 192.168.157.0/24 at MC-2, 192.168.150.0/24 at the
config/power-recovery.yaml:104:  # 192.168.150.0/24) are not routable from off-site.  ...
config/topology.yaml:220:      # 192.168.150.0/24, which nothing in DNS uses).        <- the correction itself
docs/DESIGN.md:245:192.168.150.0/24 at the teststand — are private and not routable from off site.
man/man1/mu2e-ipmi-tool.1:40:not locally: the IPMI subnets (192.168.157.0/24 at MC-2, 192.168.150.0/24 at
man/man5/mu2edaq-power-recovery.yaml.5:293:(192.168.157.0/24, 192.168.150.0/24) are not routable from off site. Changing
man/man5/mu2edaq-topology.yaml.5:270:Subnets: lab 131.225.237.0/24, data 10.226.9.0/24, IPMI 192.168.150.0/24.
```

## Why it matters
An operator debugging teststand IPMI from the man pages looks for a segment
that is not in DNS, on a gateway (`mu2edaq-gateway`) that has no interface to
the real one.

## Suggested fix
Replace each with `192.168.157.128/25 (on the MC-2 segment; driven from the
MC-2 gateways)` and point at `ipmi_gateways:` in `man 5
mu2edaq-topology.yaml`. `Project-Description.md` and `transport/fake.py`
can stay: one is the original brief, the other simulation data.

<!-- mega-review-fingerprint: 794b0f99 -->

---

## PWR-014

**Title:** `[man] .TH manual title is not the suite's 'Mu2e DAQ Manual'`

**Severity:** S4 — cosmetic, stale, hygiene
**Category:** documentation
**Location:** `man/man1/*.1`, `man/man3/*.3`, `man/man5/*.5`, `man/man7/*.7` (line 1)
**Found by:** mega-review (`mu2e-sept-mega-review`), 2026-10-06

## What
All 19 pages pass `mandoc -T lint` (no ERROR, WARNING or UNSUPP) and
`groff -mandoc -t -Tutf8 -ww -z` (no output), and every `.TH` section matches
its directory. The fifth `.TH` field, however, is `Mu2e DAQ Operations`
(section 1 and 3), `File Formats` (section 5) or `Overview` (section 7). The
mega-review contract fixes it to the literal `Mu2e DAQ Manual` across the
suite.

## Evidence
```
$ head -1 man/man1/mu2e-power-on.1 man/man5/mu2edaq-topology.yaml.5 man/man7/mu2edaq-power-recovery.7
.TH MU2E-POWER-ON 1 "2026-09-29" "mu2edaq-power-recovery 0.2.0" "Mu2e DAQ Operations"
.TH MU2EDAQ-TOPOLOGY.YAML 5 "2026-09-29" "mu2edaq-power-recovery 0.2.0" "File Formats"
.TH MU2EDAQ-POWER-RECOVERY 7 "2026-09-29" "mu2edaq-power-recovery 0.2.0" "Overview"
```

## Why it matters
Cosmetic only: the centre header differs from the other 40 packages' pages
in a combined `man -k`/HTML index.

## Suggested fix
`sed -i '1s/"[^"]*"$/"Mu2e DAQ Manual"/' man/man*/*.[1-9]`, applied after
the v0.2.0 pages are on the branch. Not done in this review, because every
page differs between the review branch and `origin/main` and an edit would
only conflict.

<!-- mega-review-fingerprint: f27c0f14 -->

---

## Fingerprint inputs

| ID | Input to `sha1(...)[:8]` | Fingerprint |
|---|---|---|
| PWR-001 | `Mu2e/mu2edaq-power-recovery:src/mu2edaq_power_recovery/tools/ipmi_tool.py:ipmi-verb-allowlist-bypass` | ebd1006d |
| PWR-002 | `Mu2e/mu2edaq-power-recovery:src/mu2edaq_power_recovery/selfupdate.py:reexec-loses-fixed-phase` | d15a7a54 |
| PWR-003 | `Mu2e/mu2edaq-power-recovery:config/power-sequence.yaml:sequence-inventory-coverage` | 11a8569f |
| PWR-004 | `Mu2e/mu2edaq-power-recovery:config/topology.yaml:inventory-host-disagreement` | 2b60420b |
| PWR-005 | `Mu2e/mu2edaq-power-recovery:bootstrap.sh:bootstrap-rewrites-tracked-file` | 832886f2 |
| PWR-006 | `Mu2e/mu2edaq-power-recovery:src/mu2edaq_power_recovery/selfupdate.py:selfupdate-unverified-code` | fc4d7f3a |
| PWR-007 | `Mu2e/mu2edaq-power-recovery:src/mu2edaq_power_recovery/creds/vault.py:ipmipasswd-fallback-unchecked` | 0ae7f39e |
| PWR-008 | `Mu2e/mu2edaq-power-recovery:src/mu2edaq_power_recovery/tools/ipmi_tool.py:ipmi-tool-ignores-username` | f982c0cd |
| PWR-009 | `Mu2e/mu2edaq-power-recovery:src/mu2edaq_power_recovery/report/publish.py:publish-delete-unguarded` | 40ea2585 |
| PWR-010 | `Mu2e/mu2edaq-power-recovery:config/power-recovery.yaml:ssh-options-jump-delegation` | 447b548d |
| PWR-011 | `Mu2e/mu2edaq-power-recovery:.github/workflows:no-ci` | 9cbf95ee |
| PWR-012 | `Mu2e/mu2edaq-power-recovery:config/power-recovery.yaml:kerberos-org-unpinned` | 9544a926 |
| PWR-013 | `Mu2e/mu2edaq-power-recovery:src/mu2edaq_power_recovery/transport/ipmi.py:stale-teststand-ipmi-subnet` | 794b0f99 |
| PWR-014 | `Mu2e/mu2edaq-power-recovery:man:man-th-manual-title` | f27c0f14 |
