"""Vault credential resolution.

The case these guard against is the one that actually happened: the IPMI
credentials live at ``ipmi/config``, not ``ipmi``. In KV v2 a folder read
returns nothing at all, which is indistinguishable from an empty secret unless
something goes and looks.
"""
from __future__ import annotations

import pytest

from mu2edaq_power_recovery.creds.vault import VaultCredentials, VaultError


class FakeKvV2:
    """The slice of hvac's KV v2 surface that VaultCredentials uses.

    *secrets* maps a full path to its data; *folders* maps a path to the keys
    directly under it, with a trailing slash marking a sub-folder, exactly as
    Vault reports them.
    """

    def __init__(self, secrets, folders):
        self.secrets = secrets
        self.folders = folders

    def read_secret_version(self, path, mount_point=None, **kwargs):
        if path not in self.secrets:
            from hvac.exceptions import InvalidPath
            raise InvalidPath(f"no secret at {path}")
        return {"data": {"data": self.secrets[path]}}

    def list_secrets(self, path, mount_point=None):
        if path not in self.folders:
            from hvac.exceptions import InvalidPath
            raise InvalidPath(f"no folder at {path}")
        return {"data": {"keys": self.folders[path]}}


class FakeClient:
    def __init__(self, kv):
        self.secrets = type("S", (), {"kv": type("KV", (), {"v2": kv})()})()


@pytest.fixture
def vault(settings, monkeypatch, tmp_path):
    """A VaultCredentials wired to a fake KV tree matching the real one."""
    # The real layout: 'ipmi' is a folder, 'ipmi/config' is the secret.
    kv = FakeKvV2(
        secrets={
            "scd/experiments/mu2e/ipmi/config": {"username": "MU2E",
                                                 "password": "s3cret"},
            "scd/experiments/mu2e/ecl": {"user": "mu2edaq", "key": "abc"},
        },
        folders={
            "scd/experiments/mu2e": ["ipmi/", "ecl", "kerberos/"],
            "scd/experiments/mu2e/ipmi": ["config"],
        },
    )
    # No local fallback, so a failure is visible rather than papered over.
    settings.set("vault.allow_file_fallback", False)
    client = VaultCredentials(settings)
    monkeypatch.setattr(client, "client", lambda: FakeClient(kv))
    return client


def test_the_configured_default_path_is_the_secret_not_the_folder(settings):
    # The bug this fixes: 'ipmi' is a folder; the secret is 'ipmi/config'.
    assert settings.get("vault.ipmi_path") == "ipmi/config"


def test_reading_the_secret_works(vault):
    creds = vault.ipmi()
    assert creds.username == "MU2E"
    assert creds.password == "s3cret"
    assert creds.source.endswith("ipmi/config")


def test_reading_the_folder_says_what_is_inside_it(vault):
    # Pointing at the folder must not just fail; it must name the secret.
    vault.settings.set("vault.ipmi_path", "ipmi")
    with pytest.raises(VaultError) as excinfo:
        vault.ipmi()
    message = str(excinfo.value)
    assert "no secret at" in message
    assert "ipmi/config" in message, \
        "a folder read must report the secret inside it, not just fail"


def test_listing_a_folder(vault):
    assert vault.list("ipmi") == ["config"]
    assert "ecl" in vault.list("")


def test_find_secrets_walks_into_subfolders(vault):
    found = vault.find_secrets("")
    assert "ipmi/config" in found     # descended into the ipmi/ folder
    assert "ecl" in found             # and kept the leaf secret


def test_find_secrets_stops_at_the_depth_limit(vault):
    # kerberos/ has no listing in the fake tree, so it yields nothing rather
    # than raising -- discovery must never become a second failure.
    assert vault.find_secrets("", depth=1) == ["ipmi/", "ecl", "kerberos/"]


def test_listing_an_absent_path_is_empty_not_an_error(vault):
    assert vault.list("nowhere") == []


def test_synonym_fields_are_accepted(vault, monkeypatch):
    # The live secret uses username/password, but it is maintained outside
    # this repository; the synonyms are the fallback if it is ever re-keyed.
    kv = FakeKvV2(
        secrets={"scd/experiments/mu2e/ipmi/config": {"user": "MU2E",
                                                      "pass": "s3cret"}},
        folders={},
    )
    monkeypatch.setattr(vault, "client", lambda: FakeClient(kv))
    creds = vault.ipmi()
    assert creds.username == "MU2E" and creds.password == "s3cret"


def test_a_secret_with_no_password_field_is_an_error(vault, monkeypatch):
    kv = FakeKvV2(
        secrets={"scd/experiments/mu2e/ipmi/config": {"note": "moved"}},
        folders={},
    )
    monkeypatch.setattr(vault, "client", lambda: FakeClient(kv))
    with pytest.raises(VaultError) as excinfo:
        vault.ipmi()
    # The error names the fields that *are* present, so the fix is obvious.
    assert "note" in str(excinfo.value)


def test_the_file_fallback_is_used_when_vault_fails(vault, tmp_path):
    # A site-wide power event is exactly when Vault may also be down.
    password_file = tmp_path / "ipmipasswd"
    password_file.write_text("fallback-secret\n")
    vault.settings.set("vault.allow_file_fallback", True)
    vault.settings.set("vault.fallback_password_file", str(password_file))
    vault.settings.set("vault.ipmi_path", "ipmi")     # the folder: read fails
    creds = vault.ipmi()
    assert creds.password == "fallback-secret"
    assert creds.source.startswith("file:")


def test_credentials_redact_the_password(vault):
    redacted = vault.ipmi().redacted()
    assert redacted["password"] == "<redacted>"
    assert redacted["username"] == "MU2E"
