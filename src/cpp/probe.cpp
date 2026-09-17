// Implementation of the parallel reachability sweep.
//
// The probe is a non-blocking TCP connect with a bounded poll(), not ICMP.
// Three reasons, in order of importance:
//
//   1. It needs no privilege.  A raw ICMP socket needs root or CAP_NET_RAW,
//      and the recovery tools deliberately run as an ordinary user on the
//      operator's workstation.
//   2. It answers a more useful question.  "The IP stack is up" is weaker than
//      "port 22 is answering", and port 22 answering is what the next phase
//      actually needs.  A refused connection still proves the host is up, so
//      that case is reported as reachable too (see Result::reachable).
//   3. ICMP is filtered in places on the lab network; TCP/22 is not, because
//      the cluster is administered over it.

#include "mu2eprobe/probe.hpp"
#include "mu2eprobe/probe.h"

#include <cerrno>
#include <cstring>
#include <mutex>

#include <arpa/inet.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>

#ifdef _OPENMP
#include <omp.h>
#endif

namespace mu2eprobe {
namespace {

constexpr const char* kVersion = "0.1.0";

using Clock = std::chrono::steady_clock;

double elapsed_ms_since(const Clock::time_point& start) {
  return std::chrono::duration<double, std::milli>(Clock::now() - start).count();
}

/// RAII wrapper so no path out of probe_one can leak a descriptor.  A sweep
/// that leaks one descriptor per host exhausts the process limit on the
/// second run against a large cluster.
class Socket {
 public:
  explicit Socket(int fd) : fd_(fd) {}
  ~Socket() { if (fd_ >= 0) ::close(fd_); }
  Socket(const Socket&) = delete;
  Socket& operator=(const Socket&) = delete;
  int get() const { return fd_; }
  bool valid() const { return fd_ >= 0; }

 private:
  int fd_;
};

std::string errno_text(int err) {
  char buffer[128];
#if defined(__GLIBC__) && defined(_GNU_SOURCE)
  // glibc's strerror_r returns char*, which may not be 'buffer'.
  return std::string(::strerror_r(err, buffer, sizeof(buffer)));
#else
  if (::strerror_r(err, buffer, sizeof(buffer)) != 0) {
    return "unknown error";
  }
  return std::string(buffer);
#endif
}

/// getaddrinfo is not guaranteed thread-safe on every platform's libc for all
/// configurations; serialising resolution costs little (it is cached by the
/// resolver after the first lookup of each name) and removes the doubt.
std::mutex& resolver_mutex() {
  static std::mutex mutex;
  return mutex;
}

}  // namespace

const char* Result::outcome_name() const {
  return mu2e_probe_outcome_name(static_cast<int>(outcome));
}

Result probe_one(const std::string& host, const Options& options) {
  Result result;
  result.host = host;
  result.port = options.port ? options.port : 22;
  const auto start = Clock::now();

  // --- resolve ------------------------------------------------------------
  addrinfo hints{};
  hints.ai_family = AF_UNSPEC;
  hints.ai_socktype = SOCK_STREAM;
  addrinfo* candidates = nullptr;
  const std::string service = std::to_string(result.port);

  int rc = 0;
  {
    std::lock_guard<std::mutex> lock(resolver_mutex());
    rc = ::getaddrinfo(host.c_str(), service.c_str(), &hints, &candidates);
  }
  if (rc != 0 || candidates == nullptr) {
    result.outcome = Outcome::Unresolved;
    result.detail = ::gai_strerror(rc);
    result.elapsed_ms = elapsed_ms_since(start);
    return result;
  }

  // Record the first resolved address for the report, whatever happens next.
  {
    char text[INET6_ADDRSTRLEN] = {0};
    if (candidates->ai_family == AF_INET) {
      auto* in4 = reinterpret_cast<sockaddr_in*>(candidates->ai_addr);
      ::inet_ntop(AF_INET, &in4->sin_addr, text, sizeof(text));
    } else if (candidates->ai_family == AF_INET6) {
      auto* in6 = reinterpret_cast<sockaddr_in6*>(candidates->ai_addr);
      ::inet_ntop(AF_INET6, &in6->sin6_addr, text, sizeof(text));
    }
    result.address = text;
  }

  if (options.resolve_only) {
    result.outcome = Outcome::Open;
    result.elapsed_ms = elapsed_ms_since(start);
    ::freeaddrinfo(candidates);
    return result;
  }

  // --- connect ------------------------------------------------------------
  const int timeout_ms = options.timeout_ms > 0 ? options.timeout_ms : 2000;
  result.outcome = Outcome::Timeout;

  for (addrinfo* candidate = candidates; candidate != nullptr;
       candidate = candidate->ai_next) {
    Socket socket(::socket(candidate->ai_family,
                           candidate->ai_socktype, candidate->ai_protocol));
    if (!socket.valid()) {
      result.outcome = Outcome::Error;
      result.detail = errno_text(errno);
      continue;
    }

    const int flags = ::fcntl(socket.get(), F_GETFL, 0);
    if (flags < 0 || ::fcntl(socket.get(), F_SETFL, flags | O_NONBLOCK) < 0) {
      result.outcome = Outcome::Error;
      result.detail = errno_text(errno);
      continue;
    }

    const int connected =
        ::connect(socket.get(), candidate->ai_addr, candidate->ai_addrlen);
    if (connected == 0) {
      result.outcome = Outcome::Open;
      break;
    }
    if (errno == ECONNREFUSED) {
      // The host answered: it is up, sshd simply is not listening yet.
      result.outcome = Outcome::Refused;
      break;
    }
    if (errno != EINPROGRESS) {
      result.outcome = Outcome::Error;
      result.detail = errno_text(errno);
      continue;
    }

    pollfd descriptor{};
    descriptor.fd = socket.get();
    descriptor.events = POLLOUT;
    const int ready = ::poll(&descriptor, 1, timeout_ms);
    if (ready == 0) {
      result.outcome = Outcome::Timeout;
      continue;
    }
    if (ready < 0) {
      result.outcome = Outcome::Error;
      result.detail = errno_text(errno);
      continue;
    }

    // poll() reporting writability does not mean the connection succeeded;
    // SO_ERROR is the only thing that distinguishes "connected" from
    // "refused after the SYN went out".
    int socket_error = 0;
    socklen_t length = sizeof(socket_error);
    if (::getsockopt(socket.get(), SOL_SOCKET, SO_ERROR,
                     &socket_error, &length) < 0) {
      result.outcome = Outcome::Error;
      result.detail = errno_text(errno);
      continue;
    }
    if (socket_error == 0) {
      result.outcome = Outcome::Open;
      break;
    }
    if (socket_error == ECONNREFUSED) {
      result.outcome = Outcome::Refused;
      break;
    }
    if (socket_error == ETIMEDOUT) {
      result.outcome = Outcome::Timeout;
      continue;
    }
    result.outcome = Outcome::Error;
    result.detail = errno_text(socket_error);
  }

  ::freeaddrinfo(candidates);
  result.elapsed_ms = elapsed_ms_since(start);
  return result;
}

std::vector<Result> probe_many(const std::vector<std::string>& hosts,
                               const Options& options) {
  // Sized up front and written by index, so results stay in input order and no
  // synchronisation is needed between the workers.
  std::vector<Result> results(hosts.size());
  const auto count = static_cast<long>(hosts.size());

#ifdef _OPENMP
  if (options.threads > 0) {
    omp_set_num_threads(options.threads);
  }
  // Dynamic scheduling: probe durations are wildly uneven -- a host that
  // answers costs a millisecond, one that is down costs the whole timeout --
  // so a static split would leave most threads idle behind one slow chunk.
  #pragma omp parallel for schedule(dynamic, 1)
#endif
  for (long index = 0; index < count; ++index) {
    results[static_cast<std::size_t>(index)] =
        probe_one(hosts[static_cast<std::size_t>(index)], options);
  }
  return results;
}

std::vector<std::string> reachable_hosts(const std::vector<Result>& results) {
  std::vector<std::string> hosts;
  hosts.reserve(results.size());
  for (const auto& result : results) {
    if (result.reachable()) {
      hosts.push_back(result.host);
    }
  }
  return hosts;
}

const char* version() { return kVersion; }

bool has_openmp() {
#ifdef _OPENMP
  return true;
#else
  return false;
#endif
}

}  // namespace mu2eprobe

// ---------------------------------------------------------------------------
// C API
// ---------------------------------------------------------------------------

namespace {

void copy_into(char* destination, std::size_t size, const std::string& source) {
  const std::size_t length = source.size() < size - 1 ? source.size() : size - 1;
  std::memcpy(destination, source.data(), length);
  destination[length] = '\0';
}

void fill(mu2e_probe_result_t* out, const mu2eprobe::Result& result) {
  if (out == nullptr) return;
  copy_into(out->host, sizeof(out->host), result.host);
  copy_into(out->address, sizeof(out->address), result.address);
  copy_into(out->detail, sizeof(out->detail), result.detail);
  out->port = result.port;
  out->outcome = static_cast<int>(result.outcome);
  out->elapsed_ms = result.elapsed_ms;
}

mu2eprobe::Options from_c(const mu2e_probe_options_t* options) {
  mu2eprobe::Options out;
  if (options != nullptr) {
    out.port = options->port ? options->port : 22;
    out.timeout_ms = options->timeout_ms > 0 ? options->timeout_ms : 2000;
    out.threads = options->threads;
    out.resolve_only = options->resolve_only != 0;
  }
  return out;
}

}  // namespace

extern "C" {

void mu2e_probe_default_options(mu2e_probe_options_t* options) {
  if (options == nullptr) return;
  options->port = 22;
  options->timeout_ms = 2000;
  options->threads = 0;
  options->resolve_only = 0;
}

int mu2e_probe_one(const char* host, const mu2e_probe_options_t* options,
                   mu2e_probe_result_t* out) {
  if (host == nullptr) return MU2E_PROBE_ERROR;
  const auto result = mu2eprobe::probe_one(host, from_c(options));
  fill(out, result);
  return static_cast<int>(result.outcome);
}

int mu2e_probe_many(const char* const* hosts, size_t count,
                    const mu2e_probe_options_t* options,
                    mu2e_probe_result_t* results) {
  if (hosts == nullptr || results == nullptr) return -1;
  std::vector<std::string> names;
  names.reserve(count);
  for (size_t index = 0; index < count; ++index) {
    names.emplace_back(hosts[index] != nullptr ? hosts[index] : "");
  }
  const auto probed = mu2eprobe::probe_many(names, from_c(options));
  int reachable = 0;
  for (size_t index = 0; index < probed.size(); ++index) {
    fill(&results[index], probed[index]);
    if (probed[index].reachable()) ++reachable;
  }
  return reachable;
}

const char* mu2e_probe_outcome_name(int outcome) {
  switch (outcome) {
    case MU2E_PROBE_OPEN:       return "open";
    case MU2E_PROBE_REFUSED:    return "refused";
    case MU2E_PROBE_TIMEOUT:    return "timeout";
    case MU2E_PROBE_UNRESOLVED: return "unresolved";
    default:                    return "error";
  }
}

const char* mu2e_probe_version(void) { return mu2eprobe::version(); }

int mu2e_probe_has_openmp(void) { return mu2eprobe::has_openmp() ? 1 : 0; }

}  // extern "C"
