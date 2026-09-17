// mu2eprobe -- parallel host reachability probing for the Mu2e DAQ power
// recovery tools.
//
// Why this exists in C++ at all: phase 1 has to answer "which of ~60 hosts on
// three network segments are up" before it can do anything else, and phase 3
// probes a full mesh on the data network.  Doing that from Python means either
// one subprocess per host (fork/exec dominates the measurement) or a thread per
// host (the GIL turns the wait into a queue).  A TCP connect sweep in C++ with
// OpenMP costs one socket and no process per host and finishes a 60-host sweep
// in about one timeout.
//
// This library is strictly optional.  Python falls back to its own
// implementation when the extension is not built (see checks/reachability.py
// and the note in README.md), so nothing here is on the critical path for a
// recovery -- it only makes the sweep faster.
//
// C++17, no dependencies beyond the C++ standard library and POSIX sockets.

#ifndef MU2EPROBE_PROBE_HPP
#define MU2EPROBE_PROBE_HPP

#include <chrono>
#include <cstdint>
#include <string>
#include <vector>

namespace mu2eprobe {

/// Outcome of one probe against one host.
enum class Outcome {
  Open,          ///< the port accepted a connection
  Refused,       ///< the host answered but refused the port: it is UP
  Timeout,       ///< no answer inside the budget
  Unresolved,    ///< the name did not resolve
  Error,         ///< socket/system error; see Result::detail
};

/// One probe result.
struct Result {
  std::string host;         ///< host as given
  std::string address;      ///< resolved address, empty when unresolved
  std::uint16_t port = 22;
  Outcome outcome = Outcome::Error;
  double elapsed_ms = 0.0;
  std::string detail;       ///< errno text for Outcome::Error

  /// True when the host demonstrably answered.
  ///
  /// A refused connection counts: the machine is up and its TCP stack replied,
  /// which is precisely what a reachability sweep is asking.  Treating refusal
  /// as "down" would report every node whose sshd has not started yet as dead,
  /// which during a power-on is most of them.
  bool reachable() const {
    return outcome == Outcome::Open || outcome == Outcome::Refused;
  }

  const char* outcome_name() const;
};

/// Probe settings shared by a sweep.
struct Options {
  std::uint16_t port = 22;            ///< TCP port to connect to
  int timeout_ms = 2000;              ///< per-host connect budget
  int threads = 0;                    ///< 0 = OpenMP default (hardware threads)
  bool resolve_only = false;          ///< stop after name resolution
};

/// Probe one host.  Never throws; failures are reported in the Result.
Result probe_one(const std::string& host, const Options& options = {});

/// Probe many hosts in parallel.
///
/// Results are returned in the same order as *hosts*, regardless of the order
/// the probes completed -- the caller has a node list in a meaningful order and
/// should not have to re-sort it.
std::vector<Result> probe_many(const std::vector<std::string>& hosts,
                               const Options& options = {});

/// Hosts from *results* that answered, in input order.
std::vector<std::string> reachable_hosts(const std::vector<Result>& results);

/// Library version, matching the Python package version.
const char* version();

/// True when the library was compiled with OpenMP support.
bool has_openmp();

}  // namespace mu2eprobe

#endif  // MU2EPROBE_PROBE_HPP
