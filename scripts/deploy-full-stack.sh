#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# VelaFlow — One-Click Full Stack Deployment
#
# Run this ON your Proxmox host. It orchestrates:
#   1. LXC container creation
#   2. Code deployment into the container
#   3. install.sh execution (Python, venv, systemd)
#   4. LiteLLM proxy deployment on remote VPS (via SSH)
#   5. Proxy token generation and injection
#   6. Secrets configuration
#   7. NotebookLM auth (push or VNC)
#   8. Health check validation
#
# Usage:
#   bash deploy-full-stack.sh --config /path/to/deploy.env
#
# Or with inline arguments:
#   bash deploy-full-stack.sh \
#     --ctid 200 \
#     --vps-host proxy.yourdomain.com \
#     --vps-user root \
#     --domain proxy.yourdomain.com \
#     --gemini-key AIzaSy... \
#     --todoist-token abc123 \
#     --repo-path /root/velaflow
#
# See config/.env.deploy.example for all options.
# ═══════════════════════════════════════════════════════════════════════
set -euo pipefail

VERSION="1.0.0"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
info()  { echo -e "${GREEN}[deploy]${NC} $*"; }
warn()  { echo -e "${YELLOW}[deploy]${NC} $*"; }
err()   { echo -e "${RED}[deploy] ERROR:${NC} $*" >&2; }
die()   { err "$*"; exit 1; }
step()  { echo ""; echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; echo -e "${BOLD}  $*${NC}"; echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; }

# ── Defaults ──────────────────────────────────────────────────────────
CTID="${CTID:-200}"
HOSTNAME="${LXC_HOSTNAME:-velaflow}"
TEMPLATE="${LXC_TEMPLATE:-local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst}"
STORAGE="${LXC_STORAGE:-local-lvm}"
DISK_SIZE="${LXC_DISK:-10}"
RAM="${LXC_RAM:-1024}"
SWAP="${LXC_SWAP:-512}"
CORES="${LXC_CORES:-2}"
BRIDGE="${LXC_BRIDGE:-vmbr0}"

REPO_PATH="${REPO_PATH:-}"
VPS_HOST="${VPS_HOST:-}"
VPS_USER="${VPS_USER:-root}"
DOMAIN="${DOMAIN:-}"
MASTER_KEY="${LITELLM_MASTER_KEY:-}"
GEMINI_KEY="${GEMINI_API_KEY:-}"
OPENAI_KEY="${OPENAI_API_KEY:-}"
GROQ_KEY="${GROQ_API_KEY:-}"
CERTBOT_EMAIL="${CERTBOT_EMAIL:-admin@yourdomain.com}"

TODOIST_TOKEN="${TODOIST_API_TOKEN:-}"
NOTION_TOKEN="${NOTION_API_TOKEN:-}"
NOTION_ROOT="${NOTION_ROOT_PAGE_ID:-}"
SMTP_HOST_VAL="${SMTP_HOST:-smtp.gmail.com}"
SMTP_PORT_VAL="${SMTP_PORT:-587}"
SMTP_USER="${SMTP_USERNAME:-}"
SMTP_PASS="${SMTP_PASSWORD:-}"
DIGEST_FROM="${DIGEST_FROM_EMAIL:-}"
DIGEST_TO="${DIGEST_TO_EMAIL:-}"
WHATSAPP_PHONE="${WHATSAPP_PHONE_NUMBER:-}"
WHATSAPP_KEY="${CALLMEBOT_API_KEY:-}"

SKIP_PROXY="${SKIP_PROXY:-false}"
SKIP_LXC="${SKIP_LXC:-false}"
SKIP_NOTEBOOKLM="${SKIP_NOTEBOOKLM:-false}"
PUSH_AUTH="${PUSH_NOTEBOOKLM_AUTH:-false}"
NLM_COOKIE_PATH="${NLM_COOKIE_PATH:-}"
PREMIUM_LLM="${SETUP_PREMIUM_LLM:-false}"
LLM_MODEL="${LLM_MODEL:-qwen2:1.5b}"

CONFIG_FILE=""

# ── Argument Parsing ──────────────────────────────────────────────────
usage() {
    cat <<EOF
VelaFlow Full Stack Deployment v${VERSION}

Usage: $0 --config deploy.env
       $0 --ctid 200 --vps-host proxy.example.com --gemini-key AIzaSy...

Required (at minimum):
  --config FILE        Load all settings from a deploy.env file
  --gemini-key KEY     Google AI API key (stored only on VPS, never in LXC)
  --todoist-token TOK  Todoist API token

VPS / Proxy:
  --vps-host HOST      VPS hostname or IP for LiteLLM proxy
  --vps-user USER      SSH user on VPS (default: root)
  --domain DOMAIN      Domain for the proxy (e.g., proxy.yourdomain.com)
  --master-key KEY     LiteLLM master key (auto-generated if not set)
  --skip-proxy         Skip proxy deployment (use existing proxy)

LXC:
  --ctid ID            Container ID (default: 200)
  --repo-path PATH     Path to VelaFlow repo on Proxmox host
  --skip-lxc           Skip LXC creation (container already exists)

NotebookLM:
  --push-auth          Push NotebookLM cookies from host
  --nlm-cookie PATH    Path to storage_state.json on Proxmox host
  --skip-notebooklm    Skip NotebookLM setup entirely

Optional:
  --openai-key KEY     OpenAI API key (optional extra model)
  --groq-key KEY       Groq API key (optional extra model)
  --premium-llm        Also set up nested LXC with Ollama
  --llm-model MODEL    Ollama model (default: qwen2:1.5b)
EOF
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config)           CONFIG_FILE="$2";       shift 2 ;;
        --ctid)             CTID="$2";              shift 2 ;;
        --vps-host)         VPS_HOST="$2";          shift 2 ;;
        --vps-user)         VPS_USER="$2";          shift 2 ;;
        --domain)           DOMAIN="$2";            shift 2 ;;
        --master-key)       MASTER_KEY="$2";        shift 2 ;;
        --gemini-key)       GEMINI_KEY="$2";        shift 2 ;;
        --openai-key)       OPENAI_KEY="$2";        shift 2 ;;
        --groq-key)         GROQ_KEY="$2";          shift 2 ;;
        --todoist-token)    TODOIST_TOKEN="$2";     shift 2 ;;
        --notion-token)     NOTION_TOKEN="$2";      shift 2 ;;
        --repo-path)        REPO_PATH="$2";         shift 2 ;;
        --skip-proxy)       SKIP_PROXY=true;        shift ;;
        --skip-lxc)         SKIP_LXC=true;          shift ;;
        --skip-notebooklm)  SKIP_NOTEBOOKLM=true;   shift ;;
        --push-auth)        PUSH_AUTH=true;         shift ;;
        --nlm-cookie)       NLM_COOKIE_PATH="$2";  shift 2 ;;
        --premium-llm)      PREMIUM_LLM=true;      shift ;;
        --llm-model)        LLM_MODEL="$2";        shift 2 ;;
        --help|-h)          usage ;;
        *) die "Unknown argument: $1. Use --help for usage." ;;
    esac
done

# Load config file if provided
if [[ -n "${CONFIG_FILE}" ]]; then
    [[ -f "${CONFIG_FILE}" ]] || die "Config file not found: ${CONFIG_FILE}"
    info "Loading config from ${CONFIG_FILE}"
    # shellcheck disable=SC1090
    source "${CONFIG_FILE}"
fi

# Auto-detect repo path
if [[ -z "${REPO_PATH}" ]]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [[ -f "${SCRIPT_DIR}/../pyproject.toml" ]]; then
        REPO_PATH="$(cd "${SCRIPT_DIR}/.." && pwd)"
    else
        die "Cannot auto-detect repo path. Use --repo-path or run from the repo."
    fi
fi

# Auto-generate master key if not set
if [[ -z "${MASTER_KEY}" ]]; then
    MASTER_KEY="sk-$(openssl rand -hex 24)"
    info "Generated LiteLLM master key: ${MASTER_KEY}"
fi

# Validate minimum requirements
[[ -f "${REPO_PATH}/pyproject.toml" ]] || die "Invalid repo path: ${REPO_PATH}/pyproject.toml not found"
[[ -n "${TODOIST_TOKEN}" ]] || die "Todoist token required. Use --todoist-token or TODOIST_API_TOKEN in config."

# ── Banner ────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}╔═══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║        VelaFlow — One-Click Full Stack Deploy            ║${NC}"
echo -e "${BOLD}║        Version ${VERSION}                                      ║${NC}"
echo -e "${BOLD}╚═══════════════════════════════════════════════════════════╝${NC}"
echo ""
echo "  LXC Container:   ${CTID} (${HOSTNAME})"
echo "  Repo:            ${REPO_PATH}"
echo "  VPS/Proxy:       ${VPS_HOST:-SKIP}"
echo "  Domain:          ${DOMAIN:-N/A}"
echo "  Premium LLM:     ${PREMIUM_LLM}"
echo "  NotebookLM:      $(${SKIP_NOTEBOOKLM} && echo 'SKIP' || echo 'YES')"
echo ""

# ═══════════════════════════════════════════════════════════════════════
# PHASE 1: LXC Container
# ═══════════════════════════════════════════════════════════════════════
if [[ "${SKIP_LXC}" == "false" ]]; then
    step "Phase 1: Creating LXC Container ${CTID}"

    # Check if container already exists
    if pct status "${CTID}" &>/dev/null; then
        warn "Container ${CTID} already exists."
        read -rp "  Destroy and recreate? [y/N] " confirm
        if [[ "${confirm}" =~ ^[Yy] ]]; then
            pct stop "${CTID}" 2>/dev/null || true
            sleep 2
            pct destroy "${CTID}" --force
            info "Destroyed container ${CTID}"
        else
            die "Container ${CTID} exists. Use --skip-lxc to reuse, or choose another CTID."
        fi
    fi

    # Download template if needed
    if ! pveam list local | grep -q "debian-12-standard"; then
        info "Downloading Debian 12 template..."
        pveam update
        pveam download local debian-12-standard_12.7-1_amd64.tar.zst
    fi

    # Create container
    info "Creating unprivileged LXC container..."
    pct create "${CTID}" "${TEMPLATE}" \
        --hostname "${HOSTNAME}" \
        --storage "${STORAGE}" \
        --rootfs "${STORAGE}:${DISK_SIZE}" \
        --memory "${RAM}" \
        --swap "${SWAP}" \
        --cores "${CORES}" \
        --net0 "name=eth0,bridge=${BRIDGE},ip=dhcp" \
        --nameserver "8.8.8.8" \
        --unprivileged 1 \
        --features "nesting=1,keyctl=1" \
        --onboot 1 \
        --start 0

    # Security hardening
    cat >> "/etc/pve/lxc/${CTID}.conf" <<EOF

# VelaFlow security hardening
lxc.apparmor.profile: generated
lxc.cap.drop: sys_admin sys_rawio sys_module sys_ptrace net_raw mac_admin mac_override sys_boot sys_time
EOF

    pct start "${CTID}"
    info "Container starting..."

    # Wait for network
    LXC_IP=""
    for i in $(seq 1 30); do
        LXC_IP=$(pct exec "${CTID}" -- hostname -I 2>/dev/null | awk '{print $1}')
        [[ -n "${LXC_IP}" ]] && break
        sleep 2
    done
    [[ -n "${LXC_IP}" ]] || die "Container ${CTID} has no network after 60s. Check DHCP."
    info "Container ${CTID} is up at ${LXC_IP}"

else
    step "Phase 1: Using Existing Container ${CTID}"
    pct status "${CTID}" &>/dev/null || die "Container ${CTID} does not exist."
    LXC_IP=$(pct exec "${CTID}" -- hostname -I 2>/dev/null | awk '{print $1}')
    [[ -n "${LXC_IP}" ]] || die "Container ${CTID} is not running or has no network."
    info "Container ${CTID} is at ${LXC_IP}"
fi

# ═══════════════════════════════════════════════════════════════════════
# PHASE 2: Deploy Code into LXC
# ═══════════════════════════════════════════════════════════════════════
step "Phase 2: Deploying VelaFlow Code"

# Create /opt/brain in the container
pct exec "${CTID}" -- mkdir -p /opt/brain

# Push the repo via tar (excludes .venv, __pycache__, .git)
info "Packing and pushing repository..."
tar -C "${REPO_PATH}" \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='.git' \
    --exclude='*.pyc' \
    --exclude='node_modules' \
    --exclude='.pytest_cache' \
    -czf /tmp/velaflow-deploy.tar.gz .

pct push "${CTID}" /tmp/velaflow-deploy.tar.gz /tmp/velaflow-deploy.tar.gz

pct exec "${CTID}" -- bash -c "
    cd /opt/brain
    tar xzf /tmp/velaflow-deploy.tar.gz
    rm /tmp/velaflow-deploy.tar.gz
"
rm -f /tmp/velaflow-deploy.tar.gz
info "Code deployed to /opt/brain"

# ═══════════════════════════════════════════════════════════════════════
# PHASE 3: Run install.sh inside LXC
# ═══════════════════════════════════════════════════════════════════════
step "Phase 3: Running install.sh"

pct exec "${CTID}" -- bash -c "cd /opt/brain && bash scripts/install.sh"
info "install.sh complete"

# ═══════════════════════════════════════════════════════════════════════
# PHASE 4: LiteLLM Proxy on VPS
# ═══════════════════════════════════════════════════════════════════════
PROXY_URL=""
PROXY_TOKEN=""

if [[ "${SKIP_PROXY}" == "false" && -n "${VPS_HOST}" ]]; then
    step "Phase 4: Deploying LiteLLM Proxy on ${VPS_HOST}"

    [[ -n "${GEMINI_KEY}" ]] || die "Gemini API key required for proxy setup. Use --gemini-key."
    [[ -n "${DOMAIN}" ]] || DOMAIN="${VPS_HOST}"

    # Test SSH connectivity first
    info "Testing SSH to ${VPS_USER}@${VPS_HOST}..."
    ssh -o ConnectTimeout=10 -o BatchMode=yes "${VPS_USER}@${VPS_HOST}" "echo ok" \
        || die "Cannot SSH to ${VPS_USER}@${VPS_HOST}. Set up SSH key auth first."

    # Push and run proxy setup script
    info "Deploying LiteLLM proxy..."
    scp "${REPO_PATH}/scripts/setup-litellm-proxy.sh" "${VPS_USER}@${VPS_HOST}:/tmp/setup-litellm-proxy.sh"

    PROXY_ARGS="--domain ${DOMAIN} --master-key ${MASTER_KEY} --gemini-key ${GEMINI_KEY}"
    [[ -n "${OPENAI_KEY}" ]] && PROXY_ARGS="${PROXY_ARGS} --openai-key ${OPENAI_KEY}"
    [[ -n "${GROQ_KEY}" ]] && PROXY_ARGS="${PROXY_ARGS} --groq-key ${GROQ_KEY}"
    [[ -n "${CERTBOT_EMAIL}" ]] && PROXY_ARGS="${PROXY_ARGS} --email ${CERTBOT_EMAIL}"

    # shellcheck disable=SC2029
    ssh "${VPS_USER}@${VPS_HOST}" "bash /tmp/setup-litellm-proxy.sh ${PROXY_ARGS}"

    PROXY_URL="https://${DOMAIN}"

    # Wait for proxy to be healthy
    info "Waiting for proxy health check..."
    for i in $(seq 1 30); do
        if ssh "${VPS_USER}@${VPS_HOST}" "curl -sf http://localhost:4000/health" &>/dev/null; then
            break
        fi
        sleep 2
    done

    # Generate a budget-capped token for the LXC
    info "Generating budget-capped proxy token..."
    TOKEN_RESPONSE=$(ssh "${VPS_USER}@${VPS_HOST}" "curl -sf -X POST http://localhost:4000/key/generate \
        -H 'Authorization: Bearer ${MASTER_KEY}' \
        -H 'Content-Type: application/json' \
        -d '{
            \"key_alias\": \"velaflow-lxc-${CTID}\",
            \"max_budget\": 10.00,
            \"budget_duration\": \"30d\",
            \"rpm_limit\": 30,
            \"models\": [\"gemini/gemini-2.5-flash\", \"gemini/gemini-2.5-pro\", \"gemini/gemini-2.5-flash-lite\"]
        }'" 2>/dev/null)

    PROXY_TOKEN=$(echo "${TOKEN_RESPONSE}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('key',''))" 2>/dev/null)

    if [[ -n "${PROXY_TOKEN}" ]]; then
        info "Proxy token generated: ${PROXY_TOKEN:0:12}..."
    else
        warn "Could not auto-generate proxy token. Generate manually via:"
        warn "  curl -X POST https://${DOMAIN}/key/generate \\"
        warn "    -H 'Authorization: Bearer ${MASTER_KEY}' \\"
        warn "    -H 'Content-Type: application/json' \\"
        warn "    -d '{\"key_alias\": \"lxc-${CTID}\", \"max_budget\": 10.0, \"budget_duration\": \"30d\"}'"
    fi

elif [[ "${SKIP_PROXY}" == "true" ]]; then
    step "Phase 4: Skipping Proxy Deployment"
    info "Using existing proxy. Ensure LITELLM_PROXY_URL and LITELLM_PROXY_TOKEN are in config."
    # Try to extract from config file
    if [[ -n "${CONFIG_FILE}" ]]; then
        PROXY_URL="${LITELLM_PROXY_URL:-}"
        PROXY_TOKEN="${LITELLM_PROXY_TOKEN:-}"
    fi
else
    step "Phase 4: No VPS Configured — Skipping Proxy"
    warn "No --vps-host provided. LiteLLM proxy must be set up manually."
    warn "Run on your VPS: bash scripts/setup-litellm-proxy.sh --domain ... --master-key ... --gemini-key ..."
fi

# ═══════════════════════════════════════════════════════════════════════
# PHASE 5: Inject Secrets into LXC
# ═══════════════════════════════════════════════════════════════════════
step "Phase 5: Configuring Secrets"

# Build secrets.env content
SECRETS_CONTENT="# VelaFlow secrets — auto-generated by deploy-full-stack.sh
# $(date -u '+%Y-%m-%d %H:%M:%S UTC')

# Zero-Trust Proxy (real API keys stay on VPS, never here)
LITELLM_PROXY_URL=${PROXY_URL}
LITELLM_PROXY_TOKEN=${PROXY_TOKEN}

# Todoist
TODOIST_API_TOKEN=${TODOIST_TOKEN}

# Notion (optional)
NOTION_API_TOKEN=${NOTION_TOKEN}
NOTION_ROOT_PAGE_ID=${NOTION_ROOT}

# Email delivery (optional)
SMTP_HOST=${SMTP_HOST_VAL}
SMTP_PORT=${SMTP_PORT_VAL}
SMTP_USERNAME=${SMTP_USER}
SMTP_PASSWORD=${SMTP_PASS}
DIGEST_FROM_EMAIL=${DIGEST_FROM}
DIGEST_TO_EMAIL=${DIGEST_TO}

# WhatsApp (optional)
WHATSAPP_PHONE_NUMBER=${WHATSAPP_PHONE}
CALLMEBOT_API_KEY=${WHATSAPP_KEY}
"

# Write secrets into container
echo "${SECRETS_CONTENT}" | pct exec "${CTID}" -- bash -c "cat > /etc/brain/secrets.env && chmod 600 /etc/brain/secrets.env && chown root:root /etc/brain/secrets.env"
info "Secrets written to /etc/brain/secrets.env"

# ═══════════════════════════════════════════════════════════════════════
# PHASE 6: NotebookLM Auth
# ═══════════════════════════════════════════════════════════════════════
if [[ "${SKIP_NOTEBOOKLM}" == "false" ]]; then
    step "Phase 6: NotebookLM Authentication"

    if [[ "${PUSH_AUTH}" == "true" || -n "${NLM_COOKIE_PATH}" ]]; then
        # Push auth — transfer cookie file from host to LXC
        COOKIE_SRC="${NLM_COOKIE_PATH}"
        if [[ -z "${COOKIE_SRC}" ]]; then
            # Try common locations
            for candidate in \
                "${HOME}/.notebooklm/storage_state.json" \
                "/root/.notebooklm/storage_state.json"; do
                if [[ -f "${candidate}" ]]; then
                    COOKIE_SRC="${candidate}"
                    break
                fi
            done
        fi

        if [[ -n "${COOKIE_SRC}" && -f "${COOKIE_SRC}" ]]; then
            info "Pushing NotebookLM cookies from ${COOKIE_SRC}"
            pct exec "${CTID}" -- mkdir -p /opt/brain/.notebooklm
            pct push "${CTID}" "${COOKIE_SRC}" /opt/brain/.notebooklm/storage_state.json
            pct exec "${CTID}" -- bash -c "
                chmod 600 /opt/brain/.notebooklm/storage_state.json
                chown brain:brain /opt/brain/.notebooklm/storage_state.json
                chown brain:brain /opt/brain/.notebooklm
                chmod 700 /opt/brain/.notebooklm
            "
            info "Cookie pushed. Enabling NotebookLM timer..."
            pct exec "${CTID}" -- systemctl enable --now brain-notebooklm.timer
        else
            warn "Cookie file not found at: ${COOKIE_SRC:-<not specified>}"
            warn "Falling back to VNC login..."
            PUSH_AUTH=false
        fi
    fi

    if [[ "${PUSH_AUTH}" == "false" && -z "${NLM_COOKIE_PATH}" ]]; then
        info "Starting VNC-based NotebookLM login..."
        info "This will launch a headless Chromium browser in the LXC."

        pct exec "${CTID}" -- bash /opt/brain/scripts/notebooklm-lxc-login.sh &
        VNC_PID=$!

        echo ""
        echo -e "${BOLD}  Connect your VNC viewer to: ${LXC_IP}:5900${NC}"
        echo "  Sign in to Google, then return here and press ENTER."
        echo ""
        read -rp "  Press ENTER after Google sign-in is complete... "

        wait "${VNC_PID}" 2>/dev/null || true

        # Verify cookie was created
        if pct exec "${CTID}" -- test -f /opt/brain/.notebooklm/storage_state.json; then
            info "NotebookLM auth successful."
            pct exec "${CTID}" -- systemctl enable --now brain-notebooklm.timer
        else
            warn "NotebookLM cookie not found. Timer NOT enabled."
            warn "Run later: pct exec ${CTID} -- bash /opt/brain/scripts/notebooklm-lxc-login.sh"
        fi
    fi
else
    step "Phase 6: Skipping NotebookLM"
    info "NotebookLM skipped. Enable later:"
    info "  pct exec ${CTID} -- bash /opt/brain/scripts/notebooklm-lxc-login.sh"
fi

# ═══════════════════════════════════════════════════════════════════════
# PHASE 7: Premium LLM (Optional)
# ═══════════════════════════════════════════════════════════════════════
if [[ "${PREMIUM_LLM}" == "true" ]]; then
    step "Phase 7: Setting Up Premium Nested LXC (Ollama)"
    pct exec "${CTID}" -- bash /opt/brain/deploy/lxc/setup-premium-nested.sh "${LLM_MODEL}"
    info "Ollama running in nested LXC with model: ${LLM_MODEL}"
else
    step "Phase 7: Skipping Premium LLM"
fi

# ═══════════════════════════════════════════════════════════════════════
# PHASE 8: Health Check
# ═══════════════════════════════════════════════════════════════════════
step "Phase 8: Running Health Check"

pct exec "${CTID}" -- bash /opt/brain/scripts/health-check.sh
HC_EXIT=$?

# ═══════════════════════════════════════════════════════════════════════
# DONE
# ═══════════════════════════════════════════════════════════════════════
echo ""
echo -e "${BOLD}╔═══════════════════════════════════════════════════════════╗${NC}"
if [[ ${HC_EXIT} -eq 0 ]]; then
    echo -e "${BOLD}║  ${GREEN}✓  VelaFlow deployment COMPLETE — all checks passed${NC}  ${BOLD}║${NC}"
elif [[ ${HC_EXIT} -eq 2 ]]; then
    echo -e "${BOLD}║  ${YELLOW}⚠  VelaFlow deployed with warnings${NC}                   ${BOLD}║${NC}"
else
    echo -e "${BOLD}║  ${RED}✗  VelaFlow deployed with failures${NC}                   ${BOLD}║${NC}"
fi
echo -e "${BOLD}╠═══════════════════════════════════════════════════════════╣${NC}"
echo -e "${BOLD}║${NC}                                                           ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}  Container:  ${CTID} (${LXC_IP})                              ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}  Proxy:      ${PROXY_URL:-N/A}                               ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}                                                           ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}  Quick test:                                              ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}    pct exec ${CTID} -- sudo -u brain \\                      ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}      /opt/brain/venv/bin/brain daily --stdout               ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}                                                           ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}  Timers:                                                  ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}    pct exec ${CTID} -- systemctl list-timers 'brain-*'      ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}                                                           ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}  Logs:                                                    ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}    pct exec ${CTID} -- journalctl -u brain-daily -f         ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}                                                           ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}  Re-check:                                                ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}    pct exec ${CTID} -- bash /opt/brain/scripts/health-check.sh ${BOLD}║${NC}"
echo -e "${BOLD}║${NC}                                                           ${BOLD}║${NC}"
echo -e "${BOLD}╚═══════════════════════════════════════════════════════════╝${NC}"
echo ""

exit ${HC_EXIT}
