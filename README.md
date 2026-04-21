# VelaFlow

> An AI coordination platform that turns a user's fragmented personal tools —
> tasks, calendar, email, notes, documents — into a single, tenant-owned
> automated workflow. Built privacy-first: VelaFlow operators never see a
> tenant's third-party credentials, and an attacker with shell access inside
> the host cannot read them either.

---

## Table of Contents

1. [Vision](#1-vision)
2. [Why VelaFlow exists — problem, solution, consequence](#2-why-velaflow-exists--problem-solution-consequence)
3. [Familiar-stack translation](#3-familiar-stack-translation)
4. [Product Overview](#4-product-overview)
5. [Subscription Tiers](#5-subscription-tiers)
6. [Security and Privacy Model](#6-security-and-privacy-model)
7. [Engineering Status](#7-engineering-status)
8. [Architecture (Summary)](#8-architecture-summary)
9. [API Surface](#9-api-surface)
10. [Deployment](#10-deployment)
11. [Roadmap](#11-roadmap)
12. [Repository Layout](#12-repository-layout)
13. [Authorship and provenance](#13-authorship-and-provenance)
14. [License](#14-license)

---

## 1. Vision

Modern productivity tooling is fragmented. A single user typically operates
across a task manager, a calendar, an email account, a notes system, and a
growing list of AI assistants. The resulting context-switching tax is large,
prioritisation is lossy, and personal data flows through several third-party
vendors the user did not choose to combine.

VelaFlow is a single-tenant-per-customer AI coordination layer that sits
between those tools. A tenant connects the accounts they already use, selects
a subscription tier, and receives a personalised daily briefing, scheduled
automations, and — on paid tiers — private LLM inference and retrieval
against their own documents. No tenant credential is ever readable by the
operator, and no tenant can read another tenant's data.

The commercial proposition is straightforward:

- **Free tier** proves the product with a fixed daily briefing and a small
  task ceiling.
- **Standard, Premium, and VIP tiers** unlock customisable schedules,
  delivery channels, retrieval over personal documents, and private LLM
  inference with optional GPU.
- The platform is engineered to host a single operator, a family, or a small
  cohort of paying tenants on a commodity VPS, and to scale horizontally to
  thousands of tenants under Kubernetes with KEDA autoscaling.

VelaFlow is deploy-ready today for an operator running a handful of users.
The v1.2 milestone delivers a fully graphical per-tenant workflow editor;
until then personalisation is driven through a tier-gated self-service GUI
and a stable REST API.

---

## 2. Why VelaFlow exists — problem, solution, consequence

This section is written for readers who prefer specifications over
narrative. Numbers, not adjectives.

### 2.1 The tenant-facing problem (measurable, not rhetorical)

- A single knowledge worker operates across **6–8 disconnected SaaS
  surfaces** (task manager, calendar, email, notes, document store,
  chat, plus one or more AI assistants). Each surface is a separate
  auth domain, a separate data silo, and a separate notification
  stream.
- Context reconstruction after an interruption costs **15–25 minutes**
  of re-orientation before productive work resumes; this cost
  compounds with every surface added.
- Prioritisation is **lossy by construction**: each tool ranks its own
  items against its own signals, with no cross-surface view. Urgent
  email, stale tasks, and calendar conflicts are never ranked on the
  same axis.
- The surface count equals the **breach surface**: every vendor added
  multiplies the probability that one of them leaks credentials,
  profile data, or message content. No tenant chose that product
  combination as a deliberate security decision — it accreted.

### 2.2 The operator-facing problem (where most "AI productivity"
stacks fail)

- Most multi-tool assistants require the operator to hold the tenant's
  third-party credentials **in cleartext or under a single shared key**.
  A single root shell, a single leaked backup, or a single disgruntled
  operator retrieves every tenant's tokens at once. This is the
  LastPass-2022 and the Okta-2023 failure mode, applied to productivity
  data.
- Most "AI-first" platforms make the LLM a hard runtime dependency.
  When the upstream provider rate-limits, outages, deprecates a model,
  or changes pricing overnight, the product becomes unusable. There is
  no deterministic fallback path.
- Multi-tenant SaaS frequently relies on **row-level filters in shared
  tables** without a cryptographic boundary. A malformed query, an ORM
  bug, or a misfiled `WHERE` clause leaks cross-tenant data in a way
  no pentest catches until it is in production.

### 2.3 The solution — exactly what VelaFlow does

- **One API factory** (`brain.api.app:create_app`) serves every
  endpoint on `:8765`; every request passes `HTTPSOnlyMiddleware` as
  the outermost layer (HTTP → `308` before any auth, in every
  environment, loopback `/health` excepted for systemd probes).
- **Per-tenant credential encryption** with a split-knowledge
  requirement:

  ```
  KEK = HKDF-SHA256(
          ikm   = VELAFLOW_CREDENTIAL_PEPPER,
          salt  = SHA256(tenant_id || 0x1F || owner_google_sub),
          info  = "velaflow-credential-v2",
          length = 32
        )
  ciphertext = AES-256-GCM(KEK, plaintext, aad = field_name)
  ```

  Decrypting a tenant's Todoist / Notion / Gmail / Gemini token requires
  **both** the operator pepper **and** the tenant's Google OAuth
  `sub`. A root shell inside the host with only one of the two
  retrieves nothing. Ciphertext cannot be relocated between tenants
  or between fields — the AAD and the per-tenant salt both reject it.
- **Deterministic digest, AI-optional.** A parameter-free scoring
  algorithm (Bronze → Silver → Gold) ranks tasks before any LLM runs.
  The platform produces a usable daily digest even when every upstream
  LLM is unavailable. The LLM chain (Gemini 2.5 Pro → Flash →
  Flash-Lite → Groq llama-3.3-70b → optional local Ollama) is a
  transparent enrichment layer, not a hard dependency.
- **Per-tenant scheduler** (`brain.queue.scheduler`) queues each
  tenant's jobs at their configured time and timezone; **per-tenant
  quotas** are enforced at the API layer (not the GUI) and survive
  worker restarts.
- **KEDA 0→10 workers** on queue depth, HPA 1→4 API pods, independent
  0→3 premium GPU pool. LXC deployment simply pins all three to 1.
- **HMAC-SHA256-chained action ledger**: every ingest, LLM call,
  delivery, and configuration change is appended to a tamper-evident
  JSON-Lines record. `verify_chain()` returns the first broken offset
  offline.
- **Preflight refuses to boot a bad environment.** `scripts/preflight.py`
  is an `ExecStartPre=` for every systemd unit; a missing master key,
  a missing credential pepper, a reused JWT secret, or a missing TLS
  pair blocks startup before a single request is served.

### 2.4 What happens to operators who do not adopt a boundary like
this one

These failure modes are not hypothetical — the public post-mortems
are named inline.

- **Root-shell-decrypts-everything.** Any multi-tenant productivity
  stack whose decryption key lives entirely in the operator
  environment is one compromised backup, one leaked `.env`, or one
  insider away from a mass credential dump. **LastPass, 2022**: the
  attacker lifted a backup plus the single KMS key and decrypted the
  vaults offline. Replicate that pattern at VelaFlow scale and every
  tenant's Todoist, Notion, Gmail, and Gemini tokens leave with the
  backup.
- **Plaintext-by-one-misconfig.** A single missing HSTS header or a
  middleware registered in the wrong order demotes the whole fleet
  to cleartext during a window nobody notices. **Okta support case
  upload, 2023**: one misused file, one misconfigured bucket, weeks of
  exposure. VelaFlow's `HTTPSOnlyMiddleware` is the outermost ASGI
  wrapper in every environment, including local dev, precisely to
  make that misconfiguration impossible.
- **Cross-tenant-ORM-leak.** Row-level filters without a cryptographic
  or schema-level boundary leak on the first malformed query, the
  first missing `WHERE`, the first middleware bypass. **Multiple
  published SaaS post-mortems, 2022–2025.** VelaFlow binds tenant
  identity into the credential KDF salt and into GCM AAD — an
  off-tenant query does not just return filtered-zero rows, it fails
  authentication.
- **Vendor-outage-equals-platform-outage.** Hard dependency on a single
  LLM provider means every model deprecation, regional outage, or
  price change is a tenant-visible incident. VelaFlow's fallback chain
  plus the deterministic Gold layer guarantee a digest is produced
  even under full LLM provider blackout.

### 2.5 The secret formula (specs, not slogans)

| Control | Specification |
|---|---|
| Credential KEK | `HKDF-SHA256(ikm=pepper, salt=SHA256(tenant_id \|\| 0x1F \|\| owner_google_sub), info="velaflow-credential-v2", length=32)` |
| Credential cipher | AES-256-GCM, 96-bit nonce, 128-bit tag, `aad = field_name` |
| Memory pinning | `mlockall(MCL_CURRENT\|MCL_FUTURE)` at process start on API + worker; `LimitCORE=0`, `LimitMEMLOCK=infinity` on all six `brain-*.service` units |
| HTTPS enforcement | `HTTPSOnlyMiddleware` outermost in every environment; HTTP → `308`; loopback `/health` exempted for systemd probes |
| Static-analysis floor | Snyk Code (SAST) 0 findings across HIGH/MEDIUM/LOW with **0 `.snyk` ignores**; `pip-audit` (PyPA-authoritative SCA) 0 advisories; Bandit 0 medium/high |
| Regression floor | `pytest tests/ --ignore=tests/test_stress.py` → **528 passed**; any regression fails the build |
| Pentest floor | 58 adversarial tests across 19 audit rounds; every finding remediated or formally mitigated with documented evidence |
| Boot-time gate | `scripts/preflight.py` as `ExecStartPre=`; a bad environment blocks startup before any request is served |
| Tamper evidence | HMAC-SHA256-chained JSONL action ledger; `verify_chain()` returns the first broken offset offline |

Each row above is backed by a test that fails if the control regresses.
Nothing in this section is aspirational.

---

## 3. Familiar-stack translation

Readers arriving from a large cloud data-and-AI stack will recognise
most of the patterns below. VelaFlow deliberately substitutes self-hosted,
commodity-friendly equivalents — the goal is a single VPS or a small LXC
on a homelab, not a €40k/month managed bill. The intent of each choice is
the same as the cloud-native version; the cost envelope and the
operational surface are very different.

| Pattern / tool in the large-cloud world | Where it lives in VelaFlow | Why this substitution |
|---|---|---|
| Databricks Lakehouse (workspace-managed Spark + Delta) | DuckDB analytical engine in `src/brain/engine/` + SQLite per-tenant catalog in `src/brain/catalog/` | Single-node, embedded, zero-ops; a SaaS for 1–1 000 paying tenants does not need a distributed cluster until it measurably does. The engine boundary is abstract, so Spark/Delta can be slotted in without rewriting the pipeline. |
| Delta Lake / Apache Iceberg (ACID table format, time travel) | DuckDB tables + append-only HMAC-chained action ledger | Tamper-evident history across all tenant-write paths without requiring an external metastore. Full Delta semantics are not needed at this tenant count. |
| Unity Catalog (metastore, RBAC, lineage, audit) | `src/brain/catalog/` (local catalog) + `src/brain/security/rbac.py` + `src/brain/security/action_ledger.py` | RBAC is enforced at the API + tenant-manager boundary; lineage is captured in the action ledger; audit is the HMAC-chained JSON-Lines stream. Offline-verifiable, single operator. |
| Mosaic AI / Vertex AI / Bedrock (managed LLM orchestration, RAG) | `src/brain/llm/` multi-model fallback + `src/brain/rag/` (chunker, embedder, DuckDB-backed vector store) | Avoids vendor lock-in; every tenant pays for only the upstream LLM minutes they use; Premium/VIP tiers can run fully local with Ollama. |
| Pinecone / Weaviate / Qdrant (managed vector DB) | Embedded vector store over DuckDB, per-tenant partitioned | One fewer network hop; per-tenant isolation is a row filter, not an index re-provision. |
| LangChain / LlamaIndex orchestration frameworks | Thin, explicit orchestration in `src/brain/pipeline/` with versioned prompt templates under `prompts/` | The platform does not need a general graph runtime; it needs five deterministic pipeline stages with clear contracts. Prompt provenance is captured in the ledger. |
| Apache Airflow / Dagster / Prefect (workflow orchestration) | `src/brain/queue/scheduler.py` — per-tenant cron loop + in-process queue | Scales horizontally behind Redis + KEDA when required; does not require a standing scheduler container for the single-operator deployment. |
| Kafka / Pulsar / Flink / Kinesis (streaming ingress + stream processing) | In-process `TaskQueue` with at-most-once idempotency keys; Redis swap-in documented for HA; DuckDB streaming ingestion on the Bronze layer | Same backpressure + retry semantics, no broker to run for a tenant count under four digits. Stream processing stays Python-native rather than JVM. |
| Kubernetes + KEDA + HPA (event-driven autoscaling) | Same — `deploy/k8s/` includes KEDA `ScaledObject` manifests for the worker; HPA for the API; GPU pool on a separate node-selector | Identical pattern, same manifests. The LXC path is simply "scale-to-one". |
| Databricks workspace admin (cluster policies, network isolation, Private Link, VNet injection, IP allow-lists) | systemd sandboxing (`NoNewPrivileges`, `ProtectSystem=strict`, `CapabilityBoundingSet=~CAP_SYS_ADMIN`, `LockPersonality`, `RestrictRealtime`), loopback-only Prometheus/Grafana, mandatory reverse-proxy-with-auth for any external access | The same posture — "this process has the minimum privilege and the minimum network reach required to do its job" — enforced at the unit-file layer rather than at the cloud-control-plane layer. |
| Databricks RBAC at workspace/catalog/object level | `src/brain/security/rbac.py` enforced at the API + tenant-manager boundary; `owner_google_sub` is first-class on the tenant row | Role resolution happens once per request and is bound to the Google OAuth subject claim; no in-band privilege escalation path. |
| Azure Key Vault / AWS KMS / HashiCorp Vault | `src/brain/security/encryption.py` (`FieldEncryptor` for at-rest data, `CredentialEncryptor` for third-party tokens) with operator pepper and per-tenant `owner_google_sub` binding | Envelope encryption with a split-knowledge requirement (pepper + owner sub). Rotating the pepper re-wraps every credential; no external KMS quota to budget. |
| Google Cloud IAM / Azure AD B2C (managed identity) | Mandatory Google OAuth via `src/brain/api/routes/auth.py`; no password auth exposed externally; `owner_google_sub` is a first-class column on the tenant row | The identity provider is the one the tenant already trusts; VelaFlow never stores a password hash, so there is nothing to leak. |
| Snyk + Dependabot + CodeQL in a managed CI | Local gates: Snyk Code + SCA, Bandit, pip-audit, pytest, preflight — all required to return zero before release | The same bar, enforced locally and in CI. See the **Engineering Status** table. |
| Datadog / New Relic APM | Prometheus metrics on `127.0.0.1:9090` + Grafana on `127.0.0.1:3000`, behind a reverse proxy with auth for remote access | Operator-owned, loopback by default; no customer data leaves the host to a third-party SaaS. |
| Terraform / Pulumi / Helm | `docker-compose.yml`, `deploy/k8s/` manifests, and `scripts/install.sh`; Helm charts are a v1.1 packaging step | The platform targets Proxmox LXC + Compose as the primary operator experience. Terraform can wrap the k8s manifests for multi-cloud; intentionally out of scope for v1.0. |
| MLOps: model versioning, drift monitoring, automated retraining, model serving | Versioned prompt templates under `prompts/`; model-id recorded per request in the action ledger; per-tenant fallback chain records which model served each call; drift is detected via downstream KPI checks on Gold outputs | VelaFlow consumes frontier LLMs as a dependency rather than training its own; the MLOps surface is limited to prompt versioning, model selection, and outcome telemetry. Training-pipeline tooling is intentionally out of scope. |
| Fraud-detection / anomaly-detection on real-money streams | Out of scope for v1.0. The scheduler, queue, and Gold-layer scoring algorithm are the correct substrate for it, but the platform has no financial-transaction domain model | Named here for honesty — a v1.4+ connector could plug a financial stream into the Bronze ingest without platform changes. |
| Geospatial / raster / GDAL / COG pipelines | Out of scope. VelaFlow ingests text-shaped personal-productivity data, not imagery | Named for honesty; a geospatial fork would replace the Bronze ingest and the DuckDB schema, not the security or scheduling substrate. |
| SAP / S/4HANA / BW/4HANA extractors | Out of scope. VelaFlow ingests from Todoist, Notion, Google Calendar, Gmail, and user-uploaded documents | The ingestion layer is pluggable (`src/brain/connectors/`) so additional sources are a small connector, not a platform rewrite. |

The pattern the reader should see is: the **shape** of each
responsibility (catalog, credential isolation, autoscaling, lineage,
multi-tenant boundaries, RBAC, identity, observability) is the same
one a managed cloud stack would use. The **realisation** is chosen to
fit a single operator on commodity hardware, with a deliberate
migration path for each component once the tenant base justifies it.
Rows explicitly marked *out of scope* are named so the reader does
not have to guess.

---

## 4. Product Overview

### Capabilities

| Capability | Description |
|------------|-------------|
| **Unified task ingest** | Todoist API v1 with cursor pagination; optional Google Calendar OAuth2; optional Gmail IMAP triage. |
| **Deterministic prioritisation** | A parameter-free scoring algorithm ranks tasks before any LLM touches them. The platform produces a usable digest even when every AI backend is unavailable. |
| **Multi-model LLM fallback** | Gemini 2.5 Pro → Flash → Flash-Lite → Groq llama-3.3-70b. The chain transparently handles rate limits, outages, and quota exhaustion. |
| **Private LLM (paid tiers)** | Ollama-based local inference with GPU auto-detection and CPU fallback. Default model fits 4 GB. |
| **Retrieval-augmented generation** | Per-tenant document store with chunked embeddings and vector search; 500 documents on Premium, 5,000 on VIP. |
| **Two-way Notion ↔ Todoist sync** | Edits in either system are reconciled without data loss. |
| **Delivery channels** | Email (SMTP), Notion page update, optional WhatsApp via CallMeBot. |
| **Per-tenant scheduling** | A multi-tenant cron scheduler queues each tenant's jobs at the times they configure, honouring their timezone. |
| **Tenant self-service GUI** | Streamlit surface gated by tier; controls above the tenant's tier are disabled and the API enforces the same rules. |
| **Billing** | Stripe Checkout + webhooks for all paid tiers; subscription state is the source of truth for tier. |
| **Encrypted backups** | Service-account Google Drive uploads with envelope encryption; zero-touch restore. |
| **Admin dashboard** | Prometheus metrics, Grafana dashboard, HMAC-chained action ledger. |

### How a typical day runs on VelaFlow

1. The per-tenant scheduler fires the tenant's daily digest at their
   configured time and timezone.
2. The worker decrypts that tenant's third-party credentials **in memory
   only**, constructs a per-request `Settings` object, and runs the
   Bronze → Silver → Gold pipeline inside an isolated task.
3. The Gold layer produces the digest; delivery respects the tenant's
   channel toggles (email, Notion, WhatsApp).
4. Every action — ingest, LLM call, delivery, configuration change — is
   appended to an HMAC-chained JSON-Lines ledger scoped to that tenant.
5. Daily usage counters are persisted so quota enforcement survives
   worker restarts.

---

## 5. Subscription Tiers

Enforcement is always at the API layer. The GUI is a convenience surface; it
does not grant capabilities the API would refuse.

| Capability | Free | Standard | Premium | VIP |
|------------|:----:|:--------:|:-------:|:---:|
| Todoist + Notion two-way sync | Fixed schedule | Configurable | Configurable | Configurable |
| Daily digest email | ✓ | ✓ custom time | ✓ custom time | ✓ custom time |
| Pipeline runs / day | 3 | 20 | 100 | 999 |
| Tasks tracked | 100 | 1,000 | 10,000 | 50,000 |
| LLM calls / day | 5 | 50 | 200 | 999 |
| Storage quota | 50 MB | 500 MB | 5 GB | 10 GB |
| WhatsApp + overdue alerts | — | ✓ | ✓ | ✓ |
| Gmail IMAP triage | — | ✓ | ✓ | ✓ |
| Google Calendar context | — | ✓ | ✓ | ✓ |
| Weekend planner + weekly review | — | ✓ | ✓ | ✓ |
| Local LLM (Ollama) | — | — | ✓ | ✓ |
| Premium cloud LLM (Gemini Pro) | — | — | ✓ | ✓ |
| NotebookLM synchronisation | — | — | ✓ | ✓ |
| **Native on-box RAG (`/api/v1/rag/*`)** | — | — | — | **✓ 5,000 / 500 q/day** |
| Bring-your-own `gemini_api_key` | — | — | ✓ | ✓ |
| Priority support | — | — | — | ✓ |

### Roles

| Role | Scope |
|------|-------|
| **Admin / Owner** | Platform operator. Auto-provisioned on first Google OAuth login from the configured owner email. Granted the VIP tier plus platform permissions. |
| **Demo** | Time-boxed tenant created by `scripts/seed_demo.py` or an admin action. Read-only for sensitive scopes; auto-expires. |
| **Free / Standard / Premium / VIP** | End-user tenants. Tier is set by Stripe webhooks after checkout; downgrade on cancellation. |

All non-owner tenants authenticate with Google OAuth. Password-based
authentication is not exposed externally; the legacy `POST /api/v1/tenants`
endpoint remains only for programmatic bootstrap and always creates a Free
tenant regardless of email.

---

## 6. Security and Privacy Model

VelaFlow's threat model assumes an attacker may already hold shell access
inside the host. Every filesystem-touching code path is layered so a local
shell cannot induce the application to write, read, chmod, or extract
outside an allow-listed set of directories, and no tenant credential is
persisted in cleartext.

### Static-analysis posture

| Scanner | Scope | Result |
|---------|-------|--------|
| Snyk Code (SAST) | Full Python tree | **0 findings at medium+**, **0 `.snyk` ignores** |
| `pip-audit` (SCA, PyPA-authoritative) | Installed Python packages | **0 known advisories** |
| Bandit | `src/` + `scripts/` | 0 medium or high |

### Key controls

- **`src/brain/security/safe_path.py`** — every untrusted path passes
  through an allow-list (`VELAFLOW_DATA_DIR`, process HOME, cwd,
  `/var/log/brain` on POSIX, `%PROGRAMDATA%\brain` on Windows) before
  any filesystem operation.
- **Inline sink sanitisation** — every filesystem sink re-validates the
  resolved path with `Path.resolve().relative_to(base)` immediately before
  the call. A symlink TOCTOU attempt hits the sanitiser a second time.
- **File-descriptor APIs over path-string APIs** — `os.fchmod(fd, …)` on
  the already-open stream, `os.open(path, …, mode=0o600)` at creation,
  `pathlib.Path.open('a')` for the action ledger. Chmod-by-path sinks
  are eliminated rather than merely guarded.
- **Hardened archive restore** — the backup restore path uses
  `tar.extractfile()` + `shutil.copyfileobj(..., length=64 * 1024)` with
  per-member and per-write containment checks. `tar.extract(path=…)` is
  not used.
- **Per-tenant field encryption** — AES-256-GCM with PBKDF2-derived keys
  per tenant, used for non-credential data at rest (e.g. webhook signing
  keys). The platform master key is held in the operator environment only
  (`VELAFLOW_MASTER_KEY`).
- **Credential vault (third-party API keys)** — from Round 18, tokens
  for Todoist, Notion, Gmail, LiteLLM, Gemini and Google OAuth are
  encrypted under a key derived from
  `HKDF-SHA256(ikm=pepper, salt=SHA256(tenant_id || 0x1F || owner_google_sub), info="velaflow-credential-v2")`
  with AES-256-GCM and the field name passed as authenticated associated
  data. The pepper (`VELAFLOW_CREDENTIAL_PEPPER`) must differ from the
  master key. An operator with shell access alone cannot decrypt a
  tenant's credentials without **both** the pepper **and** the tenant's
  Google OAuth subject claim. Ciphertext cannot be relocated between
  tenants or between fields.
- **HTTPS-only, always** — `HTTPSOnlyMiddleware` is the outermost
  middleware in every environment (including local dev); plain-HTTP
  requests receive `308 Permanent Redirect` before any auth or tenant
  resolution runs. `X-Forwarded-Proto: https` from a trusted reverse
  proxy is honoured; the only cleartext exception is the loopback
  `/health` family for systemd `ExecStartPost` probes.
- **HMAC-chained telemetry** — `src/brain/security/action_ledger.py`
  writes an append-only, HMAC-chained JSON-Lines record. Any tamper
  attempt breaks the chain and is detectable offline.
- **Systemd sandboxing** — units set `NoNewPrivileges=yes`,
  `ProtectSystem=strict`, `ProtectHome=read-only`,
  `CapabilityBoundingSet=~CAP_SYS_ADMIN`. From Round 19, every
  `scripts/brain-*.service` additionally sets `LimitCORE=0` (no core
  dumps), `LimitMEMLOCK=infinity` (allow `mlockall`),
  `LockPersonality=true`, `RestrictRealtime=true`, `ProtectClock=true`
  and `ProtectHostname=true`. The API and worker call `mlockall`
  (`src/brain/security/memlock.py`) at startup so decrypted credential
  pages are pinned in RAM and never written to swap.
- **Loopback-only observability** — Prometheus and Grafana bind to
  `127.0.0.1`; external access must go through a reverse proxy with auth.
- **Mandatory Google OAuth** — there is no password login exposed to
  external users.

See [docs/SECURITY-AUDIT.md](docs/SECURITY-AUDIT.md) for the full audit
history and [docs/security.md](docs/security.md) for operational guidance.

---

## 7. Engineering Status

| Signal | Status |
|--------|--------|
| Test suite | **528 / 528 passing** (`pytest tests/ --ignore=tests/test_stress.py`) |
| Snyk Code (SAST) | **0 findings across HIGH / MEDIUM / LOW**, 0 ignores, no `.snyk` |
| `pip-audit` (SCA — PyPA-authoritative) | **0 known advisories** against the resolved dependency graph (`pip install --dry-run -r requirements.txt` confirms `cryptography==46.0.7` is the resolved version) |
| Bandit | 0 medium / high |
| Preflight (`scripts/preflight.py`) | 0 blocking, 0 warnings (with master key, credential pepper, JWT secret, Google OAuth client id/secret, TLS cert + key configured) |
| HTTPS enforcement | Always-on: plain HTTP is 308-redirected to HTTPS in every environment, loopback `/health` excepted for systemd probes |
| Credential model | Third-party credentials encrypted under `HKDF(pepper \|\| SHA256(tenant_id\|owner_google_sub))` with AES-256-GCM and field-name AAD; operator shell access alone cannot decrypt without **both** the pepper **and** the tenant's owner sub |
| Dependency pinning | `cryptography>=46.0.7` and the rest of the critical path pinned in `requirements.txt` |
| Pentest history | 58 adversarial tests across 19 audit rounds (R14 baseline + R17 tenant isolation + R18 credential vault / HTTPS + R19 zero-LOW scrub + memory-dump hardening + credential-in-transit end-to-end); every finding remediated or formally mitigated |
| Memory-dump hardening | `mlockall(MCL_CURRENT\|MCL_FUTURE)` at process start, `LimitCORE=0`, `LimitMEMLOCK=infinity`, `LockPersonality`, `RestrictRealtime`, `ProtectClock`, `ProtectHostname` on all six brain-*.service units — decrypted credentials cannot reach swap or a core file |

---

## 8. Architecture (Summary)

VelaFlow follows a medallion data architecture per tenant.

```
Third-party APIs ──→ [ Bronze ] ──→ [ Silver ] ──→ [ Gold ] ──→ API / Digest / Notion
                     Raw land     Clean + dedup   Score +
                     Tenant-      + PII mask      AI-enrich
                     partitioned
```

- **Bronze**: raw API payloads, tenant-partitioned, stored in DuckDB.
- **Silver**: schema validation, dedup by business key, PII masking.
- **Gold**: deterministic scoring and AI-enriched digest production.

The runtime is structured as:

- **FastAPI** application factory (`brain.api.app:create_app`) on port 8765.
- **Queue + worker** (`brain.queue.worker`) running per-tenant jobs.
- **Tenant scheduler** (`brain.queue.scheduler`) — a cron-like loop that
  scans all active tenants and enqueues their scheduled jobs.
- **DuckDB analytical engine** and **SQLite data catalog**, both local.
- **Observability**: Prometheus + Grafana on loopback.
- **Billing**: Stripe Checkout + webhooks; subscription events drive tier
  changes.

Full diagrams and per-layer detail are in [docs/architecture.md](docs/architecture.md).

---

## 9. API Surface

Selected endpoints. All authenticated endpoints require a JWT issued by
the Google OAuth flow. Tier-gated endpoints return **403 with an
`upgrade_to` field** when the tenant's tier is insufficient.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Live / ready probes |
| `/metrics` | GET | Prometheus text format |
| `/api/v1/auth/google` | GET/POST | Google OAuth flow (owner and tenants) |
| `/api/v1/tenants/me` | GET | Current tenant profile |
| `/api/v1/tenants/me/config` | PATCH | Update schedule, delivery, tokens (tier-gated) |
| `/api/v1/pipelines/run` | POST | Trigger Bronze→Silver→Gold pipeline |
| `/api/v1/tasks/scored` | GET | Retrieve scored tasks (Gold layer) |
| `/api/v1/digests/daily` | GET | Retrieve daily digest |
| `/api/v1/rag/ingest` | POST | Ingest a document into the per-tenant DuckDB VSS vector store (**VIP only** — premium keeps NotebookLM) |
| `/api/v1/rag/query` | POST | Retrieval-augmented query over the tenant's own vectors (**VIP only**) |
| `/api/v1/rag/stats` | GET | Document count and per-tier quota (**VIP only**) |
| `/api/v1/rag/documents/{id}` | DELETE | Remove a document and all its chunks (**VIP only**) |
| `/api/v1/billing/checkout` | POST | Create Stripe checkout session |
| `/api/v1/webhooks/stripe` | POST | Stripe subscription webhook (public) |
| `/api/v1/dashboard/overview` | GET | Dashboard payload (connections, config, usage) |
| `/api/v1/vault/keys` | GET / POST | Encrypted per-tenant key vault |

The full surface is documented in [docs/architecture.md](docs/architecture.md).

---

## 10. Deployment

The production surface is **declarative Terraform**. `scripts/install.sh`
remains available as a developer quick-start and is deliberately labelled
dev-only.

**Zero-cost hosting is a project invariant — today.** VelaFlow's public
service must cost the maintainer nothing at steady state. Every **shipped**
deployment target is **free forever** at the provider tier we pin: the
homelab target uses owned hardware, `generic-vm` reuses an already-paid
host, and Oracle Cloud uses OCI's **Always Free** Ampere A1.Flex tier
(4 OCPU / 24 GB RAM, no expiry). Paid clouds (AWS, Azure, GCP, Databricks)
are deliberately **not** shipped as deployment targets in this release —
they would quietly break the free-forever promise if an operator picked
one by accident. They remain **open, documented future options**: if user
demand outgrows what free tiers can serve, a paid-cloud target can be
added and activated only when paid VIP revenue covers the incremental
bill (see [docs/scaling-path.md](docs/scaling-path.md) for Stage 1 /
Stage 2). The only paid surface that exists today is the **VIP** user
tier, priced strictly above its marginal cost.

VelaFlow's deploy contract is *portable host IaC*: the same Terraform
module installs an identical VelaFlow stack on any of these free Linux
hosts. Three equal-status target directories live under
[`deploy/terraform/`](deploy/terraform/README.md) — no cloud is "primary":

| Target                                                             | Host type                                                                        | Cost                                |
|--------------------------------------------------------------------|----------------------------------------------------------------------------------|-------------------------------------|
| [`deploy/terraform/proxmox/`](deploy/terraform/proxmox/)           | Unprivileged LXC on Proxmox VE (homelab, Intel N95 / 8 GB — the hardware floor)  | **€0** — self-hosted hardware       |
| [`deploy/terraform/generic-vm/`](deploy/terraform/generic-vm/)     | Any SSH-reachable Linux host already owned (home server, old PC, LXC guest)      | **€0** when reusing existing capacity |
| [`deploy/terraform/oracle-cloud/`](deploy/terraform/oracle-cloud/) | OCI **Always Free** Ampere A1.Flex VM (4 OCPU / 24 GB RAM, free forever)         | **€0** — forever                    |

All three delegate to the shared module
[`deploy/terraform/modules/velaflow-host/`](deploy/terraform/modules/velaflow-host/),
which renders a cloud-init document (Podman + systemd unit + nginx TLS
reverse proxy + data-dir permissions) and either pushes it via SSH
remote-exec (proxmox, generic-vm) or hands it to the caller's VM as
`user_data` (oracle-cloud). Either way the VelaFlow install itself is
strictly declarative.

### Preflight

Before `terraform apply`, validate the target structurally (no Terraform
CLI required):

```powershell
python scripts/terraform_preflight.py deploy/terraform/proxmox
python scripts/terraform_preflight.py deploy/terraform/generic-vm
python scripts/terraform_preflight.py deploy/terraform/oracle-cloud
```

The same checks run under pytest via
[`tests/test_terraform_modules.py`](tests/test_terraform_modules.py), so a
malformed `.tf` fails the CI suite. All three targets are additionally
validated with the real `terraform` CLI (`fmt -check`, `init -backend=false`,
`validate`) on Terraform v1.6+ — see `docs/SECURITY-AUDIT.md` Round 21.

### Dev quick-start (not production)

```bash
scripts/install.sh                          # Bash bootstrap on a single LXC / laptop
docker compose up -d                        # docker-compose.yml for local dev
```

### Scaling out

Kubernetes + KEDA manifests in `deploy/kubernetes/` are the Stage 2
horizontal-scale surface (see
[docs/scaling-path.md](docs/scaling-path.md)); they are **not** part of
this IaC tier because they dwarf the hardware floor. Rationale:
[`docs/adr/0003-terraform-iac-vs-bash-install.md`](docs/adr/0003-terraform-iac-vs-bash-install.md).

### Minimum configuration

```bash
export VELAFLOW_MASTER_KEY=$(python -c "import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())")
export JWT_SECRET=$(python -c "import secrets;print(secrets.token_urlsafe(48))")
export GOOGLE_OAUTH_CLIENT_ID="..."
export GOOGLE_OAUTH_CLIENT_SECRET="..."
export VELAFLOW_OWNER_EMAIL="you@example.com"
python scripts/preflight.py                  # must report 0 blocking
python -m uvicorn brain.api.app:create_app --factory --host 127.0.0.1 --port 8765
```

See [docs/deployment.md](docs/deployment.md) for full step-by-step
instructions, Kubernetes manifests, Grafana dashboards, and backup setup.

---

## 11. Roadmap

- **v1.0 (current)** — API, auth, tier enforcement, tenant scheduler,
  Streamlit GUI, Stripe billing, dashboard endpoints, demo seeder,
  encrypted Drive backups, HMAC-chained action ledger.
- **v1.1** — PostgreSQL tenant registry for multi-node writes. Redis
  queue as the recommended backend past a single worker.
- **v1.2 (headline)** — a per-tenant graphical, drag-and-drop workflow
  editor. This replaces the current Streamlit surface as the primary
  personalisation interface.
- **v1.3** — webpage-monitor connector, Gmail-topic watcher.

n8n Community Edition and the in-process queue are treated as provisional
v1.0 surfaces. The platform does not depend on either for its core
pipelines; both are swappable.

---

## 12. Repository Layout

```
.
├─ src/brain/                  Application code
│  ├─ api/                    FastAPI routes and middleware
│  ├─ queue/                  Worker + tenant scheduler
│  ├─ tenant/                 Models, manager, quotas
│  ├─ security/               safe_path, action_ledger, secure_logging, rbac
│  ├─ engine/                 DuckDB pipeline (Bronze → Silver → Gold)
│  ├─ catalog/                SQLite data catalog
│  ├─ rag/                    Chunker, embedder, vector store
│  └─ gui/                    Streamlit tier-gated self-service surface
├─ scripts/
│  ├─ preflight.py            Pre-deployment validator
│  ├─ seed_demo.py            Create time-boxed demo tenants
│  ├─ drive_backup.py         Service-account Drive backup + restore
│  └─ build_pdfs.py           Regenerate technical-reference PDFs
├─ tests/                      528 pytest cases
├─ deploy/
│  ├─ docker/                 Dockerfile + Compose
│  ├─ kubernetes/             Manifests + KEDA ScaledObject
│  ├─ lxc/                    Proxmox LXC helpers
│  ├─ observability/          Grafana dashboards, Prometheus config
│  └─ terraform/              Portable host IaC (proxmox + generic-vm + oracle-cloud via shared module)
├─ docs/
│  ├─ architecture.md
│  ├─ architecture-enterprise.md
│  ├─ deployment.md
│  ├─ scaling-path.md
│  ├─ security.md
│  ├─ SECURITY-AUDIT.md
│  └─ adr/                    Architecture Decision Records (0001, 0002, 0003)
└─ config/pipeline.yaml        Declarative per-stage configuration
```

---

## 13. Authorship and provenance

Written and maintained by one person. Inception date: **2026-04-17**.

Measured at the current HEAD:

- `src/` — ~**15 000** lines of application code (R21: native RAG API, portable IaC)
- `tests/` — ~**6 000** lines of tests (**528 passing**, 0 ignores)
- `scripts/` — **~3 500** lines of operational scripts and validators
- `docs/` — **~9 500** lines of technical documentation

The code was authored with heavy use of GitHub Copilot inside VS Code.
Every architectural decision, every threat-model round, every control
listed in Section 2.5 (*The secret formula — specs, not slogans*), and
every remediation in [docs/SECURITY-AUDIT.md](docs/SECURITY-AUDIT.md)
is the maintainer's. The code is AI-assisted; the design, the
boundaries, and the risk posture are not.

---

## 14. License

MIT. See [LICENSE](LICENSE).
