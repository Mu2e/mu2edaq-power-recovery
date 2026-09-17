// pybind11 bindings for mu2eprobe.
//
// The Python surface deliberately mirrors what checks/reachability.py needs and
// nothing more: a sweep in, a list of results out.  Everything richer belongs
// in Python, where it can be changed without a rebuild on a DAQ node.
//
// The GIL is released for the duration of a sweep -- that is the entire point
// of the extension, and holding it would make the OpenMP parallelism useless
// to the caller.

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include "mu2eprobe/probe.hpp"

namespace py = pybind11;

PYBIND11_MODULE(mu2eprobe, module) {
  module.doc() =
      "Parallel TCP reachability probing for the Mu2e DAQ power-recovery "
      "tools. Optional: mu2edaq_power_recovery falls back to a pure-Python "
      "implementation when this extension is not built.";

  py::enum_<mu2eprobe::Outcome>(module, "Outcome")
      .value("OPEN", mu2eprobe::Outcome::Open)
      .value("REFUSED", mu2eprobe::Outcome::Refused)
      .value("TIMEOUT", mu2eprobe::Outcome::Timeout)
      .value("UNRESOLVED", mu2eprobe::Outcome::Unresolved)
      .value("ERROR", mu2eprobe::Outcome::Error);

  py::class_<mu2eprobe::Result>(module, "Result")
      .def_readonly("host", &mu2eprobe::Result::host)
      .def_readonly("address", &mu2eprobe::Result::address)
      .def_readonly("port", &mu2eprobe::Result::port)
      .def_readonly("outcome", &mu2eprobe::Result::outcome)
      .def_readonly("elapsed_ms", &mu2eprobe::Result::elapsed_ms)
      .def_readonly("detail", &mu2eprobe::Result::detail)
      .def_property_readonly("reachable", &mu2eprobe::Result::reachable,
                             "True when the host answered -- including a "
                             "refused connection, which still proves it is up.")
      .def_property_readonly(
          "outcome_name",
          [](const mu2eprobe::Result& self) { return self.outcome_name(); })
      .def("__repr__", [](const mu2eprobe::Result& self) {
        return "<Result " + self.host + " " + self.outcome_name() + ">";
      });

  py::class_<mu2eprobe::Options>(module, "Options")
      .def(py::init<>())
      .def_readwrite("port", &mu2eprobe::Options::port)
      .def_readwrite("timeout_ms", &mu2eprobe::Options::timeout_ms)
      .def_readwrite("threads", &mu2eprobe::Options::threads)
      .def_readwrite("resolve_only", &mu2eprobe::Options::resolve_only);

  module.def(
      "probe_one",
      [](const std::string& host, std::uint16_t port, int timeout_ms) {
        mu2eprobe::Options options;
        options.port = port;
        options.timeout_ms = timeout_ms;
        py::gil_scoped_release release;
        return mu2eprobe::probe_one(host, options);
      },
      py::arg("host"), py::arg("port") = 22, py::arg("timeout_ms") = 2000,
      "Probe one host and return a Result.");

  module.def(
      "probe_many",
      [](const std::vector<std::string>& hosts, std::uint16_t port,
         int timeout_ms, int threads) {
        mu2eprobe::Options options;
        options.port = port;
        options.timeout_ms = timeout_ms;
        options.threads = threads;
        py::gil_scoped_release release;
        return mu2eprobe::probe_many(hosts, options);
      },
      py::arg("hosts"), py::arg("port") = 22, py::arg("timeout_ms") = 2000,
      py::arg("threads") = 0,
      "Probe many hosts in parallel; results come back in input order.");

  module.def("reachable_hosts", &mu2eprobe::reachable_hosts,
             py::arg("results"),
             "The hosts from a result list that answered, in input order.");

  module.def("has_openmp", &mu2eprobe::has_openmp,
             "True when the extension was compiled with OpenMP.");

  module.attr("__version__") = mu2eprobe::version();
}
