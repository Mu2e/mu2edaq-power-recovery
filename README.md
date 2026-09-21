# mu2edaq-power-recovery

These are a set of tools and scripts to properly recover the Mu2e DAQ computing
centers from planned and unplanned power outages, bumps and other disruptions.
Basically if it can knock our machines off the air this should be able to 
systemmatically bring them back.

This is all based upon our written procedures in DocDB and in the operations
wiki.  So if you have questions about certain decisions or why the orderings
are the way they are, see those docs for details.

The approach I took is that we would assume that after a power incident we
would have only our gateway nodes available to us.  We would work external
to those nodes and systemmatically bring things up in stages, where each stage
would bring up resources, verify them, log that everything is working and then
proceed to the next step.

I also assumed that we would want to document everything in the ECL, so there
are hooks in all of this for posting to the ECL.

In terms of verification, I want EVERYTHING checked and I assume everything
is broken.  So the working model is assess the current state, power the cluster on in dependency
order, verify every component, check the network fabric between nodes, and
produce a report for the electronic logbook.

Now this is important -- Everything runs from a workstation **outside** the DAQ 
networks (i.e. Andrew's Laptop). This means that we have to navigate the 
firewalls and other DAQ boundaries at each step (which is a pain) Nodes are
reached over SSH through a gateway with Kerberos/GSSAPI; While BMCs are reached by
running `ipmitool` *on* a gateway, because the IPMI subnets are not routable
from off site (and we can't tunnel through).

So the point is this is complicated, but it should work.

---

## Quick start

The assumption is that you are going to be running this from a laptop, or some other
machine that is NOT on the DAQ network (we will also have copies installed on the 
DAQ Network but the instructions are designed for the external case)

So first install....

```sh
git clone git@github.com:Mu2e/mu2edaq-power-recovery.git
cd mu2edaq-power-recovery
./bootstrap.sh
. venv/bin/activate

mu2e-power-recovery --version        # mu2edaq-power-recovery 0.1.0
mu2e-power-recovery --list-checks    # the 25 registered checks
mu2e-power-recovery --list-nodes     # what the tools think exists

# rehearse the entire four-phase run with no cluster attached
mu2e-power-recovery --phase all --simulate
```

That rehearsal needs no credentials, no network and no cluster: it answers every
command from a built-in script. If it writes a report, the installation is
sound. The report goes to `html/`; open `html/index.html` in a browser.  The
purpose of the rehersal is to make sure that all the tools are in place 
and that everything is responding the way we intend.  Basically it's 
the double check to make sure you aren't chasing a phantom error later because
you happen to have a weird setup that is missing some tool.

Then when you are ready you will need a Kerberos ticket.  I've designed this
to work with both personal tickets that are in the k5's and with special service
tickets (which allow non-experts to also run this)

Once you have a Kerberos ticket, you can do a real read-only survey. It changes
nothing and is safe to run while the DAQ is taking data.  Basically it's just 
probing the state of the system:

```sh
mu2e-power-state --principal $USER@FNAL.GOV
```

`--phase` is accepted **only** by `mu2e-power-recovery`; the four single-phase
commands each *are* their phase. Every driver takes `--version` and `--help`.

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

This tool can switch machines off, so the destructive path is gated and fenced:

- **Dry run by default.** `run.dry_run` starts true and nothing is switched
  until it is false. `--execute` is the *supported* way to set it — but it is
  only one way. `run.dry_run` is an ordinary configuration key, so
  `run: {dry_run: false}` in `config/power-recovery.yaml`, a line in
  `config/.env`, or `MU2E_POWER_RECOVERY_RUN_DRY_RUN=false` in the environment
  arms the destructive path on its own, with no flag on the command line. The
  run says which it is — it prints `LIVE RUN -- power commands WILL be issued.`
  before the first phase — but read that banner rather than trusting that the
  absence of `--execute` means a dry run.
- **`--simulate` is inert, and is the one gate no configuration file can
  open.** It forces `run.dry_run` back to true whatever else was asked for,
  including `--execute` on the same command line, and contacts nothing — the
  local transport is scripted too, so even a `ping` is answered from the
  script, and the publisher refuses to copy a rehearsal's report to the live web
  area.
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
history, about, API, site map — 9 in all. Alongside them, `html/data/` holds 7
JSON files (`inventory`, `assess`, `poweron`, `network`, `report`, `summary`,
`run-export`), so the report is consumable by another tool and not only by a
browser. They are data files rather than per-page companions: the four phase
pages each have one, `about`/`api`/`sitemap` have none, and the run history and
detail pages are covered indirectly by `summary.json` and `report.json`. The
generated `api.html` lists exactly what is there. Re-running a phase refreshes
its page in place; each run is archived under `html/runs/<id>/`.

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

**The login is not derived from the principal.** `~/.ssh/config` legitimately
sets the login for the DAQ hosts, and `mu2edaq` and `root` both accept a
personal ticket, while an account named after your principal generally does not
exist on those nodes. `ssh.user` and `ssh.root_user` override it. This was once
implemented the other way round and was wrong — see
[docs/DESIGN.md](docs/DESIGN.md) §2.

Only an *authentication* failure advances the chain. A refused connection, a
timeout or a changed host key stops it: a host that is down refuses every
identity equally, each attempt costs a full connect timeout, and no credential
gets past a host-key mismatch.

**Credential caches.** Each `ssh` is handed its own `KRB5CCNAME`; nothing here
writes the ambient environment. Service tickets are minted into private caches
with `get-kerberos-ticket --cache`, and every cache the run created is destroyed
by name when it ends.

On macOS that is not the whole story, and it matters, because a Mac is a normal
machine to drive a recovery from. Heimdal keeps credential caches in an `API:`
*collection* rather than as files, and makes each newly minted cache the
collection **default** — so minting a service ticket displaces your personal
ticket. Displaced, not destroyed: `klist -l` still lists it throughout. The
tools record the default principal before each mint and put the pointer back
afterwards, and abandon the service identities for the rest of the run if a
restore ever fails. To sort it out by hand:

```sh
klist -l                        # every cache in the collection
kswitch -p you@FNAL.GOV         # make yours the default again
```

Before it connects to anything, a run reads the default cache and, when it
holds a service identity rather than a personal principal, **warns** and prints
the `kswitch` line above. It is a warning, not a stop: the run carries on into
every phase, so if you do not act on it, sixty logins can still fail as
`mu2eraw`. The warning goes to the console, the log and the run's notes. Read
it — it is the one line that explains a whole run's worth of refusals.

Tickets for the service identities come from
[`mu2edaq-kerberos`](../mu2edaq-kerberos), which owns the keytab-in-Vault
layout; this project never handles a keytab. Its commands are found on `PATH`,
or in a sibling checkout's venv. Turn the fallback off with
`kerberos.use_service_keytabs: false`.

**Vault.** BMC credentials come from `td/scd/experiments/mu2e/ipmi/config` on
`https://ssivault.fnal.gov:8200`. Get a token with:

```sh
vault login -method=ldap -address=https://ssivault.fnal.gov:8200
```

The tools run that for you when no usable token is cached. The secret is at
`ipmi/config` — note that `ipmi` is a KV *folder*, not the secret — with fields
`username` and `password`, both confirmed against the live secret. Check it
before you need it:

```sh
mu2e-vault-ipmi            # path, fields and whether the credentials work
mu2e-vault-ipmi --fields   # just the field names present in the secret
mu2e-vault-ipmi --list     # browse the KV tree, if the path has moved
```

The BMCs accept the Vault username `MU2E` at cipher suite 3, verified against a
live BMC, so `ipmi.username` is left unset and nothing needs configuring. It
stays available to override Vault without touching a secret that is maintained
outside this repository.

If a BMC answers `Unable to establish IPMI v2 / RMCP+ session`, that single
message covers a wrong username, a wrong password, an unsupported cipher suite
*and* a chassis with no standby power. Let the tool work out which:

```sh
mu2e-ipmi-tool --diagnose -n mu2e-trk-03 chassis power status
```

`--diagnose` is read-only. It tries the configured username and its case
variants, then cipher suites 3, 17 and 0, and stops at the first combination
that works — capped at nine attempts, because every failure counts towards the
BMC's account lockout.

If a BMC *answers and rejects* the credentials, the run stops issuing IPMI
entirely and reports one diagnosis naming the refused username. All 45 BMCs
share one credential set, so the first rejection settles the matter and retrying
it against the other 44 only advances lockout counters. (45 is the number of
nodes carrying an `ipmi:` interface in `config/topology.yaml` — 37 at MC-2 and
8 at the teststand — out of 65 nodes in total. `mu2e-node-inventory -n ipmi`
lists them.)
`ipmi.stop_on_auth_failure: false` overrides that. A BMC that does not answer at
all is deliberately *not* treated this way: after an outage a dark chassis is
the expected case, and it says much the same thing.

If Vault is unreachable — plausible during a site-wide power event — the tools
fall back to `~/.ipmipasswd`, the file the existing `mu2edaq-operations`
scripts already read, and record in the run which source was used.

## Diagnostics

Each of these does one thing and has a man page. The four Python helpers all
take `--config`, `--env-file`, `--json`, `-v`/`--verbose` and `-q`/`--quiet`.

```sh
mu2e-node-inventory                       # what the tools think exists
mu2e-node-inventory -n ipmi --hostnames   # BMC names, pipeable
mu2e-node-inventory --protected           # hosts fenced off from destructive IPMI

mu2e-ipmi-tool -c tracker chassis power status
mu2e-ipmi-tool -n mu2e-trk-03 --show-command    # the exact ipmitool line, unrun
mu2e-ipmi-tool -n mu2e-trk-03 --diagnose chassis power status

mu2e-ssh-probe mu2e-trk-03                # print the ssh command and the chain; connect to NOTHING
mu2e-ssh-probe mu2e-trk-03 --run true     # actually log in; report which credential worked
mu2e-ssh-probe -c tracker --run uptime    # ...and run something
mu2e-ssh-probe mu2e-trk-03 --run true --no-chain   # ambient ticket only, no chain

mu2e-vault-ipmi --fields                  # what is in the Vault secret
mu2e-vault-ipmi --list                    # browse the KV tree

mu2e-probe --timeout 1000 < hosts.txt     # parallel TCP sweep (C++)
```

**`mu2e-ssh-probe` opens no connection unless you pass `--run`.** Without it the
tool prints the exact `ssh` argument vector and the credentials it *would* try,
says `nothing was run. Add --run COMMAND to execute.`, and exits 0. That is
useful for reading the command, and it is worth nothing as an access test: it
exits 0 whether or not the host would answer. Use `--run true` whenever the
question is "can I get in".

With `--run`, it builds **the same credential chain a real run builds** and
names the credential that got in; when every one is refused it lists each
login/ticket pair with the reason for the refusal. That matters because an
earlier version tested only the ambient ticket and `ssh_config`'s login, which
is how the probe could succeed against a host the recovery itself could not
reach — exactly the confusion it exists to prevent. `--no-chain` restores the
ambient-only behaviour, which is worth having when you want to compare the two.

One divergence remains, and it matters when you have set
`kerberos.principal`. The probe builds the same *list* of credentials, but it
does not mint tickets: it never calls `KerberosManager.prepare()`, so the
operator credential it uses carries your configured principal's **name** while
pointing at the **ambient** cache, and the chain's service identities are
listed rather than acquired. A real run kinits each principal into a private
cache and hands `ssh` a `KRB5CCNAME` for it. So `would try: login … ticket
you@FNAL.GOV [ambient cache]` names the principal from your config, not
necessarily the one actually in the default cache — check that with `klist`
(`klist -l` on macOS) rather than reading it off the probe. There is no
`--principal` flag to make the probe mint one.

`mu2e-ipmi-tool --show-command` prints the invocation without running it, for
comparison against a known-working one. It contains no password.

Two things to know about `mu2e-ipmi-tool` before using it to judge access:

- **It does not use the credential chain.** It reaches the gateway with your
  ambient ticket only — no service-identity fallbacks, and no check of the
  default cache. So it can fail against a gateway a real run would open with a
  service identity, and it can succeed under a displaced default cache that
  would take a real run somewhere else. It tests the *BMC* credentials well; it
  is not a test of the run's SSH access. Use `mu2e-ssh-probe … --run true` for
  that.
- **Naming only BMC-less hosts silently widens the target list.** Targets are
  filtered to nodes that have a BMC, but if that filter empties the list the
  tool falls back to the unfiltered one rather than stopping — so
  `mu2e-ipmi-tool -n mu2e-dcs-03 chassis power status` (no `ipmi:` interface in
  the topology) runs `ipmitool -H None …` on the gateway instead of saying so.
  `--diagnose` gets this right and reports "has no BMC in the topology".
  `mu2e-node-inventory -n ipmi --hostnames` lists which hosts actually have one.

If a check reports **the host key has CHANGED**, no credential can get past it:
ssh aborts before authenticating, so it is not a login failure and the tools do
not report it as one. See [docs/OPERATIONS.md](docs/OPERATIONS.md) for how to
verify the new key before touching `known_hosts`.

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

Three variables are read directly rather than through that mechanism:

| Variable | Effect |
|---|---|
| `NO_COLOR` | Set to anything: never colour the console output. |
| `FORCE_COLOR` | Set to anything: colour even when stdout is not a terminal — piping to a file, or CI. `NO_COLOR` wins. |
| `MU2E_POWER_RECOVERY_UPDATED` | Set by phase 0 across its own re-exec, so the restarted process does not check for updates a second time. `--no-self-update` is the supported way to skip the check. |

`VAULT_ADDR` and `VAULT_TOKEN` are read by `hvac` and the `vault` CLI as usual.

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

On Windows use `bootstrap.ps1` (`-WithCpp` to build the C++ library),
`start-mu2edaq-power-recovery.ps1` and `stop-mu2edaq-power-recovery.ps1`
(`-Status` to report only, `-Force` to kill at once, `-Grace <seconds>`,
default 30). Both start scripts pass every argument straight through to
`mu2e-power-recovery`. A Windows workstation needs an OpenSSH client (shipped
with Windows 10 and later) and a Kerberos client that can obtain an FNAL.GOV
ticket; everything else works unchanged.

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
pytest                                              # 309 tests, no cluster needed
mu2e-power-recovery --phase all --simulate          # end-to-end rehearsal
ctest --test-dir build --output-on-failure          # all four ctest entries
```

`ctest` runs four tests: `mu2eprobe-unit` (C++), `pytest`, `docs-check`
(`tools/generate-docs.py --check`) and `simulated-run`. The last three need
`venv/` to exist, so they are registered only when the build either creates it
(`-DBOOTSTRAP_VENV=ON`, the default) or finds it already there — which is the
case after `./bootstrap.sh`, since bootstrap makes the venv first and then
configures `build/` with `-DBOOTSTRAP_VENV=OFF`. Configure a tree with
`-DBOOTSTRAP_VENV=OFF` and no venv and CMake says it is skipping them.

Every check is a pure function of a transport, so the whole suite runs against
a scripted `FakeTransport`. Nothing in the test suite touches the DAQ network,
needs a Kerberos ticket, or reads Vault — which is the point, because the only
time it matters that the tests pass is *before* an outage.

That claim is *partly enforced*, not merely asserted. An autouse fixture in
`tests/conftest.py` monkeypatches `LocalTransport.run` and fails any test that
shells out through it to `ssh`, `scp`, `rsync`, `ping`, `ping6`, `ipmitool`,
`kinit`, `klist`, `kdestroy`, `vault`, `get-kerberos-ticket` or `vault-client`;
a test that genuinely needs to opts out with `@pytest.mark.allow_network`.
`tests/unit/test_network_guard.py` tests the guard itself. It was added after
the suite was found making real ssh connections to the gateways, and extended
after a `--simulate` run was found pinging them.

**Know what the guard does not cover.** It hooks one method, so anything that
does not go through `LocalTransport.run` walks straight past it:

- `creds/ticketsource.py` calls `subprocess.run` directly for `klist`,
  `klist -l`, `kswitch` and `get-kerberos-ticket`/`vault-client`;
- `creds/vault.py` calls `subprocess.call(["vault", "login", …])` and issues
  HTTPS to Vault through `hvac`.

A new test that exercised `KerberosManager`'s service path or
`VaultCredentials` would therefore contact the real Kerberos collection or
`ssivault.fnal.gov` without tripping anything. The existing tests stub those
paths; if you add one that does not, stub it yourself — the guard will not
catch you.

## Documentation

- `man 1 mu2e-power-recovery` — the driver, and one page per command
- `man 5 mu2edaq-topology.yaml` — and one page per config file
- `man 7 mu2edaq-power-recovery` — overview and the full check reference
- `man 3 libmu2eprobe` — the C/C++/Python probe API
- `man 1 mu2edaq-generate-docs` — `tools/generate-docs.py`, which regenerates
  the generated man-page sections and checks the rest against the code
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
