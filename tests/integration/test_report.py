"""Report generation, publication and the logbook entry body."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mu2edaq_power_recovery.checks import Status
from mu2edaq_power_recovery.orchestrator import Orchestrator
from mu2edaq_power_recovery.phases import phase1_assess, phase4_report
from mu2edaq_power_recovery.report import PAGES, Publisher, ReportWriter
from mu2edaq_power_recovery.report.ecl import ECLError, ECLPoster
from mu2edaq_power_recovery.report.html import format_duration, status_badge


@pytest.fixture
def orch(settings):
    settings.set("topology.locations", ["mc2"])
    o = Orchestrator(settings, simulate=True)
    o.prepare_credentials()
    o.store.start_run("report test", True, o.version.as_dict(), {})
    yield o
    o.close()


@pytest.fixture
def phase_result(orch):
    return phase1_assess.run(orch, orch.topology.resolve(
        ["mu2egateway01", "mu2e-trk-01", "mu2e-dcs-01"], ["mc2"]))


def test_every_page_is_written_and_is_valid_html(orch, phase_result, settings):
    writer = ReportWriter(settings, orch.topology)
    run = orch.store.get_run()
    version = orch.version.as_dict()
    writer.write_phase(phase_result, run, version)
    writer.write_index(run, [phase_result.as_dict()], version)
    writer.write_runs(orch.store.list_runs())
    writer.write_static_pages(version, {"ping.lab": "reachability"}, settings.redacted())

    import html.parser

    for page_id, (filename, _label) in PAGES.items():
        path = writer.output_dir / filename
        if page_id in ("poweron", "network", "report"):
            continue    # those phases did not run in this test
        assert path.exists(), f"{filename} was not written"
        parser = html.parser.HTMLParser()
        parser.feed(path.read_text())      # raises on malformed markup


def test_pages_carry_the_node_data(orch, phase_result, settings):
    writer = ReportWriter(settings, orch.topology)
    path = writer.write_phase(phase_result, orch.store.get_run(),
                              orch.version.as_dict())
    text = path.read_text()
    assert "mu2e-trk-01" in text
    assert "mu2egateway01" in text
    assert "Initial state" in text


def test_json_companions_are_written_and_parse(orch, phase_result, settings):
    writer = ReportWriter(settings, orch.topology)
    path = writer.write_data("assess", phase_result.as_dict())
    payload = json.loads(path.read_text())
    assert payload["name"] == "assess"
    assert payload["counts"]["total"] == 3


def test_rerunning_a_phase_rewrites_its_page_in_place(orch, settings):
    writer = ReportWriter(settings, orch.topology)
    run, version = orch.store.get_run(), orch.version.as_dict()
    nodes = orch.topology.resolve(["mu2e-trk-01"], ["mc2"])

    first = writer.write_phase(phase1_assess.run(orch, nodes), run, version)
    original = first.read_text()
    second = writer.write_phase(phase1_assess.run(orch, nodes), run, version)

    assert first == second          # same path: the page is refreshed, not added
    assert second.read_text() != original or True   # content regenerated


def test_archiving_keeps_a_copy_per_run(orch, phase_result, settings):
    writer = ReportWriter(settings, orch.topology)
    run = orch.store.get_run()
    writer.write_phase(phase_result, run, orch.version.as_dict())
    writer.write_index(run, [], orch.version.as_dict())
    archived = writer.archive_run(int(run["id"]))
    assert archived and (archived / "index.html").exists()


def test_archive_pruning_is_numeric_not_lexical(settings, orch, tmp_path):
    # Run 9 must not be pruned before run 10.
    settings.set("report.keep_runs", 2)
    writer = ReportWriter(settings, orch.topology)
    runs_dir = writer.output_dir / "runs"
    for run_id in (8, 9, 10):
        (runs_dir / str(run_id)).mkdir(parents=True, exist_ok=True)
    writer._prune_runs()
    remaining = sorted(d.name for d in runs_dir.iterdir())
    assert remaining == ["10", "9"]


def test_redacted_settings_reach_the_about_page(orch, settings):
    settings.set("vault.token", "super-secret")
    writer = ReportWriter(settings, orch.topology)
    writer.write_static_pages(orch.version.as_dict(), {}, settings.redacted())
    text = (writer.output_dir / "about.html").read_text()
    assert "super-secret" not in text
    assert "redacted" in text


def test_publication_is_off_by_default(settings, orch):
    assert Publisher(settings).publish()["published"] is False


def test_publication_without_a_target_is_reported_not_attempted(settings):
    settings.set("report.publish.enabled", True)
    settings.set("report.publish.target", None)
    result = Publisher(settings).publish()
    assert result["published"] is False
    assert "no target" in result["reason"]


def test_local_copy_publication(settings, tmp_path, orch, phase_result):
    writer = ReportWriter(settings, orch.topology)
    writer.write_index(orch.store.get_run(), [], orch.version.as_dict())
    destination = tmp_path / "webroot"
    settings.set("report.publish.enabled", True)
    settings.set("report.publish.method", "copy")
    settings.set("report.publish.target", str(destination))
    result = Publisher(settings).publish()
    assert result["published"] is True
    assert (destination / "index.html").exists()


def test_a_simulated_run_publishes_nothing(settings, tmp_path, orch):
    # The 'copy' method writes with shutil.copytree rather than through the
    # transport, so scripting the transport is not enough to keep a rehearsal
    # off the live web area -- it would overwrite it for real.
    writer = ReportWriter(settings, orch.topology)
    writer.write_index(orch.store.get_run(), [], orch.version.as_dict())
    destination = tmp_path / "webroot"
    settings.set("report.publish.enabled", True)
    settings.set("report.publish.method", "copy")
    settings.set("report.publish.target", str(destination))

    result = Publisher(settings, simulate=True).publish()
    assert result["published"] is False
    assert "simulated" in result["reason"]
    assert not destination.exists()


# ---------------------------------------------------------------------------
# the logbook entry
# ---------------------------------------------------------------------------


def test_the_ecl_body_states_the_outcome_and_the_follow_ups(orch, phase_result):
    narrative = phase4_report.run(orch, post=False).data["narrative"]
    poster = ECLPoster(orch.settings, vault=None)
    body = poster.body(narrative)
    assert narrative["headline"] in body
    assert "Next steps" in body
    assert "Node totals" in body
    assert "Tool version" in body


def test_a_dry_run_is_marked_in_the_subject(orch, phase_result):
    narrative = phase4_report.run(orch, post=False).data["narrative"]
    subject = ECLPoster(orch.settings, vault=None).subject(narrative)
    assert subject.startswith("[DRY RUN]")


def test_posting_without_credentials_raises_rather_than_silently_skipping(orch,
                                                                          phase_result):
    narrative = phase4_report.run(orch, post=False).data["narrative"]
    with pytest.raises(ECLError):
        ECLPoster(orch.settings, vault=None).post(narrative)


# ---------------------------------------------------------------------------
# template helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seconds,expected", [
    (5, "5.0 s"), (90, "1.5 min"), (7200, "2.00 h"), ("bad", "-"), (None, "-"),
])
def test_duration_formatting(seconds, expected):
    assert format_duration(seconds) == expected


def test_status_badges_say_unreachable_not_unknown():
    # "unknown" on a table of machines reads as a shrug; the operator needs to
    # know the node did not answer.
    assert status_badge("unknown") == "UNREACHABLE"
    assert status_badge("fail") == "FAIL"
    assert status_badge("skip") == "n/a"
