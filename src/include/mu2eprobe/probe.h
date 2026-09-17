/* mu2eprobe -- C API.
 *
 * A thin C wrapper over the C++ implementation, so the sweep is callable from
 * C, from ctypes, and from anything else that speaks the C ABI -- the project
 * convention is that a library has both a C/C++ interface and Python bindings,
 * and a C++-only library would not satisfy the first half of that.
 *
 * All functions are safe to call from multiple threads; mu2e_probe_many runs
 * its own parallelism internally.
 */

#ifndef MU2EPROBE_PROBE_H
#define MU2EPROBE_PROBE_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Outcome codes, matching mu2eprobe::Outcome. */
#define MU2E_PROBE_OPEN        0
#define MU2E_PROBE_REFUSED     1
#define MU2E_PROBE_TIMEOUT     2
#define MU2E_PROBE_UNRESOLVED  3
#define MU2E_PROBE_ERROR       4

/* One probe result.  'address' and 'detail' are NUL-terminated and owned by
 * the struct; they are fixed-size buffers so the caller never frees anything. */
typedef struct {
  char host[256];
  char address[64];
  uint16_t port;
  int outcome;
  double elapsed_ms;
  char detail[128];
} mu2e_probe_result_t;

typedef struct {
  uint16_t port;      /* TCP port; 0 selects 22               */
  int timeout_ms;     /* per-host budget; 0 selects 2000      */
  int threads;        /* 0 = OpenMP default                   */
  int resolve_only;   /* non-zero: stop after name resolution */
} mu2e_probe_options_t;

/* Fill *options with the defaults. */
void mu2e_probe_default_options(mu2e_probe_options_t* options);

/* Probe one host.  Returns the outcome code and fills *out (may be NULL). */
int mu2e_probe_one(const char* host, const mu2e_probe_options_t* options,
                   mu2e_probe_result_t* out);

/* Probe 'count' hosts into the caller's 'results' array, which must hold at
 * least 'count' entries.  Returns the number of hosts that answered, or -1 on
 * a bad argument. */
int mu2e_probe_many(const char* const* hosts, size_t count,
                    const mu2e_probe_options_t* options,
                    mu2e_probe_result_t* results);

/* Human-readable name for an outcome code. */
const char* mu2e_probe_outcome_name(int outcome);

/* Library version string, e.g. "0.1.0". */
const char* mu2e_probe_version(void);

/* Non-zero when compiled with OpenMP. */
int mu2e_probe_has_openmp(void);

#ifdef __cplusplus
}  /* extern "C" */
#endif

#endif /* MU2EPROBE_PROBE_H */
