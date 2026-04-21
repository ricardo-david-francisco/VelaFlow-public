# Deployment Guide

Step-by-step instructions to deploy VelaFlow on a Proxmox LXC container or Ubuntu LTS VM.

---

## Frequently Asked Questions (pre-flight)

> **Q: Is VelaFlow deploy-ready today so I can start building the HTML frontend?**
> **A: Yes.** The platform ships an authenticated FastAPI service (`brain.api.app:app`,
> port 8765), tier-gated Streamlit GUI, preflight validator (`scripts/preflight.py`),
> observability stack (`deploy/observability/`), encrypted Google Drive backups
> (`scripts/drive_backup.py`), and an HMAC-chained action ledger. 480 / 480 tests
> pass; Snyk Code reports zero MEDIUM+ findings with zero `.snyk` ignores. The
> per-user graphical fine-tuned workflow editor is the v1.2 headline feature
> and is not required to start HTML frontend work — the REST API contract is
> stable.

> **Q: What stops a hacker who is already inside the LXC?**
> **A: A layered sanitization chain, not a single control.**
>
> - Every filesystem-touching function routes untrusted input through
>   `src/brain/security/safe_path.py::safe_resolve`, which rejects paths
>   outside an allow-list of bases (`VELAFLOW_DATA_DIR`, process HOME,
>   cwd, `/var/log/brain` on POSIX, `%PROGRAMDATA%\brain` on Windows).
> - Immediately before any sink, the resolved path is re-validated inline
>   with `Path.resolve().relative_to(base)` *in the same function*, so a
>   TOCTOU (symlink-between-check-and-use) attempt hits the sanitizer a
>   second time. Writes that escape raise `UnsafePathError` and emit an
>   action-ledger entry.
> - File-mode changes use `os.fchmod(fd, …)` on the already-open
>   descriptor or `os.open(mode=0o600)` at creation — no `chmod(path, …)`
>   call takes attacker-controlled input.
> - Tar restore uses `tar.extractfile()` + `shutil.copyfileobj` into an
>   inline-validated path; `tar.extract(path=)` is not used.
> - Data at rest is AES-256-GCM per-tenant (PBKDF2 key derivation from
>   `VELAFLOW_MASTER_KEY`), so read access inside the LXC does not yield
>   cleartext secrets.
> - systemd service units apply `NoNewPrivileges=yes`,
>   `ProtectSystem=strict`, `ProtectHome=read-only`,
>   `CapabilityBoundingSet=~CAP_SYS_ADMIN` and a read-only rootfs on
>   observability containers. From Round 19 every `brain-*.service` also
>   sets `LimitCORE=0` (no core dumps), `LimitMEMLOCK=infinity` (so
>   `mlockall` can pin decrypted credential pages in RAM),
>   `LockPersonality=true`, `RestrictRealtime=true`, `ProtectClock=true`
>   and `ProtectHostname=true`. The API and worker call `mlockall` at
>   startup (`src/brain/security/memlock.py`).
> - Observability endpoints (Prometheus, Grafana) bind to loopback only;
>   external scrapes must go through a reverse-proxy with auth.
> - The action ledger is HMAC-chained: any tamper inside the LXC breaks
>   the hash chain and is detectable offline.
>
> Snyk Code at `--severity-threshold=medium` reports 0 findings against
> this stack.

> **Q: Why are Streamlit, n8n, and Redis described as "provisional"?**
> **A: Because the v1.2 headline is a per-user graphical workflow editor,
> not any of those three.**
>
> - **Streamlit** is a tier-gating placeholder surface in v1.0; the v1.2
>   drag-and-drop editor replaces it.
> - **n8n Community Edition** is a provisional operator-workflow surface
>   (Apache-2.0 fair-code, self-hosted, €0). The platform pipelines run
>   without it; n8n is convenient, not a product dependency.
> - **Redis** is one of several swappable queue backends. The default
>   single-node deployment uses the in-process queue; Redis is selected
>   only when scaling past one worker process.

> **Q: Google Drive backup auth method?**
> **A: Service account + shared folder.** Create a service account in your GCP
> project, share a dedicated `VelaFlow-Backups` folder with its email. No OAuth
> consent screen, no 7-day refresh-token expiry, safest for unattended backups.
> See `scripts/drive_backup.py` and the `backup` optional extra in
> `pyproject.toml`.

> **Q: Does this cost me anything to host for 1,000 users?**
> **A: No.** The entire default stack — FastAPI, in-process or Redis queue, DuckDB,
> SQLite catalog, n8n Community Edition, Ollama, Prometheus, Grafana — is
> self-hostable at zero marginal cost. Premium tenants pay you; you pay nothing
> to the platform. The only cost is whatever VPS you run it on. Oracle Cloud
> Always-Free ARM A1 Flex (4 OCPU / 24 GB RAM) is validated to host the
> 1,000-user burst profile in `tests/test_stress.py::test_burst_1000_users`.

> **Q: Will I be locked into n8n Cloud pricing as I grow?**
> **A: No.** VelaFlow uses **n8n Community Edition** (Apache-2.0-style fair-code),
> which is free forever for self-hosting with unlimited users and workflows.
> The paid *n8n Cloud* SaaS is not used and not required.

> **Q: Will my production deploy today surface bugs only at runtime?**
> **A: No — run the preflight validator first** (see below). It verifies the
> Python version, every required import, every required secret (with format
> validation for the master key and JWT secret), data-dir writability, port
> availability, and config file presence BEFORE the API starts.

---

## Pre-flight validation (run this before every deploy)

```bash
# From the repo root, with your .env sourced:
python scripts/preflight.py

# Machine-readable output for CI / systemd ExecStartPre:
python scripts/preflight.py --json
```

Exit codes:

- `0` — all checks passed (zero blocking, zero warnings)
- `1` — one or more blocking checks failed — **do not start the service**
- `2` — all blocking checks passed, but one or more warnings were raised
  (e.g. optional backup extra missing). Safe to start, review the warnings.

Wire it into systemd:

```ini
[Service]
ExecStartPre=/usr/bin/python /opt/brain/scripts/preflight.py
ExecStart=/usr/bin/uvicorn brain.api.app:app --host 127.0.0.1 --port 8765
```

---

## Local observability demo stack (developer workstation)

VelaFlow exposes Prometheus metrics on `/metrics` out of the box. A **zero-cost,
hardened** local Prometheus + Grafana stack is provided under
`deploy/observability/` — intended for local development and operator
walkthroughs, not for the production data path.

```bash
# 1) Start the API so metrics are available:
uvicorn brain.api.app:app --host 127.0.0.1 --port 8765

# 2) Start Prometheus + Grafana (containers pinned, read-only rootfs, cap_drop ALL):
docker compose -f deploy/observability/docker-compose.yml up -d

# 3) Open the pre-provisioned dashboard:
#    - Grafana:    http://127.0.0.1:3000   (admin / admin)
#    - Prometheus: http://127.0.0.1:9090
#    - Dashboard:  http://127.0.0.1:3000/d/velaflow-main
```

The `velaflow-main` dashboard has 11 panels: API up/uptime, active tenants,
queue depth, worker count, pipeline runs, LLM calls, HTTP error rate, task
throughput. Refresh interval is 5 s. All ports are bound to 127.0.0.1 only.

Tear down when done: `docker compose -f deploy/observability/docker-compose.yml down -v`.

---

## Tenant self-service GUI (v1.0)

A tier-gated Streamlit self-service surface ships in v1.0 (a full n8n-style
drag-and-drop editor is tracked for v1.2):

```bash
pip install 'velaflow[gui]'
export VELAFLOW_API_URL=http://localhost:8765
streamlit run src/brain/gui/app.py
# Open http://localhost:8501 — paste a JWT from POST /api/v1/tenants/login
```

The API remains the authoritative gate — the GUI simply disables controls
the tenant's tier cannot write. Disallowed PATCH bodies still return
403 with an `upgrade_path` field.

---

## Security-first design: secrets at rest are never real keys

The container holds a **budget-capped LiteLLM proxy token**, not real AI keys.
If the container is compromised, the attacker can spend a few dollars at most before
the token expires — then revoke it in one curl call and issue a new one. Real API keys
never leave your VPS. See [docs/security.md](security.md) for the full model.

All non-AI secrets (`TODOIST_API_TOKEN`, `NOTION_API_TOKEN`, etc.) sit in
`/etc/brain/secrets.env` (permissions `0600`, owned by `root:root`). They are
read from the filesystem once at process start — they are never written to logs
and never transmitted to the proxy. Rotating any secret is one command.

---

## Deployment Modes

| Mode | Command | Use case |
|------|---------|---------|
| Standard | `bash scripts/install.sh` | Personal Proxmox LXC — proxy token only, no real AI keys |
| Dev | `bash scripts/install.sh --dev` | Ubuntu VM / local testing — direct AI keys permitted |

---

## Prerequisites

Before you start, gather these credentials. All can be obtained for free.

| Credential | Where to get it | Required? |
|------------|----------------|-----------|
| Todoist API token | [todoist.com/prefs/integrations](https://todoist.com/prefs/integrations) | Yes |
| Notion integration token | [notion.so/profile/integrations](https://www.notion.so/profile/integrations) | Yes |
| Notion root page ID | Share your 2nd-Brain page to the integration; copy the 32-char ID from the URL | Yes |
| Gmail App Password | [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) (2FA required) | Yes |
| LiteLLM proxy URL + token | Your own VPS — see [docs/security.md](security.md) Part 1 | Yes (Standard mode) |
| Google AI Studio key | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | Dev mode only |
| CallMeBot API key | [callmebot.com](https://www.callmebot.com/blog/free-api-whatsapp-messages/) | No (WhatsApp is optional) |
| Google Calendar credentials | Google Cloud Console OAuth2 | No (optional) |

> **Dev mode only:** direct AI keys are permitted in `secrets.env` as a fallback.
> Never set `GOOGLE_AI_API_KEY` or `GROQ_API_KEY` in an LXC you share with others.

---

## Step 1 — Create the LXC Container (Proxmox)

SSH into your Proxmox host and create the container via the UI or the following CLI template:

```
Template:  debian-12-standard
CTID:      200
Hostname:  velaflow
RAM:       1024 MB (512 MB minimum; 2 GB if running n8n alongside)
Cores:     2
Disk:      10 GB
Network:   DHCP (note the assigned IP after boot)
Features:  nesting=1, keyctl=1
```

> **nesting=1** is required for Docker (n8n optional).
> **keyctl=1** is required for correct systemd operation inside the container.

Enter the container:

```bash
pct enter 200
```

---

## Step 2 — Run the Installer

```bash
# Install git and clone the repo
apt-get update && apt-get install -y git curl
git clone https://github.com/<your-username>/VelaFlow.git /opt/brain
cd /opt/brain

# Standard installation (proxy model — recommended for LXC)
bash scripts/install.sh

# Developer mode (direct AI keys — local Ubuntu VM testing only)
bash scripts/install.sh --dev
```

The installer does the following automatically:

1. Creates a `brain` system user (no login shell, no sudo, no home directory)
2. Installs Python 3.11+, pip, git, and all system-level dependencies
3. Creates a Python venv at `/opt/brain/venv` and installs the `brain` package
4. Installs `notebooklm-py[browser]` and Playwright Chromium
5. Creates directory structure: `/etc/brain/` (secrets), `/var/log/brain/`, `/opt/brain/.notebooklm/` (cookies, `chmod 700`)
6. Installs all systemd service and timer units. Enables 4 core timers automatically.
7. Prints a post-install summary box showing what still requires manual action.

> **Ubuntu VM (dev mode):** `install.sh --dev` works identically on Ubuntu 22.04/24.04.
> Direct AI keys are permitted in dev mode. Do not use dev mode on a shared LXC.

---

## Step 3 — Configure Secrets

Edit the secrets file created by the installer:

```bash
nano /etc/brain/secrets.env
```

Minimum required for standard (proxy) mode:

```ini
# === AI routing — proxy only, real keys stay on your VPS ===
LITELLM_PROXY_URL=https://your-litellm-proxy.example.com
LITELLM_PROXY_TOKEN=sk-proxy-xxxxxxxxxxxxxxxxxxxxxxxx
LITELLM_PROXY_MODEL=gemini/gemini-2.5-flash

# === Data integrations ===
TODOIST_API_TOKEN=your_todoist_token
NOTION_API_TOKEN=your_notion_token
NOTION_ROOT_PAGE_ID=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# === Email delivery ===
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=you@gmail.com
SMTP_PASSWORD=your_gmail_app_password
DIGEST_FROM_EMAIL=you@gmail.com
DIGEST_TO_EMAIL=you@gmail.com
```

Optional additions:

```ini
# WhatsApp (requires one-time CallMeBot registration — see callmebot.com)
CALLMEBOT_PHONE=+351xxxxxxxxx
CALLMEBOT_API_KEY=your_callmebot_key

# Google Calendar OAuth2 (see Step 9)
GOOGLE_OAUTH_CLIENT_SECRETS_FILE=/etc/brain/google-credentials.json
GOOGLE_OAUTH_TOKEN_FILE=/etc/brain/google-token.json
```

File permissions are set correctly by the installer. Verify:

```bash
stat /etc/brain/secrets.env
# Expected: File: /etc/brain/secrets.env  Access: 0600  Uid: 0 (root)
```

### Rotating a secret (zero downtime)

```bash
# Edit a single value in-place
sed -i 's|^TODOIST_API_TOKEN=.*|TODOIST_API_TOKEN=new_token_here|' /etc/brain/secrets.env

# Or open the full file
nano /etc/brain/secrets.env

# Restart only the unit that uses the changed secret
systemctl restart brain-daily.service
# Timers fire fresh child processes — no restart needed for timer-level changes
```

### Expanding AI budget or access (without touching the LXC)

All AI budget, model access, and rate limits are controlled on your VPS via LiteLLM.
No changes to the LXC are needed:

1. Open `http://your-vps:4000/ui`
2. Navigate to **Keys** and find `brain-personal`
3. Edit `max_budget`, `rpm_limit`, or `models` — changes apply immediately

To hand off the container to someone else, issue a **new hard-capped token** from the
proxy dashboard and update only `LITELLM_PROXY_TOKEN` in the LXC:

```bash
# Issue a $2-capped, 48-hour token on your VPS:
curl -X POST https://your-proxy.example.com/key/generate \
  -H "Authorization: Bearer sk-master-changeme" \
  -H "Content-Type: application/json" \
  -d '{"key_alias": "handover", "max_budget": 2.0, "budget_duration": "48h",
       "rpm_limit": 5, "models": ["gemini/gemini-2.5-flash"]}'

# Apply in the LXC:
sed -i 's|^LITELLM_PROXY_TOKEN=.*|LITELLM_PROXY_TOKEN=sk-proxy-new-token|' \
  /etc/brain/secrets.env
systemctl restart brain-daily.service
```

Revocation is one command and takes effect instantly across all containers using that token.

---

## Step 4 — First-time Notion Setup

```bash
source /opt/brain/venv/bin/activate

# Creates Todoist planner sections + full Notion dashboard (idempotent)
python -m brain notion-setup
```

Copy the printed IDs into `/etc/brain/secrets.env`:

```ini
NOTION_COMMAND_CENTER_ID=...
NOTION_DAILY_PLANNER_DB_ID=...
NOTION_WEEKLY_PLANNER_DB_ID=...
NOTION_WEEKEND_PLANNER_DB_ID=...
NOTION_BOARD_DB_ID=...
TODOIST_DAILY_PLANNER_SECTION_ID=...
TODOIST_WEEKLY_PLANNER_SECTION_ID=...
TODOIST_WEEKEND_PLANNER_SECTION_ID=...
```

---

## Step 5 — Verify the Installation

```bash
source /opt/brain/venv/bin/activate

# Preview daily digest — no emails, no side effects
python -m brain daily --stdout --no-llm

# Confirm AI proxy connection
python -m brain daily --stdout

# Confirm Notion sync
python -m brain notion-sync

# Check timers
systemctl list-timers brain-*
```

Expected: formatted daily briefing, 4 active timers (`brain-daily`, `brain-weekly`,
`brain-weekend`, `brain-sync`). `brain-notebooklm.timer` should appear as inactive
(not yet enabled — that comes after Step 6).

---

## Step 6 — NotebookLM Authentication in LXC

NotebookLM requires a one-time Google browser login to generate session cookies.
LXC containers are headless (no display), so this cannot run interactively by
default. Two methods are provided — **use whichever suits your setup.**

---

### Method A — VNC login directly in LXC (recommended for fresh installs)

This method runs entirely inside the LXC. A virtual display (Xvfb) is started
and a VNC server exposes it so you can see and interact with the Chromium
browser from your desktop.

**The `install.sh` already installed everything needed (Xvfb, x11vnc, Chromium).**

```bash
# From Proxmox host — run inside LXC 200
pct exec 200 -- bash /opt/brain/scripts/notebooklm-lxc-login.sh
```

The script will print:
```
  Connect VNC from your desktop to:  <your-proxmox-host-ip>:5900
```

Open any VNC viewer (RealVNC, TigerVNC, Remmina) and connect. The Chromium
browser will open showing the Google login page. Sign in with your Google
account, wait for the NotebookLM homepage to load, then press **Enter** in
the terminal where the script is running.

Cookies are saved to `/opt/brain/.notebooklm/storage_state.json` inside the LXC.

---

### Method B — Push cookies from Windows (if already authenticated on desktop)

Since you already ran `notebooklm login` on this Windows machine, the
simplest path is pushing the existing cookies from Windows to the LXC in
one command. No VNC needed.

```powershell
# From Windows PowerShell — requires SSH key auth to your Proxmox host
.\scripts\notebooklm-push-auth.ps1 -ProxmoxHost <your-proxmox-host-ip>

# Or with custom container ID / user
.\scripts\notebooklm-push-auth.ps1 -ProxmoxHost <your-proxmox-host-ip> -CTID <your-ctid> -ProxmoxUser <proxmox-user>
```

The script reads `%USERPROFILE%\.notebooklm\storage_state.json`, base64-encodes
it, SSHes into Proxmox and writes it into the LXC container with `pct exec`.

---

### After authentication (both methods)

```bash
# Verify cookies work
pct exec 200 -- sudo -u brain \
  NOTEBOOKLM_HOME=/opt/brain/.notebooklm \
  /opt/brain/venv/bin/python -c "
import asyncio
from notebooklm import NotebookLMClient
async def check():
    async with await NotebookLMClient.from_storage() as c:
        nbs = await c.notebooks.list()
        print(f'Auth OK — {len(nbs)} notebooks.')
asyncio.run(check())
"

# Run first sync (creates the notebook on first run)
pct exec 200 -- sudo -u brain \
  NOTEBOOKLM_HOME=/opt/brain/.notebooklm \
  /opt/brain/venv/bin/brain notebooklm-sync --stdout
```

Copy the printed `NOTEBOOKLM_NOTEBOOK_ID=...` into `/etc/brain/secrets.env`:

```ini
NOTEBOOKLM_HOME=/opt/brain/.notebooklm
NOTEBOOKLM_NOTEBOOK_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
NOTEBOOKLM_NOTEBOOK_NAME=VelaFlow
```

Then enable the weekly timer:

```bash
pct exec 200 -- systemctl enable --now brain-notebooklm.timer
```

---

### Cookie renewal (every 2–4 weeks)

Google session cookies expire. Re-authenticate using the same method you used
initially:

- **Method A:** Re-run `notebooklm-lxc-login.sh` in the LXC.
- **Method B:** Re-run `notebooklm login` on Windows, then re-run `notebooklm-push-auth.ps1`.

No reconfiguration is needed — the next scheduled sync picks up the new cookie file automatically.

---

## Step 7 — systemd Timer Schedule

| Timer | Fires | Command | Auto-enabled |
|-------|-------|--------|-------------|
| `brain-daily.timer` | Mon-Fri 07:00 | `brain daily` | Yes |
| `brain-weekly.timer` | Sunday 20:00 | `brain weekly` | Yes |
| `brain-weekend.timer` | Friday 17:00 | `brain weekend` | Yes |
| `brain-sync.timer` | 06/10/14/18/22:00 | `brain notion-sync --full` | Yes |
| `brain-notebooklm.timer` | Sunday 21:00 | `brain notebooklm-sync` | **No — enable after Step 6** |

```bash
# All 4 core timers are enabled by install.sh. Verify:
systemctl list-timers brain-*

# Enable NotebookLM timer after completing Step 6:
systemctl enable --now brain-notebooklm.timer

# Live log tailing:
journalctl -u brain-daily.service -f
journalctl -u brain-notebooklm.service --since today

# Manual trigger (useful for testing without waiting for the timer):
systemctl start brain-daily.service
```

---

## Step 8 — Enterprise Platform (Docker Compose)

When deploying the multi-tenant API with Docker Compose, the following environment
variables are **mandatory** — `docker-compose.yml` will refuse to start without them:

| Variable | Purpose | Generate |
|---|---|---|
| `JWT_SECRET` | JWT token signing (64+ chars recommended) | `python -c 'import secrets; print(secrets.token_urlsafe(64))'` |
| `VELAFLOW_MASTER_KEY` | AES-256-GCM field encryption master key (base64) | `python -c 'import secrets,base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'` |
| `REDIS_PASSWORD` | Redis authentication | `python -c 'import secrets; print(secrets.token_urlsafe(32))'` |
| `N8N_ENCRYPTION_KEY` | n8n credential encryption | `openssl rand -hex 32` |
| `OAUTH2_PROXY_EMAIL_DOMAINS` | Allowed Google login domains (no default — **must be set explicitly**) | `example.com` or `example.com,company.org` |
| `ENVIRONMENT` | Set to `production` to disable Swagger docs | `production` |

Create a `.env` file in the project root:

```ini
JWT_SECRET=<generated value>
VELAFLOW_MASTER_KEY=<generated value>
REDIS_PASSWORD=<generated value>
N8N_ENCRYPTION_KEY=<generated value>
OAUTH2_PROXY_EMAIL_DOMAINS=yourdomain.com
ENVIRONMENT=production
```

**Security notes:**
- `OAUTH2_PROXY_EMAIL_DOMAINS` has no default — Docker Compose will refuse to start without it. **Never set this to `*`** (allows any Google account).
- n8n is bound to `127.0.0.1:5678` — not accessible from outside the host
- Redis requires authentication — no unauthenticated access is possible
- Swagger/OpenAPI docs are disabled when `ENVIRONMENT=production`
- All API responses include security headers (HSTS, CSP, X-Frame-Options, etc.)
- Login is rate-limited to 10 attempts per 5 minutes per IP
- Registration is rate-limited to 5 requests per 5 minutes per IP

```bash
# Start all services
docker compose up -d

# Verify
curl http://localhost:8000/health
```

---

## Step 9 — n8n (Optional Alternative Scheduler)

n8n can replace systemd timers in environments without systemd (e.g., non-LXC VMs,
Docker-only setups). The LXC deployment uses systemd by default — n8n is only needed
if you want a visual workflow editor or additional automation nodes.

```bash
# Start n8n (docker-compose.yml is already in the repo root)
docker compose up -d

# Open UI
# http://<container-ip>:5678
```

1. Create an account on first access.
2. Import each workflow JSON from `workflows/`:
   - `daily-briefing.json`
   - `overdue-alert.json`
   - `weekend-planner.json`
   - `weekly-review.json`
3. In each workflow, verify the Execute Command path points to `/opt/brain/venv/bin/brain`.
4. Activate each workflow.

---

## Step 10 — Google Calendar (Optional)

```bash
# Install the OAuth2 libraries (already in requirements if using extras)
pip install google-api-python-client google-auth-oauthlib

# Place the credentials.json from Google Cloud Console
cp /path/to/credentials.json /etc/brain/google-credentials.json
chmod 600 /etc/brain/google-credentials.json
```

Add to `/etc/brain/secrets.env`:

```ini
GOOGLE_OAUTH_CLIENT_SECRETS_FILE=/etc/brain/google-credentials.json
GOOGLE_OAUTH_TOKEN_FILE=/etc/brain/google-token.json
```

Run `python -m brain daily --stdout` once interactively to complete the OAuth2 flow.
The token file is written automatically and will auto-refresh thereafter.

---

## Encrypted Google Drive Backups (6×/day)

VelaFlow ships a hardened, self-contained backup script that snapshots
tenant state to Google Drive **client-side encrypted with AES-256-GCM**.
The Drive account holder cannot read the contents — only a holder of
`VELAFLOW_BACKUP_KEY` can decrypt.

### Schedule

- 6 backups per day, 4 hours apart.
- Anchored at 07:30 Europe/Lisbon local time (LXC timezone must be set
  to `Europe/Lisbon` — the installer does this automatically).
- Runtimes: 03:30, 07:30, 11:30, 15:30, 19:30, 23:30 local.
- Retention: 30 files (= 5 days × 6/day). Older files are moved to
  Drive trash; empty Drive trash every 30 days to fully purge.

### Setup (one-time)

1. **Generate a backup key** on a trusted machine (NOT the LXC):

   ```bash
   python -c "import secrets, base64; \
       print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
   ```

   Store this in a password manager. **Losing it = unrecoverable backups.**
   This key MUST be different from `VELAFLOW_MASTER_KEY` so that a
   runtime compromise does not decrypt backups.

2. **Create a Google Cloud service account**:
   - [Google Cloud Console](https://console.cloud.google.com) → IAM & Admin
     → Service Accounts → Create.
   - Role: none (access is granted per-folder from the Drive UI).
   - Create and download a JSON key.
   - Enable the **Google Drive API** in the project.

3. **Create a dedicated folder in your Google Drive** named e.g.
   `velaflow-backups`. Right-click → Share → paste the service account
   email (`*@*.iam.gserviceaccount.com`) → give it **Editor**. Copy the
   folder ID from the URL (the segment after `/folders/`).

4. **Install the backup dependencies** inside the LXC:

   ```bash
   /opt/velaflow/.venv/bin/pip install 'velaflow[backup]'
   ```

5. **Write `/etc/velaflow/backup.env`** (mode 0600, owned by `root:velaflow`):

   ```ini
   VELAFLOW_BACKUP_KEY=<base64url 32 bytes>
   VELAFLOW_BACKUP_SA_JSON=/etc/velaflow/backup-sa.json
   VELAFLOW_BACKUP_FOLDER_ID=<drive folder id>
   VELAFLOW_DATA_DIR=/opt/velaflow/data
   VELAFLOW_CONFIG_DIR=/opt/velaflow/config
   VELAFLOW_BACKUP_RETENTION=30
   TZ=Europe/Lisbon
   ```

   Then:

   ```bash
   sudo install -m 0600 -o root -g velaflow /path/to/backup-sa.json \
       /etc/velaflow/backup-sa.json
   sudo chmod 0600 /etc/velaflow/backup.env
   ```

6. **Install the systemd units**:

   ```bash
   sudo cp scripts/brain-drive-backup.service /etc/systemd/system/
   sudo cp scripts/brain-drive-backup.timer   /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now brain-drive-backup.timer
   ```

7. **Verify**:

   ```bash
   # Dry-run the first backup now
   sudo systemctl start brain-drive-backup.service
   sudo journalctl -u brain-drive-backup.service --since "5 min ago"
   # Confirm file appears in the Drive folder
   ```

### Restoring a backup

1. Download the `velaflow-backup-YYYYMMDDTHHMMSSZ.tar.gz.enc` from Drive.
2. On the restore host (with `VELAFLOW_BACKUP_KEY` in env):

   ```bash
   python scripts/drive_backup.py --restore /tmp/backup.tar.gz.enc /opt/velaflow-restore
   ```

   The script verifies the GCM authentication tag before extracting;
   any tampering aborts decryption cleanly. It also blocks path
   traversal (`../`) and absolute-path tar members.

### Security guarantees

| Threat | Mitigation |
|--------|------------|
| Drive account compromise | Client-side AES-256-GCM; Google sees opaque bytes |
| Service-account key leak | SA has `drive.file` scope only + folder-scoped share → no broader Drive access |
| Runtime compromise leaking master key | `VELAFLOW_BACKUP_KEY` is a separate key domain |
| Tampered backup | GCM tag + `VFBKUP01` magic + associated data — any bit flip aborts decrypt |
| Backup quota exhaustion / ban | 6 requests/day ≪ Drive 1000/100s/user quota; exponential backoff on 429/5xx |
| Key rotation | Each envelope records a key fingerprint in MANIFEST.json |
| Host compromise during backup | Systemd unit runs with `NoNewPrivileges`, `ProtectSystem=strict`, no capabilities |

---

## Verification Checklist

| Check | Command | Expected |
|-------|---------|----------|
| CLI works | `python -m brain --help` | Shows CLI help |
| Todoist connected | `python -m brain daily --stdout --no-llm` | Shows your tasks |
| AI proxy working | `python -m brain daily --stdout` | AI-polished digest |
| Email delivery | `python -m brain daily` | Email arrives in inbox |
| Notion sync | `python -m brain notion-sync` | Tasks appear in Notion |
| Core timers active | `systemctl list-timers brain-*` | 4 timers active |
| NotebookLM auth | `python -m brain notebooklm-sync --stdout` | 7 sources added/deleted |
| NotebookLM timer | `systemctl is-enabled brain-notebooklm.timer` | `enabled` |

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `brain: command not found` | Not in venv | `source /opt/brain/venv/bin/activate` |
| `401` from Todoist or Notion | Wrong token | Update `/etc/brain/secrets.env`, restart service |
| AI returns `ProxyAuthError` | Proxy token wrong or expired | Issue new token on VPS, update `LITELLM_PROXY_TOKEN` |
| AI returns `BudgetExceeded` | Token budget hit | Increase budget in LiteLLM dashboard (no LXC change needed) |
| NotebookLM `RPCError: auth` | Cookies expired | Re-run `notebooklm-lxc-login.sh` or `notebooklm-push-auth.ps1` |
| `systemctl` hangs in LXC | Missing `keyctl=1` feature | Edit container config in Proxmox: `pct set 200 -features nesting=1,keyctl=1` |
| Docker not working in LXC | Missing `nesting=1` feature | Same as above |
| Playwright `No usable sandbox` | Missing system deps | Re-run `install.sh` to install Chromium system libs |

---

## Cloud Deployment (Live Website Backend)

### Hosting Recommendations

#### Option 1: Oracle Cloud Free Tier (RECOMMENDED — Free, Always-On)

Best for VelaFlow API + n8n + Redis. No GPU needed since LLM calls go through LiteLLM proxy.

| Resource | Free Tier |
|----------|-----------|
| CPU | 4 Arm OCPUs (Ampere A1) |
| RAM | 24 GB |
| Storage | 200 GB block volume |
| Network | 10 TB/month outbound |
| Cost | **$0/month forever** |

Sign up: https://cloud.oracle.com/ → Create ARM A1 Flex instance (4 OCPU, 24 GB, Ubuntu 22.04).

#### Option 2: Vast.ai (Cheapest GPU — For Self-Hosted LLM)

| GPU | VRAM | Price | Monthly (24/7) |
|-----|------|-------|-----------------|
| Tesla V100 | 32 GB | $0.02/hr | ~$14/mo |
| RTX 3090 | 24 GB | $0.09/hr | ~$65/mo |
| A100 PCIE | 80 GB | $0.09/hr | ~$65/mo |

Per-second billing, no lock-in. Community cloud (interruptible).

#### Option 3: RunPod (GPU — Production Grade)

| GPU | VRAM | Price | Monthly (24/7) |
|-----|------|-------|-----------------|
| RTX A5000 | 24 GB | $0.27/hr | ~$194/mo |
| L4 | 24 GB | $0.39/hr | ~$280/mo |

Also offers serverless endpoints (pay per inference, no idle cost).

### Quick Start: Cloud VM

```bash
# 1. SSH into your VM
ssh ubuntu@<VM_PUBLIC_IP>

# 2. Clone and configure
sudo git clone https://github.com/your-repo/velaflow.git /opt/velaflow
cd /opt/velaflow
sudo cp config/.env.production.example config/.env
sudo nano config/.env
# Set: VELAFLOW_DOMAIN, VELAFLOW_OWNER_EMAIL, GOOGLE_OAUTH_CLIENT_ID/SECRET

# 3. Deploy (installs Docker, Caddy, starts everything)
sudo bash deploy/cloud/setup-vm.sh

# 4. Point DNS A record to VM public IP, Caddy auto-obtains TLS
```

### Secrets Management (No Hardcoded Tokens)

```
User → n8n Secrets Manager → VelaFlow API (PATCH /tenants/me/config)
                                    ↓
                           AES-256-GCM encryption → per-tenant storage
                                    ↓
                    Worker decrypts → calls LiteLLM proxy → LLM API
```

Admin email from `VELAFLOW_OWNER_EMAIL` env var (auto-promoted to admin on registration).
All API tokens stored per-tenant, encrypted, managed via API or n8n workflow (`workflows/secrets-manager.json`).

---

## Hardened LXC Deployment (Production)

For production environments, use the hardened deployment script which applies full
security controls including AppArmor, capability drops, fail2ban, UFW firewall, and
systemd sandboxing.

### Proxmox VE

```bash
# On Proxmox host:
sudo bash deploy/lxc/deploy-hardened.sh --platform proxmox --id 200 --domain velaflow.example.com
```

### Oracle Cloud (Always-Free Tier)

```bash
# SSH into Oracle Cloud ARM A1 instance (Ubuntu 22.04):
ssh ubuntu@<OCI_PUBLIC_IP>
git clone https://github.com/your-repo/velaflow.git /opt/velaflow
cd /opt/velaflow

# Full deployment (installs LXD, creates hardened LXC, configures NAT):
sudo bash deploy/cloud/setup-oracle.sh --domain velaflow.example.com
```

After deployment, add OCI Security List rules:

| Direction | Source     | Protocol | Port | Description |
|-----------|-----------|----------|------|-------------|
| Ingress   | 0.0.0.0/0 | TCP      | 22   | SSH         |
| Ingress   | 0.0.0.0/0 | TCP      | 80   | HTTP        |
| Ingress   | 0.0.0.0/0 | TCP      | 443  | HTTPS       |

### Any LXD/Incus Host

```bash
sudo bash deploy/lxc/deploy-hardened.sh --platform lxd --name velaflow
```

### Security Controls Applied

The hardened deployer applies the following security layers:

| Layer | Control | Details |
|-------|---------|---------|
| Container | Unprivileged LXC | User namespace mapping, no root on host |
| Container | AppArmor generated | Mandatory access control profile |
| Container | Capability drops | sys_admin, sys_rawio, sys_module, sys_ptrace, etc. |
| Container | cgroup limits | Memory max 6 GB, CPU quota 200% |
| Network | UFW firewall | Only 22/80/443 inbound |
| Network | fail2ban | SSH (3 attempts) + API brute-force (10 attempts) |
| Network | Caddy reverse proxy | Auto-HTTPS, security headers, blocked /docs /redoc |
| Kernel | sysctl hardening | SYN flood protection, ICMP redirect disable, ptrace restrict |
| Application | systemd sandbox | NoNewPrivileges, PrivateTmp, ProtectSystem=strict, LimitCORE=0, LimitMEMLOCK=infinity (R19), LockPersonality, RestrictRealtime, ProtectClock, ProtectHostname |
| Secrets | tmpfs mount | Runtime secrets in memory at /run/velaflow-secrets |
| Secrets | 0640 permissions | Root-owned, velaflow group read |
| Logging | Copilot export | Sanitised HMAC-chained logs every 6h |
| Updates | unattended-upgrades | Automatic Debian security patches |

### Copilot Log Access

Sanitised logs are exported every 6 hours to `/opt/velaflow/data/copilot-logs.md`.

- **Local network:** `GET http://<container-ip>/copilot/logs`
- **Via SSH tunnel:** `ssh -L 8080:<lxc-ip>:80 user@host` then `http://localhost:8080/copilot/logs`

### Nesting for KEDA/K8s (Premium Tier)

The hardened LXC is created with `nesting=1` and `keyctl=1`, enabling nested
containers for KEDA-based Kubernetes autoscaling of premium-tier workloads
(e.g., local LLM inference with Ollama). See `deploy/kubernetes/keda-scaler.yaml`.
