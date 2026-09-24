"""DuckDB Processing Engine — On-prem Spark replacement.

Provides lightweight analytical processing for the medallion
architecture using DuckDB in-process (no JVM, no Spark).
"""

from brain.engine.connection import DuckDBEngine
from brain.engine.processor import MedallionProcessor

__all__ = ["DuckDBEngine", "MedallionProcessor"]
