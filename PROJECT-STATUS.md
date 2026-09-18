# Project status — mu2edaq-power-recovery

**Version** 0.1.0 (tagged) · **Status** feature-complete, not yet exercised
against the live cluster · **Repository**
<https://github.com/Mu2e/mu2edaq-power-recovery> (public, per the Mu2e-org
convention) · **Last updated** 2026-09-17

---

## 1. Summary

All four phases specified in `Project-Description.md` are implemented, together
with the phase-0 self-update, the static report site, the logbook integration,
the diagnostics utilities, the optional C/C++ probe library and its Python
bindings, and the documentation set.

293 automated tests pass, plus 7 C++ test groups and an end-to-end simulated
four-phase run. **Nothing has yet been run against the real cluster** — the
tests and the rehearsal deliberately contact nothing, so what is verified is
the logic, not the environment. Two items remain open (§6): the MC-1 node list,
and live-cluster verification.

---

## 2. Requirements traceability

Every requirement from `Project-Description.md`, and where it is met.

| # | Requirement | Status | Where |
|---|---|---|---|
| 1 | Runs from outside the DAQ networks | ✅ | `transport/ssh.py` ProxyJump; `transport/ipmi.py` runs on the gateway |
| 2 | Uses a Kerberos principal for cluster access | ✅ | `creds/kerberos.py` |
| 3 | Uses a Vault token for secrets | ✅ | `creds/vault.py` |
| 4 | Report hosting local or remote upload | ✅ | `report/publish.py` (rsync / scp / copy) |
| 5 | Secrets from `td/scd/experiments/mu2e/`, IPMI at `.../ipmi/config` | ✅ | `config/power-recovery.yaml` `vault:`; path and fields confirmed |
| 6 | Three computing centres: MC-1, MC-2, Teststand | ⚠️ | `config/topology.yaml`; **MC-1 node list empty** (§6.1) |
| 7 | Node names from `mu2edaq-operations` `node_list.py` | ✅ | `topology.py` uses the identical entry syntax |
| 8 | Per-site network segments and CIDRs | ✅ | `topology.yaml` `subnets:` |
| 9 | Current-state tool, on-screen report | ✅ | `console.py`, `mu2e-power-state` |
| 10 | Webpage with tables by area and by class | ✅ | `report/templates/assess.html` |
| 11 | Re-running updates the webpage | ✅ | `ReportWriter.write_phase` rewrites in place |
| 12 | Check for GitHub updates and rebuild first | ✅ | `selfupdate.py` |
| 13 | Print version information | ✅ | `version.py` banner |
| 14 | Phase 1 takes no corrective action | ✅ | Read-only checks; asserted by test |
| 15 | Gateways responding | ✅ | `ping.lab` |
| 16 | Gateway login with a root-capable ticket | ✅ | `ssh.login`, `ssh.login_root` |
| 17 | Designate general and root principals | ✅ | `--principal`, `--root-principal` |
| 17a | Fall back to service keytabs when a login fails | ✅ | `creds/ticketsource.py`; cycles all identities |
| 18 | Prompt for a password when needed | ✅ | `KerberosManager._kinit` |
| 19 | Gateway disk mounts match normal config | ✅ | `disk.mounts` |
| 20 | From the gateways, check other machines respond | ✅ | `CheckContext.prober` |
| 21 | From the gateways, check power status | ✅ | `power.status` |
| 22 | Root login to other machines; disks and interfaces | ✅ | `ssh.login_root`, `disk.*`, `net.interfaces` |
| 23 | Other low-level health checks | ✅ | 25 checks; `--list-checks` |
| 24 | "Initial state" pages after phase 1 | ✅ | `initial-state.html` |
| 25 | IPMI issued from gateway01 or gateway02 | ✅ | `SSHFactory.gateway_for` picks a responsive one |
| 26 | `mu2e-mgr-01` first after the gateways | ✅ | `power-sequence.yaml` stage `manager` |
| 27 | Ensure it is on (IPMI on/off) | ✅ | `IPMIClient.ensure_on` |
| 28 | Root login works | ✅ | `ssh.login_root` |
| 29 | Disks up and not reporting errors | ✅ | `disk.local`, `disk.smart`, `disk.errors`, `disk.raid` |
| 30 | `mu2edaq` and `mu2eshift` can log in | ✅ | `login.users` (via `su -`, so it tests `/home` too) |
| 31 | Network interfaces up | ✅ | `net.interfaces` |
| 32 | Then `mu2e-dl-01`, `mu2e-dl-02` | ✅ | stage `dataloggers` |
| 33 | Then `mu2e-dcs-01`, `mu2e-dcs-02` | ✅ | stage `dcs` |
| 34 | DCS mounts verified from `mu2e-mgr-01` | ✅ | `disk.nfs_from_mgr` |
| 35 | Then `mu2e-cfo-01`, incl. attached devices | ✅ | stage `cfo`; `pcie.devices`, `pcie.driver` |
| 36 | Then crv-01, trk-01..14, calo-01..11, stm-01..02 | ✅ | stage `readout` |
| 37 | Health checks on each; "power on" pages | ✅ | `power-on.html` |
| 38 | Phase 3 inter-node connectivity per interface | ✅ | `checks/mesh.py`, `network.html` |
| 39 | Third set of webpages | ✅ | `network.html` |
| 40 | Phase 4 detailed report of all steps | ✅ | `detail.html`, `phase4_report.py` |
| 41 | Postable to the ECL via `ecl-client` | ✅ | `report/ecl.py` |

---

## 3. Component status

| Component | Status | Tests | Notes |
|---|---|---|---|
| Configuration layering | ✅ Complete | 23 | 5 layers, provenance retained, redaction |
| Topology / inventory | ✅ Complete | 14 | Upstream-compatible entry syntax |
| SSH transport | ✅ Complete | via checks | ProxyJump, GSSAPI, no ControlMaster |
| IPMI client | ✅ Complete | 25 | Runs on the gateway; secrets on stdin |
| Kerberos | ✅ Complete | 33 | Credential chains over the Mu2e service identities |
| Service identities | ✅ Complete | 33 | Via `mu2edaq-kerberos`; no keytab handled here |
| Vault | ✅ Complete | 11 | Path and fields confirmed against the live Vault |
| Check framework | ✅ Complete | 36 | 25 checks; registry; failure containment |
| Output parsers | ✅ Complete | 19 | Real command output as fixtures |
| Phase 1 assess | ✅ Complete | 6 | Read-only asserted by test |
| Phase 2 power on | ✅ Complete | 8 | Stage order, requirements, dry run |
| Phase 3 network | ✅ Complete | 4 | Full mesh + MTU probe |
| Phase 4 report | ✅ Complete | 4 | Narrative derived from the store |
| Run store | ✅ Complete | 8 | SQLite; Postgres by URL |
| Report site | ✅ Complete | 19 | 9 pages + JSON companions |
| Publication | ✅ Complete | 3 | rsync / scp / copy |
| ECL posting | ⚠️ Untested | 3 | Body and subject tested; posting needs the package (§6.2) |
| Self-update | ✅ Complete | 13 | Against real throwaway git repos |
| `libmu2eprobe` (C++) | ✅ Complete | 7 groups | C++ / C / Python surfaces |
| ssh credential chain | ✅ Complete | 33 | Per-host memo, promotion, auth-vs-unreachable |
| Python sweep fallback | ✅ Complete | 8 | Semantics identical to the native path |
| CLI | ✅ Complete | 14 | Driver + 4 single-phase entry points |
| Diagnostics (4 tools) | ✅ Complete | — | Exercised manually; see §6.4 |
| Man pages | ✅ Complete | — | 17 pages, all render warning-free |
| Documentation | ✅ Complete | — | README, INSTALL, DESIGN, OPERATIONS |

---

## 4. Test matrix

`pytest` — **293 passed**, no cluster, no credentials, no network.

| Suite | Tests | Covers |
|---|---|---|
| `unit/test_topology.py` | 14 | NodeRange expansion, aliases, classes, protection, MC-1 emptiness |
| `unit/test_settings.py` | 23 | All five precedence layers, coercion, redaction, malformed YAML |
| `unit/test_parsers.py` | 20 | `df`, `ip`, `ping` (iputils + BSD + Windows), `mdstat`, SMART, kernel errors |
| `unit/test_checks.py` | 39 | Every check's pass and fail path; framework containment; ping dialects |
| `unit/test_ipmi.py` | 43 | **Safety gates**, credentials, invocation shape, failure diagnosis, credential stop |
| `unit/test_state.py` | 8 | Round-trip, refusal auditing, append-not-overwrite |
| `unit/test_vault.py` | 11 | KV path resolution, folder-vs-secret, synonyms, file fallback |
| `unit/test_credentials.py` | 55 | Primary-first chains, root fallback, ssh-failure classification, KRB5CCNAME, ccache protection |
| `unit/test_network_guard.py` | 2 | The suite's "contacts nothing" claim is enforced, not just asserted |
| `unit/test_sweep.py` | 8 | Both backends, identical semantics |
| `unit/test_selfupdate.py` | 13 | Dirty tree, divergence, fast-forward, re-exec guard |
| `integration/test_phases.py` | 23 | All four phases end to end; simulation hermeticity |
| `integration/test_report.py` | 20 | Page rendering, archiving, publication, ECL body |
| `integration/test_cli.py` | 14 | Driver, flags, exit codes, JSON output |

**C++** — `ctest`, 7 test groups: version/OpenMP agreement, unresolvable names,
loopback, timeout bounding, input-order preservation, the reachable filter, and
the C ABI. Uses loopback and RFC 5737 TEST-NET-1 only, so it needs no network.

**End to end** — `mu2e-power-recovery --phase all --simulate` runs all four
phases over the 48-node MC-2 inventory, generates all 9 pages and 7 JSON files,
and is wired into `ctest` as `simulated-run`.

### Safety assertions (the ones that matter most)

| Assertion | Test |
|---|---|
| The BMC password never appears in a command line | `test_the_password_never_appears_in_a_command_line` |
| The password is delivered on stdin | `test_the_password_is_delivered_on_stdin` |
| `ipmitool` uses `-E`, never `-P` | `test_ipmitool_uses_the_environment_password_option` |
| Destructive verbs refused for protected hosts, even with `--execute` | `test_every_destructive_verb_is_refused_for_a_protected_host` |
| Power-*on* is still allowed for protected hosts | `test_powering_on_a_protected_host_is_allowed` |
| A dry run issues nothing | `test_a_dry_run_issues_nothing` |
| A dry run still *reads* state | `test_a_dry_run_still_reads_state` |
| `--simulate` overrides `--execute` | `test_simulate_forces_dry_run_even_with_execute` |
| Phase 1 takes no corrective action | `test_assess_takes_no_corrective_action` |
| The gateway stage never issues a power command | `test_the_gateway_stage_never_issues_a_power_command` |
| Refusals are recorded, not just successes | `test_actions_record_refusals_as_well_as_successes` |
| Re-running a phase does not erase earlier evidence | `test_rerunning_a_phase_appends_rather_than_overwrites` |

---

## 5. Compatibility matrix

| Platform | Python | Tools | C++ library | Status |
|---|---|---|---|---|
| Alma/Rocky/RHEL 9 | 3.9 (system) | ✅ | ✅ GCC + OpenMP | Target deployment |
| Alma/Rocky/RHEL 8 | 3.9 | ✅ | ✅ | Expected to work; untested |
| Ubuntu 22.04 / 24.04 | 3.10–3.12 | ✅ | ✅ | Expected to work; untested |
| macOS 14+ (arm64) | 3.12 | ✅ Verified | ✅ Built and tested, no OpenMP by default | **Development platform** |
| Windows 11 | 3.9+ | ✅ `bootstrap.ps1` | ⚠️ MSYS2/MinGW only | Needs OpenSSH + MIT Kerberos |

| Dependency | Version | Required? | Without it |
|---|---|---|---|
| Python | ≥ 3.9 | Yes | — |
| PyYAML, Jinja2, SQLAlchemy, hvac | current | Yes | — |
| OpenSSH client | any | Yes | No node access |
| Kerberos client | any | Yes | No authentication |
| git | any | For phase 0 | Self-update is skipped |
| CMake ≥ 3.16, C++17 | — | No | Python sweep fallback |
| OpenMP | — | No | Sweep runs serially |
| pybind11 | — | No | Python fallback |
| CppUnit | — | No | Built-in C++ runner |
| `ecl-client` | — | No | Report produced, not posted |
| psycopg2 | — | No | SQLite |
| `ipmitool` | on the gateway | Yes for phase 2 | No power control |

**Verified on this machine:** macOS 14 (arm64), Python 3.12.1 — 201 pytest
tests, 7 C++ test groups, the simulated four-phase run, all 17 man pages, and
`bootstrap.sh` from scratch.

---

## 6. Open items

### 6.1 MC-1 inventory — **needs input**
`config/topology.yaml` defines `mc1` with its lab subnet (131.225.246.0/24) and
records that it has no data network, but the node list is empty. MC-1 is not
carried in `mu2edaq-operations/scripts/nodes_config.yaml`, the authoritative
upstream inventory, so nothing could be imported. Adding the hostnames is a
config edit; no code change is needed. Until then the tools report MC-1 as
"no nodes configured" rather than as healthy.

### 6.2 Vault IPMI secret — ✅ **resolved 2026-09-17**
Both parts confirmed against the live Vault:

- **Path:** `td/scd/experiments/mu2e/ipmi/config`. `ipmi` is a *folder* in the
  KV v2 tree, not the secret; the original default of `ipmi` read the folder
  and returned nothing, which is indistinguishable from an empty secret.
  `vault.ipmi_path` now defaults to `ipmi/config`, and `mu2e-vault-ipmi` lists
  the tree and names the secret inside when handed a folder, so the same
  mistake now reports itself. `--list` browses deliberately.
- **Fields:** `username` and `password` — the shipped defaults.

Both remain configurable, and the synonym fallback is kept, because the secret
is maintained outside this repository and could be re-keyed without anything
here noticing until a recovery needs it. That is a fallback, not a doubt.

### 6.2a BMC username case — **needs a config line**
The Vault secret's `username` field holds `mu2e`; the working upstream
`mu2e_ipmi.sh` hard-codes `MU2E`, and IPMI usernames are case sensitive. A BMC
therefore refuses the session with "Unable to establish IPMI v2 / RMCP+
session".

`mu2e-ipmi-tool --diagnose -n <node> chassis power status` tries the plausible
usernames and cipher suites, read-only and capped at nine attempts, and prints
the change to make. Expected outcome is `ipmi.username: MU2E` in
`config/power-recovery.yaml`, which overrides Vault without touching a secret
maintained outside this repository.

Not set as the default here because it is unconfirmed which of the two the BMCs
actually accept — that needs one run of `--diagnose` against a live BMC.

### 6.3 Live-cluster verification — **not yet done**
Untested against real infrastructure, by design of the test suite:
Kerberos ticket acquisition against the FNAL KDC; Vault reads against
`ssivault.fnal.gov`; SSH through a real gateway; `ipmitool` on a real gateway
against a real BMC; an actual power-on; ECL posting.

Recommended sequence, in daylight: `mu2e-vault-ipmi` → `mu2e-ssh-probe
mu2egateway01 --run true` → `mu2e-ipmi-tool -n <one node> chassis power status`
→ `mu2e-power-state` → a dry-run `mu2e-power-on` → a real `mu2e-power-on
--execute --until manager` on a maintenance day.

### 6.4 Fixed during development, worth knowing
- **A `--simulate` run pinged the real gateways, and two phase tests failed
  intermittently because of it.** `CheckContext.prober` has nothing closer to a
  gateway than the machine driving the run, so for `node_class: gateway` it
  falls back to `ctx.local` — and the orchestrator's simulate branch replaced
  the SSH factory and the IPMI client with scripted stand-ins but left
  `self.local` as the real `LocalTransport`. `ping.lab` therefore shelled out
  and pinged `mu2egateway01` for real during a run that promises to contact
  nothing, and `test_a_failed_stage_stops_the_sequence` and
  `test_report_summarises_the_stored_run` passed or failed according to whether
  the workstation could reach Fermilab that second (the observed headline,
  `1/2 node(s) healthy; 1 failed`, is the gateway failing `ping.lab`). A
  simulated run's local transport is now scripted too. The guard that should
  have caught this did not list `ping`; it does now, so the same mistake fails
  loudly instead of intermittently. Two adjacent faults were fixed with it: BSD
  `ping` reads `-W` as *milliseconds* where iputils reads it as *seconds*, so
  driving a recovery from a Mac turned the 5 s per-packet wait into 5 ms and
  reported both gateways as answering no ICMP at all (Windows `ping` shares
  neither spelling nor output wording); and a simulated run would still have
  published its report to the live web area, because `Publisher._copy` writes
  with `shutil.copytree` rather than through the transport.
- **Two bursts a live run would have sent at the infrastructure.** Both the
  same shape as the refused-ssh burst below. (i) `SSHFactory.gateway_for()` had
  an unguarded check-then-set cache, and every worker thread asks for its
  location's gateway before its first command. On a cold cache — which is what
  a run gets whenever Vault fails, since it is `_make_ipmi_client` that
  happens to warm it — all sixteen threads probed the same two gateways at
  once, each a TCP sweep plus a full SSH handshake per credential in the
  chain. Against a gateway already refusing logins that is a couple of hundred
  connections in the first second of a phase. The probe is now serialised: the
  first caller does it, the rest wait for the answer. (ii) `IPMIClient` retried
  a BMC that had *answered and rejected the credentials*, three invocations
  with four ipmitool retries inside each, and then did the same to the next
  BMC — all 65 of them share one credential set. Given §6.2a is still open,
  that is the most likely outcome of the first live run. A credential
  rejection (RAKP, "unauthorized name") is no longer retried and now stops the
  run's IPMI with one clear diagnosis; `ipmi.stop_on_auth_failure: false`
  overrides it. A BMC that simply does not answer is deliberately not treated
  this way — "Unable to establish IPMI v2 / RMCP+ session" is what a dark
  chassis says too, and after an outage that is the expected case.
- **Four remaining ways a run could have damaged the operator's credentials.**
  Found by reviewing the fix above adversarially rather than by a failure.
  (i) `cleanup()` ran a bare `kdestroy` steered only by `KRB5CCNAME` — the same
  assumption that destroyed the ticket in the first place, and here it would
  have run at the end of *every* recovery rather than once; the cache is now
  named with `-c`, and any path outside the run's own temporary directory is
  refused outright. (ii) `KerberosManager._kinit`, which mints the operator's
  own *root-capable* principal, was still steered by `KRB5CCNAME` alone: the
  service mint had been hardened, the personal one had not. It now passes
  `-c FILE:<cache>` as well, and compares the default cache before and after.
  (iii) `default_principal()` returned `None` both when there was no default
  cache and when `klist` could not be run at all, so on a host with an unusual
  Kerberos installation the before/after comparison became `None == None` and
  passed with the guard silently disabled. "Could not look" is now a distinct
  exception, and a mint that cannot be checked is refused rather than
  attempted — which costs only the fallbacks, since the operator's own
  principal is position 0 of every chain. (iv) A `get-kerberos-ticket` that
  *timed out* skipped the default-cache comparison entirely, so a command that
  clobbered the cache and then hung was reported as a timeout and the chain
  moved on to the next six identities, each doing it again. The comparison now
  runs on the timeout path too and takes precedence over the timeout in the
  error. Also: the decision to abandon service identities for a whole run was
  carried by a substring of an error message, and is now carried by the
  exception type (`DefaultCacheClobbered`), with the substring kept underneath
  as a fallback.
- **Service-identity minting destroyed the operator's Kerberos ticket.** Found
  on the first live run. macOS ships Heimdal, whose default cache type is
  `API:`; `get-kerberos-ticket` sets `KRB5CCNAME=<bare path>`, which Heimdal
  does not read as a file cache, so all seven identities wrote into the
  operator's *default* cache instead of ours -- leaving `mu2eraw` as the
  ambient principal and making every later login, including `root@`, fail with
  "Permission denied (gssapi)". Fixed three ways: an explicit `FILE:` type on
  `--cache`, `KRB5CCNAME` also set in the subprocess environment, and a
  before/after comparison of the default cache that aborts the run with
  recovery instructions if it ever changes again.
- **The primary login was left to ssh_config.** `~/.ssh/config_mu2e` sets
  `User mu2edaq` for the DAQ hosts, so an unqualified ssh authenticated the
  personal ticket into the service account and was refused. The personal
  credential's login is now derived from the principal (`anorman@FNAL.GOV` ->
  `anorman`), with `ssh.user` still overriding.
- **A refused login was retried twelve more times per node.** Only `ping` and
  `ssh` both failing marked a node unreachable, so a host answering ICMP but
  refusing ssh had every remaining check open its own connection -- against a
  server already rate-limiting. A refused login now skips the remaining ssh
  checks, and `kex_exchange_identification` / `connection reset` stop the
  credential chain instead of advancing it.
- **The credential chain demoted the operator's own principal.** A service
  identity that had worked for a host was promoted to the front of that host's
  chain, so a later transport tried it before the personal ticket; and root
  sessions were given no fallback at all, on the reasoning that service
  accounts are ordinary users — true of the account, irrelevant to the
  principal. Both corrected: the personal principal is now position 0 in every
  chain, promotion reorders only the fallbacks among themselves, and root falls
  back by changing the ticket while keeping the `root` login.
- **The test suite made real ssh connections to the gateways.** Any test
  building a transport through `SSHFactory.for_node()` resolved a gateway, and
  resolving a gateway probes it. The suite's central claim is that it contacts
  nothing, so that is now enforced by an autouse fixture that fails a test which
  shells out to ssh, kinit, ipmitool or vault, with an `allow_network` marker to
  opt out.
- **IPMI sessions were refused on the real cluster.** The invocation carried
  `-N 5 -R 1`, added as "tuning", which cuts ipmitool to a single attempt; a
  BMC that needs a retry then fails with "Unable to establish IPMI v2 / RMCP+
  session". The upstream `mu2e_ipmi.sh` passes neither flag and relies on
  ipmitool's default of four retries. Both are now unset by default and the
  invocation otherwise matches upstream exactly, apart from `-E` in place of
  `-P`. Covered by `test_the_invocation_matches_the_known_working_upstream_form`
  and `test_ipmitool_retry_flags_are_not_sent_by_default`.
- **Designated principals had no effect.** `KerberosManager` minted tickets
  into private credential caches, but nothing put `KRB5CCNAME` into the `ssh`
  environment, so `--principal` / `--root-principal` were silently ignored and
  the ambient ticket was used. Found while adding the credential chain; the
  chain work required wiring it properly. Covered by
  `test_the_credential_cache_is_put_into_the_ssh_environment`.

### 6.5 Smaller items
- The gateway `ssh.proxy: auto` choice is cached for the life of a run; a
  gateway that dies mid-run surfaces as an SSH error on the next command rather
  than as an automatic failover.
- `ecl-client`'s Python surface has moved between releases; `report/ecl.py`
  tries `post()` then the class API. Confirm against the installed version.
- Phase 3's full mesh on the data network is O(N²) SSH-bundled probes; at the
  present 30-odd data-network nodes that is ~870 pairs in one session per
  source. If the cluster grows substantially, consider anchoring it too.
- `pytest` takes ~25 s, dominated by deliberate sweep timeouts. It was ~70 s
  until the simulated run stopped issuing real pings (§6.4).
- **Phase 2 waits for nodes one at a time.** `_wait_for_nodes` walks the
  stage's nodes in sequence, so a node that never comes back costs the whole
  `boot_timeout` (600 s by default) before the next one is even tried. The
  `readout` stage has 28 nodes: three dead ones are half an hour of an outage
  spent waiting in series, during which nothing else is happening.
  Parallelising it is straightforward — the probes are read-only `ssh true`,
  and `assess_nodes` right afterwards already runs the same nodes concurrently
  — but it changes the connection profile against machines that have just
  booted, in the most safety-critical phase, so it wants the author's eye
  rather than a drive-by fix. Found during the hostility review; not changed.
- A node that answers TCP but refuses every identity costs one connection per
  credential in the chain — up to eight with the seven Mu2e service identities
  discovered from Vault. That is the intended behaviour of the chain and it is
  bounded, and since a refused `ssh.login` now skips the node's remaining
  checks it happens once per node rather than thirteen times. Worth knowing
  before pointing a run at a cluster whose sshd is rate-limiting.

---

## 7. Design decisions

Recorded here in brief; the reasoning is in [docs/DESIGN.md](docs/DESIGN.md).

| Decision | Alternative rejected | Why |
|---|---|---|
| `ipmitool` runs on the gateway | Run it locally | IPMI subnets are not routable from off site |
| Password on stdin, `ipmitool -E` | `-P <password>` | `-P` exposes the password in the gateway's process table |
| Drive the `ssh` binary | Paramiko / asyncssh | The site's `ssh_config`, GSSAPI and host-key policy are then automatically in force |
| Drive `kinit`/`klist` | A native krb5 binding | No build-time dependency on a host that may itself be recovering |
| Separate credential cache per principal | One cache collection | Acquiring root must not displace the ordinary ticket |
| Service tickets via `mu2edaq-kerberos` | Fetch the keytab and kinit here | That package owns the keytab-in-Vault layout; a second copy would be a second thing to keep in step |
| Only an auth failure advances the credential chain | Always try every identity | Seven identities against a dead host costs seven connect timeouts and learns nothing |
| Root sessions do not fall back | Try service identities for root too | The service accounts are ordinary users; it could not succeed |
| FAIL distinct from UNKNOWN | One failure status | "Broken" and "could not look" need different responses |
| SKIP dropped before the node roll-up | Rank SKIP above OK | Otherwise a node with no BMC reports as "n/a" rather than healthy |
| Power sequence is YAML | Hard-coded order | Operations changes the order more often than the code |
| A refused TCP connection counts as reachable | Only `open` counts | During a power-on, "up but sshd not started" is the normal state |
| Static HTML report | A Flask/Litestar service | Must be readable from a laptop, copyable to a web area, attachable to a logbook entry |
| Re-running a phase appends to the store | Overwrite | A second assessment must not erase the evidence that a repair was needed |
| Phase 4 re-reads the store | Keep results in memory | Makes the report regenerable hours later without touching the cluster |
| C++ only for the reachability sweep | C++ throughout, or none | It is the one place where process/GIL overhead dominates; everything else is I/O-bound |
| Protected-host refusal is not overridable | A `--force` flag | Powering down a gateway from a remote recovery session is never the intent |
| No automatic remediation | Restart services, remount | An outage is not the moment to discover what an automatic fix does when its assumptions fail |

---

## 8. Phase completion

| Phase | Implementation | Tests | Docs | Live |
|---|---|---|---|---|
| 0 — self-update | ✅ | ✅ 13 | ✅ | ✅ fetch/up-to-date verified against origin |
| 1 — assess | ✅ | ✅ 6 + 36 check tests | ✅ | ⬜ |
| 2 — power on | ✅ | ✅ 8 + 25 IPMI tests | ✅ | ⬜ |
| 3 — network | ✅ | ✅ 4 | ✅ | ⬜ |
| 4 — report | ✅ | ✅ 4 + 19 report tests | ✅ | ⬜ |
| Report site | ✅ | ✅ 19 | ✅ | ⬜ |
| Diagnostics | ✅ | manual | ✅ | ⬜ |
| C/C++ library | ✅ | ✅ 7 groups | ✅ | n/a |

✅ done · ⬜ pending live-cluster verification (§6.3)
