# Installing mu2edaq-power-recovery

The tools run on the machine an administrator drives the recovery *from*, not
on the DAQ nodes. That machine needs network access to the gateways and a way
to obtain a Kerberos ticket; it does not need to be inside the DAQ networks.

## Requirements

### Always

| Component | Why | Minimum |
|---|---|---|
| Python | The tools | 3.9 |
| OpenSSH client | Node access, ProxyJump through a gateway | any |
| Kerberos client (`kinit`, `klist`) | Authentication to the cluster | any |
| `kswitch` | macOS/Heimdal only: restoring the default credential cache after a service ticket displaces it | ships with macOS |
| `git` | The phase-0 self-update | any |

Python dependencies (installed by `bootstrap.sh`): PyYAML, Jinja2, SQLAlchemy,
hvac.

### Optional

| Component | Enables | Without it |
|---|---|---|
| CMake ≥ 3.16, a C++17 compiler | `libmu2eprobe`, the parallel sweep | Pure-Python fallback, identical semantics, slower |
| OpenMP | Parallelism inside `libmu2eprobe` | The sweep runs serially |
| pybind11 | The `mu2eprobe` Python extension | The fallback is used |
| CppUnit | The richer C++ test harness | A built-in runner, same assertions |
| `vault` CLI | Interactive Vault login | Supply `VAULT_TOKEN` yourself |
| `ecl-client` | Posting the phase-4 report to the logbook | The HTML report is still produced |
| `psycopg2` | Postgres run store | SQLite |

Note what is *not* required on the workstation: `ipmitool` is run on the
gateway, not locally.

## Linux and macOS

```sh
git clone git@github.com:Mu2e/mu2edaq-power-recovery.git
cd mu2edaq-power-recovery
./bootstrap.sh
. venv/bin/activate
mu2e-power-recovery --phase all --simulate     # verify the installation
```

`bootstrap.sh` is idempotent: run it again after a `git pull`, or let phase 0
run it for you. It creates `venv/`, installs the package in editable mode,
regenerates `requirements.txt` from what was actually resolved, and creates
`logs/`, `data/`, `html/` and `core/`.

Environment knobs:

```sh
MU2EDAQ_PYTHON=python3.9 ./bootstrap.sh   # pick the interpreter
VENV_DIR=/opt/venvs/recovery ./bootstrap.sh
./bootstrap.sh --with-cpp                 # also build libmu2eprobe
```

### Red Hat / Alma / Rocky 9

```sh
sudo dnf install -y python3 python3-pip git openssh-clients krb5-workstation
# optional, for the C++ library:
sudo dnf install -y cmake gcc-c++ libomp-devel cppunit-devel
```

### Ubuntu / Debian

```sh
sudo apt install -y python3 python3-venv python3-pip git openssh-client krb5-user
# optional:
sudo apt install -y cmake g++ libomp-dev libcppunit-dev
```

### macOS

```sh
brew install python git
# optional; Apple clang needs libomp separately:
brew install cmake libomp cppunit
```

Apple's `clang` does not enable OpenMP by default. Without `libomp` the library
still builds and works, serially — CMake reports which.

**Kerberos on macOS is Heimdal, not MIT, and the difference is not cosmetic.**
Heimdal keeps credential caches in an `API:` *collection* rather than as files,
ignores `KRB5CCNAME=FILE:` when running `kinit`, and makes each newly minted
cache the collection **default** — so acquiring a service ticket displaces your
personal one as "the default". It is displaced, never destroyed. The tools are
built for this: they look identities up in the collection by name, record the
default before each mint and restore it afterwards, and warn at the top of a
run when the default cache holds a service identity rather than a person. That
last one is a warning, not a refusal — the run continues into every phase — so
act on it when you see it.

Nothing needs configuring, but two commands are worth knowing before you need
them:

```sh
klist -l                     # every cache in the collection, not just the default
kswitch -p you@FNAL.GOV      # make yours the default again
```

`kswitch` ships with macOS. `docs/OPERATIONS.md` has the full story, and
`docs/DESIGN.md` §2 has the reasoning.

## Windows 11

```powershell
git clone git@github.com:Mu2e/mu2edaq-power-recovery.git
cd mu2edaq-power-recovery
.\bootstrap.ps1
.\venv\Scripts\Activate.ps1
mu2e-power-recovery --phase all --simulate
```

Windows 10 and later ship an OpenSSH client; enable it under
*Settings → Apps → Optional Features* if `ssh` is not on `PATH`. You also need
a Kerberos client able to obtain an `FNAL.GOV` ticket — either the MIT
Kerberos for Windows package, or a domain-joined machine whose realm trust
provides one.

For the C++ library on Windows, use the MSYS2/MinGW toolchain: the probe uses
POSIX sockets with a winsock2 link, which MSVC does not provide directly.

```powershell
.\bootstrap.ps1 -WithCpp
```

The start and stop scripts have PowerShell equivalents.
`start-mu2edaq-power-recovery.ps1` passes every argument straight through to
`mu2e-power-recovery` and runs the read-only survey with none.
`stop-mu2edaq-power-recovery.ps1` takes `-Status` (report, change nothing),
`-Force` (kill at once rather than waiting) and `-Grace <seconds>` (default 30).

## Full CMake build

Builds the C/C++ library, the `mu2e-probe` command, the C++ unit tests, the
Python extension, the venv, and installs the man pages and config.

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build
ctest --test-dir build --output-on-failure
sudo cmake --install build --prefix /usr/local
```

Options:

| Option | Default | Effect |
|---|---|---|
| `-DBUILD_PROBE=OFF` | ON | Skip the C/C++ library entirely |
| `-DBUILD_BINDINGS=OFF` | ON | Skip the Python extension |
| `-DBUILD_TESTING=OFF` | ON | Skip the test targets |
| `-DBOOTSTRAP_VENV=OFF` | ON | Do not create or refresh `venv/` during the build. The `docs` and `pytest` targets and the `pytest`, `docs-check` and `simulated-run` ctest entries all need that interpreter, so they are registered only if `venv/` already exists; CMake prints a status line when it skips them. `bootstrap.sh` uses this flag, and makes the venv before configuring, so its `build/` has all four ctest entries. |

The build never fails because an optional component is missing. If there is no
compiler, no OpenMP or no pybind11, CMake says so and produces a working
installation that uses the Python fallback — a recovery must not depend on a
toolchain being present on the machine driving it.

## Configuration

```sh
cp config/.env.example config/.env
$EDITOR config/.env
```

`config/.env` is gitignored. Everything in it can equally be set in the
environment or on the command line; see `man 5 mu2edaq-power-recovery.yaml`.
Any configuration key is settable as `MU2E_POWER_RECOVERY_<DOTTED_PATH>`,
upper-cased with underscores.

Three variables are read directly rather than through that mechanism:

| Variable | Effect |
|---|---|
| `NO_COLOR` | Set to anything: never colour the console output. |
| `FORCE_COLOR` | Set to anything: colour even when stdout is not a terminal. `NO_COLOR` wins. |
| `MU2E_POWER_RECOVERY_UPDATED` | Set by phase 0 across its own re-exec so the restarted process does not check for updates again. Use `--no-self-update` to skip the check deliberately. |

The one thing worth setting up front is your principals:

```sh
# config/.env
MU2E_POWER_RECOVERY_KERBEROS_PRINCIPAL=you@FNAL.GOV
MU2E_POWER_RECOVERY_KERBEROS_ROOT_PRINCIPAL=you/root@FNAL.GOV
```

## Verifying the installation

Run these in order. None of them changes anything.

```sh
# 1. the package is installed and the entry points resolve
mu2e-power-recovery --version           # mu2edaq-power-recovery 0.1.0

# 2. the tools start, and the inventory parses
mu2e-node-inventory -l mc2

# 3. every check is registered  (expect 25)
mu2e-power-recovery --list-checks

# 4. the full four-phase run, against a scripted cluster
mu2e-power-recovery --phase all --simulate

# 5. the report renders
open html/index.html            # or xdg-open, or start

# 6. the test suite  (309 tests, ~30 s)
pytest

# 7. Vault holds what the tools expect (needs a Vault token)
mu2e-vault-ipmi

# 8. a gateway answers, and with which credential (needs a Kerberos ticket)
mu2e-ssh-probe mu2egateway01 --run true
```

`--run true` in step 8 is what makes it a test. Without `--run`,
`mu2e-ssh-probe` opens no connection: it prints the `ssh` command and the
credentials it would try, and exits 0 whether or not the gateway would answer.

Steps 1–6 need no credentials and no network — the test suite refuses to make
one for anything routed through its transport layer, and `--simulate` answers
every command from a built-in script. If they pass, the installation is sound.
Steps 7 and 8 test your *access* rather than the installation, and are the two
that fail at 3 a.m. if nobody tried them in daylight.

`mu2e-probe` is built by CMake rather than installed as a console script, so
check it separately if you built the C++ library:

```sh
build/mu2e-probe --version
```

## Upgrading

Phase 0 does it: every invocation that will contact the cluster checks
`origin`, fast-forwards if behind, reruns `bootstrap.sh` if a build input
changed, and restarts once. The update is fast-forward only — a dirty or
divergent working tree is reported and left alone, never reconciled
automatically. `--simulate` and `--list-checks` skip the check, so a rehearsal
never updates the code it is rehearsing.

To upgrade by hand, or to disable the automatic path:

```sh
git pull && ./bootstrap.sh
mu2e-power-recovery --no-self-update ...
```

## Uninstalling

```sh
sudo xargs rm -f < build/install_manifest.txt   # if cmake --install was used
rm -rf venv build
```

In that order: the manifest lives inside `build/`, so removing the build tree
first leaves nothing to read.

`data/power-recovery.db` and `html/` hold the record of past recoveries; delete
them deliberately, not as part of an uninstall.
