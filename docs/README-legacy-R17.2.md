# VelaFlow

> A multi-tenant AI productivity platform built on a **medallion architecture** (Bronze → Silver → Gold),
> integrating Todoist, Notion, Google Calendar, Gmail, and Google NotebookLM into one automated pipeline.
> Delivers daily task digests, AI-planned schedules, Kanban board intelligence, two-way data
> synchronisation, and automated knowledge-base updates — deployable as a hardened Proxmox LXC,
> Docker Compose stack, or Kubernetes cluster with KEDA auto-scaling.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-480%20passed-brightgreen.svg)](#tech-stack)
[![pip-audit](https://img.shields.io/badge/pip--audit-0%20vulnerabilities-brightgreen.svg)](#tech-stack)
[![Bandit](https://img.shields.io/badge/bandit-0%20issues-brightgreen.svg)](#tech-stack)
[![Snyk SCA](https://img.shields.io/badge/snyk%20SCA-0%20vulnerabilities-brightgreen.svg)](docs/SECURITY-AUDIT.md)
[![Snyk Code](https://img.shields.io/badge/snyk%20code-0%20findings%20(medium%2B)-brightgreen.svg)](docs/SECURITY-AUDIT.md)
[![Self-Hosted](https://img.shields.io/badge/self--hosted-Proxmox%20LXC-green.svg)](#deployment)
[![FastAPI](https://img.shields.io/badge/API-FastAPI-009688.svg)](#enterprise-platform)
[![Medallion](https://img.shields.io/badge/architecture-Medallion%20(B%E2%86%92S%E2%86%92G)-blue.svg)](#enterprise-platform)

---

## Overview

Modern knowledge workers operate across multiple disconnected tools. Tasks sit in Todoist, events
in Google Calendar, emails in Gmail, project notes in Notion, and research in Google NotebookLM.
The cost of context-switching between these systems — and the cognitive overhead of manually
deciding what to prioritise — is substantial.

VelaFlow eliminates that overhead by acting as an automated coordination layer:

1. **Reads** all active tasks (Todoist REST API v1 with cursor pagination), calendar events
   (Google Calendar OAuth2), and unread emails (Gmail IMAP).
2. **Scores** every task against a deterministic, parameter-free priority algorithm — no AI
   required for ranking.
3. **Generates** a polished daily plan via a multi-model LLM fallback chain
   (Gemini 2.5 Pro → Flash → Flash-Lite → Groq llama-3.3-70b).
4. **Delivers** the digest by email (SMTP), WhatsApp (CallMeBot), and directly into Notion.
5. **Syncs** task state bidirectionally between Todoist and Notion — edits in either tool are
   reconciled without data loss.
6. **Pushes** all knowledge (Notion pages + Todoist active tasks) into a Google NotebookLM
   notebook via Playwright-based browser automation.

The system requires no manual intervention once deployed. All AI uses free-tier endpoints
routed through a self-hosted proxy. Total additional infrastructure cost: **$0/month**.

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Zero-Trust Proxy** over secrets vault | Real AI keys never enter the LXC. A budget-capped LiteLLM proxy token is the only credential — theft costs $1-2, not a full billing account. Revoke in one curl call. See [docs/security.md](docs/security.md). |
| **Deterministic scoring** before LLM | Tasks are ranked by a parameter-free algorithm first. AI only polishes the output. If every AI provider fails, the system still delivers a usable digest. |
| **systemd over cron** | Structured logging via `journalctl`, dependency ordering, sandboxing (`NoNewPrivileges`, `ProtectSystem=strict`), automatic restart on failure. |
| **Two-way Notion sync** over one-way | Users edit tasks in either Todoist or Notion. Sync detects drift and reconciles without data loss or duplication. |
| **Multi-model fallback chain** | Gemini 2.5 Pro → Flash → Flash-Lite → Groq llama-3.3. Each step is cheaper/faster. The chain handles rate limits, outages, and quota exhaustion transparently. |
| **LXC over Docker-only** | Lighter resource footprint, native systemd, direct Proxmox integration. Docker runs inside for optional n8n. |
| **Unofficial NotebookLM API** | No official API exists. Uses `notebooklm-py` (Playwright browser automation) with cookie auth. Headless auth solved via Xvfb + x11vnc in LXC. |
| **Cursor-paginated API client** | Todoist API v1 uses cursor pagination for large task sets. The client handles pagination transparently and includes delete-protection safeguards. |
| **Medallion architecture** (Bronze→Silver→Gold) | Standard data engineering pattern. Raw data (Bronze) is immutable. Cleaning, dedup, PII masking (Silver) run separately from scoring/enrichment (Gold). Each layer is independently testable, replayable, and auditable. |
| **JWT over OAuth2 / external IdP** | Minimal dependencies. Custom HS256 implementation — no PyJWT or external auth provider required. Suitable for self-hosted single-operator deployments. |
| **KEDA over static replicas** | Workers scale to zero when idle (cost savings) and burst to 10 on queue depth. Premium pods with GPU scale 0-3 independently. Appropriate for compute-intensive LLM workloads on the premium tier. |
| **Per-tenant field encryption** | PBKDF2-derived keys per tenant, AES-256-GCM authenticated encryption. Compromise of one tenant's data does not expose others. |
| **In-process queue** (upgradeable to Redis) | No external dependency for single-node deployments. Queue interface is abstract — swap to Redis for multi-node. Dead letter support for failed messages. |
| **DuckDB analytical engine** | Self-hosted on-prem analytical engine. Same medallion architecture as managed lakehouses, SQL-native transforms, 512 MB memory budget. No cloud dependency. |
| **SQLite catalog** | On-prem data catalog. Namespace/schema/table registry, RBAC grants, data lineage tracking. WAL mode, secure_delete, parameterised queries only. |
| **YAML pipeline config** | Declarative `config/pipeline.yaml` — stages, grants, engine settings, and n8n webhook endpoints. |
| **n8n per-tenant orchestration** | Each tenant customises their pipeline schedule, data sources, and delivery channels via n8n's visual editor — no coding required. Webhook endpoints allow async triggers. |
| **Local LLM** (premium tier) | Ollama-based inference for privacy-first tenants. GPU auto-detection with CPU fallback. Default model: qwen2:1.5b (fits 4 GB). Scale up by changing one env var. |

---

## Feature Set

| Feature | Schedule | Delivery | Status |
|---------|----------|----------|--------|
| Daily Briefing | Mon-Fri 07:00 | Email + Notion | Tested |
| Daily Briefing | Mon-Fri 07:00 | WhatsApp (optional) | Requires CallMeBot setup |
| Overdue Alerts | Every 4 h (08-20) | WhatsApp (optional) | Requires CallMeBot setup |
| Weekend Planner | Friday 17:00 | Email + Notion | Implemented |
| Weekly Review | Sunday 20:00 | Email + Notion | Implemented |
| Board Analysis | On demand | Terminal | Implemented |
| Board Organise | On demand | Todoist (move + label tasks) | Implemented |
| Notion Sync | On demand / scheduled | Two-way Notion <-> Todoist | Tested |
| **NotebookLM Sync** | **Sunday 21:00** | **NotebookLM notebook** | **Tested** |

> WhatsApp delivery via CallMeBot is optional and requires a separate one-time registration
> at [callmebot.com](https://www.callmebot.com/blog/free-api-whatsapp-messages/).
> All core features (email, Notion, scheduling) work without it.

Notion is the central dashboard. Tasks can be read and edited from either Notion or Todoist and
remain in sync. All data stays on-premises. All AI uses free-tier or proxied API endpoints.

---

## R17.2 — Security-First Product Priorities

VelaFlow is deploy-ready for single-operator and small-team hosting **today**
(API, auth, observability, tier-gated GUI, preflight validator, encrypted Drive
backups, HMAC-chained action ledger). The platform is engineered around this
ordered priority list — stated here so there is no ambiguity about what ships
first and what is deferred.

| # | Priority | How it is implemented today |
|---|----------|------------------------------|
| 1 | **Security + privacy (zero-trust even against an attacker already inside the LXC)** | `src/brain/security/safe_path.py` allow-list sanitizer, inline `Path.resolve().relative_to(base)` guards at every filesystem sink, `os.fchmod(fd, …)` in place of `chmod(path, …)` to eliminate path-taint sinks, tar restore via `tar.extractfile()` + manual write (no `tar.extract(path=)`), per-tenant AES-256-GCM at rest, HMAC-chained action ledger, systemd sandbox (`NoNewPrivileges`, `ProtectSystem=strict`, `CapabilityBoundingSet`), loopback-only observability binds. **Snyk Code: 0 findings at `--severity-threshold=medium`, 0 `.snyk` ignores.** |
| 2 | **Autoscaling** | Kubernetes KEDA scales workers 0→10 on queue depth, HPA handles API CPU/memory, premium GPU pool scales 0→3 independently. systemd units restart on failure. |
| 3 | **Premium tiers — RAG + local LLM** | Per-tenant RAG (`src/brain/rag/`), Ollama local inference (`qwen2:1.5b` default, GPU auto-detect with CPU fallback). Premium/VIP only; enforced at the API layer. |
| 4 | **Telemetry an operator (or Copilot) can audit** | `src/brain/security/action_ledger.py` writes an append-only, HMAC-chained JSON-Lines record of every user + system action into a path-sanitized directory, with an encrypted exporter. Tamper-evident: breaking the hash chain is detectable offline. |
| 5 | **Redundancy + graceful degradation** | Multi-model fallback chain (Gemini 2.5 Pro → Flash → Flash-Lite → Groq llama-3.3-70b), deterministic scoring runs even when every AI is down so a digest is always produced, in-process queue with dead-letter, Redis adapter available when multi-node. |
| 6 | **Backups from day zero** | `scripts/drive_backup.py` with Google Service Account + shared folder. Envelope encryption. Restore is zero-touch and path-sanitized. |

### What is headline, what is provisional

- **Headline feature, v1.2: per-user graphical fine-tuned workflow editor.**
  A drag-and-drop, per-tenant flow builder is the primary product differentiator.
  It is not in v1.0. Everything below exists to make v1.2 safe and possible.
- **Provisional in v1.0:**
  - **Streamlit self-service GUI** — a placeholder surface so the tier-gating and
    API contract can be exercised end-to-end. It will be superseded by the v1.2
    graphical editor.
  - **n8n Community Edition** — a provisional orchestration surface for operator
    workflows (and because it is free to self-host). Not a product dependency;
    pipelines run without it.
  - **Redis queue backend** — one of several swappable queue adapters. The
    in-process queue is the default; Redis is only selected when a deployment
    exceeds one worker process.

### Zero-trust-inside-LXC summary

The threat model assumes an attacker has already obtained shell access inside
the LXC. Every filesystem-touching code path therefore: (a) routes untrusted
input through the `safe_path` allow-list, (b) re-validates the resolved path
inline immediately before the sink, and (c) prefers file-descriptor APIs
(`os.fchmod`, `Path.open`) so the sink does not take a path string at all.
Snyk Code's Python path-traversal rule confirms this: **0 findings, 0 ignores**.

---

## Enterprise Platform

VelaFlow v2.0 ships a **multi-tenant architecture** with a distributed medallion data flow,
JWT-authenticated REST API, field-level encryption, RBAC, DuckDB analytical engine,
SQLite-backed data catalog, and zero-trust inter-component security — all running on-prem
without any cloud dependency.

> **Honest status (R17.1).** The tenant registry is an **encrypted-JSON-on-disk** store
> (`tenants.json` + per-tenant key-derivation) that is safe for single-node deployments
> serving 1–1,000 tenants. PostgreSQL with connection pooling as a drop-in backend for
> multi-node writes is tracked for v1.1. In-process queue + Redis adapter both exist;
> Redis is the recommended backend when more than one worker process is deployed.
>
> **On n8n and cost.** VelaFlow self-hosts **n8n Community Edition**, which is
> Apache-2.0-style fair-code licensed and **free forever** for self-hosting with
> unlimited users and workflows. The paid *n8n Cloud* SaaS is **not** required and
> is not used. Total added infrastructure cost for the orchestration layer: **€0**.
>
> **Tenant self-service GUI.** A tier-gated Streamlit self-service GUI ships in v1.0
> (`src/brain/gui/app.py`). A full no-code, n8n-style visual workflow editor with
> drag-and-drop boxes is tracked for **v1.2**.

### Per-Tier Customization Matrix

Users personalise their VelaFlow flow within the limits of their subscription tier.
Enforcement is **API-level** (via `PATCH /api/v1/tenants/me/config` + tier-gated
quotas in `TenantQuota.for_tier`); a no-code self-service **GUI is tracked for
v1.2** (see Roadmap). Until then, tier-allowed fields are editable via authenticated
API calls or the CLI.

| Capability | Free | Standard | Premium | VIP |
|------------|:----:|:--------:|:-------:|:---:|
| Todoist + Notion sync | ✅ fixed schedule | ✅ configurable | ✅ configurable | ✅ configurable |
| Daily digest email | ✅ | ✅ | ✅ custom time | ✅ custom time |
| Pipeline runs / day | 3 | 20 | 100 | 999 |
| Max tasks tracked | 100 | 1,000 | 10,000 | 50,000 |
| LLM calls / day | 5 | 50 | 200 | 999 |
| Storage quota | 50 MB | 500 MB | 5 GB | 10 GB |
| WhatsApp + overdue alerts | ❌ | ✅ | ✅ | ✅ |
| Gmail IMAP triage | ❌ | ✅ | ✅ | ✅ |
| Google Calendar context | ❌ | ✅ | ✅ | ✅ |
| Custom digest time / timezone | ❌ | ✅ | ✅ | ✅ |
| Weekend planner / weekly review | ❌ | ✅ | ✅ | ✅ |
| **Local LLM (Ollama)** | ❌ | ❌ | ✅ `qwen2` | ✅ `qwen2` |
| **Premium LLM (Gemini Pro)** | ❌ | ❌ | ✅ | ✅ |
| **RAG — personal document search** | ❌ | ❌ | ✅ 500 docs / 50 q/day | ✅ 5,000 docs / 500 q/day |
| **Bring-your-own `gemini_api_key`** | ❌ | ❌ | ✅ | ✅ |
| NotebookLM sync | ❌ | ❌ | ✅ | ✅ |
| Nested LXC premium tier | ❌ | ❌ | ✅ | ✅ |
| Priority support | ❌ | ❌ | ❌ | ✅ |

**How a tenant customises** (today, v1.0):

- **Streamlit self-service GUI** (v1.0, shipped in this release):

  ```bash
  pip install 'velaflow[gui]'
  export VELAFLOW_API_URL=https://api.velaflow.example.com
  streamlit run src/brain/gui/app.py
  ```

  The GUI disables controls above the tenant's tier, and the API remains the
  authoritative gate: disallowed fields return 403 with an upgrade-path field.
  A full n8n-style drag-and-drop workflow editor is tracked for v1.2.

- **REST API** (always available):

  ```bash
  # Set your own Gemini key (Premium/VIP only — 403 for lower tiers):
  curl -X PATCH https://api.velaflow.example.com/api/v1/tenants/me/config \
      -H "Authorization: Bearer $JWT" \
      -H "Content-Type: application/json" \
      -d '{"gemini_api_key":"AIza...", "rag_enabled":true}'
  ```

The API returns **403 with an upgrade-path field** when a tenant tries to enable a
capability above their tier. Allowed fields are always persisted and survive
restarts (encrypted at rest via `VELAFLOW_MASTER_KEY`).

### Medallion Architecture (Bronze → Silver → Gold)

```
Raw APIs ──→ [ Bronze Layer ] ──→ [ Silver Layer ] ──→ [ Gold Layer ] ──→ API / Digest
              Land as-is          Clean, dedup,        Score, rank,
              Tenant-partitioned  validate, PII mask   AI-enrich
              JSON storage        Schema enforcement   Materialized views
```

| Layer | Responsibility | On-Prem Engine |
|-------|---------------|----------------|
| **Bronze** | Raw API ingestion (Todoist, Calendar, Gmail) into tenant-partitioned storage | DuckDB INSERT → `bronze_tasks` table |
| **Silver** | Schema validation, dedup by business key, PII masking, type normalization | DuckDB SQL transforms (ROW_NUMBER dedup, MD5 hash) → `silver_tasks` |
| **Gold** | Deterministic scoring, daily digest production, AI-ready datasets | DuckDB `gold_scored_tasks` — top-N query for API serving |

### Multi-Tenant API

FastAPI-based REST API with JWT authentication, tenant isolation, and RBAC:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check (live/ready probes) |
| `/status` | GET | Live status dashboard (HTML, auto-refresh 5s) |
| `/metrics` | GET | Prometheus-compatible metrics (text format) |
| `/api/v1/tenants` | POST | Register tenant + receive JWT token |
| `/api/v1/tenants/login` | POST | Authenticate and receive access token |
| `/api/v1/tenants/me` | GET | Current tenant profile |
| `/api/v1/pipelines/run` | POST | Trigger full Bronze→Silver→Gold pipeline (sync) |
| `/api/v1/pipelines/runs` | GET | Pipeline execution history |
| `/api/v1/tasks/scored` | GET | Retrieve scored tasks (Gold layer) |
| `/api/v1/digests/daily` | GET | Retrieve daily digest (Gold layer) |
| `/api/v1/webhooks/pipeline` | POST | Async pipeline trigger (n8n integration) |
| `/api/v1/webhooks/digest` | POST | Async digest generation (n8n integration) |
| `/api/v1/webhooks/catalog` | POST | Query catalog metadata — tables, lineage, stats (n8n) |
| `/api/v1/webhooks/llm` | POST | Trigger LLM text generation (n8n integration) |
| `/api/v1/webhooks/tenant` | POST | Tenant provisioning and config (n8n integration) |
| `/api/v1/webhooks/notion-sync` | POST | Notion ↔ Todoist bidirectional sync (n8n) |
| `/api/v1/webhooks/board-analysis` | POST | Board/section analysis trigger (n8n) |
| `/api/v1/webhooks/scoring-config` | POST | Update scoring weights (n8n) |
| `/api/v1/webhooks/status/{id}` | GET | Poll async job status (n8n) |
| `/api/v1/webhooks/notebooklm` | POST | NotebookLM extraction trigger (n8n) |
| `/api/v1/data/layers` | GET | List available medallion data layers |
| `/api/v1/data/{layer}/datasets` | GET | List datasets in a layer (tenant-scoped) |
| `/api/v1/data/{layer}/{dataset}` | GET | Read dataset records (paginated, RBAC-enforced) |
| `/api/v1/data/{layer}/{dataset}/stats` | GET | Dataset statistics |
| `/api/v1/vault/keys` | GET/POST | Encrypted API key vault (per-user) |
| `/api/v1/vault/keys/{name}` | GET/DELETE | Retrieve or delete a stored key |
| `/api/v1/auth/google` | POST | Google OAuth2 login |
| `/api/v1/auth/me` | GET | Current user profile |
| `/api/v1/auth/users` | GET | List tenant users (admin) |
| `/api/v1/auth/users/{id}/role` | PATCH | Update user role (admin) |
| `/api/v1/billing/checkout` | POST | Create Stripe checkout session |
| `/api/v1/webhooks/stripe` | POST | Handle Stripe webhook events |
| `/api/v1/dashboard/overview` | GET | Dashboard overview (connections, pipeline, usage) |

### Security Features

| Feature | Implementation |
|---------|---------------|
| **JWT Authentication** | HS256 tokens with iss/aud claims, 1-hour expiry, mandatory secret |
| **Google OAuth2** | PKCE flow via `/auth/google`, invite-only tenant onboarding |
| **Two-Layer RBAC** | Tier-based (free/standard/premium/vip/demo/admin) + User-role (owner/admin/member/viewer/demo), 18 permissions |
| **Ban Manager** | 3-stage escalation (5m → 30m → 24h), configurable permanent ban |
| **Field Encryption** | Per-tenant PBKDF2-derived keys, AES-256-GCM authenticated encryption |
| **Content Sanitization** | 5-layer defense: control chars → HTML removal → length enforcement → prompt injection detection → safety boundaries |
| **Prompt Injection Defense** | 7-pattern detection (instruction override, role hijack, system impersonation, delimiter escape, data exfiltration, code execution, encoding bypass) |
| **PII Detection** | Regex-based scanning for credit cards, emails, phones, SSN, IBAN, NIF |
| **PII Masking** | Automatic masking before Silver layer persistence and LLM calls |
| **Path Traversal Prevention** | All storage operations validate paths against directory traversal |
| **Zero-Trust Signing** | HMAC-SHA256 request signing with nonce + timestamp (5-min window) — replay prevention |
| **Webhook Rate Limiting** | Per-tenant sliding window rate limiter (20 req/min, configurable via `WEBHOOK_RATE_LIMIT`) |
| **Login Rate Limiting** | IP-based brute-force protection (10 login / 5 min, 5 registration / 5 min) |
| **Security Headers** | HSTS, CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy |
| **Webhook Signature Verification** | Optional HMAC-SHA256 request signature verification (`VELAFLOW_WEBHOOK_SECRET`) |
| **Content Moderation** | Webhook payloads screened via `check_bulk_content` before pipeline processing |
| **Audit Logging** | Structured security events: auth, permissions, data access, pipeline stages |
| **Input Sanitization** | Tenant ID, content, identifier, and label validation at all system boundaries |
| **Circuit Breakers** | Per-service circuit breakers with open/half-open/closed states and automatic recovery |
| **Resilience Patterns** | Circuit breakers, retry with exponential backoff, graceful degradation, rate limiting |
| **Data Explorer** | Authenticated read-only access to bronze/silver/gold layers with RBAC and tenant isolation |
| **Secure Structured Logging** | HMAC-chained tamper-evident logs with automatic PII/secret redaction (7 patterns), JSON structured output, log rotation, sanitised export for Copilot debugging |
| **Action Ledger** | Append-only HMAC-SHA256 chained JSONL log of every action + unhandled crash, with automatic PII redaction and field-length caps (R15 hardened) |
| **User API Key Vault** | Per-user encrypted API key storage via `/vault/keys` |
| **Per-Tenant BYO Gemini Key** | Users supply their own Gemini API key via `PATCH /tenants/me/config`; stored encrypted per-tenant (AES-256-GCM + PBKDF2). Platform owner can never read plaintext — zero-trust at rest (R16) |
| **Open-Registration Kill-Switch** | Set `VELAFLOW_DISABLE_OPEN_REGISTRATION=true` to force Google OAuth and disable `POST /api/v1/tenants` in production (R16) |

### Tenant Tiers

| Tier | Pipeline Runs/Day | Max Tasks | LLM Calls/Day | Premium LLM | RAG | Local LLM | Price |
|------|-------------------|-----------|---------------|-------------|-----|-----------|-------|
| Free | 3 | 100 | 5 | No | No | No | $0 |
| Standard | 20 | 1,000 | 50 | No | No | No | $3/mo |
| Premium | 100 | 10,000 | 200 | Yes (local Ollama) | Yes (500 docs) | Yes | $8/mo |
| VIP | 999 | 50,000 | 999 | Yes | Yes (5,000 docs) | Yes | $18/mo |
| Demo | VIP features | VIP limits | VIP limits | Yes | Yes | Yes | 7-day trial |
| Admin | Unlimited | Unlimited | Unlimited | Yes | Yes (unlimited) | Yes | Internal |

### n8n Per-Tenant Orchestration

Each tenant customises their VelaFlow experience through **n8n workflow templates** — no coding
required. n8n provides the visual orchestration layer that connects to VelaFlow's API:

```
┌─────────────────────────────────────────────────────────────────────────┐
│  n8n Workflow (per tenant)                                              │
│                                                                         │
│  Cron Trigger ─→ VelaFlow API ─→ Process Result ─→ Deliver             │
│  (schedule)      POST /api/v1/     (filter/format)   Email / WhatsApp  │
│                  webhooks/pipeline                    / Notion / Slack  │
└─────────────────────────────────────────────────────────────────────────┘
```

| Customisation | How |
|---------------|-----|
| **Schedule** | Change the Cron trigger node in n8n (e.g., 07:00 → 06:30) |
| **Data sources** | Toggle which sources are included in the pipeline payload |
| **Delivery channels** | Add/remove Email, WhatsApp, Notion, Slack output nodes |
| **Filters** | Add n8n IF/Switch nodes to route tasks by priority or label |
| **LLM polish** | Premium tenants route through local Ollama; others use cloud LLM |

Workflow templates are provided in `workflows/` — import into n8n and customise per tenant.

### Premium Tier — Local LLM

Premium tenants get a dedicated **Ollama instance** for privacy-first, API-limit-free inference:

| Feature | Detail |
|---------|--------|
| **Default model** | qwen2:1.5b (934 MB — fits 4 GB allocation) |
| **GPU detection** | Automatic via `nvidia-smi`; falls back to CPU if no GPU |
| **Scaling** | Change `PREMIUM_LLM_MODEL` env var to scale up (phi3:3.8b, llama3.2:3b, etc.) |
| **Isolation** | Runs in nested LXC or dedicated K8s pod — KEDA scales 0→3 on demand |
| **KEDA justification** | LLM inference is compute-intensive; scale-to-zero saves resources when idle |

### Deployment Options

| Target | Command | Scaling |
|--------|---------|---------|
| **Hardened LXC (Proxmox)** | `sudo bash deploy/lxc/deploy-hardened.sh --platform proxmox --id 200` | systemd services, nested LXC for KEDA/K8s |
| **Oracle Cloud (Always-Free)** | `sudo bash deploy/cloud/setup-oracle.sh --domain velaflow.example.com` | LXD container with NAT, auto-HTTPS |
| **Any LXD/Incus host** | `sudo bash deploy/lxc/deploy-hardened.sh --platform lxd` | systemd services |
| **Docker Compose** | `docker compose up -d` | Manual replica count |
| **Kubernetes** | Separate deployments, KEDA auto-scaling | Worker: 0-10 pods, Premium: 0-3 pods (GPU) |

All hardened deployments include: AppArmor, capability drops, UFW, fail2ban, systemd sandboxing,
tmpfs secrets, Caddy auto-HTTPS, and Copilot-accessible sanitised logs.
See [docs/deployment.md](docs/deployment.md) for full instructions.

### DuckDB Analytical Engine — Self-Hosted, Not Databricks

VelaFlow deliberately chose a **self-hosted medallion stack** over a managed lakehouse
(Databricks / Snowflake / BigQuery). Reasons:

1. **Data sovereignty** — tenant data never leaves your LXC/VM. No third-party data-processing agreements, no cross-border data-transfer audits.
2. **Zero fixed cost** — $0 idle cost on Oracle Cloud Always-Free (4 OCPU / 24 GB ARM). Managed lakehouses bill DBUs per second of cluster uptime.
3. **Lighter runtime** — DuckDB is a single 30 MB binary embedded in the Python process. No JVM, no driver node, no cluster spin-up latency. Cold start is ~100 ms vs. several minutes for a Spark cluster.
4. **Same mental model** — we kept the Bronze / Silver / Gold medallion pattern, a declarative pipeline config, and a central data catalog, so engineers familiar with Databricks are productive immediately.

The equivalent mapping we use internally:

| Managed lakehouse | VelaFlow self-hosted equivalent |
|---|---|
| Spark / Photon runtime | `brain.engine` — DuckDB (in-process, columnar, SQL-native, 512 MB budget) |
| Unity Catalog | `brain.catalog` — SQLite-backed catalog with RBAC grants and lineage |
| Delta Live Tables | `brain.engine.processor` — medallion stages written as SQL + Python |
| Databricks Asset Bundles | `config/pipeline.yaml` — declarative stages, grants, engine, environments |
| Databricks Workflows | n8n (visual orchestration) + systemd timers (deterministic schedules) |
| Databricks Jobs cluster | `brain.queue.worker` processes, auto-scaled by KEDA on Redis queue depth |

- **DuckDB**: Columnar analytical engine, SQL-native, 512 MB memory budget, spill-to-disk, batch inserts via `executemany()`
- **SQLite Catalog**: Namespace/schema/table registry with RBAC grants, lineage tracking, `busy_timeout=5000`
- **Declarative Config**: `config/pipeline.yaml` defines stages, grants, engine settings
- **Zero Cloud Dependency**: fully self-contained — runs in an LXC container, on Oracle Always-Free, or in any Kubernetes cluster

---

## Autoscaling: 1 → 1000 Users

VelaFlow auto-scales from a single idle user to 1000 concurrent users on commodity hardware
(N95 mini-PC, Oracle ARM A1 Flex, or any Kubernetes cluster). Three independent scalers keep
cost and latency bounded.

### Scaling Strategy

| Tier | Scaler | Min | Max | Trigger |
|------|--------|-----|-----|---------|
| **API pods** | Kubernetes HPA | 1 | 4 | CPU 70% / memory 80% |
| **Standard workers** | KEDA (Redis queue) | 0 | 10 | 3 messages per pod |
| **Premium LLM workers** | KEDA (Redis queue) | 0 | 3 | 1 message per pod (GPU) |
| **RAG workers** | KEDA (Redis queue) | 0 | 5 | 2 messages per pod |

### Load Profile

| Users | Queue depth | API pods | Std workers | Premium | RAG |
|-------|-------------|----------|-------------|---------|-----|
| 1 idle | 0 | 1 | 0 | 0 | 0 |
| 50 active | 5 | 1 | 2 | 0 | 1 |
| 500 burst | 100 | 2 | 10 | 1 | 3 |
| 1000 burst | 1000 | 4 | 10 | 3 | 5 |

The Oracle Cloud Always-Free ARM A1 Flex instance (4 OCPU / 24 GB RAM) comfortably handles
the 1000-user burst profile under nested K3s. See [deploy/kubernetes/hpa-api.yaml](deploy/kubernetes/hpa-api.yaml)
and [deploy/kubernetes/keda-scaler.yaml](deploy/kubernetes/keda-scaler.yaml) for the full config.

### Oracle Cloud Always-Free Target

| Resource | Allocation |
|----------|------------|
| Instance | VM.Standard.A1.Flex (ARM) |
| OCPU / RAM | 4 / 24 GB |
| Boot volume | 200 GB |
| Network | 10 Gbps, 10 TB/month egress |
| GPU | None (Ollama runs on ARM CPU, `qwen2:1.5b` recommended) |
| TLS | Caddy auto-HTTPS with Let's Encrypt |

Provision with `sudo bash deploy/cloud/setup-oracle.sh --domain velaflow.example.com`.

### Stress-Tested

The `tests/test_stress.py` suite (27 tests) validates the system under load:

- 5000-task bronze ingest / silver dedup / gold scoring end-to-end
- 1000 concurrent users in queue with multi-producer / multi-consumer workers
- 50 tenants running concurrent pipelines with per-tenant encryption isolation
- KEDA scaling simulation across idle / light / medium / burst load
- Circuit breaker and rate-limiter enforcement under 500+ concurrent threads
- Prompt injection detection across 5000 mixed payloads

---

## Observability and Debugging

Three independent layers provide production visibility without external dependencies.

### Live Status Dashboard

| Endpoint | Purpose |
|----------|---------|
| `/status` | Auto-refresh HTML dashboard (5s) — uptime, queue depth, workers, scaling, counters |
| `/metrics` | Prometheus-compatible text format (scrape with Grafana Agent or kube-prometheus) |
| `/health`, `/health/live`, `/health/ready` | Liveness / readiness probes for K8s |

### Action Ledger — Impenetrable Crash Logging

Every significant user action, API call, pipeline stage, error, and unhandled exception is
recorded to a **tamper-evident HMAC-SHA256 chained JSONL log** with automatic PII redaction.
Designed to be consumed directly by operators for offline incident response and forensics.

| Property | Detail |
|----------|--------|
| **Location** | `data/logs/actions-YYYY-MM-DD.jsonl` (daily rotation, 50k entries per segment) |
| **Integrity** | HMAC-SHA256 chain — `verify_chain()` detects any tampering |
| **Redaction** | 7 patterns (API keys, JWTs, emails, credit cards, SSNs, hex secrets, base64 secrets) |
| **Categories** | auth, api_request, pipeline, queue, worker, scaling, circuit, error, crash, security, tenant, data, llm, system |
| **Crash handler** | Installed on startup — every unhandled exception captured with redacted traceback |
| **Field caps** | Tenant/user IDs capped at 256 chars, action names at 512 chars — log-flood resistant |

Export for AI-assisted debugging:

```bash
python -m brain.security.action_ledger --export --last 100 > debug.jsonl
```

See [src/brain/security/action_ledger.py](src/brain/security/action_ledger.py) and
[tests/test_action_ledger.py](tests/test_action_ledger.py) for full API.

---


## Architecture

```
+------------------------------------------------------------------+
|  Proxmox LXC (Debian 12)                                        |
|                                                                  |
|  systemd timers                                                  |
|  brain-daily.timer      (Mon-Fri 07:00)                         |
|  brain-sync.timer       (every 4 h)             |               |
|  brain-weekly.timer     (Sun 20:00)             |               |
|  brain-weekend.timer    (Fri 17:00)             v               |
|                                                                  |
|  +-------------------------+   +------------------------------+  |
|  |  brain CLI              |   |  Your VPS — The Fortress     |  |
|  |  (Python 3.11 venv)     |-->|  LiteLLM Proxy               |  |
|  |                         |   |  Real AI keys live here only |  |
|  |  config.py  (settings)  |   +------------------------------+  |
|  |  todoist.py (API v1)    |<--> Todoist REST API v1             |
|  |  notion.py  (sync)      |<--> Notion API                      |
|  |  organizer.py (board)   |                                     |
|  |  planner.py (scoring)   |                                     |
|  |  llm.py     (AI chain)  |---> LiteLLM Proxy (proxy token)     |
|  |                         |     -> Gemini Pro   (on your VPS)   |
|  |                         |     -> Gemini Flash (on your VPS)   |
|  |                         |     -> Groq llama-3.3 (on your VPS)|
|  |  email_sender.py        |---> Gmail SMTP                      |
|  |  gmail_alerts.py        |<--- Gmail IMAP                      |
|  |  whatsapp.py            |---> CallMeBot API                   |
|  |  calendar_ctx.py        |<--- Google Calendar OAuth2          |
|  |  notebooklm.py (sync)   |---> NotebookLM (pasted-text sources)|
|  +-------------------------+                                     |
|                                                                  |
|  n8n (Docker, optional)                                          |
|  Alternative scheduler for environments without systemd.         |
+------------------------------------------------------------------+
```

See [docs/architecture.md](docs/architecture.md) for interactive Mermaid diagrams, or
[docs/architecture-visual.md](docs/architecture-visual.md) for a visual learning guide with
engineering-ready explanations.

---

## Tech Stack

### Core

| Technology | Role |
|-----------|------|
| **Python 3.11+** | Application language — type hints, `dataclasses`, PEP 8 style |
| **pytest** | Unit + integration + e2e tests (322 tests: scoring, pipeline, API, security, tenants, webhooks, LLM, catalog, engine, zero-trust, resilience, e2e) |
| **FastAPI** | Multi-tenant REST API with dependency injection and OpenAPI docs |
| **argparse** | Subcommand CLI (`brain daily`, `brain analyze`, `brain notion-sync`, etc.) |
| `pyproject.toml` + `pip` | PEP 621 packaging, editable installs, optional dependency groups |

### APIs and Data Pipelines

| Service | Protocol | Auth | Pipeline Role |
|---------|----------|------|---------------|
| **Todoist** | REST API v1 (cursor-paginated) | Bearer token | Task ingestion — delete-protected, full pagination |
| **Notion** | REST API v2022-06-28 | Integration token | Two-way sync with conflict resolution |
| **Google Calendar** | REST + OAuth2 (offline refresh) | `credentials.json` | Calendar context injection into digest |
| **Gmail** (read) | IMAP4 SSL | App Password | Unread email polling for alert context |
| **Gmail** (send) | SMTP + STARTTLS | App Password | HTML digest delivery with TLS enforcement |
| **WhatsApp** | CallMeBot HTTP API | API key | Optional push notification channel |
| **Google NotebookLM** | Playwright browser automation | Cookie-based auth | Automated knowledge-base sync (7 Markdown sources) |

### AI and LLM

| Component | Technology |
|-----------|-----------|
| Proxy | **LiteLLM** (self-hosted on VPS) — budget-capped, per-token audit trail, instant revocation |
| Multi-model fallback | Gemini 2.5 Pro → Flash → Flash-Lite → Groq llama-3.3-70b — each step cheaper/faster |
| Prompt engineering | Structured Markdown prompts (`prompts/`) — role-based, output schema constraints |
| Deterministic fallback | If all LLM providers fail, raw scored digest is delivered without AI polish |
| Scoring engine | Parameter-free priority algorithm — no AI required for task ranking |

### Infrastructure and Deployment

| Component | Technology |
|-----------|-----------|
| Containerization | **Proxmox LXC** (Debian 12) — unprivileged, capability-dropped, AppArmor-enforced |
| Docker | **Docker Compose** — API server, queue worker, Redis, n8n with health checks |
| Kubernetes | **K8s manifests** — API deployment, worker deployment, KEDA auto-scaling |
| Auto-scaling | **KEDA** — worker pods scale 0-10 on Redis queue depth, premium pods 0-3 with GPU |
| Scheduler | **systemd timers** — 5 units with dependency ordering and full sandboxing |
| Process isolation | `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`, `MemoryDenyWriteExecute` |
| Orchestration (alt.) | **n8n** (Docker) — visual workflow editor, 4 pre-built workflow templates |
| Analytical engine | **DuckDB** — columnar SQL, in-process, 512 MB memory limit, file-mode or in-memory |
| Data catalog | **SQLite** — namespace/schema/table registry, RBAC grants, lineage, WAL mode |
| Browser automation | **Playwright** (Chromium) — headless auth via Xvfb + x11vnc in LXC |
| Provisioning | **Bash** — `deploy-full-stack.sh` (one-click), `setup-lxc.sh` (Proxmox), `install.sh` (in-container), `health-check.sh` (validation) || Interactive installer | **Python** — `scripts/installer.py` — OPNsense-style TUI wizard with platform detection, numbered menus, secure secret input, cross-platform health check |
### Security and Quality Assurance

| Tool | Scope | Status |
|------|-------|--------|
| **Snyk** | Dependency + SAST scanning (31 deps, 54 files) | 0 vulnerabilities |
| **Bandit** | Python SAST (CWE coverage) | 0 HIGH, 0 MEDIUM |
| **pip-audit** | PyPI advisory cross-reference | 0 known vulnerabilities |
| **Security Audit Tests** | 34 automated pen tests (auth bypass, JWT tampering, injection, traversal) | All passing |
| **End-to-End API Tests** | 30-point API flow verification (registration → auth → RBAC → data → webhooks) | All passing |
| **Zero-Trust Proxy** | Real API keys never enter container — budget-capped proxy tokens only | Enforced |
| **Zero-Trust Signing** | HMAC-SHA256 inter-component request signing, nonce + timestamp replay prevention | Enforced |
| **Content Sanitization** | 5-layer defense: control chars → HTML → length → prompt injection → safety boundaries | Enforced |
| **Prompt Injection Defense** | 7-pattern detection at Todoist ingestion, webhook pipeline, and LLM entry points | Enforced |
| **Content Moderation** | `check_bulk_content` on webhook payloads before pipeline processing | Enforced |
| **Audit Logging** | Structured security events (auth, permissions, data access, pipeline stages) | Enforced |
| **Secure Logging** | HMAC-chained tamper-evident logs, 7-pattern PII/secret redaction, JSON output, sanitised export | Enforced |
| **Input Sanitization** | Boundary validation for tenant IDs, content, identifiers, labels, dangerous patterns | Enforced |
| **JWT (HS256)** | Custom token implementation — iss/aud claims, 1-hour expiry, mandatory secret | Enforced |
| **Google OAuth2** | PKCE flow, invite-only onboarding, ban manager (3-stage escalation) | Enforced |
| **Two-Layer RBAC** | Tier-based (6 tiers: free/standard/premium/vip/demo/admin) + User-role (5 roles), 18 permissions — enforced at API route level | Enforced |
| **Field encryption** | Per-tenant PBKDF2 key derivation, AES-256-GCM authenticated encryption | Enforced |
| **PII detection** | Regex scanner — credit cards, emails, phones, SSN, IBAN, NIF | Enforced |
| **Path traversal prevention** | All storage paths validated against base directory | Enforced |
| **Circuit Breakers** | Per-service breakers with open/half-open/closed states, automatic recovery | Enforced |
| **Data Explorer RBAC** | Per-layer read permissions (bronze/silver/gold), tenant-scoped dataset access | Enforced |
| **systemd hardening** | Kernel tunables, control groups, SUID/SGID, namespace restrictions | All 5 units |
| **LXC hardening** | AppArmor `generated`, 9 capabilities dropped, unprivileged container | Provisioning script |

---

## Quick Start

### Prerequisites

- Proxmox host with an available LXC slot (or Ubuntu 22.04/24.04 LTS for testing)
- Todoist API token — [todoist.com/prefs/integrations](https://todoist.com/prefs/integrations)
- Gmail App Password — [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
- Notion integration token — [notion.so/profile/integrations](https://www.notion.so/profile/integrations)
- A VPS running LiteLLM proxy with your AI API keys — see [docs/security.md](docs/security.md)
- (Optional) CallMeBot API key — [callmebot.com](https://www.callmebot.com/blog/free-api-whatsapp-messages/)

### 1. One-Click Deploy (Recommended)

```bash
# Copy and fill in the deploy config
cp config/.env.deploy.example config/.env.deploy
nano config/.env.deploy   # fill in API keys, VPS host, domain

# Run from your Proxmox host — does everything:
bash scripts/deploy-full-stack.sh --config config/.env.deploy
```

This single command creates the LXC, installs VelaFlow, deploys the LiteLLM proxy on
your VPS, generates a budget-capped token, injects all secrets, handles NotebookLM auth,
and runs a full health check.

### 1b. Interactive Installer (Cross-Platform)

```bash
# OPNsense-style TUI wizard — works on Windows, Linux, macOS
python scripts/installer.py          # Full interactive menu
python scripts/installer.py --quick  # Quick setup (auto-detect + defaults)
python scripts/installer.py --health # Health check only (24+ checks)
python scripts/installer.py --export-logs  # Export sanitised logs for debugging
```

### 1c. Manual Deploy (Step-by-Step)

```bash
# Deploy the LXC container
bash scripts/install.sh

# Developer mode (local testing with direct AI keys)
bash scripts/install.sh --dev
```

### 2. Configure

```bash
cp config/.env.example config/.env
nano config/.env   # fill in API keys
```

### 3. First-time Notion Setup

```bash
# Creates Todoist planner sections + full Notion dashboard
python -m brain notion-setup
# Copy the printed IDs into config/.env
```

### 4. Verify

```bash
# Full health check (validates all 30+ components)
bash scripts/health-check.sh

# Preview daily digest in terminal (no emails sent)
python -m brain daily --stdout --no-llm

# Sync planner tasks to Notion
python -m brain notion-sync

# Analyse the Kanban board
python -m brain analyze --stdout
```

### 5. Credential Maintenance

After deployment, credentials expire and proxy tokens need rotation. The LXC design
makes this straightforward — all secrets live in one file:

```bash
# Update any secret in-place (no container restart needed for most changes)
nano /etc/brain/secrets.env
systemctl restart brain-daily.service   # or whichever unit needs the new value
```

| Credential | Typical expiry | Renewal method |
|------------|---------------|----------------|
| LiteLLM proxy token | Monthly budget reset | Issue new token via proxy dashboard |
| Todoist API token | Does not expire | Rotate manually if compromised |
| Notion API token | Does not expire | Rotate via Notion integration settings |
| Gmail App Password | Does not expire | Revoke + regenerate at myaccount.google.com |
| Google Calendar OAuth | ~1 year (token file) | Re-run `brain daily --stdout` to re-auth |
| NotebookLM cookies | 2–4 weeks | Re-run `notebooklm-lxc-login.sh` or `notebooklm-push-auth.ps1` |

See [docs/deployment.md](docs/deployment.md) for per-step renewal commands.

### 6. Import n8n Workflows (optional)

```bash
# Start n8n (from the project root on the LXC host)
docker compose up -d

# Verify n8n is healthy
docker compose ps   # STATUS should show "healthy"
```

1. Open n8n at `http://<container-ip>:5678`
2. Import each JSON from `workflows/`:

| Workflow file | Schedule | What it does |
|---------------|----------|-------------|
| `daily-briefing.json` | Mon-Fri 07:00 | Runs `brain daily` → Groq polish → Email + WhatsApp |
| `overdue-alert.json` | Every 4 h (08-20) | Runs `brain alerts` → WhatsApp (skips if none) |
| `weekend-planner.json` | Friday 17:00 | Runs `brain weekend` → Groq polish → Email + WhatsApp (2 recipients) |
| `weekly-review.json` | Sunday 20:00 | Runs `brain weekly` → Groq polish → Email + WhatsApp |

---

### Enterprise Quick Start

#### Option A: Docker Compose (recommended for development)

```bash
# Copy enterprise config
cp config/.env.enterprise.example config/.env.enterprise

# Edit enterprise settings
nano config/.env.enterprise  # Set JWT_SECRET, ENCRYPTION_MASTER_KEY

# Start all services (API + worker + Redis + n8n)
docker compose --profile enterprise up -d

# Verify
curl http://localhost:8000/health
```

#### Option B: LXC with nested premium container

```bash
# On the Proxmox host — set up primary LXC
bash deploy/lxc/setup-enterprise.sh

# Inside the LXC — optional premium tier (nested LXC with Ollama)
bash deploy/lxc/setup-premium-nested.sh
```

#### Option C: Kubernetes with KEDA

```bash
kubectl apply -f deploy/kubernetes/namespace.yaml
kubectl apply -f deploy/kubernetes/configmap.yaml
kubectl apply -f deploy/kubernetes/deployment-api.yaml
kubectl apply -f deploy/kubernetes/deployment-worker.yaml
kubectl apply -f deploy/kubernetes/service.yaml
kubectl apply -f deploy/kubernetes/ingress.yaml
kubectl apply -f deploy/kubernetes/keda-scaler.yaml
```

#### Register a tenant and run the pipeline

```bash
# Register
curl -X POST http://localhost:8000/api/v1/tenants \
  -H "Content-Type: application/json" \
  -d '{"name": "MyOrg", "email": "user@example.com", "accept_tos": true}'
# (Response includes tenant_id, access_token, api_key — save all three)

# Login (requires tenant_id + email + api_key from registration)
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/tenants/login \
  -H "Content-Type: application/json" \
  -d '{"tenant_id": "<tenant_id>", "email": "user@example.com", "api_key": "<api_key>"}' | python -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Trigger pipeline
curl -X POST http://localhost:8000/api/v1/pipelines/run \
  -H "Authorization: Bearer $TOKEN"

# Get scored tasks
curl http://localhost:8000/api/v1/tasks/scored \
  -H "Authorization: Bearer $TOKEN"
```

3. Configure n8n credentials:
   - **Gmail**: Add OAuth2 or App Password credential, then re-link the `Send Email` node
   - **Environment variables**: Set `GROQ_API_KEY`, `DIGEST_FROM_EMAIL`, `DIGEST_TO_EMAIL`,
     `CALLMEBOT_PHONE`, `CALLMEBOT_API_KEY` in your `.env` or `docker-compose.yml`
4. Activate each workflow in the n8n UI

> All workflows include graceful fallback: if the Groq AI polish step fails, the raw
> `brain` CLI output is delivered instead.

---

## Project Structure

```
velaflow/
+-- README.md
+-- LICENSE
+-- pyproject.toml
+-- docker-compose.yml           # n8n + API + worker + Redis (enterprise)
+-- config/
|   +-- .env.example             # Single-tenant configuration template
|   +-- .env.enterprise.example  # Enterprise platform configuration
|   +-- .env.demo.example        # Zero-trust demo container template
+-- src/
|   +-- brain/
|       +-- __init__.py
|       +-- __main__.py
|       +-- cli.py               # CLI entry point
|       +-- config.py            # Settings from .env
|       +-- models.py            # Task, ScoredTask, DigestResult dataclasses
|       +-- todoist.py           # Todoist API v1 client (delete-protected)
|       +-- notion.py            # Notion API client + dashboard builder
|       +-- organizer.py         # Kanban intelligence: analyse and reorganise
|       +-- calendar_ctx.py      # Google Calendar integration
|       +-- gmail_alerts.py      # Gmail IMAP unread polling
|       +-- email_sender.py      # SMTP digest delivery
|       +-- whatsapp.py          # CallMeBot WhatsApp notifications
|       +-- llm.py               # LiteLLM proxy client + direct fallback chain
|       +-- planner.py           # Scoring engine and digest builder
|       +-- notebooklm.py        # Notion → NotebookLM automated sync
|       +-- pipeline/            # [Enterprise] Medallion architecture
|       |   +-- bronze.py        # Raw API data ingestion
|       |   +-- silver.py        # Cleaning, dedup, PII masking
|       |   +-- gold.py          # Scoring, enrichment, digest production
|       |   +-- scheduler.py     # Bronze→Silver→Gold DAG orchestration
|       +-- api/                 # [Enterprise] Multi-tenant REST API
|       |   +-- app.py           # FastAPI application factory
|       |   +-- auth.py          # JWT token create/verify
|       |   +-- middleware.py    # Tenant context injection
|       |   +-- dependencies.py  # FastAPI dependency injection
|       |   +-- routes/          # health, tenants, tasks, digests, pipelines, webhooks, demos
|       +-- catalog/             # [Enterprise] On-prem data catalog (namespace/schema/table + RBAC)
|       |   +-- models.py        # CatalogNamespace, CatalogSchema, CatalogTable, GrantLevel
|       |   +-- store.py         # SQLite-backed catalog with RBAC and lineage tracking
|       +-- engine/              # [Enterprise] Analytical engine (DuckDB, SQL-native medallion transforms)
|       |   +-- connection.py    # DuckDB connection factory with memory safety
|       |   +-- processor.py     # SQL-based medallion pipeline (Bronze/Silver/Gold)
|       +-- security/            # [Enterprise] Data protection + zero-trust
|       |   +-- pii.py           # PII detection and masking
|       |   +-- encryption.py    # Field-level encryption (per-tenant keys)
|       |   +-- rbac.py          # Role-based access control (6 tiers, 18 permissions)
|       |   +-- audit_log.py     # Encrypted tamper-evident audit logging (HMAC chain)
|       |   +-- zero_trust.py    # Request signing, audit logging, input sanitization
|       |   +-- sanitization.py  # 5-layer content sanitization + prompt injection defense
|       |   +-- circuit_breaker.py # Circuit breakers + health registry
|       |   +-- resilience.py    # Retry with backoff, rate limiter, graceful degrader
|       |   +-- ban.py           # 3-stage ban escalation manager
|       |   +-- google_auth.py   # Google OAuth2 PKCE flow
|       |   +-- moderation.py    # Content moderation (bulk screening)
|       +-- tenant/              # [Enterprise] Multi-tenancy
|       |   +-- models.py        # Tenant, TenantConfig, TenantQuota, TenantTier
|       |   +-- manager.py       # CRUD, lifecycle, token encryption
|       |   +-- user_manager.py  # User CRUD, invite-only onboarding, login tracking
|       |   +-- demo_manager.py  # Demo account lifecycle (TTL, cost caps, analytics)
|       +-- storage/             # [Enterprise] Storage abstraction
|       |   +-- base.py          # Abstract StorageBackend interface
|       |   +-- local.py         # Local filesystem implementation
|       |   +-- encrypted.py     # AES-256-GCM encrypted storage layer
|       +-- queue/               # [Enterprise] Async task processing
|       |   +-- tasks.py         # In-process queue with dead letter support
|       |   +-- worker.py        # Message handler dispatch with retry logic
|       +-- rag.py               # [Enterprise] RAG pipeline (DuckDB vector, chunker, embedder)
|       +-- llm_local.py         # [Enterprise] Local LLM client (Ollama, GPU detect)
+-- deploy/                      # [Enterprise] Deployment manifests
|   +-- docker/                  # Dockerfiles (API, worker, premium)
|   +-- kubernetes/              # K8s manifests + KEDA scalers
|   +-- lxc/                     # LXC setup scripts (primary + nested)
+-- config/
|   +-- pipeline.yaml            # Declarative pipeline config (stages, grants, engine, environments)
+-- prompts/
|   +-- daily-summary.md
|   +-- weekend-planner.md
|   +-- weekly-review.md
|   +-- task-prioritization.md
+-- workflows/
|   +-- daily-briefing.json      # n8n: Mon-Fri 07:00
|   +-- overdue-alert.json       # n8n: every 4 h
|   +-- weekend-planner.json     # n8n: Friday 17:00
|   +-- weekly-review.json       # n8n: Sunday 20:00
+-- scripts/
|   +-- deploy-full-stack.sh     # One-click: LXC + proxy + secrets + health check
|   +-- health-check.sh          # Post-install validation (30+ checks)
|   +-- install.sh               # In-container installer (Python, venv, systemd)
|   +-- setup-litellm-proxy.sh   # LiteLLM proxy on VPS (Docker + Nginx + TLS)
|   +-- setup-lxc.sh             # Proxmox LXC provisioning
|   +-- notebooklm-lxc-login.sh  # VNC-based NotebookLM Google auth
|   +-- notebooklm-push-auth.ps1 # Push cookies from Windows to LXC
|   +-- seed_demo.py             # Create demo VIP account
|   +-- brain-daily.service      # systemd unit
|   +-- brain-daily.timer
|   +-- brain-weekly.service
|   +-- brain-weekly.timer
|   +-- brain-weekend.service
|   +-- brain-weekend.timer
|   +-- brain-sync.service
|   +-- brain-sync.timer
|   +-- brain-notebooklm.service
|   +-- brain-notebooklm.timer
+-- tests/
|   +-- test_planner.py          # 13 tests: scoring engine, digest builders
|   +-- test_todoist.py          # 5 tests: task parsing, duration, dates
|   +-- test_llm.py              # 4 tests: fallback chain, proxy, raw fallback
|   +-- test_storage.py          # 11 tests: storage CRUD, path traversal
|   +-- test_security_pii.py     # 15 tests: PII detection, masking
|   +-- test_security.py         # 18 tests: encryption, RBAC
|   +-- test_pipeline_bronze.py  # 7 tests: bronze ingestion, tenant isolation
|   +-- test_pipeline_silver.py  # 11 tests: cleaning, dedup, PII masking
|   +-- test_pipeline_gold.py    # 8 tests: scoring, digest production
|   +-- test_pipeline_scheduler.py # 7 tests: full pipeline orchestration
|   +-- test_tenant.py           # 17 tests: tenant CRUD, encryption
|   +-- test_api_auth.py         # 6 tests: JWT create/verify/tamper
|   +-- test_worker.py           # 8 tests: queue, retry, dead letter
|   +-- test_webhooks.py         # 10 tests: webhook models, queue integration (10 endpoints)
|   +-- test_resilience.py       # 23 tests: circuit breaker, retry, rate limiter, degrader
|   +-- test_llm_local.py        # 13 tests: GPU detection, Ollama client
|   +-- test_catalog.py          # 30 tests: catalog models, namespaces, schemas, grants, lineage
|   +-- test_engine.py           # 30 tests: DuckDB engine, medallion processor, full pipeline
|   +-- test_zero_trust.py       # 26 tests: request signing, audit logging, input sanitization
|   +-- test_security_audit.py   # 34 tests: automated penetration tests (auth bypass, JWT, injection, traversal)
|   +-- test_e2e.py              # 6 tests: end-to-end pipeline, tenant isolation, encryption
+-- docs/
    +-- architecture.md          # Mermaid diagrams, module graph, failure modes
    +-- architecture-visual.md   # 10 visual diagrams for learning / NotebookLM
    +-- architecture-codebase.md # Code-level reference with design Q&A
    +-- deployment.md
    +-- security.md
    +-- notebooklm-setup.md
```

---

## CLI Reference

```bash
# Planning (supports --stdout for terminal preview, --no-llm to skip AI)
python -m brain daily              # Daily briefing: email + WhatsApp + Notion
python -m brain daily --stdout     # Preview in terminal
python -m brain weekend            # Weekend plan
python -m brain weekly             # Weekly review

# Alerts
python -m brain alerts             # Check Gmail + send WhatsApp alerts
python -m brain alerts --hours 6   # Check last 6 hours

# Board intelligence
python -m brain analyze            # AI board analysis (read-only)
python -m brain organize           # Dry run: show proposed changes
python -m brain organize --apply   # Apply: move tasks + auto-label

# Notion sync
python -m brain notion-setup       # One-time: create sections + dashboard
python -m brain notion-sync        # Sync planner sections <-> Notion DBs
python -m brain notion-sync --full # Also sync full board to Notion task board
python -m brain notion-rebuild     # Rebuild root page and Command Center layout

# NotebookLM sync (Notion pages + Todoist tasks → NotebookLM)
python -m brain notebooklm-sync            # Rebuild all sources (default)
python -m brain notebooklm-sync --no-rebuild  # Append without removing old sources
python -m brain notebooklm-sync --stdout   # Print summary (no UI output)
```

> **One-time setup required for NotebookLM sync:**
> See [docs/notebooklm-setup.md](docs/notebooklm-setup.md) for install and
> auth steps, and [docs/deployment.md](docs/deployment.md) Step 6 for the
> headless LXC cookie-transfer pattern.

### Enterprise API Reference

```bash
# Start the API server (development mode)
uvicorn brain.api.app:create_app --factory --reload --port 8000

# Health checks
curl http://localhost:8000/health          # Full health
curl http://localhost:8000/health/live      # Liveness probe (K8s)
curl http://localhost:8000/health/ready     # Readiness probe (K8s)

# Tenant management
curl -X POST http://localhost:8000/api/v1/tenants -d '{"name":"MyOrg","email":"...","accept_tos":true}'
curl -X POST http://localhost:8000/api/v1/tenants/login -d '{"tenant_id":"...","email":"...","api_key":"..."}'
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/tenants/me

# Pipeline execution
curl -X POST -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/pipelines/run
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/pipelines/runs

# Data access (Gold layer)
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/tasks/scored
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/digests/daily

# Webhook triggers (async, for n8n integration — 10 endpoints)
curl -X POST -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/webhooks/pipeline
curl -X POST -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/webhooks/digest
curl -X POST -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/webhooks/catalog \
  -d '{"action": "list_tables", "schema_name": "gold"}'
curl -X POST -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/webhooks/llm \
  -d '{"prompt": "Summarize my tasks"}'
curl -X POST -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/webhooks/tenant \
  -d '{"action": "provision", "config": {"tier": "standard"}}'
curl -X POST -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/webhooks/notion-sync \
  -d '{"direction": "bidirectional"}'
curl -X POST -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/webhooks/board-analysis \
  -d '{"section_name": "My Board"}'
curl -X POST -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/webhooks/scoring-config \
  -d '{"priority_weight": 1.2, "due_date_weight": 1.5}'
curl http://localhost:8000/api/v1/webhooks/status/{message_id} \
  -H "Authorization: Bearer $TOKEN"
curl -X POST -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/webhooks/notebooklm \
  -d '{"action": "full_sync"}'
```

---

## Scoring Algorithm

Tasks are ranked with a deterministic, parameter-free scoring function. No AI required.

| Factor | Points | Rationale |
|--------|--------|-----------|
| Overdue per day | +20/day (max 7 d) | Delay compounds urgency |
| Due today | +25 | Hard deadline |
| Due tomorrow | +16 | Imminent |
| Due in 2-3 days | +8 to +4 | Near-term pressure |
| Priority P1 | +18 | User-set maximum priority |
| Priority P2 | +10 | High priority |
| Priority P3 | +4 | Medium priority |
| Has `@focus` label | +14 | Strategic alignment |
| Has `@weekend` label | +10 | Weekend-eligible boost |
| Duration <= 30 min | +4 | Quick-win bonus |
| Duration >= 120 min | -6 | Requires dedicated block |
| No due date, no focus | -4 | Deprioritise undated backlog |

Tiebreaker order: due date -> priority -> alphabetical.

---

## Security Architecture

The system implements a **Zero-Trust Proxy Model** — a security architecture designed to ensure
that real API keys never enter the production container, even transiently.

### The problem with secrets vaults

If real API keys enter a container's memory (even via a vault fetch at startup), they can be
extracted via memory dump, `/proc` inspection, or network interception. Anyone with root access
to the Proxmox host running the LXC can do this. A vault only moves the problem — it does not
eliminate the attack surface.

### The proxy solution

Real API keys live exclusively on a **VPS you control**, running a self-hosted LiteLLM proxy.
The LXC container holds only a budget-capped proxy token:

- **Personal token**: monthly budget limit, high rate limit
- **Demo token**: $1-2 hard cap, 48-hour expiry, IP-locked, revocable in one API call

The proxy provides a complete audit trail: per-request logging, spend dashboards, and
per-token usage tracking.

### Defence in depth

| Layer | Implementation |
|-------|---------------|
| **Network** | All outbound HTTP enforces TLS (`verify=True`). Container communicates only with proxy URL. |
| **Process** | Dedicated `brain` system user: no login shell, no sudo, no home directory. |
| **systemd** | `NoNewPrivileges=true`, `PrivateTmp=true`, `ProtectSystem=strict` on all service units. |
| **Application** | `DEMO_MODE=true` disables direct key fallback in code, not just config. `BRAIN_READ_ONLY=true` blocks write operations. |
| **Budget** | LiteLLM hard-caps per-token spend. Theft of a demo token costs at most $1-2. |
| **Revocation** | One curl call revokes any token. No container redeployment required. |

### Threat model

| Attack | Traditional vault | Zero-Trust Proxy |
|--------|------------------|------------------|
| Memory dump of container | Vulnerable | Real key never in container |
| Proxy token stolen and reused | N/A | Spends budget cap ($1-2), then expires |
| Container stolen / snapshotted | Vulnerable | Safe |
| Instant revocation | Revoke vault access key | Revoke proxy token (one curl call) |

See [docs/security.md](docs/security.md) for the full threat model, rotation playbook, and
audit procedures.

---

## Workflow Schedules

| Workflow | Cron equivalent | What it does |
|----------|----------------|-------------|
| Daily Briefing | `0 7 * * 1-5` | Fetch tasks + calendar -> AI prioritise -> Email + WhatsApp + Notion |
| Overdue Alert | `0 8,12,16,20 * * *` | Check overdue tasks -> WhatsApp (skips if none) |
| Weekend Planner | `0 17 * * 5` | Fetch weekend tasks + events -> AI schedule -> Email + WhatsApp |
| Weekly Review | `0 20 * * 0` | Completed vs planned -> AI coaching review -> Email + Notion |
| Notion Sync | `0 6,10,14,18,22 * * *` | Two-way sync Notion <-> Todoist board |
| **NotebookLM Sync** | **`0 21 * * 0`** | **Pull all Notion pages -> push to NotebookLM notebook (weekly rebuild)** |

---

## Roadmap

> **Status key:**
> `[x]` confirmed working and end-to-end tested |
> `[-]` implemented, requires external service configured to validate |
> `[ ]` planned, not yet built

### Confirmed working (tested end-to-end)

- [x] Todoist API v1 integration — task fetch, scoring, prioritisation
- [x] Notion sync — full two-way sync, Command Center, planner databases
- [x] NotebookLM sync — Notion + Todoist → NotebookLM notebook (7 sources, weekly rebuild)
- [x] AI/LLM chain — Gemini Pro → Flash → Flash-Lite → Groq fallback (confirmed live)
- [x] Scoring algorithm — deterministic priority scoring, overdue compounding
- [x] Delete-protection safeguards — tasks are never deleted via API
- [x] Zero-Trust Proxy model — LiteLLM proxy, budget-capped tokens, `DEMO_MODE` flag
- [x] systemd hardened service units — installed and templated
- [x] Medallion pipeline — Bronze → Silver → Gold with tenant-partitioned storage
- [x] Multi-tenant API — FastAPI + JWT + RBAC (276 tests passing)
- [x] PII detection and masking — credit card, email, phone, SSN, IBAN, NIF
- [x] Field-level encryption — per-tenant PBKDF2 keys, HMAC integrity
- [x] Queue and worker system — async task processing with retry and dead letter
- [x] Docker Compose enterprise stack — API + worker + Redis + n8n
- [x] K8s/KEDA deployment manifests — auto-scaling worker and premium pods
- [x] Self-hosted medallion pipeline — `brain.engine` (DuckDB), `brain.catalog` (SQLite), `config/pipeline.yaml`. See "Self-Hosted, Not Databricks" section above for rationale.
- [x] Resilience patterns — circuit breakers, retry with backoff, graceful degradation
- [x] Webhook rate limiting — per-tenant sliding window, HMAC signature verification
- [x] DuckDB optimization — spill-to-disk, batch inserts, 512 MB memory budget
- [x] SQLite hardening — busy_timeout, WAL mode, secure_delete
- [x] Docker resource limits — memory/CPU caps on all services, json-file logging
- [x] Live E2E boot test — 12 component tests validating full system startup
- [x] One-click full stack deploy — LXC + VPS proxy + secrets + health check in one command
- [x] Post-install health check — 30+ validation checks (services, secrets, network, smoke tests)
- [x] RAG pipeline — DuckDB vector search, sentence-aware chunking, tenant-isolated
- [x] Local LLM integration — Ollama chat/embed, CPU (qwen2:1.5b) + GPU (qwen2:7b)
- [x] Demo account system — time-limited VIP, cost caps, encrypted audit, admin analytics
- [x] Encrypted audit logging — AES-256-GCM + HMAC chain, tamper-evident
- [x] 6 user types — admin, free, standard, premium, vip, demo (with RBAC enforcement)
- [x] K8s KEDA RAG scaler — auto-scaling RAG worker pods from 0 based on queue depth

### Implemented — requires external service to test end-to-end

- [-] Daily briefing email delivery (SMTP code complete; needs Gmail App Password + live run)
- [-] WhatsApp alerts via CallMeBot (code complete; needs CallMeBot registration + `CALLMEBOT_API_KEY`)
- [-] Overdue alerts via WhatsApp (same dependency as above)
- [-] Weekend planner (code complete; triggers on schedule, not manually tested)
- [-] Weekly review with coaching insights (code complete; triggers on schedule, not manually tested)
- [-] Kanban board intelligence: AI analysis and auto-reorganise (code complete; untested)
- [-] Gmail IMAP alert polling (code complete; tenant sets `gmail_imap_password` via `PATCH /tenants/me/config`; runtime exercise requires a Linux host with the systemd timer active)
- [-] Google Calendar context injection (code complete; tenant completes OAuth2 via `/api/v1/auth/google/calendar`; runtime exercise requires a Linux host)
- [-] systemd timers running in Proxmox LXC (units written + installer `scripts/install.sh`; not validated on this Windows dev machine — validation is an LXC provisioning step on the Oracle Cloud target)
- [-] Nested LXC premium tier with Ollama (scripts complete; requires Proxmox-inside-Proxmox or Proxmox-on-Linux-host; cannot be validated from a Windows laptop even with nested virt enabled because there is no Proxmox host present)

### Planned

- [ ] Monthly goal tracking and quarterly review
- [ ] Voice input via Whisper API
- [ ] Webhook trigger from Notion page edits
- [x] Rate limiting per tenant tier (sliding window, 20 req/min default, configurable)
- [x] Encrypted off-site backups to Google Drive (service-account auth, AES-256-GCM client-side encryption, 6×/day systemd timer). See [scripts/drive_backup.py](scripts/drive_backup.py).
- [ ] Redis-backed queue backend replacing the in-memory `TaskQueue` for multi-node HA. The KEDA manifests and Redis dependency are already in place; a `RedisTaskQueue` implementation that mirrors the `TaskQueue` interface via Redis `LPUSH` / `BRPOP` + dead-letter list is the remaining work. Tracked for **v1.1**.
- [ ] PostgreSQL tenant registry with connection pooling, replacing the encrypted JSON-on-disk registry. Current store is production-safe for a single LXC but does not support multi-node writes. Tracked for **v1.1**.
- [ ] Grafana + Prometheus observability stack. `/metrics` already exposes Prometheus format; a scrape config, dashboards, and alert rules are the remaining work. Tracked for **v1.1**.
- [ ] OAuth2 / OIDC integration for enterprise SSO (Azure AD / Okta)
- [ ] Tenant self-service pipeline customization UI (n8n-style boxes) for Premium/VIP tiers — current enforcement is API-level via `rag_enabled`, `use_local_llm`, and tier-gated endpoints; a no-code GUI frontend is tracked for **v1.2**.

---

## Documentation

| Document | Contents |
|----------|----------|
| [docs/architecture.md](docs/architecture.md) | Mermaid diagrams, module dependency graph, data flow, failure modes |
| [docs/architecture-visual.md](docs/architecture-visual.md) | 10 visual Mermaid diagrams — system, security, scoring, sync, scheduling, resilience |
| [docs/architecture-codebase.md](docs/architecture-codebase.md) | Code-level reference — every file, key functions, design patterns, design Q&A |
| [docs/architecture-enterprise.md](docs/architecture-enterprise.md) | Enterprise platform architecture — medallion, multi-tenant, RBAC, deployment |
| [docs/deployment.md](docs/deployment.md) | Step-by-step LXC deployment, timer configuration, troubleshooting |
| [docs/security.md](docs/security.md) | Full Zero-Trust Proxy design, threat model, rotation playbook |
| [docs/SECURITY-AUDIT.md](docs/SECURITY-AUDIT.md) | Security audit results — Bandit, pip-audit |
| [docs/notebooklm-setup.md](docs/notebooklm-setup.md) | NotebookLM auth patterns (VNC, push script, dev machine) |

---

## Contributing

Pull requests are welcome. See [docs/architecture.md](docs/architecture.md) for design decisions
and module responsibilities before making changes.

## License

MIT. See [LICENSE](LICENSE) for details.

