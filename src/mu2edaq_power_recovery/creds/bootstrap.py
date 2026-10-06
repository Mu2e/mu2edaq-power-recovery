"""The credential bootstrap shared by the recovery run and its diagnostics.

A diagnostic is only worth running if it reproduces what the run does. Before
this module existed each entry point assembled its own credentials: the run
called ``KerberosManager.prepare()`` and handed the manager to the ssh
factory; ``mu2e-ssh-probe`` built a manager but never prepared it, so a
configured principal was *reported* while ssh used the ambient cache; and
``mu2e-ipmi-tool`` built its factory with no manager at all. Each could then
succeed or fail where the real run would not -- the confusion the diagnostics
exist to remove.

:func:`credential_session` is now the one way in. It checks the ambient
cache, acquires the designated principals, optionally mints the service
fallbacks before any worker thread starts, builds the :class:`SSHFactory`,
and destroys the run's private caches on the way out whatever the exit path:
normal return, exception, or KeyboardInterrupt (which is also what SIGTERM
becomes once ``cli.install_sigterm_handler`` is installed).
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

from ..transport.local import LocalTransport
from ..transport.ssh import SSHFactory
from .kerberos import KerberosManager, TicketInfo

log = logging.getLogger(__name__)


@dataclass
class CredentialSession:
    """What :func:`credential_session` hands its caller."""

    #: None when the session was opened with ``chain=False``: ssh then uses
    #: the ambient ticket and its own default login, and nothing else.
    kerberos: Optional[KerberosManager]
    factory: SSHFactory
    #: role -> ticket, from KerberosManager.prepare(); empty when not prepared.
    tickets: Dict[str, TicketInfo] = field(default_factory=dict)
    #: Service identities minted up front by warm_fallbacks().
    warmed: List[str] = field(default_factory=list)
    #: The ambient-cache warning, when there is one. The caller decides how
    #: loudly to say it; it is also the first entry of *notes*.
    warning: Optional[str] = None
    #: Operator-facing notes gathered while opening the session.
    notes: List[str] = field(default_factory=list)


@contextmanager
def credential_session(settings: Any, topology: Any,
                       local: Optional[LocalTransport] = None, *,
                       chain: bool = True, prepare: bool = True,
                       warm: bool = True,
                       kerberos: Optional[KerberosManager] = None
                       ) -> Iterator[CredentialSession]:
    """Open the credentials a run -- or a faithful diagnostic -- needs.

    *chain* False skips Kerberos entirely (an ambient-only probe). *prepare*
    False is a describe-only session: nothing is acquired or minted, and
    :attr:`KerberosManager.minting` is switched off so the factory's chains
    carry only credentials that already exist. *warm* mints the service
    fallbacks up front, and is honoured only when a prepared session has
    ``kerberos.use_service_keytabs`` on.

    ``KerberosError`` from prepare() propagates to the caller, after cleanup.
    *kerberos* injects a manager, for tests.
    """
    manager: Optional[KerberosManager] = None
    try:
        session_notes: List[str] = []
        warning: Optional[str] = None
        tickets: Dict[str, TicketInfo] = {}
        warmed: List[str] = []
        if chain:
            manager = kerberos or KerberosManager(settings, local=local)
            # Before anything is attempted: is the ticket we are about to use
            # actually the operator's? On macOS the credential cache is a
            # collection, and a service identity minted into it can become
            # the default -- after which every login runs as that identity and
            # is refused, with nothing in the ssh error to say why.
            warning = manager.ambient_warning()
            if warning:
                log.warning("%s", warning)
                session_notes.append(warning)
            if prepare:
                tickets = manager.prepare()
                if warm and settings.get("kerberos.use_service_keytabs", True):
                    warmed = manager.warm_fallbacks()
                if manager.fallbacks_disabled is not None:
                    session_notes.append(manager.fallbacks_disabled.note())
            else:
                manager.minting = False
        # The factory needs the Kerberos manager: it is what supplies the
        # credential chain, and -- more basically -- what puts KRB5CCNAME
        # into the ssh environment, without which a designated principal is
        # minted into a private cache that ssh never looks at.
        factory = SSHFactory(settings, topology, local=local, kerberos=manager)
        yield CredentialSession(kerberos=manager, factory=factory,
                                tickets=tickets, warmed=warmed,
                                warning=warning, notes=session_notes)
    finally:
        if manager is not None:
            manager.cleanup()
