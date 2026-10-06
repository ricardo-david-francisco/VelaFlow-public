"""DuckDB Connection Factory — Memory-safe analytical engine.

Self-hosted analytical engine embedded in-process via DuckDB — VelaFlow's
processing engine. DuckDB runs in-process with zero external
dependencies, making it ideal for LXC deployment on constrained
hardware (8 GB RAM, N95 CPU).

Memory management:
- Default limit: 512 MB (leaves room for API, worker, n8n, LLM)
- Configurable via DUCKDB_MEMORY_LIMIT env var
- Automatic spill-to-disk for large datasets

Security:
- Database file has restricted permissions (0600)
- No network access (in-process only)
- All queries use parameterized bindings
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

try:
    import duckdb

    DUCKDB_AVAILABLE = True
except ImportError:
    DUCKDB_AVAILABLE = False


class DuckDBEngine:
    """Managed DuckDB connection with security hardening.

    Usage:
        engine = DuckDBEngine("/opt/velaflow/data/velaflow.duckdb")
        engine.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER, name TEXT)")
        rows = engine.query("SELECT * FROM t WHERE id = ?", [42])
        engine.close()
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        memory_limit: str | None = None,
        read_only: bool = False,
    ) -> None:
        if not DUCKDB_AVAILABLE:
            raise RuntimeError(
                "DuckDB is not installed. Install with: pip install duckdb>=0.9.0"
            )
        self._db_path = Path(db_path) if db_path else None
        self._read_only = read_only
        mem = memory_limit or os.environ.get("DUCKDB_MEMORY_LIMIT", "512MB")

        # Validate memory limit format to prevent injection
        import re
        if not re.match(r"^\d+[KMGT]?B$", mem, re.IGNORECASE):
            raise ValueError(f"Invalid DUCKDB_MEMORY_LIMIT format: {mem!r}")

        if self._db_path:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = duckdb.connect(str(self._db_path), read_only=read_only)
            self._harden_file()
        else:
            # In-memory database for testing
            self._conn = duckdb.connect(":memory:")

        self._conn.execute(f"SET memory_limit='{mem}'")
        self._conn.execute("SET threads=2")
        # Enable spill-to-disk under memory pressure
        if self._db_path:
            temp_dir = str(self._db_path.parent / ".duckdb_tmp")
            # Validate temp_dir — only allow safe path characters
            import re as _re
            if not _re.match(r"^[a-zA-Z0-9_/\\\-.:]+$", temp_dir):
                raise ValueError(f"Unsafe temp_directory path: {temp_dir!r}")
            Path(temp_dir).mkdir(exist_ok=True)
            self._conn.execute(f"SET temp_directory='{temp_dir}'")
        logger.info(
            "DuckDB engine initialized (memory_limit=%s, path=%s)",
            mem,
            self._db_path or ":memory:",
        )

    def _harden_file(self) -> None:
        """Restrict database file permissions on POSIX systems."""
        if os.name != "nt" and self._db_path and self._db_path.exists():
            try:
                os.chmod(self._db_path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass

    def execute(self, sql: str, params: list[Any] | None = None) -> None:
        """Execute a SQL statement (DDL or DML)."""
        if params:
            self._conn.execute(sql, params)
        else:
            self._conn.execute(sql)

    def executemany(self, sql: str, params_seq: list[tuple[Any, ...]]) -> None:
        """Execute a SQL statement with multiple parameter sets (batch insert)."""
        if not params_seq:
            return
        self._conn.executemany(sql, params_seq)

    def query(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        """Execute a query and return results as list of dicts."""
        if params:
            result = self._conn.execute(sql, params)
        else:
            result = self._conn.execute(sql)
        columns = [desc[0] for desc in result.description]
        return [dict(zip(columns, row)) for row in result.fetchall()]

    def query_scalar(self, sql: str, params: list[Any] | None = None) -> Any:
        """Execute a query and return a single scalar value."""
        if params:
            result = self._conn.execute(sql, params)
        else:
            result = self._conn.execute(sql)
        row = result.fetchone()
        return row[0] if row else None

    def table_exists(self, table_name: str) -> bool:
        """Check if a table exists in the database."""
        count = self.query_scalar(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
            [table_name],
        )
        return count > 0

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
