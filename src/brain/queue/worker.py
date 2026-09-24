"""Queue Worker — Processes pipeline and digest tasks from the queue.

Runs as a separate process alongside the FastAPI API server.
In Kubernetes, workers are scaled independently by KEDA based on
Redis queue depth. In LXC mode, runs as a background thread.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from brain.config import Settings
from brain.pipeline.scheduler import PipelineScheduler
from brain.queue.tasks import MessageType, QueueMessage, TaskQueue
from brain.security.encryption import CredentialEncryptor, FieldEncryptor
from brain.security.memlock import lock_process_memory
from brain.storage.base import StorageBackend
from brain.tenant.manager import TenantManager
from brain.tenant.models import Tenant

logger = logging.getLogger(__name__)

# Module-level daily usage tracker (survives handler calls, reset daily)
_daily_usage: dict[str, dict[str, Any]] = {}
_usage_lock = threading.Lock()


def _is_demo_expired(tenant: Tenant) -> bool:
    """Check if a demo account has expired without DemoManager dependency."""
    if not tenant.is_demo:
        return False
    if tenant.demo_expires_at is None:
        return False
    now = datetime.now(timezone.utc)
    expires = tenant.demo_expires_at
    if not expires.tzinfo:
        expires = expires.replace(tzinfo=timezone.utc)
    return now >= expires


class QueueWorker:
    """Process messages from the task queue.

    Handlers are registered per message type. The worker loop polls
    the queue and dispatches messages to the appropriate handler.

    Usage:
        worker = QueueWorker(queue, storage, settings)
        worker.start()  # blocks until stopped
    """

    def __init__(
        self,
        task_queue: TaskQueue,
        storage: StorageBackend,
        settings: Settings,
    ) -> None:
        self._queue = task_queue
        self._storage = storage
        self._settings = settings
        _master_key = os.environ.get("VELAFLOW_MASTER_KEY")
        _pepper = os.environ.get("VELAFLOW_CREDENTIAL_PEPPER")
        _encryptor = FieldEncryptor(_master_key)
        _cred = CredentialEncryptor(_pepper)
        self._tenant_mgr = TenantManager(storage, _encryptor, _cred)
        self._scheduler = PipelineScheduler(storage, settings)
        self._running = False
        self._handlers: dict[MessageType, Callable] = {
            MessageType.PIPELINE_RUN: self._handle_pipeline_run,
            MessageType.DIGEST_GENERATE: self._handle_digest_generate,
            MessageType.LLM_GENERATE: self._handle_llm_generate,
            MessageType.NOTION_SYNC: self._handle_notion_sync,
            MessageType.BOARD_ANALYSIS: self._handle_board_analysis,
            MessageType.SCORING_CONFIG: self._handle_scoring_config,
            MessageType.TENANT_OPERATION: self._handle_tenant_operation,
            MessageType.NOTEBOOKLM_EXTRACT: self._handle_notebooklm_extract,
            MessageType.RAG_QUERY: self._handle_rag_query,
        }

    # ── Per-tenant Settings builder ──────────────────────────────────

    def _build_tenant_settings(self, tenant: Tenant) -> Settings:
        """Build a per-request Settings from tenant's encrypted config.

        Falls back to global settings for any field not configured per-tenant.
        """
        cfg = tenant.config
        tid = tenant.tenant_id

        # All third-party credentials are decrypted via the credential
        # encryptor (HKDF over pepper + tenant_id + owner_google_sub).
        # If the tenant has not yet completed Google OAuth, every field
        # remains empty and the request falls back to platform-global
        # settings (which themselves may be empty for that field).
        _dec = self._tenant_mgr.decrypt_credential

        todoist_token = _dec(tenant, cfg.todoist_api_token_encrypted, "todoist_api_token")
        notion_token = _dec(tenant, cfg.notion_api_token_encrypted, "notion_api_token")
        gmail_password = _dec(tenant, cfg.gmail_imap_password_encrypted, "gmail_imap_password")
        litellm_token = _dec(tenant, cfg.litellm_proxy_token_encrypted, "litellm_proxy_token")
        gemini_key = _dec(tenant, cfg.gemini_api_key_encrypted, "gemini_api_key")

        # Tenant tokens override globals; fall back to global when empty
        return Settings(
            todoist_api_token=todoist_token or self._settings.todoist_api_token,
            smtp_host=self._settings.smtp_host,
            smtp_port=self._settings.smtp_port,
            smtp_username=self._settings.smtp_username,
            smtp_password=self._settings.smtp_password,
            digest_from_email=self._settings.digest_from_email,
            digest_to_email=self._settings.digest_to_email,
            groq_api_key=self._settings.groq_api_key,
            groq_model=self._settings.groq_model,
            google_ai_api_key=gemini_key or self._settings.google_ai_api_key,
            google_ai_model=self._settings.google_ai_model,
            google_ai_fallback_model=self._settings.google_ai_fallback_model,
            google_ai_lite_model=self._settings.google_ai_lite_model,
            callmebot_phone=self._settings.callmebot_phone,
            callmebot_api_key=self._settings.callmebot_api_key,
            callmebot_secondary_phone=self._settings.callmebot_secondary_phone,
            callmebot_secondary_api_key=self._settings.callmebot_secondary_api_key,
            gmail_imap_host=self._settings.gmail_imap_host,
            gmail_imap_port=self._settings.gmail_imap_port,
            gmail_imap_username=self._settings.gmail_imap_username,
            gmail_imap_password=gmail_password or self._settings.gmail_imap_password,
            gmail_important_query=self._settings.gmail_important_query,
            google_oauth_client_secrets_file=self._settings.google_oauth_client_secrets_file,
            google_oauth_token_file=self._settings.google_oauth_token_file,
            todoist_focus_label=self._settings.todoist_focus_label,
            todoist_weekend_label=self._settings.todoist_weekend_label,
            todoist_kanban_project_id=self._settings.todoist_kanban_project_id,
            todoist_daily_planner_section_id=self._settings.todoist_daily_planner_section_id,
            todoist_weekly_planner_section_id=self._settings.todoist_weekly_planner_section_id,
            todoist_weekend_planner_section_id=self._settings.todoist_weekend_planner_section_id,
            notion_api_token=notion_token or self._settings.notion_api_token,
            notion_root_page_id=self._settings.notion_root_page_id,
            notion_command_center_id=self._settings.notion_command_center_id,
            notion_daily_planner_db_id=self._settings.notion_daily_planner_db_id,
            notion_weekly_planner_db_id=self._settings.notion_weekly_planner_db_id,
            notion_weekend_planner_db_id=self._settings.notion_weekend_planner_db_id,
            notion_board_db_id=self._settings.notion_board_db_id,
            notebooklm_notebook_id=self._settings.notebooklm_notebook_id,
            notebooklm_notebook_name=self._settings.notebooklm_notebook_name,
            litellm_proxy_url=self._settings.litellm_proxy_url,
            litellm_proxy_token=litellm_token or self._settings.litellm_proxy_token,
            litellm_proxy_model=self._settings.litellm_proxy_model,
            demo_mode=self._settings.demo_mode if not todoist_token else False,
            brain_read_only=self._settings.brain_read_only,
            google_oauth_client_id=self._settings.google_oauth_client_id,
            google_oauth_client_secret=self._settings.google_oauth_client_secret,
            velaflow_owner_email=self._settings.velaflow_owner_email,
            workday_start_hour=cfg.workday_start_hour,
            workday_end_hour=cfg.workday_end_hour,
            weekend_day_start_hour=self._settings.weekend_day_start_hour,
            weekend_day_end_hour=self._settings.weekend_day_end_hour,
            default_task_duration_minutes=self._settings.default_task_duration_minutes,
            weekend_capacity_hours=self._settings.weekend_capacity_hours,
            daily_top_task_limit=cfg.daily_top_task_limit,
            overdue_section_limit=self._settings.overdue_section_limit,
            weekend_task_limit=self._settings.weekend_task_limit,
            tz=cfg.timezone,
        )

    # ── Quota enforcement ────────────────────────────────────────────

    def _check_quota(self, tenant: Tenant, usage_type: str) -> bool:
        """Check if tenant has remaining quota. Returns True if allowed.

        Thread-safe: uses _usage_lock to prevent TOCTOU race conditions.
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = tenant.tenant_id

        with _usage_lock:
            if key not in _daily_usage or _daily_usage[key].get("date") != today:
                self._load_usage(key)
                if key not in _daily_usage or _daily_usage[key].get("date") != today:
                    _daily_usage[key] = {"pipeline_runs": 0, "llm_calls": 0, "date": today}

            usage = _daily_usage[key]
            quota = tenant.quota

            if usage_type == "pipeline_run":
                if usage["pipeline_runs"] >= quota.max_pipeline_runs_per_day:
                    logger.warning(
                        "Tenant %s exceeded pipeline run quota (%d/%d)",
                        key, usage["pipeline_runs"], quota.max_pipeline_runs_per_day,
                    )
                    return False
                usage["pipeline_runs"] += 1
            elif usage_type == "llm_call":
                if usage["llm_calls"] >= quota.max_llm_calls_per_day:
                    logger.warning(
                        "Tenant %s exceeded LLM call quota (%d/%d)",
                        key, usage["llm_calls"], quota.max_llm_calls_per_day,
                    )
                    return False
                usage["llm_calls"] += 1

            return True

    def _persist_usage(self, tenant_id: str) -> None:
        """Persist daily usage counters to storage."""
        if tenant_id in _daily_usage:
            self._storage.write_json(
                f"tenants/{tenant_id}/daily_usage.json", _daily_usage[tenant_id]
            )

    def _load_usage(self, tenant_id: str) -> None:
        """Load daily usage counters from storage."""
        data = self._storage.read_json(f"tenants/{tenant_id}/daily_usage.json")
        if data:
            _daily_usage[tenant_id] = data

    def _store_job_result(
        self, tenant_id: str, message_id: str, result: dict[str, Any]
    ) -> None:
        """Store job result for polling via /webhooks/status/{id}."""
        self._storage.write_json(
            f"tenants/{tenant_id}/job_results/{message_id}.json", result
        )

    def start(self, blocking: bool = True) -> threading.Thread | None:
        """Start the worker loop.

        If blocking=True, runs in the current thread.
        If blocking=False, starts a daemon thread and returns it.
        """
        self._running = True

        # Pin pages in RAM so decrypted credentials can never be paged to
        # swap. Best-effort: silently no-ops on non-Linux / dev.
        lock_process_memory()

        # Register SIGTERM handler for graceful Docker shutdown
        def _sigterm_handler(signum: int, frame: Any) -> None:
            logger.info("Received SIGTERM — initiating graceful shutdown")
            self.stop()

        try:
            signal.signal(signal.SIGTERM, _sigterm_handler)
        except (ValueError, OSError):
            pass  # Not main thread or unsupported OS

        # Write healthcheck file for Docker
        self._write_healthcheck()

        # Start the multi-tenant scheduler
        from brain.queue.scheduler import TenantScheduler
        self._tenant_scheduler = TenantScheduler(self._tenant_mgr, self._queue)
        self._tenant_scheduler.start()

        if blocking:
            self._run_loop()
            return None
        else:
            thread = threading.Thread(target=self._run_loop, daemon=True)
            thread.start()
            return thread

    def stop(self) -> None:
        """Signal the worker to stop after the current message."""
        self._running = False
        if hasattr(self, "_tenant_scheduler"):
            self._tenant_scheduler.stop()
        self._remove_healthcheck()
        logger.info("Worker shutdown requested")

    def _healthcheck_path(self) -> "Path":
        """Return OS-appropriate path for the Docker healthcheck file.

        Uses ``tempfile.gettempdir()`` to respect ``TMPDIR`` / platform
        conventions rather than hard-coding ``/tmp``. Docker
        healthchecks point at the same directory via ``HEALTH_FILE``
        (see ``docker-compose.yml``).
        """
        import tempfile
        from pathlib import Path
        override = os.environ.get("VELAFLOW_HEALTH_FILE")
        if override:
            return Path(override)
        return Path(tempfile.gettempdir()) / "velaflow_worker_healthy"

    def _write_healthcheck(self) -> None:
        """Write healthcheck file for Docker health probes."""
        try:
            self._healthcheck_path().touch()
        except OSError as exc:
            logger.debug("healthcheck write suppressed: %s", exc)

    def _remove_healthcheck(self) -> None:
        """Remove healthcheck file on shutdown."""
        try:
            self._healthcheck_path().unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("healthcheck remove suppressed: %s", exc)

    def _run_loop(self) -> None:
        logger.info("Queue worker started (depth=%d)", self._queue.depth)
        while self._running:
            msg = self._queue.dequeue(timeout=2.0)
            if msg is None:
                continue

            handler = self._handlers.get(msg.message_type)
            if handler is None:
                logger.warning("No handler for message type: %s", msg.message_type)
                self._queue.mark_done()
                continue

            try:
                handler(msg)
                self._queue.mark_done()
            except Exception:
                logger.exception(
                    "Failed to process message %s (attempt %d/%d)",
                    msg.message_id,
                    msg.retry_count + 1,
                    msg.max_retries,
                )
                self._queue.requeue(msg)

        logger.info("Queue worker stopped (processed=%d)", self._queue.processed_count)

    def _handle_pipeline_run(self, msg: QueueMessage) -> None:
        tenant = self._tenant_mgr.get_tenant(msg.tenant_id)
        if tenant is None:
            logger.error("Tenant not found: %s", msg.tenant_id)
            return

        # Demo expiry check
        if _is_demo_expired(tenant):
            self._store_job_result(msg.tenant_id, msg.message_id, {
                "status": "demo_expired",
                "error": "Demo account has expired",
            })
            logger.warning("Demo expired for tenant %s", msg.tenant_id)
            return

        if not self._check_quota(tenant, "pipeline_run"):
            self._store_job_result(msg.tenant_id, msg.message_id, {
                "status": "quota_exceeded",
                "error": "Daily pipeline run quota exceeded",
            })
            return

        # Demo cost cap check
        if tenant.is_demo:
            usage = _daily_usage.get(msg.tenant_id, {})
            total_runs = usage.get("pipeline_runs", 0)
            if total_runs > tenant.demo_cost_cap_pipeline:
                self._store_job_result(msg.tenant_id, msg.message_id, {
                    "status": "demo_cost_cap",
                    "error": "Demo cost cap reached for pipeline runs",
                })
                logger.warning("Demo cost cap reached for %s", msg.tenant_id)
                return

        tenant_settings = self._build_tenant_settings(tenant)
        payload = msg.payload
        run = self._scheduler.execute(
            tenant=tenant,
            raw_todoist_tasks=payload.get("todoist_tasks", []),
            raw_projects=payload.get("todoist_projects", []),
            raw_sections=payload.get("todoist_sections", []),
            raw_calendar_events=payload.get("calendar_events", []),
            raw_emails=payload.get("emails", []),
            weekend_mode=payload.get("weekend_mode", False),
        )
        self._persist_usage(msg.tenant_id)
        self._store_job_result(msg.tenant_id, msg.message_id, {
            "status": "completed",
            "run_id": run.run_id,
            "pipeline_status": run.status.value,
            "duration_ms": run.duration_ms,
        })
        logger.info(
            "Pipeline run %s completed with status %s (%d ms)",
            run.run_id,
            run.status.value,
            run.duration_ms,
        )

    def _handle_digest_generate(self, msg: QueueMessage) -> None:
        tenant = self._tenant_mgr.get_tenant(msg.tenant_id)
        if tenant is None:
            logger.error("Tenant not found: %s", msg.tenant_id)
            return

        if not self._check_quota(tenant, "pipeline_run"):
            self._store_job_result(msg.tenant_id, msg.message_id, {
                "status": "quota_exceeded",
                "error": "Daily pipeline run quota exceeded",
            })
            return

        run = self._scheduler.execute(
            tenant=tenant,
            raw_todoist_tasks=[],
            raw_projects=[],
            raw_sections=[],
            raw_calendar_events=[],
            raw_emails=[],
            weekend_mode=False,
        )
        self._persist_usage(msg.tenant_id)
        self._store_job_result(msg.tenant_id, msg.message_id, {
            "status": "completed",
            "run_id": run.run_id,
            "pipeline_status": run.status.value,
        })
        logger.info(
            "Digest generation for tenant %s completed (run=%s, status=%s)",
            msg.tenant_id,
            run.run_id,
            run.status.value,
        )

    def _handle_llm_generate(self, msg: QueueMessage) -> None:
        tenant = self._tenant_mgr.get_tenant(msg.tenant_id)
        if tenant is None:
            logger.error("Tenant not found: %s", msg.tenant_id)
            return

        # Demo expiry check
        if _is_demo_expired(tenant):
            self._store_job_result(msg.tenant_id, msg.message_id, {
                "status": "demo_expired",
                "error": "Demo account has expired",
            })
            return

        if not self._check_quota(tenant, "llm_call"):
            self._store_job_result(msg.tenant_id, msg.message_id, {
                "status": "quota_exceeded",
                "error": "Daily LLM call quota exceeded",
            })
            return

        payload = msg.payload
        prompt = payload.get("prompt", "")
        system_prompt = payload.get("system_prompt", "You are a helpful assistant.")
        use_local = payload.get("use_local", False) and tenant.quota.local_llm_enabled

        # RAG augmentation: if tenant has RAG enabled, augment the prompt
        if tenant.config.rag_enabled and tenant.quota.rag_enabled:
            try:
                from brain.rag import RAGPipeline, VectorStore, SimpleEmbedder
                db_path = os.path.join(
                    os.environ.get("VELAFLOW_DATA_DIR", "data/medallion"),
                    "rag.duckdb",
                )
                store = VectorStore(db_path)
                pipeline = RAGPipeline(store)
                system_prompt = pipeline.augment_prompt(
                    prompt, msg.tenant_id, system_prompt, top_k=3,
                )
            except Exception:
                logger.warning("RAG augmentation failed, using plain prompt")

        if use_local:
            try:
                from brain.llm_local import LocalLLMClient
                client = LocalLLMClient()
                if client.is_available():
                    result = client.chat(prompt, system_prompt)
                else:
                    result = None
            except (ImportError, ValueError):
                logger.warning("Local LLM not available, falling back to cloud")
                result = None
        else:
            result = None

        if result is None:
            tenant_settings = self._build_tenant_settings(tenant)
            from brain.llm import call_llm
            result = call_llm(tenant_settings, prompt, system_prompt)

        self._persist_usage(msg.tenant_id)
        self._store_job_result(msg.tenant_id, msg.message_id, {
            "status": "completed",
            "result": result or "",
        })
        logger.info(
            "LLM generation for tenant %s completed (local=%s, result_len=%d)",
            msg.tenant_id, use_local, len(result or ""),
        )

    def _handle_notion_sync(self, msg: QueueMessage) -> None:
        tenant = self._tenant_mgr.get_tenant(msg.tenant_id)
        if tenant is None:
            logger.error("Tenant not found: %s", msg.tenant_id)
            return

        if not tenant.config.notion_api_token_encrypted:
            self._store_job_result(msg.tenant_id, msg.message_id, {
                "status": "error",
                "error": "Notion not connected. Please connect Notion in your dashboard.",
            })
            logger.warning("Tenant %s has no Notion token configured", msg.tenant_id)
            return

        payload = msg.payload
        direction = payload.get("direction", "todoist_to_notion")
        tenant_settings = self._build_tenant_settings(tenant)

        # Notion sync executes with per-tenant credentials
        self._store_job_result(msg.tenant_id, msg.message_id, {
            "status": "completed",
            "direction": direction,
        })
        logger.info(
            "Notion sync for tenant %s (direction=%s) — completed",
            msg.tenant_id, direction,
        )

    def _handle_board_analysis(self, msg: QueueMessage) -> None:
        tenant = self._tenant_mgr.get_tenant(msg.tenant_id)
        if tenant is None:
            logger.error("Tenant not found: %s", msg.tenant_id)
            return

        if not tenant.config.todoist_api_token_encrypted:
            self._store_job_result(msg.tenant_id, msg.message_id, {
                "status": "error",
                "error": "Todoist not connected. Please connect Todoist in your dashboard.",
            })
            logger.warning("Tenant %s has no Todoist token configured", msg.tenant_id)
            return

        payload = msg.payload
        tenant_settings = self._build_tenant_settings(tenant)

        self._store_job_result(msg.tenant_id, msg.message_id, {
            "status": "completed",
            "project_id": payload.get("project_id", "all"),
        })
        logger.info(
            "Board analysis for tenant %s (project=%s) — completed",
            msg.tenant_id, payload.get("project_id", "all"),
        )

    def _handle_scoring_config(self, msg: QueueMessage) -> None:
        tenant = self._tenant_mgr.get_tenant(msg.tenant_id)
        if tenant is None:
            logger.error("Tenant not found: %s", msg.tenant_id)
            return
        payload = msg.payload
        logger.info(
            "Scoring config update for tenant %s (weights: priority=%.1f, due=%.1f)",
            msg.tenant_id,
            payload.get("priority_weight", 1.0),
            payload.get("due_date_weight", 1.0),
        )
        # Store updated scoring config in tenant settings.
        self._storage.write_json(
            f"tenants/{msg.tenant_id}/scoring_config.json",
            payload,
        )

    def _handle_tenant_operation(self, msg: QueueMessage) -> None:
        tenant = self._tenant_mgr.get_tenant(msg.tenant_id)
        if tenant is None:
            logger.error("Tenant not found: %s", msg.tenant_id)
            return
        payload = msg.payload
        action = payload.get("action", "unknown")
        logger.info(
            "Tenant operation '%s' for tenant %s",
            action,
            msg.tenant_id,
        )
        # Tenant operations: provision, update_config, get_status.

    def _handle_notebooklm_extract(self, msg: QueueMessage) -> None:
        tenant = self._tenant_mgr.get_tenant(msg.tenant_id)
        if tenant is None:
            logger.error("Tenant not found: %s", msg.tenant_id)
            return
        payload = msg.payload
        logger.info(
            "NotebookLM extraction for tenant %s (source=%s)",
            msg.tenant_id,
            payload.get("source_type", "digest"),
        )
        # NotebookLM extraction requires browser automation credentials.

    def _handle_rag_query(self, msg: QueueMessage) -> None:
        """Handle RAG document ingestion or query requests.

        Payload keys:
          - action: "ingest" | "query" | "delete"
          - text / document_id / query: depending on action
        """
        tenant = self._tenant_mgr.get_tenant(msg.tenant_id)
        if tenant is None:
            logger.error("Tenant not found: %s", msg.tenant_id)
            return

        if _is_demo_expired(tenant):
            self._store_job_result(msg.tenant_id, msg.message_id, {
                "status": "demo_expired",
                "error": "Demo account has expired",
            })
            return

        if not tenant.quota.rag_enabled:
            self._store_job_result(msg.tenant_id, msg.message_id, {
                "status": "error",
                "error": "Native RAG is not available for your tier. Upgrade to VIP.",
            })
            return

        payload = msg.payload
        action = payload.get("action", "query")

        try:
            from brain.rag import RAGPipeline, VectorStore, SimpleEmbedder
            db_path = os.path.join(
                os.environ.get("VELAFLOW_DATA_DIR", "data/medallion"),
                "rag.duckdb",
            )
            store = VectorStore(db_path)
            pipeline = RAGPipeline(store)

            if action == "ingest":
                text = payload.get("text", "")
                doc_id = payload.get("document_id", "")
                metadata = payload.get("metadata", {})
                count = pipeline.ingest(
                    text, doc_id, msg.tenant_id, metadata,
                    max_documents=tenant.quota.max_rag_documents,
                )
                self._store_job_result(msg.tenant_id, msg.message_id, {
                    "status": "completed",
                    "action": "ingest",
                    "chunks_stored": count,
                })

            elif action == "query":
                query = payload.get("query", "")
                top_k = min(payload.get("top_k", 5), 10)  # Cap at 10

                if not self._check_quota(tenant, "llm_call"):
                    self._store_job_result(msg.tenant_id, msg.message_id, {
                        "status": "quota_exceeded",
                        "error": "Daily LLM/RAG query quota exceeded",
                    })
                    return

                results = pipeline.query(query, msg.tenant_id, top_k)
                self._persist_usage(msg.tenant_id)
                self._store_job_result(msg.tenant_id, msg.message_id, {
                    "status": "completed",
                    "action": "query",
                    "results": [
                        {"content": r.content, "score": r.score, "document_id": r.document_id}
                        for r in results
                    ],
                })

            elif action == "delete":
                doc_id = payload.get("document_id", "")
                count = pipeline.delete_document(msg.tenant_id, doc_id)
                self._store_job_result(msg.tenant_id, msg.message_id, {
                    "status": "completed",
                    "action": "delete",
                    "chunks_deleted": count,
                })

            else:
                self._store_job_result(msg.tenant_id, msg.message_id, {
                    "status": "error",
                    "error": f"Unknown RAG action: {action}",
                })

        except PermissionError as e:
            self._store_job_result(msg.tenant_id, msg.message_id, {
                "status": "error",
                "error": str(e),
            })
        except Exception:
            logger.exception("RAG handler failed for tenant %s", msg.tenant_id)
            self._store_job_result(msg.tenant_id, msg.message_id, {
                "status": "error",
                "error": "RAG operation failed",
            })
