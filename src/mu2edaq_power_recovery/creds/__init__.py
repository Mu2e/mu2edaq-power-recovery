"""Credentials: Kerberos tickets for SSH, Vault secrets for IPMI.

Neither module ever writes a secret to disk, to a log line, or to a command
line.  Passwords typed by the operator go straight to ``kinit`` on stdin;
BMC passwords fetched from Vault go to the gateway on stdin.
"""
from .kerberos import KerberosManager, KerberosError, TicketInfo
from .vault import VaultCredentials, VaultError, IPMICredentials

__all__ = [
    "KerberosManager", "KerberosError", "TicketInfo",
    "VaultCredentials", "VaultError", "IPMICredentials",
]
