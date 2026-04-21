"""End-to-end credential-in-transit tests.

These tests exercise the full path a real tenant credential travels:

    1. Tenant is created and bound to a Google OAuth `sub` claim.
    2. The tenant stores a third-party credential (Todoist / Notion /
       Gmail / LiteLLM / Gemini) via ``TenantManager.update_config``.
    3. Only the ciphertext is persisted on disk.
    4. ``QueueWorker._build_tenant_settings`` decrypts per-request into
       an ephemeral ``Settings`` object.
    5. The Settings object carries the plaintext in RAM for exactly the
       duration of the handler call; after GC, no ciphertext or
       plaintext remains on disk in cleartext form.

Coverage goals:
    - Every credential-bearing field round-trips correctly.
    - Tenant A cannot read tenant B's credentials even with identical
      field names (salt isolation).
    - Ciphertext on disk never contains the plaintext substring.
    - Switching `owner_google_sub` (simulating an attacker tampering
      with the tenant row) makes decryption fail.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brain.config import Settings
from brain.queue.tasks import TaskQueue
from brain.queue.worker import QueueWorker
from brain.security.encryption import (
    CredentialEncryptor,
    CredentialNotDecryptable,
    FieldEncryptor,
)
from brain.storage.local import LocalStorageBackend
from brain.tenant.manager import TenantManager
from tests._fakes import fake_api_key, fake_token


# Each field name in the Settings/TenantConfig model and the
# matching credential plaintext we will round-trip through the vault.
_CREDENTIAL_FIELDS = [
    ("todoist_token", "todoist_api_token", "todoist_api_token_encrypted",
     lambda: fake_token("todoist_", 40)),
    ("notion_token", "notion_api_token", "notion_api_token_encrypted",
     lambda: fake_token("ntn_", 46)),
    ("gmail_imap_password", "gmail_imap_password", "gmail_imap_password_encrypted",
     lambda: fake_token("gmail_", 32)),
    ("litellm_proxy_token", "litellm_proxy_token",
     "litellm_proxy_token_encrypted", lambda: fake_token("sk-", 32)),
    ("gemini_api_key", "gemini_api_key", "gemini_api_key_encrypted",
     lambda: fake_api_key("AIzaSy-", 32)),
]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    storage = LocalStorageBackend(str(tmp_path / "data"))
    master = FieldEncryptor.generate_master_key()
    pepper = CredentialEncryptor.generate_pepper()
    enc = FieldEncryptor(master)
    cred = CredentialEncryptor(pepper)
    monkeypatch.setenv("VELAFLOW_MASTER_KEY", master)
    monkeypatch.setenv("VELAFLOW_CREDENTIAL_PEPPER", pepper)
    mgr = TenantManager(storage, enc, cred)
    return {
        "storage": storage,
        "mgr": mgr,
        "data_dir": tmp_path / "data",
        "pepper": pepper,
    }


class TestCredentialInTransit:
    """For every third-party credential: tenant → vault → worker Settings."""

    @pytest.mark.parametrize(
        "kw,field_name,cfg_attr,gen",
        _CREDENTIAL_FIELDS,
        ids=[f[0] for f in _CREDENTIAL_FIELDS],
    )
    def test_roundtrip_via_tenant_manager(self, env, kw, field_name, cfg_attr, gen) -> None:
        mgr: TenantManager = env["mgr"]
        plaintext = gen()
        tenant = mgr.create_tenant("Alice", "alice@example.com")
        mgr.bind_owner_sub(tenant.tenant_id, "google-oauth-sub-alice")

        mgr.update_config(tenant.tenant_id, **{kw: plaintext})
        reloaded = mgr.get_tenant(tenant.tenant_id)
        ciphertext = getattr(reloaded.config, cfg_attr)

        assert ciphertext, f"{cfg_attr} was not stored"
        assert plaintext not in ciphertext, "plaintext leaked into ciphertext"

        recovered = mgr.decrypt_credential(reloaded, ciphertext, field_name)
        assert recovered == plaintext

    @pytest.mark.parametrize(
        "kw,field_name,cfg_attr,gen",
        _CREDENTIAL_FIELDS,
        ids=[f[0] for f in _CREDENTIAL_FIELDS],
    )
    def test_plaintext_never_on_disk(self, env, kw, field_name, cfg_attr, gen) -> None:
        """Every file written under the data dir must not contain the plaintext.

        This is a byte-level guarantee: if any file in the persistence
        tree contains the secret substring, the test fails.
        """
        mgr: TenantManager = env["mgr"]
        plaintext = gen()
        tenant = mgr.create_tenant("Bob", "bob@example.com")
        mgr.bind_owner_sub(tenant.tenant_id, "google-oauth-sub-bob")
        mgr.update_config(tenant.tenant_id, **{kw: plaintext})

        data_dir: Path = env["data_dir"]
        needle = plaintext.encode()
        for path in data_dir.rglob("*"):
            if not path.is_file():
                continue
            blob = path.read_bytes()
            assert needle not in blob, (
                f"plaintext found on disk at {path.relative_to(data_dir)}"
            )

    def test_cross_tenant_isolation(self, env) -> None:
        """Tenant A's ciphertext must not decrypt under tenant B."""
        mgr: TenantManager = env["mgr"]
        pt_a = fake_token("todoist_", 40)
        pt_b = fake_token("todoist_", 40)

        ta = mgr.create_tenant("A", "a@x.com")
        mgr.bind_owner_sub(ta.tenant_id, "sub-a")
        mgr.update_config(ta.tenant_id, todoist_token=pt_a)

        tb = mgr.create_tenant("B", "b@x.com")
        mgr.bind_owner_sub(tb.tenant_id, "sub-b")
        mgr.update_config(tb.tenant_id, todoist_token=pt_b)

        ra = mgr.get_tenant(ta.tenant_id)
        rb = mgr.get_tenant(tb.tenant_id)

        # Each tenant reads its own secret correctly.
        assert mgr.decrypt_credential(
            ra, ra.config.todoist_api_token_encrypted, "todoist_api_token"
        ) == pt_a
        assert mgr.decrypt_credential(
            rb, rb.config.todoist_api_token_encrypted, "todoist_api_token"
        ) == pt_b

        # A ciphertext attached to the wrong tenant is rejected.
        with pytest.raises(CredentialNotDecryptable):
            mgr.decrypt_credential(
                rb, ra.config.todoist_api_token_encrypted, "todoist_api_token"
            )

    def test_owner_sub_tamper_detection(self, env) -> None:
        """If the stored row's owner_sub is changed, decryption must fail."""
        mgr: TenantManager = env["mgr"]
        pt = fake_token("ntn_", 46)
        t = mgr.create_tenant("Carol", "c@x.com")
        mgr.bind_owner_sub(t.tenant_id, "sub-carol")
        mgr.update_config(t.tenant_id, notion_token=pt)

        loaded = mgr.get_tenant(t.tenant_id)
        loaded.owner_google_sub = "sub-attacker"
        with pytest.raises(CredentialNotDecryptable):
            mgr.decrypt_credential(
                loaded, loaded.config.notion_api_token_encrypted, "notion_api_token"
            )

    def test_worker_build_settings_decrypts_all_fields(self, env, tmp_path) -> None:
        """The worker's per-request Settings builder decrypts every field."""
        mgr: TenantManager = env["mgr"]
        values = {kw: gen() for kw, _, _, gen in _CREDENTIAL_FIELDS}
        t = mgr.create_tenant("Dan", "d@x.com")
        mgr.bind_owner_sub(t.tenant_id, "sub-dan")
        mgr.update_config(t.tenant_id, **values)
        reloaded = mgr.get_tenant(t.tenant_id)

        queue = TaskQueue()
        worker = QueueWorker(queue, env["storage"], Settings())
        settings = worker._build_tenant_settings(reloaded)  # noqa: SLF001

        assert settings.todoist_api_token == values["todoist_token"]
        assert settings.notion_api_token == values["notion_token"]
        assert settings.gmail_imap_password == values["gmail_imap_password"]
        assert settings.litellm_proxy_token == values["litellm_proxy_token"]
        assert settings.google_ai_api_key == values["gemini_api_key"]

    def test_unbound_tenant_yields_no_credentials(self, env, tmp_path) -> None:
        """A tenant that never completed OAuth has no plaintext secrets."""
        mgr: TenantManager = env["mgr"]
        t = mgr.create_tenant("Eve", "e@x.com")
        # Intentionally skip bind_owner_sub. update_config with credentials
        # must be refused.
        with pytest.raises(RuntimeError, match="owner_google_sub"):
            mgr.update_config(t.tenant_id, todoist_token="ignored")

        reloaded = mgr.get_tenant(t.tenant_id)
        queue = TaskQueue()
        worker = QueueWorker(queue, env["storage"], Settings(todoist_api_token=""))
        settings = worker._build_tenant_settings(reloaded)  # noqa: SLF001
        # Nothing was stored, nothing can be leaked.
        assert settings.todoist_api_token == ""
        assert settings.notion_api_token == ""
