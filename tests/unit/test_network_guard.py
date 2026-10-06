"""Meta-tests: the autouse network guard in ``tests/conftest.py`` must bite.

Each test drives one path production code uses to reach the outside and
asserts the guard blocked it.  A block raises ``pytest.fail.Exception`` (a
BaseException) and is recorded; ``no_real_network.expect`` consumes the record
so the test's own teardown check does not fail it a second time.
"""
import socket
import subprocess

import pytest

Blocked = pytest.fail.Exception


def test_the_guard_catches_a_real_ssh_attempt(settings, topology, no_real_network):
    from mu2edaq_power_recovery.transport import SSHFactory
    factory = SSHFactory(settings, topology)
    transport = factory.for_host("mu2egateway01.fnal.gov", direct=True)
    with pytest.raises(Blocked, match="tried to run 'ssh'"):
        transport.run("true")
    no_real_network.expect("'ssh'")


def test_the_guard_catches_a_real_ping(no_real_network):
    """Pinging a DAQ host is contacting the DAQ network.

    Left off the blocked list, this was how a *simulated* run reached the
    cluster for two weeks: gateway nodes are probed from the workstation's own
    transport, so ping.lab really did ping mu2egateway01, and the phase tests
    passed or failed according to whether it answered that second.
    """
    from mu2edaq_power_recovery.transport import LocalTransport

    with pytest.raises(Blocked, match="tried to run 'ping'"):
        LocalTransport().run("ping -c 1 -W 1 -q mu2egateway01.fnal.gov")
    no_real_network.expect("'ping'")


@pytest.mark.parametrize("argv", [
    ["klist"],
    ["klist", "-l"],
    ["/usr/bin/kswitch", "-p", "mu2edaq@FNAL.GOV"],
    ["get-kerberos-ticket", "mu2edaq"],
    ["vault-client", "read"],
    ["env", "KRB5CCNAME=FILE:/tmp/x", "kinit", "-k"],
    ["/bin/sh", "-c", "cd /tmp && ipmitool -I lanplus power status"],
])
def test_direct_credential_subprocesses_are_blocked(argv, no_real_network):
    # creds/ticketsource.py calls subprocess.run itself, not LocalTransport.
    with pytest.raises(Blocked):
        subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    no_real_network.expect("tried to run")


def test_a_shell_string_is_looked_into(no_real_network):
    with pytest.raises(Blocked, match="'kdestroy'"):
        subprocess.call("true; kdestroy -A", shell=True)
    no_real_network.expect("'kdestroy'")


def test_vault_login_subprocess_is_blocked(settings, no_real_network):
    from mu2edaq_power_recovery.creds.vault import VaultCredentials
    vault = VaultCredentials(settings)
    vault.auto_login = True
    with pytest.raises(Blocked, match="'vault'"):
        vault._interactive_login()
    no_real_network.expect("'vault'")


def test_hvac_https_is_blocked_even_through_except_exception(settings, monkeypatch,
                                                            no_real_network):
    """VaultCredentials.client() catches Exception around every hvac call.

    An AssertionError-based guard would be converted into a VaultError there and
    the test would pass having tried to reach Vault; the BaseException gets out.
    """
    pytest.importorskip("hvac")
    from mu2edaq_power_recovery.creds.vault import VaultCredentials
    settings.set("vault.addr", "https://10.20.30.40:8200")
    vault = VaultCredentials(settings)
    monkeypatch.setattr(vault, "_cached_token", lambda: "s.not-a-real-token")
    with pytest.raises(Blocked, match="connect to"):
        vault.client()
    no_real_network.expect("connect to")


def test_the_gateway_tcp_prefilter_is_blocked(settings, topology, no_real_network):
    """SSHFactory._select_gateway sweeps TCP/22 inside ``except Exception``."""
    from mu2edaq_power_recovery.transport import SSHFactory
    factory = SSHFactory(settings, topology)
    with pytest.raises(Blocked, match="sweep"):
        factory._select_gateway("mc2")
    no_real_network.expect("sweep")


def test_a_non_loopback_socket_is_blocked(no_real_network):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(Blocked, match="connect to"):
            sock.connect_ex(("10.226.9.1", 22))
    finally:
        sock.close()
    no_real_network.expect("connect to")


def test_loopback_and_test_net_stay_usable():
    # The sweep tests rely on these; the guard must not get in their way.
    from mu2edaq_power_recovery import sweep
    assert sweep.sweep(["127.0.0.1", "192.0.2.1", "nothing.invalid"],
                       port=9, timeout_ms=200)


def test_git_and_python_still_run():
    # version.py and the self-update tests shell out to git; test_docs runs
    # the interpreter.
    subprocess.run(["git", "--version"], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["python3", "-c", "pass"], check=True)


def test_a_swallowed_block_still_fails_the_test(no_real_network):
    """If code catches even BaseException, the teardown check reports it."""
    try:
        subprocess.run(["klist"])
    except BaseException:  # simulating over-broad production code
        pass
    assert no_real_network.violations, "the block was not recorded"
    no_real_network.expect("'klist'")


@pytest.mark.allow_network
def test_the_opt_out_marker_disables_the_guard():
    # Nothing blocked is run here; the point is that the guard is not armed.
    assert subprocess.Popen.__init__.__name__ != "guarded_popen"
