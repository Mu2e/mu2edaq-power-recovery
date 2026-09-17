# Design

This document records what the tools are, how they are put together, and why
each significant choice was made rather than its alternative.

## 1. The problem

After a power event at MC-1, MC-2 or the HEERC teststand, someone has to find
out what survived, bring the cluster back in an order that works, prove it
works, and write down what happened. Today that is done by hand from a mix of
`mu2edaq-operations` shell scripts and institutional memory, by whoever is
available, often at an unsociable hour.

Three properties follow from that context and drive everything below:

1. **The operator is outside the DAQ networks.** They have a laptop, a Kerberos
   principal and a Vault token. They do not have a shell on a DAQ node until
   they have made one.
2. **Nothing may be assumed to be up** — including the tools' own
   infrastructure. Vault may be down. GitHub may be unreachable. Half the
   gateways may be dark.
3. **The record matters as much as the recovery.** The logbook entry is what
   the next person reads, and the evidence behind it has to be real.

## 2. Execution model

There are exactly two ways this software touches a machine.

### SSH, with the gateway as the only door

Gateways are contacted directly; everything else through `ssh -J <gateway>`.
ProxyJump rather than nested `ssh host1 ssh host2` because the nesting turns
every remote command into a quoting problem, and the quoting bugs only appear
for the commands with the most interesting output.

Authentication is GSSAPI with delegation: the ticket that opened the gateway
session authenticates the second hop. `BatchMode=yes` is set deliberately — a
missing ticket becomes a fast, legible failure rather than a worker thread
blocked on a password prompt that nobody can see, forty lines up in a parallel
run's output.

No persistent `ControlMaster`. It would be faster, but a recovery touches a
host a handful of times over minutes, and a shared control socket turns one
wedged connection into a stuck fleet.

Root access uses a *separate* Kerberos principal in a *separate* credential
cache, because acquiring the root ticket must not displace the ordinary one,
and an SSH command should select its identity by which environment it is given.

**Credential chains.** No single identity can log in to every node, so each host
is tried against an ordered chain. Three rules govern it:

1. *The operator's own principal is always first*, for root as well as ordinary
   sessions. The run belongs to that identity, and a service credential must
   never be used where a personal one would have done. Nothing displaces it —
   not the per-host memo, not the promotion of a fallback that worked elsewhere.
2. *Service identities are fallbacks, and the run returns to the personal
   ticket.* Nothing mutates the ambient environment or the default credential
   cache: each ssh invocation is handed its own `KRB5CCNAME`, and service
   tickets are minted into private caches via `get-kerberos-ticket --cache`.
3. *Root falls back too, by changing the ticket rather than the login.*
   Authenticating as `mu2edaq` and logging in to the `root` account is something
   a node's `root/.k5login` can authorise. An earlier version refused root any
   fallback on the reasoning that service accounts are ordinary users — true of
   the account, irrelevant to the principal, and wrong.

### IPMI, executed on the gateway

`ipmitool` is never run locally. The IPMI subnets — 192.168.157.0/24 at MC-2,
192.168.150.0/24 at the teststand — are private and not routable from off site.
Every BMC command is an SSH command to a gateway that runs `ipmitool` there.
This is also what the requirements ask for, so the constraint and the
specification agree.

**Credential handling.** `ipmitool -P <password>` puts the BMC password in the
gateway's process table for every user on that machine to read. Instead the
password is written to the remote shell's stdin, read into `IPMI_PASSWORD`, and
picked up by `ipmitool -E`. It appears in no argument vector on either host.
There is a test that asserts this, because it is the sort of property that
quietly regresses.

## 3. Checks as data

A check is a function `(CheckContext) -> CheckResult` registered under a dotted
id (`disk.local`, `net.data`). It gets its transports and thresholds from the
context, returns a structured result, and is forbidden from printing, from
raising for an ordinary failure, and from deciding on its own to change
anything.

That shape buys three things:

- the same check bodies serve phase 1 (assessment), phase 2 (post-power-on
  verification) and the standalone diagnostics;
- which checks apply to which host class is configuration
  (`config/checks.yaml`), not code;
- the entire suite runs against a scripted `FakeTransport` with no cluster, so
  the tests can be run *before* an outage — which is the only time it matters
  that they pass.

A check named in the config with no implementation is reported as `skip` with
an explanation. It is never silently dropped: an unimplemented check must not
be indistinguishable from a passing one.

### Failure containment

`run_check` converts every escape into a result. A check that raises becomes
`UNKNOWN` with its traceback in the detail, because a bug in one check must not
abort the assessment of a fifty-node cluster at three in the morning. A
transport failure becomes `UNKNOWN` rather than `FAIL` — see below.

### FAIL is not UNREACHABLE

The status vocabulary separates "we looked and it is wrong" (`FAIL`) from "we
could not look" (`UNKNOWN`, shown as UNREACHABLE). Collapsing them would make a
machine that is merely unplugged indistinguishable from one that is broken, and
those need different responses.

The node-level roll-up has two rules that fall out of this:

- **Skipped checks are dropped before the roll-up.** A node with no BMC skips
  `power.status`; a node with no PCIe card skips `pcie.devices`. Ranking SKIP
  above OK — which is right for a single check — and then reducing with it
  reported most of the cluster as "n/a". A node whose every *applicable* check
  passed is OK.
- **A node nothing could reach is UNKNOWN, not FAIL,** even though its ping and
  ssh checks individually failed. The individual results keep their own status;
  only the verdict changes.

## 4. Phases

### Phase 0 — self-update

Every invocation checks `origin`, fast-forwards if behind, reruns
`bootstrap.sh` if the pull touched a build input, and re-executes itself once
(guarded by an environment variable) so the new code is the code that runs.

Three deliberate conservatisms:

- **Fast-forward only.** A dirty or divergent working tree is reported and left
  alone. An outage is the worst possible moment for the tool to reconcile
  someone's local edit on their behalf.
- **A failed or slow fetch is a warning.** The network may be part of what is
  broken; the run continues with the checked-out revision.
- **It re-executes rather than pretending.** Python has already imported the
  modules that changed on disk, so continuing in-process would run the old code
  while claiming the new version.

### Phase 1 — assess

Read-only by construction: it runs only registered checks, none of which issue
a state-changing IPMI verb.

The gateways are assessed first and alone. If none can be logged in to, the
phase stops rather than producing fifty identical UNREACHABLE rows — everything
downstream is probed *through* a gateway, so the result would be a fact about
the gateway, reported fifty times.

It also records what phase 2 will need: whether a gateway is usable for IPMI,
how many BMCs answered, and which did not. The point of running phase 1
separately is to learn that the BMC network is unreachable *before* committing
to a power-on sequence, not halfway through one.

Finally it records each BMC's event-log length as a baseline, so phase 2 can
report only the events its own power-on produced. BMC clocks routinely lose
time across an outage, so filtering the log by its own timestamps would
silently discard real events.

### Phase 2 — power on

The sequence is data (`config/power-sequence.yaml`); this module is the engine
that walks it. The order is operational: `mu2e-mgr-01` exports `/home` and the
shared areas, so every later node's mounts fail without it; the CFO distributes
the fabric the readout nodes sync to.

Per stage: read power state → power on what is off → wait for SSH → settle →
run the stage's profile → evaluate `require:` (`all` / `majority` / `any`).

- The **settle wait is skipped** when nothing in the stage actually booted.
  There is no reason to wait thirty seconds for a stage whose nodes were
  already running.
- **`require: majority`** on the readout stage, so one dead tracker node does
  not hold up eleven working ones. The report still lists every failure.
- **A failed stage stops the sequence** by default. Later stages depend on the
  services the earlier ones provide, so continuing produces failures that say
  nothing new. `--from <stage>` resumes after a repair.
- After a power-on, the node's context carries `expect_recent_boot`, so
  `host.uptime` can flag a machine that reports days of uptime — which means
  the IPMI command reached a different chassis than the operator thought.

### Phase 3 — network

Phases 1 and 2 prove each node is reachable *from a gateway*. This proves the
nodes reach *each other*, which a switch that came back with a missing VLAN or
the wrong MTU will fail while every node still looks healthy alone.

- **Full mesh on the data network**, sampled against anchors on the lab and
  IPMI networks. A full mesh is O(N²); on the network the DAQ actually uses,
  and which is small, that is affordable, and elsewhere it is not worth it.
- **One SSH session per source**, not per pair. A full mesh over thirty nodes
  is 870 pairs; 870 sessions would take longer than the rest of the recovery.
  The probe script brackets each target's output with markers and the results
  are split back apart.
- **Jumbo-frame probe**: 8972 bytes of payload plus 8 of ICMP header plus 20 of
  IP header is exactly a 9000-byte frame, sent with DF set, so it fails if any
  hop is not jumbo-clean.
- **The output is interpreted, not just tabulated.** A node that reached
  nothing and a target nobody reached are reported separately, because the
  first is a node or switch-port problem and the second suggests a one-way
  path, ARP or a host firewall. Notes are aggregated with counts and a sample,
  not emitted one per host — a fabric-wide failure would otherwise produce
  fifty identical lines and bury the one that differs.

### Phase 4 — report

Probes nothing; reads the run back out of the store. That is what makes it
repeatable: an operator can regenerate a report hours later, or repost it after
fixing the ECL credentials, without touching the cluster.

The narrative is derived entirely from stored rows, so it cannot disagree with
the evidence tables beside it. Follow-ups are derived too: a check that failed
on three or more nodes is called out as one likely shared cause rather than as
three separate problems.

## 5. State

SQLAlchemy Core over SQLite, with a URL switch to Postgres. Tables: `runs`,
`phases`, `node_states`, `check_results`, `actions`, `events`.

Two design points:

- **Re-running a phase appends, it does not overwrite.** A second assessment
  after a repair must not erase the evidence that the repair was needed.
  "Latest" is what a report page means by current state.
- **`actions` records refusals, not only successes**, and an attempt is written
  *before* the command is issued, so a tool that dies mid-power-cycle still
  leaves a record of what it was doing.

## 6. Reports

Static HTML, Jinja2, Tailwind from a CDN, about forty lines of vanilla
JavaScript for filtering and sorting. No server and no build step: the report
has to be readable from a laptop during an outage, copied to a web area, or
attached to a logbook entry, and any of those rules out a running application.

Every page has a JSON companion, so the report is consumable by the next tool
and not only by a human. The heavy full run export is a separate file from the
readable narrative, so `data/report.json` stays small enough to read.

Per-class and per-area tables are compact roll-ups rather than repeats of the
all-nodes table — rendering the full per-check detail twice doubled the page
weight for no extra information.

## 7. The optional C++ library

`libmu2eprobe` is a parallel TCP reachability sweep: C++17, a C ABI, a
`mu2e-probe` command, pybind11 bindings, OpenMP.

**Why C++ at all.** Phase 1 opens by asking "which of about sixty hosts on
three segments are up". One subprocess per host is dominated by fork/exec; one
Python thread per host turns the wait into a queue behind the GIL. A
non-blocking connect sweep costs one socket and no process per host.

**Why TCP, not ICMP.** A raw ICMP socket needs root or `CAP_NET_RAW`, and these
tools deliberately run as an ordinary user. TCP/22 also answers a more useful
question than "the IP stack is up", and is not filtered on the lab network
because the cluster is administered over it.

**A refused connection counts as reachable.** The machine answered; its sshd
simply is not listening yet. During a power-on that is the normal intermediate
state, and treating it as "down" would report most of the cluster as dead.

**It is strictly optional.** `mu2edaq_power_recovery.sweep` uses the extension
when it is importable and a thread pool otherwise, with identical semantics and
the same outcome vocabulary. Which backend ran is printed in the banner and
recorded in the report, so a timing difference between two runs has an
explanation. A recovery must not depend on a toolchain being present on the
machine driving it.

Where it is actually used in the Python path: choosing a responsive gateway. A
gateway whose chassis is dark costs a full `ConnectTimeout` to discover through
ssh, and during an outage that is the likely case for at least one of them. The
sweep answers in one bounded connect; a gateway that passes it still has to
pass the SSH handshake, because an open port is not a working login.

## 8. Configuration

Five layers, lowest first: built-in defaults, YAML, `config/.env`, environment,
command line. Implemented literally — each layer is applied in turn to one
nested dict — rather than by reading flags at the point of use.

The environment spelling of a key is mechanical (`MU2E_POWER_RECOVERY_` plus
the upper-cased dotted path), so a key added to the YAML is immediately
overridable with no code change and no registration table.

Built-in defaults exist so the tools still run with `config/` deleted. A
missing config file is not an error; an unparseable one is, because silently
running an outage recovery with default thresholds after a typo would be worse
than stopping.

The origin of every overridden value is retained, so the report can state which
layer set a setting — during an outage, "why did it use a five second timeout"
needs an answer that does not involve reading four files.

## 9. Safety model

| Mechanism | What it stops |
|---|---|
| `run.dry_run` default true, `--execute` required | A power command issued by accident |
| `--simulate` overrides `--execute` | A rehearsal that turns out not to be one |
| `protected:` host list, checked before the command is built | Cutting off access to the cluster being recovered |
| Gateway stage is `power_on: false` | Power-cycling the jump host mid-sequence |
| Attempt recorded before it is issued | Losing the audit trail to a crash |
| Password on stdin, `ipmitool -E` | Credentials in a process table |
| Private Kerberos caches, destroyed after the run | Root-capable tickets outliving the recovery |

The protected-host refusal is not overridable by any flag. That is the one
place where the tool declines to do what it is told, and it is deliberate:
powering down a gateway or the NFS server from a remote recovery session is
never the intended outcome of a command typed at three in the morning.

## 10. What was deliberately not built

- **A daemon or web service.** The report is static files. Recovery is an
  operator-driven activity, and a service would be one more thing to be down.
- **A Paramiko/asyncssh SSH implementation.** Driving the `ssh` binary means
  the site's `ssh_config`, GSSAPI setup and host-key policy are automatically
  the ones in force, with no second configuration to keep in step.
- **A native Kerberos binding.** Same reasoning: `kinit`/`klist` use the site's
  `krb5.conf`, and there is no build-time dependency on `python-krb5` on a host
  that may itself be recovering.
- **A reimplementation of the ECL posting protocol.** `ecl-client` already has
  one; a second copy would be a second thing to keep current.
- **Automatic remediation.** The tools power machines on and report what is
  wrong. They do not restart services, remount filesystems or reseat drivers.
  An outage recovery is not the moment to discover what an automatic fix does
  when its assumptions do not hold.
