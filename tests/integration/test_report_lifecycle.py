"""The report lifecycle: regeneration, finalisation, reconciliation, bundles,
logbook attachments and the --json contract (#3, #4, #5, #6, #18, #24)."""
from __future__ import annotations

import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest

from mu2edaq_power_recovery import cli
from mu2edaq_power_recovery.checks import CheckResult, Status
from mu2edaq_power_recovery.phases import phase4_report
from mu2edaq_power_recovery.report.ecl import ECLPoster
from mu2edaq_power_recovery.state import RunStore


def _base(tmp_path, simulate=True):
    return ((["--simulate"] if simulate else []) + [
        "--no-self-update", "-q",
        "--database-url", f"sqlite:///{tmp_path / 'cli.db'}",
        "--output-dir", str(tmp_path / "html"),
        "--location", "mc2",
    ])


def run_cli(tmp_path, *args, simulate=True):
    return cli.main(_base(tmp_path, simulate) + list(args))


def _db(tmp_path, sql, *params):
    with sqlite3.connect(str(tmp_path / "cli.db")) as conn:
        return conn.execute(sql, params).fetchall()


def _run_count(tmp_path):
    if not (tmp_path / "cli.db").exists():
        return 0
    try:
        return _db(tmp_path, "select count(*) from runs")[0][0]
    except sqlite3.OperationalError:
        return 0


def _json(path):
    return json.loads(Path(path).read_text())


def _no_credentials_from_here(monkeypatch):
    from mu2edaq_power_recovery.orchestrator import Orchestrator
    monkeypatch.setattr(Orchestrator, "prepare_credentials",
                        lambda self: pytest.fail("prepare_credentials called"))


def _as_real_runs(tmp_path):
    """Mark the stored runs as not simulated.

    The suite can only create runs with --simulate (a real one would contact
    the cluster), and a simulated run is never posted or published. Tests of
    the posting path seed with a rehearsal and then clear the flag, which
    stands in for a real dry run stored by an earlier invocation.
    """
    _db(tmp_path, "update runs set simulated = 0")


@pytest.fixture
def no_credentials(monkeypatch):
    from mu2edaq_power_recovery.orchestrator import Orchestrator

    def refuse(self):
        raise AssertionError("prepare_credentials() reached")

    monkeypatch.setattr(Orchestrator, "prepare_credentials", refuse)


# ---------------------------------------------------------------------------
# #3 regeneration operates on the selected run
# ---------------------------------------------------------------------------


def test_regeneration_attaches_to_the_selected_run(tmp_path, capsys, monkeypatch):
    assert run_cli(tmp_path, "--phase", "assess", "--node", "mu2e-trk-01") == 0
    assert run_cli(tmp_path, "--phase", "assess", "--node", "mu2e-trk-02") == 0
    before = _db(tmp_path, "select status, finished_at from runs where id = 1")

    from mu2edaq_power_recovery.orchestrator import Orchestrator
    monkeypatch.setattr(Orchestrator, "prepare_credentials",
                        lambda self: pytest.fail("prepare_credentials called"))
    capsys.readouterr()
    assert run_cli(tmp_path, "--phase", "report", "--run-id", "1") == 0
    assert "REPORT ONLY" in capsys.readouterr().out

    assert _run_count(tmp_path) == 2                     # no new run row
    assert _db(tmp_path, "select status, finished_at from runs where id = 1") \
        == before                                        # status untouched
    assert _db(tmp_path, "select run_id from phases where name = 'report'") == [(1,)]
    assert any("report assembled for run 1" in m for (m,) in _db(
        tmp_path, "select message from events where run_id = 1"))
    assert not _db(tmp_path, "select id from phases where run_id = 2 "
                             "and name = 'report'")

    bundle = tmp_path / "html" / "runs" / "1"
    for name in ("summary", "report", "assess"):
        assert _json(bundle / "data" / f"{name}.json")["run_id"] == 1
    assert _json(bundle / "data" / "run-export.json")["run"]["id"] == 1
    assert _json(bundle / "data" / "summary.json")["run"]["id"] == 1
    for page in ("index.html", "detail.html", "initial-state.html"):
        assert '<dd class="font-mono">1</dd>' in (bundle / page).read_text()
    # Run 2 is still the newest, so the top level still shows it.
    assert _json(tmp_path / "html" / "data" / "summary.json")["run_id"] == 2
    assert not (tmp_path / "html" / "detail.html").exists()


def test_report_without_run_id_uses_the_latest_run(tmp_path, no_credentials):
    store = RunStore(f"sqlite:///{tmp_path / 'cli.db'}")
    store.start_run("seed", True, {}, {})
    store.finish_run("complete")
    store.close()
    # Exit 1: a run with no nodes assessed is UNKNOWN ("we could not look").
    assert run_cli(tmp_path, "--phase", "report") == 1
    assert _run_count(tmp_path) == 1
    assert _db(tmp_path, "select run_id from phases where name='report'") == [(1,)]


def test_a_missing_run_id_is_a_clean_usage_error(tmp_path, capsys, no_credentials):
    code = run_cli(tmp_path, "--phase", "report", "--run-id", "7")
    captured = capsys.readouterr()
    assert code == 2
    assert "run 7 is not in the run store" in captured.err
    assert "Traceback" not in captured.err + captured.out
    assert _run_count(tmp_path) == 0


def test_an_empty_store_has_nothing_to_report(tmp_path, capsys, no_credentials):
    assert run_cli(tmp_path, "--phase", "report") == 2
    assert "no run to report on" in capsys.readouterr().err


def test_run_id_is_only_valid_with_the_report_phase(tmp_path, capsys,
                                                    no_credentials):
    assert run_cli(tmp_path, "--phase", "all", "--run-id", "1") == 2
    assert "only valid with --phase report" in capsys.readouterr().err
    assert _run_count(tmp_path) == 0


# ---------------------------------------------------------------------------
# #4 the run is finished before the report is rendered or posted
# ---------------------------------------------------------------------------


def test_a_completed_run_reports_its_final_state(tmp_path, settings):
    assert run_cli(tmp_path, "--phase", "all") == 0
    bundle = tmp_path / "html" / "runs" / "1"
    for path in (bundle / "data" / "summary.json",
                 bundle / "data" / "run-export.json",
                 tmp_path / "html" / "data" / "summary.json"):
        run = _json(path)["run"]
        assert run["status"] == "complete"
        assert run["finished_at"]
    report = _json(bundle / "data" / "report.json")
    narrative = report["data"]["narrative"]
    assert narrative["run"]["status"] == "complete"
    assert narrative["run"]["finished_at"]
    detail = (bundle / "detail.html").read_text()
    assert "in progress" not in detail and "not finished" not in detail
    body = ECLPoster(settings).body(narrative)
    assert "still running" not in body and "not finished" not in body
    assert "Run status   : complete" in body
    assert f"Finished     : {narrative['run']['finished_at']}" in body


@pytest.mark.parametrize("raised, status, code", [
    (RuntimeError("the mesh probe blew up"), "error", 2),
    (KeyboardInterrupt(), "interrupted", 3),
])
def test_error_and_interrupted_runs_carry_their_terminal_status(
        tmp_path, monkeypatch, raised, status, code):
    from mu2edaq_power_recovery.phases import phase3_network

    def explode(*a, **kw):
        raise raised

    monkeypatch.setattr(phase3_network, "run", explode)
    assert run_cli(tmp_path, "--phase", "all", "--node", "mu2e-trk-01") == code
    assert _db(tmp_path, "select status from runs") == [(status,)]
    summary = _json(tmp_path / "html" / "runs" / "1" / "data" / "summary.json")
    assert summary["run"]["status"] == status and summary["run"]["finished_at"]

    monkeypatch.undo()
    assert run_cli(tmp_path, "--phase", "report", "--run-id", "1") in (0, 1)
    narrative = _json(tmp_path / "html" / "runs" / "1" / "data" /
                      "report.json")["data"]["narrative"]
    assert narrative["run"]["status"] == status
    assert _db(tmp_path, "select status from runs") == [(status,)]


# ---------------------------------------------------------------------------
# #5 reconciliation
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    s = RunStore(f"sqlite:///{tmp_path / 'r.db'}")
    s.start_run("t", True, {}, {})
    yield s
    s.close()


def _phase(store, name, number, results, node_status=None):
    store.start_phase(name, number)
    rows = [CheckResult(node=h, check_id=c, status=st, summary=f"{c} {st.value}")
            for h, c, st in results]
    store.record_checks(rows)
    hosts = sorted({h for h, _c, _s in results})
    for h in hosts:
        store.record_node(h, "mc2", "readout", (node_status or {}).get(h, "ok"),
                          data={"unreachable": False})
    store.finish_phase("complete")


def test_a_failure_rechecked_as_passing_is_resolved(store, settings):
    _phase(store, "assess", 1, [("n1", "disk.mounts", Status.FAIL),
                                ("n1", "ping.lab", Status.OK)], {"n1": "fail"})
    _phase(store, "poweron", 2, [("n1", "disk.mounts", Status.OK),
                                 ("n1", "ping.lab", Status.OK)])
    narrative = phase4_report.build_narrative(store.export_run())
    assert narrative["outstanding"] == []
    assert [(r["node"], r["check"], r["phase"], r["resolved_by"])
            for r in narrative["resolved"]] == [("n1", "disk.mounts", "assess",
                                                  "poweron")]
    assert narrative["status"] == "ok"
    assert narrative["counts"]["ok"] == 1 and narrative["counts"]["fail"] == 0
    assert "verified healthy" in narrative["headline"]
    assert not any("shared cause" in s for s in narrative["next_steps"])
    assert phase4_report.overall_status(store.export_run()) is Status.OK
    body = ECLPoster(settings).body(narrative)
    assert "Outstanding problems" not in body
    assert "Resolved during the run (1)" in body


def test_a_failure_with_no_later_evidence_stays_outstanding(store):
    _phase(store, "assess", 1, [("n1", "disk.mounts", Status.FAIL)], {"n1": "fail"})
    narrative = phase4_report.build_narrative(store.export_run())
    assert [(o["node"], o["check"]) for o in narrative["outstanding"]] == \
        [("n1", "disk.mounts")]
    assert narrative["status"] == "fail"
    assert narrative["resolved"] == []


def test_a_subset_recheck_does_not_clear_an_unrechecked_failure(store):
    # Phase 2 re-checks a subset of the profile; a phase-1-only failure of a
    # check it did not run is still current, whatever the node row says.
    _phase(store, "assess", 1, [("n1", "pcie.driver", Status.FAIL),
                                ("n1", "ping.lab", Status.FAIL)], {"n1": "fail"})
    _phase(store, "poweron", 2, [("n1", "ping.lab", Status.OK)], {"n1": "ok"})
    narrative = phase4_report.build_narrative(store.export_run())
    assert [(o["check"], o["phase"]) for o in narrative["outstanding"]] == \
        [("pcie.driver", "assess")]
    assert [r["check"] for r in narrative["resolved"]] == ["ping.lab"]
    assert narrative["failed"] == ["n1"]
    assert narrative["status"] == "fail"


def test_a_later_failure_supersedes_an_earlier_pass(store):
    _phase(store, "assess", 1, [("n1", "disk.mounts", Status.OK)])
    _phase(store, "poweron", 2, [("n1", "disk.mounts", Status.FAIL)], {"n1": "fail"})
    narrative = phase4_report.build_narrative(store.export_run())
    assert [o["phase"] for o in narrative["outstanding"]] == ["poweron"]


def test_an_unreachable_node_keeps_its_unsuperseded_failure(store):
    # Phase 1 sees a FAIL; phase 2 then cannot reach the node and records no
    # checks. The failure is still current: the node is FAIL, not UNKNOWN,
    # and the run is a failure ("it is broken"), not "we could not look".
    _phase(store, "assess", 1, [("n1", "disk.mounts", Status.FAIL),
                                ("n1", "ping.lab", Status.OK)], {"n1": "fail"})
    store.start_phase("poweron", 2)
    store.record_node("n1", "mc2", "readout", "unknown",
                      data={"unreachable": True})
    store.finish_phase("complete")
    export = store.export_run()
    state = phase4_report.reconcile(export)
    assert state["node_status"] == {"n1": "fail"}
    assert state["status"] is Status.FAIL
    narrative = phase4_report.build_narrative(export)
    assert narrative["counts"]["fail"] == 1
    assert narrative["counts"]["unknown"] == 0
    assert narrative["failed"] == ["n1"] and narrative["unreachable"] == []
    assert "1 failed, 0 unreachable" in narrative["headline"]
    assert [(o["check"], o["phase"]) for o in narrative["outstanding"]] == \
        [("disk.mounts", "assess")]


def test_an_unreachable_node_with_only_good_checks_is_unknown(store):
    _phase(store, "assess", 1, [("n1", "ping.lab", Status.OK)])
    store.start_phase("poweron", 2)
    store.record_node("n1", "mc2", "readout", "unknown",
                      data={"unreachable": True})
    store.finish_phase("complete")
    state = phase4_report.reconcile(store.export_run())
    assert state["node_status"] == {"n1": "unknown"}
    assert state["status"] is Status.UNKNOWN


def test_phase1_fail_then_phase2_pass_exits_0(tmp_path, monkeypatch):
    # End to end: /home is missing when phase 1 looks, back when phase 2
    # re-checks. The run is healthy; the report says the failure is resolved.
    from mu2edaq_power_recovery import orchestrator as orch_module
    from mu2edaq_power_recovery.transport import ScriptedResponse
    original = orch_module.healthy_node_rules

    def flaky_rules():
        return [(r"mountpoint -q", ScriptedResponse(rc=1, once=True))] + \
            list(original())

    monkeypatch.setattr(orch_module, "healthy_node_rules", flaky_rules)
    assert run_cli(tmp_path, "--phase", "all", "--node", "mu2e-trk-01",
                   "--node", "mu2e-trk-02", "--continue-on-error") == 0
    report = _json(tmp_path / "html" / "runs" / "1" / "data" / "report.json")
    narrative = report["data"]["narrative"]
    assert narrative["outstanding"] == []
    assert any(r["check"] == "disk.mounts" and r["phase"] == "assess"
               and r["resolved_by"] == "poweron" for r in narrative["resolved"])
    assert report["status"] == "ok"
    assert _db(tmp_path, "select status from runs") == [("complete",)]
    # The phase-1 page keeps the evidence that the failure happened.
    assess = _json(tmp_path / "html" / "runs" / "1" / "data" / "assess.json")
    assert assess["status"] == "fail"


# ---------------------------------------------------------------------------
# #6 per-run bundles hold that run's data only
# ---------------------------------------------------------------------------


def test_two_runs_into_one_output_dir_do_not_mix(tmp_path):
    assert run_cli(tmp_path, "--phase", "assess", "--node", "mu2e-trk-01") == 0
    assert run_cli(tmp_path, "--phase", "network", "--node", "mu2e-dl-01",
                   "--node", "mu2e-dl-02") == 0
    html = tmp_path / "html"
    first, second = html / "runs" / "1", html / "runs" / "2"

    assert (first / "initial-state.html").exists()
    assert (first / "data" / "assess.json").exists()
    assert not (first / "network.html").exists()

    assert not (second / "initial-state.html").exists()
    assert not (second / "data" / "assess.json").exists()
    assert (second / "network.html").exists()
    for page in second.glob("*.html"):
        assert "initial-state.html" not in page.read_text(), page.name
    for data in (second / "data").glob("*.json"):
        payload = _json(data)
        rid = payload.get("run_id", (payload.get("run") or {}).get("id")) \
            if isinstance(payload, dict) else 2
        assert rid == 2, data.name
    assert all(n["hostname"].startswith("mu2e-dl-")
               for n in _json(second / "data" / "inventory.json")) or \
        _json(second / "data" / "inventory.json") == []

    # The latest view is run 2: no stale phase-1 page or data at the top.
    assert not (html / "initial-state.html").exists()
    assert not (html / "data" / "assess.json").exists()
    assert _json(html / "data" / "summary.json")["run_id"] == 2
    assert "runs/1/index.html" in (html / "runs.html").read_text()


# ---------------------------------------------------------------------------
# #18 the logbook gets the selected run's rendered bundle
# ---------------------------------------------------------------------------


class _Vault:
    def ecl(self):
        return {"user": "u", "key": "k"}


@pytest.fixture
def ecl_stub(monkeypatch):
    posted = []
    module = types.ModuleType("ecl_client")

    def post(**kw):
        if module.fail:
            raise RuntimeError("logbook is down")
        posted.append(kw)
        return "entry 123"

    module.post = post
    module.fail = False
    monkeypatch.setitem(sys.modules, "ecl_client", module)
    monkeypatch.setattr(phase4_report, "make_vault", lambda orch: _Vault())
    return module, posted


def test_report_only_post_attaches_the_run_bundle(tmp_path, ecl_stub, monkeypatch):
    module, posted = ecl_stub
    assert run_cli(tmp_path, "--phase", "assess", "--node", "mu2e-trk-01") == 0
    assert run_cli(tmp_path, "--phase", "network", "--node", "mu2e-dl-01") in (0, 1)
    _as_real_runs(tmp_path)

    from mu2edaq_power_recovery.orchestrator import Orchestrator
    monkeypatch.setattr(Orchestrator, "prepare_credentials",
                        lambda self: pytest.fail("prepare_credentials called"))
    assert run_cli(tmp_path, "--phase", "report", "--run-id", "1", "--post-ecl",
                   simulate=False) == 0

    bundle = tmp_path / "html" / "runs" / "1"
    assert len(posted) == 1
    files = posted[0]["files"]
    assert sorted(files) == sorted(str(p) for p in bundle.glob("*.html"))
    assert str(bundle / "detail.html") in files
    assert all(f.startswith(str(bundle)) for f in files)
    report = _json(bundle / "data" / "report.json")
    assert report["ecl"]["posted"] is True
    assert any("posted the recovery report for run 1" in m for (m,) in _db(
        tmp_path, "select message from events where run_id = 1"))
    assert _run_count(tmp_path) == 2


def test_a_failed_post_leaves_the_complete_local_report(tmp_path, ecl_stub):
    module, _posted = ecl_stub
    module.fail = True
    assert run_cli(tmp_path, "--phase", "assess", "--node", "mu2e-trk-01") == 0
    _as_real_runs(tmp_path)
    assert run_cli(tmp_path, "--phase", "report", "--post-ecl",
                   simulate=False) == 0
    bundle = tmp_path / "html" / "runs" / "1"
    assert (bundle / "detail.html").exists()
    report = _json(bundle / "data" / "report.json")
    assert report["ecl"]["posted"] is False
    assert "logbook is down" in report["ecl"]["error"]
    assert _db(tmp_path, "select level from events where message like "
                         "'logbook posting failed%'") == [("error",)]


def test_a_simulated_run_never_posts(tmp_path, ecl_stub):
    _module, posted = ecl_stub
    assert run_cli(tmp_path, "--phase", "all", "--node", "mu2e-trk-01",
                   "--node", "mu2e-trk-02", "--post-ecl") == 0
    assert posted == []
    report = _json(tmp_path / "html" / "runs" / "1" / "data" / "report.json")
    assert "simulated" in report["ecl"]["reason"]


@pytest.mark.parametrize("flags", [["--post-ecl"], ["--publish"],
                                   ["--publish-target", "/nowhere"],
                                   ["--run-id", "1", "--post-ecl"]])
def test_a_stored_simulated_run_is_never_posted_or_published_later(
        tmp_path, ecl_stub, capsys, monkeypatch, flags):
    # '--phase all --simulate' then a non-simulated 'mu2e-power-report
    # --post-ecl' used to post the rehearsal as if it were a real dry run.
    _module, posted = ecl_stub
    assert run_cli(tmp_path, "--phase", "all", "--node", "mu2e-trk-01",
                   "--node", "mu2e-trk-02") == 0
    assert _db(tmp_path, "select simulated from runs") == [(1,)]
    _no_credentials_from_here(monkeypatch)
    reports_before = _db(tmp_path, "select count(*) from phases "
                                   "where name = 'report'")
    capsys.readouterr()
    code = run_cli(tmp_path, "--phase", "report", *flags, simulate=False)
    err = capsys.readouterr().err
    assert code == 2
    assert "run 1 was a simulated run" in err
    assert posted == []
    assert not (tmp_path / "nowhere").exists()
    # Refused before anything was recorded against the run.
    assert _db(tmp_path, "select count(*) from phases where name = 'report'") \
        == reports_before


def test_a_stored_simulated_run_still_regenerates_locally(tmp_path, ecl_stub,
                                                          monkeypatch):
    _module, posted = ecl_stub
    assert run_cli(tmp_path, "--phase", "all", "--node", "mu2e-trk-01",
                   "--node", "mu2e-trk-02") == 0
    _no_credentials_from_here(monkeypatch)
    assert run_cli(tmp_path, "--phase", "report", simulate=False) == 0
    assert posted == []
    bundle = tmp_path / "html" / "runs" / "1"
    assert "SIMULATED" in (bundle / "index.html").read_text()
    assert "SIMULATED RUN" in (bundle / "detail.html").read_text()
    assert "simulated" in (tmp_path / "html" / "runs.html").read_text()
    assert _json(bundle / "data" / "run-export.json")["run"]["simulated"] == 1


def test_post_refuses_a_stored_simulated_run_directly(tmp_path, ecl_stub):
    # The second line behind the driver's refusal: phase4_report.post()
    # itself reads the stored flag, whatever the orchestrator says.
    _module, posted = ecl_stub
    store = RunStore(f"sqlite:///{tmp_path / 'p.db'}")
    rid = store.start_run("t", True, {}, {}, simulated=True)
    orch = types.SimpleNamespace(simulate=False, store=store, settings=None)
    info = phase4_report.post(orch, rid, {"run": {}})
    store.close()
    assert info["posted"] is False and "simulated" in info["reason"]
    assert posted == []


# ---------------------------------------------------------------------------
# #24 --json: stdout is one JSON document
# ---------------------------------------------------------------------------


def test_json_all_phases_is_one_document_without_ansi(tmp_path, capsys,
                                                      monkeypatch):
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)
    code = cli.main([a for a in _base(tmp_path) if a != "-q"]
                    + ["--phase", "all", "--node", "mu2e-trk-01",
                       "--node", "mu2e-trk-02", "--json"])
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert "\x1b" not in captured.out
    assert "Phase 1: Initial state" in captured.err       # humans: stderr
    assert doc["exit_code"] == code == 0
    assert doc["run_id"] == 1 and doc["status"] == "complete"
    assert [p["name"] for p in doc["phases"]] == ["assess", "poweron",
                                                  "network", "report"]
    assert "export" not in doc["phases"][-1]["data"]
    assert doc["report"]["ecl"]["posted"] is False
    assert doc["report"]["output_dir"] == str(tmp_path / "html")
    assert doc["version"]["package_version"]


def test_json_list_nodes_is_pure_json(capsys, monkeypatch):
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert cli.main(["--list-nodes", "--location", "mc2", "--no-self-update",
                     "--json"]) == 0
    nodes = json.loads(capsys.readouterr().out)
    assert any(n["hostname"].startswith("mu2e-trk-01") for n in nodes)


def test_json_error_path_is_one_document(tmp_path, capsys, no_credentials):
    code = run_cli(tmp_path, "--phase", "report", "--run-id", "5", "--json")
    captured = capsys.readouterr()
    doc = json.loads(captured.out)
    assert code == doc["exit_code"] == 2
    assert "run 5 is not in the run store" in doc["error"]
    assert doc["run_id"] is None
    assert "error:" in captured.err


def test_json_single_phase_entry_points_inherit_the_contract(tmp_path, capsys):
    code = cli.main_state(_base(tmp_path) + ["--node", "mu2e-trk-01", "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 0 and doc["phases"][0]["name"] == "assess"
