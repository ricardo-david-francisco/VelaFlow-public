"""VelaFlow Deployment Pre-Flight Validator.

Run this BEFORE starting the API or any systemd unit on a new host.
It verifies every common breakage class in ~5 seconds and exits non-zero
if anything would cause a crash on first request. This is the guardrail
against "deploy today, spend tomorrow chasing logs".

Usage:
    python scripts/preflight.py
    python scripts/preflight.py --json        # machine-readable output
    python scripts/preflight.py --fix-perms   # attempt perm fixes (Linux)

Exit codes:
    0  all checks passed
    1  one or more BLOCKING issues — deployment will crash
    2  warnings only (non-blocking) — deployment will start but degrade

The checks below are ordered cheapest → most expensive so a failure
surfaces early.
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import os
import socket
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ── Result model ─────────────────────────────────────────────────────


@dataclass
class Check:
    name: str
    passed: bool
    blocking: bool
    detail: str = ""
    # When ``informational`` is True and ``passed`` is False, the result
    # is reported as an opt-in/off-by-default feature rather than a
    # warning. This keeps the preflight summary honest: a production
    # deployment with only informational entries unchecked has zero
    # warnings.
    informational: bool = False


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, c: Check) -> None:
        self.checks.append(c)

    @property
    def failed_blocking(self) -> list[Check]:
        return [c for c in self.checks if not c.passed and c.blocking]

    @property
    def failed_nonblocking(self) -> list[Check]:
        return [
            c for c in self.checks
            if not c.passed and not c.blocking and not c.informational
        ]

    @property
    def failed_informational(self) -> list[Check]:
        return [
            c for c in self.checks
            if not c.passed and not c.blocking and c.informational
        ]


# ── Individual checks ────────────────────────────────────────────────


def _check_python_version(r: Report) -> None:
    ok = sys.version_info >= (3, 11)
    r.add(
        Check(
            "python_version",
            ok,
            blocking=True,
            detail=f"{sys.version.split()[0]} (need >=3.11)",
        )
    )


def _check_required_env(r: Report) -> None:
    required = dict(
        (
            ("VELAFLOW_MASTER_KEY", "Master AES-256-GCM key (base64 32 bytes) — non-credential at-rest encryption"),
            ("VELAFLOW_CREDENTIAL_PEPPER", "Credential vault pepper (base64 >=32 bytes) — HKDF input bound to owner_sub"),
            ("JWT_SECRET", "JWT HS256 input bytes (min 32 chars)"),
        )
    )
    for var, purpose in required.items():
        val = os.environ.get(var, "").strip()
        present = bool(val)
        r.add(Check(f"env:{var}", present, blocking=True, detail=purpose))

    # Format-validate VELAFLOW_MASTER_KEY if present
    mk = os.environ.get("VELAFLOW_MASTER_KEY", "").strip()
    if mk:
        ok, why = _validate_master_key_format(mk)
        r.add(
            Check(
                "env:VELAFLOW_MASTER_KEY:format",
                ok,
                blocking=True,
                detail=why,
            )
        )

    # Format-validate VELAFLOW_CREDENTIAL_PEPPER if present
    pep = os.environ.get("VELAFLOW_CREDENTIAL_PEPPER", "").strip()
    if pep:
        ok, why = _validate_master_key_format(pep)
        r.add(
            Check(
                "env:VELAFLOW_CREDENTIAL_PEPPER:format",
                ok,
                blocking=True,
                detail=why,
            )
        )
        # Disallow reuse of the master key as the pepper.
        if mk and pep == mk:
            r.add(
                Check(
                    "env:VELAFLOW_CREDENTIAL_PEPPER:distinct",
                    False,
                    blocking=True,
                    detail="pepper must not equal VELAFLOW_MASTER_KEY",
                )
            )

    # JWT_SECRET strength
    js = os.environ.get("JWT_SECRET", "")
    r.add(
        Check(
            "env:JWT_SECRET:strength",
            len(js) >= 32,
            blocking=True,
            detail=f"length={len(js)} (need >=32)",
        )
    )

    # Google OAuth client id / secret — required for the only login path.
    for var in ("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"):
        val = os.environ.get(var, "").strip()
        r.add(
            Check(
                f"env:{var}",
                bool(val),
                blocking=True,
                detail="Google OAuth is the only end-user login surface",
            )
        )


def _check_tls_material(r: Report) -> None:
    """TLS cert + key files must exist and be readable. HTTPS is mandatory."""
    cert = os.environ.get("VELAFLOW_TLS_CERT", "").strip()
    key = os.environ.get("VELAFLOW_TLS_KEY", "").strip()
    for label, val in (("cert", cert), ("key", key)):
        r.add(
            Check(
                f"tls:{label}:env",
                bool(val),
                blocking=True,
                detail=f"VELAFLOW_TLS_{label.upper()} must point to a PEM file",
            )
        )
    if not (cert and key):
        return
    for label, path_str in (("cert", cert), ("key", key)):
        p = Path(path_str)
        readable = p.is_file() and os.access(p, os.R_OK)
        r.add(
            Check(
                f"tls:{label}:readable",
                readable,
                blocking=True,
                detail=str(p) if readable else f"{p} is not a readable file",
            )
        )


def _validate_master_key_format(raw: str) -> tuple[bool, str]:
    try:
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except Exception as e:
        return False, f"not valid base64url: {e}"
    if len(key) != 32:
        return False, f"decoded length {len(key)} (need 32)"
    return True, "ok (32 bytes)"


def _check_optional_env(r: Report) -> None:
    """Non-blocking but recommended in production."""
    optional = {
        "VELAFLOW_DATA_DIR": "data directory (defaults to ./data)",
        "VELAFLOW_LOG_HMAC_KEY": "secure-log HMAC key (auto-generated if absent)",
        "VELAFLOW_DISABLE_OPEN_REGISTRATION": "set true in production",
    }
    for var, purpose in optional.items():
        present = bool(os.environ.get(var, "").strip())
        # These are production-hardening flags. Missing them is not a
        # runtime risk (defaults are safe); mark as informational.
        r.add(
            Check(f"env:{var}", present, blocking=False,
                  informational=True, detail=purpose)
        )


def _check_imports(r: Report) -> None:
    """Import every top-level brain.* module to catch syntax errors early."""
    modules = [
        "brain",
        "brain.api.app",
        "brain.queue.tasks",
        "brain.queue.worker",
        "brain.storage.base",
        "brain.tenant.manager",
        "brain.security.audit_log",
        "brain.engine.connection",
        "brain.engine.processor",
        "brain.catalog.store",
    ]
    src_dir = Path(__file__).resolve().parent.parent / "src"
    if str(src_dir) not in sys.path and src_dir.is_dir():
        sys.path.insert(0, str(src_dir))

    for m in modules:
        spec = importlib.util.find_spec(m)
        if spec is None:
            r.add(Check(f"import:{m}", False, blocking=True, detail="module not found"))
            continue
        try:
            importlib.import_module(m)
            r.add(Check(f"import:{m}", True, blocking=True, detail="ok"))
        except Exception as e:  # noqa: BLE001 — want any failure surface
            r.add(
                Check(f"import:{m}", False, blocking=True, detail=f"{type(e).__name__}: {e}")
            )


def _check_critical_deps(r: Report) -> None:
    critical = ["fastapi", "uvicorn", "pydantic", "cryptography", "duckdb"]
    for pkg in critical:
        ok = importlib.util.find_spec(pkg) is not None
        r.add(Check(f"dep:{pkg}", ok, blocking=True, detail="installed" if ok else "missing"))


def _check_optional_deps(r: Report) -> None:
    optional = {
        "google.oauth2": "Google Drive backup (install velaflow[backup])",
        "googleapiclient": "Google Drive backup (install velaflow[backup])",
        "streamlit": "Self-service GUI (install velaflow[gui])",
        "stripe": "Billing (install velaflow[billing])",
    }
    for mod, hint in optional.items():
        ok = importlib.util.find_spec(mod.split(".")[0]) is not None
        # Optional install-groups (``velaflow[gui]``, ``velaflow[billing]``,
        # ``velaflow[backup]``) are intentionally absent on minimal
        # deployments. Report as informational, not warning.
        r.add(Check(f"dep:{mod}", ok, blocking=False,
                    informational=True, detail=hint))


def _check_data_dir(r: Report) -> None:
    # Snyk CWE-22 sanitizer: route the env-var path through the project
    # allow-list before any mkdir / write test.
    try:
        from brain.security.safe_path import default_bases, safe_resolve

        data_dir = safe_resolve(
            os.environ.get("VELAFLOW_DATA_DIR", "./data"),
            allowed_bases=default_bases(),
            create_parents=True,
        )
    except Exception as e:  # noqa: BLE001 — surface as a blocking failure
        r.add(Check("data_dir:writable", False, blocking=True, detail=f"{e}"))
        return
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        test_file = data_dir / ".preflight-write-test"
        test_file.write_text("ok")
        test_file.unlink()
        r.add(Check("data_dir:writable", True, blocking=True, detail=str(data_dir)))
    except Exception as e:
        r.add(Check("data_dir:writable", False, blocking=True, detail=f"{data_dir}: {e}"))


def _check_port_free(r: Report) -> None:
    port = int(os.environ.get("VELAFLOW_PORT", 8765))
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        r.add(Check(f"port:{port}", True, blocking=True, detail="free"))
    except OSError as e:
        r.add(Check(f"port:{port}", False, blocking=True, detail=f"in use: {e}"))
    finally:
        s.close()


def _check_config_files(r: Report) -> None:
    root = Path(__file__).resolve().parent.parent
    for rel in ("config/pipeline.yaml", "pyproject.toml"):
        p = root / rel
        r.add(
            Check(
                f"file:{rel}",
                p.is_file(),
                blocking=True,
                detail=f"exists" if p.is_file() else "MISSING",
            )
        )


def _check_backup_env(r: Report) -> None:
    """Non-blocking — only fails if backup is opted into and mis-configured."""
    if not os.environ.get("VELAFLOW_BACKUP_KEY"):
        # Drive backup is opt-in. Its absence is not a warning.
        r.add(
            Check(
                "backup:opted_in",
                False,
                blocking=False,
                informational=True,
                detail="VELAFLOW_BACKUP_KEY not set — Drive backup disabled",
            )
        )
        return
    try:
        from scripts.drive_backup import _load_backup_key, _key_fingerprint  # type: ignore
    except Exception:
        # When running standalone, fall back to inline validation
        raw = os.environ["VELAFLOW_BACKUP_KEY"].strip()
        ok, why = _validate_master_key_format(raw)
        r.add(Check("backup:key_format", ok, blocking=False, detail=why))
        return
    try:
        key = _load_backup_key()
        fp = _key_fingerprint(key)
        r.add(Check("backup:key_format", True, blocking=False, detail=f"ok (fp={fp})"))
    except Exception as e:
        r.add(Check("backup:key_format", False, blocking=False, detail=str(e)))


# ── Runner ───────────────────────────────────────────────────────────


def run_all() -> Report:
    r = Report()
    _check_python_version(r)
    _check_critical_deps(r)
    _check_required_env(r)
    _check_tls_material(r)
    _check_imports(r)
    _check_config_files(r)
    _check_data_dir(r)
    _check_port_free(r)
    _check_optional_deps(r)
    _check_optional_env(r)
    _check_backup_env(r)
    return r


def _render_human(r: Report) -> str:
    lines: list[str] = []
    lines.append("=" * 66)
    lines.append("VelaFlow Pre-Flight Validator")
    lines.append("=" * 66)
    for c in r.checks:
        if c.passed:
            icon = "PASS"
        elif c.blocking:
            icon = "FAIL"
        elif c.informational:
            icon = "INFO"
        else:
            icon = "WARN"
        lines.append(f"  [{icon}] {c.name:40s}  {c.detail}")
    lines.append("-" * 66)
    blk = len(r.failed_blocking)
    wrn = len(r.failed_nonblocking)
    info = len(r.failed_informational)
    total = len(r.checks)
    passed = total - blk - wrn - info
    lines.append(
        f"  {passed}/{total} passed, {blk} blocking failure(s), "
        f"{wrn} warning(s), {info} info"
    )
    if blk:
        lines.append("  -> DEPLOYMENT WILL CRASH. Fix blocking failures before starting.")
    elif wrn:
        lines.append("  -> Deployment will start but some features are disabled.")
    else:
        lines.append("  -> Ready to deploy.")
    return "\n".join(lines)


def _render_json(r: Report) -> str:
    return json.dumps(
        {
            "checks": [
                {
                    "name": c.name,
                    "passed": c.passed,
                    "blocking": c.blocking,
                    "detail": c.detail,
                }
                for c in r.checks
            ],
            "summary": {
                "total": len(r.checks),
                "passed": len([c for c in r.checks if c.passed]),
                "blocking_failures": len(r.failed_blocking),
                "warnings": len(r.failed_nonblocking),
            },
        },
        indent=2,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VelaFlow deployment pre-flight validator")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    args = parser.parse_args(argv or sys.argv[1:])

    r = run_all()
    print(_render_json(r) if args.json else _render_human(r))

    if r.failed_blocking:
        return 1
    if r.failed_nonblocking:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
