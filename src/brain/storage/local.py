"""Local filesystem storage backend.

Stores medallion data as JSON files on the local filesystem.
Designed for single-node LXC deployment and development use.

For analytical queries, the DuckDB engine (brain.engine) provides
SQL access over the same data via the medallion processor.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

from brain.storage.base import StorageBackend

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"
_write_lock = threading.Lock() if _IS_WINDOWS else None


class LocalStorageBackend(StorageBackend):
    """Store medallion data as JSON files on the local filesystem.

    Directory structure:
        {base_dir}/
            bronze/{tenant_id}/{source}/{batch_id}.json
            silver/{tenant_id}/{source}/{entity}.json
            gold/{tenant_id}/{entity}.json
            runs/{tenant_id}/{run_id}.json
    """

    def __init__(self, base_dir: str | Path) -> None:
        self._base = Path(base_dir).resolve()
        self._base.mkdir(parents=True, exist_ok=True)

    def write_json(self, key: str, data: dict[str, Any]) -> None:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: write to temp file then rename
        # On Windows os.replace is not atomic — serialize writes with a lock
        tmp_path = path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            if _write_lock is not None:
                with _write_lock:
                    os.replace(tmp_path, path)
            else:
                os.replace(tmp_path, path)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise

    def read_json(self, key: str) -> dict[str, Any] | None:
        path = self._resolve(key)
        if not path.is_file():
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def list_keys(self, prefix: str) -> list[str]:
        base = self._resolve(prefix)
        if not base.exists():
            return []
        # If prefix points to a directory, list all files recursively
        if base.is_dir():
            keys = []
            for p in sorted(base.rglob("*.json")):
                rel = p.relative_to(self._base)
                keys.append(str(rel).replace("\\", "/"))
            return keys
        # If prefix is a partial path, find matching files
        parent = base.parent
        if not parent.exists():
            return []
        stem = base.name
        keys = []
        for p in sorted(parent.rglob("*.json")):
            rel = str(p.relative_to(self._base)).replace("\\", "/")
            if rel.startswith(prefix.rstrip("/")):
                keys.append(rel)
        return keys

    def delete(self, key: str) -> bool:
        path = self._resolve(key)
        if path.is_file():
            path.unlink()
            return True
        return False

    def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()

    def write_text(self, key: str, text: str) -> None:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            if _write_lock is not None:
                with _write_lock:
                    os.replace(tmp_path, path)
            else:
                os.replace(tmp_path, path)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise

    def read_text(self, key: str) -> str | None:
        path = self._resolve(key)
        if not path.is_file():
            return None
        with open(path, encoding="utf-8") as f:
            return f.read()

    def _resolve(self, key: str) -> Path:
        # Prevent directory traversal and absolute path attacks
        clean = Path(key.replace("\\", "/"))
        if ".." in clean.parts:
            raise ValueError(f"Path traversal detected: {key}")
        resolved = (self._base / clean).resolve()
        # self._base is already resolved in __init__
        if not str(resolved).startswith(str(self._base)):
            raise ValueError(f"Path escapes base directory: {key}")
        return resolved
