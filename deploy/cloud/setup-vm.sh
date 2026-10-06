#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# VelaFlow — Cloud VM Deployment Script
# Targets: Oracle Cloud Free Tier (ARM A1 Flex, 4 OCPU, 24 GB RAM)
#          Also works on any Ubuntu 22.04+ VM with Docker installed.
#
# Usage:
#   1. SSH into your VM
#   2. Clone the repo: git clone <your-repo-url> /opt/velaflow
#   3. cd /opt/velaflow
#   4. Copy config: cp config/.env.production.example config/.env
#   5. Edit config/.env with your secrets
#   6. Run: sudo bash deploy/cloud/setup-vm.sh
#
# What this script does:
#   - Installs Docker, Docker Compose, Caddy (reverse proxy + auto-HTTPS)
#   - Creates a non-root velaflow user
#   - Configures firewall (UFW) — only 80, 443, 22
#   - Sets up Caddy for automatic TLS (Let's Encrypt)
#   - Starts all services via docker-compose
#   - Enables auto-restart on reboot
# ═══════════════════════════════════════════════════════════════════════
set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────
INSTALL_DIR="${INSTALL_DIR:-/opt/velaflow}"
VELAFLOW_USER="${VELAFLOW_USER:-velaflow}"
DOMAIN="${DOMAIN:-}"  # Set via env or config/.env

# ── Pre-flight ────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: Run as root (sudo bash $0)" >&2
    exit 1
fi

if [[ ! -f "${INSTALL_DIR}/docker-compose.yml" ]]; then
    echo "ERROR: ${INSTALL_DIR}/docker-compose.yml not found." >&2
    echo "Clone the repo to ${INSTALL_DIR} first." >&2
    exit 1
fi

if [[ ! -f "${INSTALL_DIR}/config/.env" ]]; then
    echo "ERROR: ${INSTALL_DIR}/config/.env not found." >&2
    echo "Copy config/.env.production.example → config/.env and fill in secrets." >&2
    exit 1
fi

# Load domain from .env if not set
if [[ -z "$DOMAIN" ]]; then
    DOMAIN=$(grep -E '^VELAFLOW_DOMAIN=' "${INSTALL_DIR}/config/.env" | cut -d= -f2 | tr -d '"' | tr -d "'")
fi
if [[ -z "$DOMAIN" ]]; then
    echo "ERROR: VELAFLOW_DOMAIN not set in config/.env or environment." >&2
    exit 1
fi

echo "═══════════════════════════════════════════════════════════════"
echo " VelaFlow Cloud Deployment"
echo " Domain: ${DOMAIN}"
echo " Install dir: ${INSTALL_DIR}"
echo "═══════════════════════════════════════════════════════════════"

# ── 1. System packages ───────────────────────────────────────────────
echo "[1/7] Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq \
    apt-transport-https \
    ca-certificates \
    curl \
    gnupg \
    lsb-release \
    ufw \
    fail2ban \
    unattended-upgrades

# ── 2. Docker ────────────────────────────────────────────────────────
echo "[2/7] Installing Docker..."
if ! command -v docker &>/dev/null; then
    curl -fsSL https://get.docker.com | sh
fi
systemctl enable docker
systemctl start docker

# ── 3. Caddy (reverse proxy + auto-HTTPS) ───────────────────────────
echo "[3/7] Installing Caddy..."
if ! command -v caddy &>/dev/null; then
    apt-get install -y -qq debian-keyring debian-archive-keyring
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq
    apt-get install -y -qq caddy
fi

# ── 4. Create velaflow user ─────────────────────────────────────────
echo "[4/7] Setting up ${VELAFLOW_USER} user..."
if ! id "${VELAFLOW_USER}" &>/dev/null; then
    useradd --system --create-home --shell /usr/sbin/nologin "${VELAFLOW_USER}"
fi
usermod -aG docker "${VELAFLOW_USER}"
chown -R "${VELAFLOW_USER}:${VELAFLOW_USER}" "${INSTALL_DIR}"

# ── 5. Firewall ──────────────────────────────────────────────────────
echo "[5/7] Configuring firewall..."
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp    # SSH
ufw allow 80/tcp    # HTTP (Caddy redirect)
ufw allow 443/tcp   # HTTPS (Caddy)
ufw --force enable

# ── 6. Caddy config ─────────────────────────────────────────────────
echo "[6/7] Configuring Caddy reverse proxy..."
cat > /etc/caddy/Caddyfile <<CADDY_EOF
# VelaFlow — Caddy Reverse Proxy with Automatic HTTPS
# Caddy auto-obtains Let's Encrypt TLS certificates.

${DOMAIN} {
    # VelaFlow API
    handle /api/* {
        reverse_proxy localhost:8000
    }
    handle /health {
        reverse_proxy localhost:8000
    }
    handle /docs {
        reverse_proxy localhost:8000
    }
    handle /openapi.json {
        reverse_proxy localhost:8000
    }

    # n8n (behind OAuth2 Proxy)
    handle /n8n/* {
        uri strip_prefix /n8n
        reverse_proxy localhost:4180
    }

    # Default: API
    handle {
        reverse_proxy localhost:8000
    }

    # Security headers
    header {
        Strict-Transport-Security "max-age=63072000; includeSubDomains; preload"
        X-Content-Type-Options "nosniff"
        X-Frame-Options "DENY"
        Referrer-Policy "strict-origin-when-cross-origin"
        -Server
    }

    log {
        output file /var/log/caddy/velaflow.log {
            roll_size 10mb
            roll_keep 5
        }
    }
}
CADDY_EOF

mkdir -p /var/log/caddy
systemctl enable caddy
systemctl restart caddy

# ── 7. Start services ───────────────────────────────────────────────
echo "[7/7] Starting VelaFlow services..."
cd "${INSTALL_DIR}"

# Generate secrets if missing
if ! grep -q '^JWT_SECRET=' config/.env 2>/dev/null || grep -q 'JWT_SECRET=$' config/.env; then
    echo "JWT_SECRET=$(openssl rand -hex 32)" >> config/.env
fi
if ! grep -q '^VELAFLOW_MASTER_KEY=' config/.env 2>/dev/null || grep -q 'VELAFLOW_MASTER_KEY=$' config/.env; then
    echo "VELAFLOW_MASTER_KEY=$(openssl rand -hex 32)" >> config/.env
fi
if ! grep -q '^N8N_ENCRYPTION_KEY=' config/.env 2>/dev/null || grep -q 'N8N_ENCRYPTION_KEY=$' config/.env; then
    echo "N8N_ENCRYPTION_KEY=$(openssl rand -hex 16)" >> config/.env
fi
if ! grep -q '^REDIS_PASSWORD=' config/.env 2>/dev/null || grep -q 'REDIS_PASSWORD=$' config/.env; then
    echo "REDIS_PASSWORD=$(openssl rand -hex 24)" >> config/.env
fi
if ! grep -q '^OAUTH2_PROXY_COOKIE_SECRET=' config/.env 2>/dev/null || grep -q 'OAUTH2_PROXY_COOKIE_SECRET=$' config/.env; then
    echo "OAUTH2_PROXY_COOKIE_SECRET=$(openssl rand -base64 32 | tr -d '\n')" >> config/.env
fi

# Set CORS for the domain
if ! grep -q '^CORS_ALLOWED_ORIGINS=' config/.env; then
    echo "CORS_ALLOWED_ORIGINS=https://${DOMAIN}" >> config/.env
fi

# Start
sudo -u "${VELAFLOW_USER}" docker compose --env-file config/.env up -d --build

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo " VelaFlow deployed successfully!"
echo ""
echo " API:  https://${DOMAIN}/api/v1/tenants"
echo " n8n:  https://${DOMAIN}/n8n/"
echo " Docs: https://${DOMAIN}/docs"
echo ""
echo " Next steps:"
echo "  1. Point your domain DNS A record to this VM's public IP"
echo "  2. Caddy will auto-obtain TLS certificates"
echo "  3. Register: POST https://${DOMAIN}/api/v1/tenants"
echo "  4. Update secrets via n8n Secrets Manager workflow"
echo "═══════════════════════════════════════════════════════════════"
