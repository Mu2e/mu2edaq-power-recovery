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
mu2e-power-netcheck                          # phase 3
mu2e-power-report --post-ecl                 # phase 4

pytest                                       # 309 tests, no cluster needed
cmake -S . -B build && cmake --build build   # optional C/C++ library
ctest --test-dir build --output-on-failure
```

Diagnostics: `mu2e-node-inventory`, `mu2e-ipmi-tool`, `mu2e-ssh-probe`,
`mu2e-vault-ipmi` (Python console scripts) and `mu2e-probe` (the C++ binary,
built by CMake to `build/mu2e-probe`, not a console script).

`--phase` belongs to `mu2e-power-recovery` alone; the four single-phase drivers
reject it. All five drivers take `--version` and `--help`; the four Python
helpers take `--config`, `--env-file`, `--json`, `-v`/`--verbose` and
`-q`/`--quiet`.

## 3. Architecture

```
src/mu2edaq_power_recovery/
  settings.py      layered config (defaults < yaml < .env < env < CLI)
  topology.py      node inventory; NodeRange expansion, classes, protection
  version.py       provenance banner (version, revision, config digest)
  selfupdate.py    phase 0: fast-forward, rebuild, re-exec
  sweep.py         reachability sweep; native extension or Python fallback
  orchestrator.py  shared run state, concurrency, check execution
  state.py         SQLAlchemy run store (SQLite; Postgres by URL)
  console.py       on-screen report
  cli.py           the driver and the four single-phase entry points
  creds/           kerberos.py (kinit/klist, credential chains),
                   ticketsource.py (adapter over mu2edaq-kerberos),
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

## 4. Configuration schema

Four YAML files in `config/`, each with a man page in section 5:

```yaml
# power-recovery.yaml -- everything except the inventory. 77 keys; man 5
# mu2edaq-power-recovery.yaml documents each one.
run:      {label, dry_run: true, stop_on_stage_failure, phase_timeout,
           from_stage, until_stage}
topology: {file, sequence_file, checks_file, locations: [mc2, teststand]}
selfupdate: {enabled, remote, branch, rebuild_globs, allow_dirty, timeout}
ssh:      {user, root_user, proxy: auto, connect_timeout, command_timeout,
           options, max_sessions}
ipmi:     {execute_on: gateway, username, tool, interface, privilege,
           cipher_suite, timeout, retries, power_on_delay, message_timeout,
           tool_retries, extra_args, stop_on_auth_failure}
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
mounts and services, check profiles, the phase-3 mesh).

Precedence is fixed: **command line > environment > `config/.env` > YAML >
built-in defaults**. Any key is settable as `MU2E_POWER_RECOVERY_<DOTTED_PATH>`
upper-cased with underscores.

Three variables are read directly rather than through that mechanism:
`NO_COLOR` (never colour), `FORCE_COLOR` (colour even off a tty; `NO_COLOR`
wins) — both in `console.py` — and `MU2E_POWER_RECOVERY_UPDATED`
(`selfupdate.REEXEC_GUARD`), set across phase 0's `os.execve` so the
re-executed process does not check for updates again.

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

That is **partly enforced**: the autouse `no_real_network` fixture in
`tests/conftest.py` monkeypatches `LocalTransport.run` and fails any test whose
first command word is `ssh`, `scp`, `rsync`, `ping`, `ping6`, `ipmitool`,
`kinit`, `klist`, `kdestroy`, `vault`, `get-kerberos-ticket` or `vault-client`.
Opt out with `@pytest.mark.allow_network` (declared in `pyproject.toml`);
`tests/unit/test_network_guard.py` is the meta-test. Do not weaken it — it was
added after the suite was caught making real ssh connections, and extended
after a `--simulate` run was caught pinging the live gateways.

**The guard hooks one method, so know what it misses.** Anything not routed
through `LocalTransport.run` is unprotected: `creds/ticketsource.py` calls
`subprocess.run` directly (`klist`, `klist -l`, `kswitch`,
`get-kerberos-ticket`/`vault-client`), and `creds/vault.py` calls
`subprocess.call(["vault", "login", ...])` and talks HTTPS to Vault through
`hvac`. A new test touching `KerberosManager`'s service path or
`VaultCredentials` would reach the real Kerberos collection or
`ssivault.fnal.gov` without tripping anything — stub those yourself.

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
  each mint and restore it after. `cleanup()` names each cache to `kdestroy -c`
  and refuses any file cache outside the run's own temporary directory.
- **Never weaken the protected-host refusal.** It is the one place the tool
  declines to do what it is told, and it is deliberate.
- **Keep FAIL and UNKNOWN distinct.** "It is broken" and "we could not look"
  need different responses.
- Parsers live in `checks/parsers.py` with their own tests, against real
  command output rather than invented samples.

## 7. Known gaps

- **MC-1 has no node list.** `config/topology.yaml` defines the location and
  its lab subnet but the inventory is empty — MC-1 is not in the upstream
  `mu2edaq-operations/scripts/nodes_config.yaml`. Fill it in and every phase
  picks it up.
- **The Vault secret is maintained outside this repository.** It is at
  `td/scd/experiments/mu2e/ipmi/config` (note: `ipmi` is a KV folder, not the
  secret) with fields `username`/`password`, both confirmed against the live
  secret. If it is ever re-keyed, `mu2e-vault-ipmi` diagnoses it and
  `vault.ipmi_path` / `ipmi_*_field` fix it without a code change.
- **`ecl-client`'s Python surface is version-dependent.** `report/ecl.py` tries
  the module-level `post()` first and the class API second.
- **Abandoning the service identities is decided by a substring.**
  `KerberosManager.service_credential` tests
  `"default credential cache" in str(exc)` to tell an unrecoverable
  displacement from an ordinary "no keytab for this identity". Reword that
  message in `ticketsource.py` and the run silently downgrades the first to the
  second and carries on minting under a displaced default. Carrying the
  decision on the exception *type* would fix it.
- **Phase 2 waits for nodes one at a time.** `_wait_for_nodes` walks a stage's
  nodes in sequence, so one node that never returns costs the whole
  `boot_timeout` before the next is tried. The `readout` stage has 28 nodes.
- **`--execute` is not an independent gate.** It only sets
  `run.dry_run = False` (`cli.py:184-185`), and `IPMIClient` is gated on
  `run.dry_run` alone (`orchestrator.py:308`). Setting `run.dry_run: false` in
  the config, `.env` or the environment arms live power commands with no flag.
  Several documents claimed two gates; they have been corrected to describe
  this. `--simulate` is the only genuinely independent gate.
- **Nothing installs a SIGTERM handler.** No module under `src/` imports
  `signal`; `cli.py` catches `KeyboardInterrupt` only. So
  `stop-mu2edaq-power-recovery.sh` kills the run outright: the store is left
  saying `running` and `KerberosManager.cleanup()` never runs, leaving the
  run's private root-capable caches behind. SIGINT is the clean path.
- **The Kerberos startup guard warns; it does not stop the run.**
  `orchestrator.py:230-233` logs `ambient_warning()` and continues.
  `ambient_warning()` covers one fatal condition and one benign one, so making
  it halt means separating them first.
- **An unmatched `--from`/`--until` stage name is silently ignored**
  (`phase2_poweron.py:289-295`), so a typo widens the power-on to the whole
  sequence rather than erroring.
- **Nothing has been run against the live cluster end to end.** See
  [PROJECT-STATUS.md](PROJECT-STATUS.md) §6.3 for what is and is not verified.

§6.5 of PROJECT-STATUS.md carries these with the reasoning and the fix each
would take.
