#!/usr/bin/env python3
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# VelaFlow â€” Interactive Setup Wizard
#
# OPNsense-style terminal installer with menu navigation, sensible
# defaults, secure secret input, platform auto-detection, and
# cross-platform support (Proxmox, VMware/Ubuntu, Oracle Cloud, local).
#
# Usage:
#   python scripts/installer.py                # Interactive wizard
#   python scripts/installer.py --quick        # Quick setup (defaults + keys only)
#   python scripts/installer.py --health       # Health check only
#   python scripts/installer.py --export-logs  # Export sanitized logs
#
# Security:
#   - Secrets entered via getpass (never echoed to terminal)
#   - Config files written with restrictive permissions (0600 on Linux)
#   - Tokens validated before writing (format + length)
#   - No secrets stored in command history or logs
#   - Input sanitised against injection
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
from __future__ import annotations

import getpass
import hashlib
import json
import logging
import os
import platform
import re
import secrets
import shutil
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

# â”€â”€ Constants â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
VELAFLOW_VERSION = "2.0.0"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = PROJECT_ROOT / "logs"

# ANSI colour codes (disabled on Windows without VT support)
_SUPPORTS_COLOR = (
    hasattr(sys.stdout, "isatty")
    and sys.stdout.isatty()
    and (os.name != "nt" or os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM"))
)

if _SUPPORTS_COLOR:
    _R = "\033[0;31m"   # Red
    _G = "\033[0;32m"   # Green
    _Y = "\033[1;33m"   # Yellow
    _B = "\033[1m"       # Bold
    _C = "\033[0;36m"   # Cyan
    _N = "\033[0m"       # Reset
else:
    _R = _G = _Y = _B = _C = _N = ""

logger = logging.getLogger("velaflow.installer")


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# Utility Functions
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def _clear_screen() -> None:
    print("\033c", end="", flush=True)


def _banner(title: str) -> None:
    width = 60
    print()
    print(f"{_B}{'â•' * width}{_N}")
    print(f"{_B}  {title}{_N}")
    print(f"{_B}{'â•' * width}{_N}")
    print()


def _section(title: str) -> None:
    print()
    print(f"{_B}â”€â”€ {title} {'â”€' * max(1, 50 - len(title))}{_N}")
    print()


def _ok(msg: str) -> None:
    print(f"  {_G}âœ“{_N} {msg}")


def _warn(msg: str) -> None:
    print(f"  {_Y}âš {_N} {msg}")


def _fail(msg: str) -> None:
    print(f"  {_R}âœ—{_N} {msg}")


def _info(msg: str) -> None:
    print(f"  {_C}â†’{_N} {msg}")


def _prompt(label: str, default: str = "", secret: bool = False,
            required: bool = False, validator: Any = None) -> str:
    """Prompt user for input with optional default, secret masking, and validation."""
    suffix = ""
    if default and not secret:
        suffix = f" [{default}]"
    elif default and secret:
        suffix = " [****]"

    while True:
        if secret:
            value = getpass.getpass(f"  {label}{suffix}: ")
        else:
            value = input(f"  {label}{suffix}: ").strip()

        if not value and default:
            value = default

        if required and not value:
            _fail("This field is required.")
            continue

        if validator and value:
            err = validator(value)
            if err:
                _fail(err)
                continue

        return value


def _menu(title: str, options: list[tuple[str, str]],
          allow_zero: bool = True) -> str:
    """Display a numbered menu and return the selected key."""
    _banner(title)
    for key, label in options:
        marker = f"  [{key}]" if key != "0" else f"\n  [{key}]"
        print(f"{marker} {label}")
    print()

    valid_keys = {k for k, _ in options}
    while True:
        choice = input(f"  Select: ").strip()
        if choice in valid_keys:
            return choice
        _fail(f"Invalid choice. Enter one of: {', '.join(sorted(valid_keys))}")


def _confirm(question: str, default: bool = True) -> bool:
    """Yes/No confirmation prompt."""
    hint = "[Y/n]" if default else "[y/N]"
    answer = input(f"  {question} {hint}: ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# Input Validators
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def _validate_domain(value: str) -> str | None:
    """Validate domain name format."""
    if value == "localhost":
        return None
    pattern = r"^[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?)*\.[a-zA-Z]{2,}$"
    if not re.match(pattern, value):
        return "Invalid domain format. Examples: api.example.com, velaflow.mysite.org"
    return None


def _validate_email(value: str) -> str | None:
    """Validate email format."""
    pattern = r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"
    if not re.match(pattern, value):
        return "Invalid email format."
    return None


def _validate_port(value: str) -> str | None:
    """Validate port number."""
    try:
        p = int(value)
        if not (1 <= p <= 65535):
            raise ValueError
    except ValueError:
        return "Port must be a number between 1 and 65535."
    return None


def _validate_token(value: str) -> str | None:
    """Validate API token basic format (no injection, reasonable length)."""
    if len(value) < 8:
        return "Token seems too short (minimum 8 characters)."
    if len(value) > 512:
        return "Token seems too long (maximum 512 characters)."
    if re.search(r'[;\n\r`$(){}|<>]', value):
        return "Token contains suspicious characters. Paste the raw token only."
    return None


def _validate_url(value: str) -> str | None:
    """Validate URL format."""
    if not re.match(r'^https?://[a-zA-Z0-9.\-]+(:[0-9]+)?(/.*)?$', value):
        return "Invalid URL. Must start with http:// or https://"
    return None


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# Platform Detection
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def detect_platform() -> dict[str, str]:
    """Detect the current platform and virtualisation layer."""
    info: dict[str, str] = {
        "os": platform.system(),
        "release": platform.release(),
        "arch": platform.machine(),
        "platform": "unknown",
        "python": platform.python_version(),
    }

    if info["os"] == "Windows":
        info["platform"] = "windows-local"
        return info

    # Linux detection
    try:
        if Path("/etc/pve").is_dir():
            info["platform"] = "proxmox-host"
            return info
    except PermissionError:
        pass

    # Check for LXC container
    try:
        with open("/proc/1/environ", "rb") as f:
            env = f.read()
            if b"container=lxc" in env:
                info["platform"] = "proxmox-lxc"
                return info
    except (FileNotFoundError, PermissionError):
        pass

    # Check for VMware
    try:
        _virt_bin = shutil.which("systemd-detect-virt") or "/usr/bin/systemd-detect-virt"
        result = subprocess.run(
            [_virt_bin], capture_output=True, text=True, timeout=5, check=False
        )
        virt = result.stdout.strip()
        if virt == "vmware":
            info["platform"] = "vmware-guest"
        elif virt == "oracle":
            info["platform"] = "oracle-cloud"
        elif virt == "kvm":
            info["platform"] = "kvm-vm"
        elif virt in ("none", ""):
            info["platform"] = "bare-metal"
        else:
            info["platform"] = f"vm-{virt}"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # Check Oracle Cloud via metadata
        try:
            _curl_bin = shutil.which("curl") or "/usr/bin/curl"
            result = subprocess.run(
                [_curl_bin, "-sf", "--max-time", "2",
                 "http://169.254.169.254/opc/v2/instance/"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            if result.returncode == 0:
                info["platform"] = "oracle-cloud"
                return info
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        info["platform"] = "linux-local"

    return info


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# Configuration Builder
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class ConfigBuilder:
    """Collects configuration values and writes config/.env securely."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self._load_existing()

    def _load_existing(self) -> None:
        """Load existing config/.env if present."""
        env_file = CONFIG_DIR / ".env"
        if env_file.is_file():
            with open(env_file) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, val = line.partition("=")
                        val = val.strip()
                        # Strip surrounding quotes
                        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                            val = val[1:-1]
                        self.values[key.strip()] = val

    @staticmethod
    def _escape_env_value(value: str) -> str:
        """Escape a value for safe inclusion inside double quotes in .env."""
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    def set(self, key: str, value: str) -> None:
        if value:
            self.values[key] = value

    def get(self, key: str, default: str = "") -> str:
        return self.values.get(key, default)

    def write(self) -> Path:
        """Write config/.env with secure permissions."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        env_path = CONFIG_DIR / ".env"

        # Build content with sections
        sections = [
            ("VelaFlow Configuration", "Auto-generated by installer wizard"),
            None,  # separator
        ]

        lines = [
            "# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•",
            "# VelaFlow â€” Configuration (auto-generated by installer)",
            f"# Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            "# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•",
            "",
        ]

        # Group by category
        groups = [
            ("Platform & Domain", [
                "VELAFLOW_DOMAIN", "VELAFLOW_API_PORT", "CORS_ALLOWED_ORIGINS",
                "ENVIRONMENT",
            ]),
            ("Todoist (REQUIRED)", [
                "TODOIST_API_TOKEN",
            ]),
            ("Zero-Trust Proxy", [
                "LITELLM_PROXY_URL", "LITELLM_PROXY_TOKEN", "LITELLM_PROXY_MODEL",
                "DEMO_MODE", "BRAIN_READ_ONLY",
            ]),
            ("AI Keys (direct â€” local dev only)", [
                "GOOGLE_AI_API_KEY", "GOOGLE_AI_MODEL", "GOOGLE_AI_FALLBACK_MODEL",
                "GOOGLE_AI_LITE_MODEL", "GROQ_API_KEY", "GROQ_MODEL",
            ]),
            ("Email Delivery", [
                "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD",
                "DIGEST_FROM_EMAIL", "DIGEST_TO_EMAIL",
            ]),
            ("Notion Integration", [
                "NOTION_API_TOKEN", "NOTION_ROOT_PAGE_ID",
                "NOTION_COMMAND_CENTER_ID", "NOTION_DAILY_PLANNER_DB_ID",
                "NOTION_WEEKLY_PLANNER_DB_ID", "NOTION_WEEKEND_PLANNER_DB_ID",
                "NOTION_BOARD_DB_ID",
            ]),
            ("WhatsApp (CallMeBot)", [
                "CALLMEBOT_PHONE", "CALLMEBOT_API_KEY",
                "CALLMEBOT_SECONDARY_PHONE", "CALLMEBOT_SECONDARY_API_KEY",
            ]),
            ("Gmail IMAP", [
                "GMAIL_IMAP_HOST", "GMAIL_IMAP_PORT",
                "GMAIL_IMAP_USERNAME", "GMAIL_IMAP_PASSWORD",
            ]),
            ("Google Calendar OAuth", [
                "GOOGLE_OAUTH_CLIENT_SECRETS_FILE", "GOOGLE_OAUTH_TOKEN_FILE",
            ]),
            ("Google OAuth2 (multi-user auth)", [
                "GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET",
                "VELAFLOW_OWNER_EMAIL",
            ]),
            ("NotebookLM", [
                "NOTEBOOKLM_NOTEBOOK_ID", "NOTEBOOKLM_NOTEBOOK_NAME",
            ]),
            ("Ollama / Local LLM", [
                "OLLAMA_BASE_URL", "OLLAMA_CPU_MODEL", "OLLAMA_GPU_MODEL",
            ]),
            ("RAG", [
                "RAG_DUCKDB_PATH", "RAG_CHUNK_SIZE", "RAG_CHUNK_OVERLAP",
            ]),
            ("Schedule", [
                "WORKDAY_START_HOUR", "WORKDAY_END_HOUR",
                "WEEKEND_DAY_START_HOUR", "WEEKEND_DAY_END_HOUR",
                "DEFAULT_TASK_DURATION_MINUTES", "WEEKEND_CAPACITY_HOURS",
            ]),
            ("Digest Limits", [
                "DAILY_TOP_TASK_LIMIT", "OVERDUE_SECTION_LIMIT", "WEEKEND_TASK_LIMIT",
            ]),
            ("Security", [
                "JWT_SECRET", "VELAFLOW_MASTER_KEY",
            ]),
            ("Timezone", [
                "TZ",
            ]),
            ("Logging", [
                "LOG_LEVEL", "LOG_DIR", "LOG_MAX_SIZE_MB", "LOG_RETENTION_DAYS",
            ]),
        ]

        for group_name, keys in groups:
            has_values = any(k in self.values for k in keys)
            if has_values:
                lines.append(f"# â”€â”€ {group_name} {'â”€' * max(1, 50 - len(group_name))}")
                for k in keys:
                    if k in self.values:
                        lines.append(f'{k}="{self._escape_env_value(self.values[k])}"')
                lines.append("")

        # Write remaining keys not in any group
        grouped = {k for _, keys in groups for k in keys}
        extras = {k: v for k, v in self.values.items() if k not in grouped}
        if extras:
            lines.append("# â”€â”€ Other â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€")
            for k, v in sorted(extras.items()):
                lines.append(f'{k}="{self._escape_env_value(v)}"')
            lines.append("")

        content = "\n".join(lines) + "\n"

        # Write atomically
        tmp_path = env_path.with_suffix(".tmp")
        with open(tmp_path, "w") as f:
            f.write(content)

        # Set restrictive permissions on Linux before rename
        if os.name != "nt":
            os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600

        # Atomic rename
        tmp_path.replace(env_path)

        return env_path


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# Secret Generation
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def _generate_secret(length: int = 32) -> str:
    """Generate a cryptographically secure random secret."""
    return secrets.token_urlsafe(length)


def _generate_master_key() -> str:
    """Generate a 256-bit master key (base64url-encoded)."""
    return secrets.token_urlsafe(32)


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# Wizard Steps
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def step_platform(config: ConfigBuilder) -> dict[str, str]:
    """Detect and display platform information."""
    _section("Platform Detection")
    info = detect_platform()

    platform_labels = {
        "windows-local": "Windows (local development)",
        "proxmox-host": "Proxmox VE (hypervisor host)",
        "proxmox-lxc": "Proxmox LXC container",
        "vmware-guest": "VMware virtual machine",
        "oracle-cloud": "Oracle Cloud Infrastructure",
        "kvm-vm": "KVM virtual machine",
        "bare-metal": "Bare metal Linux server",
        "linux-local": "Linux (local)",
    }

    label = platform_labels.get(info["platform"], info["platform"])
    _ok(f"Platform: {_B}{label}{_N}")
    _ok(f"OS: {info['os']} {info['release']}")
    _ok(f"Architecture: {info['arch']}")
    _ok(f"Python: {info['python']}")

    # Validate Python version
    if sys.version_info < (3, 11):
        _fail(f"Python 3.11+ required, found {info['python']}")
        _info("Install Python 3.11+ and re-run the installer.")
        sys.exit(1)

    return info


def step_domain(config: ConfigBuilder) -> None:
    """Configure domain and network settings."""
    _section("Domain & Network")

    print("  The domain determines CORS origins, TLS certificates, and API URLs.")
    print("  Use 'localhost' for local development or testing.")
    print("  You can change this later via the admin panel or by re-running the installer.\n")

    current_domain = config.get("VELAFLOW_DOMAIN", "localhost")
    domain = _prompt("Domain", default=current_domain, validator=_validate_domain)
    config.set("VELAFLOW_DOMAIN", domain)

    port = _prompt("API port", default=config.get("VELAFLOW_API_PORT", "8000"),
                   validator=_validate_port)
    config.set("VELAFLOW_API_PORT", port)

    # Set CORS origins based on domain
    if domain == "localhost":
        origins = f"http://localhost:{port},http://localhost:3000,http://127.0.0.1:{port}"
    else:
        origins = f"https://{domain},https://www.{domain}"
    config.set("CORS_ALLOWED_ORIGINS", origins)

    _ok(f"Domain: {domain}")
    _ok(f"API port: {port}")
    _ok(f"CORS origins: {origins}")


def step_deployment_target(config: ConfigBuilder, platform_info: dict[str, str]) -> str:
    """Choose deployment target."""
    _section("Deployment Target")

    options = [
        ("1", "This machine (local install / VM)"),
        ("2", "Proxmox LXC (will configure container)"),
        ("3", "Docker Compose (containerised stack)"),
    ]

    # Pre-select based on platform
    auto = "1"
    if platform_info["platform"] == "proxmox-host":
        auto = "2"
        _info("Proxmox detected â€” LXC deployment recommended.")
    elif platform_info["platform"] == "proxmox-lxc":
        auto = "1"
        _info("Running inside LXC â€” local install selected.")

    print(f"  Default: [{auto}]\n")
    for key, label in options:
        print(f"  [{key}] {label}")
    print()

    choice = input(f"  Select [{auto}]: ").strip() or auto
    if choice not in {"1", "2", "3"}:
        choice = auto

    targets = {"1": "local", "2": "proxmox-lxc", "3": "docker"}
    return targets[choice]


def step_required_keys(config: ConfigBuilder) -> None:
    """Collect required API tokens."""
    _section("Required API Keys")

    # Todoist
    print("  Todoist API Token (REQUIRED)")
    _info("Get yours at: https://app.todoist.com/app/settings/integrations/developer")
    token = _prompt("Todoist token", default=config.get("TODOIST_API_TOKEN"),
                    secret=True, required=True, validator=_validate_token)
    config.set("TODOIST_API_TOKEN", token)
    _ok("Todoist token set")

    print()

    # AI provider
    print("  AI Provider: Choose how AI features are powered.\n")
    has_proxy = bool(config.get("LITELLM_PROXY_URL"))
    has_gemini = bool(config.get("GOOGLE_AI_API_KEY"))

    options = [
        ("1", "LiteLLM Proxy (recommended â€” zero-trust, keys stay on VPS)"),
        ("2", "Direct Gemini API key (simpler, for local/dev use)"),
        ("3", "Skip AI (deterministic scoring only, no LLM polish)"),
    ]

    default_ai = "1" if has_proxy else ("2" if has_gemini else "1")
    for key, label in options:
        print(f"  [{key}] {label}")
    print()

    ai_choice = input(f"  Select [{default_ai}]: ").strip() or default_ai

    if ai_choice == "1":
        print()
        _info("LiteLLM Proxy â€” your real API keys stay on your VPS.")
        _info("See docs/security.md for setup instructions.")
        url = _prompt("Proxy URL", default=config.get("LITELLM_PROXY_URL", ""),
                      validator=_validate_url)
        config.set("LITELLM_PROXY_URL", url)

        token = _prompt("Proxy token", default=config.get("LITELLM_PROXY_TOKEN"),
                        secret=True, validator=_validate_token)
        config.set("LITELLM_PROXY_TOKEN", token)

        model = _prompt("Proxy model alias",
                        default=config.get("LITELLM_PROXY_MODEL", "gemini/gemini-2.5-flash"))
        config.set("LITELLM_PROXY_MODEL", model)
        _ok("LiteLLM proxy configured")

    elif ai_choice == "2":
        print()
        _info("Get a free Gemini API key at: https://aistudio.google.com/apikey")
        key = _prompt("Gemini API key", default=config.get("GOOGLE_AI_API_KEY"),
                      secret=True, required=True, validator=_validate_token)
        config.set("GOOGLE_AI_API_KEY", key)
        _ok("Gemini API key set")

    else:
        _warn("AI features disabled. Digests will use deterministic scoring only.")


def step_optional_integrations(config: ConfigBuilder) -> None:
    """Configure optional integrations."""
    _section("Optional Integrations")

    # Notion
    if _confirm("Configure Notion integration?", default=bool(config.get("NOTION_API_TOKEN"))):
        print()
        _info("Create at: https://www.notion.so/my-integrations")
        token = _prompt("Notion API token", default=config.get("NOTION_API_TOKEN"),
                        secret=True, validator=_validate_token)
        config.set("NOTION_API_TOKEN", token)

        page_id = _prompt("Notion root page ID (2nd-Brain page)",
                          default=config.get("NOTION_ROOT_PAGE_ID"))
        if page_id:
            config.set("NOTION_ROOT_PAGE_ID", page_id)
        _ok("Notion configured")

    print()

    # Email
    if _confirm("Configure email delivery (daily digests)?",
                default=bool(config.get("SMTP_USERNAME"))):
        print()
        config.set("SMTP_HOST", _prompt("SMTP host", default=config.get("SMTP_HOST", "smtp.gmail.com")))
        config.set("SMTP_PORT", _prompt("SMTP port", default=config.get("SMTP_PORT", "587"),
                                         validator=_validate_port))
        config.set("SMTP_USERNAME", _prompt("SMTP username (email)", validator=_validate_email))
        config.set("SMTP_PASSWORD", _prompt("SMTP password (app password)", secret=True,
                                             validator=_validate_token))
        config.set("DIGEST_FROM_EMAIL", config.get("SMTP_USERNAME"))
        to_email = _prompt("Digest recipient email",
                           default=config.get("DIGEST_TO_EMAIL", config.get("SMTP_USERNAME")),
                           validator=_validate_email)
        config.set("DIGEST_TO_EMAIL", to_email)
        _ok("Email delivery configured")

    print()

    # WhatsApp
    if _confirm("Configure WhatsApp alerts (CallMeBot)?",
                default=bool(config.get("CALLMEBOT_PHONE"))):
        print()
        _info("Register at: https://www.callmebot.com/blog/free-api-whatsapp-messages/")
        config.set("CALLMEBOT_PHONE", _prompt("Phone number (+country code)"))
        config.set("CALLMEBOT_API_KEY", _prompt("CallMeBot API key", secret=True))
        _ok("WhatsApp configured")


def step_security(config: ConfigBuilder) -> None:
    """Generate security secrets."""
    _section("Security Configuration")

    # JWT Secret
    if not config.get("JWT_SECRET"):
        jwt_secret = _generate_secret(48)
        config.set("JWT_SECRET", jwt_secret)
        _ok("JWT secret generated (48-byte random)")
    else:
        _ok("JWT secret already set")

    # Master Key
    if not config.get("VELAFLOW_MASTER_KEY"):
        master_key = _generate_master_key()
        config.set("VELAFLOW_MASTER_KEY", master_key)
        _ok("Master encryption key generated (256-bit)")
    else:
        _ok("Master encryption key already set")

    # Environment mode
    env_mode = config.get("ENVIRONMENT", "development")
    if _confirm(f"Production mode? (currently: {env_mode})",
                default=(env_mode == "production")):
        config.set("ENVIRONMENT", "production")
        _ok("Production mode enabled (Swagger docs disabled, strict headers)")
    else:
        config.set("ENVIRONMENT", "development")
        _ok("Development mode (Swagger docs available at /docs)")


def step_logging(config: ConfigBuilder) -> None:
    """Configure secure logging."""
    _section("Logging Configuration")

    print("  VelaFlow includes secure structured logging with:")
    print("  - Automatic PII/secret redaction")
    print("  - JSON structured format for analysis")
    print("  - Log rotation with configurable retention")
    print("  - Tamper-evident log chain (HMAC)")
    print("  - Safe export command for debugging with Copilot\n")

    level = _prompt("Log level", default=config.get("LOG_LEVEL", "INFO"))
    config.set("LOG_LEVEL", level.upper())

    log_dir = _prompt("Log directory", default=config.get("LOG_DIR", str(LOG_DIR)))
    config.set("LOG_DIR", log_dir)

    max_size = _prompt("Max log file size (MB)", default=config.get("LOG_MAX_SIZE_MB", "50"))
    config.set("LOG_MAX_SIZE_MB", max_size)

    retention = _prompt("Log retention (days)", default=config.get("LOG_RETENTION_DAYS", "30"))
    config.set("LOG_RETENTION_DAYS", retention)

    _ok(f"Logging: {level.upper()} â†’ {log_dir}")
    _ok(f"Rotation: {max_size}MB files, {retention} day retention")


def step_timezone(config: ConfigBuilder) -> None:
    """Configure timezone."""
    _section("Timezone")
    tz = _prompt("Timezone", default=config.get("TZ", "Europe/Lisbon"))
    config.set("TZ", tz)
    _ok(f"Timezone: {tz}")


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# Installation Actions
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def action_create_directories() -> None:
    """Create required data directories."""
    _section("Creating Directories")

    dirs = [
        DATA_DIR,
        DATA_DIR / "medallion" / "bronze",
        DATA_DIR / "medallion" / "silver",
        DATA_DIR / "medallion" / "gold",
        DATA_DIR / "medallion" / "tenants",
        DATA_DIR / "medallion" / "runs",
        LOG_DIR,
    ]

    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        _ok(f"Directory: {d.relative_to(PROJECT_ROOT)}")


def action_install_dependencies() -> None:
    """Install Python dependencies."""
    _section("Installing Dependencies")

    python = sys.executable
    _info(f"Using Python: {python}")

    # Upgrade pip
    subprocess.run(
        [python, "-m", "pip", "install", "--upgrade", "pip"],
        capture_output=True, check=False,
    )

    # Install package with enterprise extras
    _info("Installing VelaFlow with enterprise dependencies...")
    result = subprocess.run(
        [python, "-m", "pip", "install", "-e", ".[all]"],
        cwd=str(PROJECT_ROOT),
        capture_output=True, text=True, check=False,
    )

    if result.returncode == 0:
        _ok("Dependencies installed successfully")
    else:
        _warn("Some dependencies may have failed:")
        # Show last 5 lines of error
        for line in result.stderr.strip().split("\n")[-5:]:
            print(f"    {line}")


# -----------------------------------------------------------------------------
# Autonomous system bootstrap (fresh Proxmox-LXC / VMware guest / OCI Always-Free)
# -----------------------------------------------------------------------------
# VelaFlow must be deployable on a vanilla Debian/Ubuntu host without the
# operator pre-installing anything beyond Python + git. These helpers detect
# apt and run the minimum privileged commands to pull in docker, terraform,
# and the basic tooling the installer itself relies on. If apt is not
# present (e.g. Alpine, RHEL derivatives) they exit gracefully so the
# operator can supply the tools by hand.


def _have(binary: str) -> bool:
    return shutil.which(binary) is not None


def _run_bootstrap(cmd: list[str]) -> int:
    """Run a privileged bootstrap command, printing the trimmed invocation.

    All commands pass list arguments only, never ``shell=True``, and resolve
    their executable from PATH. Used only for system bootstrap.
    """
    _info("  $ " + " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
        for line in tail:
            print(f"    {line}")
    return result.returncode


def _sudo(cmd: list[str]) -> list[str]:
    """Prefix a command with sudo when not running as root."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return cmd
    sudo_bin = shutil.which("sudo")
    if sudo_bin is None:
        return cmd
    return [sudo_bin, *cmd]


def action_bootstrap_system() -> None:
    """Install OS-level tooling the rest of the installer depends on.

    On Debian/Ubuntu: installs curl, unzip, ca-certificates, gnupg, docker,
    docker-compose-plugin and terraform. Idempotent: anything already on
    PATH is skipped. Non-Debian hosts are no-ops with a clear hint.
    """
    _section("Bootstrapping system tooling (autonomous)")

    apt_get = shutil.which("apt-get")
    if apt_get is None:
        _warn("apt-get not found — assuming non-Debian host.")
        _warn("Install curl, unzip, docker, docker-compose-plugin, terraform manually.")
        return

    pkgs = ["curl", "unzip", "ca-certificates", "gnupg", "lsb-release"]
    missing = [p for p in pkgs if not _have(p)]
    if missing:
        _info(f"Installing base packages: {', '.join(missing)}")
        _run_bootstrap(_sudo([apt_get, "update", "-y"]))
        _run_bootstrap(_sudo([apt_get, "install", "-y", *missing]))
    else:
        _ok("Base packages already present")

    # Docker (official convenience script — idempotent, runs apt under the hood).
    if not _have("docker"):
        _info("Installing Docker Engine via get.docker.com")
        curl = shutil.which("curl")
        sh = shutil.which("sh")
        if curl and sh:
            dl = subprocess.run(
                [curl, "-fsSL", "https://get.docker.com"],
                capture_output=True, text=True, check=False,
            )
            if dl.returncode == 0 and dl.stdout:
                script_path = PROJECT_ROOT / "data" / "_docker-get.sh"
                script_path.parent.mkdir(parents=True, exist_ok=True)
                script_path.write_text(dl.stdout, encoding="utf-8")
                _run_bootstrap(_sudo([sh, str(script_path)]))
                try:
                    script_path.unlink()
                except OSError as exc:
                    logger_exc = exc  # keep name to avoid unused-var warning
                    _info(f"(cleanup) could not remove {script_path}: {logger_exc}")
            else:
                _warn("Docker bootstrap download failed — install docker manually.")
        else:
            _warn("curl or sh missing — cannot bootstrap Docker automatically.")
    else:
        _ok("Docker already installed")

    # Terraform — pinned major via apt repo from HashiCorp.
    if not _have("terraform"):
        _info("Installing Terraform from HashiCorp apt repo")
        gpg = shutil.which("gpg")
        tee = shutil.which("tee")
        curl = shutil.which("curl")
        if gpg and tee and curl:
            keyring = "/usr/share/keyrings/hashicorp-archive-keyring.gpg"
            list_path = "/etc/apt/sources.list.d/hashicorp.list"
            # Download key and dearmor — all list-arg subprocess calls.
            dl = subprocess.run(
                [curl, "-fsSL", "https://apt.releases.hashicorp.com/gpg"],
                capture_output=True, check=False,
            )
            if dl.returncode == 0 and dl.stdout:
                key_tmp = PROJECT_ROOT / "data" / "_hashicorp.asc"
                key_tmp.parent.mkdir(parents=True, exist_ok=True)
                key_tmp.write_bytes(dl.stdout)
                _run_bootstrap(_sudo([gpg, "--dearmor", "-o", keyring, str(key_tmp)]))
                try:
                    key_tmp.unlink()
                except OSError:
                    pass
                codename_res = subprocess.run(
                    [shutil.which("lsb_release") or "/usr/bin/lsb_release", "-cs"],
                    capture_output=True, text=True, check=False,
                )
                codename = (codename_res.stdout or "stable").strip() or "stable"
                repo_line = (
                    f"deb [signed-by={keyring}] "
                    f"https://apt.releases.hashicorp.com {codename} main\n"
                )
                list_tmp = PROJECT_ROOT / "data" / "_hashicorp.list"
                list_tmp.write_text(repo_line, encoding="utf-8")
                _run_bootstrap(_sudo([
                    shutil.which("install") or "/usr/bin/install",
                    "-m", "0644", str(list_tmp), list_path,
                ]))
                try:
                    list_tmp.unlink()
                except OSError:
                    pass
                _run_bootstrap(_sudo([apt_get, "update", "-y"]))
                _run_bootstrap(_sudo([apt_get, "install", "-y", "terraform"]))
            else:
                _warn("HashiCorp key download failed — install terraform manually.")
        else:
            _warn("gpg/tee/curl missing — cannot bootstrap Terraform automatically.")
    else:
        _ok("Terraform already installed")

    # Final summary.
    for tool in ("docker", "terraform", "curl", "unzip"):
        status = "present" if _have(tool) else "MISSING"
        (_ok if status == "present" else _warn)(f"{tool}: {status}")


def action_detect_hardware() -> None:
    """Probe nvidia-smi and cache the result in ``data/hardware.json``.

    The product runtime (``brain.llm_local.detect_hardware``) reads this
    cache and never shells out. Running this action is optional: if the
    cache is missing the runtime falls back to CPU-only mode.
    """
    _section("Detecting hardware (GPU/CPU)")

    cache_path = DATA_DIR / "hardware.json"
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi is None:
        cache_path.write_text(
            json.dumps({"has_gpu": False, "gpu_name": "none", "gpu_memory_mb": 0}),
            encoding="utf-8",
        )
        _ok(f"No NVIDIA GPU detected — wrote CPU profile to {cache_path}")
        return

    result = subprocess.run(
        [nvidia_smi, "--query-gpu=name,memory.total",
         "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        cache_path.write_text(
            json.dumps({"has_gpu": False, "gpu_name": "none", "gpu_memory_mb": 0}),
            encoding="utf-8",
        )
        _warn(f"nvidia-smi returned rc={result.returncode} — wrote CPU profile")
        return

    line = result.stdout.strip().splitlines()[0]
    parts = [p.strip() for p in line.split(",")]
    gpu_name = parts[0] if parts else "unknown"
    try:
        gpu_mem = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        gpu_mem = 0

    payload = {
        "has_gpu": True,
        "gpu_name": gpu_name,
        "gpu_memory_mb": gpu_mem,
    }
    cache_path.write_text(json.dumps(payload), encoding="utf-8")
    _ok(f"GPU cached: {gpu_name} ({gpu_mem} MB) -> {cache_path}")


def action_verify_installation() -> None:
    """Run basic import checks."""
    _section("Verifying Installation")

    checks = [
        ("brain", "import brain"),
        ("brain.config", "from brain.config import Settings"),
        ("brain.api.app", "from brain.api.app import create_app"),
        ("brain.planner", "from brain.planner import score_task"),
        ("brain.security.pii", "from brain.security.pii import PIIDetector"),
        ("brain.security.encryption", "from brain.security.encryption import FieldEncryptor"),
        ("brain.security.audit_log", "from brain.security.audit_log import AuditLog"),
        ("brain.tenant.demo_manager", "from brain.tenant.demo_manager import DemoManager"),
        ("brain.engine.connection", "from brain.engine.connection import DuckDBConnectionPool"),
    ]

    python = sys.executable
    passed = 0
    for name, import_stmt in checks:
        result = subprocess.run(
            [python, "-c", import_stmt],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode == 0:
            _ok(f"{name}")
            passed += 1
        else:
            _fail(f"{name}: {result.stderr.strip().split(chr(10))[-1][:80]}")

    print(f"\n  {passed}/{len(checks)} import checks passed")


def action_test_config(config: ConfigBuilder) -> None:
    """Test that configuration loads correctly."""
    _section("Testing Configuration")

    python = sys.executable
    test_code = """
import os, sys
os.environ.setdefault('TODOIST_API_TOKEN', 'test')
sys.path.insert(0, 'src')
from brain.config import Settings
s = Settings.from_env()
print(f"Settings loaded: {len([f for f in dir(s) if not f.startswith('_')])} fields")
print(f"Todoist token: {'set' if s.todoist_api_token else 'MISSING'}")
print(f"Domain: {os.environ.get('VELAFLOW_DOMAIN', 'not set')}")
"""
    result = subprocess.run(
        [python, "-c", test_code],
        cwd=str(PROJECT_ROOT),
        capture_output=True, text=True, timeout=30, check=False,
    )

    if result.returncode == 0:
        for line in result.stdout.strip().split("\n"):
            _ok(line.strip())
    else:
        _fail(f"Config test failed: {result.stderr.strip()[:120]}")


def action_start_api(config: ConfigBuilder) -> int | None:
    """Start the FastAPI server for testing."""
    _section("Starting API Server")

    port = config.get("VELAFLOW_API_PORT", "8000")
    domain = config.get("VELAFLOW_DOMAIN", "localhost")

    _info(f"Starting VelaFlow API on port {port}...")
    _info(f"API docs: http://localhost:{port}/docs")
    _info("Press Ctrl+C to stop\n")

    python = sys.executable
    env = os.environ.copy()
    env["VELAFLOW_API_PORT"] = port
    env["CORS_ALLOWED_ORIGINS"] = config.get("CORS_ALLOWED_ORIGINS", f"http://localhost:{port}")

    # Bind to loopback by default; operators can expose publicly only
    # via a reverse proxy that terminates TLS and enforces auth. The
    # installer does not ship a public-facing listener.
    bind_host = os.environ.get("VELAFLOW_BIND_HOST", "127.0.0.1")
    try:
        proc = subprocess.Popen(
            [python, "-m", "uvicorn", "brain.api.app:create_app",
             "--factory", "--host", bind_host, "--port", port,
             "--log-level", "info"],
            cwd=str(PROJECT_ROOT),
            env=env,
        )
        return proc.pid
    except FileNotFoundError:
        _fail("uvicorn not found. Install with: pip install uvicorn[standard]")
        return None


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# Log Export (secure, sanitised)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

# Patterns to redact from log exports
_REDACT_PATTERNS = [
    (re.compile(r'(TODOIST_API_TOKEN|NOTION_API_TOKEN|GOOGLE_AI_API_KEY|GROQ_API_KEY|'
                r'SMTP_PASSWORD|JWT_SECRET|VELAFLOW_MASTER_KEY|LITELLM_PROXY_TOKEN|'
                r'CALLMEBOT_API_KEY|GOOGLE_OAUTH_CLIENT_SECRET|GMAIL_IMAP_PASSWORD)'
                r'\s*[=:]\s*\S+', re.IGNORECASE),
     r'\1=***REDACTED***'),
    (re.compile(r'(Bearer|token|sk-|key-|AIza)\S{8,}', re.IGNORECASE),
     '***TOKEN_REDACTED***'),
    (re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'),
     '***EMAIL_REDACTED***'),
    (re.compile(r'\b\d{3}[-.]?\d{3}[-.]?\d{4}\b'),
     '***PHONE_REDACTED***'),
    (re.compile(r'\+\d{1,3}\d{9,12}\b'),
     '***PHONE_REDACTED***'),
    (re.compile(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b'),
     '***IP_REDACTED***'),
]


def sanitize_log_content(content: str) -> str:
    """Remove secrets, PII, and sensitive data from log content."""
    result = content
    for pattern, replacement in _REDACT_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def export_logs(output_path: Path | None = None) -> Path:
    """Export sanitised logs suitable for debugging with Copilot."""
    _banner("VelaFlow â€” Secure Log Export")

    log_dir = LOG_DIR
    if not log_dir.is_dir():
        # Fall back to checking common log locations
        alt_dirs = [
            PROJECT_ROOT / "logs",
            Path("/var/log/brain"),
            Path("/var/log/velaflow"),
        ]
        for alt in alt_dirs:
            if alt.is_dir():
                log_dir = alt
                break

    if not log_dir.is_dir():
        _warn("No log directory found. Creating empty export.")
        log_dir = LOG_DIR
        log_dir.mkdir(parents=True, exist_ok=True)

    # Collect log files
    log_files = sorted(log_dir.glob("*.log")) + sorted(log_dir.glob("*.json"))

    if not output_path:
        output_path = PROJECT_ROOT / f"velaflow-logs-{time.strftime('%Y%m%d-%H%M%S')}.md"

    lines = [
        "# VelaFlow â€” Sanitised Debug Log Export",
        f"",
        f"Exported: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"Platform: {platform.platform()}",
        f"Python: {platform.python_version()}",
        f"VelaFlow: {VELAFLOW_VERSION}",
        "",
        "---",
        "",
        "> **Note:** All secrets, tokens, emails, phone numbers, and IP addresses",
        "> have been automatically redacted. This file is safe to paste into",
        "> GitHub Copilot Chat or a GitHub Issue for debugging assistance.",
        "",
    ]

    if not log_files:
        lines.append("*No log files found.*\n")
        lines.append("If the system has been running, check:\n")
        lines.append("- `journalctl -u brain-daily` (systemd deployments)")
        lines.append("- `logs/` directory (local installations)")
        lines.append("- Docker container logs: `docker logs velaflow-api`")
    else:
        for lf in log_files[-5:]:  # Last 5 log files only
            lines.append(f"## {lf.name}")
            lines.append("")
            try:
                content = lf.read_text(errors="replace")
                # Only last 200 lines per file
                content_lines = content.strip().split("\n")
                if len(content_lines) > 200:
                    lines.append(f"*({len(content_lines)} total lines, showing last 200)*\n")
                    content_lines = content_lines[-200:]

                sanitised = sanitize_log_content("\n".join(content_lines))
                lines.append("```")
                lines.append(sanitised)
                lines.append("```")
                lines.append("")
            except Exception as e:
                lines.append(f"*Error reading {lf.name}: {e}*\n")

    # Add system info for debugging context
    lines.extend([
        "## System Information",
        "",
        "```",
        f"OS: {platform.platform()}",
        f"Python: {sys.version}",
        f"Working Directory: {os.getcwd()}",
        f"VelaFlow Root: {PROJECT_ROOT}",
        "```",
        "",
    ])

    output_path.write_text("\n".join(lines))
    _ok(f"Sanitised logs exported to: {output_path}")
    _info("Safe to paste into GitHub Copilot Chat or create a GitHub Issue.")
    _info("All secrets, tokens, and PII have been automatically removed.")

    return output_path


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# Health Check (Python-native, cross-platform)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def run_health_check() -> int:
    """Cross-platform health check (works on Windows, Linux, macOS)."""
    _banner("VelaFlow â€” Health Check")

    passed = 0
    failed = 0
    warnings = 0
    total = 0

    def check(label: str, test_fn) -> bool:
        nonlocal passed, failed, total
        total += 1
        try:
            result = test_fn()
            if result:
                _ok(label)
                passed += 1
                return True
            else:
                _fail(label)
                failed += 1
                return False
        except Exception as e:
            _fail(f"{label}: {e}")
            failed += 1
            return False

    def check_warn(label: str, test_fn) -> bool:
        nonlocal passed, warnings, total
        total += 1
        try:
            result = test_fn()
            if result:
                _ok(label)
                passed += 1
                return True
            else:
                _warn(f"{label} (non-critical)")
                warnings += 1
                return True
        except Exception:
            _warn(f"{label} (non-critical)")
            warnings += 1
            return True

    python = sys.executable

    def _import_check(stmt: str) -> bool:
        r = subprocess.run(
            [python, "-c", stmt], capture_output=True, timeout=30, check=False
        )
        return r.returncode == 0

    # 1. System Prerequisites
    _section("System Prerequisites")
    check("Python 3.11+", lambda: sys.version_info >= (3, 11))
    check("Project root exists", lambda: PROJECT_ROOT.is_dir())
    check("src/brain/ exists", lambda: (PROJECT_ROOT / "src" / "brain").is_dir())
    check_warn("config/.env exists", lambda: (CONFIG_DIR / ".env").is_file())
    check_warn("logs/ directory exists", lambda: LOG_DIR.is_dir())
    check_warn("data/ directory exists", lambda: DATA_DIR.is_dir())

    # 2. Python Environment
    _section("Python Environment")
    check("brain package importable",
          lambda: _import_check("import brain"))
    check("Settings loadable",
          lambda: _import_check(
              "import os; os.environ.setdefault('TODOIST_API_TOKEN','t');"
              "from brain.config import Settings; Settings.from_env()"))
    check("FastAPI app creates",
          lambda: _import_check(
              "import os; os.environ.setdefault('TODOIST_API_TOKEN','t');"
              "os.environ.setdefault('JWT_SECRET','test');"
              "os.environ.setdefault('VELAFLOW_MASTER_KEY','test_key_32_bytes_long_enough!!');"
              "from brain.api.app import create_app; create_app()"))
    check("PII detector loads",
          lambda: _import_check(
              "from brain.security.pii import PIIDetector; "
              "d = PIIDetector(); assert d.detect('hello') == []"))
    check("Encryption round-trip",
          lambda: _import_check(
              "import base64, secrets; "
              "from brain.security.encryption import FieldEncryptor; "
              "k = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(); "
              "e = FieldEncryptor(k); "
              "assert e.decrypt(e.encrypt('test', 'healthcheck'), 'healthcheck') == 'test'"))
    check("Task scoring",
          lambda: _import_check(
              "from brain.planner import score_task; from brain.models import Task; "
              "from brain.config import Settings; "
              "t = Task(id='1', content='Test', project_name='Inbox', priority=4); "
              "s = score_task(t, Settings()); assert s.score >= 0"))

    # 3. Security Components
    _section("Security Components")
    check("AuditLog importable",
          lambda: _import_check("from brain.security.audit_log import EncryptedAuditLog"))
    check("RBAC importable",
          lambda: _import_check("from brain.security.rbac import RBACPolicy"))
    check("BanManager importable",
          lambda: _import_check("from brain.security.ban import BanManager"))
    check("Sanitization importable",
          lambda: _import_check("from brain.security.sanitization import sanitize_text"))
    check("ZeroTrust importable",
          lambda: _import_check("from brain.security.zero_trust import RequestSigner"))
    check_warn("SecureLogging importable",
               lambda: _import_check("from brain.security.secure_logging import SecureLogger"))

    # 4. Enterprise Components
    _section("Enterprise Components")
    check("DuckDB engine",
          lambda: _import_check("from brain.engine.connection import DuckDBEngine"))
    check("Medallion pipeline",
          lambda: _import_check("from brain.pipeline.bronze import BronzeLayer"))
    check("Data catalog",
          lambda: _import_check("from brain.catalog.store import CatalogStore"))
    check("Queue worker",
          lambda: _import_check("from brain.queue.worker import QueueWorker"))
    check("Tenant manager",
          lambda: _import_check("from brain.tenant.manager import TenantManager"))
    check("Demo manager",
          lambda: _import_check("from brain.tenant.demo_manager import DemoManager"))

    # 5. Network (optional)
    _section("Network Connectivity")
    check_warn("DNS resolution",
               lambda: _import_check(
                   "import socket; socket.getaddrinfo('api.todoist.com', 443)"))
    check_warn("HTTPS outbound (Todoist)",
               lambda: _import_check(
                   "import urllib.request; "
                   "urllib.request.urlopen('https://api.todoist.com', timeout=10)"))

    # Summary
    _section("Summary")
    if failed == 0 and warnings == 0:
        print(f"  {_G}ALL {total} CHECKS PASSED{_N}")
        return 0
    elif failed == 0:
        print(f"  {_G}{passed}/{total} passed{_N}, {_Y}{warnings} warnings{_N}")
        print(f"  {_Y}Warnings are non-critical â€” system is functional.{_N}")
        return 0
    else:
        print(f"  {_R}{failed} FAILED{_N}, {_G}{passed} passed{_N}, {_Y}{warnings} warnings{_N}")
        print(f"  {_R}Fix the failed checks before using the system.{_N}")
        return 1


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# Main Wizard Flow
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def wizard_quick(config: ConfigBuilder) -> None:
    """Quick setup: platform detect, required keys, generate secrets, done."""
    _clear_screen()
    _banner("VelaFlow â€” Quick Setup")

    platform_info = step_platform(config)
    step_domain(config)
    step_required_keys(config)
    step_security(config)
    step_logging(config)
    step_timezone(config)

    # Write config
    _section("Writing Configuration")
    env_path = config.write()
    _ok(f"Configuration written to: {env_path}")

    if os.name != "nt":
        _ok(f"File permissions: 0600 (owner read/write only)")

    # Create directories
    action_create_directories()

    # Bootstrap OS tooling (docker, terraform, curl, unzip) for fresh hosts.
    if _confirm("Bootstrap system tooling (docker, terraform) via apt? (skip on non-Debian)"):
        action_bootstrap_system()

    # Cache hardware profile so the runtime never shells out.
    action_detect_hardware()

    # Install dependencies
    if _confirm("Install/update Python dependencies?"):
        action_install_dependencies()

    # Verify
    action_verify_installation()
    action_test_config(config)

    _banner("Setup Complete")
    print("  Next steps:")
    _info("Run the daily briefing: brain daily --stdout")
    _info("Set up Notion:         brain notion-setup")
    _info("Start the API server:  brain api --port 8000")
    _info("Run health check:      python scripts/installer.py --health")
    _info("Export logs:            python scripts/installer.py --export-logs")


def wizard_full(config: ConfigBuilder) -> None:
    """Full setup: every option configurable."""
    _clear_screen()
    _banner("VelaFlow â€” Full Setup Wizard")

    platform_info = step_platform(config)
    target = step_deployment_target(config, platform_info)
    step_domain(config)
    step_required_keys(config)
    step_optional_integrations(config)
    step_security(config)
    step_logging(config)
    step_timezone(config)

    # Write config
    _section("Writing Configuration")
    env_path = config.write()
    _ok(f"Configuration written to: {env_path}")

    if os.name != "nt":
        _ok(f"File permissions: 0600 (owner read/write only)")

    # Create directories
    action_create_directories()

    if _confirm("Bootstrap system tooling (docker, terraform) via apt? (skip on non-Debian)"):
        action_bootstrap_system()
    action_detect_hardware()

    # Install dependencies
    if _confirm("Install/update Python dependencies?"):
        action_install_dependencies()

    # Verify
    action_verify_installation()
    action_test_config(config)

    _banner("Setup Complete")
    _ok(f"Target: {target}")
    _ok(f"Config: {env_path}")
    print()
    print("  Next steps:")
    _info("Run the daily briefing: brain daily --stdout")
    _info("Set up Notion:         brain notion-setup")
    _info("Start the API server:  brain api --port 8000")
    _info("Run health check:      python scripts/installer.py --health")

    if target == "proxmox-lxc":
        print()
        _info("For full Proxmox LXC deployment:")
        _info("  bash scripts/deploy-full-stack.sh --config config/.env.deploy")

    if target == "docker":
        print()
        _info("For Docker Compose deployment:")
        _info("  docker-compose up -d")


def wizard_reconfigure(config: ConfigBuilder) -> None:
    """Edit a specific section of existing configuration."""
    _clear_screen()

    sections = [
        ("1", "Domain & Network",         step_domain),
        ("2", "API Keys & AI Provider",   step_required_keys),
        ("3", "Optional Integrations",    step_optional_integrations),
        ("4", "Security (regenerate keys)", step_security),
        ("5", "Logging",                  step_logging),
        ("6", "Timezone",                 step_timezone),
        ("0", "Back to main menu",        None),
    ]

    while True:
        choice = _menu(
            "VelaFlow â€” Reconfigure",
            [(k, label) for k, label, _ in sections],
        )
        if choice == "0":
            break

        for k, label, fn in sections:
            if k == choice and fn:
                fn(config)
                break

        # Save after each section
        env_path = config.write()
        _ok(f"Configuration saved: {env_path}")


def main() -> None:
    """Main entry point for the VelaFlow installer wizard."""
    # Parse CLI flags
    if "--bootstrap" in sys.argv:
        action_bootstrap_system()
        action_detect_hardware()
        return
    if "--detect-hardware" in sys.argv:
        action_detect_hardware()
        return
    if "--quick" in sys.argv:
        config = ConfigBuilder()
        wizard_quick(config)
        return
    if "--health" in sys.argv:
        code = run_health_check()
        sys.exit(code)
    if "--export-logs" in sys.argv:
        export_logs()
        return

    # Interactive main menu
    config = ConfigBuilder()

    while True:
        _clear_screen()
        choice = _menu(
            f"VelaFlow v{VELAFLOW_VERSION} â€” Setup Wizard",
            [
                ("1", "Quick Setup  (recommended defaults + required keys)"),
                ("2", "Full Setup   (configure every option)"),
                ("3", "Reconfigure  (edit existing installation)"),
                ("4", "Health Check (validate current installation)"),
                ("5", "Export Logs  (sanitised, safe for Copilot debugging)"),
                ("0", "Exit"),
            ],
        )

        if choice == "0":
            print("\n  Goodbye.\n")
            break
        elif choice == "1":
            wizard_quick(config)
            input("\n  Press Enter to continue...")
        elif choice == "2":
            wizard_full(config)
            input("\n  Press Enter to continue...")
        elif choice == "3":
            wizard_reconfigure(config)
        elif choice == "4":
            run_health_check()
            input("\n  Press Enter to continue...")
        elif choice == "5":
            export_logs()
            input("\n  Press Enter to continue...")


if __name__ == "__main__":
    main()
