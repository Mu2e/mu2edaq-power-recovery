def test_the_guard_catches_a_real_ssh_attempt(settings, topology):
    """Meta-test: the network guard must actually bite."""
    import pytest
    from mu2edaq_power_recovery.transport import SSHFactory
    factory = SSHFactory(settings, topology)
    transport = factory.for_host("mu2egateway01.fnal.gov", direct=True)
    with pytest.raises(AssertionError, match="tried to run 'ssh' for real"):
        transport.run("true")


def test_the_guard_catches_a_real_ping():
    """Meta-test: pinging a DAQ host is contacting the DAQ network.

    Left off the blocked list, this was how a *simulated* run reached the
    cluster for two weeks: gateway nodes are probed from the workstation's own
    transport, so ping.lab really did ping mu2egateway01, and the phase tests
    passed or failed according to whether it answered that second.
    """
    import pytest
    from mu2edaq_power_recovery.transport import LocalTransport

    with pytest.raises(AssertionError, match="tried to run 'ping' for real"):
        LocalTransport().run("ping -c 1 -W 1 -q mu2egateway01.fnal.gov")
