"""IPMI (and ECL) credentials from HashiCorp Vault.

Secrets live at ``td/scd/experiments/mu2e/`` on https://ssivault.fnal.gov:8200,
with the BMC credentials at ``td/scd/experiments/mu2e/ipmi``
(Project-Description.md).  The access pattern -- KV v2, a token cached at
~/.vault-token by ``vault login -method=ldap``, auto-login when the cached
token has gone -- is the same one mu2edaq-kerberos already uses, deliberately:
an operator who can read keytabs with that tool can read BMC credentials with
this one, with no second setup step.

Fallback
--------
``vault.allow_file_fallback`` lets the run continue from ``~/.ipmipasswd`` (the
file the existing mu2edaq-operations scripts read) when Vault itself is down.
That matters here more than it would elsewhere: a site-wide power event is
exactly when the Vault server may also be unavailable, and being unable to
reach Vault must not be the reason the DAQ cannot be powered back on.  The
fallback is recorded in the run so the report states which source was used.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from ..transport.local import LocalTransport

log = logging.getLogger(__name__)

try:
    import hvac
    from hvac.exceptions import Forbidden, InvalidPath, VaultError as _HvacError
except ImportError:  # pragma: no cover - only when hvac is genuinely absent
    hvac = None
    Forbidden = InvalidPath = _HvacError = Exception


class VaultError(RuntimeError):
    """Vault could not supply a credential the run needs."""


@dataclass
class IPMICredentials:
    """A BMC username/password and a note of where it came from."""

    username: str
    password: str
    source: str = "vault"

    def redacted(self) -> Dict[str, Any]:
        return {"username": self.username, "password": "<redacted>", "source": self.source}


class VaultCredentials:
    """Reads KV v2 secrets under the configured base path."""

    def __init__(self, settings: Any, local: Optional[LocalTransport] = None):
        self.settings = settings
        self.local = local or LocalTransport(default_timeout=60)
        self.addr: str = settings.get("vault.addr")
        self.kv_mount: str = settings.get("vault.kv_mount", "td")
        self.base_path: str = settings.get("vault.base_path", "scd/experiments/mu2e")
        self.timeout: int = int(settings.get("vault.timeout", 30))
        self.auto_login: bool = bool(settings.get("vault.auto_login", True))
        self._client = None
        self._token: Optional[str] = None

    # -- token handling ----------------------------------------------------

    def _cached_token(self) -> Optional[str]:
        token = os.environ.get("VAULT_TOKEN")
        if token:
            return token.strip()
        try:
            return (Path.home() / ".vault-token").read_text().strip() or None
        except OSError:
            return None

    def _interactive_login(self) -> Optional[str]:
        """Run ``vault login -method=ldap``, which caches to ~/.vault-token.

        We shell out rather than implementing the LDAP handshake: the CLI is
        what the site documents, it handles MFA prompts, and reimplementing it
        would give us a second thing to keep in step with SSI's configuration.
        """
        if not self.auto_login:
            return None
        log.info("no usable Vault token; running 'vault login'")
        print(f"\nA Vault token is needed for {self.addr}.")
        try:
            # inherit stdio so the CLI can prompt (password, MFA push, ...)
            import subprocess
            rc = subprocess.call(
                ["vault", "login", "-method=ldap", f"-address={self.addr}"],
                timeout=300,
            )
        except (OSError, Exception) as exc:  # vault CLI missing, or timeout
            log.warning("vault login could not be run: %s", exc)
            return None
        if rc != 0:
            log.warning("vault login exited %s", rc)
            return None
        return self._cached_token()

    def client(self):
        """An authenticated hvac client, or raise :class:`VaultError`."""
        if self._client is not None:
            return self._client
        if hvac is None:
            raise VaultError(
                "the 'hvac' package is not installed, so Vault cannot be read.\n"
                "Install it (pip install hvac) or configure "
                "vault.allow_file_fallback with a local password file."
            )
        token = self._cached_token()
        client = hvac.Client(url=self.addr, token=token, timeout=self.timeout)
        try:
            authenticated = bool(token) and client.is_authenticated()
        except Exception as exc:  # network failure, TLS problem
            raise VaultError(f"cannot reach Vault at {self.addr}: {exc}") from exc
        if not authenticated:
            token = self._interactive_login()
            if not token:
                raise VaultError(
                    f"no valid Vault token for {self.addr}.\n"
                    f"Run:  vault login -method=ldap -address={self.addr}"
                )
            client = hvac.Client(url=self.addr, token=token, timeout=self.timeout)
            try:
                if not client.is_authenticated():
                    raise VaultError("Vault rejected the token just obtained")
            except VaultError:
                raise
            except Exception as exc:
                raise VaultError(f"cannot reach Vault at {self.addr}: {exc}") from exc
        self._client = client
        self._token = token
        return client

    # -- reads -------------------------------------------------------------

    def read(self, relative_path: str) -> Dict[str, Any]:
        """Read one KV v2 secret under the base path and return its data."""
        path = f"{self.base_path.strip('/')}/{relative_path.strip('/')}"
        client = self.client()
        try:
            resp = client.secrets.kv.v2.read_secret_version(
                path=path, mount_point=self.kv_mount, raise_on_deleted_version=True)
        except TypeError:
            # hvac < 1.2 has no raise_on_deleted_version parameter.
            resp = client.secrets.kv.v2.read_secret_version(
                path=path, mount_point=self.kv_mount)
        except InvalidPath as exc:
            raise VaultError(f"no secret at {self.kv_mount}/{path}") from exc
        except Forbidden as exc:
            raise VaultError(
                f"permission denied reading {self.kv_mount}/{path}; "
                f"your Vault policy may not cover this path") from exc
        except Exception as exc:
            raise VaultError(f"error reading {self.kv_mount}/{path}: {exc}") from exc
        return (resp or {}).get("data", {}).get("data", {})

    # -- specific credentials ---------------------------------------------

    def ipmi(self) -> IPMICredentials:
        """BMC credentials, from Vault or (if permitted) the local fallback."""
        user_field = self.settings.get("vault.ipmi_user_field", "username")
        pass_field = self.settings.get("vault.ipmi_password_field", "password")
        rel = self.settings.get("vault.ipmi_path", "ipmi")

        try:
            data = self.read(rel)
        except VaultError as exc:
            log.warning("Vault read failed: %s", exc)
            fallback = self._file_fallback()
            if fallback:
                return fallback
            raise

        username = data.get(user_field)
        password = data.get(pass_field)
        if not password:
            # Tolerate a different field naming rather than failing outright:
            # the exact field names in the live secret could not be confirmed
            # from the repositories, so accept the obvious synonyms and say
            # which one was used.
            for alt in ("password", "pass", "passwd", "ipmi_password", "value"):
                if data.get(alt):
                    password = data[alt]
                    log.info("using Vault field %r for the IPMI password", alt)
                    break
        if not username:
            for alt in ("username", "user", "ipmi_user", "login"):
                if data.get(alt):
                    username = data[alt]
                    break
            username = username or self.settings.get("vault.fallback_user", "MU2E")
        if not password:
            fallback = self._file_fallback()
            if fallback:
                return fallback
            raise VaultError(
                f"the secret at {self.kv_mount}/{self.base_path}/{rel} has no "
                f"password field (looked for {pass_field!r}); "
                f"present fields: {', '.join(sorted(data)) or '(none)'}"
            )
        return IPMICredentials(username=str(username), password=str(password),
                               source=f"vault:{self.kv_mount}/{self.base_path}/{rel}")

    def _file_fallback(self) -> Optional[IPMICredentials]:
        """~/.ipmipasswd, the file the existing operations scripts already use."""
        if not self.settings.get("vault.allow_file_fallback", True):
            return None
        path_value = self.settings.get("vault.fallback_password_file", "~/.ipmipasswd")
        path = Path(str(path_value)).expanduser()
        try:
            password = path.read_text().strip()
        except OSError:
            return None
        if not password:
            return None
        log.warning("using IPMI password from %s because Vault was unavailable", path)
        return IPMICredentials(
            username=str(self.settings.get("vault.fallback_user", "MU2E")),
            password=password,
            source=f"file:{path}",
        )

    def ecl(self) -> Dict[str, Any]:
        """ECL API credentials for phase 4; empty dict when unavailable."""
        rel = self.settings.get("ecl.credential_path", "ecl")
        try:
            return self.read(rel)
        except VaultError as exc:
            log.warning("no ECL credentials from Vault: %s", exc)
            return {}
