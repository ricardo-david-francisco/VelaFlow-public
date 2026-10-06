# NotebookLM Integration Setup

This document covers the one-time setup required to enable automatic syncing
of the entire Notion 2nd-Brain workspace into a NotebookLM notebook.

---

## How It Works

```
Notion 2nd-Brain
 └─ root page
     ├─ Command Center      ──┐
     ├─ Daily Planner          │  brain notebooklm-sync
     ├─ Weekly Planner         │  (runs weekly)
     ├─ ... (all subpages)   ──┘
     └─ databases              │
                               ▼
                       NotebookLM notebook
                        "VelaFlow"
                       (pasted-text sources,
                        one per top-level page)
```

The brain CLI:
1. Reads all child pages of the Notion root page recursively (up to 4 levels deep).
2. Converts each top-level section + its subpages into Markdown text.
3. Deletes stale text sources from the NotebookLM notebook (rebuild mode).
4. Re-adds fresh content as pasted-text sources — one source per top-level Notion page.

NotebookLM can then answer questions, generate Audio Overviews, Study Guides,
Mind Maps, etc., all grounded in your private Notion knowledge base.

---

## Prerequisites

- `notebooklm-py` library and Playwright (for browser-based login)
- A personal Google account with access to [notebooklm.google.com](https://notebooklm.google.com)
- `NOTION_API_TOKEN` and `NOTION_ROOT_PAGE_ID` already configured (see deployment guide)

---

## Step 1 — Install the Library

On the machine/LXC that runs the brain scheduler:

```bash
pip install "notebooklm-py[browser]"
playwright install chromium
```

Or if installed from the repo:

```bash
pip install -e ".[notebooklm]"
playwright install chromium
```

---

## Step 2 — Authenticate with Google (one-time)

### On the LXC container (headless — recommended)

The installer already set up Xvfb, x11vnc, and Playwright Chromium. Run the
VNC-based login helper from your Proxmox host:

```bash
pct exec 200 -- bash /opt/brain/scripts/notebooklm-lxc-login.sh
```

The script will print:
```
  Connect VNC from your desktop to:  <lxc-ip>:5900
```

Open any VNC viewer (RealVNC, TigerVNC, Remmina) and connect. Chromium opens
showing the Google login page. Sign in, wait for the NotebookLM homepage to load,
then press **Enter** in the terminal where the script is running.

Cookies are saved to `/opt/brain/.notebooklm/storage_state.json`.

### From Windows desktop (if already logged in to NotebookLM)

If you already ran `notebooklm login` on your Windows machine, push the existing
cookies to the LXC in one command. No VNC needed:

```powershell
.\scripts\notebooklm-push-auth.ps1 -ProxmoxHost 192.168.1.10
```

The script reads `%USERPROFILE%\.notebooklm\storage_state.json`, base64-encodes it,
SSHes into Proxmox and writes the file into the LXC container via `pct exec`.

### On a desktop / Ubuntu VM (dev mode)

```bash
NOTEBOOKLM_HOME=/opt/brain/.notebooklm notebooklm login
```

A Chromium window opens. Sign in with your Google account. Cookies are saved to
`$NOTEBOOKLM_HOME/storage_state.json`.

> **Cookie location:** `NOTEBOOKLM_HOME/storage_state.json`
> Default: `~/.notebooklm/storage_state.json`
> LXC production path: `/opt/brain/.notebooklm/storage_state.json`
> Override with the `NOTEBOOKLM_HOME` env var.

---

## Step 3 — First Sync (creates the notebook)

```bash
# Inside the LXC:
sudo -u brain \
  NOTEBOOKLM_HOME=/opt/brain/.notebooklm \
  /opt/brain/venv/bin/brain notebooklm-sync --stdout

# Or on a dev machine:
brain notebooklm-sync --stdout
```

On the first run, `NOTEBOOKLM_NOTEBOOK_ID` is not set, so the command creates a
new notebook called `VelaFlow` and prints:

```
NotebookLM sync complete — 7 sources added, 0 deleted.
NOTEBOOKLM_NOTEBOOK_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

The 7 sources are:
- 🧠 Command Center
- 📅 Daily Planner
- 📆 Weekly Planner
- 🏖️ Weekend Planner
- 📊 Task Board
- 📝 Blog & Notes
- Todoist Active Tasks (all active tasks, grouped by project → section)

Copy that value into `/etc/brain/secrets.env` (LXC) or `config/.env` (dev):

```ini
NOTEBOOKLM_HOME=/opt/brain/.notebooklm
NOTEBOOKLM_NOTEBOOK_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
NOTEBOOKLM_NOTEBOOK_NAME=VelaFlow
```

All subsequent runs use the existing notebook and replace all sources atomically
(full rebuild mode by default). No duplicate sources.

---

## Step 4 — Verify in NotebookLM

Open [notebooklm.google.com](https://notebooklm.google.com) and open the
**VelaFlow** notebook.  You should see one source per top-level Notion
page (Command Center, Daily Planner, etc.).

---

## Step 5 — Enable the Weekly Timer

The `install.sh` script already installed the unit files. You only need to enable
the timer after completing the auth step above:

```bash
# Inside the LXC (or from the Proxmox host via pct exec):
systemctl enable --now brain-notebooklm.timer

# Verify
systemctl status brain-notebooklm.timer
journalctl -u brain-notebooklm -n 50
```

The timer fires every Sunday at 21:00 and performs a full rebuild of all 7 sources.

---

## Manual Sync Commands

```bash
# Full rebuild (default — recommended for scheduled runs)
brain notebooklm-sync

# Append only — add new sources without removing existing ones
brain notebooklm-sync --no-rebuild

# Print summary to stdout instead of logging
brain notebooklm-sync --stdout
```

---

## Authentication Maintenance

| Situation | Action |
|-----------|--------|
| CSRF token expired | Handled automatically by the library |
| Google session cookie expired (every 2–4 weeks) | See below |
| Sync fails with `RPCError: auth` or `401` | Re-authenticate (see below) |

### Re-authenticate (LXC)

```bash
# Method A — VNC login (from Proxmox host):
pct exec 200 -- bash /opt/brain/scripts/notebooklm-lxc-login.sh

# Method B — push from Windows (if already logged in on desktop):
.\scripts\notebooklm-push-auth.ps1 -ProxmoxHost <your-proxmox-host-ip>
```

No configuration changes needed after re-auth — the cookie file is overwritten
in-place and the next sync picks it up automatically.

---

## Configuration Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `NOTEBOOKLM_NOTEBOOK_ID` | After first run | _(empty)_ | Target notebook ID (printed on first sync) |
| `NOTEBOOKLM_NOTEBOOK_NAME` | No | `VelaFlow` | Name used when creating a new notebook |
| `NOTEBOOKLM_HOME` | No | `~/.notebooklm` | Override auth cookie storage directory |

---

## Limitations and Notes

- **Unofficial API**: `notebooklm-py` uses internal Google APIs reverse-engineered
  from the browser.  They can change without notice.  The library is actively
  maintained (10 k+ GitHub stars as of April 2026).
- **Free tier**: NotebookLM free tier supports up to 50 sources per notebook.
  The brain sync creates one source per top-level Notion page — well within the limit
  for a typical 2nd-Brain workspace.
- **Unsupported block types**: Images, embedded files, and synced blocks are skipped.
  Text content is fully captured.
- **Cookie expiry**: Google cookies typically last 2–4 weeks.  Expect one manual
  `notebooklm login` per month.  Everything else is fully automated.
- **No NotebookLM Enterprise required**: This integration uses the free consumer
  tier via the unofficial Python library.  Zero extra cost.
