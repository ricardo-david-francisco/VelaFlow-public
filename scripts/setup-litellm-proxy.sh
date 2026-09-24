#!/usr/bin/env bash
# setup-litellm-proxy.sh — Deploy LiteLLM proxy on a fresh Debian/Ubuntu VPS
#
# Run as root or with sudo on your personal VPS (NOT inside an LXC you share).
# This script installs Docker, deploys LiteLLM, and configures Nginx + Let's Encrypt.
#
# Usage:
#   chmod +x scripts/setup-litellm-proxy.sh
#   sudo ./scripts/setup-litellm-proxy.sh \
#       --domain proxy.yourdomain.com \
#       --master-key <your-master-key> \
#       --gemini-key AIzaSy...
#
# After setup:
#   - LiteLLM API available at https://proxy.yourdomain.com
#   - Dashboard at https://proxy.yourdomain.com/ui
#   - Generate demo tokens via the dashboard or the API (see docs/security.md)

set -euo pipefail

# ── Argument parsing ──────────────────────────────────────────────────────────
DOMAIN=""
MASTER_KEY=""
GEMINI_KEY=""
OPENAI_KEY=""
GROQ_KEY=""
EMAIL="admin@yourdomain.com"

usage() {
    echo "Usage: $0 --domain DOMAIN --master-key KEY --gemini-key KEY [--openai-key KEY] [--groq-key KEY] [--email EMAIL]"
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --domain)     DOMAIN="$2";     shift 2 ;;
        --master-key) MASTER_KEY="$2"; shift 2 ;;
        --gemini-key) GEMINI_KEY="$2"; shift 2 ;;
        --openai-key) OPENAI_KEY="$2"; shift 2 ;;
        --groq-key)   GROQ_KEY="$2";   shift 2 ;;
        --email)      EMAIL="$2";      shift 2 ;;
        *) usage ;;
    esac
done

[[ -z "$DOMAIN" || -z "$MASTER_KEY" || -z "$GEMINI_KEY" ]] && usage

echo "==> Setting up LiteLLM proxy on $DOMAIN"

# ── Install Docker ────────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    echo "==> Installing Docker..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
fi

# ── Create working directory ──────────────────────────────────────────────────
WORKDIR="/opt/litellm-proxy"
mkdir -p "$WORKDIR"
cd "$WORKDIR"

# ── LiteLLM config.yaml ───────────────────────────────────────────────────────
cat > config.yaml << LITELLM_CONFIG
model_list:
  - model_name: gemini/gemini-2.5-flash
    litellm_params:
      model: gemini/gemini-2.5-flash
      api_key: os.environ/GEMINI_API_KEY

  - model_name: gemini/gemini-2.5-pro
    litellm_params:
      model: gemini/gemini-2.5-pro
      api_key: os.environ/GEMINI_API_KEY

  - model_name: gemini/gemini-2.5-flash-lite
    litellm_params:
      model: gemini/gemini-2.5-flash-lite-preview-06-17
      api_key: os.environ/GEMINI_API_KEY
LITELLM_CONFIG

if [[ -n "$OPENAI_KEY" ]]; then
cat >> config.yaml << EOF
  - model_name: gpt-4o-mini
    litellm_params:
      model: openai/gpt-4o-mini
      api_key: os.environ/OPENAI_API_KEY
EOF
fi

if [[ -n "$GROQ_KEY" ]]; then
cat >> config.yaml << EOF
  - model_name: groq/llama-3.3-70b
    litellm_params:
      model: groq/llama-3.3-70b-versatile
      api_key: os.environ/GROQ_API_KEY
EOF
fi

cat >> config.yaml << LITELLM_SETTINGS

litellm_settings:
  success_callback: []
  failure_callback: []
  request_timeout: 90
  num_retries: 2

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
  database_url: "sqlite:///./litellm.db"
  store_model_in_db: true
LITELLM_SETTINGS

# ── .env for the container ────────────────────────────────────────────────────
cat > .env << ENV
LITELLM_MASTER_KEY=${MASTER_KEY}
GEMINI_API_KEY=${GEMINI_KEY}
OPENAI_API_KEY=${OPENAI_KEY:-}
GROQ_API_KEY=${GROQ_KEY:-}
ENV

chmod 600 .env

# ── docker-compose.yml ────────────────────────────────────────────────────────
cat > docker-compose.yml << COMPOSE
services:
  litellm:
    image: ghcr.io/berriai/litellm:main-latest
    restart: unless-stopped
    ports:
      - "127.0.0.1:4000:4000"
    env_file: .env
    volumes:
      - ./config.yaml:/app/config.yaml:ro
      - ./litellm.db:/app/litellm.db
    command: ["--config", "/app/config.yaml", "--port", "4000", "--num_workers", "4"]
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:4000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
COMPOSE

docker compose up -d
echo "==> LiteLLM container started on 127.0.0.1:4000"

# ── Install Nginx + Certbot ───────────────────────────────────────────────────
if ! command -v nginx &>/dev/null; then
    echo "==> Installing Nginx and Certbot..."
    apt-get update -qq
    apt-get install -y nginx certbot python3-certbot-nginx
fi

# ── Nginx config ──────────────────────────────────────────────────────────────
cat > /etc/nginx/sites-available/litellm << NGINX
server {
    listen 80;
    server_name ${DOMAIN};

    location / {
        proxy_pass http://127.0.0.1:4000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_read_timeout 120s;
    }
}
NGINX

ln -sf /etc/nginx/sites-available/litellm /etc/nginx/sites-enabled/litellm
nginx -t
systemctl reload nginx

# ── Let's Encrypt TLS ─────────────────────────────────────────────────────────
echo "==> Obtaining TLS certificate for $DOMAIN..."
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$EMAIL"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "====================================================="
echo " LiteLLM proxy is live at: https://${DOMAIN}"
echo " Dashboard:                https://${DOMAIN}/ui"
echo " Master key:               ${MASTER_KEY}"
echo "====================================================="
echo ""
echo "To generate a demo token (48h, \$1 budget, 5 req/min):"
echo ""
echo "  curl -X POST https://${DOMAIN}/key/generate \\"
echo "    -H 'Authorization: Bearer ${MASTER_KEY}' \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -d '{"
echo "      \"key_alias\": \"demo-company-2026-04\","
echo "      \"max_budget\": 1.00,"
echo "      \"duration\": \"48h\","
echo "      \"rpm_limit\": 5"
echo "    }'"
echo ""
echo "Inject the returned token into the demo LXC as:"
echo "  LITELLM_PROXY_TOKEN=sk-demo-<returned-token>"
echo "  LITELLM_PROXY_URL=https://${DOMAIN}"
echo "  DEMO_MODE=true"
echo ""
echo "See docs/security.md for the full Zero-Trust architecture."
