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
| `-DBOOTSTRAP_VENV=OFF` | ON | Do not create the venv during the build |

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

The one thing worth setting up front is your principals:

```sh
# config/.env
MU2E_POWER_RECOVERY_KERBEROS_PRINCIPAL=you@FNAL.GOV
MU2E_POWER_RECOVERY_KERBEROS_ROOT_PRINCIPAL=you/root@FNAL.GOV
```

## Verifying the installation

Run these in order. None of them changes anything.

```sh
# 1. the tools start, and the inventory parses
mu2e-node-inventory -l mc2

# 2. every check is registered
mu2e-power-recovery --list-checks

# 3. the full four-phase run, against a scripted cluster
mu2e-power-recovery --phase all --simulate

# 4. the report renders
open html/index.html            # or xdg-open, or start

# 5. the test suite
pytest

# 6. Vault holds what the tools expect (needs a Vault token)
mu2e-vault-ipmi

# 7. a gateway answers (needs a Kerberos ticket)
mu2e-ssh-probe mu2egateway01 --run true
```

Steps 1–5 need no credentials and no network. If they pass, the installation is
sound; 6 and 7 test your access rather than the installation.

## Upgrading

Phase 0 does it: every invocation checks `origin`, fast-forwards if behind,
reruns `bootstrap.sh` if a build input changed, and restarts once. The update
is fast-forward only — a dirty or divergent working tree is reported and left
alone, never reconciled automatically.

To upgrade by hand, or to disable the automatic path:

```sh
git pull && ./bootstrap.sh
mu2e-power-recovery --no-self-update ...
```

## Uninstalling

```sh
rm -rf venv build
sudo xargs rm -f < build/install_manifest.txt   # if cmake --install was used
```

`data/power-recovery.db` and `html/` hold the record of past recoveries; delete
them deliberately, not as part of an uninstall.
