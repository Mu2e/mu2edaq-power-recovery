# Outage runbook

What to actually do, in order. Intended to be followed by someone who did not
write the tools, at an hour when they would rather not be.

---

## Before the outage (do this in daylight)

```sh
cd mu2edaq-power-recovery
git pull && ./bootstrap.sh
pytest                                          # 309 tests, should be all green
mu2e-power-recovery --phase all --simulate      # full rehearsal, contacts nothing
open html/index.html                            # check the report looks right
```

Then confirm your access — this is the part that fails at 3 a.m. if it was
never tested:

```sh
kinit you@FNAL.GOV
kinit -c /tmp/krb5cc_root you/root@FNAL.GOV     # if you use a separate root principal
klist                                           # is the default cache YOURS?
klist -l                                        # macOS: the whole collection

vault login -method=ldap -address=https://ssivault.fnal.gov:8200
mu2e-vault-ipmi                                 # BMC credentials readable?
mu2e-ipmi-tool -n mu2e-trk-03 chassis power status    # does a BMC accept them?

mu2e-ssh-probe mu2egateway01 --run true              # gateway answers, with which credential?
mu2e-ssh-probe mu2e-mgr-01 --root --run 'id -u'      # root works through the jump host?
```

**`--run` is not optional here.** Without it `mu2e-ssh-probe` opens no
connection at all: it prints the `ssh` command and the credentials it would
try, says `nothing was run`, and exits 0. A bare `mu2e-ssh-probe mu2egateway01`
therefore always "passes" and tells you nothing about access. `--run true` is
the cheapest command that actually logs in.

With `--run`, the probe builds the same credential chain a real run builds and
names the credential that got in. (It did not always: an earlier version tested
only your ambient ticket, which is how probing a gateway could succeed while the
recovery failed against it.) It also acquires what a run acquires: a
configured `kerberos.principal` (or `--principal`) is kinit'd into a private
cache that `ssh` is pointed at, and the service identities are minted up front
and destroyed on exit. Without `--run` nothing is acquired, and any credential
that has no ticket yet is listed as `would try (not acquired)`.

`mu2e-ipmi-tool` opens its gateway session through the same credential
bootstrap as a run (your principal first, then the service identities, with
the default-cache check), so a gateway it cannot reach is one the run cannot
reach either. It still tests the *BMC* credentials first and foremost; whether
the run can log in to the *nodes* is `mu2e-ssh-probe --run true`.

`klist` matters more than it looks. If the default credential cache holds a
service identity rather than you, every login in the run is attempted as that
identity and refused, and nothing in the ssh error says so. A run checks this
before connecting to anything and **warns** — it does not stop, it carries on
through every phase — so noticing it here, in daylight, is the difference
between one `kswitch` and a whole run of refusals. See *How the credentials
actually work* below.

If `mu2e-vault-ipmi` reports that the configured field names are not in the
secret, fix `vault.ipmi_user_field` / `vault.ipmi_password_field` in
`config/power-recovery.yaml` now, not during the recovery.

The BMCs accept the Vault username `MU2E` at cipher suite 3 — verified against a
live BMC — so nothing needs configuring and `ipmi.username` stays unset. If that
ever stops being true, `mu2e-ipmi-tool --diagnose -n <node> chassis power status`
tries the plausible usernames and cipher suites read-only and prints the change
to make. Do it in daylight: every failed attempt counts towards the BMC's
account lockout, which is why `--diagnose` caps itself at nine.

---

## How the credentials actually work

Five minutes here saves an hour at 3 a.m., because most of the confusing
failures in this tool are credential failures wearing a disguise.

**One chain per host, your principal always first.** For each node the tools try
your own ticket, then `mu2edaq` and `mu2eshift`, then every other identity in
Vault (`mu2edcs`, `mu2edqm`, `mu2eraw`, `mu2e-controlroom`, `mu2e-teststand`),
and then give up on that node and start the next one from your ticket again. A
service identity that worked for a host is remembered *for that host*, but only
ever ordered ahead of the other fallbacks — never ahead of you.

**Root sessions fall back too**, keeping the `root` login and varying only the
ticket: a node's `root/.k5login` can authorise `mu2edaq` to become root. Turn it
off with `kerberos.root_fallback: false`.

**Only an authentication failure moves the chain along.** A refused connection,
a timeout, rate limiting (`kex_exchange_identification`, "connection reset by
peer", "too many authentication failures") and a changed host key each stop it
at the first attempt. A host that is down refuses every identity equally, so
walking all eight costs eight connect timeouts and learns nothing — and a node
that refuses the login now skips its remaining SSH checks instead of opening
twelve more connections to a server that is already rate-limiting.

**The login does not come from your principal.** `ssh_config` sets it, and that
is correct: `mu2edaq` and `root` both accept your personal ticket, while an
account named after your principal does not exist on those hosts. If a login
looks wrong, `ssh.user` / `ssh.root_user` override it; do not go hunting for a
bug in principal parsing.

**Run with `-v` when anything is unclear.** The primary login/ticket pair is
logged before anything is attempted, and every attempt after that logs
`login X ticket Y [cache Z] -> ok/refused`.

### On macOS: the ticket that moved

macOS ships Heimdal, which keeps credential caches in an `API:` *collection*
rather than as files and makes each newly minted cache the collection
**default**. Minting a service ticket therefore repoints "your default ticket"
at it. Your ticket is **displaced, not destroyed** — `klist -l` lists it
throughout, and `kswitch` brings it back instantly.

The tools record the default principal before each mint, put the pointer back
afterwards, and abandon the service identities for the rest of the run if a
restore ever fails. If you find yourself in that state by hand — every login
refused, `klist` showing `mu2eraw` or similar:

```sh
klist -l                        # every cache in the collection
kswitch -p you@FNAL.GOV         # make yours the default again
klist                           # confirm
```

`kdestroy --all && kinit you@FNAL.GOV` also works and throws away more.

---

### When it still goes wrong

Three failures are common enough to have their own sections further down, and
they are the ones whose symptom does not name its cause:

- [When a gateway refuses every credential](#when-a-gateway-refuses-every-credential)
- [When a node's host key has changed](#when-a-nodes-host-key-has-changed)
- [When the run stops issuing IPMI](#when-the-run-stops-issuing-ipmi)

---

## Phase 1 — find out what survived

```sh
mu2e-power-state --label "Sept 2026 outage" \
    --principal you@FNAL.GOV --root-principal you/root@FNAL.GOV
```

This changes nothing. It is safe to run repeatedly, and safe to run while the
DAQ is taking data.

**Read the output in this order:**

1. **Did a gateway answer?** If the phase stopped with "no gateway could be
   logged in to", nothing else matters yet. The failure lists every credential
   it tried and why each was refused — see *When a gateway refuses every
   credential* below, which distinguishes a dark gateway from a bad ticket.
2. **Is phase 2 possible?** The readiness block says whether a gateway is
   usable for IPMI and how many BMCs answered. A BMC that does not answer means
   that chassis has no standby power at all — it cannot be switched on
   remotely, and someone has to go to the rack.
3. **What is already up?** The POWER column. Anything already `on` will be left
   alone by phase 2.
4. **What is broken?** The failure detail below the table.

Then open `html/initial-state.html` and keep it. It is the "before" picture.

---

## Phase 2 — bring it up

**First, without doing anything:**

```sh
mu2e-power-on
```

That is a dry run: it reads every power state and shows what it *would* switch
on, stage by stage. Read it. If the stage list or the node list is not what you
expect, stop and find out why.

**Then, for real:**

```sh
mu2e-power-on --execute --label "Sept 2026 outage"
```

`--execute` is this invocation's authorisation; nothing else on the command
line or in the configuration arms a run by itself. An unattended run that
cannot pass the flag sets `run.dry_run: false` and `run.label: <label>` in its
configuration and `MU2E_POWER_RECOVERY_ARM=<label>` in its own environment
(never in `config/.env`, which is refused). `run.dry_run: false` with neither
exits 2 with instructions.

It will work through the stages in order and stop at the first one that does
not meet its requirement.

### If a stage fails

The sequence stops deliberately: everything after it depends on it, so
continuing would produce failures that tell you nothing new.

```sh
# see exactly what failed
mu2e-power-state --node mu2e-mgr-01 -v

# fix it by hand, then resume from that stage
mu2e-power-on --execute --from manager
```

Stage names are the `name:` keys in `config/power-sequence.yaml` —
`gateways`, `manager`, `dataloggers`, `dcs`, `cfo`, `readout`. A misspelt
`--from`/`--until` (or `run.from_stage`/`run.until_stage`), or a reversed range,
exits 2 before any password prompt, listing the valid names; nothing is
contacted.

### Powering on only some nodes

```sh
# one readout node; everything it depends on is VERIFIED, never powered
mu2e-power-on --execute --node mu2e-trk-01

# that node and nothing else at all (no predecessor checks)
mu2e-power-on --execute --from readout --node mu2e-trk-01
```

With `--node`, the stages holding the named nodes are cut down to them; the
stages before them (from `--from`, or the start of the sequence) are
**verify-only**: their power state is read, they are waited for and checked,
but no power command is sent to them. The run prints the plan as `scope:` lines
before it starts. If a predecessor is off or never answers, the run stops
before the requested nodes — even with `--continue-on-error` — and names the
stage. Bring that stage up explicitly and re-run:

```sh
mu2e-power-on --execute --from manager --until manager
mu2e-power-on --execute --node mu2e-trk-01
```

Stages after the requested nodes are not run. A node in no stage (for example
`mu2e-trk-15`, which is in the inventory but not in `readout`), or in a stage
outside `--from`/`--until`/`--location`, is an error naming its stage.
`--location teststand` alone is an error with the shipped sequence, whose
stages are all MC-2.

### How long a stage can take

Each stage waits for all its nodes at once, under one deadline of
`boot_timeout` (600 s by default) — at most `ssh.max_sessions` ssh attempts at a
time — so a stage of dead nodes costs one `boot_timeout`, not one per node.
`run.phase_timeout` (7200 s) bounds the whole phase: every ssh call is capped at
the time left, and stages the budget never reached are UNKNOWN, `not run:
phase_timeout expired` (not FAIL — nothing was looked at). Resume with
`--from <stage>` as the note says.

If you know a node is dead and want the rest of the cluster up anyway:

```sh
mu2e-power-on --execute --continue-on-error
```

### Common stage failures

| Symptom | Usually means | Next step |
|---|---|---|
| A node never answers SSH after power-on | It powered on but did not boot | `mu2e-ipmi-tool -n <node> sel list last 20` — the BMC log survives what the host did not |
| `disk.mounts` fails across many nodes | `mu2e-mgr-01` is not exporting yet | Check the manager stage passed; `svc.nfs_export` on mgr-01 |
| `login.users` fails but `ssh.login_root` passes | `/home` is not mounted, so `su -` lands nowhere | Same as above |
| `net.data` reports 1000 Mb/s | The link renegotiated after a switch reboot | Bounce the port; this is the quiet failure that makes the DAQ ten times too slow |
| `pcie.devices` finds nothing | Cold-boot PCIe training failure | Needs a full AC power cycle of that chassis, not a warm reboot |
| `power.status` says unreachable | The BMC has no standby power | Physical check at the rack |
| `Unable to establish IPMI v2 / RMCP+ session` | Most often a dark chassis; otherwise username, password or cipher suite | Physical check first; then `mu2e-ipmi-tool --diagnose -n <node> chassis power status` |
| `ssh.login` says **the host key has CHANGED** | The node was reimaged, or the connection is intercepted | Verify out of band before editing `known_hosts` — see above |
| Every node in a stage refused every credential | Almost always the default credential cache, not the nodes | `klist`; on macOS `klist -l` and `kswitch -p you@FNAL.GOV` |
| The run reports it has stopped issuing IPMI | A BMC rejected the credentials; they are shared, so the rest would too | See *When the run stops issuing IPMI* above |

---

## Phase 3 — prove the fabric works

```sh
mu2e-power-netcheck
```

Nodes that failed phase 2 are skipped by default. Read the aggregated notes
first, not the matrix:

- **"N nodes reached nothing"** — with more than two, look for one shared
  cause (the segment's switch or uplink) before looking at individual NICs.
- **"N hosts could not be reached by anyone although they probe out"** —
  suspect a one-way path, ARP, or a host firewall.
- **"connectivity is fine but N paths cannot carry a full jumbo frame"** — a
  switch or interface came back with the wrong MTU. Everything will work, and
  the DAQ will not reach its throughput.

---

## Phase 4 — write it down

```sh
mu2e-power-report --post-ecl
```

Check the narrative before it goes to the logbook — it is generated from the
stored evidence, but the *label* is yours and is what someone will search for
later.

To regenerate or repost afterwards:

```sh
mu2e-power-report --run-id 17              # re-render run 17 only
mu2e-power-report --run-id 17 --post-ecl   # ...and post it
```

What regeneration does and does not do:

- It works on run 17 itself. No new run is created, run 17's status and
  finish time are not changed, and a `report` phase plus events are added to
  run 17's timeline. `html/runs/17/` is re-rendered from the store; the top
  level of `html/` changes only if 17 is the newest run.
- It needs no Kerberos ticket. Vault is contacted only for `--post-ecl`, to
  read the ECL credentials. A run id that is not in the store exits 2 and
  writes nothing — check `html/runs.html` for the ids.
- The logbook entry carries run 17's own pages, `detail.html` included. If the
  post fails, the local report is still complete; the failure is an `error`
  event in run 17's timeline and in `html/runs/17/data/report.json` (`ecl`).
  Fix the credential or the network and run the same command again.
- "Outstanding problems" lists only what is still wrong. A failure that a
  later phase re-checked and passed is under "Resolved during the run".

---

## All four at once

Once you have done this a few times and trust it:

```sh
mu2e-power-recovery --phase all --execute \
    --label "Sept 2026 planned outage" \
    --principal you@FNAL.GOV --root-principal you/root@FNAL.GOV \
    --post-ecl --publish
```

It stops between phases if an earlier one failed in a way that makes the later
ones meaningless.

---

## When a gateway refuses every credential

Phase 1 stops here deliberately. Everything else is probed *through* a gateway,
so carrying on would produce fifty UNREACHABLE rows that are one fact about the
gateway reported fifty times.

The failure lists each login/ticket pair and the reason it was refused, rather
than just "gateway mu2egateway01 did not answer ssh". Read the reasons:

| What the reasons say | What it means | Next |
|---|---|---|
| Every pair `auth`, `Permission denied (gssapi)` | The tickets are wrong, or the default cache is not yours | `klist`, `klist -l`, `kswitch -p you@FNAL.GOV`, then `kinit` |
| Every pair `unreachable`, refused or timed out | The gateway is down, or you are off the network | Try the other gateway; check the VPN; after an outage a dark chassis is plausible |
| `hostkey` | The gateway's key changed | Verify it out of band — next section — before editing `known_hosts` |
| One attempt only, then nothing | That failure was not an auth failure, so the chain stopped | The reason on that line is the real one |

Reproduce it on its own, with the chain the run uses:

```sh
mu2e-ssh-probe mu2egateway01 --run true -v
mu2e-ssh-probe mu2egateway02 --run true -v
mu2e-ssh-probe mu2egateway01 --run true --no-chain -v   # ambient ticket only
```

`--run true` is what makes these reproduce anything. Without it the probe
connects to nothing and exits 0, however broken the gateway is.

`--no-chain` is the comparison worth having: it tells you whether the chain is
the problem or your ambient ticket is. If `--no-chain` works and the full chain
does not, a service identity has displaced your default cache — `kswitch -p`
and try again.

If neither gateway answers and both are known to have power, there is nothing
this tool can do from off site. The recovery starts with someone on the floor.

---

## When a node's host key has changed

```
ssh.login   FAIL   the host key has CHANGED -- ssh refused before authenticating
```

This is **not** a login failure, and no credential can get past it: ssh aborts
before it authenticates anything. `StrictHostKeyChecking=accept-new` accepts an
unknown host and refuses a *changed* one, which is the behaviour you want.

After a power event the usual cause is that the node was reimaged, and a
recovery is exactly when that happens. The other reading is that the connection
is being intercepted. **Verify the key out of band before touching
`known_hosts`** — from a gateway, which is inside the network:

```sh
mu2e-ssh-probe mu2egateway01 --run 'ssh-keyscan mu2e-calo-01'
```

Compare that against what you hold:

```sh
ssh-keygen -F mu2e-calo-01.fnal.gov
```

Only when the two explain each other — a reimage you can confirm from the BMC
event log, a ticket, or the person who did it — drop the stale entry and re-run
that node:

```sh
ssh-keygen -R mu2e-calo-01.fnal.gov
mu2e-power-state --node mu2e-calo-01 -v
```

Do not reach for `StrictHostKeyChecking=no`. It would turn one specific, loud,
correct refusal into a silent acceptance across the whole cluster, on the night
you are least able to notice.

---

## When the run stops issuing IPMI

If a BMC *answers and rejects* the credentials, the run stops issuing IPMI
altogether and reports one diagnosis naming the refused username. That is
deliberate: all 45 BMCs share one credential set, so the first rejection settles
the matter, and retrying it against the other 44 only advances lockout counters
on every BMC in the building. (45 nodes of the 65 in the topology carry an
`ipmi:` interface: 37 at MC-2, 8 at the teststand.)

```sh
mu2e-vault-ipmi --fields                              # what the secret holds now
mu2e-ipmi-tool -n mu2e-trk-03 --show-command          # the invocation, no password in it
mu2e-ipmi-tool -n mu2e-trk-03 --diagnose chassis power status
```

The likely causes are a rotated password or a re-keyed Vault secret, both
maintained outside this repository. `ipmi.username` overrides the Vault
username; `vault.ipmi_user_field` / `vault.ipmi_password_field` fix a renamed
field. To carry on regardless — for instance when you believe only one BMC is
misconfigured — set `ipmi.stop_on_auth_failure: false`.

The refusal shows as **UNKNOWN** on `power.status`, `power.sensors` and
`power.sel` ("IPMI credentials refused"), not as FAIL "BMC does not answer",
and phase 2 records `credentials_refused` for each node rather than
`unreachable`. Phase 1's readiness block says phase 2 is not ready. Only one
BMC was actually asked: until a credential has worked once, IPMI commands are
issued one at a time.

Each of those unproven commands is preceded by one `ping -c 1 -W 1 <bmc>` from
the gateway. A BMC that does not answer is reported **FAIL** "does not answer"
on `power.status` straight away, without ipmitool and without waiting its turn,
and its `power.sensors` / `power.sel` are **UNKNOWN** ("not read: the BMC did
not answer"). If the BMCs filter ICMP, every one will look dark: set
`ipmi.reachability_precheck: false`.

Two BMCs that *do* answer ping and then fail with `Unable to establish IPMI v2 /
RMCP+ session`, before any BMC has accepted the credential, also stop the run's
IPMI — the diagnosis says "likeliest cause is a wrong username". Check the
username first (`mu2e-ipmi-tool --diagnose`, above), then the cipher suite.

A BMC that simply does **not answer** is not treated this way, and says much the
same thing (`Unable to establish IPMI v2 / RMCP+ session`) — the ping pre-check
is what tells the two apart. After an outage a
chassis with no standby power is the expected case, not a credential problem.

---

## Things you may need mid-recovery

```sh
# one node's power state
mu2e-ipmi-tool -n mu2e-trk-03 chassis power status

# the BMC event log — survives an outage the host did not
mu2e-ipmi-tool -n mu2e-trk-03 sel list last 20

# temperatures, if the room's cooling is also in question
mu2e-ipmi-tool -c tracker sdr elist | grep -i temp

# the exact ipmitool invocation, unrun, to compare with a known-working one
mu2e-ipmi-tool -n mu2e-trk-03 --show-command

# force one node on by hand
mu2e-ipmi-tool -n mu2e-trk-03 --execute chassis power on

# which credential actually opens this node, and what refused
mu2e-ssh-probe mu2e-trk-03 --root --run true -v

# which hosts are fenced off from destructive IPMI
mu2e-node-inventory --protected

# re-check one node only
mu2e-power-state --node mu2e-trk-03 -v

# is a run still going?
./stop-mu2edaq-power-recovery.sh --status

# stop a run that is going wrong  (--force to skip the grace period)
./stop-mu2edaq-power-recovery.sh
```

On Windows the equivalents are `stop-mu2edaq-power-recovery.ps1 -Status`,
`-Force` and `-Grace <seconds>` (default 30).

**SIGTERM is a clean stop; SIGKILL is not.** The driver's SIGTERM handler
raises the same KeyboardInterrupt as Ctrl-C. Mid-phase, the worker pool is shut
down without waiting (`cancel_futures`): queued nodes and mesh sources are
never started, the interruption is recorded, the run is marked `interrupted`,
and the private Kerberos caches are destroyed straight away — including one a
service mint was writing when the signal arrived, and with the default cache
restored if that mint had displaced it. Nodes already mid-command finish that
command in the background before the process exits, which can take up to
`ssh.command_timeout`; if the stop script's grace period expires first it
sends SIGKILL, by which time cleanup has already run.

Only after SIGKILL (`--force`, or a grace period that expired *before* the
interrupt was handled) do these apply:

- **The run is left as `running` in the store.** It is never marked
  `interrupted`, and the phase in progress records no end. `mu2e-power-report
  --run-id <id>` still builds a report from the evidence already stored; it
  shows the run as `not finished (running)` and does not change that status.
- **The run's private Kerberos caches are not destroyed.** Cleanup never
  executes, so the caches it created — including any root-capable service
  tickets — survive the process. On macOS one of them may also still be the
  collection default. Clean up by hand:

  ```sh
  klist -l                        # what survived; look for mu2edaq, mu2eraw, ...
  kswitch -p you@FNAL.GOV         # if the default is not yours
  kdestroy -c <cache>             # destroy each cache the run left behind
  ```

Ctrl-C (SIGINT) takes the same clean path as SIGTERM.

## What the tools will refuse to do

- `chassis power off`, `cycle` and `reset` are refused for the gateways,
  `mu2e-mgr-01` and `mu2e-dcs-01`. No flag overrides this. If you genuinely need
  to power-cycle one of those, do it deliberately from a session on the gateway
  itself, having thought about how you will get back in.
  (`mu2e-node-inventory --protected` lists them.) Powering one *on* is allowed.
- Any power command at all unless this invocation authorised it: `--execute`,
  or `MU2E_POWER_RECOVERY_ARM` equal to the configured `run.label` with
  `run.dry_run: false`. `run.dry_run: false` alone is refused (exit 2), and the
  token is refused in `config/.env`. A live run prints `LIVE RUN -- power
  commands WILL be issued (authorised by ...)`.
- A power command to a node outside `--node`/`--location`, or to a
  predecessor stage of a `--node` run: those are verified only.
- Running a `--from`/`--until` typo: it exits 2 instead of widening the run.
- Anything whatsoever under `--simulate`, which overrides `--execute`. The local
  transport is scripted too, so even a `ping` in a rehearsal is answered from
  the script.
- Publishing the report during a `--simulate` run: it is written locally and the
  publisher stops before touching the live web area. A rehearsal must not
  overwrite the real report.
- Continuing past a stage that did not meet its requirement, unless you pass
  `--continue-on-error`.
- Destroying a credential cache the run did not create. Cleanup names each of
  its own caches to `kdestroy -c` and refuses a file cache outside its own
  temporary directory — the plausible foreign path is your own ticket.

## Afterwards

- `html/` holds the newest run's report; `html/runs/<id>/` holds each run's
  own pages and data, rendered from the store — they never contain another
  run's evidence, and `mu2e-power-report --run-id <id>` rebuilds them.
- `data/power-recovery.db` holds everything, including the full command output
  behind every check.
- `logs/power-recovery.log` holds the run log, rotated.

If something behaved unexpectedly, those three plus the version banner from the
top of the run are what to attach to the ticket. Re-run with `-v` if you can:
the debug log carries the per-attempt credential lines (`login X ticket Y
[cache Z] -> ok/refused`), which is what makes a credential failure readable
after the fact.

Your own Kerberos state is worth capturing too, before you start fixing it:

```sh
klist ; klist -l          # -l on macOS shows the whole collection
```
