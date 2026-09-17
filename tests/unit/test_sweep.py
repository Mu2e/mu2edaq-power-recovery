"""The reachability sweep and its two interchangeable backends."""
from __future__ import annotations

import pytest

from mu2edaq_power_recovery import sweep as S


def test_backend_is_reported():
    # Either backend is valid; which one is in use goes into the run banner so
    # a timing difference between runs has an explanation.
    assert S.backend() in ("libmu2eprobe", "python")


def test_an_empty_host_list_is_not_an_error():
    assert S.sweep([]) == []


def test_loopback_answers():
    # Port 9 (discard) is normally closed, so this exercises the refusal path:
    # a refused connection still proves the host is up.
    results = S.sweep(["127.0.0.1"], port=9, timeout_ms=1000)
    assert len(results) == 1
    assert results[0].reachable
    assert results[0].outcome in ("open", "refused")


def test_an_unresolvable_name_is_not_reachable():
    # .invalid is reserved by RFC 2606 and never resolves.
    result = S.sweep(["no-such-host.invalid"], timeout_ms=500)[0]
    assert not result.reachable
    assert result.outcome == "unresolved"


def test_an_unroutable_address_times_out_within_budget():
    # RFC 5737 TEST-NET-1: never routed to a live host.
    result = S.sweep(["192.0.2.1"], port=22, timeout_ms=400)[0]
    assert not result.reachable
    assert result.outcome in ("timeout", "error")
    assert result.elapsed_ms < 5000


def test_results_come_back_in_input_order():
    hosts = ["127.0.0.1", "no-such-host.invalid", "localhost"]
    results = S.sweep(hosts, port=9, timeout_ms=500)
    assert [r.host for r in results] == hosts


def test_reachable_returns_only_the_answering_hosts():
    hosts = ["127.0.0.1", "no-such-host.invalid"]
    assert S.reachable(hosts, port=9, timeout_ms=500) == ["127.0.0.1"]


def test_the_python_fallback_matches_the_native_semantics():
    # Whichever backend is active, a refusal counts as reachable and the
    # outcome vocabulary is the same -- otherwise a report generated on a
    # workstation with the extension would disagree with one generated without.
    result = S._probe_python("127.0.0.1", port=9, timeout_ms=1000)
    assert result.reachable
    assert result.outcome in ("open", "refused")
    assert set(result.as_dict()) == {"host", "reachable", "outcome", "address",
                                     "elapsed_ms", "detail"}
