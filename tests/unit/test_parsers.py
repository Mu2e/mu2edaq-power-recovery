"""Output parsers.

These guard against the failure mode that matters most in a health check: a
parser that silently misreads output and reports a healthy verdict for a broken
machine.  Every sample here is real command output, not invented.
"""
from __future__ import annotations

import pytest

from mu2edaq_power_recovery.checks import parsers as P

DF_OUTPUT = """Filesystem                Type  1024-blocks      Used Available Capacity Mounted on
/dev/mapper/rhel-root     xfs      52403200  18321408  34081792      35% /
/dev/sda1                 xfs       1038336    329216    709120      32% /boot
mu2e-mgr-01:/home         nfs4   2147483648 429496730 1717986918     20% /home
/dev/mapper/rhel-data     xfs     943718400 897024000  46694400      96% /data
"""


def test_parse_df_reads_every_column():
    rows = P.parse_df(DF_OUTPUT)
    assert len(rows) == 4
    root = rows[0]
    assert root.mountpoint == "/" and root.fstype == "xfs" and root.use_pct == 35
    assert rows[3].use_pct == 96


def test_parse_df_identifies_network_filesystems():
    rows = {r.mountpoint: r for r in P.parse_df(DF_OUTPUT)}
    assert rows["/home"].is_network
    assert not rows["/"].is_network


def test_parse_df_ignores_unparseable_rows():
    rows = P.parse_df(DF_OUTPUT + "garbage line with too few\n")
    assert len(rows) == 4


IP_LINK = """1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN
2: eno1: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc mq state UP
3: ens1f0: <NO-CARRIER,BROADCAST,MULTICAST,UP> mtu 9000 qdisc mq state DOWN
"""
IP_ADDR = """2: eno1    inet 131.225.245.51/24 brd 131.225.245.255 scope global eno1
3: ens1f0    inet 10.226.9.51/24 brd 10.226.9.255 scope global ens1f0
"""


def test_interface_needs_carrier_not_just_admin_up():
    # ens1f0 is admin UP but has NO-CARRIER: a cable-out NIC must not pass.
    interfaces = P.parse_ip_link(IP_LINK)
    assert interfaces["eno1"].up
    assert not interfaces["ens1f0"].up
    assert interfaces["ens1f0"].mtu == 9000


def test_addresses_merge_onto_the_link_view():
    interfaces = P.parse_ip_addr(IP_ADDR, P.parse_ip_link(IP_LINK))
    assert interfaces["eno1"].addresses == ["131.225.245.51/24"]
    assert interfaces["ens1f0"].mtu == 9000     # link data survives the merge


def test_address_in_subnet():
    assert P.address_in_subnet("10.226.9.51/24", "10.226.9.0/24")
    assert P.address_in_subnet("131.225.245.7", "131.225.245.0/24")
    assert not P.address_in_subnet("131.225.237.7", "131.225.245.0/24")
    # Remote output is untrusted; garbage must be False, not an exception.
    assert not P.address_in_subnet("not-an-address", "10.226.9.0/24")


PING_OK = """3 packets transmitted, 3 received, 0% packet loss, time 2003ms
rtt min/avg/max/mdev = 0.112/0.147/0.201/0.031 ms"""
PING_LOSS = """3 packets transmitted, 1 received, 66% packet loss, time 2035ms
rtt min/avg/max/mdev = 0.300/0.300/0.300/0.000 ms"""
PING_DEAD = "3 packets transmitted, 0 received, 100% packet loss, time 2045ms"
PING_BSD = """3 packets transmitted, 3 packets received, 0.0% packet loss
round-trip min/avg/max/stddev = 0.052/0.081/0.121/0.029 ms"""


def test_parse_ping_variants():
    ok = P.parse_ping(PING_OK)
    assert ok.alive and ok.loss_pct == 0.0 and ok.rtt_avg_ms == 0.147

    lossy = P.parse_ping(PING_LOSS)
    assert lossy.alive and lossy.loss_pct == 66.0

    dead = P.parse_ping(PING_DEAD)
    assert not dead.alive and dead.loss_pct == 100.0

    # BSD ping on macOS words it differently; both must parse.
    bsd = P.parse_ping(PING_BSD)
    assert bsd.alive and bsd.rtt_avg_ms == 0.081


def test_parse_ping_on_empty_output_is_not_alive():
    assert not P.parse_ping("").alive


PING_WINDOWS = """
Ping statistics for 131.225.245.51:
    Packets: Sent = 3, Received = 3, Lost = 0 (0% loss),
Approximate round trip times in milli-seconds:
    Minimum = 0ms, Maximum = 2ms, Average = 1ms
"""
PING_WINDOWS_DEAD = """
Ping statistics for 131.225.245.51:
    Packets: Sent = 3, Received = 0, Lost = 3 (100% loss),
"""


def test_parse_ping_reads_the_windows_wording():
    # The gateways are the one class probed from the operator's own
    # workstation, and INSTALL.md says that may be Windows 11.  Windows ping
    # shares no wording with either Unix ping, so without this an operator on
    # Windows would be told both gateways are dead.
    win = P.parse_ping(PING_WINDOWS)
    assert win.alive and win.transmitted == 3 and win.received == 3
    assert win.loss_pct == 0.0 and win.rtt_avg_ms == 1.0

    dead = P.parse_ping(PING_WINDOWS_DEAD)
    assert not dead.alive and dead.loss_pct == 100.0


def test_parse_load_handles_comma_separators():
    assert P.parse_load("load average: 0.31, 0.22, 0.09") == (0.31, 0.22, 0.09)
    assert P.parse_load("load averages: 1.00 2.00 3.00") == (1.0, 2.0, 3.0)
    assert P.parse_load("no load here") is None


def test_parse_proc_uptime():
    assert P.parse_proc_uptime("252.31 3921.55") == 252.31
    assert P.parse_proc_uptime("nonsense") is None


MDSTAT_HEALTHY = """Personalities : [raid1]
md0 : active raid1 sda1[0] sdb1[1]
      1048512 blocks [2/2] [UU]
"""
MDSTAT_DEGRADED = """Personalities : [raid1]
md0 : active raid1 sda1[0]
      1048512 blocks [2/1] [U_]
"""
MDSTAT_REBUILDING = """Personalities : [raid1]
md0 : active raid1 sda1[0] sdb1[1]
      1048512 blocks [2/2] [UU]
      [==>..................]  recovery = 12.3% (129000/1048512) finish=2.1min
"""


def test_mdstat_healthy_degraded_and_rebuilding():
    assert P.parse_mdstat(MDSTAT_HEALTHY)[0].healthy
    degraded = P.parse_mdstat(MDSTAT_DEGRADED)[0]
    assert not degraded.healthy and "[2/1]" in degraded.detail
    # A rebuild started by an unclean power-down is serving data but is not
    # healthy, and the operator has to know about it.
    rebuilding = P.parse_mdstat(MDSTAT_REBUILDING)[0]
    assert not rebuilding.healthy and "recovery" in rebuilding.detail


def test_mdstat_with_no_arrays():
    assert P.parse_mdstat("Personalities : [raid1]\nunused devices: <none>\n") == []


@pytest.mark.parametrize("text,expected", [
    ("SMART overall-health self-assessment test result: PASSED", True),
    ("SMART overall-health self-assessment test result: FAILED!", False),
    ("SMART Health Status: OK", True),
    ("SMART Health Status: FAILURE", False),
    ("Device does not support SMART", None),
])
def test_parse_smart_health(text, expected):
    assert P.parse_smart_health(text) is expected


def test_find_disk_errors_matches_real_kernel_messages():
    log = (
        "kernel: sd 0:0:0:0: [sda] tag#12 FAILED Result: hostbyte=DID_OK\n"
        "kernel: blk_update_request: I/O error, dev sda, sector 123456\n"
        "kernel: EXT4-fs error (device sda1): ext4_find_entry:1455\n"
        "systemd: Started Session 3 of user mu2edaq.\n"
    )
    hits = P.find_disk_errors(log)
    assert len(hits) == 2
    assert not any("Started Session" in hit for hit in hits)


def test_find_disk_errors_is_quiet_on_a_normal_boot():
    assert P.find_disk_errors("kernel: Linux version 5.14.0\n"
                              "systemd: Reached target Multi-User System.\n") == []


# ---------------------------------------------------------------------------
# ipmitool sel list (#10)
# ---------------------------------------------------------------------------

#: The layout ipmitool prints for ``sel list``: hex record id (``%4x``),
#: date, time, sensor, event, direction. ``Pre-Init`` replaces the date for
#: events logged before the BMC's clock was set -- the case after an outage --
#: and the time column is then a raw counter. The clear record is what a BMC
#: writes as record 1 after ``sel clear``.
SEL_LIST = """   1 | 09/18/2026 | 13:55:02 | Event Logging Disabled #0x07 | Log area reset/cleared | Asserted
   2 | Pre-Init  |0000000029| Power Supply #0x51 | Power Supply AC lost | Asserted
   3 | Pre-Init  |0000000031| System ACPI Power State #0x0a | S0/G0: working | Asserted
   4 | 09/18/2026 | 14:02:11 | Power Unit #0x01 | Power off/down | Asserted
  1a | 09/18/2026 | 14:07:45 | Memory #0x53 | Correctable ECC | Asserted
  1b | 09/18/2026 | 14:09:30 | Temperature #0x30 | Upper Critical going high | Asserted
  1c | 09/18/2026 | 14:10:00 | OEM record df | 040320
"""


def test_parse_sel_list_reads_ids_as_hex():
    rows = P.parse_sel_list(SEL_LIST)
    assert [r.record_id for r in rows] == ["1", "2", "3", "4", "1a", "1b", "1c"]
    assert rows[4].sensor == "Memory #0x53"
    assert rows[4].event == "Correctable ECC"
    assert rows[4].direction == "Asserted"


def test_parse_sel_list_keeps_pre_init_rows():
    # Pre-Init is precisely what a BMC logs across an outage.
    row = P.parse_sel_list(SEL_LIST)[1]
    assert row.date == "Pre-Init" and row.time == "0000000029"
    assert row.event == "Power Supply AC lost"


def test_parse_sel_list_accepts_a_row_without_a_direction():
    row = P.parse_sel_list(SEL_LIST)[-1]
    assert row.sensor == "OEM record df" and row.direction == ""


def test_parse_sel_list_recognises_the_clear_record():
    rows = P.parse_sel_list(SEL_LIST)
    assert rows[0].is_clear and not any(r.is_clear for r in rows[1:])


def test_parse_sel_list_skips_the_empty_log_message():
    assert P.parse_sel_list("SEL has no entries\n") == []
    assert P.parse_sel_list("") == []


def test_parse_sel_list_normalises_leading_zeros():
    rows = P.parse_sel_list("001A | 09/18/2026 | 14:07:45 | Memory #0x53 | "
                            "Correctable ECC | Asserted")
    assert rows[0].record_id == "1a"


def test_a_sel_fingerprint_ignores_the_timestamp():
    # BMC clocks are what an outage resets; the same record must fingerprint
    # the same however its time is rendered.
    a, = P.parse_sel_list("  1a | 09/18/2026 | 14:07:45 | Memory #0x53 | "
                          "Correctable ECC | Asserted")
    b, = P.parse_sel_list("  1a | 01/01/1970 | 00:00:07 | Memory  #0x53 | "
                          "correctable ECC | Asserted")
    assert a.fingerprint == b.fingerprint


def _sel(first, last, event="Power Supply AC lost"):
    return P.parse_sel_list("\n".join(
        f"{i:4x} | 09/18/2026 | 14:00:00 | Power Supply #0x51 | {event} | Asserted"
        for i in range(first, last + 1)))


def test_diff_sel_empty_baseline_and_empty_log():
    diff = P.diff_sel({}, [])
    assert diff.new == [] and not diff.cleared and not diff.possibly_truncated


def test_diff_sel_short_log_that_grew():
    diff = P.diff_sel(P.sel_baseline(_sel(1, 5)), _sel(1, 8))
    assert [e.record_id for e in diff.new] == ["6", "7", "8"]
    assert not diff.cleared and not diff.possibly_truncated


def test_diff_sel_full_rotated_tail():
    # 20 rows before, 20 rows after: the length comparison saw nothing here.
    before, after = _sel(0x41, 0x54), _sel(0x44, 0x57)
    assert len(before) == len(after) == 20
    diff = P.diff_sel(P.sel_baseline(before), after)
    assert [e.record_id for e in diff.new] == ["55", "56", "57"]
    assert not diff.cleared and not diff.possibly_truncated


def test_diff_sel_identical_history_is_not_new():
    rows = _sel(0x41, 0x54)
    diff = P.diff_sel(P.sel_baseline(rows), rows)
    assert diff.new == [] and not diff.cleared


def test_diff_sel_cleared_with_reused_ids():
    before = _sel(1, 6)
    after = _sel(1, 3, event="Power Supply Failure detected")
    diff = P.diff_sel(P.sel_baseline(before), after)
    assert diff.cleared and diff.reused == ["1", "2", "3"]
    assert len(diff.new) == 3


def test_diff_sel_cleared_by_a_new_clear_record():
    before = _sel(0x30, 0x34)
    after = P.parse_sel_list("   1 | 09/18/2026 | 15:00:00 | Event Logging "
                             "Disabled #0x07 | Log area reset/cleared | Asserted")
    diff = P.diff_sel(P.sel_baseline(before), after)
    assert diff.cleared and diff.reused == []


def test_diff_sel_a_pre_existing_clear_record_is_history():
    rows = P.parse_sel_list(SEL_LIST)
    assert not P.diff_sel(P.sel_baseline(rows), rows).cleared


def test_diff_sel_every_row_new_in_a_full_tail_may_be_truncated():
    diff = P.diff_sel(P.sel_baseline(_sel(1, 20)), _sel(0x30, 0x43))
    assert len(diff.new) == 20 and diff.possibly_truncated
    # A short reading cannot have lost anything off its front.
    assert not P.diff_sel({}, _sel(1, 3)).possibly_truncated


# Rows captured live on 2026-10-01 (ipmitool sel list, from mu2egateway01)
# from three BMC generations: a padded-id log, a decimal-looking id log and
# a 4-digit hex log. Replaces the modelled sample the #10 parser was built on.
LIVE_SEL = {
    "mu2e-trk-01": [
        "b240 | 09/11/2026 | 06:01:01 | System Event #0xff | Timestamp Clock Sync | Asserted",
        "b35f | 09/17/2026 | 05:02:18 | System Event | OEM System boot event | Asserted",
        "b62e | 10/01/2026 | 14:01:01 | System Event #0xff | Timestamp Clock Sync | Asserted",
    ],
    "mu2e-calo-02": [
        "   4 | 06/09/2016 | 10:22:20 | Session Audit #0xff |  | Asserted",
        "  29 | 10/10/2018 | 17:40:50 | Physical Security #0xaa | General Chassis intrusion () | Asserted",
        " 203 | 07/18/2024 | 18:53:47 | Physical Security #0xaa | General Chassis intrusion () | Deasserted",
    ],
    "mu2e-dl-01": [
        "   1 | 10/18/2024 | 12:38:41 | Unknown #0xff |  | Asserted",
        "   4 | 10/18/2024 | 12:41:49 | Power Supply #0xc8 | Presence detected () | Asserted",
        "  db | 09/17/2026 | 15:55:11 | Power Supply #0xc9 | Presence detected () | Asserted",
    ],
}


@pytest.mark.parametrize("node", sorted(LIVE_SEL))
def test_parse_sel_list_on_live_rows(node):
    from mu2edaq_power_recovery.checks.parsers import parse_sel_list
    rows = LIVE_SEL[node]
    entries = parse_sel_list("\n".join(rows))
    assert len(entries) == len(rows)
    assert [e.record_id for e in entries] == [format(int(r.split("|")[0], 16), "x")
                                              for r in rows]
    assert all(e.direction in ("Asserted", "Deasserted") for e in entries)


def test_a_capture_cut_mid_row_drops_only_the_fragments():
    """What the 64 KiB cap produced on a full trk-01 listing."""
    from mu2edaq_power_recovery.checks.parsers import parse_sel_list
    text = (LIVE_SEL["mu2e-trk-01"][0] + "\nb3c7 | 0\n...[output truncated]...\nsserted\n"
            + LIVE_SEL["mu2e-trk-01"][2])
    assert [e.record_id for e in parse_sel_list(text)] == ["b240", "b62e"]
