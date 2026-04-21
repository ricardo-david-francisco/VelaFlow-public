# Security Architecture: Zero-Trust Proxy Model

## The Golden Rule

When you hand someone a container — for a demo, a client deployment, or even your own
Proxmox LXC that you know could be compromised — you must treat that container as a
**fully hostile environment** from day one.

> **Never trust the client environment.**
> Never put a real API key inside any container you do not have sole physical control over.

Root access to a Proxmox host gives an attacker:

- Memory dumps (`/proc/<pid>/mem`) — extracts any secret that has ever been in RAM
- Filesystem inspection — reads any file, including encrypted volumes after mount
- Network interception — sees all outbound TLS traffic on the container network interface
- Container snapshots — clones the running state including in-memory secrets

**No vault, no encryption at rest, and no split-key technology prevents this.**
If the real API key ever enters the container's memory to make an outbound API call,
it can be stolen. This is why "fetch-to-RAM at startup" vault models are
fundamentally insufficient for containers you share.

---

## The Solution: Zero-Trust Proxy Model

The fix is architectural: **the real API key never enters the container at all**.
Instead of injecting a key, the container sends requests to a proxy you control.
The proxy attaches the real key and forwards the request to the AI provider.

```
┌─────────────────────────────────────────────────┐
│  LXC Container  (personal or demo — zero-trust) │
│                                                  │
│  brain CLI ──► LITELLM_PROXY_URL                 │
│                Authorization: Bearer sk-proxy-xx │
└──────────────────────┬──────────────────────────┘
                       │  HTTPS (budget-capped token)
                       ▼
┌─────────────────────────────────────────────────┐
│  Your VPS — The Fortress (you control this)     │
│                                                  │
│  LiteLLM Proxy                                   │
│    ├─ real GOOGLE_AI_API_KEY (env var, RAM only) │
│    ├─ rate limits, budget caps, audit log        │
│    └─ token management (issue / revoke)          │
└──────────────────────┬──────────────────────────┘
                       │  HTTPS (real key attached by proxy)
                       ▼
            Google AI / OpenAI / Anthropic
```

**What the LXC container holds:**

| Variable | Value | Can be stolen? | Worst case if stolen |
|----------|-------|----------------|----------------------|
| `LITELLM_PROXY_URL` | `https://your-proxy.example.com` | Yes | Attacker knows proxy URL |
| `LITELLM_PROXY_TOKEN` | `sk-proxy-xxxx` (budget-capped) | Yes | Attacker spends your token's budget cap, then it self-destructs |
| `TODOIST_API_TOKEN` | real Todoist token | Yes | Attacker can read/write your Todoist |
| `NOTION_API_TOKEN` | real Notion token | Yes | Attacker can read/write your Notion |
| Real AI key | **not present** | N/A | **Impossible to steal** |

The AI key is the only secret that protects your financial exposure. All others are
scoped to your personal data, not your billing account.

---

## Part 1 — Set Up Your LiteLLM Proxy (VPS)

### Requirements

- A VPS you control exclusively (DigitalOcean, Hetzner, AWS, Linode, etc.)
- Docker or a Python environment
- A domain or static IP

### Install LiteLLM

```bash
pip install litellm[proxy]

# Or with Docker:
docker run -d \
  --name litellm \
  -p 4000:4000 \
  -e GOOGLE_AI_API_KEY=$GOOGLE_AI_API_KEY \
  -e OPENAI_API_KEY=$OPENAI_API_KEY \
  -e LITELLM_MASTER_KEY=sk-master-changeme \
  ghcr.io/berriai/litellm:main-latest \
  --config /app/config.yaml
```

### Configure models (config.yaml on the VPS)

```yaml
model_list:
  - model_name: gemini/gemini-2.5-flash
    litellm_params:
      model: gemini/gemini-2.5-flash
      api_key: os.environ/GOOGLE_AI_API_KEY

  - model_name: gemini/gemini-2.5-pro
    litellm_params:
      model: gemini/gemini-2.5-pro
      api_key: os.environ/GOOGLE_AI_API_KEY

  - model_name: groq/llama-3.3-70b-versatile
    litellm_params:
      model: groq/llama-3.3-70b-versatile
      api_key: os.environ/GROQ_API_KEY

general_settings:
  master_key: sk-master-changeme    # change this
  store_model_in_db: true
  database_url: "sqlite:///./litellm.db"
```

Your real API keys go into the VPS environment (e.g. systemd `EnvironmentFile` or
Docker `-e` flags). They **never leave your VPS**.

---

## Part 2 — Issue Proxy Tokens

Tokens are generated via the LiteLLM dashboard (`http://your-vps:4000/ui`) or API.
You issue a different token per use case with different constraints.

### Personal-use token

```bash
curl -X POST https://your-proxy.example.com/key/generate \
  -H "Authorization: Bearer sk-master-changeme" \
  -H "Content-Type: application/json" \
  -d '{
    "key_alias": "brain-personal",
    "max_budget": 20.0,
    "budget_duration": "1mo",
    "rpm_limit": 60,
    "models": ["gemini/gemini-2.5-flash", "gemini/gemini-2.5-pro"],
    "metadata": {"use_case": "personal-lxc"}
  }'
```

### Demo token

```bash
curl -X POST https://your-proxy.example.com/key/generate \
  -H "Authorization: Bearer sk-master-changeme" \
  -H "Content-Type: application/json" \
  -d '{
    "key_alias": "demo-client-acme-2026-04",
    "max_budget": 1.0,
    "budget_duration": "48h",
    "rpm_limit": 5,
    "models": ["gemini/gemini-2.5-flash"],
    "allowed_ips": ["203.0.113.42"],
    "metadata": {"recipient": "Acme Corp", "expires": "2026-04-18"}
  }'
```

**Token constraints:**

| Constraint | Personal | Demo |
|------------|----------|------|
| Budget cap | $20 / month | $1 hard cap |
| Time expiry | Rolling monthly reset | 48 hours |
| Rate limit | 60 req/min | 5 req/min |
| IP lock | Optional | Recommended |
| Models | Full set | Flash only |

### Revoke a token instantly

```bash
curl -X POST https://your-proxy.example.com/key/delete \
  -H "Authorization: Bearer sk-master-changeme" \
  -H "Content-Type: application/json" \
  -d '{"keys": ["sk-proxy-xxxxxxxxxxxxxxxxxxxxxxxx"]}'
```

One request. Token is dead across all containers using it.

---

## Part 3 — Configure the LXC Container

Set these in `/etc/brain/secrets.env` (installed by `scripts/install.sh`):

```ini
# AI routing — proxy only, real keys stay on the VPS
LITELLM_PROXY_URL=https://your-proxy.example.com
LITELLM_PROXY_TOKEN=sk-proxy-xxxxxxxxxxxxxxxxxxxxxxxx
LITELLM_PROXY_MODEL=gemini/gemini-2.5-flash

# Demo container: add these two lines
DEMO_MODE=true
BRAIN_READ_ONLY=true

# All other required secrets (Todoist, Notion, SMTP, etc.)
TODOIST_API_TOKEN=...
NOTION_API_TOKEN=...
```

**Do NOT set `GOOGLE_AI_API_KEY` or `GROQ_API_KEY`** in any container.
When `DEMO_MODE=true`, the application enforces proxy-only — direct key fallback is
disabled in code (`llm.py`), not just absent from the config.

---

## Part 4 — Why This Is Immune to Memory Attacks

| Attack vector | Traditional vault / secrets manager | Zero-Trust Proxy |
|---------------|--------------------------------------|------------------|
| Memory dump | **Vulnerable** — real key in RAM after fetch | **Safe** — real key never enters container |
| Filesystem inspection | Vulnerable after fetch | Safe |
| Network sniffing on container NIC | Vulnerable — key in Authorization header to AI provider | Safe — only proxy token visible |
| Container snapshot / theft | Vulnerable | Only proxy token stolen (budget-capped, revocable in seconds) |
| Proxy token stolen and reused | N/A | Worst case: attacker spends budget cap, then token dies |

The fundamental difference: a vault moves the secret into the container's memory
(just-in-time). The proxy model means **the secret travels on the wire only between
your VPS and the AI provider** — a connection you control on both ends.

---

## Part 5 — Audit and Monitoring

All requests flow through your proxy, giving you:

- **Real-time spend dashboard** at `http://your-vps:4000/ui`
- **Per-token audit log** — see exactly what each container requested
- **Anomaly detection** — unusually high request volume triggers a budget block
- **Instant revocation** — one API call kills a token globally

If you see abuse, you revoke the token. The attacker's container stops working within
seconds. Your real keys are never exposed.

---

## Part 6 — Secret Rotation Playbook

### Rotate the LiteLLM proxy token

```bash
# 1. Issue a new token on your VPS
curl -X POST https://your-proxy.example.com/key/generate \
  -H "Authorization: Bearer sk-master-changeme" \
  -H "Content-Type: application/json" \
  -d '{"key_alias": "brain-personal-$(date +%Y%m)", "max_budget": 20.0,
       "budget_duration": "1mo", "rpm_limit": 60,
       "models": ["gemini/gemini-2.5-flash", "gemini/gemini-2.5-pro"]}'

# 2. Update in the LXC (single sed command, no restart needed at next trigger)
sed -i 's|^LITELLM_PROXY_TOKEN=.*|LITELLM_PROXY_TOKEN=sk-proxy-new-value|' \
  /etc/brain/secrets.env

# 3. Optionally revoke the old token immediately
curl -X POST https://your-proxy.example.com/key/delete \
  -H "Authorization: Bearer sk-master-changeme" \
  -H "Content-Type: application/json" \
  -d '{"keys": ["sk-proxy-old-value"]}'
```

### Rotate a data service token (Todoist, Notion, Gmail)

```bash
# Edit one line — no package restarts required, takes effect on next execution
sed -i 's|^TODOIST_API_TOKEN=.*|TODOIST_API_TOKEN=new_token_here|' \
  /etc/brain/secrets.env
```

### Renew NotebookLM browser cookies (every 2–4 weeks)

```bash
# Option A — from inside the LXC (VNC-based)
pct exec 200 -- bash /opt/brain/scripts/notebooklm-lxc-login.sh

# Option B — push from Windows desktop (already logged into NotebookLM)
.\scripts\notebooklm-push-auth.ps1 -ProxmoxHost <your-proxmox-host-ip>
```

### Renew Google Calendar OAuth token (~annually)

```bash
# Trigger the OAuth flow interactively
sudo -u brain /opt/brain/venv/bin/brain daily --stdout
# Follow the printed URL, approve, paste the code. Token file is updated automatically.
```

### Grant a demo user access (no LXC changes needed)

```bash
# Issue a hard-capped token on your VPS:
curl -X POST https://your-proxy.example.com/key/generate \
  -H "Authorization: Bearer sk-master-changeme" \
  -H "Content-Type: application/json" \
  -d '{
    "key_alias": "demo-$(date +%Y%m%d)",
    "max_budget": 2.0,
    "budget_duration": "48h",
    "rpm_limit": 5,
    "models": ["gemini/gemini-2.5-flash"],
    "metadata": {"recipient": "demo user"}
  }'

# Set in the demo container:
sed -i 's|^LITELLM_PROXY_TOKEN=.*|LITELLM_PROXY_TOKEN=sk-proxy-demo-token|' \
  /etc/brain/secrets.env

# Optionally lock the container to read-only mode:
echo "DEMO_MODE=true"       >> /etc/brain/secrets.env
echo "BRAIN_READ_ONLY=true" >> /etc/brain/secrets.env
```

### Revoke all access instantly

```bash
# One command on your VPS — all containers using that token lose AI access immediately
curl -X POST https://your-proxy.example.com/key/delete \
  -H "Authorization: Bearer sk-master-changeme" \
  -H "Content-Type: application/json" \
  -d '{"keys": ["sk-proxy-target-token"]}'
```

---

## Summary

| Property | Traditional vault model | **Zero-Trust Proxy (this project)** |
|----------|-------------------------|--------------------------------------|
| Real key enters container RAM | Yes (at startup) | **No** |
| Revocable on compromise | Yes (revoke access key) | **Yes (revoke proxy token)** |
| Cost exposure on token theft | Full API budget | **$1-2 hard cap** |
| Setup complexity | High (vault account, policies, paths) | **Low (LiteLLM + one curl command)** |
| Audit trail | Limited | **Full per-request log** |
| Memory dump attack | **Vulnerable** | **Safe** |
| Expand access for a new user | Reprovision or grant vault role | **Issue new token, one curl call** |
| Remove access | Revoke vault binding | **Delete token, one curl call** |

---

## Encrypted Audit Logging (Round 10-12)

All platform actions are recorded in encrypted, tamper-evident audit logs:

### Architecture

```
┌────────────────────────────────────────────┐
│  AuditEntry                                │
│  ┌─ timestamp                              │
│  ├─ tenant_id                              │
│  ├─ user_id                                │
│  ├─ action (login, pipeline_run, etc.)     │
│  ├─ resource                               │
│  ├─ detail (JSON)                          │
│  ├─ previous_hash ── links to prior entry  │
│  └─ chain_hash ── SHA-256(payload|prev)    │
└────────────────────────────────────────────┘
         │
         ▼  AES-256-GCM encrypt
┌─────────────────────┐
│  Encrypted blob      │──► storage
│  (per-tenant key)    │    tenants/{id}/audit/{YYYY-MM}.log
└─────────────────────┘
```

### Security Properties

- **At-rest encryption**: AES-256-GCM with per-tenant derived keys (PBKDF2-HMAC-SHA256)
- **Tamper detection**: HMAC chain — each entry includes hash of previous entry
- **Chain verification**: `verify_chain()` detects modification, deletion, or reordering
- **LXC attacker defense**: Root access to container yields only encrypted blobs — no readable audit data without master key
- **Monthly rotation**: Logs segmented by month for manageable verification and archival

### Demo Account Audit

Demo accounts have an additional encrypted event trail (`demo_events.jsonl`) that records:
- Account creation, expiry, cost cap hits
- All errors forwarded to admin
- Usage analytics aggregation

All demo events encrypted with the same per-tenant key derivation, ensuring isolation.

---

## Secure Structured Logging

`brain.security.secure_logging` provides application-wide logging with automatic PII redaction and tamper evidence:

### Redaction Patterns (7 categories)

| Pattern | Example | Replacement |
|---------|---------|-------------|
| API tokens | `sk-abc123...`, `xoxb-...`, Bearer tokens | `[REDACTED_TOKEN]` |
| Email addresses | `user@example.com` | `[REDACTED_EMAIL]` |
| Phone numbers | `+1-555-0123`, `(555) 012-3456` | `[REDACTED_PHONE]` |
| IPv4 addresses | `192.168.1.100` | `[REDACTED_IP]` |
| SSN | `123-45-6789` | `[REDACTED_SSN]` |
| Credit cards | `4111-1111-1111-1111` | `[REDACTED_CC]` |
| JWT tokens | `eyJhbGci...` | `[REDACTED_JWT]` |

### HMAC Chain

Each log record includes a chain hash: `[chain:ABCDEF1234567890...]`. The hash is computed as:

```
HMAC-SHA256(key, previous_hash + "|" + formatted_message)[:32]
```

Deleting or modifying any record breaks the chain. The HMAC key is derived from `VELAFLOW_MASTER_KEY` (or a persisted random key if no master key is set).

### Sanitised Export

`SecureLogger.export_sanitised()` double-redacts logs and outputs Markdown safe for sharing with GitHub Copilot or other debugging tools:

```bash
python scripts/installer.py --export-logs
```

### Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `LOG_LEVEL` | `INFO` | Minimum log level |
| `LOG_DIR` | `logs` | Log directory path |
| `LOG_MAX_SIZE_MB` | `50` | Max log file size before rotation |
| `LOG_RETENTION_DAYS` | `30` | Log retention period |
