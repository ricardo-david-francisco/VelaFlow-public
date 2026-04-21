# Architecture — VelaFlow

> Technical reference for the self-hosted AI productivity automation system.

---

## R17.2 Security Perimeter (zero-trust inside the LXC)

VelaFlow's threat model assumes an attacker may already hold shell access
inside the LXC. Every filesystem-touching code path is therefore layered
with a Snyk-verified zero-finding sanitization chain:

```mermaid
flowchart LR
    ENV["Untrusted input<br/>(env var / CLI arg / API body)"]
    SP["safe_path.safe_resolve<br/>(allow-list of bases)"]
    RES["Path.resolve()<br/>+ inline .relative_to(base)<br/>re-check at the sink"]
    FD{"Does an fd API exist?"}
    FDY["os.fchmod(fd) /<br/>os.open(mode=0600) /<br/>Path.open('a')"]
    STR["Guarded str-path sink<br/>(last resort)"]
    REFUSE["UnsafePathError<br/>write refused,<br/>ledger entry emitted"]
    OK["Write / chmod / read<br/>inside allow-list only"]

    ENV --> SP
    SP -->|inside base| RES
    SP -->|escape| REFUSE
    RES -->|inside base| FD
    RES -->|escape| REFUSE
    FD -->|yes| FDY --> OK
    FD -->|no| STR --> OK
```

- `src/brain/security/safe_path.py` — allow-list resolver (`safe_resolve`,
  `default_bases`, `UnsafePathError`).
- Every sink (`action_ledger._append`, `secure_logging._HMACRotatingHandler`,
  `secure_logging._derive_key`, `scripts/drive_backup.py::run_restore`,
  `scripts/chat_to_markdown.py`, `scripts/preflight.py::_check_data_dir`)
  repeats the `relative_to(base)` guard locally so Snyk sees a per-function
  sanitizer and does not depend on cross-module dataflow.
- `os.fchmod` on an already-open fd replaces `os.chmod` on a path string.
  `os.open(..., mode=0o600)` sets the HMAC-key file mode at creation rather
  than via a separate chmod. `Path.open` replaces the builtin `open()` in
  the action-ledger append sink.
- Tar restore uses `tar.extractfile()` + `shutil.copyfileobj(..., length=64*1024)`
  into a path whose `.resolve().relative_to(target)` is checked inline for
  every member. `tar.extract(path=)` is not used.

Result: `snyk code test --severity-threshold=medium` reports **0 findings**
with **0 `.snyk` ignores**. See [SECURITY-AUDIT.md § Round 17.2](SECURITY-AUDIT.md).

---

## High-Level System Architecture

```mermaid
graph TB
    subgraph Internet["External Services"]
        TODOIST["Todoist API v1<br/>(cursor-paginated)"]
        NOTION["Notion API<br/>Central Dashboard<br/>Two-way Sync"]
        GCAL["Google Calendar<br/>OAuth2 API"]
        GMAIL_IMAP["Gmail IMAP<br/>Unread alerts"]
        GEMINI_PRO["Gemini 2.5 Pro<br/>(on proxy VPS)"]        
        GEMINI_FLASH["Gemini 2.5 Flash<br/>(on proxy VPS)"]
        GEMINI_LITE["Gemini 2.5 Flash-Lite<br/>(on proxy VPS)"]
        GROQ["Groq API<br/>llama-3.3-70b (on proxy VPS)"]
        LITELLM["LiteLLM Proxy<br/>Your VPS — real keys here"]
        CALLMEBOT["CallMeBot<br/>WhatsApp API"]
        GMAIL_SMTP["Gmail SMTP<br/>App Password"]
        NLM["NotebookLM<br/>(free consumer tier)<br/>unofficial API"]
    end

    subgraph LXC["Proxmox LXC (Debian 12)"]
        subgraph SYSTEMD["systemd timers"]
            T_DAILY["brain-daily.timer<br/>Mon-Fri 07:00"]
            T_SYNC["brain-sync.timer<br/>Every 4h"]
            T_WEEKLY["brain-weekly.timer<br/>Sun 20:00"]
            T_WEEKEND["brain-weekend.timer<br/>Fri 17:00"]
            T_NLM["brain-notebooklm.timer<br/>Sun 21:00"]
        end

        subgraph PYTHON["Python 3.11+ (venv at /opt/brain)"]
            CLI["cli.py — CLI entry point"]
            CONFIG["config.py<br/>Settings from environment"]
            TODOIST_MOD["todoist.py<br/>API v1 Client (delete-protected)"]
            NOTION_MOD["notion.py<br/>Dashboard Builder + Two-way Sync"]
            ORGANIZER["organizer.py<br/>Board Intelligence — Analyse + Reorganise"]
            CAL_MOD["calendar_ctx.py<br/>Events Fetcher"]
            GMAIL_MOD["gmail_alerts.py<br/>IMAP Reader"]
            PLANNER["planner.py<br/>Scoring Engine"]
            LLM_MOD["llm.py<br/>Gemini Fallback Chain"]
            EMAIL_MOD["email_sender.py<br/>SMTP Sender"]
            WA_MOD["whatsapp.py<br/>CallMeBot Client"]
            NLM_MOD["notebooklm.py<br/>Notion \u2192 NotebookLM Sync"]
        end

        DOCKER["Docker (optional)<br/>n8n alternative scheduler"]
    end

    subgraph NOTION_PAGES["Notion Dashboard"]
        CMD_CTR["Command Center"]
        DAILY_PAGE["Daily Planner + Tasks DB"]
        WEEKLY_PAGE["Weekly Planner + Tasks DB"]
        WEEKEND_PAGE["Weekend Planner + Tasks DB"]
        BOARD_PAGE["Task Board + All Tasks DB"]
    end

    subgraph TODOIST_BOARD["Todoist Kanban Board"]
        direction TB
        S_REJECTED["Rejected"]
        S_BACKLOG["Backlog"]
        S_LOW["To Do - Low"]
        S_NORMAL["To Do - Normal"]
        S_HIGH["To Do - High"]
        S_URGENT["To Do - Urgent/Today"]
        S_WEEKEND_P["Weekend Planner (AI)"]
        S_WEEKLY_P["Weekly Planner (AI)"]
        S_DAILY_P["Daily Planner (AI)"]
        S_DOING["Doing"]
        S_RECURRING["Ongoing recurring"]
        S_BLOCKED["Blocked"]
        S_DONE["Done"]
    end

    subgraph RECIPIENTS["Notification Recipients"]
        INBOX["Email Inbox"]
        PRIMARY_WA["WhatsApp — primary"]
        SECONDARY_WA["WhatsApp — secondary recipient"]
    end

    %% Scheduling
    T_DAILY -->|"ExecStart"| CLI
    T_SYNC -->|"ExecStart"| CLI
    T_WEEKLY -->|"ExecStart"| CLI
    T_WEEKEND -->|"ExecStart"| CLI
    T_NLM -->|"ExecStart"| CLI

    %% Config
    CLI --> CONFIG

    %% CLI internal wiring
    CLI --> TODOIST_MOD
    CLI --> NOTION_MOD
    CLI --> ORGANIZER
    CLI --> CAL_MOD
    CLI --> GMAIL_MOD
    CLI --> PLANNER
    CLI --> LLM_MOD
    CLI --> EMAIL_MOD
    CLI --> WA_MOD
    CLI --> NLM_MOD

    %% External API calls
    TODOIST_MOD <-->|"HTTPS API v1"| TODOIST
    NOTION_MOD <-->|"HTTPS API"| NOTION
    CAL_MOD -->|"OAuth2"| GCAL
    GMAIL_MOD -->|"IMAP4_SSL"| GMAIL_IMAP
    LLM_MOD -->|"Primary (proxy token)"| LITELLM
    LITELLM -->|"real key, server-side"| GEMINI_PRO
    LITELLM -.->|"Fallback 1"| GEMINI_FLASH
    LITELLM -.->|"Fallback 2"| GEMINI_LITE
    LITELLM -.->|"Fallback 3"| GROQ
    LLM_MOD -.->|"Dev fallback (no proxy)"| GEMINI_FLASH
    EMAIL_MOD -->|"SMTP/TLS"| GMAIL_SMTP
    WA_MOD -->|"HTTPS GET"| CALLMEBOT

    %% Notion structure
    NOTION --- CMD_CTR
    NOTION --- DAILY_PAGE
    NOTION --- WEEKLY_PAGE
    NOTION --- WEEKEND_PAGE
    NOTION --- BOARD_PAGE

    %% Two-way sync
    NOTION_MOD <-->|"Two-way sync"| DAILY_PAGE
    NOTION_MOD <-->|"Two-way sync"| WEEKLY_PAGE
    NOTION_MOD <-->|"Two-way sync"| WEEKEND_PAGE
    TODOIST <-->|"Planner sections"| NOTION_MOD

    %% Delivery
    GMAIL_SMTP --> INBOX
    CALLMEBOT --> PRIMARY_WA
    CALLMEBOT --> SECONDARY_WA
    NLM_MOD -->|"pasted-text sources<br/>(notebooklm-py)"| NLM
    NOTION_MOD -->|"read all pages"| NLM_MOD
```

---

## Workflow Schedule and Routing

```mermaid
graph LR
    subgraph SCHEDULES["systemd Timers"]
        S1["Mon-Fri 07:00<br/>Daily Briefing"]
        S2["Every 4h 08-22<br/>Notion Sync"]
        S3["Friday 17:00<br/>Weekend Planner"]
        S4["Sunday 20:00<br/>Weekly Review"]
        S5["Sunday 21:00<br/>NotebookLM Sync"]
    end

    subgraph CLI_CMDS["brain CLI Commands"]
        C1["brain daily"]
        C2["brain notion-sync --full"]
        C3["brain weekend"]
        C4["brain weekly"]
        C5["brain notion-sync"]
        C6["brain analyze"]
        C7["brain organize"]
        C8["brain alerts"]
        C9["brain notebooklm-sync"]
    end

    subgraph DELIVERY["Delivery Channels"]
        EMAIL["Email (SMTP)"]
        WA_P["WhatsApp primary"]
        WA_S["WhatsApp secondary"]
        NOTION_OUT["Notion Planner DBs"]
        NLM_OUT["NotebookLM notebook"]
    end

    S1 --> C1
    S2 --> C2
    S3 --> C3
    S4 --> C4
    S5 --> C9

    C1 --> EMAIL
    C1 --> WA_P
    C1 --> NOTION_OUT
    C8 --> WA_P
    C3 --> EMAIL
    C3 --> WA_P
    C3 --> WA_S
    C3 --> NOTION_OUT
    C4 --> EMAIL
    C4 --> NOTION_OUT
    C5 --> NOTION_OUT    C9 --> NLM_OUT    C2 --> NOTION_OUT
```

---

## Task Scoring Algorithm

```mermaid
flowchart TD
    START["Task from Todoist"] --> OVERDUE{"Overdue?"}
    OVERDUE -->|"Yes"| OV_SCORE["+min(days,7) x 20<br/>Max +140"]
    OVERDUE -->|"No"| TODAY{"Due today?"}
    TODAY -->|"Yes"| TODAY_SCORE["+25"]
    TODAY -->|"No"| TOMORROW{"Due tomorrow?"}
    TOMORROW -->|"Yes"| TOM_SCORE["+16"]
    TOMORROW -->|"No"| SOON{"Due in 2-3 days?"}
    SOON -->|"2d"| SOON2["+8"]
    SOON -->|"3d"| SOON3["+4"]
    SOON -->|"No"| NEXT["Continue"]

    OV_SCORE --> PRIORITY
    TODAY_SCORE --> PRIORITY
    TOM_SCORE --> PRIORITY
    SOON2 --> PRIORITY
    SOON3 --> PRIORITY
    NEXT --> PRIORITY

    PRIORITY{"Priority?"}
    PRIORITY -->|"P1"| P1["+18"]
    PRIORITY -->|"P2"| P2["+10"]
    PRIORITY -->|"P3"| P3["+4"]
    PRIORITY -->|"P4"| P4["+0"]

    P1 --> LABELS
    P2 --> LABELS
    P3 --> LABELS
    P4 --> LABELS

    LABELS{"Labels?"}
    LABELS -->|"@focus"| FOCUS["+14"]
    LABELS -->|"@weekend (weekend mode)"| WEEKEND["+10"]
    LABELS -->|"None"| NOLABEL["Continue"]

    FOCUS --> DURATION
    WEEKEND --> DURATION
    NOLABEL --> DURATION

    DURATION{"Duration?"}
    DURATION -->|"30 min or less"| QUICK["+4 quick win"]
    DURATION -->|"120 min or more"| LONG["-6 complex"]
    DURATION -->|"Other"| NORMAL["+0"]

    QUICK --> NODATE
    LONG --> NODATE
    NORMAL --> NODATE

    NODATE{"No due date and no @focus?"}
    NODATE -->|"Yes"| PENALTY["-4"]
    NODATE -->|"No"| FINAL["Final Score"]

    PENALTY --> FINAL
```

---

## Data Flow — Daily Briefing

```mermaid
sequenceDiagram
    participant SD as systemd timer
    participant CLI as brain CLI
    participant P as Planner Engine
    participant L as LiteLLM Proxy (VPS)
    participant NO as Notion API
    participant G as Google Calendar
    participant I as Gmail IMAP
    participant P as Planner Engine
    participant L as LiteLLM Proxy (VPS)
    participant S as Gmail SMTP
    participant W as CallMeBot

    Note over SD: Mon-Fri 07:00
    SD->>CLI: ExecStart brain daily

    par Fetch data
        CLI->>T: GET /api/v1/tasks/filter
        T-->>CLI: Task[] (cursor-paginated)
        CLI->>G: GET /calendar/v3/events
        G-->>CLI: CalendarEvent[]
        CLI->>I: IMAP SEARCH UNSEEN
        I-->>CLI: EmailAlert[]
    end

    CLI->>P: score_tasks(tasks)
    P-->>CLI: ScoredTask[] sorted
    CLI->>P: build_daily_digest(scored, events, emails)
    P-->>CLI: DigestResult

    CLI->>L: polish_digest(text, prompt)
    Note over L: proxy token → real key on VPS → Gemini/Groq
    L-->>CLI: Polished briefing

    par Deliver
        CLI->>S: SMTP TLS digest email
        CLI->>W: HTTPS GET WhatsApp alert
    end

    CLI->>T: GET tasks in Daily Planner section
    T-->>CLI: Task[]
    CLI->>NO: POST pages (upsert Daily Planner DB)
    NO-->>CLI: Created/updated pages
```

---

## Notion Dashboard Structure

```mermaid
graph TD
    ROOT["2nd-Brain root page"]
    ROOT --> CC["Command Center<br/>Status, CLI reference, navigation"]
    ROOT --> DP["Daily Planner<br/>AI-selected tasks for today"]
    ROOT --> WP["Weekly Planner<br/>This week priority stack"]
    ROOT --> WKP["Weekend Planner<br/>Saturday and Sunday tasks"]
    ROOT --> BOARD["Task Board<br/>Full Todoist board mirror"]

    DP --> DPD["Daily Tasks DB<br/>Synced from Todoist Daily Planner section"]
    WP --> WPD["Weekly Tasks DB<br/>Synced from Todoist Weekly Planner section"]
    WKP --> WKPD["Weekend Tasks DB<br/>Synced from Todoist Weekend Planner section"]
    BOARD --> BDB["All Tasks DB<br/>Full board snapshot — stale tasks marked Done"]

    DPD <-->|"brain notion-sync"| T_DAILY["Todoist: Daily Planner section"]
    WPD <-->|"brain notion-sync"| T_WEEKLY["Todoist: Weekly Planner section"]
    WKPD <-->|"brain notion-sync"| T_WEEKEND["Todoist: Weekend Planner section"]
```

---

## Todoist Kanban Board Sections

```
Rejected              <- Discarded tasks
Backlog               <- Future tasks (no date)
To Do - Low           <- P4 tasks with dates
To Do - Normal        <- P3 tasks
To Do - High          <- P2 tasks
To Do - Urgent/Today  <- P1 + overdue + due today
─────────────── AI PLANNER SECTIONS ──────────────
Weekend Planner       <- AI-selected for weekend   (brain weekend)
Weekly Planner        <- AI-selected for this week (brain weekly)
Daily Planner         <- AI-selected for today     (brain daily)
───────────────────────────────────────────────────
Doing                 <- Currently in progress
Ongoing recurring     <- Recurring tasks
Blocked               <- Blocked (deprioritised by AI, -20 penalty)
Done                  <- Completed (marked automatically by sync)
```

---

## Security Architecture

> **Round 16 update (2026-04-30):** Per-tenant BYO Gemini API keys are now
> encrypted at rest with `FieldEncryptor` (AES-256-GCM + per-tenant PBKDF2-derived
> keys), stored in `TenantConfig.gemini_api_key_encrypted`, decrypted only
> inside a request-scoped `Settings` instance in
> `Worker._build_tenant_settings()`, and wiped by `deactivate_tenant()`. The
> platform owner cannot read a tenant's Gemini key plaintext from storage,
> logs, or memory dumps of the main process.

```mermaid
flowchart TD
    subgraph LXC["LXC Container — Zero-Trust Design"]
        SE["/etc/brain/secrets.env<br/>0600 root:root<br/>Proxy token + data tokens only"]
        SD_UNIT["systemd service unit<br/>EnvironmentFile=/etc/brain/secrets.env"]
        BRAIN_SVC["brain CLI process<br/>Reads secrets from environment"]

        SE --> SD_UNIT
        SD_UNIT --> BRAIN_SVC
    end

    subgraph VPS["Your VPS — The Fortress"]
        LITELLM_PROXY["LiteLLM Proxy<br/>Real AI keys (env vars, RAM only)<br/>Budget caps, rate limits, audit log"]
    end

    subgraph PROVIDERS["AI Providers"]
        GEMINI["Google AI Studio"]
        GROQ_P["Groq API"]
    end

    BRAIN_SVC -->|"HTTPS<br/>budget-capped proxy token"| LITELLM_PROXY
    LITELLM_PROXY -->|"HTTPS<br/>real API key (never leaves VPS)"| GEMINI
    LITELLM_PROXY -->|"HTTPS<br/>real API key"| GROQ_P

    subgraph DEMO["Demo / Shared LXC"]
        DEMO_ENV["secrets.env<br/>DEMO_MODE=true<br/>BRAIN_READ_ONLY=true"]
        DEMO_TOKEN["Proxy token<br/>$1-2 hard cap, 48h expiry"]

        DEMO_ENV --> DEMO_TOKEN
        DEMO_TOKEN -->|"HTTPS"| LITELLM_PROXY
    end

    subgraph HARDENING["systemd Hardening"]
        H1["NoNewPrivileges=true"]
        H2["PrivateTmp=true"]
        H3["ProtectSystem=strict"]
        H4["CapabilityBoundingSet= (empty)"]
        H5["Dedicated brain system user<br/>no login shell, no sudo"]
    end
```

---

## Module Dependency Graph

```mermaid
graph TD
    CLI["cli.py — entry point"]
    CONFIG["config.py — settings"]
    MODELS["models.py — dataclasses"]
    TODOIST["todoist.py — API client"]
    NOTION["notion.py — dashboard + sync"]
    ORGANIZER["organizer.py — board intelligence"]
    CALENDAR["calendar_ctx.py — events"]
    GMAIL["gmail_alerts.py — IMAP"]
    PLANNER["planner.py — scoring engine"]
    LLM["llm.py — AI chain"]
    EMAIL["email_sender.py — SMTP"]
    WA["whatsapp.py — CallMeBot"]
    NLM["notebooklm.py — NotebookLM sync"]

    CLI --> CONFIG
    CLI --> TODOIST
    CLI --> NOTION
    CLI --> ORGANIZER
    CLI --> CALENDAR
    CLI --> GMAIL
    CLI --> PLANNER
    CLI --> LLM
    CLI --> EMAIL
    CLI --> WA
    CLI --> NLM

    TODOIST --> CONFIG
    TODOIST --> MODELS
    NOTION --> CONFIG
    NOTION --> MODELS
    ORGANIZER --> CONFIG
    ORGANIZER --> MODELS
    CALENDAR --> CONFIG
    CALENDAR --> MODELS
    GMAIL --> CONFIG
    GMAIL --> MODELS
    PLANNER --> CONFIG
    PLANNER --> MODELS
    LLM --> CONFIG
    EMAIL --> CONFIG
    EMAIL --> MODELS
    WA --> CONFIG
    NLM --> CONFIG
    NLM --> TODOIST
```

---

## Failure Modes and Fallbacks

| Failure | Recovery |
|---------|---------|
| Gemini Pro rate-limited (429) | Auto-retry with Gemini Flash |
| All Gemini models fail | Try Groq llama-3.3-70b; if that fails, output raw deterministic digest |
| LiteLLM proxy unreachable | Service exits non-zero; systemd logs error; retries on next timer fire |
| Proxy token expired / budget exceeded | Revoke + reissue on VPS; update `LITELLM_PROXY_TOKEN` in secrets.env |
| Todoist API down | CLI exits non-zero; systemd logs error; retries on next timer fire |
| Gmail IMAP error | Skip email section; digest still delivers without email context |
| WhatsApp (CallMeBot) fails | Email still sends independently |
| Notion API error | Sync logged; retried on next `brain notion-sync` run |
| NotebookLM cookie expired | Sync fails; re-authenticate via VNC or push script (see deployment.md Step 6) |

---

## Resilience Patterns (v2.0)

All enterprise components use resilience patterns from `brain.security.resilience`:

```mermaid
graph LR
    subgraph Resilience["Resilience Layer"]
        CB["Circuit Breaker<br/>CLOSED→OPEN→HALF_OPEN<br/>failure_threshold=5, reset=60s"]
        RETRY["Retry with Backoff<br/>Exponential: 1s→2s→4s<br/>max_retries=3"]
        RL["Rate Limiter<br/>Sliding window<br/>20 req/min per tenant"]
        GD["Graceful Degrader<br/>Primary → Fallback<br/>Never crashes"]
    end

    API["API Request"] --> RL
    RL -->|"Under limit"| CB
    CB -->|"Circuit CLOSED"| RETRY
    RETRY -->|"Success"| RESULT["Result"]
    RETRY -->|"All retries fail"| GD
    GD -->|"Fallback result"| RESULT
    CB -->|"Circuit OPEN"| GD
```

| Pattern | Implementation | Configuration |
|---------|---------------|---------------|
| **Circuit Breaker** | `CircuitBreaker(failure_threshold=5, reset_timeout=60)` | Per-service instance, thread-safe |
| **Retry** | `@retry_with_backoff(max_retries=3, base_delay=1.0)` | Exponential backoff, configurable exception filter |
| **Rate Limiter** | `RateLimiter(max_requests=20, window_seconds=60)` | Per-tenant key, sliding window, `WEBHOOK_RATE_LIMIT` env var |
| **Graceful Degrader** | `GracefulDegrader(primary_fn, fallback_fn)` | Logs degradation, always returns a result |

### Secure Structured Logging

All application and security events are processed through `brain.security.secure_logging`:

```mermaid
graph LR
    subgraph SecureLogging["Secure Logging Pipeline"]
        APP["Application Code"] -->|"logger.info()"| REDACT["Redacting Formatter<br/>7 PII/secret patterns"]
        REDACT --> HMAC_HANDLER["HMAC Rotating Handler<br/>Chain hash per record"]
        HMAC_HANDLER --> FILE["JSON Log File<br/>0600 permissions"]
        FILE --> EXPORT["Sanitised Export<br/>(double-redaction)"]
        EXPORT --> COPILOT["Copilot / GitHub Agent<br/>Safe for debugging"]
    end
```

| Component | Implementation | Details |
|-----------|---------------|---------|
| **Redacting Formatter** | `_RedactingFormatter` | Strips API tokens, emails, phones, IPs, SSNs, credit cards, JWTs from all log output |
| **HMAC Chain** | `_HMACRotatingHandler` | Each log record includes `[chain:HASH]` — deletion or tampering breaks the chain |
| **HMAC Key** | Derived from `VELAFLOW_MASTER_KEY` or persisted random key | Not guessable from machine identity |
| **Sanitised Export** | `SecureLogger.export_sanitised()` | Double-redacts and produces Markdown safe for Copilot debugging |
| **JSON Format** | Structured output with `ts`, `level`, `logger`, `msg` fields | Machine-parseable for log aggregation |
| **Rotation** | 50 MB max file size, 10 backup files | Configurable via `LOG_MAX_SIZE_MB` |

### Interactive Installer (TUI Wizard)

`scripts/installer.py` provides an OPNsense-style terminal installer:

```mermaid
graph TD
    START["python scripts/installer.py"] --> MENU{"Main Menu"}
    MENU -->|"1"| QUICK["Quick Setup<br/>Auto-detect + defaults"]
    MENU -->|"2"| FULL["Full Setup<br/>All integrations"]
    MENU -->|"3"| RECONFIG["Reconfigure<br/>Edit existing"]
    MENU -->|"4"| HEALTH["Health Check<br/>24+ cross-platform checks"]
    MENU -->|"5"| EXPORT["Export Logs<br/>Sanitised for debugging"]

    QUICK --> PLATFORM["Platform Detection<br/>Windows/Proxmox/VMware/Oracle"]
    PLATFORM --> DOMAIN["Domain & Network"]
    DOMAIN --> KEYS["Required API Keys<br/>Secure input (getpass)"]
    KEYS --> SECRETS["Generate Secrets<br/>JWT + Master Key"]
    SECRETS --> WRITE["Write config/.env<br/>Quoted values, 0600 perms"]

    HEALTH --> PREREQ["System Prerequisites"]
    PREREQ --> PYENV["Python Environment"]
    PYENV --> SECURITY["Security Components"]
    SECURITY --> ENTERPRISE["Enterprise Components"]
    ENTERPRISE --> NETWORK["Network Connectivity"]
    NETWORK --> SUMMARY["Summary: pass/fail/warn"]
```

### Webhook Security (10 endpoints)

| Feature | Implementation |
|---------|---------------|
| **Rate Limiting** | Per-tenant sliding window (20 req/min, configurable via `WEBHOOK_RATE_LIMIT`) |
| **Signature Verification** | HMAC-SHA256 (`VELAFLOW_WEBHOOK_SECRET`), optional — enabled when secret is set |
| **Job Tracking** | `GET /webhooks/status/{id}` for async job polling (capped at 1000 entries) |
| **Catalog Singleton** | `@lru_cache` CatalogStore — no connection-per-request overhead |

---

## Multi-Tenant Platform (Round 7-9)

### Tenant Scheduler

The `TenantScheduler` runs as a background thread in the worker process, replacing per-tenant n8n
cron workflows:

- Scans all active tenants every 60 seconds
- Enqueues pipeline runs based on each tenant's `TenantConfig`:
  - **Daily digest**: fires at `daily_digest_time` on configured `daily_digest_days`
  - **Overdue alerts**: fires every `overdue_alert_interval_hours` when enabled
  - **Weekend planner**: Friday 17:00 UTC when enabled
  - **Weekly review**: Sunday 20:00 UTC when enabled
- Deduplication prevents double-enqueue within the same tick

### Billing Integration (Stripe)

- **Checkout**: `POST /billing/checkout` creates a Stripe Checkout Session with redirect URL validation
- **Webhooks**: `POST /webhooks/stripe` handles `checkout.session.completed`, `customer.subscription.deleted`, `invoice.payment_failed`
- Open redirect prevention: all redirect URLs validated against an allow-list
- Stripe SDK is lazy-imported (optional dependency: `pip install velaflow[billing]`)

### Dashboard API

- `GET /dashboard/overview` returns tenant connection status, pipeline config, and usage statistics
- Reads from in-memory `_daily_usage` (protected by `threading.Lock`)

### Per-Tenant Settings

The worker builds a per-request `Settings` object for each tenant, decrypting encrypted tokens
(todoist, notion, gmail, litellm) and falling back to global settings for unconfigured fields.
The global `Settings` object is never mutated (`frozen=True`).

## Enterprise Features (Round 10-12)

### RAG Pipeline (Retrieval-Augmented Generation)

The `brain.rag` module provides a complete RAG pipeline for **VIP** tenants only (plus `demo` and `admin` for evaluation and ops). Premium tenants keep the NotebookLM export workflow instead:

- **DocumentChunker**: Sentence-aware splitting with configurable chunk size (512 tokens) and overlap (64 tokens). Enforces 5MB document size limit.
- **SimpleEmbedder**: Offline hash-based trigram embedding (dimension=128). No API calls required — works without internet access. L2-normalized, position-weighted.
- **VectorStore**: DuckDB-backed vector storage with tenant-scoped isolation. Uses `list_cosine_similarity` for search. Each tenant's vectors are isolated by `tenant_id` column — no cross-tenant leakage.
- **RAGPipeline**: End-to-end pipeline: `ingest()` (chunk → embed → store with quota check), `query()` (embed → search), `augment_prompt()` (query → inject context into system prompt), `delete_document()`, `purge_tenant()`.

Available to **VIP** tier only (enforced via RBAC `USE_RAG` permission; `demo` and `admin` also hold it). Premium is deliberately excluded so the €18/month VIP subscription has a clear differentiator against ChatGPT Plus — see [`adr/0002-local-rag-vs-mosaic-ai.md`](adr/0002-local-rag-vs-mosaic-ai.md).

### Local LLM (Ollama Integration)

- **CPU model**: `qwen2:1.5b` — runs on any Oracle Cloud ARM instance
- **GPU model**: `qwen2:7b` — uses NVIDIA GPU when available
- `LocalLLMClient.chat()`: OpenAI-style messages format via `/api/chat`
- `LocalLLMClient.embed()`: Generates embeddings via Ollama `/api/embeddings`
- K8s KEDA auto-scales GPU pods from 0 when premium LLM requests queue up

### Demo Account System

Six user types: `admin`, `free`, `standard`, `premium`, `vip`, `demo`.

The `DemoManager` (`brain.tenant.demo_manager`) provides time-limited VIP accounts:

- **7-day TTL**: Auto-expires, no renewal without admin action
- **Cost caps**: Configurable pipeline run cap (default 50) and LLM call cap (default 100)
- **Usage analytics**: Every action logged with encrypted audit trail
- **Error forwarding**: Admin notified immediately on demo errors
- **Encrypted audit**: All demo events encrypted with per-tenant derived keys

Admin API routes at `/api/v1/admin/demos`:
- `POST /demos` — Create time-limited VIP demo
- `GET /demos` — List all demo accounts with status
- `GET /demos/{id}/analytics` — Usage analytics for a demo
- `POST /demos/{id}/check-expiry` — Auto-expire if TTL exceeded

### Encrypted Audit Logging

`EncryptedAuditLog` (`brain.security.audit_log`) provides tamper-evident audit trails:

- AES-256-GCM encrypted entries (per-tenant keys via `FieldEncryptor`)
- SHA-256 HMAC chain: each entry hashed with previous entry for tamper detection
- `verify_chain()` detects modification, deletion, or reordering of entries
- Entries rotated monthly (`tenants/{tenant_id}/audit/{YYYY-MM}.log`)
- Attacker with root filesystem access sees only encrypted blobs

### RBAC Enhancements

New permissions: `USE_RAG`, `USE_LOCAL_LLM`.

| Permission | free | standard | premium | vip | demo | admin |
|---|---|---|---|---|---|---|
| USE_RAG | | | | ✓ | ✓ | ✓ |
| USE_LOCAL_LLM | | | ✓ | ✓ | ✓ | ✓ |
| USE_PREMIUM_LLM | | | ✓ | ✓ | ✓ | ✓ |
| MANAGE_TENANT | | | | | | ✓ |

Demo tier has VIP-equivalent features without tenant/user management.

### K8s KEDA Scalers

Three auto-scalers in `deploy/kubernetes/keda-scaler.yaml`:

1. **Worker scaler**: Scales general workers 0→10 based on pipeline queue depth
2. **Premium scaler**: Scales GPU pods 0→3 for local LLM requests (GPU node affinity)
3. **RAG scaler**: Scales RAG workers 0→5 for vector search/ingest requests

---

## Deployment Architecture

### Hardened LXC Deployment Flow

```mermaid
flowchart TB
    subgraph Host["Host (Proxmox / Oracle Cloud / Ubuntu)"]
        direction TB
        UFW_H["UFW Firewall<br/>22/80/443 only"]
        F2B_H["fail2ban<br/>SSH brute-force"]
        NAT["iptables NAT<br/>host:80→LXC:80<br/>host:443→LXC:443"]
    end

    subgraph LXC["Hardened LXC Container"]
        direction TB
        AA["AppArmor: generated"]
        CAPS["Capability Drops<br/>sys_admin, sys_ptrace, etc."]
        CGROUP["cgroup2 Limits<br/>6GB RAM, 200% CPU"]

        subgraph Services["Systemd Services (sandboxed)"]
            API["velaflow-api<br/>uvicorn :8000<br/>NoNewPrivileges=true<br/>ProtectSystem=strict"]
            Worker["velaflow-worker<br/>Queue processor"]
            LogExport["velaflow-log-export<br/>Timer: every 6h"]
        end

        subgraph Security["Security Layer"]
            UFW_C["UFW: 22/80/443"]
            F2B_C["fail2ban: SSH + API"]
            Sysctl["Kernel hardening<br/>SYN flood, ptrace restrict"]
        end

        subgraph Secrets["Credential Isolation"]
            TMPFS["tmpfs /run/velaflow-secrets<br/>(RAM only, never on disk)"]
            ENV["/etc/velaflow/secrets.env<br/>0640 root:velaflow"]
        end

        Caddy["Caddy Reverse Proxy<br/>Auto-HTTPS (Let's Encrypt)<br/>Security headers<br/>/docs /redoc blocked"]
    end

    Internet["Internet"] --> UFW_H --> NAT --> Caddy
    Caddy --> API
    API --> Worker
    LogExport --> |"Sanitised logs"| CopilotLogs["/copilot/logs<br/>(local network only)"]
    ENV --> API
    ENV --> Worker
    TMPFS -.-> |"Runtime secrets"| API

    style LXC fill:#1a1a2e,stroke:#e94560,stroke-width:2px,color:#fff
    style Host fill:#0f3460,stroke:#16213e,stroke-width:2px,color:#fff
    style Services fill:#16213e,stroke:#e94560,color:#fff
    style Security fill:#16213e,stroke:#e94560,color:#fff
    style Secrets fill:#16213e,stroke:#e94560,color:#fff
```

### Oracle Cloud Always-Free Topology

```mermaid
flowchart LR
    DNS["DNS A Record"] --> OCI["Oracle Cloud<br/>ARM A1 Instance<br/>4 OCPU, 24GB RAM"]
    OCI --> LXD["LXD Bridge<br/>10.10.10.0/24"]
    LXD --> VF["VelaFlow LXC<br/>10.10.10.x<br/>4GB RAM, 2 cores"]
    VF --> DuckDB["DuckDB<br/>/opt/velaflow/data"]
    VF --> Caddy2["Caddy<br/>Auto-TLS"]

    OCI -.-> |"Security List<br/>TCP 22/80/443"| SL["OCI Security List"]
    OCI -.-> |"SSH tunnel"| Copilot["Copilot<br/>/copilot/logs"]

    style OCI fill:#f39c12,stroke:#e74c3c,stroke-width:2px,color:#000
    style VF fill:#1a1a2e,stroke:#e94560,stroke-width:2px,color:#fff
```

## Autoscaling Architecture (Round 15)

VelaFlow auto-scales from 1 idle user to 1000 concurrent users on Oracle Cloud Always-Free
hardware using Kubernetes HPA (API) and KEDA (queue-driven workers).

```mermaid
flowchart TB
    Users["Users<br/>(1 → 1000)"] --> Ingress["Ingress / Caddy"]
    Ingress --> API["velaflow-api pods<br/>HPA 1-4<br/>CPU 70% / Mem 80%"]

    API --> Queue[("Redis Queue<br/>velaflow:pipeline<br/>velaflow:premium:llm<br/>velaflow:rag")]

    Queue --> |"listLength=3"| Worker["velaflow-worker<br/>KEDA 0-10"]
    Queue --> |"listLength=1"| Premium["velaflow-premium<br/>KEDA 0-3<br/>Ollama ARM CPU"]
    Queue --> |"listLength=2"| RAG["velaflow-rag<br/>KEDA 0-5<br/>DuckDB VSS"]

    subgraph KEDA["KEDA Operator"]
        Scaler["ScaledObject<br/>pollInterval=15s<br/>cooldown=60-300s<br/>enableTLS=true (R15-M2)"]
    end

    Queue -. "metric scrape<br/>(TLS)" .-> Scaler
    Scaler -. "scale 0..N" .-> Worker
    Scaler -. "scale 0..N" .-> Premium
    Scaler -. "scale 0..N" .-> RAG

    Worker --> DuckDB["DuckDB Medallion<br/>Bronze → Silver → Gold"]
    Premium --> DuckDB
    RAG --> DuckDB

    style API fill:#16213e,stroke:#00d9ff,color:#fff
    style Queue fill:#2d3436,stroke:#fdcb6e,color:#fff
    style KEDA fill:#1a1a2e,stroke:#00b894,color:#fff
    style Worker fill:#0b3d2e,stroke:#00b894,color:#fff
    style Premium fill:#3d2e0b,stroke:#fdcb6e,color:#fff
    style RAG fill:#0b1d3d,stroke:#74b9ff,color:#fff
```

**Load profile** (validated by `tests/test_stress.py`):

| Concurrent users | Queue depth | API pods | Std workers | Premium | RAG |
|------------------|-------------|----------|-------------|---------|-----|
| 1 idle           | 0           | 1        | 0           | 0       | 0   |
| 50 active        | ~5          | 1        | 2           | 0       | 1   |
| 500 burst        | ~100        | 2        | 10 (cap)    | 1       | 3   |
| 1000 burst       | ~1000       | 4 (cap)  | 10 (cap)    | 3 (cap) | 5 (cap) |

At the 1000-user ceiling, the next step is to upgrade the Oracle shape (or add a node). All
scalers honour their `maxReplicaCount` rather than starving the API pods of CPU.

## Action Ledger — Tamper-Evident Crash & Audit Log (Round 15)

Every significant action, API call, pipeline stage, error, and unhandled exception is recorded
to an HMAC-SHA256-chained JSONL log designed for offline-verifiable post-mortem analysis.

```mermaid
flowchart LR
    subgraph App["Application"]
        API2["FastAPI routes"]
        MW["ActionLedgerMiddleware"]
        Pipe["Pipeline stages"]
        Crash["sys.excepthook<br/>(install_crash_handler)"]
    end

    API2 --> MW
    MW --> Ledger
    Pipe --> Ledger
    Crash --> Ledger

    subgraph Ledger["ActionLedger"]
        Redact["Redact 7 patterns<br/>(api_key, jwt, email,<br/>cc, ssn, hex, base64)"]
        Cap["Cap fields<br/>(tenant 256, user 256,<br/>action 512) — R15-L1"]
        Lock["threading.Lock<br/>(atomic build + append)"]
        Chain["HMAC-SHA256<br/>prev || canonical(entry)"]
        Rotate["Rotate every<br/>_max_entries — R15-M1"]
        File[("data/logs/<br/>actions-YYYY-MM-DD.jsonl")]
    end

    Redact --> Cap --> Lock --> Chain --> Rotate --> File

    File -. "verify_chain()" .-> Audit["Auditor / CI"]
    File -. "export --last 100" .-> Export["Operator<br/>debug export"]

    style Ledger fill:#1a1a2e,stroke:#e94560,color:#fff
    style Crash fill:#3d0b0b,stroke:#e94560,color:#fff
    style File fill:#0b3d2e,stroke:#00b894,color:#fff
```

**Integrity guarantee.** Each entry includes `prev = HMAC(KEY, prev_prev || canonical(prev_entry))`.
Flipping any byte in the file is detected by `verify_chain()` which returns the first broken
offset. The HMAC key is read from `VELAFLOW_LOG_HMAC_KEY`; if unset the ledger runs with a
process-local random key **and logs a CRITICAL warning** (R15-H1) — operators must provision a
persistent key for cross-restart tamper detection.

**Field caps (R15-L1).** `tenant_id` and `user_id` are truncated to 256 chars, `action` to 512
chars before serialization. This prevents adversarial log flooding from blowing out disk or
memory.

**Rotation (R15-M1).** A new genesis block is written every `_max_entries` (default 50,000)
entries, bounding any single segment's file size and keeping `verify_chain()` deterministic
per segment.

## Encrypted Off-Site Backups — Google Drive

Six backups per day (4h apart, anchored at 07:30 Europe/Lisbon) are uploaded
to a shared Google Drive folder. Backups are **client-side encrypted with
AES-256-GCM before leaving the host** using `VELAFLOW_BACKUP_KEY` — a key
deliberately kept separate from `VELAFLOW_MASTER_KEY` so that runtime
compromise does not decrypt backups, and vice versa.

```mermaid
flowchart LR
    subgraph LXC["LXC (Oracle Cloud Always-Free)"]
        DATA[("VELAFLOW_DATA_DIR<br/>medallion + tenant registry")]
        CFG[("/opt/velaflow/config")]
        LEDGER[("Action ledger<br/>HMAC-chained")]
        TIMER["systemd<br/>brain-drive-backup.timer<br/>03,07,11,15,19,23:30 Europe/Lisbon"]
        SCRIPT["scripts/drive_backup.py<br/>tar + AES-256-GCM"]
        TIMER -->|triggers| SCRIPT
        DATA --> SCRIPT
        CFG --> SCRIPT
        LEDGER --> SCRIPT
    end

    subgraph DRIVE["Google Drive (shared folder)"]
        FOLDER[("velaflow-backups/<br/>&nbsp;velaflow-backup-*.tar.gz.enc<br/>&nbsp;retention = 30 files")]
    end

    KEY["VELAFLOW_BACKUP_KEY<br/>(separate key domain<br/>from runtime master key)"]
    SA["Service Account JSON<br/>scope: drive.file<br/>shared to folder only"]

    KEY -.->|encrypt| SCRIPT
    SA  -.->|auth| SCRIPT
    SCRIPT -->|HTTPS upload<br/>AES-GCM envelope| FOLDER
    SCRIPT -->|trashes files past retention| FOLDER
```

**Envelope format** (`VFBKUP01 | nonce(12) | ciphertext + GCM tag`): magic
bytes are bound as Associated Data so any tampering — header rewrite,
bit-flip, truncation — fails authentication and aborts the restore. Each
upload records the key fingerprint (first 16 hex chars of SHA-256 of the
key) in `MANIFEST.json` so rotated keys remain identifiable.

**Restore** is `python scripts/drive_backup.py --restore <file> <target>`;
path-traversal is blocked (Python 3.12+ `filter="data"`, manual check on
older).

## Oracle Cloud Always-Free — Reference Deployment

| Resource | Always-Free allocation | VelaFlow footprint |
|----------|-----------------------|--------------------|
| Instance | VM.Standard.A1.Flex (ARM) | 1 instance |
| OCPU / RAM | 4 / 24 GB | All of it |
| Boot volume | 200 GB total (2 instances) | 50 GB for OS + VelaFlow |
| Object Storage | 20 GB | Backups |
| Outbound data | 10 TB / month | Far more than needed |
| GPU | None (free) | Ollama on ARM CPU with `qwen2:1.5b` (~5 tok/s) |

Provision: `sudo bash deploy/cloud/setup-oracle.sh --domain velaflow.example.com`.
The script installs LXD, creates the hardened LXC, configures NAT, deploys Caddy with
Let's Encrypt auto-HTTPS, and registers the systemd units.

**Reference walkthrough.**

1. Open `https://<domain>/status` in a browser — zero-dependency HTML dashboard, auto-refresh 5s.
2. Inject 5000 synthetic tasks via Todoist API or `tests/test_stress.py::test_bronze_ingest_5000_tasks`.
3. Queue depth rises; `/status` shows **SCALING UP**; workers spin 0 → 3 → 10.
4. Queue drains; `/status` shows **IDLE (scaled to 0)**.
5. Open `/metrics` to show Prometheus-compatible output for kube-prometheus scraping.
6. Export the action ledger tail: `python -m brain.security.action_ledger --export --last 100`
   — produces HMAC-chained tamper-evident records for offline post-mortem analysis.

---

## Local observability (v1.0)

The `/metrics` endpoint exposes Prometheus text format. The hardened
demo stack under `deploy/observability/` wires Prometheus + Grafana
locally with zero cost and auto-provisions an 11-panel `velaflow-main`
dashboard. Intended for local development and operator walkthroughs,
not the production data path.

```mermaid
flowchart LR
    API["FastAPI<br/>brain.api.app<br/>:8765/metrics"] -->|5s scrape| P[Prometheus<br/>:9090]
    P -->|PromQL| G[Grafana<br/>:3000]
    G -->|render| D["Dashboard<br/>velaflow-main<br/>(11 panels)"]
    D -->|screenshot| DEMO[Operator walkthrough]

    subgraph Host["Host (loopback-only binds)"]
        API
    end
    subgraph Stack["deploy/observability/<br/>(docker compose, read-only rootfs,<br/>cap_drop ALL, mem_limit 512m)"]
        P
        G
    end
```

Start it with:

```bash
uvicorn brain.api.app:app --host 127.0.0.1 --port 8765 &
docker compose -f deploy/observability/docker-compose.yml up -d
# open http://127.0.0.1:3000/d/velaflow-main (admin/admin)
```

