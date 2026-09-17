"""The run store."""
from __future__ import annotations

import pytest

from mu2edaq_power_recovery.checks import CheckResult, Status
from mu2edaq_power_recovery.state import RunStore


@pytest.fixture
def store(tmp_path) -> RunStore:
    s = RunStore(f"sqlite:///{tmp_path / 'runs.db'}")
    yield s
    s.close()


def test_a_run_records_its_operator_and_version(store):
    run_id = store.start_run("outage test", dry_run=True,
                             version={"package_version": "0.1.0"},
                             settings={"run": {"dry_run": True}})
    run = store.get_run(run_id)
    assert run["label"] == "outage test"
    assert run["dry_run"] == 1
    assert run["version"]["package_version"] == "0.1.0"
    assert run["operator"]


def test_checks_and_nodes_round_trip(store):
    store.start_run("t", True, {}, {})
    phase_id = store.start_phase("assess", 1)
    store.record_checks([
        CheckResult(node="n1", check_id="ping.lab", status=Status.OK, summary="up"),
        CheckResult(node="n1", check_id="disk.local", status=Status.FAIL,
                    summary="full", data={"full": ["/data"]}),
    ])
    store.record_node("n1", "mc2", "tracker", "fail", "1 check failed", "on")
    store.finish_phase("complete", "1 node")

    checks = store.get_checks(phase_id)
    assert {c["check_id"] for c in checks} == {"ping.lab", "disk.local"}
    assert next(c for c in checks if c["check_id"] == "disk.local")["data"]["full"] \
        == ["/data"]
    nodes = store.get_nodes(phase_id)
    assert nodes[0]["power_state"] == "on"


def test_actions_record_refusals_as_well_as_successes(store):
    # The audit trail has to show what was refused, not only what was done.
    store.start_run("t", False, {}, {})
    store.start_phase("poweron", 2)
    store.record_action("mu2egateway01", "power_off", "mu2egateway01-ipmi",
                        "refused", False, "protected host")
    store.record_action("mu2e-trk-01", "power_on", "mu2e-trk-01-ipmi",
                        "power_on", False, "issued")
    outcomes = {a["hostname"]: a["outcome"] for a in store.get_actions()}
    assert outcomes == {"mu2egateway01": "refused", "mu2e-trk-01": "power_on"}


def test_rerunning_a_phase_appends_rather_than_overwrites(store):
    # Re-assessing after a repair must not erase the evidence that the repair
    # was needed; 'latest' is what the report page means by current state.
    store.start_run("t", True, {}, {})
    first = store.start_phase("assess", 1)
    store.finish_phase("complete", "first pass")
    second = store.start_phase("assess", 1)
    store.finish_phase("complete", "after the repair")

    assert first != second
    assert len(store.get_phases()) == 2
    assert store.latest_phase("assess")["summary"] == "after the repair"


def test_export_is_json_serialisable(store):
    import json
    store.start_run("t", True, {"git_commit": "abc"}, {})
    store.start_phase("assess", 1)
    store.record_checks([CheckResult(node="n1", check_id="ping.lab",
                                     status=Status.OK, summary="up")])
    store.record_node("n1", "mc2", "tracker", "ok")
    store.record_event("something happened")
    store.finish_phase("complete")
    store.finish_run()

    export = store.export_run()
    # Datetimes must already be strings: this feeds the report and the logbook.
    json.dumps(export)
    assert export["phases"][0]["checks"][0]["check_id"] == "ping.lab"
    assert export["events"][0]["message"] == "something happened"


def test_latest_run_id_and_listing(store):
    first = store.start_run("one", True, {}, {})
    store.finish_run()
    second = store.start_run("two", True, {}, {})
    assert store.latest_run_id() == second
    assert [r["id"] for r in store.list_runs()][:2] == [second, first]


def test_from_settings_uses_sqlite_under_the_project(settings, tmp_path):
    store = RunStore.from_settings(settings)
    assert store.url.startswith("sqlite:///")
    assert str(tmp_path) in store.url
    store.close()


def test_a_database_url_overrides_the_sqlite_path(settings, tmp_path):
    # The Postgres hook: a full SQLAlchemy URL is used verbatim.
    settings.set("database.url", f"sqlite:///{tmp_path / 'explicit.db'}")
    store = RunStore.from_settings(settings)
    assert store.url.endswith("explicit.db")
    store.close()
