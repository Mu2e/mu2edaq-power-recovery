def test_the_guard_catches_a_real_ssh_attempt(settings, topology):
    """Meta-test: the network guard must actually bite."""
    import pytest
    from mu2edaq_power_recovery.transport import SSHFactory
    factory = SSHFactory(settings, topology)
    transport = factory.for_host("mu2egateway01.fnal.gov", direct=True)
    with pytest.raises(AssertionError, match="tried to run 'ssh' for real"):
        transport.run("true")
