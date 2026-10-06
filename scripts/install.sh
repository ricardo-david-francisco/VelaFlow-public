#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# VelaFlow — Ubuntu LTS / Proxmox LXC installer   (DEV-ONLY QUICK-START)
#
# Scope: this Bash installer is a fast, single-host bring-up for a developer
# LXC or laptop. It is imperative by design. The PRODUCTION deployment path
# is Terraform, via three equal-status targets that all delegate to the
# shared `modules/velaflow-host/` module:
#
#   deploy/terraform/proxmox/       — Proxmox LXC on homelab Proxmox VE
#   deploy/terraform/generic-vm/    — any SSH-reachable Linux host
#   deploy/terraform/oracle-cloud/  — OCI Always Free Ampere A1.Flex VM
#
# No cloud is "primary" — pick whichever host you own. See
# docs/adr/0003-terraform-iac-vs-bash-install.md for the rationale.
#
# Tested on: Ubuntu 22.04 LTS, Ubuntu 24.04 LTS, Debian 12 (Proxmox default LXC)
# Run as root inside the LXC or a fresh Ubuntu VM:
#   bash scripts/install.sh [--dev]
#
# Flags:
#   --dev     Developer mode: direct API keys in /etc/brain/secrets.env
#             (local testing only — use the proxy model for any real LXC)
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

BRAIN_USER="brain"
BRAIN_HOME="/opt/brain"
BRAIN_CONFIG="/etc/brain"
LOG_DIR="/var/log/brain"
PYTHON_MIN="3.11"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[brain]${NC} $*"; }
warn()  { echo -e "${YELLOW}[brain]${NC} $*"; }
die()   { echo -e "${RED}[brain] ERROR:${NC} $*" >&2; exit 1; }

DEV_MODE=false
for arg in "$@"; do
  case "$arg" in
    --dev)   DEV_MODE=true  ;;
    --help|-h) echo "Usage: install.sh [--dev]"; exit 0 ;;
  esac
done

[[ $EUID -eq 0 ]] || die "Run as root: sudo bash scripts/install.sh"

# 1. System packages
info "Installing system packages..."
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  python3 python3-pip python3-venv python3-dev git curl ca-certificates \
  build-essential libssl-dev libffi-dev 2>/dev/null

python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" \
  || die "Python 3.11+ required. Try: apt-get install python3.11"

# 2. Dedicated non-login user
if ! id "${BRAIN_USER}" &>/dev/null; then
  useradd --system --no-create-home --shell /usr/sbin/nologin \
    --home-dir "${BRAIN_HOME}" "${BRAIN_USER}"
  info "Created system user '${BRAIN_USER}'"
fi

# 3. Directory structure
mkdir -p "${BRAIN_HOME}" "${BRAIN_CONFIG}" "${LOG_DIR}"
chown root:root "${BRAIN_CONFIG}"; chmod 750 "${BRAIN_CONFIG}"
chown "${BRAIN_USER}:${BRAIN_USER}" "${BRAIN_HOME}" "${LOG_DIR}"
chmod 750 "${LOG_DIR}"

# 4. Virtual environment + package install
VENV="${BRAIN_HOME}/venv"
[[ -d "${VENV}" ]] || sudo -u "${BRAIN_USER}" python3 -m venv "${VENV}"
info "Installing brain package into venv..."
sudo -u "${BRAIN_USER}" "${VENV}/bin/pip" install --quiet --upgrade pip
[[ -f "pyproject.toml" ]] || die "Run install.sh from the brain repo root directory."
sudo -u "${BRAIN_USER}" "${VENV}/bin/pip" install --quiet -e "."
info "Package installed: $(${VENV}/bin/brain --help 2>/dev/null | head -1 || echo 'ok')"

# 4b. NotebookLM optional dependency + Playwright Chromium
info "Installing notebooklm-py and Playwright Chromium..."
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  libglib2.0-0 libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
  libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
  libxrandr2 libgbm1 libasound2 2>/dev/null || true
sudo -u "${BRAIN_USER}" "${VENV}/bin/pip" install --quiet "notebooklm-py[browser]>=0.3.0"
sudo -u "${BRAIN_USER}" PLAYWRIGHT_BROWSERS_PATH="${BRAIN_HOME}/.playwright" \
  "${VENV}/bin/playwright" install chromium --with-deps 2>/dev/null \
  && info "  Playwright Chromium installed." \
  || warn "  Playwright Chromium install had warnings (non-fatal)."
# Point notebooklm-py at the brain-user home for cookie storage
NLM_AUTH_DIR="${BRAIN_HOME}/.notebooklm"
mkdir -p "${NLM_AUTH_DIR}"
chown "${BRAIN_USER}:${BRAIN_USER}" "${NLM_AUTH_DIR}"
chmod 700 "${NLM_AUTH_DIR}"

# 5. Secrets configuration
ENV_FILE="${BRAIN_CONFIG}/secrets.env"
info "Configuring secrets (${ENV_FILE})..."

if [[ ! -f "${ENV_FILE}" ]]; then
  if [[ -f "config/.env.example" ]]; then
    cp "config/.env.example" "${ENV_FILE}"
  else
    cat > "${ENV_FILE}" <<ENVEOF
# VelaFlow — fill in LITELLM_PROXY_URL and LITELLM_PROXY_TOKEN.
# Obtain a budget-capped proxy token from your LiteLLM dashboard.
# See docs/security.md — Zero-Trust Proxy Model.
LITELLM_PROXY_URL=
LITELLM_PROXY_TOKEN=
TODOIST_API_TOKEN=
NOTION_API_TOKEN=
NOTION_ROOT_PAGE_ID=
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=
SMTP_PASSWORD=
DIGEST_FROM_EMAIL=
DIGEST_TO_EMAIL=
ENVEOF
  fi
fi
chmod 600 "${ENV_FILE}"; chown root:root "${ENV_FILE}"
warn "Edit ${ENV_FILE} — set LITELLM_PROXY_URL and LITELLM_PROXY_TOKEN."
warn "See docs/security.md for the Zero-Trust Proxy setup guide."
ENV_FILE_LINE="EnvironmentFile=${ENV_FILE}"

# 6. Systemd units
info "Installing systemd units..."
SYSTEMD_DIR="/etc/systemd/system"
SCRIPTS_DIR="$(pwd)/scripts"

for unit in brain-daily brain-weekly brain-weekend brain-sync brain-notebooklm; do
  if [[ -f "${SCRIPTS_DIR}/${unit}.service" ]]; then
    sed -e "s|@@VENV@@|${VENV}|g" \
        -e "s|@@BRAIN_HOME@@|${BRAIN_HOME}|g" \
        -e "s|@@ENV_FILE_LINE@@|${ENV_FILE_LINE}|g" \
        -e "s|@@BRAIN_USER@@|${BRAIN_USER}|g" \
        "${SCRIPTS_DIR}/${unit}.service" > "${SYSTEMD_DIR}/${unit}.service"
    info "  Installed ${unit}.service"
  fi
  if [[ -f "${SCRIPTS_DIR}/${unit}.timer" ]]; then
    cp "${SCRIPTS_DIR}/${unit}.timer" "${SYSTEMD_DIR}/${unit}.timer"
    info "  Installed ${unit}.timer"
  fi
done

systemctl daemon-reload
for timer in brain-daily brain-weekly brain-weekend brain-sync; do
  [[ -f "${SYSTEMD_DIR}/${timer}.timer" ]] && systemctl enable --now "${timer}.timer" 2>/dev/null && \
    info "  Enabled: ${timer}.timer"
done
# brain-notebooklm.timer is NOT auto-enabled — requires auth first (see Step 6 in docs/deployment.md)
info "  brain-notebooklm.timer installed but NOT started (auth required first)"

# 7. Summary
info ""
info "╔══════════════════════════════════════════════════╗"
info "║   VelaFlow installed successfully!        ║"
info "╚══════════════════════════════════════════════════╝"
warn "Secrets: ${BRAIN_CONFIG}/secrets.env  ← SET LITELLM_PROXY_URL + LITELLM_PROXY_TOKEN"
info "Test:    sudo -u ${BRAIN_USER} ${VENV}/bin/brain daily --stdout"
info "Sync:    sudo -u ${BRAIN_USER} ${VENV}/bin/brain notion-sync --full"
info "Logs:    journalctl -u brain-daily -f"
info "Timers:  systemctl list-timers 'brain-*'"
info "Proxy:   Real API keys stay on YOUR VPS running LiteLLM — see docs/security.md"
warn ""
warn "┌──────────────────────────────────────────────────┐"
warn "│ NotebookLM: Auth still required                   │"
warn "│                                                  │"
warn "│ Option A — VNC login (from scratch):             │"
warn "│   bash scripts/notebooklm-lxc-login.sh           │"
warn "│   Then connect VNC from desktop to <ip>:5900      │"
warn "│                                                  │"
warn "│ Option B — Push from Windows (if already logged  │"
warn "│ into NotebookLM on your desktop):                │"
warn "│   .\scripts\notebooklm-push-auth.ps1 \           │"
warn "│     -ProxmoxHost <ip>                            │"
warn "│                                                  │"
warn "│ Then enable timer after first successful sync:   │"
warn "│   systemctl enable --now brain-notebooklm.timer  │"
warn "└──────────────────────────────────────────────────┘"
