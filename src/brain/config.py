"""Configuration loaded from config/.env or environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


def _find_env_file() -> Path | None:
    """Walk up from CWD looking for config/.env or .env."""
    candidates = [
        Path.cwd() / "config" / ".env",
        Path.cwd() / ".env",
        Path(__file__).resolve().parent.parent.parent / "config" / ".env",
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None


@dataclass(frozen=True)
class Settings:
    # Todoist
    todoist_api_token: str = ""

    # SMTP
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    digest_from_email: str = ""
    digest_to_email: str = ""

    # Groq
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"

    # Google AI Studio (primary for planning)
    google_ai_api_key: str = ""
    google_ai_model: str = "gemini-2.5-pro"
    google_ai_fallback_model: str = "gemini-2.5-flash"
    google_ai_lite_model: str = "gemini-2.5-flash-lite"

    # WhatsApp / CallMeBot
    callmebot_phone: str = ""
    callmebot_api_key: str = ""
    callmebot_secondary_phone: str = ""
    callmebot_secondary_api_key: str = ""

    # Gmail IMAP
    gmail_imap_host: str = "imap.gmail.com"
    gmail_imap_port: int = 993
    gmail_imap_username: str = ""
    gmail_imap_password: str = ""
    gmail_important_query: str = 'X-GM-RAW "label:important is:unread newer_than:1d"'

    # Google Calendar OAuth
    google_oauth_client_secrets_file: str = "credentials.json"
    google_oauth_token_file: str = ".google-token.json"

    # Todoist labels
    todoist_focus_label: str = "focus"
    todoist_weekend_label: str = "weekend"
    todoist_kanban_project_id: str = ""

    # Todoist planner section IDs (populated by brain notion-setup)
    todoist_daily_planner_section_id: str = ""
    todoist_weekly_planner_section_id: str = ""
    todoist_weekend_planner_section_id: str = ""

    # Notion integration
    notion_api_token: str = ""
    notion_root_page_id: str = ""          # 2nd-Brain page ID
    notion_command_center_id: str = ""
    notion_daily_planner_db_id: str = ""
    notion_weekly_planner_db_id: str = ""
    notion_weekend_planner_db_id: str = ""
    notion_board_db_id: str = ""

    # NotebookLM integration
    notebooklm_notebook_id: str = ""           # set after first brain notebooklm-sync run
    notebooklm_notebook_name: str = "VelaFlow"  # used when creating the notebook

    # ── Zero-Trust Proxy (demo / LXC handover) ──────────────────────────────
    # When set, ALL AI calls are routed through the LiteLLM proxy instead of
    # calling Google AI / Groq directly. Real API keys stay on your VPS.
    # See docs/security.md for the full Zero-Trust architecture.
    litellm_proxy_url: str = ""           # e.g. https://proxy.yourserver.com
    litellm_proxy_token: str = ""         # e.g. your-proxy-token  (budget-capped)
    litellm_proxy_model: str = "gemini/gemini-2.5-flash"  # model alias on the proxy
    demo_mode: bool = False               # True → proxy required, real keys ignored
    brain_read_only: bool = False         # True → no writes back to Todoist (demo safety)

    # ── Ollama / Local LLM ────────────────────────────────────────────────────
    ollama_base_url: str = "http://localhost:11434"  # Ollama API endpoint
    ollama_cpu_model: str = "qwen2:1.5b"             # CPU-friendly model
    ollama_gpu_model: str = "qwen2:7b"               # GPU-capable model

    # ── RAG (Retrieval-Augmented Generation) ──────────────────────────────────
    rag_duckdb_path: str = ""              # DuckDB vector store path (default: data/medallion/rag.duckdb)
    rag_chunk_size: int = 512              # Max tokens per chunk
    rag_chunk_overlap: int = 64            # Overlap between chunks

    # ── Google OAuth2 (multi-user auth) ──────────────────────────────────────
    # Required for POST /api/v1/auth/google login flow.
    # Create at https://console.cloud.google.com/apis/credentials
    google_oauth_client_id: str = ""      # OAuth 2.0 Client ID
    google_oauth_client_secret: str = ""  # OAuth 2.0 Client Secret (backend only)
    velaflow_owner_email: str = ""  # Platform owner auto-provision (set VELAFLOW_OWNER_EMAIL)

    # Schedule
    workday_start_hour: int = 9
    workday_end_hour: int = 18
    weekend_day_start_hour: int = 9
    weekend_day_end_hour: int = 18
    default_task_duration_minutes: int = 30
    weekend_capacity_hours: int = 3

    # Digest limits
    daily_top_task_limit: int = 5
    overdue_section_limit: int = 7
    weekend_task_limit: int = 8

    # Timezone
    tz: str = "Europe/Lisbon"

    # ── Domain & Network ───────────────────────────────────────────────────
    velaflow_domain: str = "localhost"       # API domain (CORS, TLS)
    velaflow_api_port: int = 8000            # API server port

    # ── Secure Logging ────────────────────────────────────────────────────
    log_level: str = "INFO"                  # DEBUG, INFO, WARNING, ERROR
    log_dir: str = "logs"                    # Log file directory
    log_max_size_mb: int = 50                # Max log file size before rotation
    log_retention_days: int = 30             # Days to keep rotated logs

    @classmethod
    def from_env(cls) -> Settings:
        env_file = _find_env_file()
        if env_file:
            load_dotenv(env_file, override=False)

        def _get(key: str, default: str = "") -> str:
            return os.environ.get(key, default)

        def _int(key: str, default: int) -> int:
            val = os.environ.get(key, "")
            if val.strip():
                return int(val)
            return default

        return cls(
            todoist_api_token=_get("TODOIST_API_TOKEN"),
            smtp_host=_get("SMTP_HOST", "smtp.gmail.com"),
            smtp_port=_int("SMTP_PORT", 587),
            smtp_username=_get("SMTP_USERNAME"),
            smtp_password=_get("SMTP_PASSWORD"),
            digest_from_email=_get("DIGEST_FROM_EMAIL"),
            digest_to_email=_get("DIGEST_TO_EMAIL"),
            groq_api_key=_get("GROQ_API_KEY"),
            groq_model=_get("GROQ_MODEL", "llama-3.3-70b-versatile"),
            google_ai_api_key=_get("GOOGLE_AI_API_KEY"),
            google_ai_model=_get("GOOGLE_AI_MODEL", "gemini-2.5-pro"),
            google_ai_fallback_model=_get("GOOGLE_AI_FALLBACK_MODEL", "gemini-2.5-flash"),
            google_ai_lite_model=_get("GOOGLE_AI_LITE_MODEL", "gemini-2.5-flash-lite"),
            callmebot_phone=_get("CALLMEBOT_PHONE"),
            callmebot_api_key=_get("CALLMEBOT_API_KEY"),
            callmebot_secondary_phone=_get("CALLMEBOT_SECONDARY_PHONE"),
            callmebot_secondary_api_key=_get("CALLMEBOT_SECONDARY_API_KEY"),
            gmail_imap_host=_get("GMAIL_IMAP_HOST", "imap.gmail.com"),
            gmail_imap_port=_int("GMAIL_IMAP_PORT", 993),
            gmail_imap_username=_get("GMAIL_IMAP_USERNAME"),
            gmail_imap_password=_get("GMAIL_IMAP_PASSWORD"),
            gmail_important_query=_get(
                "GMAIL_IMPORTANT_QUERY",
                'X-GM-RAW "label:important is:unread newer_than:1d"',
            ),
            google_oauth_client_secrets_file=_get(
                "GOOGLE_OAUTH_CLIENT_SECRETS_FILE", "credentials.json"
            ),
            google_oauth_token_file=_get(
                "GOOGLE_OAUTH_TOKEN_FILE", ".google-token.json"
            ),
            todoist_focus_label=_get("TODOIST_FOCUS_LABEL", "focus"),
            todoist_weekend_label=_get("TODOIST_WEEKEND_LABEL", "weekend"),
            todoist_kanban_project_id=_get("TODOIST_KANBAN_PROJECT_ID", ""),
            todoist_daily_planner_section_id=_get("TODOIST_DAILY_PLANNER_SECTION_ID", ""),
            todoist_weekly_planner_section_id=_get("TODOIST_WEEKLY_PLANNER_SECTION_ID", ""),
            todoist_weekend_planner_section_id=_get("TODOIST_WEEKEND_PLANNER_SECTION_ID", ""),
            notion_api_token=_get("NOTION_API_TOKEN"),
            notion_root_page_id=_get("NOTION_ROOT_PAGE_ID", ""),
            notion_command_center_id=_get("NOTION_COMMAND_CENTER_ID", ""),
            notion_daily_planner_db_id=_get("NOTION_DAILY_PLANNER_DB_ID", ""),
            notion_weekly_planner_db_id=_get("NOTION_WEEKLY_PLANNER_DB_ID", ""),
            notion_weekend_planner_db_id=_get("NOTION_WEEKEND_PLANNER_DB_ID", ""),
            notion_board_db_id=_get("NOTION_BOARD_DB_ID", ""),
            notebooklm_notebook_id=_get("NOTEBOOKLM_NOTEBOOK_ID", ""),
            notebooklm_notebook_name=_get("NOTEBOOKLM_NOTEBOOK_NAME", "VelaFlow"),
            litellm_proxy_url=_get("LITELLM_PROXY_URL", ""),
            litellm_proxy_token=_get("LITELLM_PROXY_TOKEN", ""),
            litellm_proxy_model=_get("LITELLM_PROXY_MODEL", "gemini/gemini-2.5-flash"),
            demo_mode=_get("DEMO_MODE", "").lower() in ("1", "true", "yes"),
            brain_read_only=_get("BRAIN_READ_ONLY", "").lower() in ("1", "true", "yes"),
            ollama_base_url=_get("OLLAMA_BASE_URL", "http://localhost:11434"),
            ollama_cpu_model=_get("OLLAMA_CPU_MODEL", "qwen2:1.5b"),
            ollama_gpu_model=_get("OLLAMA_GPU_MODEL", "qwen2:7b"),
            rag_duckdb_path=_get("RAG_DUCKDB_PATH", ""),
            rag_chunk_size=int(_get("RAG_CHUNK_SIZE", "512")),
            rag_chunk_overlap=int(_get("RAG_CHUNK_OVERLAP", "64")),
            google_oauth_client_id=_get("GOOGLE_OAUTH_CLIENT_ID", ""),
            google_oauth_client_secret=_get("GOOGLE_OAUTH_CLIENT_SECRET", ""),
            velaflow_owner_email=_get("VELAFLOW_OWNER_EMAIL", ""),
            workday_start_hour=_int("WORKDAY_START_HOUR", 9),
            workday_end_hour=_int("WORKDAY_END_HOUR", 18),
            weekend_day_start_hour=_int("WEEKEND_DAY_START_HOUR", 9),
            weekend_day_end_hour=_int("WEEKEND_DAY_END_HOUR", 18),
            default_task_duration_minutes=_int("DEFAULT_TASK_DURATION_MINUTES", 30),
            weekend_capacity_hours=_int("WEEKEND_CAPACITY_HOURS", 3),
            daily_top_task_limit=_int("DAILY_TOP_TASK_LIMIT", 5),
            overdue_section_limit=_int("OVERDUE_SECTION_LIMIT", 7),
            weekend_task_limit=_int("WEEKEND_TASK_LIMIT", 8),
            tz=_get("TZ", "Europe/Lisbon"),
            velaflow_domain=_get("VELAFLOW_DOMAIN", "localhost"),
            velaflow_api_port=_int("VELAFLOW_API_PORT", 8000),
            log_level=_get("LOG_LEVEL", "INFO"),
            log_dir=_get("LOG_DIR", "logs"),
            log_max_size_mb=_int("LOG_MAX_SIZE_MB", 50),
            log_retention_days=_int("LOG_RETENTION_DAYS", 30),
        )
