# VelaFlow Enterprise Architecture

> Multi-tenant distributed medallion architecture with JWT authentication, RBAC, field-level
> encryption, DuckDB analytical engine, SQLite data catalog, zero-trust inter-component security,
> and auto-scaling deployment — fully on-prem, no cloud dependency.

---

## System Overview

```mermaid
graph TB
    subgraph "Client Layer"
        CLI[CLI - brain commands]
        API[REST API - FastAPI]
        N8N[n8n Workflows<br/>Per-tenant orchestration<br/>5 webhook endpoints]
    end

    subgraph "Authentication & Authorization"
        JWT[JWT Token Service<br/>HS256, iss/aud claims, 1h expiry]
        RBAC[RBAC Engine<br/>4 roles, 13 permissions]
        MW[Tenant Context Middleware<br/>Request-scoped isolation]
        ZT[Zero-Trust Signing<br/>HMAC-SHA256, nonce, replay prevention]
    end

    subgraph "Medallion Pipeline"
        B[Bronze Layer<br/>Raw API ingestion]
        S[Silver Layer<br/>Clean, dedup, PII mask]
        G[Gold Layer<br/>Score, enrich, digest]
    end

    subgraph "Analytical Engine"
        DUCK[DuckDB Engine<br/>SQL-native, 512 MB limit]
        CAT[SQLite Catalog<br/>Namespaces, schemas, tables]
        LIN[Lineage Tracker<br/>Stage-level provenance]
        MP[Medallion Processor<br/>SQL transforms in DuckDB]
    end

    subgraph "Data Protection"
        PII[PII Detector<br/>6 pattern categories]
        ENC[Field Encryptor<br/>Per-tenant PBKDF2 keys]
        PATH[Path Validator<br/>Traversal prevention]
        SAN[Input Sanitizer<br/>Boundary validation]
        AUD[Audit Logger<br/>Structured security events]
    end

    subgraph "Tenant Management"
        TM[Tenant Manager<br/>CRUD, lifecycle]
        TQ[Tenant Quotas<br/>Tier-based limits]
        TS[Tenant Storage<br/>Partitioned isolation]
    end

    subgraph "Queue & Workers"
        Q[Task Queue<br/>In-process / Redis]
        W[Queue Worker<br/>Handler dispatch + retry]
        DL[Dead Letter Queue<br/>Failed message capture]
    end

    subgraph "LLM Layer"
        LLM[Cloud LLM Proxy<br/>Gemini / Groq]
        LLLM[Local LLM<br/>Ollama - qwen2:1.5b<br/>Premium tier only]
    end

    subgraph "Storage"
        LS[Local Storage<br/>JSON files, atomic writes]
        DDB[DuckDB Files<br/>Columnar analytical store]
        CDB[SQLite Catalog DB<br/>WAL mode, secure_delete]
    end

    subgraph "External APIs"
        TOD[Todoist API v1]
        CAL[Google Calendar]
        GMAIL[Gmail IMAP]
    end

    API --> JWT --> RBAC --> MW
    MW --> ZT
    N8N -->|5 webhooks| API
    MW --> B
    CLI --> B
    B --> S --> G
    S --> PII
    S --> ENC
    B --> LS
    S --> LS
    G --> LS
    B --> TOD & CAL & GMAIL
    G --> LLM
    G -.->|premium| LLLM
    TM --> TS
    TQ --> TM
    Q --> W --> DL
    MP --> DUCK
    MP --> CAT --> LIN
    DUCK --> DDB
    CAT --> CDB
    SAN --> MW
    AUD --> API
```

---

## Medallion Architecture

### Bronze Layer (`src/brain/pipeline/bronze.py`)

Raw data lands exactly as received from external APIs. No transformation, no cleaning.

```mermaid
sequenceDiagram
    participant API as External API
    participant B as Bronze Layer
    participant S as Storage

    B->>API: Fetch raw data
    API-->>B: JSON response
    B->>B: Add metadata (timestamp, tenant_id, batch_id)
    B->>S: Write to bronze/{tenant_id}/{source}/{batch_id}.json
```

**Design principles:**
- Data is immutable once written
- Tenant-partitioned by directory structure
- Batch IDs enable replay and audit
- No schema enforcement — schema is applied at Silver

**Sources:**
| Source | Method | Data |
|--------|--------|------|
| Todoist | `ingest_todoist(tasks)` | Raw task objects |
| Calendar | `ingest_calendar(events)` | Raw calendar events |
| Gmail | `ingest_gmail(emails)` | Raw email metadata |

### Silver Layer (`src/brain/pipeline/silver.py`)

Cleans, validates, deduplicates, and masks PII.

```mermaid
flowchart LR
    B[Bronze Data] --> V[Schema Validation]
    V --> D[Dedup by Business Key]
    D --> P[PII Detection & Masking]
    P --> N[Type Normalization]
    N --> S[Silver Storage]
```

**Processing steps:**
1. **Schema validation** — Required fields enforced per source type
2. **Deduplication** — Business key extraction (task ID, event ID, message ID)
3. **PII masking** — Automatic scanning and replacement before persistence
4. **Type normalization** — Date parsing, priority mapping, duration extraction

### Gold Layer (`src/brain/pipeline/gold.py`)

Produces consumption-ready datasets: scored task lists and daily digests.

```mermaid
flowchart LR
    S[Silver Data] --> SC[Scoring Engine]
    SC --> R[Rank & Sort]
    R --> D[Daily Digest Builder]
    D --> G[Gold Storage]
    G --> API[REST API / Email / Notion]
```

**Outputs:**
| Dataset | Description | Consumer |
|---------|-------------|----------|
| Scored Tasks | Tasks with deterministic priority scores | API `/tasks/scored`, CLI |
| Daily Digest | Top-N tasks + calendar context + insights | API `/digests/daily`, Email |

### Pipeline Scheduler (`src/brain/pipeline/scheduler.py`)

Orchestrates the full Bronze → Silver → Gold DAG.

```python
# Single execution
scheduler = PipelineScheduler(bronze, silver, gold)
result = scheduler.execute(tenant_id="t_abc123")
# Returns: PipelineRun with per-stage timing and status
```

---

## Multi-Tenant Architecture

### Tenant Isolation Model

```mermaid
graph TB
    subgraph "Tenant A (Free)"
        A_S[Storage: data/tenants/a/]
        A_K[Encryption Key: PBKDF2(master, 'a')]
        A_Q[Quota: 3 runs/day, 100 tasks]
    end

    subgraph "Tenant B (Premium)"
        B_S[Storage: data/tenants/b/]
        B_K[Encryption Key: PBKDF2(master, 'b')]
        B_Q[Quota: 100 runs/day, 10000 tasks]
    end

    subgraph "Shared Infrastructure"
        API[FastAPI Server]
        Q[Task Queue]
        W[Workers]
    end

    API --> A_S & B_S
    Q --> W
```

**Isolation guarantees:**
- **Storage**: Each tenant gets a separate directory partition
- **Encryption**: Per-tenant derived keys — compromise of one does not expose others
- **Quotas**: Tier-based limits enforced before pipeline execution
- **RBAC**: Permissions checked at every API endpoint

### Tenant Tiers

| Tier | Pipeline Runs/Day | Max Tasks | LLM Calls/Day | Premium LLM | Nested LXC |
|------|-------------------|-----------|---------------|-------------|------------|
| Free | 3 | 100 | 5 | No | No |
| Standard | 20 | 1,000 | 50 | No | No |
| Premium | 100 | 10,000 | 200 | Yes (Ollama — qwen2:1.5b) | Yes |

---

## n8n Per-Tenant Orchestration

Each tenant customises their VelaFlow pipeline through n8n’s visual workflow editor.
n8n acts as the orchestration layer between the tenant and the VelaFlow API:

```mermaid
sequenceDiagram
    participant N as n8n Workflow
    participant API as VelaFlow API
    participant Q as Task Queue
    participant W as Worker
    participant D as Delivery

    N->>API: POST /api/v1/webhooks/pipeline [Bearer JWT]
    API->>Q: Enqueue PIPELINE_RUN message
    API-->>N: 202 {message_id, status: "queued"}

    N->>API: POST /api/v1/webhooks/catalog [Bearer JWT]
    API-->>N: 200 {tables, lineage, stats}

    N->>API: POST /api/v1/webhooks/llm [Bearer JWT]
    API-->>N: 200 {generated_text}

    N->>API: POST /api/v1/webhooks/tenant [Bearer JWT]
    API-->>N: 200 {tenant provisioned}

    Q->>W: Dequeue message
    W->>W: Execute Bronze → Silver → Gold
    W-->>Q: Done

    N->>API: GET /api/v1/digests/daily [Bearer JWT]
    API-->>N: {digest, scored_tasks}

    N->>D: Email / WhatsApp / Notion / Slack
```

**Per-tenant customisation (no coding required):**
- Schedule: change Cron trigger in n8n
- Data sources: toggle which APIs are included
- Delivery channels: add/remove Email, WhatsApp, Notion, Slack nodes
- Filters: n8n IF/Switch nodes route tasks by priority or label
- LLM polish: premium tenants route through local Ollama

**n8n webhook endpoints (5 total):**

| Endpoint | Purpose | Mode |
|----------|---------|------|
| `POST /webhooks/pipeline` | Trigger full Bronze→Silver→Gold pipeline | Async (queued) |
| `POST /webhooks/digest` | Generate daily digest | Async (queued) |
| `POST /webhooks/catalog` | Query catalog metadata (tables, lineage, stats) | Synchronous |
| `POST /webhooks/llm` | Trigger LLM text generation | Synchronous |
| `POST /webhooks/tenant` | Tenant provisioning and config management | Synchronous |

---

## Premium Tier — Local LLM

Premium tenants get a dedicated Ollama instance for privacy-first inference.
GPU is used automatically if available; otherwise CPU-only mode is used.

```mermaid
flowchart LR
    REQ[Premium LLM Request] --> DET{GPU Available?}
    DET -->|Yes| GPU[GPU Inference<br/>nvidia-smi detected]
    DET -->|No| CPU[CPU Inference<br/>qwen2:1.5b default]
    GPU --> OLL[Ollama Server :11434]
    CPU --> OLL
    OLL --> RES[Generated Text]
```

| Feature | Detail |
|---------|--------|
| Default model | qwen2:1.5b (934 MB — fits 4 GB allocation) |
| GPU detection | Automatic via `nvidia-smi` at startup |
| Scaling | KEDA scales premium pods 0→3 based on LLM queue depth |
| Upgrade path | Change `PREMIUM_LLM_MODEL` env var: phi3:3.8b → llama3.2:3b → larger |

---

## Security Architecture

### Authentication Flow

```mermaid
sequenceDiagram
    participant C as Client
    participant API as FastAPI
    participant Auth as JWT Service
    participant RBAC as RBAC Engine
    participant P as Pipeline

    C->>API: POST /api/v1/tenants/login {email, password}
    API->>Auth: Verify credentials
    Auth-->>API: JWT token (tenant_id, role, exp)
    API-->>C: {access_token: "eyJ..."}

    C->>API: GET /api/v1/tasks/scored [Authorization: Bearer eyJ...]
    API->>Auth: verify_token(token)
    Auth-->>API: TokenClaims(tenant_id, role)
    API->>RBAC: check_permission(role, READ_TASKS)
    RBAC-->>API: Allowed
    API->>P: get_scored_tasks(tenant_id)
    P-->>API: [ScoredTask, ...]
    API-->>C: 200 OK [{task, score}, ...]
```

### RBAC Permission Matrix

| Permission | Free | Standard | Premium | Admin |
|-----------|------|----------|---------|-------|
| `READ_BRONZE` | Yes | Yes | Yes | Yes |
| `READ_SILVER` | Yes | Yes | Yes | Yes |
| `READ_GOLD` | Yes | Yes | Yes | Yes |
| `WRITE_BRONZE` | Yes | Yes | Yes | Yes |
| `VIEW_TENANT` | Yes | Yes | Yes | Yes |
| `RUN_PIPELINE` | Yes | Yes | Yes | Yes |
| `VIEW_PIPELINE_RUNS` | Yes | Yes | Yes | Yes |
| `GENERATE_DIGEST` | Yes | Yes | Yes | Yes |
| `MANAGE_TENANT` | No | Yes | Yes | Yes |
| `USE_LLM` | No | Yes | Yes | Yes |
| `USE_PREMIUM_LLM` | No | No | Yes | Yes |
| `ADMIN_ALL` | No | No | No | Yes |

### PII Detection Patterns

| Pattern | Regex | Example |
|---------|-------|---------|
| Credit Card | `\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b` | `4111-1111-1111-1111` |
| Email | `\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z]{2,}\b` | `user@example.com` |
| International Phone | `\b\+\d{1,3}[\s.-]?\d{3,14}\b` | `+351 912 345 678` |
| US SSN | `\b\d{3}-\d{2}-\d{4}\b` | `123-45-6789` |
| IBAN | `\b[A-Z]{2}\d{2}[A-Z0-9]{4,30}\b` | `PT50000201231234567890154` |
| Portuguese NIF | `\b\d{9}\b` (context-aware) | `123456789` |

### Field-Level Encryption

```
Plaintext → PBKDF2(master_key, tenant_id, 100k iterations) → tenant_key
tenant_key + random_nonce → AES-256-GCM → ciphertext (with authentication tag)
Stored: nonce + ciphertext (GCM tag embedded)
```

### Zero-Trust Inter-Component Security (`security/zero_trust.py`)

All inter-component communication is signed and verified:

```mermaid
sequenceDiagram
    participant C as Component A
    participant S as RequestSigner
    participant V as Component B

    C->>S: sign(method, path, body, component_id)
    S->>S: Generate nonce + timestamp
    S->>S: HMAC-SHA256(secret, method|path|timestamp|nonce|body_hash)
    S-->>C: SignedRequest{signature, nonce, timestamp}

    C->>V: HTTP request + SignedRequest headers
    V->>S: verify(method, path, body, signed)
    S->>S: Check timestamp drift (5-min window)
    S->>S: Check nonce not replayed
    S->>S: Recompute HMAC and compare
    S-->>V: Valid / Invalid
```

**Defense layers:**
| Attack | Defense |
|--------|---------|
| Replay attack | Nonce tracking — each nonce used exactly once |
| Timestamp drift | 5-minute window — rejects stale requests |
| Body tampering | HMAC covers method + path + timestamp + nonce + body hash |
| Secret extraction | `VELAFLOW_COMPONENT_SECRET` env var, ephemeral fallback |

**Input sanitization** (`InputSanitizer`):
| Boundary | Validation |
|----------|-----------|
| Tenant ID | Safe chars only `[a-zA-Z0-9_-]`, max 64 chars |
| Content | Max 10,000 chars |
| Identifiers | Alphanumeric + underscore, max 128 chars |
| Labels | Max 50 labels, each validated as identifier |
| Dangerous patterns | SQL injection, XSS, path traversal detection |

**Audit logging** (`AuditLogger`):
- `AUTH_SUCCESS` / `AUTH_FAILURE` — authentication events
- `PERMISSION_DENIED` — RBAC violations
- `DATA_ACCESS` — data layer reads/writes with record counts
- `PIPELINE_EVENT` — stage completion with record counts
- `SECURITY_EVENT` — anomalous patterns (path traversal, injection attempts)

---

## Deployment Architecture

### LXC (Single-Node)

```mermaid
graph TB
    subgraph "Proxmox Host"
        subgraph "Primary LXC (Debian 12)"
            API[FastAPI Server :8000]
            W[Queue Worker]
            R[Redis]
            SYS[systemd timers]
            subgraph "Nested LXC (Premium Tier)"
                OL[Ollama + Local LLM]
                PW[Premium Worker]
            end
        end
    end

    API --> R --> W
    W -.->|premium tasks| OL
```

### Kubernetes with KEDA

```mermaid
graph TB
    subgraph "velaflow namespace"
        subgraph "API Deployment"
            API1[API Pod 1]
            API2[API Pod 2]
        end
        subgraph "Worker Deployment (KEDA)"
            W1[Worker Pod 1]
            W2[Worker Pod 2]
            Wn[Worker Pod N<br/>max: 10]
        end
        subgraph "Premium Deployment (KEDA)"
            P1[Premium Pod 1<br/>GPU]
        end
        SVC[Service :8000]
        ING[Ingress]
        REDIS[Redis]
        KEDA[KEDA Scaler<br/>Queue depth trigger]
    end

    ING --> SVC --> API1 & API2
    API1 & API2 --> REDIS
    KEDA --> W1 & W2 & Wn
    KEDA --> P1
    REDIS --> W1 & W2 & Wn
```

### On-Prem Analytical Engine — DuckDB

The medallion pipeline runs entirely on-prem via DuckDB. VelaFlow deliberately
chose a self-hosted stack over a managed lakehouse (Databricks / Snowflake /
BigQuery) for data sovereignty, zero idle cost on Oracle Always-Free, and a
lightweight in-process runtime (no JVM, no cluster spin-up).

```mermaid
graph LR
    subgraph "DuckDB Engine (in-process)"
        BT[bronze_tasks<br/>Raw ingested records]
        ST[silver_tasks<br/>Deduped, validated, hashed]
        GT[gold_scored_tasks<br/>Scored + enriched]
    end

    subgraph "SQLite Catalog"
        NS[Namespaces]
        SCH[Schemas<br/>bronze / silver / gold]
        TBL[Table Registry]
        GR[RBAC Grants]
        LIN[Lineage Records]
    end

    BT -->|SQL: ROW_NUMBER dedup<br/>MD5 hash, content filter| ST
    ST -->|Python scoring + SQL persist| GT
    TBL --> NS & SCH
    GR --> SCH
    LIN --> TBL
```

| Component | Purpose | Config |
|-----------|---------|--------|
| `engine/connection.py` | DuckDB connection factory, memory-safe, parameterised queries | `DUCKDB_MEMORY_LIMIT` (default 512 MB) |
| `engine/processor.py` | SQL-based medallion transforms (Bronze→Silver→Gold) | Tables auto-registered in catalog |
| `catalog/store.py` | SQLite-backed catalog with RBAC grants and lineage | `VELAFLOW_CATALOG_DB` path |
| `catalog/models.py` | Domain models: Namespace, Schema, Table, Grant, Lineage | Enum-based grant levels |
| `config/pipeline.yaml` | Declarative pipeline config (stages, grants, engine, environments) | Stages, grants, engine, environments |

---

## Module Dependency Graph

```mermaid
graph TB
    subgraph "Original Modules (unchanged)"
        CLI[cli.py]
        CFG[config.py]
        MOD[models.py]
        PLN[planner.py]
        TOD[todoist.py]
        NOT[notion.py]
        LLM[llm.py]
    end

    subgraph "Enterprise Modules (new)"
        B[pipeline/bronze.py]
        S[pipeline/silver.py]
        G[pipeline/gold.py]
        SCH[pipeline/scheduler.py]
        APP[api/app.py]
        AUTH[api/auth.py]
        MID[api/middleware.py]
        WHK[api/routes/webhooks.py]
        PII[security/pii.py]
        ENC[security/encryption.py]
        ZT[security/zero_trust.py]
        DUCK[engine/connection.py]
        MPROC[engine/processor.py]
        CMOD[catalog/models.py]
        CSTR[catalog/store.py]
        RBAC_M[security/rbac.py]
        TEN[tenant/manager.py]
        TMOD[tenant/models.py]
        STR[storage/local.py]
        QUE[queue/tasks.py]
        WRK[queue/worker.py]
        LLM_L[llm_local.py]
    end

    %% Enterprise depends on original
    G --> PLN
    G --> MOD
    S --> PII
    B --> STR
    S --> STR
    G --> STR
    TEN --> STR
    TEN --> ENC
    TEN --> TMOD
    SCH --> B & S & G
    APP --> AUTH & MID & RBAC_M
    APP --> WHK
    WHK --> QUE
    WRK --> QUE
    LLM_L -.->|premium tier| LLM

    %% Original internal deps (unchanged)
    CLI --> CFG & PLN & TOD & NOT & LLM
    PLN --> MOD
```

---

## Test Coverage Summary

| Module | Tests | Key Assertions |
|--------|-------|---------------|
| Storage | 11 | CRUD, path traversal prevention, overwrite, nested paths |
| PII Detection | 15 | All 6 pattern categories, custom patterns, multi-match masking |
| Encryption + RBAC | 22 | Roundtrip, tenant isolation, key tampering, all 4 roles |
| Bronze Pipeline | 7 | Per-source ingestion, tenant isolation, batch listing |
| Silver Pipeline | 13 | Schema validation, dedup, PII masking, persistence |
| Gold Pipeline | 8 | Scoring accuracy, digest production, persistence |
| Pipeline Scheduler | 7 | Full DAG orchestration, multi-source, error propagation |
| Tenant Management | 17 | CRUD lifecycle, tier changes, token encryption, partition isolation |
| JWT Auth | 6 | Create/verify tokens, expiry, tampering, missing claims |
| Queue/Worker | 10 | FIFO ordering, retry logic, dead letter, handler dispatch |
| Webhooks | 6 | Models, queue integration for n8n |
| Local LLM | 13 | GPU detection, hardware profile, Ollama client |
| **Total** | **159** | **All passing** |

---

## Data Flow: End-to-End Request

```mermaid
sequenceDiagram
    participant U as User
    participant API as FastAPI
    participant JWT as JWT Service
    participant RBAC as RBAC
    participant SCH as Scheduler
    participant B as Bronze
    participant S as Silver
    participant G as Gold
    participant TOD as Todoist API
    participant STR as Storage

    U->>API: POST /api/v1/pipelines/run [Bearer token]
    API->>JWT: verify_token()
    JWT-->>API: tenant_id, role
    API->>RBAC: check(role, RUN_PIPELINE)
    RBAC-->>API: OK

    API->>SCH: execute(tenant_id)

    SCH->>B: ingest_todoist(tenant_id, tasks)
    B->>TOD: Fetch tasks
    TOD-->>B: Raw tasks
    B->>STR: Write bronze/tenant_id/todoist/batch.json
    B-->>SCH: BronzeResult

    SCH->>S: process_todoist(tenant_id)
    S->>STR: Read bronze data
    S->>S: Validate, dedup, mask PII
    S->>STR: Write silver/tenant_id/todoist/batch.json
    S-->>SCH: SilverResult

    SCH->>G: produce_scored_tasks(tenant_id)
    G->>STR: Read silver data
    G->>G: Score (deterministic algorithm)
    G->>STR: Write gold/tenant_id/scored_tasks.json
    G-->>SCH: GoldResult

    SCH-->>API: PipelineRun(status=completed)
    API-->>U: 200 OK {run_id, stages, timing}
```
