"""Tests for the local storage backend."""

from __future__ import annotations

import pytest

from brain.storage.local import LocalStorageBackend


@pytest.fixture()
def storage(tmp_path):
    return LocalStorageBackend(tmp_path)


class TestLocalStorageBackend:
    def test_write_and_read(self, storage):
        storage.write_json("bronze/t1/todoist/batch1.json", {"count": 3})
        result = storage.read_json("bronze/t1/todoist/batch1.json")
        assert result == {"count": 3}

    def test_read_missing_returns_none(self, storage):
        assert storage.read_json("nonexistent.json") is None

    def test_exists(self, storage):
        assert not storage.exists("test.json")
        storage.write_json("test.json", {"ok": True})
        assert storage.exists("test.json")

    def test_delete(self, storage):
        storage.write_json("to_delete.json", {"x": 1})
        assert storage.delete("to_delete.json") is True
        assert storage.exists("to_delete.json") is False

    def test_delete_missing(self, storage):
        assert storage.delete("missing.json") is False

    def test_list_keys(self, storage):
        storage.write_json("bronze/t1/todoist/a.json", {})
        storage.write_json("bronze/t1/todoist/b.json", {})
        storage.write_json("bronze/t1/calendar/c.json", {})

        keys = storage.list_keys("bronze/t1/todoist")
        assert len(keys) == 2
        assert all("todoist" in k for k in keys)

    def test_list_keys_empty(self, storage):
        assert storage.list_keys("nothing/") == []

    def test_path_traversal_blocked(self, storage):
        with pytest.raises(ValueError, match="Path traversal"):
            storage.write_json("../../../etc/passwd", {"hack": True})

    def test_path_traversal_read(self, storage):
        with pytest.raises(ValueError, match="Path traversal"):
            storage.read_json("../../secret.json")

    def test_overwrite(self, storage):
        storage.write_json("data.json", {"v": 1})
        storage.write_json("data.json", {"v": 2})
        assert storage.read_json("data.json") == {"v": 2}

    def test_nested_deep_path(self, storage):
        key = "bronze/tenant_abc/todoist/2024/01/batch_001.json"
        storage.write_json(key, {"nested": True})
        assert storage.read_json(key) == {"nested": True}
