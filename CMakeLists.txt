# mu2edaq-power-recovery
#
# Two things are built here:
#
#   * libmu2eprobe  -- the optional C/C++ parallel reachability sweep, its C
#                      ABI, a stand-alone `mu2e-probe` command, CppUnit tests,
#                      and (when pybind11 is available) the Python extension.
#   * the Python venv -- bootstrapped, tested with pytest, and installed along
#                      with the man pages and config, so `cmake --build` does
#                      the same thing on Linux, macOS and Windows.
#
#   cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
#   cmake --build build
#   ctest --test-dir build --output-on-failure
#   cmake --install build --prefix /usr/local
#
# Nothing in the recovery tools requires the C++ half: if a compiler, OpenMP or
# pybind11 is missing, the build still produces a working installation and the
# Python fallback is used at runtime.  That is deliberate -- a recovery must not
# depend on a toolchain being present on the machine driving it.

cmake_minimum_required(VERSION 3.16)
project(mu2edaq-power-recovery VERSION 0.1.0 LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_C_STANDARD 11)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "build type" FORCE)
endif()

option(BUILD_PROBE      "Build the C/C++ libmu2eprobe library"        ON)
option(BUILD_BINDINGS   "Build the pybind11 Python extension"         ON)
option(BUILD_TESTING    "Build the test suites"                       ON)
option(BOOTSTRAP_VENV   "Create the Python venv during the build"     ON)

include(GNUInstallDirs)
enable_testing()

# ---------------------------------------------------------------------------
# libmu2eprobe
# ---------------------------------------------------------------------------
if(BUILD_PROBE)
  add_library(mu2eprobe src/cpp/probe.cpp)
  target_include_directories(mu2eprobe PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/src/include>
    $<INSTALL_INTERFACE:${CMAKE_INSTALL_INCLUDEDIR}>)
  set_target_properties(mu2eprobe PROPERTIES
    VERSION ${PROJECT_VERSION} SOVERSION ${PROJECT_VERSION_MAJOR})

  # OpenMP is what makes the sweep worth having in C++ at all, but the library
  # is correct without it -- the parallel-for simply runs serially.
  find_package(OpenMP QUIET)
  if(OpenMP_CXX_FOUND)
    target_link_libraries(mu2eprobe PUBLIC OpenMP::OpenMP_CXX)
    message(STATUS "mu2eprobe: OpenMP enabled")
  else()
    message(STATUS "mu2eprobe: OpenMP not found -- sweeps will run serially")
  endif()

  if(WIN32)
    # The probe uses POSIX sockets; on Windows that means winsock2 plus the
    # compatibility shims the MSYS2/MinGW toolchain provides.
    target_link_libraries(mu2eprobe PUBLIC ws2_32)
  endif()

  add_executable(mu2e-probe src/cpp/mu2e-probe-cli.cpp)
  target_link_libraries(mu2e-probe PRIVATE mu2eprobe)

  install(TARGETS mu2eprobe mu2e-probe
          EXPORT mu2eprobe-targets
          RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR}
          LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}
          ARCHIVE DESTINATION ${CMAKE_INSTALL_LIBDIR})
  install(DIRECTORY src/include/ DESTINATION ${CMAKE_INSTALL_INCLUDEDIR})
endif()

# ---------------------------------------------------------------------------
# C++ / C unit tests (CppUnit where available, a built-in runner otherwise)
# ---------------------------------------------------------------------------
if(BUILD_PROBE AND BUILD_TESTING)
  find_package(PkgConfig QUIET)
  if(PkgConfig_FOUND)
    pkg_check_modules(CPPUNIT QUIET cppunit)
  endif()

  add_executable(mu2eprobe-tests tests/cpp/test_probe.cpp)
  target_link_libraries(mu2eprobe-tests PRIVATE mu2eprobe)
  if(CPPUNIT_FOUND)
    # The test file compiles either way; CPPUNIT selects the richer harness.
    target_compile_definitions(mu2eprobe-tests PRIVATE HAVE_CPPUNIT=1)
    target_include_directories(mu2eprobe-tests PRIVATE ${CPPUNIT_INCLUDE_DIRS})
    # _LINK_LIBRARIES carries absolute paths; _LIBRARIES carries bare names
    # that the linker cannot find unless _LIBRARY_DIRS is also on the search
    # path (Homebrew and /usr/local installs both hit this).
    if(CPPUNIT_LINK_LIBRARIES)
      target_link_libraries(mu2eprobe-tests PRIVATE ${CPPUNIT_LINK_LIBRARIES})
    else()
      target_link_directories(mu2eprobe-tests PRIVATE ${CPPUNIT_LIBRARY_DIRS})
      target_link_libraries(mu2eprobe-tests PRIVATE ${CPPUNIT_LIBRARIES})
    endif()
    message(STATUS "mu2eprobe: building tests with CppUnit")
  else()
    message(STATUS "mu2eprobe: CppUnit not found -- using the built-in runner")
  endif()
  add_test(NAME mu2eprobe-unit COMMAND mu2eprobe-tests)
endif()

# ---------------------------------------------------------------------------
# Python extension
# ---------------------------------------------------------------------------
if(BUILD_PROBE AND BUILD_BINDINGS)
  find_package(Python3 3.9 COMPONENTS Interpreter Development.Module QUIET)
  find_package(pybind11 CONFIG QUIET)
  if(NOT pybind11_FOUND AND Python3_Interpreter_FOUND)
    # pybind11 ships its own CMake config inside the wheel; ask the interpreter
    # where it is rather than requiring a system package.
    execute_process(COMMAND ${Python3_EXECUTABLE} -m pybind11 --cmakedir
                    OUTPUT_VARIABLE PYBIND11_CMAKE_DIR
                    OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET)
    if(PYBIND11_CMAKE_DIR)
      find_package(pybind11 CONFIG QUIET PATHS "${PYBIND11_CMAKE_DIR}")
    endif()
  endif()

  if(pybind11_FOUND)
    pybind11_add_module(mu2eprobe_python python/bindings.cpp)
    set_target_properties(mu2eprobe_python PROPERTIES OUTPUT_NAME mu2eprobe)
    target_link_libraries(mu2eprobe_python PRIVATE mu2eprobe)
    install(TARGETS mu2eprobe_python
            LIBRARY DESTINATION ${Python3_SITEARCH})
    message(STATUS "mu2eprobe: building the Python extension")
  else()
    message(STATUS "mu2eprobe: pybind11 not found -- skipping the Python "
                   "extension; the pure-Python fallback will be used")
  endif()
endif()

# ---------------------------------------------------------------------------
# Python application: venv, pytest, installation
# ---------------------------------------------------------------------------
find_package(Python3 3.9 COMPONENTS Interpreter QUIET)
if(Python3_Interpreter_FOUND)
  set(VENV_DIR "${CMAKE_CURRENT_SOURCE_DIR}/venv")
  if(WIN32)
    set(VENV_PY "${VENV_DIR}/Scripts/python.exe")
    set(BOOTSTRAP powershell -ExecutionPolicy Bypass -File
                  "${CMAKE_CURRENT_SOURCE_DIR}/bootstrap.ps1")
  else()
    set(VENV_PY "${VENV_DIR}/bin/python")
    set(BOOTSTRAP "${CMAKE_CURRENT_SOURCE_DIR}/bootstrap.sh")
  endif()

  if(BOOTSTRAP_VENV)
    add_custom_command(
      OUTPUT "${VENV_PY}"
      COMMAND ${CMAKE_COMMAND} -E env MU2EDAQ_PYTHON=${Python3_EXECUTABLE} ${BOOTSTRAP}
      WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}"
      COMMENT "Bootstrapping the mu2edaq-power-recovery virtual environment")
    add_custom_target(venv ALL DEPENDS "${VENV_PY}")
    set(VENV_TARGET venv)
  else()
    set(VENV_TARGET "")
  endif()

  # The Python targets and tests need ${VENV_PY} to *exist*; they do not need
  # this build to have created it.  Registering them only under BOOTSTRAP_VENV
  # was wrong in exactly the case that matters: bootstrap.sh and bootstrap.ps1
  # make the venv themselves and then configure this tree with
  # -DBOOTSTRAP_VENV=OFF, so the only build tree an operator actually creates
  # was the one with no 'docs' target and no docs-check -- the drift check was
  # absent from the tree it was written to guard.  Register them whenever the
  # interpreter will be there: either this build makes it, or it is on disk
  # already.  A tree configured with OFF against no venv still skips them,
  # rather than registering tests that cannot run.
  if(BOOTSTRAP_VENV OR EXISTS "${VENV_PY}")
    add_custom_target(pytest
      COMMAND "${VENV_PY}" -m pytest -q
      WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}"
      DEPENDS ${VENV_TARGET}
      COMMENT "Running the pytest suite")

    # The check reference in the section-7 page is generated from the live
    # check registry.  `cmake --build build --target docs` rewrites it; the
    # docs-check test below fails the suite if the committed page has drifted
    # away from the code, which is what happened when the table was produced
    # by a throwaway script nobody kept.
    add_custom_target(docs
      COMMAND "${VENV_PY}" "${CMAKE_CURRENT_SOURCE_DIR}/tools/generate-docs.py"
              --write
      WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}"
      DEPENDS ${VENV_TARGET}
      COMMENT "Regenerating the generated man page sections")

    if(BUILD_TESTING)
      add_test(NAME pytest COMMAND "${VENV_PY}" -m pytest -q
               WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}")
      # Documentation drift: the generated section against the registry, a man
      # page for every console script, a man5 entry for every configuration
      # key, and no groff warnings anywhere.
      add_test(NAME docs-check
               COMMAND "${VENV_PY}"
                       "${CMAKE_CURRENT_SOURCE_DIR}/tools/generate-docs.py" --check
               WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}")
      # A simulated end-to-end run: proves the four phases, the check bodies and
      # the report generator all still work together, with no cluster attached.
      add_test(NAME simulated-run
               COMMAND "${VENV_PY}" -m mu2edaq_power_recovery
                       --phase all --simulate --no-self-update -q
                       --output-dir "${CMAKE_CURRENT_BINARY_DIR}/report-test"
               WORKING_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}")
    endif()
  else()
    message(STATUS "mu2edaq-power-recovery: no venv at ${VENV_PY} and "
                   "BOOTSTRAP_VENV=OFF -- skipping the docs/pytest targets and "
                   "the Python ctest entries. Run ./bootstrap.sh and "
                   "re-configure to get them.")
  endif()
endif()

# ---------------------------------------------------------------------------
# Installation of the non-compiled parts
# ---------------------------------------------------------------------------
install(DIRECTORY man/ DESTINATION ${CMAKE_INSTALL_MANDIR}
        FILES_MATCHING PATTERN "*.[1-8]")
install(DIRECTORY config/ DESTINATION share/mu2edaq-power-recovery/config
        FILES_MATCHING PATTERN "*.yaml" PATTERN "*.example")
install(PROGRAMS
          bootstrap.sh
          start-mu2edaq-power-recovery.sh
          stop-mu2edaq-power-recovery.sh
        DESTINATION share/mu2edaq-power-recovery)

message(STATUS "")
message(STATUS "mu2edaq-power-recovery ${PROJECT_VERSION}")
message(STATUS "  C/C++ probe library : ${BUILD_PROBE}")
message(STATUS "  Python extension    : ${BUILD_BINDINGS}")
message(STATUS "  Python venv         : ${BOOTSTRAP_VENV}")
message(STATUS "  build type          : ${CMAKE_BUILD_TYPE}")
message(STATUS "")
