# CLAUDE.md — mu2edaq-power-recovery

## 1. Project overview

Tools to recover the Mu2e DAQ computing centres (MC-1, MC-2, and the HEERC
teststand) from a planned or unplanned power outage. They are driven from a
workstation **outside** the DAQ networks and work in four phases: a read-only
assessment, a dependency-ordered power-on, an inter-node network check, and a
consolidated report for the electronic logbook. Nodes are reached over SSH
through a gateway with Kerberos/GSSAPI; BMCs are reached by running `ipmitool`
*on* a gateway, because the IPMI subnets are not routable from off site.

## 2. Setup and running

```sh
./bootstrap.sh                  # venv + deps; idempotent
. venv/bin/activate

mu2e-power-recovery --phase all --simulate   # rehearse; contacts nothing
mu2e-power-state                             # phase 1, read-only
mu2e-power-on --execute                      # phase 2, actually switches on
mu2e-power-on --execute --node mu2e-trk-01   # one node; predecessors verify-only
mu2e-power-netcheck                          # phase 3
mu2e-power-report --post-ecl                 # phase 4

pytest                                       # no cluster needed
mu2e-node-inventory --validate               # inventory gaps (exit 1 on errors)
python -m mu2edaq_power_recovery.runlock status   # who holds the run lock
cmake -S . -B build && cmake --build build   # optional C/C++ library
ctest --test-dir build --output-on-failure
```

Diagnostics: `mu2e-node-inventory`, `mu2e-ipmi-tool`, `mu2e-ssh-probe`,
`mu2e-vault-ipmi` (Python console scripts) and `mu2e-probe` (the C++ binary,
built by CMake to `build/mu2e-probe`, not a console script).

Live power commands need this invocation's authorisation: `--execute`, or
`MU2E_POWER_RECOVERY_ARM=<run.label>` in the process environment with
`run.dry_run: false` and that `run.label` configured. `run.dry_run: false`
alone exits 2 (`cli.authorize_live`), for every entry point.

**Run lock.** Phases 1-3 without `--simulate` take an exclusive OS lock on
`run.lock_file` (`logs/power-recovery.lock`, `runlock.py`) in `cli._main`
after authorisation and before phase 0; busy exits 2 naming the holder.
`--simulate`, `--list-*` and report-only never lock (`cli.needs_run_lock`).
The JSON record in the file is believed only while the lock is held; the file
is never deleted. The lock is released just before the phase-0 re-exec and
re-taken by the child. The start scripts keep no PID file; the stop scripts
signal only `runlock pid` (held lock) after re-checking the command line.
Tests get a private lock via the autouse `private_run_lock` fixture
(`MU2E_POWER_RECOVERY_RUN_LOCK_FILE` into `tmp_path`) — keep it, or the suite
creates `logs/power-recovery.lock` in the checkout.

`--phase` belongs to `mu2e-power-recovery` alone; the four single-phase drivers
reject it. All five drivers take `--version` and `--help`; the four Python
helpers take `--config`, `--env-file`, `--json`, `-v`/`--verbose` and
`-q`/`--quiet`.

## 3. Architecture

```
src/mu2edaq_power_recovery/
  settings.py      layered config (defaults < yaml < .env < env < CLI)
  topology.py      node inventory; NodeRange expansion, classes, protection,
                   validate() findings (--validate only)
  version.py       provenance banner (version, revision, config digest)
  selfupdate.py    phase 0: fast-forward, rebuild, re-exec; rollback on a
                   failed rebuild
  runlock.py       the single-run OS lock; `python -m ... status|pid`
  sweep.py         reachability sweep; native extension or Python fallback
  orchestrator.py  shared run state, concurrency, check execution
  state.py         SQLAlchemy run store (SQLite; Postgres by URL)
  console.py       on-screen report
  cli.py           the driver and the four single-phase entry points
  creds/           kerberos.py (kinit/klist, credential chains),
                   ticketsource.py (adapter over mu2edaq-kerberos),
                   bootstrap.py (credential_session: the one way the run
                   and the ssh/ipmi diagnostics open credentials),
                   vault.py (hvac: IPMI and ECL secrets)
  transport/       base.py, local.py, ssh.py, ipmi.py, fake.py
  checks/          base.py (registry), parsers.py, and one module per area
  phases/          phase1_assess .. phase4_report
  report/          html.py + templates/, publish.py, ecl.py
  tools/           the four diagnostics helpers
src/cpp/, src/include/, python/    libmu2eprobe and its pybind11 bindings
tools/generate-docs.py            regenerates the generated man page sections
                                  and checks the docs against the code;
                                  man 1 mu2edaq-generate-docs
```

**Threading.** Checks run in a `ThreadPoolExecutor` bounded by
`ssh.max_sessions`; each is one `ssh` subprocess, so the work is I/O-bound and
the GIL is not the limit. The C++ sweep releases the GIL.

**Data flow.** Topology + checks config → orchestrator builds a `CheckContext`
per node → checks return `CheckResult` → `NodeAssessment` rolls them up →
`PhaseResult` → run store → report/console/ECL. Phases never render; they
return `PhaseResult`, so console, HTML and logbook output cannot drift apart.

**Report lifecycle.** `run_phases()` runs phases 1-3 only and persists each
result's verdict/notes/duration into its phase row (`data._result`). `main()`
then finishes the run (`finish_run`, on error and interrupt paths too) before
anything is assembled: `phase4_report.assemble(store, rid)` records the report
phase against `rid` and builds the narrative from the final export;
`ReportWriter.render_run()` renders `runs/<rid>/` from that export alone (the
top level is re-rendered only when `rid` is the newest run); `phase4_report.post()`
posts with the bundle's pages and records the outcome on `rid`; publication
runs once, last. Report-only runs `store.attach(rid)` and never call
`prepare_credentials`. Current state is `phase4_report.reconcile()`: newest
result per `(hostname, check_id)`; headline, counts, exit code and ECL text all
come from it. With `--json`, stdout is one JSON document and every human line
goes to stderr — print to the `out` stream `main` passes down, never bare
`print()`.

## 4. Configuration schema

Four YAML files in `config/`, each with a man page in section 5:

```yaml
# power-recovery.yaml -- everything except the inventory. 78 keys; man 5
# mu2edaq-power-recovery.yaml documents each one.
run:      {label, dry_run: true, stop_on_stage_failure, phase_timeout,
           from_stage, until_stage, lock_file}
topology: {file, sequence_file, checks_file, locations: [mc2, teststand]}
selfupdate: {enabled, remote, branch, rebuild_globs, allow_dirty, timeout}
ssh:      {user, root_user, proxy: auto, connect_timeout, command_timeout,
           options, max_sessions}
ipmi:     {execute_on: gateway, username, tool, interface, privilege,
           cipher_suite, timeout, retries, power_on_delay, message_timeout,
           tool_retries, extra_args, stop_on_auth_failure,
           reachability_precheck}
kerberos: {principal, root_principal, min_lifetime, prompt, verify_users,
           use_service_keytabs, root_fallback, service_identities,
           discover_identities, get_kerberos_ticket_command,
           vault_client_command, vault_client_args}
vault:    {addr, kv_mount: td, base_path, ipmi_path: ipmi/config,
           ipmi_user_field, ipmi_password_field, allow_file_fallback,
           fallback_password_file, fallback_user, auto_login, timeout}
database: {url, path}          # url set => Postgres
report:   {output_dir, title, keep_runs,
           publish: {enabled, method, target, options}}
ecl:      {enabled, url, category, credential_path, attach_html}
logging:  {level, file, capture_command_output, max_capture_bytes}
```

`ipmi.username` is null by default: take it from Vault. `ipmi.message_timeout`
and `ipmi.tool_retries` are null too — sending `-N`/`-R` cut ipmitool to a
single attempt and broke sessions on BMCs that needed a retry, so the
invocation now matches upstream `mu2e_ipmi.sh` exactly apart from `-E` in place
of `-P`.

`topology.yaml` (inventory, subnets, classes, `protected:`),
`power-sequence.yaml` (phase-2 stages), `checks.yaml` (thresholds, expected
mounts and services, check profiles, the phase-3 mesh with per-network
`origin: nodes|gateways` and `targets: all|anchors`).

Precedence is fixed: **command line > environment > `config/.env` > YAML >
built-in defaults**. Any key is settable as `MU2E_POWER_RECOVERY_<DOTTED_PATH>`
upper-cased with underscores.

Four variables are read directly rather than through that mechanism:
`NO_COLOR` (never colour), `FORCE_COLOR` (colour even off a tty; `NO_COLOR`
wins) — both in `console.py` — `MU2E_POWER_RECOVERY_UPDATED`
(`selfupdate.REEXEC_GUARD`), set across phase 0's `os.execve` so the
re-executed process does not check for updates again, and
`MU2E_POWER_RECOVERY_ARM` (`settings.ARM_ENV`), the live-run token, which
`load()` never ingests and refuses in `config/.env`.

## 5. Testing

```sh
pytest                                       # unit + integration
pytest tests/unit/test_ipmi.py               # the safety assertions
mu2e-power-recovery --phase all --simulate   # end-to-end rehearsal
ctest --test-dir build --output-on-failure   # C++ (CppUnit or built-in runner)
```

Nothing in the suite touches the DAQ network, needs a Kerberos ticket, or reads
Vault. Every check is a pure function of a transport; faults are injected with
`FakeTransport.expect_first(pattern, response)`, which puts one rule ahead of
the healthy baseline.

That is **enforced** by the autouse `no_real_network` fixture in
`tests/conftest.py`, in three layers:

- `subprocess.Popen.__init__` — so `LocalTransport.run` *and* the direct
  `subprocess.run`/`call` in `creds/ticketsource.py` and `creds/vault.py` —
  refuses `BLOCKED_COMMANDS` (`ssh`, `scp`, `rsync`, `ping`, `ping6`,
  `ipmitool`, `kinit`, `klist`, `kdestroy`, `kswitch`, `vault`,
  `get-kerberos-ticket`, `vault-client`, `mu2e-probe`, `curl`, `wget`, `nc`),
  looking inside `sh -c` payloads, `shell=True` strings and `env VAR=…`
  prefixes;
- `socket.connect`/`connect_ex` to anything but loopback, AF_UNIX or TEST-NET-1
  (192.0.2.0/24) — this is what stops `hvac` and the Python sweep;
- `sweep.sweep` for any host but loopback, TEST-NET-1 or `*.invalid` — the
  native backend opens its sockets in C++.

A block raises `pytest.fail.Exception`, a **BaseException**, because the paths
it guards are wrapped in `except Exception` in production
(`SSHFactory._select_gateway`, every `VaultCredentials` call): an
`AssertionError` there was swallowed and the test passed having reached out.
Each block is also recorded and fails the test at teardown. Meta-tests consume
an expected block with `no_real_network.expect(fragment)`. Opt out with
`@pytest.mark.allow_network` (declared in `pyproject.toml`);
`tests/unit/test_network_guard.py` has a meta-test per path. Do not weaken it —
it was added after the suite was caught making real ssh connections, extended
after a `--simulate` run was caught pinging the live gateways, and extended
again (#22) when it found a credentials test running the developer's real
`klist`.

`FakeTransport`'s rule scan is under a lock: clones deliberately share the rule
list and `assess_nodes` runs a node per thread, so an unguarded `once=True` rule
could fire for two nodes or for neither.

## 6. Conventions for changes

- **A new check** is a function decorated with `@register("area.name", "what it
  verifies")` in `checks/`, plus its id in a profile in `config/checks.yaml`,
  plus a test that makes it fail. An id in the config with no implementation is
  reported as `skip`, never silently dropped.
- **A new node or site** is a `config/topology.yaml` edit. No code change.
- **A change to the power-on order** is a `config/power-sequence.yaml` edit. No
  code change.
- **Never let a secret reach an argument vector.** Passwords go to stdin;
  `ipmitool` uses `-E`, never `-P`. There is a test for this.
- **Never handle a keytab here.** `mu2edaq-kerberos` owns the keytab-in-Vault
  layout and mints the tickets; `creds/ticketsource.py` is a thin adapter over
  its commands. A second copy of that logic would be a second thing to keep in
  step with how the secrets are stored.
- **Only an auth failure advances the credential chain.** `classify_ssh_failure`
  makes that call; getting it wrong means either cycling seven identities
  against a dead host, or giving up on a node one of them could have opened.
  The order of its tests is load-bearing: **hostkey first**, then unreachable,
  then auth. A jump host that is down prints a credential message further down
  its output, and a changed host key aborts ssh *before* authentication, so
  reading either as an auth failure walks the whole chain for nothing.
- **A changed host key is its own classification, not a login failure.** It
  stops the chain at the first attempt and `ssh.login` reports it as what it is,
  names reimage-after-outage as the likely cause, and points at `ssh-keyscan`
  from a gateway. Reimaged nodes are exactly what an outage tool meets; the
  other reading is interception, so the tool must never quietly retry past it.
- **The operator's own principal is always chain position 0**, for root as well
  as ordinary sessions, and nothing may displace it — not the per-host memo, not
  promotion. Service identities are fallbacks; the run returns to the personal
  ticket after each node (`KerberosManager.restore_primary`).
- **Never derive the ssh login from the principal.** This was tried
  (`anorman@FNAL.GOV` -> `anorman`) and verified wrong against the live cluster:
  `mu2edaq` and `root` both accept the personal ticket, and no `anorman` account
  exists on those hosts. `ssh_config` sets the login legitimately; the failure
  that prompted the change was the displaced ticket alone. The revert is
  deliberate — do not reintroduce it.
- **Credential caches may be names, not paths.** macOS/Heimdal keeps caches in
  an `API:` collection and makes each freshly minted one the collection default,
  so a service ticket has a ccache *name* (`API:<uuid>`) and minting one
  *displaces* — never destroys — the operator's. Anything handling a cache must
  accept a name: `FILE:API:<uuid>` names nothing, and a bare `kdestroy` steered
  only by `KRB5CCNAME` destroys the default. Record the default principal before
  each mint and restore it after -- on *any* exit from the mint, including
  KeyboardInterrupt (`TicketSource.ticket()`'s `except BaseException`). A
  service cache is put in `_caches` before the mint so an interrupted one is
  still destroyed; and because on macOS the ticket is in `API:<uuid>`, not at
  that path, an interrupted `_kinit` and an interrupted or timed-out
  (`TicketTimeout`) service mint also look the principal up in the
  collection and record that name (`_record_collection_cache`, key
  `<role>@collection`). That bookkeeping never masks the exception in flight,
  and never records the cache of the principal that was the default before
  the mint. `cleanup()` names each cache to `kdestroy -c`, tolerates
  one that was never created, and refuses any file cache outside the run's
  own temporary directory.
- **A readable default is a precondition for a service mint.**
  `TicketSource.ticket()` raises `DefaultCacheUnverifiable` before
  `get-kerberos-ticket` runs when `default_principal_status()` cannot name the
  default principal, and `DefaultCacheDisplaced(before, after)` when a restore
  fails. Both are `DefaultCacheGuardError`; `service_credential` catches that
  *type* and sets `fallbacks_disabled` for the run. The one exception: with
  both primary roles on private caches (`_primaries_private()`) and klist
  reporting `NoDefaultCache` (a type, not a message), the mint proceeds and a
  default it leaves naming the identity is destroyed by name
  (`destroy_default`), else `DefaultCacheDisplaced(None, after)`. `_kinit`
  (`_guard_kinit`) proceeds on `NoDefaultCache` -- it mints the operator's own
  principal, whose becoming the default is no displacement -- and refuses, before
  the prompt, only a default that exists but cannot be read. Never key control flow on a
  message again — the substring test this replaced is how a reworded error could
  have let the chain keep minting under a displaced default.
- **Every mint holds `KerberosManager._mint_lock`** (an RLock): read default,
  mint, collection lookup, restore, as one transaction, with a double-checked
  fast path and results published to `_service` last. `_kinit` takes it too.
  `_successful` has its own small lock so an ssh success callback never waits
  behind a mint. `vault-client identities` runs once per manager
  (`_discovered`); the `use_service_keytabs` switch is still read live. `warm_fallbacks()` mints every fallback before workers start
  — only with `use_service_keytabs` on, never under `--simulate` or in a
  show-only diagnostic.
- **Open credentials through `creds/bootstrap.py:credential_session`**, not by
  building a `KerberosManager` and `SSHFactory` by hand. It is what makes the
  diagnostics reproduce the run, and its `finally` is what destroys the private
  caches. `prepare=False` is describe-only: it sets `minting = False` and
  acquires nothing.
- **Never weaken the protected-host refusal.** It is the one place the tool
  declines to do what it is told, and it is deliberate.
- **Keep FAIL and UNKNOWN distinct.** "It is broken" and "we could not look"
  need different responses. A refused IPMI credential is UNKNOWN
  (`PowerState.REFUSED`, ensure_on action `credentials_refused`), never FAIL
  "BMC does not answer", and never the protected-host refusal.
  In phase 3 that is `MeshEdge.tested`: a source
  that could not be reached, a probe that raised, or output missing a
  target's markers is UNKNOWN and stays out of the isolation analysis.
  With `origin: gateways` coverage is per target (`uncovered_targets()`): a
  dark gateway whose partner tested every BMC does not make the network
  UNKNOWN, and a location with BMCs but no gateway is UNKNOWN, never OK.
- **One `CredentialBreaker` per BMC account, shared by every `IPMIClient`.**
  Pass the orchestrator's `ipmi_breaker` to any client you build; a client with
  its own breaker would present a refused credential again. While unproven it
  admits one invocation at a time (`AUTH_PROBE_CONCURRENCY`, a constant, not a
  config key); do not raise it. The reachability ping
  (`ipmi.reachability_precheck`) runs *before* the gate and must stay
  gate-free — it is what keeps dark BMCs from being serialised. "Unable to
  establish" trips the breaker only from BMCs that answered that ping, and
  only at `ESTABLISH_FAILURE_LIMIT` (2) distinct BMCs; RAKP / "unauthorized
  name" trip it at once. After `power.status` is UNREACHABLE or REFUSED,
  `assess_node` reports `power.sensors`/`power.sel` UNKNOWN without a call.
- **SEL comparisons go by record id.** `parse_sel_list` / `diff_sel` in
  `checks/parsers.py`; never compare the tail's length or the BMC's timestamps.
- **Every hostname goes through `topology.valid_hostname()`** — at topology
  load and in `Topology.resolve()` — and is still `shlex.quote`d wherever it
  is put into shell source (`_ping_command`, the mesh script). Any new
  validator reuses that function rather than a copy. ssh argv ends options
  with `--` before the destination; new options go before it.
- **The IPMI network is probed from gateways** (`origin: gateways` in
  `checks.yaml`'s mesh section). A node's `ipmi` entry is its BMC, which the
  node's own OS cannot route to; never make node OSes the sources for it.
- **Live mode is decided once, by `cli.authorize_live()`, per invocation.**
  Never let a configuration layer arm a run: `--execute` does not write
  `run.dry_run` as an override; the decision is written back afterwards so
  everything downstream reads one value. The ARM token stays out of
  `settings._apply_env` and out of `config/.env`. `--simulate` beats
  everything.
- **Phase 2's scope is a `SequencePlan` made before credentials.**
  `plan_sequence()` is the only place `--node`/`--location`/`--from`/`--until`
  become stages; anything it cannot honour exactly is a
  `SequenceSelectionError` (exit 2), never a widening. `--node` predecessors
  are verify-only — never add a power path for them — and `_power_stage` must
  keep refusing hosts outside `plan.allowed_power`. Validate names at plan
  time, not in `run_stage`.
- **A BMC is driven through its own location's gateway.** Use
  `Orchestrator.ipmi_for(node)`, not `orch.ipmi`, for per-node IPMI; build any
  new client with `_make_ipmi_client(creds, gateway)` so it gets the shared
  breaker and `protected=topology.is_protected`.
- **Time goes through the orchestrator's clock and sleep.** No `time.sleep` or
  `time.monotonic` in phases; use `orch.clock`/`orch.sleep` and a `Deadline`
  (`phases/base.py`), so tests run on a fake clock. Run phase work under
  `orch.budget(deadline)`; work it never reached is UNKNOWN `TIMEOUT_SUMMARY`,
  not FAIL. Boot waits share one stage deadline and a `ssh.max_sessions`
  semaphore; do not reintroduce a per-node wait.
- Parsers live in `checks/parsers.py` with their own tests, against real
  command output rather than invented samples.

## 7. Known gaps

- **MC-1 has no node list (#25, data still required).** `config/topology.yaml`
  defines the location, its subnets, `status: pending` metadata and commented
  templates, but the inventory is empty — MC-1 is not in the upstream
  `mu2edaq-operations/scripts/nodes_config.yaml`. Fill it in, run
  `mu2e-node-inventory --validate` (`Topology.validate()`; never called at
  load) and every phase picks it up; phases 1-3 note "no nodes configured for
  <loc>" meanwhile. mc1 is not in the default `topology.locations`.
- **Shared subnets.** mc1 ipmi == mc2 ipmi (192.168.157.0/24) and teststand
  data == mc2 data (10.226.9.0/24); `--validate` warns. Unconfirmed with the
  network owner.
- **The Vault secret is maintained outside this repository.** It is at
  `td/scd/experiments/mu2e/ipmi/config` (note: `ipmi` is a KV folder, not the
  secret) with fields `username`/`password`, both confirmed against the live
  secret. If it is ever re-keyed, `mu2e-vault-ipmi` diagnoses it and
  `vault.ipmi_path` / `ipmi_*_field` fix it without a code change.
- **`ecl-client`'s Python surface is version-dependent.** `report/ecl.py` tries
  the module-level `post()` first and the class API second.
- **Resolved in fix/poweron-safety** (kept here so nobody re-adds them): phase
  2 now waits for a stage's nodes concurrently (#13); `--execute` is a real
  per-invocation gate (`cli.authorize_live`, #11); an unknown `--from`/`--until`
  is an error (#2); `--node`/`--location` bound phase 2 (#1). SIGTERM *is*
  handled: `cli.install_sigterm_handler()` routes it into the Ctrl-C path, so
  the stop script's TERM marks the run `interrupted` and runs cleanup; only
  SIGKILL (`--force`) skips it.
- **Power commands within a stage are serial.** `_power_stage` walks the
  stage's nodes in order (deliberately: the breaker serialises them until the
  BMC account is proven, and it spreads the inrush). A stage of dark BMCs
  therefore costs one `ipmi.timeout` × retries per node before the boot wait.
- **`mu2e-trk-15`..`18` are in the inventory but in no phase-2 stage**
  (`readout` lists trk 1-14). Left as is by decision; `--node mu2e-trk-15`
  on a power-on is therefore an error naming that it is in no stage.
- **SIGKILL is not a clean stop.** SIGTERM is: `cli.install_sigterm_handler()`
  raises KeyboardInterrupt, `assess_nodes` and the mesh probe shut their pools
  down with `cancel_futures=True` instead of waiting out the queue, and
  `KerberosManager.cleanup()` runs. Workers already mid-command keep the
  process alive until that command ends (ThreadPoolExecutor threads are
  joined at interpreter exit), so a stop script's SIGKILL after its grace
  period can still arrive -- after cleanup. `sweep.py` still uses a `with`
  pool; its work items are single short connects.
- **The Kerberos startup guard warns; it does not stop the run.**
  `creds/bootstrap.py:credential_session` logs `ambient_warning()` and
  continues.
  `ambient_warning()` covers one fatal condition and one benign one, so making
  it halt means separating them first.
- **Resolved in fix/report-lifecycle** (kept so nobody re-adds them):
  `--run-id` regeneration attaches to the selected run and needs no
  credentials (#3); the run is finished before the report is rendered or
  posted (#4); superseded failures are `resolved`, not outstanding (#5);
  `runs/<id>/` is rendered from that run's data only — no `archive_run` copy
  of the shared directory (#6); ECL attachments are the run's rendered bundle
  and Vault is created only to post (#18); `--json` stdout is exactly one
  JSON document (#24). The `vault login` child's stdout is our stderr
  (fix/ops), so a first-time Vault login no longer corrupts `--json`.
- **Resolved in fix/ops** (kept so nobody re-adds them): no PID file — the
  driver's run lock replaces it and the stop scripts check identity before
  signalling (#12); a required rebuild that fails rolls the checkout back
  (`git reset --hard`/`--keep` to the full pre-update SHA), never re-execs, and
  exits 2 if the reset fails; the result is `version.selfupdate` in the run
  store (#21). A re-executed child records its own phase-0 result, not the
  parent's.
- **Nothing has been run against the live cluster end to end.** See
  [PROJECT-STATUS.md](PROJECT-STATUS.md) §6.3 for what is and is not verified.

§6.5 of PROJECT-STATUS.md carries these with the reasoning and the fix each
would take.
