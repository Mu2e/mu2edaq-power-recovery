---
title: mu2edaq-power-recovery interfaces
group: operations
tier: A
audience: expert
summary: What mu2edaq-power-recovery reads, writes, runs and talks to - command surface, subprocess contracts, secrets, network endpoints, run store and report data files
---

# mu2edaq-power-recovery — interfaces

Every external contract of the package, each with a citation into the code.
Citations are against `origin/main` at `dc91bb2` (v0.2.0); see the revision
note in [ARCHITECTURE.md](ARCHITECTURE.md). Mechanism and rationale are in
[DESIGN.md](DESIGN.md). Option-by-option detail and exit statuses are in the
man pages; this page does not repeat them.

The package has no daemon and binds no port. Everything below is either
something an operator runs or something the tools reach out to.

## 1. Command surface

| Command | Kind | Entry point | Man page |
|---|---|---|---|
| `mu2e-power-recovery` | driver, phases 1-4 (`--phase`) | `cli.py:673` `main` | `man1/mu2e-power-recovery.1` |
| `mu2e-power-state` | phase 1, always dry | `cli.py:1053` | `man1/mu2e-power-state.1` |
| `mu2e-power-on` | phase 2 | `cli.py:1058` | `man1/mu2e-power-on.1` |
| `mu2e-power-netcheck` | phase 3, always dry | `cli.py:1068` | `man1/mu2e-power-netcheck.1` |
| `mu2e-power-report` | phase 4, store only | `cli.py:1074` | `man1/mu2e-power-report.1` |
| `mu2e-node-inventory` | inventory, `--protected`, `--validate` | `tools/node_inventory.py` | `man1/mu2e-node-inventory.1` |
| `mu2e-ipmi-tool` | one ipmitool command per BMC, via a gateway | `tools/ipmi_tool.py:95` | `man1/mu2e-ipmi-tool.1` |
| `mu2e-ssh-probe` | show or run the run's exact ssh argv | `tools/ssh_probe.py` | `man1/mu2e-ssh-probe.1` |
| `mu2e-vault-ipmi` | check the Vault BMC secret | `tools/vault_ipmi.py` | `man1/mu2e-vault-ipmi.1` |
| `mu2e-probe` | C++ TCP sweep (CMake target, not a console script) | `src/cpp/mu2e-probe-cli.cpp` | `man1/mu2e-probe.1` |
| `python -m mu2edaq_power_recovery.runlock status\|pid` | run-lock inspection | `runlock.py:292` | `man1/mu2edaq-runlock.1` |
| `bootstrap.sh`/`.ps1`, `start-`/`stop-mu2edaq-power-recovery.sh`/`.ps1` | setup, launch, stop | — | `man1/mu2edaq-power-recovery-bootstrap.1` |
| `tools/generate-docs.py` | regenerate and check generated man sections | — | `man1/mu2edaq-generate-docs.1` |

Console scripts are declared at `pyproject.toml:46-56`. All nine have a
section-1 page.

**Driver exit codes** (`cli.py:84-91`): 0 clean, 1 node failures, 2 could not
start (configuration, credentials, live-run authorisation, scope, lock busy,
failed rollback), 3 interrupted. With `--json`, stdout carries exactly one JSON
document `{version, run_id, status, exit_code, phases, report[, error]}` and
every human line goes to stderr (`cli.py:673-702`).

**Live-run authorisation** (`cli.py:282-371`). Live means `--execute`, or
`MU2E_POWER_RECOVERY_ARM` equal to a non-empty `run.label` with
`run.dry_run: false`. `run.dry_run: false` on its own exits 2 for any
invocation that includes phase 2. `--simulate` always wins. This governs the
drivers only. `mu2e-ipmi-tool` has its own `--execute` and `--yes`, which
cover five `chassis power` spellings (PWR-001).

## 2. Environment

| Variable | Effect | Where |
|---|---|---|
| `MU2E_POWER_RECOVERY_<DOTTED_PATH>` | overrides any config key; type taken from the existing value; lists are comma-separated | `settings.py:208`, `:437-450` |
| `MU2E_POWER_RECOVERY_ARM` | per-invocation live token; never configuration; an error in `config/.env` | `settings.py:37-42`, `:454-458` |
| `MU2E_POWER_RECOVERY_UPDATED` | set across the phase-0 `execve`; suppresses a second update | `selfupdate.py:41` |
| `MU2E_POWER_RECOVERY_RUN_LOCK_FILE` | `run.lock_file` (used by the tests' private lock) | via the generic mapping |
| `VAULT_ADDR` | sets `vault.addr` | `settings.py:465-466` |
| `VAULT_TOKEN` | used before `~/.vault-token` | `creds/vault.py:86` |
| `KRB5CCNAME` | set per ssh/kinit child to select a credential cache | `transport/ssh.py:184-200` |
| `VAULT_ORG` | not read here, but inherited by `vault-client`/`get-kerberos-ticket`, where it selects the identity set (PWR-012) | `creds/ticketsource.py:309` |
| `NO_COLOR`, `FORCE_COLOR` | console colour | `console.py` |
| `VENV_DIR` | start script's venv location | `start-mu2edaq-power-recovery.sh:26` |

## 3. Files

| Path | R/W | Content |
|---|---|---|
| `config/power-recovery.yaml`, `topology.yaml`, `power-sequence.yaml`, `checks.yaml` | R | configuration; `man 5 mu2edaq-*.yaml` |
| `config/.env` | R | dotenv layer; no interpolation (`settings.py:275`) |
| `~/.vault-token` | R (written by `vault login`) | Vault token |
| `~/.ipmipasswd` | R | BMC password fallback; no permission check (PWR-007) |
| `data/power-recovery.db` | R/W | SQLite run store (`database.path`); Postgres when `database.url` is set |
| `logs/power-recovery.log` | W | Python log (`logging.file`) |
| `logs/power-recovery.lock` | R/W | run lock plus `{pid, started_at, cmdline, host}`; never deleted |
| `html/`, `html/runs/<id>/` | W | static report; the newest `report.keep_runs` bundles are kept |
| `$TMPDIR/mu2e-recovery-krb5-*/` | W | private ccaches (mode 0700, `mkdtemp`); destroyed by name at exit (`creds/kerberos.py:237-238`) |
| `requirements.txt` | **W by `bootstrap.sh`** | regenerated from `pip freeze` on every bootstrap (PWR-005) |

## 4. Subprocess contracts

| Program | Where it runs | Invocation | Secrets | Where |
|---|---|---|---|---|
| `ssh` | workstation | `ssh <ssh.options> -o ConnectTimeout=N [-J <gateway>] -- [login@]host '<cmd>'` | Kerberos via `KRB5CCNAME`; `BatchMode=yes` | `transport/ssh.py:202-229` |
| `ipmitool` | **gateway**, over `ssh` with no `-J` | `/bin/sh -c 'IFS= read -r IPMI_PASSWORD \|\| exit 97; export IPMI_PASSWORD; timeout T ipmitool -I lanplus -H <bmc> -U <user> -L Operator -C 3 -E [extra_args] <args>'`. `-N`/`-R` only when configured (null by default) | password on stdin | `transport/ipmi.py:301-338`, `:469-497` |
| `ping` | gateway (BMC pre-check, 3 echoes); nodes or gateways (phase-3 mesh) | iputils/BSD/Windows dialect chosen per transport | — | `transport/ipmi.py:405-424`; `checks/mesh.py:329-356` |
| `kinit`, `klist`, `kdestroy`, `kswitch` | workstation | `kinit -c <cache> <principal>` | password on stdin | `creds/kerberos.py:407-449` |
| `vault-client` (mu2edaq-kerberos) | workstation | `vault-client identities [kerberos.vault_client_args]` | uses the operator's Vault token | `creds/ticketsource.py:295-320` |
| `get-kerberos-ticket` (mu2edaq-kerberos) | workstation | `get-kerberos-ticket <identity> --cache <path> [vault_client_args]` | keytab handled entirely by mu2edaq-kerberos | `creds/ticketsource.py:384` |
| `vault login -method=ldap` | workstation, interactive | stdout redirected to stderr | prints the token to the terminal | `creds/vault.py:112-116` |
| `git` | workstation (checkout) | `fetch --quiet <remote> <branch>`; `rev-list --left-right --count`; `merge --ff-only`; `reset --hard\|--keep <sha>` on rollback | — | `selfupdate.py:168-246` |
| `bootstrap.sh` / `bootstrap.ps1` | workstation | phase-0 rebuild when `selfupdate.rebuild_globs` match | — | `selfupdate.py:291-315` |
| `rsync` / `scp` | workstation | `rsync -az --delete html/ <target>`; `scp -r html/. <target>` | ssh credentials of the operator | `report/publish.py:84-102` |

Lookup order for the mu2edaq-kerberos commands: `kerberos.<cmd>_command`,
then `PATH`, then `../mu2edaq-kerberos/venv/bin/<cmd>` (or `Scripts\<cmd>.exe`),
then, for the ticket command only, `../mu2edaq-kerberos/src/mu2edaq_kerberos/get-kerberos-ticket.sh`
(`creds/ticketsource.py:176-195`).

## 5. Network endpoints (all outbound)

| Endpoint | Protocol | From | Config |
|---|---|---|---|
| `mu2egateway01/02.fnal.gov`, `mu2edaq-gateway.fnal.gov` | ssh 22/tcp, GSSAPI | workstation | `topology.yaml` `gateways:`, `ipmi_gateways:` |
| DAQ nodes | ssh 22/tcp via `-J` | workstation, through a gateway | `topology.yaml` |
| BMCs `*-ipmi.fnal.gov` (192.168.157.0/24) | RMCP+ 623/udp, ICMP | gateway | `topology.yaml:102-118`, `:234-237` |
| gateways, TCP/22 pre-filter | TCP connect | workstation (`libmu2eprobe` or Python fallback) | `transport/ssh.py:487-498` |
| `https://ssivault.fnal.gov:8200` | HTTPS (hvac; TLS verification left at the hvac default) | workstation | `vault.addr` |
| Kerberos KDCs | krb5 | workstation | the site's `krb5.conf` |
| `https://dbweb8.fnal.gov:8443/ECL/mu2e`, category `DAQ/Recovery` | HTTPS via `ecl-client` | workstation | `ecl.url`, `ecl.category` |
| `selfupdate.remote` (the checkout's `origin`, `git@github.com:Mu2e/mu2edaq-power-recovery.git` here) | git over ssh or https | workstation | `selfupdate.remote`, `selfupdate.branch` |
| `report.publish.target` | rsync/scp over ssh | workstation | `report.publish.*` |

## 6. Secrets

| Secret | Location | Fields | Used by |
|---|---|---|---|
| BMC account | Vault KV v2 `td/scd/experiments/mu2e/ipmi/config` (mount `td`, `vault.base_path` + `vault.ipmi_path`) | `username`, `password` (synonyms accepted, `creds/vault.py:256-272`) | `IPMIClient`; `ipmi.username` overrides the user for the run only (PWR-008) |
| BMC fallback | `~/.ipmipasswd`, user `vault.fallback_user` (`MU2E`) | whole file is the password | taken on any Vault error (PWR-007) |
| ECL API | Vault `td/scd/experiments/mu2e/ecl` | `user`/`username`, `key`/`password`/`api_key` | `report/ecl.py:38-49` |
| Service keytabs | Vault `td/scd/experiments/mu2e/kerberos/<identity>` | owned by mu2edaq-kerberos; never read here | `creds/ticketsource.py` |

## 7. ECL entry

Built by `ECLPoster` (`report/ecl.py:53-151`) from the reconciled narrative:

- **subject:** `[DRY RUN] <run.label> -- <headline>`;
- **body:** fixed-order plain text: phases, node totals, powered-on hosts,
  up to 60 outstanding and 30 resolved problems, incomplete power actions,
  next steps;
- **tags:** `power-recovery`, plus `dry-run` and `needs-attention` as they
  apply;
- **attachments:** the run's rendered bundle pages when `ecl.attach_html`
  is set.

A run stored as simulated is never posted (`phases/phase4_report.py:141-150`,
`cli.py:833-845`).

## 8. Run store schema

SQLAlchemy Core, SQLite by default (`state.py:34-130`). Tables:

| Table | Columns |
|---|---|
| `runs` | `id, label, started_at, finished_at, operator, workstation, dry_run, status, version, settings, simulated` |
| `phases` | `id, run_id, name, number, started_at, finished_at, status, summary, data` |
| `node_states` | `id, phase_id, hostname, location, node_class, status, power_state, summary, data` |
| `check_results` | `id, phase_id, hostname, check_id, status, summary, detail, data, evidence, duration, recorded_at` |
| `actions` | `id, run_id, phase_id, hostname, action, target, outcome, dry_run, detail, recorded_at` |
| `events` | `id, run_id, level, message, ...` |

`runs.settings` holds `Settings.redacted()`: the value of any key whose last
path component ends in `pass|passwd|password|token|secret|key` is replaced.
Every power attempt is written to `actions` as `attempting` **before** it is
issued (`phases/phase2_poweron.py:477`).

## 9. Report data files

Pages: `index.html`, `initial-state.html`, `power-on.html`, `network.html`,
`detail.html`, `runs.html`, `about.html`, `api.html`, `sitemap.html`
(`report/html.py:34-44`). JSON under `data/` (`report/html.py:542-550`):

| File | Content |
|---|---|
| `summary.json` | run metadata, phase status, reconciled counts |
| `assess.json` | every phase-1 check result with evidence |
| `poweron.json` | phase-2 stage outcomes and power actions |
| `network.json` | phase-3 connectivity matrices |
| `report.json` | phase-4 narrative and ECL outcome |
| `run-export.json` | the complete run export, including every command's captured stdout/stderr |
| `inventory.json` | the nodes the run recorded |

`run-export.json` carries raw remote command output: interface addresses,
mounts, service states, kernel and SEL lines. Treat a published report as
internal.
