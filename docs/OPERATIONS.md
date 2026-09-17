# Outage runbook

What to actually do, in order. Intended to be followed by someone who did not
write the tools, at an hour when they would rather not be.

---

## Before the outage (do this in daylight)

```sh
cd mu2edaq-power-recovery
git pull && ./bootstrap.sh
pytest                                          # should be all green
mu2e-power-recovery --phase all --simulate      # full rehearsal, contacts nothing
open html/index.html                            # check the report looks right
```

Then confirm your access — this is the part that fails at 3 a.m. if it was
never tested:

```sh
kinit you@FNAL.GOV
kinit -c /tmp/krb5cc_root you/root@FNAL.GOV     # if you use a separate root principal
vault login -method=ldap -address=https://ssivault.fnal.gov:8200
mu2e-vault-ipmi                                 # BMC credentials readable?
mu2e-ssh-probe mu2egateway01 --run true         # gateway answers?
mu2e-ssh-probe mu2e-mgr-01 --root --run 'id -u' # root works through the jump host?
```

If `mu2e-vault-ipmi` reports that the configured field names are not in the
secret, fix `vault.ipmi_user_field` / `vault.ipmi_password_field` in
`config/power-recovery.yaml` now, not during the recovery.

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
   logged in to", nothing else matters yet. Check the gateways have power
   (physically, or from another machine on site) and that your ticket is valid.
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

## Things you may need mid-recovery

```sh
# one node's power state
mu2e-ipmi-tool -n mu2e-trk-03 chassis power status

# the BMC event log — survives an outage the host did not
mu2e-ipmi-tool -n mu2e-trk-03 sel list last 20

# temperatures, if the room's cooling is also in question
mu2e-ipmi-tool -c tracker sdr elist | grep -i temp

# force one node on by hand
mu2e-ipmi-tool -n mu2e-trk-03 --execute chassis power on

# what ssh command is the tool actually running?
mu2e-ssh-probe mu2e-trk-03 --root

# re-check one node only
mu2e-power-state --node mu2e-trk-03 -v

# stop a run that is going wrong
./stop-mu2edaq-power-recovery.sh
```

## What the tools will refuse to do

`chassis power off`, `cycle` and `reset` are refused for the gateways,
`mu2e-mgr-01` and `mu2e-dcs-01`. No flag overrides this. If you genuinely need
to power-cycle one of those, do it deliberately from a session on the gateway
itself, having thought about how you will get back in.

## Afterwards

- `html/` holds the report; `html/runs/<id>/` holds this run's copy as you read
  it at the time.
- `data/power-recovery.db` holds everything, including the full command output
  behind every check.
- `logs/power-recovery.log` holds the run log, rotated.

If something behaved unexpectedly, those three plus the version banner from the
top of the run are what to attach to the ticket.
