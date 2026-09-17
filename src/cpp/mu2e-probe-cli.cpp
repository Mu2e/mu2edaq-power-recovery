// mu2e-probe -- a stand-alone sweep, so the C++ library is usable and testable
// without Python.  Reads hosts from the command line or from stdin.
//
//   mu2e-probe mu2e-trk-01 mu2e-trk-02
//   mu2e-node-inventory --hostnames | mu2e-probe --timeout 1000
//   mu2e-probe --port 623 --quiet mu2e-trk-01-ipmi   # a BMC's RMCP port

#include "mu2eprobe/probe.hpp"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <string>
#include <vector>

namespace {

void usage(const char* program) {
  std::printf(
      "usage: %s [options] [host ...]\n"
      "\n"
      "Probe TCP reachability of DAQ hosts in parallel. With no host\n"
      "arguments, hosts are read from standard input, one per line.\n"
      "\n"
      "options\n"
      "  -p, --port PORT       TCP port to probe (default 22)\n"
      "  -t, --timeout MS      per-host connect budget in ms (default 2000)\n"
      "  -j, --threads N       worker threads (default: hardware threads)\n"
      "  -r, --resolve-only    resolve names, do not connect\n"
      "  -u, --up-only         print only hosts that answered\n"
      "  -q, --quiet           print nothing; use the exit status\n"
      "      --version         print the library version and exit\n"
      "  -h, --help            this text\n"
      "\n"
      "exit status\n"
      "  0  every host answered\n"
      "  1  at least one host did not answer\n"
      "  2  usage error\n",
      program);
}

}  // namespace

int main(int argc, char** argv) {
  mu2eprobe::Options options;
  std::vector<std::string> hosts;
  bool up_only = false;
  bool quiet = false;

  for (int index = 1; index < argc; ++index) {
    const std::string argument = argv[index];
    auto needs_value = [&](const char* name) -> const char* {
      if (index + 1 >= argc) {
        std::fprintf(stderr, "error: %s needs a value\n", name);
        std::exit(2);
      }
      return argv[++index];
    };

    if (argument == "-h" || argument == "--help") {
      usage(argv[0]);
      return 0;
    } else if (argument == "--version") {
      std::printf("mu2eprobe %s (OpenMP: %s)\n", mu2eprobe::version(),
                  mu2eprobe::has_openmp() ? "yes" : "no");
      return 0;
    } else if (argument == "-p" || argument == "--port") {
      options.port = static_cast<std::uint16_t>(std::atoi(needs_value("--port")));
    } else if (argument == "-t" || argument == "--timeout") {
      options.timeout_ms = std::atoi(needs_value("--timeout"));
    } else if (argument == "-j" || argument == "--threads") {
      options.threads = std::atoi(needs_value("--threads"));
    } else if (argument == "-r" || argument == "--resolve-only") {
      options.resolve_only = true;
    } else if (argument == "-u" || argument == "--up-only") {
      up_only = true;
    } else if (argument == "-q" || argument == "--quiet") {
      quiet = true;
    } else if (!argument.empty() && argument[0] == '-') {
      std::fprintf(stderr, "error: unknown option %s\n", argument.c_str());
      usage(argv[0]);
      return 2;
    } else {
      hosts.push_back(argument);
    }
  }

  if (hosts.empty()) {
    std::string line;
    while (std::getline(std::cin, line)) {
      // Tolerate whitespace and blank lines so a piped node list needs no
      // cleaning up first.
      const auto begin = line.find_first_not_of(" \t\r\n");
      if (begin == std::string::npos) continue;
      const auto end = line.find_last_not_of(" \t\r\n");
      hosts.push_back(line.substr(begin, end - begin + 1));
    }
  }
  if (hosts.empty()) {
    std::fprintf(stderr, "error: no hosts given\n");
    usage(argv[0]);
    return 2;
  }

  const auto results = mu2eprobe::probe_many(hosts, options);
  int down = 0;
  for (const auto& result : results) {
    if (!result.reachable()) ++down;
    if (quiet) continue;
    if (up_only && !result.reachable()) continue;
    std::printf("%-34s %-12s %-16s %7.1f ms%s%s\n",
                result.host.c_str(), result.outcome_name(),
                result.address.empty() ? "-" : result.address.c_str(),
                result.elapsed_ms,
                result.detail.empty() ? "" : "  ",
                result.detail.c_str());
  }
  if (!quiet) {
    std::printf("\n%zu/%zu host(s) answered\n", results.size() - down,
                results.size());
  }
  return down > 0 ? 1 : 0;
}
