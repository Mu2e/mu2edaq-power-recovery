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
is tried against an ordered chain. Four rules govern it:

1. *The operator's own principal is always first*, for root as well as ordinary
   sessions. The run belongs to that identity, and a service credential must
   never be used where a personal one would have done. Nothing displaces it —
   not the per-host memo, not the promotion of a fallback that worked elsewhere.
2. *Service identities are fallbacks, and the run returns to the personal
   ticket.* Each ssh invocation is handed its own `KRB5CCNAME`; nothing writes
   the ambient environment.
3. *Root falls back too, by changing the ticket rather than the login.*
   Authenticating as `mu2edaq` and logging in to the `root` account is something
   a node's `root/.k5login` can authorise. An earlier version refused root any
   fallback on the reasoning that service accounts are ordinary users — true of
   the account, irrelevant to the principal, and wrong.
4. *The login is not derived from the principal.* See below; this was
   implemented the other way round once, and reverted.

Every attempt is logged as `login X ticket Y [cache Z] -> ok/refused`, and the
primary pair is logged before anything is tried. A refused chain records the
login, principal and cache of each attempt, so the report carries them. Without
that, "Permission denied (gssapi)" fifty times is not a diagnosis.

#### Why only an authentication failure advances the chain

The chain exists to find an identity a host will accept. Only a failure that
says *this identity was rejected* is evidence that another might do better.
Everything else is a fact about the host or the path, and repeating it seven
more times costs seven more connect timeouts and learns nothing. So
`classify_ssh_failure` sorts stderr into four buckets, and the order of the
tests is load-bearing:

1. **hostkey** — `Host key verification failed`, `REMOTE HOST IDENTIFICATION HAS
   CHANGED`, and the DNS-spoofing and man-in-the-middle wordings. Tested
   *first*, because ssh aborts on a host-key mismatch before it authenticates
   anything, so any credential-sounding text further down the output is noise.
   Stops the chain at the first attempt.
2. **unreachable** — refused connections, timeouts, and the rate-limiting
   family: `kex_exchange_identification`, "connection reset by peer", "too many
   authentication failures". Tested before `auth` because a jump host that is
   down prints both a connection error *and*, further down, something about
   credentials; reading that as an auth failure sends the chain through every
   identity against a machine that is simply off. Rate limiting belongs here for
   the same practical reason: six more identities is what caused it.
3. **auth** — the only bucket that advances the chain.
4. **unknown** — also advances it. The cost is one extra attempt; the
   alternative is refusing an identity that might have worked because ssh
   phrased its complaint in a way we did not anticipate.

A changed host key gets more than a classification. `ssh.login` reports it as
what it is — *the host key has CHANGED, ssh refused before authenticating* —
names reimage-after-outage as the likely cause, points at `ssh-keyscan` from a
gateway as the out-of-band check, and sets `hostkey_changed` in the result data.
This is not general good manners; it is specific to an outage tool. Reimaged
nodes are precisely what a recovery meets, and the alternative reading of a
changed key is interception, so the one thing the tool must not do is quietly
try eight credentials against it and report "SSH login failed".

A refused login also stops the node's remaining SSH checks. Previously a node
counted as unreachable only when ping *and* ssh both failed, so one refusal was
followed by twelve more checks each opening its own connection — against a
server that was very likely refusing because of the connection rate.

#### Why the login is not derived from the principal

`~/.ssh/config` sets `User mu2edaq` for the DAQ hosts. That looked like the bug
when the personal ticket was being refused everywhere, and the login was changed
to come from the principal instead (`anorman@FNAL.GOV` → `anorman`).

Verified against the live cluster, that was wrong. `mu2edaq` and `root` both
accept the personal ticket; an account named after the principal does not exist
on those hosts, so deriving the login broke logins that had been working.
`ssh_config` was right all along, and the real failure was the displaced default
credential cache described below — a single cause that produced a symptom
pointing at something else entirely. The derivation was reverted and the
reasoning recorded in the code, because it is an attractive-looking fix that
will suggest itself again. `ssh.user` and `ssh.root_user` remain the supported
overrides.

This is also the cost of the decision to drive the `ssh` binary (§10): the
site's `ssh_config` is in force, which is what we want, and it means the login
is not ours to infer.

#### Credential caches are not always files

The intent is simple: acquiring a root ticket must not displace the ordinary
one, so each principal gets a private cache and each ssh invocation is given its
own `KRB5CCNAME`. On MIT Kerberos that is exactly what happens.

macOS ships **Heimdal**, and it does not work that way. Heimdal keeps credential
caches in an `API:` *collection* rather than as files, and two consequences
follow that this project had to be rebuilt around:

- **Heimdal ignores `KRB5CCNAME=FILE:` for `kinit`.** The private cache file we
  asked for never appeared, so every service identity was rejected as unusable
  even though the ticket was perfectly good. It simply has a ccache *name*
  (`API:<uuid>`) rather than a path. The ticket source now looks the identity up
  in the collection (`klist -l`) and uses that name;
  `Credential.environ()`/`describe()` pass through a value that already carries
  a ccache type untouched, and `KerberosManager.ensure()` has the same fallback
  — without it, `--principal` did not work at all on the platform the recovery
  is most likely to be driven from, reporting "kinit appeared to succeed but no
  valid ticket is present".
- **Minting displaces the default; it does not destroy it.** Heimdal makes each
  newly minted cache the collection default, whatever `--cache` or `KRB5CCNAME`
  said. So minting a service ticket silently repoints "the default ticket" at
  it, and every later login runs as that identity. The first diagnosis of this
  was that the operator's ticket had been *destroyed*; that was wrong, and the
  distinction matters, because a displaced ticket is still in the collection and
  `kswitch -p <principal>` restores it instantly. The tools now record the
  default principal before each mint and put the pointer back afterwards. A mint
  that *times out* runs the check and restore too — the command had run, so it
  could have moved the default and then hung, which is the shape of the original
  incident — and a clobber is reported in preference to the timeout.

Two guards follow from this, and both are about the *next* run rather than this
one. Before connecting to anything, a run reads the default cache and, when it
holds a service identity rather than a personal principal, logs a warning and
prints the `kswitch`/`kdestroy` line that fixes it: otherwise sixty logins fail
as `mu2eraw` with nothing in the ssh error to explain why.

**This guard warns; it does not stop the run.** `Orchestrator.prepare()` calls
`KerberosManager.ambient_warning()`, logs whatever comes back and appends it to
the run's notes, then continues into `prepare()`, the SSH factory and every
phase. The design intent was to halt, and halting is arguably right — an
operator who misses one WARNING line then watches every login fail for a reason
that was printed once, at the top, two hours earlier. It is recorded as an open
item rather than changed here, because `ambient_warning()` returns a string for
*two* conditions and only one of them is fatal: a service identity holding the
default cache (every login will be refused), and the benign case where the
ambient principal merely differs from `kerberos.principal` (the run uses the
configured one and is fine). Halting on a non-empty return would stop runs that
should proceed; the fix is to separate the two conditions, which is a code
change with an author's judgement in it. Read the top of the run.

And cleanup names each
cache to `kdestroy -c` rather than steering a bare `kdestroy` with
`KRB5CCNAME` — `FILE:API:<uuid>` names nothing, so on macOS every service ticket
used to survive the run and sit in the collection waiting to become its default.
Cleanup refuses outright any *file* cache whose path is not inside the run's own
temporary directory: the plausible foreign path is the operator's own.

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

**The invocation matches upstream `mu2e_ipmi.sh` exactly, apart from `-E`.**
`-N 5 -R 1` was once added as tuning; `-R 1` cuts ipmitool to a single attempt,
so a BMC that needed a retry failed with "Unable to establish IPMI v2 / RMCP+
session" — indistinguishable from bad credentials. `ipmi.message_timeout` and
`ipmi.tool_retries` now default to unset, `ipmi.extra_args` exists for anything
genuinely needed, and the gateway-side wall-clock bound was widened so it cannot
itself cut ipmitool's retries short. Diverging from a known-working invocation
is a choice that has to earn itself.

**A credential rejection is neither retried nor repeated.** All 45 BMCs share
one credential set, so the first rejection settles the matter: the run stops
issuing IPMI and reports one diagnosis naming the refused username and how to
find the right one. Previously it retried the same rejected credentials three
times per BMC, with four ipmitool retries inside each, and then did the same to
the next BMC — a few hundred failed authentications against machines that count
them towards account lockout. Recognised only from RAKP failures and
"unauthorized name". "Unable to establish IPMI v2 / RMCP+ session" is
deliberately **excluded**: a dark chassis says exactly that, and after an outage
a dark chassis is the expected case. `ipmi.stop_on_auth_failure: false`
overrides the whole behaviour.

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

That last claim is enforced rather than asserted: an autouse fixture fails any
test that shells out to `ssh`, `ping`, `ipmitool`, `kinit`, `vault` and the
rest, with an `allow_network` marker to opt out. It was added after the suite
was found opening real ssh connections to the gateways — resolving a gateway
probes it, and any test building a transport resolved one — and extended after
a `--simulate` run was found pinging them.

### The command must be in the dialect of the host that runs it

`ping` is not one program. iputils reads `-W` as **seconds**, BSD `ping` reads
the same flag as **milliseconds**, and Windows spells the pair `-n`/`-w` and
shares neither the flags nor the output wording. Sending the iputils form
everywhere turned a five-second per-packet wait into five milliseconds whenever
a recovery was driven from a Mac, and both gateways duly reported no ICMP at
all — a wrong answer that looked exactly like a real finding.

So `ping_dialect(transport)` picks the spelling from `Transport.platform`
(`base.py` defaults to `linux`; `LocalTransport` reports `sys.platform`), and
the parser reads the Windows summary as well. The MTU probe's iputils-only
`-M do`/`-s` options are deliberately left alone: only phase 3 asks for them,
and phase 3 always runs on a gateway.

The general point is that a check runs on whichever host its transport points
at, and that host is not necessarily the one the tests ran on.

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

Every invocation that will contact the cluster checks `origin`, fast-forwards
if behind, reruns `bootstrap.sh` if the pull touched a build input, and
re-executes itself once (guarded by an environment variable) so the new code is
the code that runs.

Two invocations skip it, both deliberately: `--simulate`, because a rehearsal
should not depend on the network or change the code under test halfway through,
and `--list-checks`, which returns before phase 0 is reached. `--list-nodes`
*does* run it, since it is handled after the orchestrator is built.

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
- **One SSH session per source**, not per pair. The shipped topology has 49
  nodes on the data network, so the mesh is 49x48 = 2352 ordered pairs — a
  simulated run reports `data: 2352/2352 paths ok`. As 2352 SSH sessions that
  would take longer than the rest of the recovery; as 49 sessions, one per
  source, it is affordable. The probe script brackets each target's output with
  markers and the results are split back apart.
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

That resolution is **serialised**. `SSHFactory.gateway_for()` had an unguarded
check-then-set cache, and every worker thread asks for its location's gateway
before its first command — so on a cold cache all sixteen threads probed the
same two gateways at once, each a TCP sweep plus a full SSH handshake per
credential in the chain. Against a gateway already refusing logins that is a
couple of hundred connections in the first second of a phase. The cache is cold
exactly when it matters, too: it is `_make_ipmi_client` that happens to warm it,
and that path returns early when Vault has failed, while phase 3 and
`mu2e-power-netcheck` can start cold outright. The first caller now probes and
the rest wait behind a lock.

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
| Private Kerberos caches, destroyed by name after the run | Root-capable tickets outliving the recovery |
| Cleanup refuses a file cache outside the run's own directory | Destroying the operator's own ticket |
| Default-cache principal recorded before each mint and restored after | A service identity silently becoming the run's identity |
| Startup *warning* on the default credential cache (it does not stop the run) | A whole run attempted as `mu2eraw`, with nothing to say why |
| A credential rejection stops IPMI for the run | Locking out 45 BMC accounts with one wrong password |
| `Publisher` refuses to publish under `--simulate` | A rehearsal overwriting the live report |

The protected-host refusal is not overridable by any flag. That is the one
place where the tool declines to do what it is told, and it is deliberate:
powering down a gateway or the NFS server from a remote recovery session is
never the intended outcome of a command typed at three in the morning.

`--simulate` needs the whole row, not just the SSH and IPMI transports.
`CheckContext.prober` has nothing closer to a gateway than the machine driving
the run, so for `node_class: gateway` it falls back to `ctx.local` — and while
the simulate branch replaced the SSH factory and the IPMI client, it left
`self.local` as a real `LocalTransport`. `ping.lab` therefore shelled out and
pinged `mu2egateway01` for real during a run whose entire claim is that it
contacts nothing, which also made two phase tests pass or fail according to
whether the workstation could reach Fermilab that second. The local transport is
scripted under `--simulate` too. For the same reason `Publisher` takes a
`simulate` flag and returns before any other test: `_copy` uses
`shutil.copytree` directly rather than going through the transport, so scripting
transports is not enough to hold it back.

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
