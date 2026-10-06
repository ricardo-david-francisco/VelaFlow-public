# VelaFlow — System Architecture (Visual Reference)

> Use this document with NotebookLM to generate technical deep-dive questions and build a
> learning path. Every diagram below is self-contained and explains one dimension
> of the system.

---

## 1. High-Level System Architecture

This is the "big picture." It shows every external service, how VelaFlow connects
to them, and where secrets live.

```mermaid
flowchart TB
    subgraph PROXMOX["Proxmox Host"]
        subgraph LXC["LXC Container · Debian 12 · Unprivileged"]
            direction TB
            TIMERS["systemd timers<br/>5 scheduled units<br/>Persistent=true"]
            CLI["brain CLI<br/>(Python 3.11+ venv)"]
            DOCKER_N8N["Docker · n8n<br/>(optional visual scheduler)"]
            SECRETS["/etc/brain/secrets.env<br/>mode 0600 · proxy token only"]

            TIMERS -->|"ExecStart"| CLI
            SECRETS -.->|"EnvironmentFile"| CLI
        end
    end

    subgraph VPS["Your VPS — The Fortress"]
        PROXY["LiteLLM Proxy<br/>Real AI API keys live here<br/>Budget caps · per-token audit"]
    end

    subgraph AI_PROVIDERS["AI Providers"]
        GEMINI_PRO["Gemini 2.5 Pro"]
        GEMINI_FLASH["Gemini 2.5 Flash"]
        GEMINI_LITE["Gemini Flash-Lite"]
        GROQ["Groq llama-3.3-70b"]
    end

    subgraph DATA_SERVICES["Data Services"]
        TODOIST["Todoist REST API v1<br/>Cursor-paginated"]
        NOTION["Notion API<br/>Central Dashboard"]
        GCAL["Google Calendar<br/>OAuth2"]
        GMAIL_IN["Gmail IMAP4_SSL<br/>Unread polling"]
        NLM["Google NotebookLM<br/>Playwright automation"]
    end

    subgraph DELIVERY["Delivery Channels"]
        GMAIL_OUT["Gmail SMTP<br/>STARTTLS enforced"]
        WHATSAPP["CallMeBot<br/>WhatsApp API"]
        NOTION_DASH["Notion Dashboard<br/>4 synced databases"]
    end

    %% Zero-Trust AI path
    CLI -->|"HTTPS · budget-capped token"| PROXY
    PROXY -->|"real key (never leaves VPS)"| GEMINI_PRO
    PROXY -.->|"fallback 1"| GEMINI_FLASH
    PROXY -.->|"fallback 2"| GEMINI_LITE
    PROXY -.->|"fallback 3"| GROQ

    %% Data ingestion
    CLI <-->|"HTTPS Bearer"| TODOIST
    CLI <-->|"HTTPS Bearer"| NOTION
    CLI -->|"OAuth2 refresh"| GCAL
    CLI -->|"IMAP4_SSL"| GMAIL_IN
    CLI -->|"Playwright + cookies"| NLM

    %% Delivery
    CLI -->|"SMTP/TLS"| GMAIL_OUT
    CLI -->|"HTTPS GET"| WHATSAPP
    CLI -->|"REST upsert"| NOTION_DASH

    %% n8n alternative
    DOCKER_N8N -.->|"alternative scheduler"| CLI

    style VPS fill:#2d6a4f,color:#fff
    style PROXY fill:#2d6a4f,color:#fff
    style LXC fill:#1b263b,color:#fff
    style SECRETS fill:#e63946,color:#fff
```

### How to explain this technically

> "VelaFlow runs inside an unprivileged Proxmox LXC container. Five systemd
> timers trigger a Python CLI that pulls data from Todoist, Google Calendar, and
> Gmail, scores tasks deterministically, polishes the output with an LLM, and
> delivers the result via email, WhatsApp, and Notion. The key security decision
> is the Zero-Trust Proxy: real AI API keys never enter the container — only a
> budget-capped proxy token lives there. If the container is compromised, the
> attacker can spend at most a few dollars before the token self-destructs."

---

## 2. Data Flow — Daily Briefing (end-to-end)

This is the most important flow to understand in technical reviews. It exercises every
major component.

```mermaid
sequenceDiagram
    autonumber
    participant Timer as systemd timer<br/>Mon-Fri 07:00
    participant CLI as brain CLI
    participant Todoist as Todoist API v1
    participant GCal as Google Calendar
    participant Gmail as Gmail IMAP
    participant Scorer as planner.py<br/>Scoring Engine
    participant LLM as llm.py<br/>Fallback Chain
    participant Proxy as LiteLLM Proxy<br/>(VPS)
    participant SMTP as Gmail SMTP
    participant WA as CallMeBot
    participant Notion as Notion API

    Timer->>CLI: ExecStart: brain daily

    rect rgb(40, 60, 90)
        Note over CLI,Gmail: PARALLEL DATA INGESTION
        CLI->>Todoist: GET /tasks (cursor-paginated)
        Todoist-->>CLI: Task[] (today + upcoming 7d)
        CLI->>GCal: GET /calendar/v3/events
        GCal-->>CLI: CalendarEvent[]
        CLI->>Gmail: IMAP SEARCH UNSEEN (last 24h)
        Gmail-->>CLI: EmailAlert[]
    end

    rect rgb(60, 40, 20)
        Note over CLI,Scorer: DETERMINISTIC SCORING
        CLI->>Scorer: score_task() for each task
        Note over Scorer: Overdue +20/day (cap 7d)<br/>Due today +25<br/>Priority P1 +18<br/>@focus +14<br/>Quick-win +4<br/>No date & no focus −4
        Scorer-->>CLI: ScoredTask[] sorted by score
        CLI->>Scorer: build_daily_digest(scored, events, emails)
        Scorer-->>CLI: DigestResult (subject + body)
    end

    rect rgb(30, 70, 50)
        Note over CLI,Proxy: LLM POLISH (Zero-Trust)
        CLI->>LLM: polish_digest(raw_text, prompt)
        LLM->>Proxy: POST /chat/completions (proxy token)
        Note over Proxy: Routes to Gemini Pro (real key on VPS)
        Proxy-->>LLM: Polished text
        Note over LLM: If proxy fails → Gemini Flash → Flash-Lite → Groq → raw text
        LLM-->>CLI: Final polished briefing
    end

    rect rgb(70, 40, 60)
        Note over CLI,Notion: PARALLEL DELIVERY
        CLI->>SMTP: Send HTML digest email (STARTTLS)
        CLI->>WA: Send WhatsApp summary (HTTPS GET)
        CLI->>Notion: Upsert to Daily Planner DB
    end
```

### How to explain this technically

> "The daily briefing follows a pipeline pattern: ingest, score, polish, deliver.
> First, we fetch tasks, calendar events, and emails in parallel. Then the
> scoring engine — which is pure Python, no ML — ranks every task using a
> deterministic point system: overdue days, priority level, labels, and duration.
> The ranked output is sent to an LLM for natural-language polish via a
> multi-model fallback chain. If all LLMs fail, the raw scored digest is still
> delivered. Finally, the result is pushed to email, WhatsApp, and Notion
> simultaneously."

---

## 3. Multi-Model LLM Fallback Chain

```mermaid
flowchart TD
    START["polish_digest() called<br/>with raw text + system prompt"]

    START --> PROXY_CHECK{"LiteLLM Proxy<br/>configured?"}

    PROXY_CHECK -->|"Yes"| PROXY_CALL["Call proxy<br/>(budget-capped token)"]
    PROXY_CALL --> PROXY_OK{"Success?"}
    PROXY_OK -->|"Yes"| DONE["Return polished text"]
    PROXY_OK -->|"No"| DEMO_CHECK{"demo_mode?"}
    DEMO_CHECK -->|"Yes"| RAW["Return raw text<br/>(no further fallback)"]
    DEMO_CHECK -->|"No"| GOOGLE_CHECK

    PROXY_CHECK -->|"No"| GOOGLE_CHECK{"Google AI key<br/>available?"}

    GOOGLE_CHECK -->|"Yes"| G_PRO["Gemini 2.5 Pro"]
    G_PRO --> G_PRO_OK{"Success?"}
    G_PRO_OK -->|"Yes"| DONE
    G_PRO_OK -->|"No (429/error)"| G_FLASH["Gemini 2.5 Flash"]
    G_FLASH --> G_FLASH_OK{"Success?"}
    G_FLASH_OK -->|"Yes"| DONE
    G_FLASH_OK -->|"No"| G_LITE["Gemini Flash-Lite"]
    G_LITE --> G_LITE_OK{"Success?"}
    G_LITE_OK -->|"Yes"| DONE
    G_LITE_OK -->|"No"| GROQ_CHECK

    GOOGLE_CHECK -->|"No"| GROQ_CHECK{"Groq key<br/>available?"}

    GROQ_CHECK -->|"Yes"| GROQ_CALL["Groq llama-3.3-70b"]
    GROQ_CALL --> GROQ_OK{"Success?"}
    GROQ_OK -->|"Yes"| DONE
    GROQ_OK -->|"No"| RAW

    GROQ_CHECK -->|"No"| RAW

    style DONE fill:#2d6a4f,color:#fff
    style RAW fill:#e9c46a,color:#000
    style PROXY_CALL fill:#264653,color:#fff
    style G_PRO fill:#264653,color:#fff
    style G_FLASH fill:#287271,color:#fff
    style G_LITE fill:#2a9d8f,color:#fff
    style GROQ_CALL fill:#e76f51,color:#fff
```

### How to explain this technically

> "The fallback chain is a resilience pattern. The primary path goes through our
> self-hosted LiteLLM proxy, which holds the real API keys. If the proxy is down,
> we try Google AI models in descending quality: Pro, Flash, Flash-Lite. Each
> step is cheaper and faster, so we gracefully degrade through rate limits and
> quota exhaustion. If all Google models fail, we try Groq as an external
> fallback. If everything fails, we still deliver the raw deterministic digest —
> the system never silently drops a scheduled briefing."

---

## 4. Task Scoring Algorithm

```mermaid
flowchart TD
    INPUT["Task from Todoist API"]

    INPUT --> DUE{"Due date?"}

    DUE -->|"Overdue"| OV["+min(days_past, 7) × 20<br/>Max +140 points"]
    DUE -->|"Today"| TD["+25"]
    DUE -->|"Tomorrow"| TM["+16"]
    DUE -->|"2 days"| D2["+8"]
    DUE -->|"3 days"| D3["+4"]
    DUE -->|"4+ days or none"| D_NONE["+0"]

    OV --> PRIO
    TD --> PRIO
    TM --> PRIO
    D2 --> PRIO
    D3 --> PRIO
    D_NONE --> PRIO

    PRIO{"Todoist priority?"}
    PRIO -->|"P1 (urgent)"| P1["+18"]
    PRIO -->|"P2 (high)"| P2["+10"]
    PRIO -->|"P3 (medium)"| P3["+4"]
    PRIO -->|"P4 (none)"| P4["+0"]

    P1 --> LBL
    P2 --> LBL
    P3 --> LBL
    P4 --> LBL

    LBL{"Labels?"}
    LBL -->|"@focus"| FOCUS["+14"]
    LBL -->|"@weekend<br/>(weekend mode only)"| WKND["+10"]
    LBL -->|"Neither"| NO_LBL["+0"]

    FOCUS --> DUR
    WKND --> DUR
    NO_LBL --> DUR

    DUR{"Duration?"}
    DUR -->|"≤ 30 min"| QUICK["+4 (quick-win bonus)"]
    DUR -->|"≥ 120 min"| LONG["−6 (complex penalty)"]
    DUR -->|"Other / unset"| MED["+0"]

    QUICK --> BACKLOG
    LONG --> BACKLOG
    MED --> BACKLOG

    BACKLOG{"No due date<br/>AND no @focus?"}
    BACKLOG -->|"Yes"| BL_PEN["−4 (backlog penalty)"]
    BACKLOG -->|"No"| FINAL

    BL_PEN --> FINAL["Final score<br/>Tiebreaker: due date → priority → alphabetical"]

    style INPUT fill:#264653,color:#fff
    style FINAL fill:#2d6a4f,color:#fff
    style OV fill:#e76f51,color:#fff
    style TD fill:#e9c46a,color:#000
```

### How to explain this technically

> "The scoring algorithm is parameter-free and deterministic — no machine
> learning, no configuration needed. Each task accumulates points from five
> independent factors: urgency (overdue compounds at 20 points per day, capped
> at 7 days), deadline proximity, user-set priority, label signals like @focus,
> and duration bonuses for quick wins. The design principle is that even without
> any AI, the system produces a usable priority ranking. AI only polishes the
> presentation."

---

## 5. Zero-Trust Proxy Security Model

```mermaid
flowchart LR
    subgraph CONTAINER["LXC Container (untrusted perimeter)"]
        direction TB
        ENV["/etc/brain/secrets.env<br/>LITELLM_PROXY_TOKEN=sk-budget-xxx<br/>TODOIST_API_TOKEN=...<br/>NOTION_TOKEN=..."]
        BRAIN["brain CLI process"]
        ENV -->|"read at startup"| BRAIN
    end

    subgraph FORTRESS["Your VPS (trusted perimeter)"]
        direction TB
        LITELLM["LiteLLM Proxy"]
        REAL_KEYS["Real API Keys<br/>GOOGLE_AI_API_KEY=...<br/>GROQ_API_KEY=..."]
        BUDGET["Budget Caps<br/>$20/month personal<br/>$2 hard cap demo"]
        AUDIT["Audit Trail<br/>per-request logging<br/>spend dashboard"]
        REAL_KEYS --> LITELLM
        BUDGET --> LITELLM
        AUDIT --> LITELLM
    end

    subgraph PROVIDERS["AI Providers"]
        GOOGLE["Google AI Studio"]
        GROQ["Groq Cloud"]
    end

    BRAIN -->|"HTTPS + proxy token<br/>(budget-capped)"| LITELLM
    LITELLM -->|"HTTPS + real key<br/>(never leaves VPS)"| GOOGLE
    LITELLM -->|"HTTPS + real key"| GROQ

    COMPROMISED["Attacker compromises LXC"]
    COMPROMISED -->|"steals proxy token"| BRAIN
    COMPROMISED -.->|"max damage: $2 (demo)<br/>or $20 (personal)"| LITELLM
    COMPROMISED -.->|"cannot access real keys"| FORTRESS

    style CONTAINER fill:#1b263b,color:#fff
    style FORTRESS fill:#2d6a4f,color:#fff
    style COMPROMISED fill:#e63946,color:#fff
    style BUDGET fill:#e9c46a,color:#000
```

### How to explain this technically

> "The core security insight is that secrets vaults don't solve the real problem.
> If a real API key enters a container's memory — even transiently via a vault
> fetch — it can be extracted via /proc, memory dump, or network interception.
> Instead, I keep real keys exclusively on a VPS I control, running a LiteLLM
> reverse proxy. The container gets only a budget-capped proxy token. If the
> token is stolen, the attacker can spend a few dollars before the budget
> self-destructs. Revocation is one curl call — no container redeployment
> needed."

---

## 6. Notion Two-Way Sync

```mermaid
sequenceDiagram
    autonumber
    participant Timer as brain-sync.timer<br/>Every 4h
    participant CLI as brain notion-sync
    participant Todoist as Todoist API
    participant Notion as Notion API

    Timer->>CLI: ExecStart

    rect rgb(40, 60, 90)
        Note over CLI,Notion: TODOIST → NOTION (forward sync)
        CLI->>Todoist: GET tasks in planner sections<br/>(Daily, Weekly, Weekend)
        Todoist-->>CLI: Task[]
        loop For each planner section
            CLI->>Notion: Query DB for existing pages
            Notion-->>CLI: Page[] with todoist_id property
            alt Task exists in Notion
                CLI->>Notion: PATCH page (update properties)
            else Task is new
                CLI->>Notion: POST page (create)
            end
        end
    end

    rect rgb(60, 40, 50)
        Note over CLI,Todoist: NOTION → TODOIST (reverse sync)
        CLI->>Notion: Query for Notion-only tasks<br/>(no todoist_id, not synced)
        Notion-->>CLI: Page[] created in Notion UI
        loop For each Notion-only task
            CLI->>Todoist: POST /tasks (create in correct section)
            Todoist-->>CLI: new Task with id
            CLI->>Notion: PATCH page (store todoist_id)
        end
    end

    Note over CLI: Log: X created, Y updated, Z errors
```

### How to explain this technically

> "The sync is idempotent and conflict-aware. Forward sync reads Todoist planner
> sections and upserts into Notion databases — existing pages are updated, new
> tasks are created. Reverse sync detects tasks created directly in Notion (they
> have no todoist_id) and pushes them back to Todoist, then stores the returned
> ID in Notion so the next sync recognises them. There's no local database — all
> state lives in Todoist and Notion, making the system stateless and safe to
> re-run."

---

## 7. systemd Scheduling and Hardening

```mermaid
flowchart TD
    subgraph TIMERS["systemd Timers (Persistent=true)"]
        T1["brain-daily.timer<br/>Mon-Fri 07:00"]
        T2["brain-sync.timer<br/>Every 4h<br/>(06,10,14,18,22)"]
        T3["brain-weekly.timer<br/>Sun 20:00"]
        T4["brain-weekend.timer<br/>Fri 17:00"]
        T5["brain-notebooklm.timer<br/>Sun 21:00"]
    end

    subgraph SERVICES["systemd Services (Type=oneshot)"]
        S1["brain-daily.service<br/>brain daily"]
        S2["brain-sync.service<br/>brain notion-sync --full"]
        S3["brain-weekly.service<br/>brain weekly"]
        S4["brain-weekend.service<br/>brain weekend"]
        S5["brain-notebooklm.service<br/>brain notebooklm-sync"]
    end

    subgraph HARDENING["Sandboxing Directives"]
        H1["User=brain<br/>(no login shell, no sudo)"]
        H2["NoNewPrivileges=true"]
        H3["PrivateTmp=true"]
        H4["PrivateDevices=true"]
        H5["ProtectSystem=strict"]
        H6["ProtectHome=true"]
        H7["MemoryDenyWriteExecute=true"]
        H8["CapabilityBoundingSet=<br/>(all capabilities dropped)"]
    end

    T1 --> S1
    T2 --> S2
    T3 --> S3
    T4 --> S4
    T5 --> S5

    S1 --> H1
    S2 --> H1
    S3 --> H1
    S4 --> H1
    S5 --> H1

    H1 --> H2 --> H3 --> H4 --> H5 --> H6 --> H7 --> H8

    MISSED["Container reboots or<br/>timer fires while offline"]
    MISSED -->|"Persistent=true<br/>reschedules immediately"| TIMERS

    style HARDENING fill:#264653,color:#fff
    style MISSED fill:#e9c46a,color:#000
```

### How to explain this technically

> "I chose systemd timers over cron for three reasons: structured logging via
> journalctl, dependency ordering between units, and kernel-level sandboxing.
> Every service unit runs as a dedicated brain user with zero capabilities,
> private /tmp, read-only filesystem, and memory write-execute prevention. The
> Persistent=true flag ensures that if the container is offline when a timer
> fires, the job runs immediately on next boot."

---

## 8. Deployment Topology

```mermaid
flowchart TB
    subgraph PROXMOX_HOST["Proxmox Host"]
        subgraph LXC200["LXC 200: VelaFlow"]
            direction TB
            OPT_BRAIN["/opt/brain<br/>Python venv + source<br/>(read-only via ProtectSystem)"]
            ETC_BRAIN["/etc/brain/secrets.env<br/>mode 0600, root-only"]
            VAR_LOG["/var/log/brain<br/>systemd journals"]
            SYSTEMD_UNITS["5 timer/service pairs"]
            DOCKER_NEST["Docker (nesting=1)<br/>n8n container (optional)"]
        end
    end

    subgraph YOUR_VPS["Your VPS"]
        LITELLM["LiteLLM Proxy<br/>Real AI keys (env vars, RAM only)<br/>Token management"]
    end

    subgraph PROVISIONING["Provisioning Scripts"]
        SETUP_LXC["setup-lxc.sh<br/>AppArmor, 9 caps dropped<br/>unprivileged, nesting"]
        INSTALL["install.sh<br/>Create user, venv, units, secrets"]
        SETUP_PROXY["setup-litellm-proxy.sh<br/>VPS proxy configuration"]
    end

    SETUP_LXC -->|"Run on Proxmox host"| LXC200
    INSTALL -->|"Run inside LXC"| OPT_BRAIN
    SETUP_PROXY -->|"Run on VPS"| LITELLM

    LXC200 -->|"HTTPS"| YOUR_VPS

    style LXC200 fill:#1b263b,color:#fff
    style YOUR_VPS fill:#2d6a4f,color:#fff
    style ETC_BRAIN fill:#e63946,color:#fff
```

---

## 9. CLI Command Map

```mermaid
flowchart LR
    BRAIN["python -m brain"]

    BRAIN --> DAILY["daily<br/>--stdout --no-llm"]
    BRAIN --> WEEKEND["weekend<br/>--stdout --no-llm"]
    BRAIN --> WEEKLY["weekly<br/>--stdout --no-llm"]
    BRAIN --> ALERTS["alerts<br/>--hours N --stdout"]
    BRAIN --> ANALYZE["analyze<br/>--stdout"]
    BRAIN --> ORGANIZE["organize<br/>--apply --no-move --no-label"]
    BRAIN --> NSETUP["notion-setup"]
    BRAIN --> NSYNC["notion-sync<br/>--full"]
    BRAIN --> NREBUILD["notion-rebuild"]
    BRAIN --> NLMSYNC["notebooklm-sync<br/>--no-rebuild --stdout"]

    DAILY -->|"Todoist + Calendar + Gmail<br/>→ Score → LLM → Email/WA/Notion"| OUT1["Email + WhatsApp + Notion"]
    WEEKEND -->|"Weekend tasks → capacity allocation<br/>→ LLM → Email/WA/Notion"| OUT2["Email + WhatsApp + Notion"]
    WEEKLY -->|"Active vs completed → velocity<br/>→ LLM coaching → Email/Notion"| OUT3["Email + Notion"]
    ALERTS -->|"Overdue tasks → WhatsApp"| OUT4["WhatsApp"]
    ANALYZE -->|"Board health → AI insights"| OUT5["Terminal / WhatsApp"]
    ORGANIZE -->|"Move tasks + auto-label"| OUT6["Todoist mutations"]
    NSETUP -->|"Create sections + dashboards"| OUT7["Todoist + Notion"]
    NSYNC -->|"Two-way sync planner DBs"| OUT8["Notion ↔ Todoist"]
    NREBUILD -->|"Rebuild Command Center layout"| OUT9["Notion"]
    NLMSYNC -->|"Notion pages → NotebookLM"| OUT10["NotebookLM"]

    style BRAIN fill:#264653,color:#fff
```

---

## 10. Failure Modes and Resilience

```mermaid
flowchart TD
    subgraph FAILURES["Possible Failures"]
        F1["LLM proxy down"]
        F2["Gemini rate-limited (429)"]
        F3["All AI providers fail"]
        F4["Todoist API down"]
        F5["Gmail IMAP error"]
        F6["WhatsApp/CallMeBot fails"]
        F7["Notion API error"]
        F8["NotebookLM cookies expired"]
        F9["Container offline during timer"]
    end

    subgraph RECOVERIES["Recovery Actions"]
        R1["Try Gemini Flash → Flash-Lite → Groq → raw text"]
        R2["Try next model in chain"]
        R3["Deliver raw deterministic digest (no AI)"]
        R4["Exit non-zero → systemd logs → retry next timer fire"]
        R5["Skip email section → digest still delivers"]
        R6["Email still sends independently"]
        R7["Log error → retry on next notion-sync run"]
        R8["Re-authenticate via VNC or push script"]
        R9["Persistent=true → reschedule on next boot"]
    end

    F1 --> R1
    F2 --> R2
    F3 --> R3
    F4 --> R4
    F5 --> R5
    F6 --> R6
    F7 --> R7
    F8 --> R8
    F9 --> R9

    style FAILURES fill:#e63946,color:#fff
    style RECOVERIES fill:#2d6a4f,color:#fff
```

