"""Tests for scripts/drive_backup.py — envelope round-trip and tamper detection.

Network-touching paths (Drive API) are not exercised here; they require a
live service account. These tests cover the cryptographic envelope and the
tar-packaging logic, which are where an auth bug would turn into a silent
data-loss or data-leak incident.
"""

from __future__ import annotations

import base64
import importlib.util
import io
import os
import secrets
import sys
import tarfile
from pathlib import Path

import pytest

# Load scripts/drive_backup.py as a module without depending on it being on
# sys.path — scripts/ is not a package.
_THIS = Path(__file__).resolve()
_SCRIPT_PATH = _THIS.parent.parent / "scripts" / "drive_backup.py"
_spec = importlib.util.spec_from_file_location("drive_backup", _SCRIPT_PATH)
assert _spec and _spec.loader
drive_backup = importlib.util.module_from_spec(_spec)
sys.modules["drive_backup"] = drive_backup
_spec.loader.exec_module(drive_backup)


@pytest.fixture
def key() -> bytes:
    return secrets.token_bytes(32)


class TestEnvelope:
    def test_round_trip(self, key: bytes) -> None:
        plaintext = b"hello velaflow backup payload"
        envelope = drive_backup._encrypt_stream(plaintext, key)
        assert envelope.startswith(drive_backup._MAGIC)
        recovered = drive_backup._decrypt_stream(envelope, key)
        assert recovered == plaintext

    def test_wrong_key_fails(self, key: bytes) -> None:
        other = secrets.token_bytes(32)
        envelope = drive_backup._encrypt_stream(b"secret", key)
        with pytest.raises(Exception):
            drive_backup._decrypt_stream(envelope, other)

    def test_tampered_ciphertext_fails(self, key: bytes) -> None:
        envelope = bytearray(drive_backup._encrypt_stream(b"secret", key))
        # Flip a bit in the ciphertext region (after magic + nonce)
        offset = len(drive_backup._MAGIC) + drive_backup._NONCE_SIZE + 2
        envelope[offset] ^= 0x01
        with pytest.raises(Exception):
            drive_backup._decrypt_stream(bytes(envelope), key)

    def test_tampered_magic_fails(self, key: bytes) -> None:
        envelope = bytearray(drive_backup._encrypt_stream(b"secret", key))
        envelope[0] = ord("X")
        with pytest.raises(Exception, match="magic"):
            drive_backup._decrypt_stream(bytes(envelope), key)

    def test_short_envelope_rejected(self, key: bytes) -> None:
        with pytest.raises(Exception):
            drive_backup._decrypt_stream(b"too short", key)


class TestKeyLoading:
    def test_base64_roundtrip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        raw = secrets.token_bytes(32)
        monkeypatch.setenv(
            "VELAFLOW_BACKUP_KEY", base64.urlsafe_b64encode(raw).decode()
        )
        loaded = drive_backup._load_backup_key()
        assert loaded == raw

    def test_hex_roundtrip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        raw = secrets.token_bytes(32)
        monkeypatch.setenv("VELAFLOW_BACKUP_KEY", raw.hex())
        loaded = drive_backup._load_backup_key()
        assert loaded == raw

    def test_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VELAFLOW_BACKUP_KEY", raising=False)
        with pytest.raises(RuntimeError, match="required"):
            drive_backup._load_backup_key()

    def test_wrong_length_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(
            "VELAFLOW_BACKUP_KEY", base64.urlsafe_b64encode(b"short").decode()
        )
        with pytest.raises(RuntimeError, match="32 bytes"):
            drive_backup._load_backup_key()

    def test_fingerprint_stable(self) -> None:
        k = b"\x00" * 32
        assert drive_backup._key_fingerprint(k) == drive_backup._key_fingerprint(k)
        assert len(drive_backup._key_fingerprint(k)) == 16


class TestTarball:
    def test_build_and_extract(self, tmp_path: Path, key: bytes) -> None:
        src = tmp_path / "data"
        src.mkdir()
        (src / "hello.txt").write_text("world")

        manifest = {"version": 1, "key_fingerprint": "abc"}
        blob = drive_backup._build_tarball([src], manifest)

        # Round-trip: encrypt, decrypt, and confirm contents
        envelope = drive_backup._encrypt_stream(blob, key)
        recovered = drive_backup._decrypt_stream(envelope, key)
        assert recovered == blob

        # Inspect the tar to confirm MANIFEST.json is present and files included
        with tarfile.open(fileobj=io.BytesIO(recovered), mode="r:gz") as tar:
            names = tar.getnames()
            assert "MANIFEST.json" in names
            assert any(n.endswith("hello.txt") for n in names)
