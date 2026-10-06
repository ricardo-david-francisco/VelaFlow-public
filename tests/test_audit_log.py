"""Tests for encrypted tamper-evident audit logging."""

from __future__ import annotations

import json

import pytest

from brain.security.audit_log import AuditEntry, EncryptedAuditLog
from brain.security.encryption import FieldEncryptor
from brain.storage.local import LocalStorageBackend


@pytest.fixture()
def storage(tmp_path):
    return LocalStorageBackend(tmp_path)


@pytest.fixture()
def encryptor():
    return FieldEncryptor(FieldEncryptor.generate_master_key())


@pytest.fixture()
def audit_log(storage, encryptor):
    return EncryptedAuditLog(storage, encryptor)


class TestAuditEntry:
    """Verify audit entry creation and hashing."""

    def test_entry_has_chain_hash(self) -> None:
        entry = AuditEntry(
            tenant_id="t1",
            action="login",
            resource="/api/v1/auth",
        )
        assert entry.chain_hash
        assert len(entry.chain_hash) == 64  # SHA-256 hex

    def test_entry_with_previous_hash(self) -> None:
        first = AuditEntry(tenant_id="t1", action="action1")
        second = AuditEntry(
            tenant_id="t1",
            action="action2",
            previous_hash=first.chain_hash,
        )
        assert second.previous_hash == first.chain_hash
        assert second.chain_hash != first.chain_hash

    def test_to_dict_roundtrip(self) -> None:
        entry = AuditEntry(
            tenant_id="t1",
            action="pipeline_run",
            resource="pipeline/daily",
            detail={"status": "success"},
            user_id="user1",
        )
        d = entry.to_dict()
        restored = AuditEntry.from_dict(d)
        assert restored.tenant_id == entry.tenant_id
        assert restored.action == entry.action
        assert restored.chain_hash == entry.chain_hash

    def test_hash_changes_on_tamper(self) -> None:
        entry = AuditEntry(
            tenant_id="t1",
            action="delete_data",
            detail={"target": "bronze"},
        )
        original_hash = entry.chain_hash
        # Tamper with the entry
        entry.action = "read_data"
        new_hash = entry._compute_hash()
        assert new_hash != original_hash


class TestEncryptedAuditLog:
    """Verify encrypted log storage and chain verification."""

    def test_log_and_read(self, audit_log) -> None:
        audit_log.log("t1", "login", "/api/v1/auth", {"ip": "1.2.3.4"})
        entries = audit_log.read("t1")
        assert len(entries) == 1
        assert entries[0].action == "login"
        assert entries[0].detail == {"ip": "1.2.3.4"}

    def test_chain_integrity(self, audit_log) -> None:
        audit_log.log("t1", "action1")
        audit_log.log("t1", "action2")
        audit_log.log("t1", "action3")
        assert audit_log.verify_chain("t1") is True

    def test_chain_detects_tampering(self, audit_log, storage, encryptor) -> None:
        audit_log.log("t1", "action1")
        audit_log.log("t1", "action2")
        audit_log.log("t1", "action3")

        # Tamper: rewrite second entry with different action
        log_path = EncryptedAuditLog._log_path("t1")
        raw = storage.read_text(log_path)
        lines = raw.strip().split("\n")
        assert len(lines) == 3

        # Decrypt second, modify, re-encrypt
        decrypted = encryptor.decrypt(lines[1], "t1", field_name="audit_log")
        data = json.loads(decrypted)
        data["action"] = "tampered_action"
        tampered_json = json.dumps(data)
        lines[1] = encryptor.encrypt(tampered_json, "t1", field_name="audit_log")
        storage.write_text(log_path, "\n".join(lines) + "\n")

        assert audit_log.verify_chain("t1") is False

    def test_tenant_isolation(self, audit_log) -> None:
        audit_log.log("t1", "action_t1")
        audit_log.log("t2", "action_t2")
        entries_t1 = audit_log.read("t1")
        entries_t2 = audit_log.read("t2")
        assert len(entries_t1) == 1
        assert len(entries_t2) == 1
        assert entries_t1[0].action == "action_t1"
        assert entries_t2[0].action == "action_t2"

    def test_count(self, audit_log) -> None:
        assert audit_log.count("t1") == 0
        audit_log.log("t1", "a1")
        audit_log.log("t1", "a2")
        assert audit_log.count("t1") == 2

    def test_purge(self, audit_log) -> None:
        audit_log.log("t1", "action1")
        audit_log.log("t1", "action2")
        assert audit_log.count("t1") == 2
        audit_log.purge("t1")
        assert audit_log.count("t1") == 0

    def test_read_empty_tenant(self, audit_log) -> None:
        entries = audit_log.read("nonexistent")
        assert entries == []

    def test_verify_empty_tenant(self, audit_log) -> None:
        assert audit_log.verify_chain("nonexistent") is True

    def test_read_with_limit_and_offset(self, audit_log) -> None:
        for i in range(5):
            audit_log.log("t1", f"action_{i}")
        entries = audit_log.read("t1", limit=2, offset=1)
        assert len(entries) == 2
        assert entries[0].action == "action_1"
        assert entries[1].action == "action_2"

    def test_user_id_preserved(self, audit_log) -> None:
        audit_log.log("t1", "admin_action", user_id="admin@velaflow.com")
        entries = audit_log.read("t1")
        assert entries[0].user_id == "admin@velaflow.com"
