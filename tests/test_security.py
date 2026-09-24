"""Tests for field-level encryption and RBAC."""

from __future__ import annotations

import pytest

from brain.security.encryption import FieldEncryptor
from brain.security.rbac import Permission, RBACPolicy


class TestFieldEncryptor:
    def _make_encryptor(self):
        return FieldEncryptor(FieldEncryptor.generate_master_key())

    def test_encrypt_decrypt_roundtrip(self):
        enc = self._make_encryptor()
        plaintext = "sk-todoist-api-token-12345"
        tenant_id = "tn_abc123"
        encrypted = enc.encrypt(plaintext, tenant_id)
        decrypted = enc.decrypt(encrypted, tenant_id)
        assert decrypted == plaintext

    def test_different_tenants_different_ciphertext(self):
        enc = self._make_encryptor()
        plaintext = "shared-secret"
        ct1 = enc.encrypt(plaintext, "tenant_a")
        ct2 = enc.encrypt(plaintext, "tenant_b")
        assert ct1 != ct2

    def test_wrong_tenant_fails(self):
        enc = self._make_encryptor()
        encrypted = enc.encrypt("secret", "tenant_a")
        with pytest.raises(Exception):
            enc.decrypt(encrypted, "tenant_b")

    def test_tampered_ciphertext_fails(self):
        enc = self._make_encryptor()
        encrypted = enc.encrypt("secret", "tenant_a")
        # Flip a character in the middle
        chars = list(encrypted)
        mid = len(chars) // 2
        chars[mid] = "A" if chars[mid] != "A" else "B"
        tampered = "".join(chars)
        with pytest.raises(Exception):
            enc.decrypt(tampered, "tenant_a")

    def test_generate_master_key(self):
        key = FieldEncryptor.generate_master_key()
        assert isinstance(key, str)
        assert len(key) > 30

    def test_explicit_master_key(self):
        key = FieldEncryptor.generate_master_key()
        enc = FieldEncryptor(master_key=key)
        encrypted = enc.encrypt("test", "t1")
        decrypted = enc.decrypt(encrypted, "t1")
        assert decrypted == "test"

    def test_derive_tenant_key_deterministic(self):
        enc = self._make_encryptor()
        k1 = enc.derive_tenant_key("tn_001")
        k2 = enc.derive_tenant_key("tn_001")
        assert k1 == k2

    def test_derive_tenant_key_different_tenants(self):
        enc = self._make_encryptor()
        k1 = enc.derive_tenant_key("tn_001")
        k2 = enc.derive_tenant_key("tn_002")
        assert k1 != k2


class TestRBACPolicy:
    def test_free_has_read_gold(self):
        policy = RBACPolicy()
        assert policy.has_permission("free", Permission.READ_GOLD)

    def test_free_lacks_premium_llm(self):
        policy = RBACPolicy()
        assert not policy.has_permission("free", Permission.USE_PREMIUM_LLM)

    def test_standard_has_llm(self):
        policy = RBACPolicy()
        assert policy.has_permission("standard", Permission.USE_LLM)

    def test_premium_has_premium_llm(self):
        policy = RBACPolicy()
        assert policy.has_permission("premium", Permission.USE_PREMIUM_LLM)

    def test_admin_has_everything(self):
        policy = RBACPolicy()
        for perm in Permission:
            assert policy.has_permission("admin", perm)

    def test_require_permission_raises(self):
        policy = RBACPolicy()
        with pytest.raises(PermissionError):
            policy.require_permission("free", Permission.USE_PREMIUM_LLM)

    def test_require_permission_passes(self):
        policy = RBACPolicy()
        policy.require_permission("admin", Permission.ADMIN_ALL)

    def test_unknown_role(self):
        policy = RBACPolicy()
        assert not policy.has_permission("unknown_role", Permission.READ_GOLD)

    def test_custom_role(self):
        custom = {"viewer": {Permission.READ_GOLD, Permission.VIEW_TENANT}}
        policy = RBACPolicy(custom_roles=custom)
        assert policy.has_permission("viewer", Permission.READ_GOLD)
        assert not policy.has_permission("viewer", Permission.WRITE_BRONZE)

    def test_available_roles(self):
        roles = RBACPolicy.available_roles()
        assert "free" in roles
        assert "admin" in roles
        assert "vip" in roles
        assert "demo" in roles
        assert len(roles) == 6

    def test_demo_has_rag_permission(self):
        policy = RBACPolicy()
        assert policy.has_permission("demo", Permission.USE_RAG)

    def test_demo_has_local_llm_permission(self):
        policy = RBACPolicy()
        assert policy.has_permission("demo", Permission.USE_LOCAL_LLM)

    def test_demo_lacks_admin(self):
        policy = RBACPolicy()
        assert not policy.has_permission("demo", Permission.ADMIN_ALL)
        assert not policy.has_permission("demo", Permission.MANAGE_TENANT)
        assert not policy.has_permission("demo", Permission.MANAGE_USERS)

    def test_vip_has_rag(self):
        policy = RBACPolicy()
        assert policy.has_permission("vip", Permission.USE_RAG)

    def test_premium_lacks_rag(self):
        # Native RAG is VIP-only — premium keeps NotebookLM export.
        policy = RBACPolicy()
        assert not policy.has_permission("premium", Permission.USE_RAG)

    def test_free_lacks_rag(self):
        policy = RBACPolicy()
        assert not policy.has_permission("free", Permission.USE_RAG)

    def test_free_lacks_local_llm(self):
        policy = RBACPolicy()
        assert not policy.has_permission("free", Permission.USE_LOCAL_LLM)
