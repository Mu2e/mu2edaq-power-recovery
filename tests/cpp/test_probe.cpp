// Unit tests for libmu2eprobe.
//
// Built against CppUnit when it is available (HAVE_CPPUNIT) and against a tiny
// built-in runner otherwise, so the tests run on a bare DAQ node with no test
// framework installed.  The assertions are identical either way.
//
// The tests deliberately avoid depending on anything outside the machine: they
// probe loopback (which always answers or refuses) and a reserved TEST-NET-1
// address from RFC 5737 (which never answers), so they neither need the DAQ
// network nor produce traffic on it.

#include "mu2eprobe/probe.hpp"
#include "mu2eprobe/probe.h"

#include <chrono>
#include <cstring>
#include <iostream>
#include <string>
#include <vector>

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------
#ifdef HAVE_CPPUNIT
#include <cppunit/TestAssert.h>
#include <cppunit/TestFixture.h>
#include <cppunit/extensions/HelperMacros.h>
#include <cppunit/ui/text/TestRunner.h>
#define CHECK_TRUE(message, condition) CPPUNIT_ASSERT_MESSAGE(message, condition)
#define CHECK_EQUAL(message, expected, actual) \
  CPPUNIT_ASSERT_EQUAL_MESSAGE(message, expected, actual)
#else
namespace {
int g_failures = 0;
void check_true(const char* message, bool condition) {
  if (!condition) {
    std::cerr << "FAIL: " << message << "\n";
    ++g_failures;
  }
}
template <typename T>
void check_equal(const char* message, const T& expected, const T& actual) {
  if (!(expected == actual)) {
    std::cerr << "FAIL: " << message << " (expected " << expected
              << ", got " << actual << ")\n";
    ++g_failures;
  }
}
}  // namespace
#define CHECK_TRUE(message, condition) check_true(message, (condition))
#define CHECK_EQUAL(message, expected, actual) \
  check_equal(message, (expected), (actual))
#endif

namespace {

// RFC 5737 TEST-NET-1: guaranteed never to be routed to a live host, so this
// address exercises the timeout path without depending on the site's network.
constexpr const char* kUnroutable = "192.0.2.1";

void test_version_and_openmp() {
  CHECK_TRUE("version string is non-empty",
             std::strlen(mu2eprobe::version()) > 0);
  CHECK_TRUE("C and C++ report the same version",
             std::string(mu2eprobe::version()) == mu2e_probe_version());
  // has_openmp is a build property; either answer is correct, but the C and
  // C++ views of it must agree.
  CHECK_EQUAL("C and C++ agree on OpenMP",
              mu2eprobe::has_openmp() ? 1 : 0, mu2e_probe_has_openmp());
}

void test_unresolvable_host() {
  // .invalid is reserved by RFC 2606 and must never resolve.
  const auto result = mu2eprobe::probe_one("no-such-host.invalid");
  CHECK_TRUE("an unresolvable name is not reachable", !result.reachable());
  CHECK_TRUE("an unresolvable name reports Unresolved",
             result.outcome == mu2eprobe::Outcome::Unresolved);
  CHECK_TRUE("the failing host name is echoed back",
             result.host == "no-such-host.invalid");
}

void test_loopback_answers() {
  // Nothing may be listening on this port, but loopback always *answers* --
  // either with a connection or with a refusal -- and both count as reachable.
  mu2eprobe::Options options;
  options.port = 9;            // discard: closed on a normal host
  options.timeout_ms = 1000;
  const auto result = mu2eprobe::probe_one("127.0.0.1", options);
  CHECK_TRUE("loopback answers (open or refused)", result.reachable());
  CHECK_TRUE("the resolved address is recorded", !result.address.empty());
}

void test_timeout_is_bounded() {
  mu2eprobe::Options options;
  options.timeout_ms = 300;
  const auto start = std::chrono::steady_clock::now();
  const auto result = mu2eprobe::probe_one(kUnroutable, options);
  const double elapsed = std::chrono::duration<double, std::milli>(
      std::chrono::steady_clock::now() - start).count();

  CHECK_TRUE("an unroutable address is not reachable", !result.reachable());
  // The budget is per address family and the OS adds its own scheduling
  // latency, so allow generous slack; the point is that it returns at all
  // rather than blocking for the kernel's default SYN retry window.
  CHECK_TRUE("the probe honours its timeout budget", elapsed < 5000.0);
}

void test_order_is_preserved() {
  // The sweep is parallel and completion order is arbitrary; results must
  // still come back in input order, because the caller's node list is ordered
  // meaningfully and re-sorting it would be its problem otherwise.
  const std::vector<std::string> hosts = {
      "127.0.0.1", "no-such-host.invalid", "localhost", kUnroutable};
  mu2eprobe::Options options;
  options.timeout_ms = 300;
  options.port = 9;
  const auto results = mu2eprobe::probe_many(hosts, options);

  CHECK_EQUAL("one result per host", hosts.size(), results.size());
  for (std::size_t index = 0; index < hosts.size(); ++index) {
    CHECK_TRUE("result order matches input order",
               results[index].host == hosts[index]);
  }
  CHECK_TRUE("the unresolvable entry is still Unresolved",
             results[1].outcome == mu2eprobe::Outcome::Unresolved);
}

void test_reachable_hosts_filter() {
  std::vector<mu2eprobe::Result> results(3);
  results[0].host = "a"; results[0].outcome = mu2eprobe::Outcome::Open;
  results[1].host = "b"; results[1].outcome = mu2eprobe::Outcome::Timeout;
  results[2].host = "c"; results[2].outcome = mu2eprobe::Outcome::Refused;

  const auto hosts = mu2eprobe::reachable_hosts(results);
  CHECK_EQUAL("only the answering hosts are returned",
              static_cast<std::size_t>(2), hosts.size());
  CHECK_TRUE("open hosts are included", hosts[0] == "a");
  CHECK_TRUE("refused hosts count as reachable", hosts[1] == "c");
}

void test_c_api() {
  mu2e_probe_options_t options;
  mu2e_probe_default_options(&options);
  CHECK_EQUAL("default port is 22", static_cast<int>(22),
              static_cast<int>(options.port));

  options.timeout_ms = 300;
  options.port = 9;

  mu2e_probe_result_t single;
  const int outcome = mu2e_probe_one("127.0.0.1", &options, &single);
  CHECK_TRUE("the C API reports an answer from loopback",
             outcome == MU2E_PROBE_OPEN || outcome == MU2E_PROBE_REFUSED);
  CHECK_TRUE("the C API fills in the host", std::string(single.host) == "127.0.0.1");

  const char* hosts[2] = {"127.0.0.1", kUnroutable};
  mu2e_probe_result_t results[2];
  const int reachable = mu2e_probe_many(hosts, 2, &options, results);
  CHECK_EQUAL("exactly one of the two answered", 1, reachable);

  CHECK_TRUE("outcome names are stable",
             std::string(mu2e_probe_outcome_name(MU2E_PROBE_TIMEOUT)) == "timeout");
  CHECK_EQUAL("a null host list is rejected", -1,
              mu2e_probe_many(nullptr, 2, &options, results));
}

void run_all() {
  test_version_and_openmp();
  test_unresolvable_host();
  test_loopback_answers();
  test_timeout_is_bounded();
  test_order_is_preserved();
  test_reachable_hosts_filter();
  test_c_api();
}

}  // namespace

#ifdef HAVE_CPPUNIT
class ProbeTest : public CppUnit::TestFixture {
 public:
  CPPUNIT_TEST_SUITE(ProbeTest);
  CPPUNIT_TEST(testVersion);
  CPPUNIT_TEST(testUnresolvable);
  CPPUNIT_TEST(testLoopback);
  CPPUNIT_TEST(testTimeout);
  CPPUNIT_TEST(testOrder);
  CPPUNIT_TEST(testFilter);
  CPPUNIT_TEST(testCApi);
  CPPUNIT_TEST_SUITE_END();

  void testVersion() { test_version_and_openmp(); }
  void testUnresolvable() { test_unresolvable_host(); }
  void testLoopback() { test_loopback_answers(); }
  void testTimeout() { test_timeout_is_bounded(); }
  void testOrder() { test_order_is_preserved(); }
  void testFilter() { test_reachable_hosts_filter(); }
  void testCApi() { test_c_api(); }
};
CPPUNIT_TEST_SUITE_REGISTRATION(ProbeTest);

int main() {
  CppUnit::TextUi::TestRunner runner;
  runner.addTest(CppUnit::TestFactoryRegistry::getRegistry().makeTest());
  return runner.run() ? 0 : 1;
}
#else
int main() {
  std::cout << "mu2eprobe tests (built-in runner)\n";
  run_all();
  if (g_failures == 0) {
    std::cout << "all checks passed\n";
    return 0;
  }
  std::cerr << g_failures << " check(s) failed\n";
  return 1;
}
#endif
