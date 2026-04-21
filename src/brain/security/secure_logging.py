"""Secure structured logging with automatic PII/secret redaction.

Provides:
- JSON-structured log output for analysis
- Automatic secret and PII redaction (tokens, emails, phone numbers, IPs)
- Size-based log rotation with configurable retention
- HMAC chain for tamper detection (append-only integrity)
- Safe export function for debugging (Copilot-ready output)

Security properties:
- Secrets are redacted BEFORE writing to disk (defense in depth)
- Log files are created with 0600 permissions on Linux
- HMAC chain detects log tampering/deletion
- Export function double-checks redaction before output

Usage:
    from brain.security.secure_logging import SecureLogger, setup_logging

    # Automatic setup (reads LOG_LEVEL, LOG_DIR from environment)
    setup_logging()

    # Manual setup
    logger = SecureLogger(log_dir="/var/log/brain", level="INFO")
    logger.info("User login", user_id="u123", action="auth.login")
    logger.error("API call failed", error="timeout", endpoint="/api/v1/tasks")

    # Export sanitised logs
    logger.export_sanitised("debug-output.md")
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import logging.handlers
import os
import re
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Local import: sanitize every filesystem sink that accepts untrusted
# input (env vars, constructor args passed in from config). See
# brain.security.safe_path for the Snyk CWE-22 rationale.
from brain.security.safe_path import UnsafePathError, default_bases, safe_resolve


# ── Redaction Patterns ────────────────────────────────────────────────
# Applied BEFORE writing to disk. Order matters: more specific first.
_REDACT_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Named secret environment variables
    (re.compile(
        r'(TODOIST_API_TOKEN|NOTION_API_TOKEN|GOOGLE_AI_API_KEY|GROQ_API_KEY|'
        r'SMTP_PASSWORD|JWT_SECRET|VELAFLOW_MASTER_KEY|LITELLM_PROXY_TOKEN|'
        r'LITELLM_MASTER_KEY|CALLMEBOT_API_KEY|GOOGLE_OAUTH_CLIENT_SECRET|'
        r'GMAIL_IMAP_PASSWORD|OPENAI_API_KEY|N8N_ENCRYPTION_KEY|REDIS_PASSWORD)'
        r'[\s]*[=:]\s*\S+',
        re.IGNORECASE,
    ), r'\1=***REDACTED***'),
    # Bearer tokens and API keys in headers/strings
    (re.compile(r'(Bearer\s+)\S{8,}', re.IGNORECASE), r'\1***REDACTED***'),
    (re.compile(r'(sk-|key-|token-|AIza)\S{8,}', re.IGNORECASE), '***TOKEN***'),
    # Email addresses
    (re.compile(r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Z|a-z]{2,}\b'), '***EMAIL***'),
    # Phone numbers (international and US formats)
    (re.compile(r'\+\d{1,3}\d{9,12}\b'), '***PHONE***'),
    (re.compile(r'\b\d{3}[-.]?\d{3}[-.]?\d{4}\b'), '***PHONE***'),
    # IP addresses (IPv4) — but not version numbers like 3.11.0
    (re.compile(r'\b(?:25[0-5]|2[0-4]\d|[01]?\d\d?)(?:\.(?:25[0-5]|2[0-4]\d|[01]?\d\d?)){3}\b'),
     '***IP***'),
    # SSN patterns
    (re.compile(r'\b\d{3}-\d{2}-\d{4}\b'), '***SSN***'),
]


def redact(text: str) -> str:
    """Remove secrets, tokens, PII from text. Safe for log writing."""
    result = text
    for pattern, replacement in _REDACT_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


class _RedactingFormatter(logging.Formatter):
    """Log formatter that automatically redacts secrets from all output."""

    def __init__(self, fmt: str | None = None, datefmt: str | None = None,
                 json_format: bool = False) -> None:
        super().__init__(fmt, datefmt)
        self._json_format = json_format

    def format(self, record: logging.LogRecord) -> str:
        if self._json_format:
            return self._format_json(record)
        # Let the parent format first (preserving %d etc.), then redact
        formatted = super().format(record)
        return redact(formatted)

    def _format_json(self, record: logging.LogRecord) -> str:
        """Format as a JSON line for structured log analysis."""
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        entry = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(msg),
        }

        # Add extra fields (from logger.info("msg", extra={...}))
        for key in ("user_id", "tenant_id", "action", "resource",
                     "request_id", "status_code", "duration_ms",
                     "error", "endpoint", "method"):
            val = getattr(record, key, None)
            if val is not None:
                entry[key] = redact(str(val))

        if record.exc_info and record.exc_info[1]:
            entry["exception"] = redact(self.formatException(record.exc_info))

        return json.dumps(entry, ensure_ascii=False)


class _HMACRotatingHandler(logging.handlers.RotatingFileHandler):
    """Rotating file handler with HMAC chain for tamper detection.

    Each log line is appended with an HMAC that chains to the previous
    line, creating a tamper-evident log. If any line is modified or
    deleted, the chain breaks and verification fails.
    """

    def __init__(self, filename: str, maxBytes: int = 50_000_000,
                 backupCount: int = 10, hmac_key: bytes | None = None) -> None:
        # Snyk CWE-22 sanitizer: resolve and validate the log path against
        # the project-wide allow-list before any filesystem operation.
        # `filename` may originate from an env var read by the caller.
        safe_file = safe_resolve(
            filename, allowed_bases=default_bases(), create_parents=True
        )
        # Inline revalidation at each filesystem sink so Snyk's taint
        # engine sees a local sanitizer (Path.resolve().relative_to).
        bases_resolved = [Path(str(b)).resolve() for b in default_bases()]
        safe_file_resolved = safe_file.resolve()

        def _inside_base(p: Path) -> bool:
            for b in bases_resolved:
                try:
                    p.relative_to(b)
                    return True
                except ValueError:
                    continue
            return False

        if not _inside_base(safe_file_resolved):
            raise UnsafePathError("log file escapes allow-list")

        log_dir = safe_file_resolved.parent
        log_dir.mkdir(parents=True, exist_ok=True)

        super().__init__(str(safe_file_resolved), maxBytes=maxBytes,
                         backupCount=backupCount, encoding="utf-8")

        # Apply restrictive permissions via fchmod(fd, ...) — chmod
        # takes a file descriptor, NOT a path, eliminating the
        # path-traversal sink entirely. Directory perms are set with
        # mkdir(mode=...) above being insufficient on NT; on POSIX the
        # umask at process start is our primary control.
        if os.name != "nt":
            try:
                # RotatingFileHandler has opened the file already; we
                # tighten perms on the open stream.
                if self.stream is not None:
                    os.fchmod(self.stream.fileno(),
                              stat.S_IRUSR | stat.S_IWUSR)  # 0600
            except (OSError, AttributeError):
                pass

        self._hmac_key = hmac_key or self._derive_key()
        self._previous_hash = "0" * 64  # Genesis hash

        self._hmac_key = hmac_key or self._derive_key()
        self._previous_hash = "0" * 64  # Genesis hash

    def _derive_key(self) -> bytes:
        """Derive HMAC key from VELAFLOW_MASTER_KEY or a persisted random key."""
        # Prefer the master key if available (strongest option).
        master = os.environ.get("VELAFLOW_MASTER_KEY", "")
        if master:
            return hashlib.sha256(f"log-hmac:{master}".encode()).digest()
        # Fallback: generate-and-persist a random key alongside the log dir.
        # baseFilename is guaranteed sanitized by __init__ above, so the
        # parent directory is inside the allow-list.
        log_dir = Path(self.baseFilename).parent.resolve()
        key_path = (log_dir / ".log_hmac_key").resolve()
        # Inline Snyk-recognized sanitizer at the sink — relative_to
        # raises ValueError if key_path escapes log_dir.
        try:
            key_path.relative_to(log_dir)
        except ValueError:
            raise UnsafePathError("HMAC key path escapes log dir")
        if key_path.exists():
            return key_path.read_bytes()
        key = os.urandom(32)
        try:
            # Open with restrictive mode at creation so we never need
            # chmod-with-path (no PT sink). 0600 on POSIX.
            if os.name != "nt":
                flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                fd = os.open(str(key_path), flags,
                             stat.S_IRUSR | stat.S_IWUSR)
                try:
                    os.write(fd, key)
                finally:
                    os.close(fd)
            else:
                key_path.write_bytes(key)
        except OSError:
            pass  # Non-fatal; key still usable for this process lifetime.
        return key

    def emit(self, record: logging.LogRecord) -> None:
        """Write log record with HMAC chain hash appended."""
        try:
            msg = self.format(record)
            # Compute chain HMAC
            chain_input = f"{self._previous_hash}|{msg}"
            chain_hash = hmac.new(
                self._hmac_key, chain_input.encode(), hashlib.sha256
            ).hexdigest()[:32]  # 128-bit truncation
            self._previous_hash = chain_hash

            # Append chain hash to the log line
            record.msg = f"{record.msg} [chain:{chain_hash}]"
            super().emit(record)
        except Exception:
            self.handleError(record)


class SecureLogger:
    """High-level secure logging interface for VelaFlow.

    Wraps Python's logging module with automatic redaction, structured
    JSON output, HMAC chain integrity, and safe log export.
    """

    def __init__(
        self,
        log_dir: str | Path = "logs",
        level: str = "INFO",
        max_size_mb: int = 50,
        retention_count: int = 10,
        json_format: bool = True,
        enable_hmac: bool = True,
        name: str = "velaflow",
    ) -> None:
        # Snyk CWE-22 sanitizer: log_dir commonly originates from an env
        # var (VELAFLOW_LOG_DIR) or config. Route through the allow-list.
        self.log_dir = safe_resolve(
            log_dir, allowed_bases=default_bases(), create_parents=True
        )
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self._logger = logging.getLogger(name)
        self._logger.setLevel(getattr(logging, level.upper(), logging.INFO))

        # Avoid duplicate handlers on re-initialisation
        self._logger.handlers.clear()

        # Console handler (human-readable, redacted)
        console = logging.StreamHandler()
        console.setFormatter(_RedactingFormatter(
            fmt="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            datefmt="%H:%M:%S",
        ))
        self._logger.addHandler(console)

        # File handler (JSON structured, HMAC chained, redacted)
        log_file = self.log_dir / "velaflow.log"
        max_bytes = max_size_mb * 1024 * 1024

        if enable_hmac:
            file_handler = _HMACRotatingHandler(
                str(log_file), maxBytes=max_bytes, backupCount=retention_count,
            )
        else:
            file_handler = logging.handlers.RotatingFileHandler(
                str(log_file), maxBytes=max_bytes, backupCount=retention_count,
                encoding="utf-8",
            )

        file_handler.setFormatter(_RedactingFormatter(json_format=json_format))
        self._logger.addHandler(file_handler)

    @property
    def logger(self) -> logging.Logger:
        return self._logger

    def info(self, msg: str, **kwargs: Any) -> None:
        self._logger.info(msg, extra=kwargs)

    def warning(self, msg: str, **kwargs: Any) -> None:
        self._logger.warning(msg, extra=kwargs)

    def error(self, msg: str, **kwargs: Any) -> None:
        self._logger.error(msg, extra=kwargs)

    def critical(self, msg: str, **kwargs: Any) -> None:
        self._logger.critical(msg, extra=kwargs)

    def debug(self, msg: str, **kwargs: Any) -> None:
        self._logger.debug(msg, extra=kwargs)

    def audit(self, action: str, user_id: str = "", tenant_id: str = "",
              resource: str = "", **kwargs: Any) -> None:
        """Log an audit-level event (always INFO+, always written)."""
        self._logger.info(
            f"AUDIT: {action}",
            extra={
                "action": action,
                "user_id": user_id,
                "tenant_id": tenant_id,
                "resource": resource,
                **kwargs,
            },
        )

    def export_sanitised(self, output_path: str | Path | None = None) -> Path:
        """Export all logs with double-redaction, suitable for Copilot debugging."""
        if output_path is None:
            output_path = Path(f"velaflow-logs-{time.strftime('%Y%m%d-%H%M%S')}.md")
        else:
            output_path = Path(output_path)

        log_files = sorted(self.log_dir.glob("velaflow.log*"))

        lines = [
            "# VelaFlow — Sanitised Debug Log Export",
            "",
            f"Exported: {datetime.now(timezone.utc).isoformat()}",
            f"Log directory: {self.log_dir}",
            "",
            "---",
            "",
            "> All secrets, tokens, emails, phone numbers, and IP addresses",
            "> have been automatically redacted. Safe for GitHub Copilot Chat.",
            "",
        ]

        for lf in log_files[-5:]:
            lines.append(f"## {lf.name}")
            lines.append("")
            try:
                content = lf.read_text(errors="replace")
                log_lines = content.strip().split("\n")
                if len(log_lines) > 300:
                    lines.append(f"*Showing last 300 of {len(log_lines)} lines*\n")
                    log_lines = log_lines[-300:]
                # Double-redact for safety
                sanitised = redact("\n".join(log_lines))
                lines.append("```json")
                lines.append(sanitised)
                lines.append("```")
                lines.append("")
            except Exception as e:
                lines.append(f"*Error reading: {e}*\n")

        output_path.write_text("\n".join(lines))
        return output_path


def setup_logging(
    log_dir: str | None = None,
    level: str | None = None,
    max_size_mb: int | None = None,
    json_format: bool = True,
) -> SecureLogger:
    """Configure VelaFlow-wide secure logging from environment or arguments.

    Reads from environment variables if arguments not provided:
    - LOG_LEVEL (default: INFO)
    - LOG_DIR (default: logs/)
    - LOG_MAX_SIZE_MB (default: 50)
    """
    _level = level or os.environ.get("LOG_LEVEL", "INFO")
    _log_dir = log_dir or os.environ.get("LOG_DIR", "logs")
    _max_size = max_size_mb or int(os.environ.get("LOG_MAX_SIZE_MB", "50"))

    return SecureLogger(
        log_dir=_log_dir,
        level=_level,
        max_size_mb=_max_size,
        json_format=json_format,
    )
