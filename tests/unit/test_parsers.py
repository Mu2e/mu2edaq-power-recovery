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
