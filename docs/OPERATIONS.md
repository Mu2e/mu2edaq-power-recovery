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
recovery failed against it.) One difference survives: the probe does not mint
tickets, so it uses whatever is in your **ambient** cache even when
`kerberos.principal` names something else, and lists the service identities
rather than acquiring them. That makes `klist` below the authority on which
ticket you actually hold, not the probe's output.

`mu2e-ipmi-tool` tests the *BMC* credentials, not the run's SSH access: it
reaches the gateway with your ambient ticket only, with no credential chain and
no check of the default cache. Do not read a working `mu2e-ipmi-tool` as
evidence that the recovery can log in.

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

**Spell the stage name right, and check the stage list it prints before you
walk away.** An unmatched `--from` or `--until` is silently ignored, not
rejected: `--from manger` does not error, it starts from the *first* stage and
powers on the whole sequence, readout included. The stage names are the `name:`
keys in `config/power-sequence.yaml` — `gateways`, `manager`, `dataloggers`,
`dcs`, `cfo`, `readout`. A dry run (`mu2e-power-on --from <stage>`, no `--execute`)
lists the stages it would act on, which is the cheap way to confirm the name
took.

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
mu2e-power-report --run-id 17 --post-ecl
```

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

A BMC that simply does **not answer** is not treated this way, and says much the
same thing (`Unable to establish IPMI v2 / RMCP+ session`). After an outage a
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

**Stopping a run is not clean, and you have to tidy up after it.** The stop
script sends SIGTERM, but nothing in the driver installs a SIGTERM handler, so
the process dies where it stands. Two consequences, neither of which the run
can report for itself:

- **The run is left as `running` in the store.** It is never marked
  `interrupted`, and the phase in progress records no end. `mu2e-power-report
  --run-id <id>` still builds a report from the evidence already stored.
- **The run's private Kerberos caches are not destroyed.** Cleanup never
  executes, so the caches it created — including any root-capable service
  tickets — survive the process. On macOS one of them may also still be the
  collection default. Clean up by hand:

  ```sh
  klist -l                        # what survived; look for mu2edaq, mu2eraw, ...
  kswitch -p you@FNAL.GOV         # if the default is not yours
  kdestroy -c <cache>             # destroy each cache the run left behind
  ```

Ctrl-C (SIGINT) *is* handled: it records the interruption, marks the run
`interrupted` and runs cleanup. If you are at the terminal the run is on,
interrupt it there rather than using the stop script. `--force` is documented
as the path where the store may not record the interruption; in practice
neither path records it.

## What the tools will refuse to do

- `chassis power off`, `cycle` and `reset` are refused for the gateways,
  `mu2e-mgr-01` and `mu2e-dcs-01`. No flag overrides this. If you genuinely need
  to power-cycle one of those, do it deliberately from a session on the gateway
  itself, having thought about how you will get back in.
  (`mu2e-node-inventory --protected` lists them.) Powering one *on* is allowed.
- Any power command at all while `run.dry_run` is true, which is the default.
  Note what this does *not* say: `--execute` is one way to set `run.dry_run`
  false, not a second independent gate. `run: {dry_run: false}` in
  `config/power-recovery.yaml` or `config/.env`, or
  `MU2E_POWER_RECOVERY_RUN_DRY_RUN=false` in the environment, arms live power
  commands with no flag on the command line. The run prints `LIVE RUN -- power
  commands WILL be issued.` when that is the case; read the banner.
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

- `html/` holds the report; `html/runs/<id>/` holds this run's copy as you read
  it at the time.
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
