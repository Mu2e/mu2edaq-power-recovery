"""Check implementations, run against a scripted transport.

Each test injects one fault into an otherwise healthy node and asserts that the
check notices it and says something useful.  A check that cannot be made to
fail in a test cannot be trusted to fail on the cluster.

Faults are injected with ``FakeTransport.expect_first``, which puts one rule
ahead of the healthy baseline: everything else about the node stays working, so
a failure in these tests is about the check under test and nothing else.
"""
from __future__ import annotations

import pytest

from mu2edaq_power_recovery.checks import REGISTRY, Status, run_check
from mu2edaq_power_recovery.checks.base import rollup, worst
from mu2edaq_power_recovery.transport import FakeTransport, ScriptedResponse
from mu2edaq_power_recovery.transport.base import TransportError


# ---------------------------------------------------------------------------
# framework
# ---------------------------------------------------------------------------


def test_every_profile_id_has_an_implementation(checks_config):
    listed = {cid for ids in checks_config["profiles"].values() for cid in ids}
    missing = sorted(listed - set(REGISTRY))
    assert not missing, f"checks.yaml names unimplemented checks: {missing}"


def test_an_unknown_check_id_is_skipped_not_silently_dropped(make_context):
    result = run_check("no.such.check", make_context())
    assert result.status is Status.SKIP
    assert "not registered" in result.detail


def test_a_raising_check_becomes_unknown_not_a_crash(make_context, monkeypatch):
    def boom(ctx):
        raise ValueError("deliberate")
    monkeypatch.setitem(REGISTRY, "test.boom", boom)
    result = run_check("test.boom", make_context())
    # A bug in one check must not abort the assessment of a 50-node cluster.
    assert result.status is Status.UNKNOWN
    assert "ValueError" in result.summary


def test_transport_failure_is_unknown_not_fail(make_context, monkeypatch):
    # Patched on the class: the context asks the factory for a transport and
    # gets a clone back, so patching one instance would miss it.
    def unreachable(*args, **kwargs):
        raise TransportError("host is down")
    monkeypatch.setattr(FakeTransport, "run", unreachable)
    result = run_check("disk.local", make_context())
    # "We could not look" must never be reported as "we looked and it is wrong".
    assert result.status is Status.UNKNOWN


def test_rollup_ignores_not_applicable_checks():
    # A node with no BMC skips power.status; that must not make the node 'n/a'.
    assert rollup([Status.OK, Status.SKIP, Status.OK]) is Status.OK
    assert rollup([Status.SKIP, Status.SKIP]) is Status.SKIP
    assert rollup([Status.OK, Status.WARN, Status.FAIL]) is Status.FAIL
    # worst() keeps the raw ranking, which is right for a single check.
    assert worst([Status.OK, Status.SKIP]) is Status.SKIP


# ---------------------------------------------------------------------------
# reachability and login
# ---------------------------------------------------------------------------


def test_ping_passes_on_a_clean_reply(make_context):
    result = run_check("ping.lab", make_context())
    assert result.status is Status.OK
    assert result.data["loss_pct"] == 0.0


def test_ping_warns_on_partial_loss(make_context, fake_transport):
    fake_transport.expect_first(r"\bping\b", ScriptedResponse(
        stdout="3 packets transmitted, 1 received, 66% packet loss, time 2035ms"))
    assert run_check("ping.lab", make_context()).status is Status.WARN


def test_ping_fails_when_nothing_answers(make_context, fake_transport):
    fake_transport.expect_first(r"\bping\b", ScriptedResponse(
        stdout="3 packets transmitted, 0 received, 100% packet loss"))
    assert run_check("ping.lab", make_context()).status is Status.FAIL


def test_root_login_requires_uid_zero(make_context, fake_transport):
    fake_transport.expect_first(r"\bid -u\b", ScriptedResponse(stdout="1001"))
    result = run_check("ssh.login_root", make_context())
    assert result.status is Status.FAIL
    assert "expected 0" in result.summary


def test_service_account_login_exercises_the_home_directory(make_context, fake_transport):
    # The usual post-outage failure: the account exists and authenticates, but
    # /home is not mounted, so `su -` lands nowhere.
    fake_transport.expect_first(r"\bsu\s+-\s", ScriptedResponse(
        stderr="su: warning: cannot change directory to /home/mu2edaq: "
               "No such file or directory", rc=1))
    result = run_check("login.users", make_context())
    assert result.status is Status.FAIL
    assert "mu2edaq" in result.summary


# ---------------------------------------------------------------------------
# disks
# ---------------------------------------------------------------------------


def test_disk_local_passes_on_a_healthy_node(make_context):
    assert run_check("disk.local", make_context()).status is Status.OK


def test_disk_local_fails_over_the_threshold(make_context, fake_transport):
    fake_transport.expect_first(r"\bdf\b", ScriptedResponse(stdout=(
        "Filesystem   Type 1024-blocks    Used Available Capacity Mounted on\n"
        "/dev/sda1    xfs      1000000  960000     40000      96% /data")))
    result = run_check("disk.local", make_context())
    assert result.status is Status.FAIL
    assert "/data" in result.data["full"]


def test_disk_local_warns_between_the_thresholds(make_context, fake_transport):
    fake_transport.expect_first(r"\bdf\b", ScriptedResponse(stdout=(
        "Filesystem   Type 1024-blocks    Used Available Capacity Mounted on\n"
        "/dev/sda1    xfs      1000000  850000    150000      85% /data")))
    assert run_check("disk.local", make_context()).status is Status.WARN


def test_missing_mount_fails(make_context, fake_transport):
    fake_transport.expect_first(r"mountpoint -q /home", ScriptedResponse(rc=1))
    result = run_check("disk.mounts", make_context())
    assert result.status is Status.FAIL
    assert "/home" in result.data["missing"]


def test_the_nfs_server_does_not_check_its_own_export(make_context):
    # mu2e-mgr-01 exports /home; it must not be asked to mount it from itself.
    result = run_check("disk.mounts", make_context("mu2e-mgr-01.fnal.gov"))
    assert result.status is Status.OK
    assert any("/home" in entry for entry in result.data["skipped"])


def test_nfs_from_the_wrong_server_fails(make_context, fake_transport):
    # Passes 'mountpoint' and fails the moment anything reads a file.
    fake_transport.expect_first(r"\bdf\b", ScriptedResponse(stdout=(
        "Filesystem            Type 1024-blocks   Used Available Capacity Mounted on\n"
        "old-server:/home      nfs4     1000000 200000    800000      20% /home\n"
        "mu2e-mgr-01:/daqlogs  nfs4     1000000 100000    900000      10% /daqlogs")))
    result = run_check("disk.nfs_from_mgr", make_context("mu2e-dcs-01.fnal.gov"))
    assert result.status is Status.FAIL
    assert any("old-server" in entry for entry in result.data["wrong"])


def test_raid_degradation_fails(make_context, fake_transport):
    fake_transport.expect_first(r"/proc/mdstat", ScriptedResponse(stdout=(
        "Personalities : [raid1]\n"
        "md0 : active raid1 sda1[0]\n"
        "      1048512 blocks [2/1] [U_]\n")))
    result = run_check("disk.raid", make_context("mu2e-mgr-01.fnal.gov"))
    assert result.status is Status.FAIL


def test_smart_failure_is_reported(make_context, fake_transport):
    fake_transport.expect_first(r"smartctl", ScriptedResponse(
        stdout="SMART overall-health self-assessment test result: FAILED!"))
    assert run_check("disk.smart", make_context()).status is Status.FAIL


def test_storage_errors_in_the_boot_log_fail(make_context, fake_transport):
    fake_transport.expect_first(r"journalctl|dmesg", ScriptedResponse(
        stdout="kernel: blk_update_request: I/O error, dev sdb, sector 98765"))
    result = run_check("disk.errors", make_context())
    assert result.status is Status.FAIL
    assert result.data["count"] == 1


# ---------------------------------------------------------------------------
# network
# ---------------------------------------------------------------------------


def test_interfaces_pass_when_every_expected_subnet_is_present(make_context):
    assert run_check("net.interfaces", make_context()).status is Status.OK


def test_a_missing_data_interface_fails(make_context, fake_transport):
    fake_transport.expect_first(r"ip -o -4 addr", ScriptedResponse(stdout=(
        "1: lo    inet 127.0.0.1/8 scope host lo\n"
        "2: eno1  inet 131.225.245.51/24 scope global eno1")))
    result = run_check("net.data", make_context())
    assert result.status is Status.FAIL
    assert "10.226.9.0/24" in result.summary


def test_a_data_link_at_the_wrong_speed_fails(make_context, fake_transport):
    # The quiet post-outage failure: everything works, ten times too slowly.
    fake_transport.expect_first(r"/sys/class/net/.*/speed",
                                ScriptedResponse(stdout="1000"))
    result = run_check("net.data", make_context())
    assert result.status is Status.FAIL
    assert "1000" in result.summary


def test_data_check_is_skipped_where_there_is_no_data_network(make_context):
    # The DCS servers have no data NIC by design; that is not a failure.
    assert run_check("net.data",
                     make_context("mu2e-dcs-01.fnal.gov")).status is Status.SKIP


def test_a_carrierless_interface_fails(make_context, fake_transport):
    fake_transport.expect_first(r"ip -o link", ScriptedResponse(stdout=(
        "2: eno1: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 state UP\n"
        "5: ens1f0: <NO-CARRIER,BROADCAST,MULTICAST,UP> mtu 9000 state DOWN")))
    result = run_check("net.data", make_context())
    assert result.status is Status.FAIL
    assert "no link" in result.summary


def test_dns_failure_is_reported(make_context, fake_transport):
    fake_transport.expect_first(r"getent hosts", ScriptedResponse(rc=2))
    assert run_check("net.dns", make_context()).status is Status.FAIL


def test_disabled_forwarding_on_a_gateway_fails(make_context, fake_transport):
    fake_transport.expect_first(r"ip_forward", ScriptedResponse(stdout="0"))
    result = run_check("net.forwarding", make_context("mu2egateway01.fnal.gov"))
    assert result.status is Status.FAIL


def test_an_empty_firewall_ruleset_fails(make_context, fake_transport):
    # A gateway with no rules looks healthy and is wide open: worse than down.
    fake_transport.expect_first(r"\bnft\b|\biptables\b",
                                ScriptedResponse(stdout="", rc=1))
    result = run_check("svc.firewall", make_context("mu2egateway01.fnal.gov"))
    assert result.status is Status.FAIL


def test_default_policies_alone_are_not_a_firewall(make_context, fake_transport):
    fake_transport.expect_first(r"\bnft\b", ScriptedResponse(rc=1))
    fake_transport.expect_first(r"\biptables\b", ScriptedResponse(
        stdout="-P INPUT ACCEPT\n-P FORWARD ACCEPT\n-P OUTPUT ACCEPT"))
    result = run_check("svc.firewall", make_context("mu2egateway01.fnal.gov"))
    assert result.status is Status.FAIL
    assert "default policies" in result.summary


# ---------------------------------------------------------------------------
# host and services
# ---------------------------------------------------------------------------


def test_uptime_passes_on_a_freshly_booted_node(make_context):
    assert run_check("host.uptime", make_context()).status is Status.OK


def test_uptime_flags_a_node_that_did_not_actually_reboot(make_context):
    # Phase 2 sets expect_recent_boot after issuing a power-on; a long uptime
    # then means the IPMI command reached a different chassis.
    ctx = make_context(baseline={"expect_recent_boot": True})
    ctx.ssh_factory.transport.expect_first(
        r"cat /proc/uptime", ScriptedResponse(stdout="864000.0 1.0"))
    result = run_check("host.uptime", ctx)
    assert result.status is Status.WARN
    assert "just powered on" in result.summary


def test_a_tainted_kernel_warns(make_context, fake_transport):
    fake_transport.expect_first(r"/proc/sys/kernel/tainted",
                                ScriptedResponse(stdout="4096"))
    assert run_check("host.kernel", make_context()).status is Status.WARN


def test_a_missing_pcie_card_fails(make_context, fake_transport):
    fake_transport.expect_first(r"lspci|xilinx", ScriptedResponse(stdout=""))
    result = run_check("pcie.devices", make_context("mu2e-trk-01.fnal.gov"))
    assert result.status is Status.FAIL
    assert "AC power cycle" in result.detail


def test_pcie_checks_skip_hosts_without_a_card(make_context):
    assert run_check("pcie.devices",
                     make_context("mu2e-dcs-01.fnal.gov")).status is Status.SKIP


def test_an_unloaded_driver_fails(make_context, fake_transport):
    fake_transport.expect_first(r"lsmod", ScriptedResponse(stdout=""))
    result = run_check("pcie.driver", make_context("mu2e-trk-01.fnal.gov"))
    assert result.status is Status.FAIL
    assert "not loaded" in result.summary


def test_a_stopped_service_fails(make_context, fake_transport):
    fake_transport.expect_first(r"systemctl is-active",
                                ScriptedResponse(stdout="inactive", rc=3))
    fake_transport.expect_first(r"pgrep|ps -C", ScriptedResponse(rc=1))
    result = run_check("svc.running", make_context("mu2e-dl-01.fnal.gov"))
    assert result.status is Status.FAIL
    assert "node_exporter" in result.summary


def test_missing_nfs_exports_fail(make_context, fake_transport):
    fake_transport.expect_first(r"exportfs|showmount|/proc/fs/nfsd/exports",
                                ScriptedResponse(stdout="", rc=1))
    result = run_check("svc.nfs_export", make_context("mu2e-mgr-01.fnal.gov"))
    assert result.status is Status.FAIL
