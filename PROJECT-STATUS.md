# Project status — mu2edaq-power-recovery

**Version** 0.1.0 (tagged) · **Status** feature-complete; credentials, Vault
and BMC access verified live, no power-on yet (§6.3) · **Repository**
<https://github.com/Mu2e/mu2edaq-power-recovery> (public, per the Mu2e-org
convention) · **Last updated** 2026-09-18

---

## 1. Summary

All four phases specified in `Project-Description.md` are implemented, together
with the phase-0 self-update, the static report site, the logbook integration,
the diagnostics utilities, the optional C/C++ probe library and its Python
bindings, and the documentation set.

309 automated tests pass, plus 7 C++ test groups and an end-to-end simulated
four-phase run. The suite's "contacts nothing" claim is now *enforced* by a
test-collection guard rather than merely asserted (§6.4).

**The four phases have not yet been run end to end against the real cluster**,
and the tests and rehearsal deliberately contact nothing, so what they verify is
the logic rather than the environment. Individual pieces *have* now been
exercised live, and that is where most of §6.4 came from: Vault reads, the IPMI
credentials and cipher suite, SSH through a real gateway, and the Kerberos
credential chain on macOS. Two items remain open (§6): the MC-1 node list, and
an actual power-on.

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
| 18 | Prompt for a password when needed | ✅ | `KerberosManager._kinit`; regression-tested after a lost import broke it (§6.4) |
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
| SSH transport | ✅ Complete | via checks | ProxyJump, GSSAPI, no ControlMaster; gateway resolution serialised |
| IPMI client | ✅ Complete | 43 | Runs on the gateway; secrets on stdin; upstream-matching invocation |
| Kerberos | ✅ Complete | 68 | Credential chains; Heimdal collection caches; displacement guard |
| Service identities | ✅ Complete | 68 | Via `mu2edaq-kerberos`; no keytab handled here |
| Vault | ✅ Complete | 11 | Path and fields confirmed against the live Vault |
| Check framework | ✅ Complete | 39 | 25 checks; registry; failure containment; ping dialects |
| Output parsers | ✅ Complete | 20 | Real command output as fixtures; iputils/BSD/Windows |
| Phase 1 assess | ✅ Complete | 7 | Read-only asserted by test; simulation hermeticity |
| Phase 2 power on | ✅ Complete | 8 | Stage order, requirements, dry run |
| Phase 3 network | ✅ Complete | 4 | Full mesh + MTU probe |
| Phase 4 report | ✅ Complete | 4 | Narrative derived from the store |
| Run store | ✅ Complete | 8 | SQLite; Postgres by URL |
| Report site | ✅ Complete | 20 | 9 pages + JSON companions |
| Publication | ✅ Complete | in report suite | rsync / scp / copy; refuses to publish under `--simulate` |
| ECL posting | ⚠️ Untested | in report suite | Body and subject tested; posting needs the package (§6.5) |
| Self-update | ✅ Complete | 13 | Against real throwaway git repos |
| `libmu2eprobe` (C++) | ✅ Complete | 7 groups | C++ / C / Python surfaces |
| ssh credential chain | ✅ Complete | 68 | Per-host memo, promotion, auth-vs-unreachable-vs-hostkey |
| Host-key handling | ✅ Complete | in credentials suite | Own classification; stops the chain; verified against a live reimaged node |
| Python sweep fallback | ✅ Complete | 8 | Semantics identical to the native path |
| CLI | ✅ Complete | 14 | Driver + 4 single-phase entry points |
| Test network guard | ✅ Complete | 2 | Autouse fixture + its meta-test |
| Diagnostics (4 tools) | ✅ Complete | — | Exercised live; `mu2e-ssh-probe` now builds the real credential chain |
| Man pages | ✅ Complete | — | 18 pages, all render warning-free; the single-phase drivers cross-reference the driver page rather than listing flags (§6.5) |
| Documentation | ✅ Complete | — | README, CLAUDE, INSTALL, DESIGN, OPERATIONS, this file |

---

## 4. Test matrix

`pytest` — **309 passed** in ~29 s, no cluster, no credentials, no network.

| Suite | Tests | Covers |
|---|---|---|
| `unit/test_topology.py` | 14 | NodeRange expansion, aliases, classes, protection, MC-1 emptiness |
| `unit/test_settings.py` | 23 | All five precedence layers, coercion, redaction, malformed YAML |
| `unit/test_parsers.py` | 20 | `df`, `ip`, `ping` (iputils + BSD + Windows), `mdstat`, SMART, kernel errors |
| `unit/test_checks.py` | 39 | Every check's pass and fail path; framework containment; ping dialects |
| `unit/test_ipmi.py` | 43 | **Safety gates**, credentials, invocation shape, failure diagnosis, credential stop |
| `unit/test_state.py` | 8 | Round-trip, refusal auditing, append-not-overwrite |
| `unit/test_vault.py` | 11 | KV path resolution, folder-vs-secret, synonyms, file fallback |
| `unit/test_credentials.py` | 68 | Primary-first chains, root fallback, ssh-failure classification, KRB5CCNAME, collection caches, host keys, cleanup, password prompting |
| `unit/test_network_guard.py` | 2 | The suite's "contacts nothing" claim is enforced, not just asserted |
| `unit/test_docs.py` | 3 | `tools/generate-docs.py --check`: the man pages still match the code, and `--check` writes nothing |
| `unit/test_sweep.py` | 8 | Both backends, identical semantics |
| `unit/test_selfupdate.py` | 13 | Dirty tree, divergence, fast-forward, re-exec guard |
| `integration/test_phases.py` | 23 | All four phases end to end; simulation hermeticity |
| `integration/test_report.py` | 20 | Page rendering, archiving, publication, ECL body |
| `integration/test_cli.py` | 14 | Driver, flags, exit codes, JSON output |

**C++** — `ctest`, 7 test groups: version/OpenMP agreement, unresolvable names,
loopback, timeout bounding, input-order preservation, the reachable filter, and
the C ABI. Uses loopback and RFC 5737 TEST-NET-1 only, so it needs no network.

**End to end** — `mu2e-power-recovery --phase all --simulate` runs all four
phases over the full 65-node inventory (the shipped default is
`topology.locations: [mc2, teststand]` — 48 nodes at MC-2, 17 at the teststand;
phase 4 reports "All 65 node(s) verified healthy"), generates all 9 pages and 7
JSON files, and is wired into `ctest` as `simulated-run`.

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

**Verified on this machine:** macOS 14 (arm64), Python 3.12.1 — 309 pytest
tests, 7 C++ test groups, the simulated four-phase run, all 18 man pages, and
`bootstrap.sh` from scratch.

macOS deserves its own line rather than a tick. It is the development platform
*and* a realistic platform to drive a recovery from, and it is the one that
ships Heimdal rather than MIT Kerberos. Three separate defects came out of that
difference (§6.4), all of them invisible on Linux: `KRB5CCNAME=FILE:` ignored by
`kinit`, a minted ticket displacing the collection default, and `ping -W` read
as milliseconds instead of seconds. Anything touching credential caches or
building a shell command should be exercised on both.

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

### 6.2a BMC username case — ✅ **resolved 2026-09-18**
`mu2e-ipmi-tool --diagnose` against a live BMC returned `username 'MU2E',
cipher suite 3` on the first attempt, reading `Chassis Power is on`. The Vault
secret now returns `MU2E`, so `ipmi.username` stays null (take it from Vault)
and no config change is needed. The `--diagnose` mode and the `ipmi.username`
override remain for the next time the two disagree.

### 6.3 Live-cluster verification — **partly done**

What has now been exercised against real infrastructure:

| | |
|---|---|
| ✅ Vault reads against `ssivault.fnal.gov` | Path and both field names confirmed (§6.2) |
| ✅ BMC credentials and cipher suite | `MU2E`, suite 3, read-only `chassis power status` (§6.2a) |
| ✅ SSH through a real gateway | Including a node whose host key had changed (§6.4) |
| ✅ Kerberos chain on macOS/Heimdal | Collection caches, displacement, restore (§6.4) |
| ✅ Phase 0 self-update | Fetch and up-to-date paths against `origin` |
| ⬜ **An actual power-on** | Nothing has been switched on by this tool |
| ⬜ Phases 1–4 run end to end on the cluster | |
| ⬜ `ipmitool` state-changing verbs on a real BMC | |
| ⬜ ECL posting | Needs the `ecl-client` package and a category |

Recommended sequence for the rest, in daylight, on a maintenance day:
`mu2e-vault-ipmi` → `mu2e-ssh-probe mu2egateway01 --run true` →
`mu2e-ipmi-tool -n <one node> chassis power status` → `mu2e-power-state` → a
dry-run `mu2e-power-on` → a real `mu2e-power-on --execute --until manager`.

`--run true` is not optional: without it `mu2e-ssh-probe` opens no connection
and exits 0 regardless. And `mu2e-ipmi-tool` uses the ambient ticket with no
credential chain, so it verifies the BMC credentials, not the run's SSH
access.

Note the first four cost nothing and change nothing; the value of running them
is that they fail on *access*, which is the failure mode that cannot be
rehearsed off site.

### 6.4 Real bugs found and fixed

Every one of these was a defect in shipped behaviour, not a refactor. They are
recorded because several were misdiagnosed first, and the wrong diagnosis is as
worth keeping as the right one. Newest first.

- **`kinit` could not prompt for a password at all.** The commit that stopped
  deriving the ssh login from the principal removed `import getpass` with the
  `getpass.getuser()` call it had added — but `KerberosManager._kinit` still
  calls `getpass.getpass`. So `--principal` or `--root-principal` without a
  live ticket raised `NameError: name 'getpass' is not defined` instead of
  prompting, which is requirement 18. Import restored; the path now has a test
  that exercises it, which is how it was found.
- **Three more places the credential handling had not caught up with the
  collection model.** All consequences of a service ticket now having a ccache
  *name* (`API:<uuid>`) rather than a path. (i) `cleanup()` built
  `KRB5CCNAME=FILE:API:<uuid>`, which names nothing, and ran a bare `kdestroy`
  — so on macOS *every service ticket survived the run*, sitting in the
  operator's collection where it can become the collection default and make
  the next run log in as a service identity, which is exactly the state the
  new startup guard exists to warn about. Each cache is now named to kdestroy
  with `-c`, and a file cache outside the run's own directory is refused
  outright rather than handed to it. (ii) `KerberosManager._kinit` was still
  steered by `KRB5CCNAME=FILE:` alone — the spelling Heimdal ignores — for the
  operator's own, root-capable principal, with no displacement check at all.
  It now passes `-c` as well and restores the default pointer if minting moved
  it; unlike the service path it warns rather than aborting, because a
  designated principal *is* the run. (iii) `ensure()` had no collection
  fallback, so when Heimdal wrote no file it reported "kinit appeared to
  succeed but no valid ticket is present" and `--principal` simply did not
  work on the platform the recovery is driven from. It now looks the principal
  up in the collection, as `TicketSource.ticket()` already does for the
  service identities. `_klist` and `environ_for` take a ccache name too.
- **A mint that timed out skipped the displacement check.** The command had
  run, so it could have repointed the default cache and then hung — and the
  caller saw only a timeout, logged a warning, and moved on to the next six
  identities under the wrong identity. That is the shape of the original
  incident. The check and the restore now run on the timeout path as well, and
  a failed restore is reported in preference to the timeout.
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
  BMC — all 45 of them share one credential set, so the first rejection
  settles the matter for the rest. §6.2a has since closed in the good
  direction (`MU2E` works), so this is no longer the expected outcome of the
  first live run, but it remains what a rotated password or a changed Vault
  field would produce. A credential
  rejection (RAKP, "unauthorized name") is no longer retried and now stops the
  run's IPMI with one clear diagnosis; `ipmi.stop_on_auth_failure: false`
  overrides it. A BMC that simply does not answer is deliberately not treated
  this way — "Unable to establish IPMI v2 / RMCP+ session" is what a dark
  chassis says too, and after an outage that is the expected case.
- **A changed host key was reported as a login failure.** Found against
  `mu2e-calo-01`, whose ED25519 key no longer matched the stored ECDSA entry.
  `StrictHostKeyChecking=accept-new` correctly refuses a *changed* key, so ssh
  aborts before authenticating -- but the tool then walked all eight
  credentials against it, reported "SSH login failed", and hammered the
  gateway doing so. Host-key failures are now their own classification, stop
  the chain immediately, and are reported as what they are, with the advice to
  verify the new fingerprint out of band before touching `known_hosts`. This
  matters specifically for an outage tool: reimaged nodes are exactly what a
  recovery meets.
- **`mu2e-ssh-probe` did not use the credential chain.** It built an
  `SSHFactory` with no Kerberos manager, so it tested only the ambient ticket
  and ssh_config's login -- meaning the diagnostic could succeed against a host
  the real run could not reach, which is the confusion it exists to prevent. It
  now builds the same chain, reports which credential succeeded, and lists each
  refused login/ticket pair with its reason. `--no-chain` restores the old
  ambient-only behaviour.
- **macOS keeps credential caches in a collection, and minting displaces the
  default.** The earlier diagnosis ("the operator's ticket was destroyed") was
  wrong: Heimdal never destroyed it. It keeps caches in an API: *collection*
  and makes each newly minted cache the collection default, so the personal
  ticket is displaced, not deleted — `klist -l` still listed it throughout, and
  `kswitch -p <principal>` restores it instantly. The tool now records the
  default before each mint and puts the pointer back after, aborting the whole
  chain only if a restore fails.
- **Heimdal ignores `KRB5CCNAME=FILE:` for kinit.** So the private cache file
  we asked for never appeared and every service identity was rejected as
  unusable. The ticket is perfectly good, it just has a ccache *name*
  (`API:<uuid>`) rather than a path; the tool now looks it up in the collection
  by identity and uses that name. All seven identities resolve on macOS.
- **The login must NOT be derived from the principal.** Deriving it
  (`anorman@FNAL.GOV` → `anorman`) was a wrong fix for a misdiagnosed symptom:
  verified against the live cluster, `mu2edaq` and `root` both log in fine with
  the personal ticket, while `anorman` is refused because no such account
  exists on those hosts. ssh_config was right; the original failure was the
  displaced ticket alone. Reverted, with the reasoning recorded in the code.
- **A failed helper reported the wrong cause.** `get-kerberos-ticket` appends a
  "Known identities:" listing after an error, so taking the last line of its
  output reported `nova:` as the failure. `summarise_tool_error` now skips the
  hint block and surfaces the real line — which immediately exposed that the
  helper takes `python3` from the ambient PATH (the system Python, with no
  hvac, unless a venv happens to be active). That interpreter is now pinned to
  ours, which has hvac by construction.
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
- **(superseded) "The primary login was left to ssh_config."** `~/.ssh/config_mu2e`
  sets `User mu2edaq` for the DAQ hosts, and that was read as the reason the
  personal ticket was being refused everywhere, so the login was derived from
  the principal instead. Verified against the live cluster the diagnosis was
  wrong and the change was reverted — see *The login must NOT be derived from
  the principal* above. Kept here because it is a plausible-looking inference
  from a real symptom and will be proposed again by whoever meets the symptom
  next.
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

### 6.5 Found and deliberately left

Each of these is understood, reproducible, and not fixed — with the reason.

The nine items immediately below were found in a documentation audit on
2026-09-18. In every case the *documentation* has been corrected to describe
what the code does; the code behaviour is left for the author. They are ordered
by how much an operator would care.

- **`--execute` is not a second gate, although four documents said it was.**
  `cli.py:184-185` does `if args.execute: overrides["run.dry_run"] = False`, and
  `orchestrator.py:308` passes `run.dry_run` straight to `IPMIClient`. Nothing
  re-checks the flag, so `run: {dry_run: false}` in the config file, `.env` or
  `MU2E_POWER_RECOVERY_RUN_DRY_RUN=false` arms live power commands with no flag
  on the command line — verified by resolving the shipped parser against such a
  config. Only `--simulate` is genuinely independent. The docs now say so. If
  the two-gate behaviour was the intent, it is a two-line change in
  `run_phases`/`_make_ipmi_client` (require `args.execute` *and* `not
  run.dry_run`), but it would break any operator who arms a run from the config
  file today, so it is the author's call. **Highest-value item in this group.**
- **Nothing installs a SIGTERM handler, so `stop-…-recovery.sh` is not a clean
  stop.** No module under `src/` imports `signal`; `cli.py:483` catches only
  `KeyboardInterrupt` (SIGINT). Under the default disposition SIGTERM kills the
  process, so `record_event("run interrupted by the operator")`,
  `finish_run("interrupted")` and the `finally: orch.close()` never run. The run
  is left as `running` in the store and `KerberosManager.cleanup()` never
  executes — the run's private, root-capable caches survive, which is exactly
  the residue the macOS section warns about. A `signal.signal(SIGTERM, …)` that
  raises `KeyboardInterrupt` would reuse the working path, but installing
  handlers in a threaded, safety-critical driver deserves the author's eye. The
  stop script, its PowerShell twin and the man page now describe the real
  behaviour and tell the operator to clean up by hand.
- **The Kerberos startup guard warns and continues.** `orchestrator.py:230-233`
  logs `ambient_warning()` and appends it to the run notes, then proceeds into
  `prepare()` and every phase; `kerberos.py:547-572` only builds a string. Four
  documents said the run *stops*. Halting is not a one-line change because
  `ambient_warning()` returns a string for two conditions and only one is fatal
  — a service identity holding the default cache (every login will be refused)
  versus the benign case where the ambient principal merely differs from
  `kerberos.principal`. Separating them, then exiting on the first, is the fix.
- **An unmatched `--from`/`--until` stage name is silently ignored.**
  `phase2_poweron.py:289-295`: `start = names.index(from_stage) if from_stage in
  names else 0`. So `mu2e-power-on --execute --from manger` runs the *whole*
  sequence, readout included, with no error and no note. For a flag whose
  purpose is to bound what gets powered on, this should arguably be an error.
  Documented as a caveat in `man 1 mu2e-power-on` and the runbook for now.
- **`mu2e-ipmi-tool` reaches the gateway with the ambient ticket only.**
  `ipmi_tool.py:126` builds `SSHFactory(settings, topology, local=local)` with no
  `KerberosManager`, so `credentials_for` returns `[]` and `SSHTransport.run`
  falls to `chain = [None]`; it never calls `ambient_warning()` either. This is
  the same defect §6.4 records as fixed for `mu2e-ssh-probe`, still present here:
  the helper can fail against a gateway a real run would open with a service
  identity, or succeed under a displaced default cache. Its man page and the
  runbook now say the tool does not test the run's SSH access.
- **`mu2e-ssh-probe` builds the chain's credential *list* but not its
  tickets.** It never calls `KerberosManager.prepare()`, so `operator_credential()`
  returns a `Credential` whose `cache` is `None` — the ambient cache — while its
  `principal` comes from `kerberos.principal`. Since `docs/INSTALL.md` recommends
  setting that principal in `config/.env`, this divergence is the normal case,
  and `describe()` prints `ticket you@FNAL.GOV [ambient cache]` even when the
  ambient cache holds someone else. There is no `--principal` flag to compensate.
  Documented in `man 1 mu2e-ssh-probe`.
- **`mu2e-ipmi-tool` silently un-filters when every named host lacks a BMC.**
  `ipmi_tool.py:97`: `nodes = [n for n in nodes if n.ipmi_host] or nodes`. So
  `-n mu2e-dcs-03` (on the lab network, no `ipmi:` interface) proceeds and builds
  `ipmitool -H None …` on the gateway. The `--diagnose` path handles this
  correctly (`ipmi_tool.py:253-255`). Documented in the man page.
- **The test suite's network guard covers one method.**
  `tests/conftest.py:125-142` monkeypatches `LocalTransport.run` only. Two
  credential paths bypass it entirely: `creds/ticketsource.py` calls
  `subprocess.run` directly (klist, `klist -l`, kswitch, `get-kerberos-ticket`
  / `vault-client`), and `creds/vault.py` calls `subprocess.call(["vault",
  "login", …])` and issues HTTPS through `hvac`. A test exercising
  `KerberosManager`'s service path or `VaultCredentials` would contact the real
  Kerberos collection or `ssivault.fnal.gov` without tripping the guard. The
  existing tests stub those paths, so nothing leaks today; README and CLAUDE.md
  now state the real scope rather than claiming the whole list is enforced.
- **The displacement guard around a mint is best-effort, not a precondition.**
  `ticketsource.py:256-259` reads `before = self.default_principal()` and, when
  that returns `None`, logs a debug line and mints anyway;
  `_restore_default_if_displaced` then returns immediately at `:327-328` because
  `before` is falsy. So a mint with no readable default principal runs
  *unguarded* — the opposite of what `man 1 mu2e-power-recovery` claimed ("a mint
  whose guard cannot be taken is refused"), and the case the guard exists for.
  The page now describes the real behaviour.

- **Abandoning the service identities for a whole run is decided by a
  substring.** `KerberosManager.service_credential` (`kerberos.py:425`) tests
  `"default credential cache" in str(exc)` to tell an unrecoverable
  displacement from an ordinary "no keytab for this identity". Reword the
  message in `ticketsource.py` and the first silently downgrades to the second,
  and the run carries on minting under a displaced default — the exact failure
  the check exists to stop. Carrying the decision on the exception *type* would
  fix it. Left alone because the wording is fresh and the author may want a view
  on the shape. **This is the highest-value item in this section.**
- **Phase 2 waits for nodes one at a time.** `_wait_for_nodes` walks the stage's
  nodes in sequence, so a node that never comes back costs the whole
  `boot_timeout` (600 s by default) before the next one is even tried. The
  `readout` stage has 28 nodes: three dead ones are half an hour of an outage
  spent waiting in series, during which nothing else happens. Parallelising it
  is straightforward — the probes are read-only `ssh true`, and `assess_nodes`
  immediately afterwards already runs the same nodes concurrently — but it
  changes the connection profile against machines that have just booted, in the
  most safety-critical phase, so it wants the author's eye rather than a
  drive-by fix.
- **The single-phase drivers cross-reference rather than list their flags.**
  `man 1 mu2e-power-recovery` is complete; the four single-phase pages point at
  it instead of repeating the options they accept. `build_parser()` exposes 31
  actions (including `-h/--help` and `--phase`); with a phase fixed it is 30, so
  each single-phase driver accepts 29 options besides `--help`. Whether a
  cross-reference is acceptable in place of a full list is a style decision for
  the author. *(The rest of this item is now closed: all four diagnostic
  helpers' pages document `--config`, `--env-file`, `-v`/`--verbose` and
  `-q`/`--quiet`; every man1 page documents `--help`; and all five drivers
  document `--version`. Note the four diagnostic helpers have no `--version` in
  their parsers, so their pages correctly omit it.)*
- **`mu2e-node-inventory -n/--network`'s argparse help understates it.** It says
  only "print this network's interface names rather than the lab names"; the
  code (`node_inventory.py:71-81`) *also* filters the listing to nodes present
  on that network. The man page describes both behaviours correctly, so the fix
  belongs in the help string.
- ~~`config/power-recovery.yaml`'s vault comment names a key that does not
  exist.~~ **Fixed.** It said the file fallback used `ipmi.default_user`; the
  key is `vault.fallback_user`. Three further wrong comments in the same file
  were corrected with it: `ssh.user`'s `null => $USER` (null means "let ssh
  decide", i.e. `ssh_config` first), and `execute_on` and
  `capture_command_output`, both presented as working switches although neither
  is read anywhere in `src/`.
- The gateway `ssh.proxy: auto` choice is cached for the life of a run; a
  gateway that dies mid-run surfaces as an SSH error on the next command rather
  than as an automatic failover. Resolution is now serialised behind a lock
  (§6.4), which bounds the cost but does not add failover.
- `ecl-client`'s Python surface has moved between releases; `report/ecl.py`
  tries `post()` then the class API. Confirm against the installed version.
- Phase 3's full mesh on the data network is O(N²) SSH-bundled probes; at the
  present 49 data-network nodes that is 2352 ordered pairs, bundled into one
  session per source (49 sessions) — measured, not estimated: a simulated run
  prints `data: 2352/2352 paths ok`. If the cluster grows substantially,
  consider anchoring it too.
- `pytest` takes ~30 s, dominated by deliberate sweep timeouts. It was ~70 s
  until the simulated run stopped issuing real pings (§6.4).
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
| Separate credential cache per principal | One shared cache | Acquiring root must not displace the ordinary ticket. On Heimdal a "cache" may be a collection *name* rather than a path, and minting displaces the default anyway — so the default is recorded and restored around every mint |
| The login comes from `ssh_config`, never from the principal | Derive it (`anorman@FNAL.GOV` → `anorman`) | Verified live: `mu2edaq` and `root` accept the personal ticket and no such account exists on the nodes. Driving the `ssh` binary means the site's config is authoritative |
| Service tickets via `mu2edaq-kerberos` | Fetch the keytab and kinit here | That package owns the keytab-in-Vault layout; a second copy would be a second thing to keep in step |
| Only an auth failure advances the credential chain | Always try every identity | Seven identities against a dead host costs seven connect timeouts and learns nothing |
| Root sessions fall back by changing the ticket, keeping the `root` login | Refuse root any fallback | A node's `root/.k5login` can authorise `mu2edaq` to become root. The refusal reasoned about the *account* when what matters is the *principal*. `kerberos.root_fallback` turns it off |
| A changed host key is its own classification | Treat it as a login failure | ssh aborts before authenticating, so no credential can help; and after an outage the cause is usually a reimage, which needs verifying out of band rather than retrying |
| An IPMI credential rejection stops the run's IPMI | Retry, and carry on to the next BMC | All 45 BMCs share one credential set, so the first rejection settles it; retrying only advances lockout counters. A *dark* chassis is excluded, because it reports the same message |
| `--simulate` scripts the local transport too | Script only the SSH and IPMI transports | `ping.lab` on a gateway falls back to the local transport, so a rehearsal pinged the live gateways for real |
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
| 1 — assess | ✅ | ✅ 7 + 39 check tests | ✅ | ◐ SSH, Kerberos and BMC reads verified; no full phase run |
| 2 — power on | ✅ | ✅ 8 + 43 IPMI tests | ✅ | ⬜ nothing has been switched on |
| 3 — network | ✅ | ✅ 4 | ✅ | ⬜ |
| 4 — report | ✅ | ✅ 4 + 20 report tests | ✅ | ⬜ |
| Report site | ✅ | ✅ 20 | ✅ | ⬜ |
| Diagnostics | ✅ | manual | ✅ | ✅ all four run against the live cluster |
| C/C++ library | ✅ | ✅ 7 groups | ✅ | n/a |
| Credentials (Kerberos, Vault) | ✅ | ✅ 68 + 11 | ✅ | ✅ chain, collection caches and Vault reads verified |

✅ done · ◐ partly · ⬜ pending live-cluster verification (§6.3)
