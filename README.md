# mu2edaq-power-recovery

Tools to recover the Mu2e DAQ computing centres from a planned or unplanned
power outage: assess the current state, power the cluster on in dependency
order, verify every component, check the network fabric between nodes, and
produce a report for the electronic logbook.

Everything runs from a workstation **outside** the DAQ networks. Nodes are
reached over SSH through a gateway with Kerberos/GSSAPI; BMCs are reached by
running `ipmitool` *on* a gateway, because the IPMI subnets are not routable
from off site.

---

## Quick start

```sh
git clone git@github.com:Mu2e/mu2edaq-power-recovery.git
cd mu2edaq-power-recovery
./bootstrap.sh
. venv/bin/activate

# rehearse the entire four-phase run with no cluster attached
mu2e-power-recovery --phase all --simulate

# the real read-only survey
mu2e-power-state --principal $USER@FNAL.GOV
```

The report is written to `html/`; open `html/index.html` in a browser.

## What it does

| Phase | Command | What happens |
|---|---|---|
| 0 | *(automatic)* | Check GitHub for a newer revision, fast-forward, rebuild if needed, restart, print the version and config digest. |
| 1 | `mu2e-power-state` | **Read-only** survey of every node: reachability, logins, disks, mounts, interfaces, link speeds, services, PCIe, and chassis power from the BMC. Nothing is changed. |
| 2 | `mu2e-power-on` | Power the cluster on in dependency order, verifying each stage before starting the next. Dry run unless `--execute`. |
| 3 | `mu2e-power-netcheck` | Node-to-node connectivity across the lab, data and IPMI segments, with a jumbo-frame probe on the data network. |
| 4 | `mu2e-power-report` | The consolidated narrative, optionally posted to the ECL. |

`mu2e-power-recovery --phase all` runs them in sequence.

## The power-on order

Defined as data in [`config/power-sequence.yaml`](config/power-sequence.yaml),
not in code:

```
gateways (verify only)
  → mu2e-mgr-01                       NFS and home server; everything mounts from it
    → mu2e-dl-01, mu2e-dl-02          data loggers
      → mu2e-dcs-01, mu2e-dcs-02      detector control; mounts verified against mgr-01
        → mu2e-cfo-01                 clock and control fabric
          → mu2e-crv-01, trk-01..14, calo-01..11, stm-01..02
```

Each stage: read power state → power on what is off → wait for SSH → settle →
run the stage's check profile → decide whether it met its `require:`
(`all`, `majority` or `any`). A stage that fails stops the sequence, because
every later stage depends on the services the earlier ones provide. Resume with
`--from <stage>` after fixing it.

## Safety

This tool can switch machines off, so the destructive path is gated twice and
fenced once:

- **Dry run by default.** `--execute` is required for any power command, *and*
  `run.dry_run` must be false. A config edit alone cannot arm it.
- **`--simulate` is inert.** It overrides `--execute` and contacts nothing.
- **Protected hosts.** `chassis power off`, `cycle` and `reset` are refused for
  the gateways, `mu2e-mgr-01` and `mu2e-dcs-01`, *regardless of flags*.
  Powering any of them down would cut you off from the cluster you are
  recovering, or stop the NFS server the rest of it mounts from. The list is
  `protected:` in `config/topology.yaml`.
- **The gateway stage never issues a power command at all.**
- **Audit trail.** Every state-changing attempt, including every refusal, is
  written to the run store *before* the command is issued.
- **Secrets stay out of process tables.** BMC passwords reach the gateway on
  stdin and `ipmitool` is invoked with `-E`, never `-P`. Kerberos passwords go
  straight to `kinit` on stdin.

## Reading a report

| Status | Meaning |
|---|---|
| **OK** | The check ran and the answer was right. |
| **WARN** | Right enough to proceed, worth your attention. |
| **FAIL** | The check ran and the answer was wrong. |
| **UNREACHABLE** | The check could not be run: the node did not answer. |
| **n/a** | Not applicable to this host (no BMC, no PCIe card). |

FAIL and UNREACHABLE are never merged. "It is broken" and "we could not look"
need different responses during a recovery.

Pages: overview, initial state, power on, network, detailed report, run
history, about, API, site map. Every page has a JSON companion under
`html/data/`, so the report is consumable by another tool and not only by a
browser. Re-running a phase refreshes its page in place; each run is archived
under `html/runs/<id>/`.

## Credentials

**Kerberos.** Designate the principals you want; the tools prompt for a
password only when there is no usable ticket, and keep each principal in its
own credential cache, destroyed when the run ends.

```sh
mu2e-power-recovery --phase all --execute \
    --principal anorman@FNAL.GOV \
    --root-principal anorman/root@FNAL.GOV
```

**Your own principal always goes first.** On every machine, for root sessions
as much as for ordinary ones. It is the identity the run belongs to, and no
service credential is used where a personal one would have done.

**Service identities are fallbacks only.** When your ticket is refused by a
host, the tools try `mu2edaq` and `mu2eshift`, then every other identity in
Vault (`mu2edcs`, `mu2edqm`, `mu2eraw`, `mu2e-controlroom`, `mu2e-teststand`),
until one gets in or the list is exhausted — then go back to your principal for
the next machine. A service identity that worked somewhere is ordered ahead of
the *other fallbacks*, never ahead of your own ticket. The report records which
identity opened each node.

For a **root** session the fallbacks keep the `root` login and change only the
ticket: authenticating as `mu2edaq` and logging in to the root account is
something a node's `root/.k5login` can authorise, and is why root has fallbacks
at all. Turn it off with `kerberos.root_fallback: false`.

Nothing here touches your default credential cache or the process environment:
each `ssh` gets its own `KRB5CCNAME`, and service tickets are minted into
private caches with `get-kerberos-ticket --cache`.

Tickets for these come from [`mu2edaq-kerberos`](../mu2edaq-kerberos), which
owns the keytab-in-Vault layout; this project never handles a keytab. Its
commands are found on `PATH`, or in a sibling checkout's venv. Turn the
fallback off with `kerberos.use_service_keytabs: false`.

Only an *authentication* failure advances the chain. A refused connection or a
timeout stops it — a host that is down refuses every identity equally, and each
attempt would cost a full connect timeout. Root sessions never fall back: the
service accounts are ordinary users.

**Vault.** BMC credentials come from `td/scd/experiments/mu2e/ipmi/config` on
`https://ssivault.fnal.gov:8200`. Get a token with:

```sh
vault login -method=ldap -address=https://ssivault.fnal.gov:8200
```

The tools run that for you when no usable token is cached. The secret is at
`ipmi/config` — note that `ipmi` is a KV folder, not the secret — with fields
`username` and `password`. Check it before you need it:

```sh
mu2e-vault-ipmi            # path, fields and whether the credentials work
mu2e-vault-ipmi --list     # browse the KV tree, if the path has moved
```

If a BMC answers `Unable to establish IPMI v2 / RMCP+ session`, that one
message covers a wrong username, a wrong password and an unsupported cipher
suite alike. Let the tool work out which:

```sh
mu2e-ipmi-tool --diagnose -n mu2e-trk-03 chassis power status
```

IPMI usernames are **case sensitive**, and that is the usual culprit — the
Vault secret and the BMC account have been seen disagreeing on case. Set
`ipmi.username` to override Vault without touching the secret.

If Vault is unreachable — plausible during a site-wide power event — the tools
fall back to `~/.ipmipasswd`, the file the existing `mu2edaq-operations`
scripts already read, and record in the run which source was used.

## Diagnostics

Each of these does one thing, and has a man page:

```sh
mu2e-node-inventory                       # what the tools think exists
mu2e-node-inventory -n ipmi --hostnames   # BMC names, pipeable
mu2e-ipmi-tool -c tracker chassis power status
mu2e-ssh-probe mu2e-trk-03                # the exact ssh command, unrun
mu2e-ssh-probe -c tracker --run true      # ...and run
mu2e-vault-ipmi --fields                  # what is in the Vault secret
mu2e-probe --timeout 1000 < hosts.txt     # parallel TCP sweep (C++)
```

## Configuration

Four files in `config/`, all YAML, all documented in man section 5:

| File | Holds |
|---|---|
| `power-recovery.yaml` | Everything else: SSH, IPMI, Vault, Kerberos, database, report, logging. |
| `topology.yaml` | Node inventory, per-site subnets, host classes, the protected list. |
| `power-sequence.yaml` | The phase-2 stage order. |
| `checks.yaml` | Thresholds, expected mounts and services, check profiles. |

Precedence, lowest first: built-in defaults → YAML → `config/.env` →
environment → command line. Any key is settable from the environment as
`MU2E_POWER_RECOVERY_<PATH>`, e.g.

```sh
MU2E_POWER_RECOVERY_SSH_CONNECT_TIMEOUT=30 mu2e-power-state
```

### MC-1 inventory is not yet filled in

`config/topology.yaml` defines `mc1` with its lab subnet (131.225.246.0/24) and
no data network, but **its node list is empty** — MC-1 is not carried in
`mu2edaq-operations/scripts/nodes_config.yaml`, which is the authoritative
upstream inventory, so nothing could be imported. Add the hostnames and every
phase picks MC-1 up with no code change. Until then the tools report it as
having no nodes configured rather than as healthy.

## Installation

```sh
./bootstrap.sh                 # venv + dependencies, idempotent
./bootstrap.sh --with-cpp      # ...and build the optional C/C++ library
```

On Windows use `bootstrap.ps1` / `start-mu2edaq-power-recovery.ps1`. A Windows
workstation needs an OpenSSH client (shipped with Windows 10 and later) and a
Kerberos client that can obtain an FNAL.GOV ticket; everything else works
unchanged.

Full CMake build, for the C/C++ library, its tests and the Python extension:

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build
ctest --test-dir build --output-on-failure
cmake --install build --prefix /usr/local
```

See [docs/INSTALL.md](docs/INSTALL.md) for the platform-by-platform detail.

## The optional C++ library

`libmu2eprobe` does a parallel TCP reachability sweep under OpenMP, with a C
ABI, a `mu2e-probe` command and pybind11 bindings. It exists because phase 1
opens by asking "which of sixty hosts are up", and doing that one subprocess at
a time is dominated by fork/exec cost.

It is strictly optional. Without it, `mu2edaq_power_recovery.sweep` falls back
to a thread pool with identical semantics — same outcome vocabulary, same
treatment of a refused connection as *reachable*. Which backend is in use is
printed in the run banner and recorded in the report, so a timing difference
between two runs has an explanation. See `man 3 libmu2eprobe`.

## Testing

```sh
pytest                                              # 293 tests, no cluster needed
mu2e-power-recovery --phase all --simulate          # end-to-end rehearsal
ctest --test-dir build --output-on-failure          # C++ and Python
```

Every check is a pure function of a transport, so the whole suite runs against
a scripted `FakeTransport`. Nothing in the test suite touches the DAQ network,
needs a Kerberos ticket, or reads Vault — which is the point, because the only
time it matters that the tests pass is *before* an outage.

## Documentation

- `man 1 mu2e-power-recovery` — the driver, and one page per command
- `man 5 mu2edaq-topology.yaml` — and one page per config file
- `man 7 mu2edaq-power-recovery` — overview and the full check reference
- `man 3 libmu2eprobe` — the C/C++/Python probe API
- [docs/INSTALL.md](docs/INSTALL.md) — installation and bootstrap
- [docs/DESIGN.md](docs/DESIGN.md) — architecture and the decisions behind it
- [docs/OPERATIONS.md](docs/OPERATIONS.md) — the outage runbook
- [PROJECT-STATUS.md](PROJECT-STATUS.md) — phase completion and test matrices

## Related repositories

| Repository | Relationship |
|---|---|
| `mu2edaq-operations` | Authoritative node inventory (`node_list.py`, `nodes_config.yaml`) and the on-node health checks whose semantics this reimplements. |
| `mu2edaq-kerberos` | Mints the service-identity tickets used as login fallbacks, from keytabs in Vault. Optional but recommended. |
| `ecl-client` | Posts the phase-4 report to the electronic logbook. |

## License

See [LICENSE](LICENSE).
