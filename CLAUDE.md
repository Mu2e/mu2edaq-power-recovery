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

pytest                                       # 271 tests, no cluster needed
cmake -S . -B build && cmake --build build   # optional C/C++ library
ctest --test-dir build --output-on-failure
```

Diagnostics: `mu2e-node-inventory`, `mu2e-ipmi-tool`, `mu2e-ssh-probe`,
`mu2e-vault-ipmi`, `mu2e-probe`.

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
# power-recovery.yaml — everything except the inventory
run:      {label, dry_run: true, stop_on_stage_failure, from_stage, until_stage}
topology: {file, sequence_file, checks_file, locations: [mc2, teststand]}
selfupdate: {enabled, remote, branch, rebuild_globs, allow_dirty, timeout}
ssh:      {user, root_user, proxy: auto, connect_timeout, options, max_sessions}
ipmi:     {execute_on: gateway, tool, interface, privilege, cipher_suite, retries}
kerberos: {principal, root_principal, min_lifetime, prompt, verify_users,
           use_service_keytabs, service_identities, discover_identities}
vault:    {addr, kv_mount: td, base_path, ipmi_path: ipmi/config, ipmi_*_field,
           allow_file_fallback, fallback_password_file, auto_login}
database: {url, path}          # url set => Postgres
report:   {output_dir, title, keep_runs, publish: {enabled, method, target}}
ecl:      {enabled, url, category, credential_path, attach_html}
logging:  {level, file, capture_command_output, max_capture_bytes}
```

`topology.yaml` (inventory, subnets, classes, `protected:`),
`power-sequence.yaml` (phase-2 stages), `checks.yaml` (thresholds, expected
mounts and services, check profiles, the phase-3 mesh).

Precedence is fixed: **command line > environment > `config/.env` > YAML >
built-in defaults**. Any key is settable as `MU2E_POWER_RECOVERY_<DOTTED_PATH>`
upper-cased with underscores.

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
- **The operator's own principal is always chain position 0**, for root as well
  as ordinary sessions, and nothing may displace it — not the per-host memo, not
  promotion. Service identities are fallbacks; the run returns to the personal
  ticket after each node (`KerberosManager.restore_primary`). Never mutate the
  ambient environment or the default ccache: `get-kerberos-ticket` is always
  called with `--cache`.
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
  secret) with fields `username`/`password`, both confirmed. If it is ever
  re-keyed, `mu2e-vault-ipmi` diagnoses it and `vault.ipmi_path` /
  `ipmi_*_field` fix it without a code change.
- **`ecl-client`'s Python surface is version-dependent.** `report/ecl.py` tries
  the module-level `post()` first and the class API second.
