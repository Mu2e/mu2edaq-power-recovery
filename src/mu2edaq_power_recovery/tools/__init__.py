"""Stand-alone diagnostics.

Each is a small tool that does one thing an operator needs when the driver
itself is misbehaving, or when they want to check one fact without running a
phase: what the inventory is, whether a BMC answers, what ssh command is
actually being issued, and whether the Vault path holds what it should.

They share the project's configuration and topology, so what they report is
what a real run would do -- a diagnostic that consults different settings than
the thing it is diagnosing is worse than none.
"""
