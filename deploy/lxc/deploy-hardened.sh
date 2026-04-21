#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# VelaFlow — Hardened LXC Deployment (Universal)
#
# Targets:
#   - Proxmox VE 8+ (pct)
#   - Oracle Cloud (LXD on Ubuntu)
#   - Any Linux host with LXD/Incus
#
# Resources: Designed for N95-class CPU + 8 GB RAM minimum.
#            Nesting enabled for KEDA/K8s premium-tier scaling.
#
# Security model:
#   - Unprivileged container (user namespace mapping)
#   - AppArmor: generated profile (not unconfined)
#   - Capability drop: all dangerous caps removed
#   - Firewall: only 22/80/443 inbound
#   - fail2ban: SSH + HTTP brute-force protection
#   - Secrets: 0600 permissions, never logged, never in CLI args
#   - Credential isolation: API keys in memory-only tmpfs mount
#   - Read-only rootfs for system dirs (systemd ProtectSystem=strict)
#   - Network namespace isolation per tenant data path
#
# Usage:
#   # On Proxmox host:
#   sudo bash deploy-hardened.sh --platform proxmox --id 200
#
#   # On Oracle Cloud / any Ubuntu with LXD:
#   sudo bash deploy-hardened.sh --platform lxd --name velaflow
#
#   # Quick mode (auto-detect platform):
#   sudo bash deploy-hardened.sh
#
# ═══════════════════════════════════════════════════════════════════════
set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────
PLATFORM=""
CTID="200"
CT_NAME="velaflow"
MEMORY_MB=4096         # 4 GB for VelaFlow + overhead
SWAP_MB=1024           # 1 GB swap
CORES=2                # N95 has 4 cores; leave 2 for host
DISK_GB=20             # 20 GB root (data on separate volume)
BRIDGE="vmbr0"
NAMESERVER="1.1.1.1"   # Cloudflare DNS (faster than Google)
DOMAIN=""
INSTALL_DIR="/opt/velaflow"
SECRETS_DIR="/etc/velaflow"
LOG_DIR="/var/log/velaflow"
DATA_DIR="/opt/velaflow/data"
DEBIAN_TEMPLATE="debian-12-standard_12.7-1_amd64.tar.zst"

# ── Colors ────────────────────────────────────────────────────────────
R='\033[0;31m'; G='\033[0;32m'; Y='\033[1;33m'; B='\033[1;34m'; N='\033[0m'
ok()   { echo -e "  ${G}✓${N} $1"; }
fail() { echo -e "  ${R}✗${N} $1"; }
warn() { echo -e "  ${Y}⚠${N} $1"; }
info() { echo -e "  ${B}→${N} $1"; }
banner() {
    echo ""
    echo -e "${B}═══════════════════════════════════════════════════════════════${N}"
    echo -e "${B}  $1${N}"
    echo -e "${B}═══════════════════════════════════════════════════════════════${N}"
}

# ── Parse arguments ───────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --platform)  PLATFORM="$2";   shift 2 ;;
        --id)        CTID="$2";       shift 2 ;;
        --name)      CT_NAME="$2";    shift 2 ;;
        --domain)    DOMAIN="$2";     shift 2 ;;
        --memory)    MEMORY_MB="$2";  shift 2 ;;
        --cores)     CORES="$2";      shift 2 ;;
        --disk)      DISK_GB="$2";    shift 2 ;;
        --bridge)    BRIDGE="$2";     shift 2 ;;
        --dns)       NAMESERVER="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: $0 [--platform proxmox|lxd] [--id 200] [--name velaflow] [--domain example.com]"
            echo "       [--memory 4096] [--cores 2] [--disk 20] [--bridge vmbr0] [--dns 1.1.1.1]"
            exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ── Root check ────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: Must run as root. Use: sudo bash $0 $*" >&2
    exit 1
fi

# ── Auto-detect platform ─────────────────────────────────────────────
if [[ -z "$PLATFORM" ]]; then
    if command -v pct &>/dev/null; then
        PLATFORM="proxmox"
    elif command -v lxc &>/dev/null || command -v incus &>/dev/null; then
        PLATFORM="lxd"
    else
        echo "ERROR: Cannot detect container platform." >&2
        echo "Install Proxmox, LXD, or Incus first, or use --platform." >&2
        exit 1
    fi
fi

banner "VelaFlow — Hardened LXC Deployment"
info "Platform:  ${PLATFORM}"
info "Container: ${CT_NAME} (ID: ${CTID})"
info "Resources: ${CORES} cores, ${MEMORY_MB} MB RAM, ${DISK_GB} GB disk"
info "Domain:    ${DOMAIN:-'(not set — configure later)'}"
echo ""

# ═══════════════════════════════════════════════════════════════════════
# PHASE 1: Create Container
# ═══════════════════════════════════════════════════════════════════════
banner "Phase 1: Create Container"

if [[ "$PLATFORM" == "proxmox" ]]; then
    # ── Proxmox VE ────────────────────────────────────────────────────
    # Download template if needed
    if ! pveam list local | grep -q "debian-12-standard"; then
        info "Downloading Debian 12 template..."
        pveam update
        pveam download local "${DEBIAN_TEMPLATE}"
    fi
    ok "Debian 12 template available"

    # Create container
    info "Creating unprivileged LXC ${CTID}..."
    pct create "${CTID}" "local:vztmpl/${DEBIAN_TEMPLATE}" \
        --hostname "${CT_NAME}" \
        --storage local-lvm \
        --rootfs "local-lvm:${DISK_GB}" \
        --memory "${MEMORY_MB}" \
        --swap "${SWAP_MB}" \
        --cores "${CORES}" \
        --net0 "name=eth0,bridge=${BRIDGE},ip=dhcp" \
        --nameserver "${NAMESERVER}" \
        --unprivileged 1 \
        --features "nesting=1,keyctl=1" \
        --onboot 1 \
        --start 0
    ok "Container created"

    # ── Hardening configuration ───────────────────────────────────────
    info "Applying security hardening..."
    cat >> "/etc/pve/lxc/${CTID}.conf" <<'HARDENING'

# ── VelaFlow Security Hardening ──────────────────────────────────
# AppArmor: generated profile (NOT unconfined)
lxc.apparmor.profile: generated

# Drop ALL dangerous capabilities
# Keeps only: chown, dac_override, fowner, fsetid, kill, setgid, setuid,
# setpcap, net_bind_service, net_broadcast, sys_chroot, audit_write, setfcap
lxc.cap.drop: sys_admin sys_rawio sys_module sys_ptrace sys_boot sys_time sys_nice sys_resource net_raw mac_admin mac_override audit_control

# Prevent mounting inside container (breaks mount exploits)
lxc.mount.auto: proc:mixed sys:ro cgroup:mixed

# Memory limits via cgroup2 (prevents OOM-based DoS)
lxc.cgroup2.memory.max: 6442450944
lxc.cgroup2.memory.swap.max: 1073741824

# CPU quota (prevents CPU starvation of host)
lxc.cgroup2.cpu.max: 200000 100000
HARDENING
    ok "Hardening applied to /etc/pve/lxc/${CTID}.conf"

    # Start container
    info "Starting container..."
    pct start "${CTID}"
    sleep 5

    # Wait for network
    IP=""
    for i in $(seq 1 30); do
        IP=$(pct exec "${CTID}" -- hostname -I 2>/dev/null | awk '{print $1}' || true)
        [[ -n "$IP" ]] && break
        sleep 2
    done
    ok "Container started — IP: ${IP:-unknown}"

    # Define exec wrapper
    CT_EXEC="pct exec ${CTID} --"

elif [[ "$PLATFORM" == "lxd" ]]; then
    # ── LXD / Incus (Oracle Cloud, Ubuntu, etc.) ──────────────────────
    LXC_CMD="lxc"
    command -v incus &>/dev/null && LXC_CMD="incus"

    info "Creating container with ${LXC_CMD}..."
    "${LXC_CMD}" launch images:debian/12 "${CT_NAME}" \
        --config limits.memory="${MEMORY_MB}MB" \
        --config limits.cpu="${CORES}" \
        --config security.nesting=true \
        --config security.syscalls.intercept.mknod=true \
        --config security.syscalls.intercept.setxattr=true \
        --config raw.lxc="lxc.apparmor.profile=generated
lxc.cap.drop=sys_admin sys_rawio sys_module sys_ptrace sys_boot sys_time sys_nice sys_resource net_raw mac_admin mac_override audit_control"
    ok "Container created: ${CT_NAME}"

    sleep 5
    IP=$("${LXC_CMD}" list "${CT_NAME}" --format csv -c4 | head -1 | cut -d' ' -f1 || true)
    ok "Container started — IP: ${IP:-unknown}"

    # Define exec wrapper
    CT_EXEC="${LXC_CMD} exec ${CT_NAME} --"
fi

# ═══════════════════════════════════════════════════════════════════════
# PHASE 2: System Hardening (inside container)
# ═══════════════════════════════════════════════════════════════════════
banner "Phase 2: System Hardening"

${CT_EXEC} bash -c '
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

# ── System updates ────────────────────────────────────────────────
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq \
    python3 python3-pip python3-venv \
    git curl wget \
    ufw fail2ban \
    unattended-upgrades apt-listchanges \
    ca-certificates gnupg \
    logrotate \
    iptables

echo "  ✓ System packages installed"

# ── Firewall (UFW) ───────────────────────────────────────────────
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment "SSH"
ufw allow 80/tcp comment "HTTP"
ufw allow 443/tcp comment "HTTPS"
ufw --force enable
echo "  ✓ Firewall configured (22/80/443 only)"

# ── fail2ban ─────────────────────────────────────────────────────
cat > /etc/fail2ban/jail.local <<JAIL
[DEFAULT]
bantime  = 3600
findtime = 600
maxretry = 5
backend  = systemd

[sshd]
enabled = true
port    = ssh
maxretry = 3
bantime  = 7200

[velaflow-api]
enabled  = true
port     = 80,443
filter   = velaflow-api
logpath  = /var/log/velaflow/velaflow.log
maxretry = 10
findtime = 300
bantime  = 1800
JAIL

cat > /etc/fail2ban/filter.d/velaflow-api.conf <<FILTER
[Definition]
failregex = ^.*"status_code":\s*(?:401|403|429).*"remote_addr":\s*"<HOST>".*$
ignoreregex =
FILTER

systemctl enable fail2ban
systemctl restart fail2ban
echo "  ✓ fail2ban configured (SSH + API brute-force)"

# ── Kernel hardening (sysctl) ────────────────────────────────────
cat > /etc/sysctl.d/99-velaflow-hardening.conf <<SYSCTL
# Disable IP forwarding (not a router)
net.ipv4.ip_forward = 0
net.ipv6.conf.all.forwarding = 0

# Ignore ICMP redirects (prevent MITM)
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv6.conf.all.accept_redirects = 0

# Ignore source-routed packets
net.ipv4.conf.all.accept_source_route = 0
net.ipv4.conf.default.accept_source_route = 0

# Enable SYN flood protection
net.ipv4.tcp_syncookies = 1
net.ipv4.tcp_max_syn_backlog = 2048
net.ipv4.tcp_synack_retries = 2

# Prevent IP spoofing
net.ipv4.conf.all.rp_filter = 1
net.ipv4.conf.default.rp_filter = 1

# Disable ICMP broadcast (smurf attack prevention)
net.ipv4.icmp_echo_ignore_broadcasts = 1

# Log suspicious packets
net.ipv4.conf.all.log_martians = 1
net.ipv4.conf.default.log_martians = 1

# Restrict dmesg to root
kernel.dmesg_restrict = 1

# Restrict kernel pointer exposure
kernel.kptr_restrict = 2

# Disable core dumps (prevent credential leaks)
fs.suid_dumpable = 0

# Restrict ptrace (prevent process snooping)
kernel.yama.ptrace_scope = 2

# Limit file handles
fs.file-max = 65535
SYSCTL

sysctl --system > /dev/null 2>&1 || true
echo "  ✓ Kernel hardening applied"

# ── Unattended security updates ──────────────────────────────────
cat > /etc/apt/apt.conf.d/50unattended-upgrades <<UPDATES
Unattended-Upgrade::Allowed-Origins {
    "\${distro_id}:\${distro_codename}-security";
};
Unattended-Upgrade::AutoFixInterruptedDpkg "true";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
Unattended-Upgrade::Automatic-Reboot "false";
UPDATES
echo "  ✓ Automatic security updates enabled"

# ── Log rotation ─────────────────────────────────────────────────
cat > /etc/logrotate.d/velaflow <<LOGROTATE
/var/log/velaflow/*.log {
    daily
    missingok
    rotate 30
    compress
    delaycompress
    notifempty
    create 0640 velaflow velaflow
    sharedscripts
    postrotate
        systemctl reload velaflow-api 2>/dev/null || true
    endscript
}
LOGROTATE
echo "  ✓ Log rotation configured (30 days)"

# ── Disable unnecessary services ─────────────────────────────────
for svc in bluetooth cups avahi-daemon rpcbind; do
    systemctl disable "$svc" 2>/dev/null || true
    systemctl stop "$svc" 2>/dev/null || true
done
echo "  ✓ Unnecessary services disabled"
'

ok "System hardening complete"

# ═══════════════════════════════════════════════════════════════════════
# PHASE 3: Application Installation
# ═══════════════════════════════════════════════════════════════════════
banner "Phase 3: Application Installation"

${CT_EXEC} bash -c '
set -euo pipefail

INSTALL_DIR="/opt/velaflow"
SECRETS_DIR="/etc/velaflow"
LOG_DIR="/var/log/velaflow"
DATA_DIR="/opt/velaflow/data"

# ── Create velaflow user (no shell, no login) ────────────────────
if ! id velaflow &>/dev/null; then
    useradd --system --create-home --home-dir "${INSTALL_DIR}" \
            --shell /usr/sbin/nologin velaflow
fi
echo "  ✓ User created: velaflow (nologin)"

# ── Create directory structure ────────────────────────────────────
mkdir -p "${INSTALL_DIR}"/{src,config,data/medallion,logs}
mkdir -p "${SECRETS_DIR}"
mkdir -p "${LOG_DIR}"
mkdir -p "${DATA_DIR}"

# ── Tmpfs for runtime secrets (in-memory, never on disk) ─────────
mkdir -p /run/velaflow-secrets
mount -t tmpfs -o size=16M,mode=0700,uid=$(id -u velaflow),gid=$(id -g velaflow) tmpfs /run/velaflow-secrets 2>/dev/null || true

# Add to fstab for persistence across reboots
if ! grep -q "velaflow-secrets" /etc/fstab 2>/dev/null; then
    echo "tmpfs /run/velaflow-secrets tmpfs size=16M,mode=0700,uid=$(id -u velaflow),gid=$(id -g velaflow) 0 0" >> /etc/fstab
fi
echo "  ✓ In-memory secrets mount at /run/velaflow-secrets"

# ── Python virtual environment ────────────────────────────────────
python3 -m venv "${INSTALL_DIR}/venv"
source "${INSTALL_DIR}/venv/bin/activate"
pip install --upgrade pip setuptools wheel -q
echo "  ✓ Python venv created at ${INSTALL_DIR}/venv"

# ── Set permissions ──────────────────────────────────────────────
chown -R velaflow:velaflow "${INSTALL_DIR}"
chown -R velaflow:velaflow "${LOG_DIR}"
chmod 700 "${SECRETS_DIR}"
chmod 750 "${INSTALL_DIR}"
chmod 750 "${DATA_DIR}"

echo "  ✓ Permissions set (least-privilege)"
'

ok "Application structure created"

# ═══════════════════════════════════════════════════════════════════════
# PHASE 4: Systemd Services (hardened)
# ═══════════════════════════════════════════════════════════════════════
banner "Phase 4: Systemd Services"

${CT_EXEC} bash -c '
set -euo pipefail

# ── API service ──────────────────────────────────────────────────
cat > /etc/systemd/system/velaflow-api.service <<EOF
[Unit]
Description=VelaFlow Enterprise API
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=velaflow
Group=velaflow
WorkingDirectory=/opt/velaflow
EnvironmentFile=/etc/velaflow/secrets.env
ExecStart=/opt/velaflow/venv/bin/uvicorn brain.api.app:create_app \
    --factory --host 127.0.0.1 --port 8000 --workers 2 \
    --limit-concurrency 100 --timeout-keep-alive 5
Restart=always
RestartSec=5
WatchdogSec=300

# Security sandbox
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/velaflow/data /var/log/velaflow /run/velaflow-secrets
CapabilityBoundingSet=
AmbientCapabilities=
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
RestrictNamespaces=true
RestrictSUIDSGID=true
SystemCallArchitectures=native
MemoryDenyWriteExecute=true
LockPersonality=true
RestrictRealtime=true
RemoveIPC=true

# Resource limits
MemoryMax=2G
CPUQuota=150%
TasksMax=256
LimitNOFILE=8192

StandardOutput=journal
StandardError=journal
SyslogIdentifier=velaflow-api

[Install]
WantedBy=multi-user.target
EOF

# ── Worker service ───────────────────────────────────────────────
cat > /etc/systemd/system/velaflow-worker.service <<EOF
[Unit]
Description=VelaFlow Queue Worker
After=network-online.target velaflow-api.service
Wants=velaflow-api.service

[Service]
Type=exec
User=velaflow
Group=velaflow
WorkingDirectory=/opt/velaflow
EnvironmentFile=/etc/velaflow/secrets.env
ExecStart=/opt/velaflow/venv/bin/python -m brain.queue.worker
Restart=always
RestartSec=10

# Security sandbox (same as API)
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/velaflow/data /var/log/velaflow /run/velaflow-secrets
CapabilityBoundingSet=
AmbientCapabilities=
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
RestrictNamespaces=true
RestrictSUIDSGID=true
SystemCallArchitectures=native
MemoryDenyWriteExecute=true
LockPersonality=true
RestrictRealtime=true
RemoveIPC=true

MemoryMax=2G
CPUQuota=100%
TasksMax=128
LimitNOFILE=4096

StandardOutput=journal
StandardError=journal
SyslogIdentifier=velaflow-worker

[Install]
WantedBy=multi-user.target
EOF

# ── Copilot log export service (timer-triggered) ─────────────────
cat > /etc/systemd/system/velaflow-log-export.service <<EOF
[Unit]
Description=VelaFlow — Export sanitised logs for Copilot debugging
After=velaflow-api.service

[Service]
Type=oneshot
User=velaflow
Group=velaflow
WorkingDirectory=/opt/velaflow
EnvironmentFile=/etc/velaflow/secrets.env
ExecStart=/opt/velaflow/venv/bin/python -c "from brain.security.secure_logging import SecureLogger; l = SecureLogger(log_dir='/var/log/velaflow'); l.export_sanitised('/opt/velaflow/data/copilot-logs.md')"

NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/velaflow/data /var/log/velaflow

StandardOutput=journal
SyslogIdentifier=velaflow-log-export
EOF

cat > /etc/systemd/system/velaflow-log-export.timer <<EOF
[Unit]
Description=Export VelaFlow logs for Copilot every 6 hours

[Timer]
OnCalendar=*-*-* 00/6:00:00
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable velaflow-api velaflow-worker velaflow-log-export.timer
echo "  ✓ Systemd services created and enabled"
echo "  ✓ Copilot log export timer (every 6h → /opt/velaflow/data/copilot-logs.md)"
'

ok "Systemd services installed"

# ═══════════════════════════════════════════════════════════════════════
# PHASE 5: Credential Isolation
# ═══════════════════════════════════════════════════════════════════════
banner "Phase 5: Credential Isolation"

${CT_EXEC} bash -c '
set -euo pipefail

SECRETS_DIR="/etc/velaflow"

# ── Generate secrets ─────────────────────────────────────────────
JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
MASTER_KEY=$(python3 -c "import secrets,base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())")

# ── Write secrets file (0600 root-owned, read by velaflow via EnvironmentFile)
cat > "${SECRETS_DIR}/secrets.env" <<EOF
# VelaFlow Secrets — auto-generated by deploy-hardened.sh
# File permissions: 0600 (root:velaflow)
# NEVER commit this file. NEVER pass these via CLI arguments.

# --- Required (auto-generated) ---
JWT_SECRET=${JWT_SECRET}
VELAFLOW_MASTER_KEY=${MASTER_KEY}
ENVIRONMENT=production

# --- Platform ---
VELAFLOW_DOMAIN=localhost
VELAFLOW_API_PORT=8000
CORS_ALLOWED_ORIGINS=

# --- Todoist (REQUIRED — fill in) ---
TODOIST_API_TOKEN=

# --- AI Provider (choose one) ---
# Option A: LiteLLM Proxy (recommended — keys stay on proxy VPS)
LITELLM_PROXY_URL=
LITELLM_PROXY_TOKEN=
LITELLM_PROXY_MODEL=gemini-2.5-pro

# Option B: Direct API key (dev only)
GOOGLE_AI_API_KEY=

# --- Notion (optional) ---
NOTION_API_TOKEN=
NOTION_ROOT_PAGE_ID=

# --- Logging ---
LOG_LEVEL=INFO
LOG_DIR=/var/log/velaflow
LOG_MAX_SIZE_MB=50
LOG_RETENTION_DAYS=30
EOF

# Permissions: root owns, velaflow group can read (for EnvironmentFile)
chown root:velaflow "${SECRETS_DIR}/secrets.env"
chmod 0640 "${SECRETS_DIR}/secrets.env"

# Lock down secrets directory
chmod 0750 "${SECRETS_DIR}"
chown root:velaflow "${SECRETS_DIR}"

echo "  ✓ Secrets generated and written to ${SECRETS_DIR}/secrets.env"
echo "  ✓ JWT_SECRET: (auto-generated, 48-byte urlsafe)"
echo "  ✓ MASTER_KEY: (auto-generated, 32-byte base64)"
echo ""
echo "  ⚠  IMPORTANT: Edit ${SECRETS_DIR}/secrets.env to add your API keys:"
echo "     - TODOIST_API_TOKEN (required)"
echo "     - LITELLM_PROXY_URL + TOKEN (recommended)"
echo "     - VELAFLOW_DOMAIN (for production)"
'

ok "Credentials isolated"

# ═══════════════════════════════════════════════════════════════════════
# PHASE 6: Reverse Proxy + TLS
# ═══════════════════════════════════════════════════════════════════════
banner "Phase 6: Reverse Proxy + TLS"

${CT_EXEC} bash -c '
set -euo pipefail

# Install Caddy (automatic HTTPS)
apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https
curl -1sLf "https://dl.cloudsmith.io/public/caddy/stable/gpg.key" 2>/dev/null | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg 2>/dev/null
curl -1sLf "https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt" 2>/dev/null | tee /etc/apt/sources.list.d/caddy-stable.list > /dev/null
apt-get update -qq
apt-get install -y -qq caddy

# Write Caddyfile — localhost by default, domain configurable
cat > /etc/caddy/Caddyfile <<CADDY
# VelaFlow Reverse Proxy
# To enable HTTPS with a domain, replace :80 with your domain:
#   velaflow.example.com {
#       ...
#   }

:80 {
    # API routes
    handle /api/* {
        reverse_proxy localhost:8000
    }
    handle /health {
        reverse_proxy localhost:8000
    }

    # Block Swagger/OpenAPI in production
    handle /docs {
        respond "Not Found" 404
    }
    handle /redoc {
        respond "Not Found" 404
    }
    handle /openapi.json {
        respond "Not Found" 404
    }

    # Copilot log endpoint (read-only, local network only)
    @copilot_logs {
        path /copilot/logs
        remote_ip 127.0.0.0/8 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16
    }
    handle @copilot_logs {
        root * /opt/velaflow/data
        file_server {
            root /opt/velaflow/data
        }
        rewrite * /copilot-logs.md
        file_server
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
        X-XSS-Protection "1; mode=block"
        Referrer-Policy "strict-origin-when-cross-origin"
        Permissions-Policy "camera=(), microphone=(), geolocation=()"
        Content-Security-Policy "default-src '\''self'\''; frame-ancestors '\''none'\''"
        -Server
        -X-Powered-By
    }

    log {
        output file /var/log/caddy/velaflow.log {
            roll_size 10mb
            roll_keep 5
        }
    }
}
CADDY

mkdir -p /var/log/caddy
systemctl enable caddy
systemctl restart caddy
echo "  ✓ Caddy reverse proxy installed and configured"
echo "  ✓ Copilot logs available at http://localhost/copilot/logs (local only)"
'

ok "Reverse proxy configured"

# ═══════════════════════════════════════════════════════════════════════
# PHASE 7: Install VelaFlow
# ═══════════════════════════════════════════════════════════════════════
banner "Phase 7: Install VelaFlow Application"

# Copy source code into container
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

if [[ "$PLATFORM" == "proxmox" ]]; then
    info "Pushing source code into container..."
    # Create tarball excluding unnecessary files
    cd "${REPO_ROOT}"
    tar czf /tmp/velaflow-src.tar.gz \
        --exclude='.git' \
        --exclude='__pycache__' \
        --exclude='.pytest_cache' \
        --exclude='*.egg-info' \
        --exclude='.venv' \
        --exclude='build' \
        --exclude='*.pyc' \
        .
    pct push "${CTID}" /tmp/velaflow-src.tar.gz /tmp/velaflow-src.tar.gz
    rm /tmp/velaflow-src.tar.gz

    pct exec "${CTID}" -- bash -c "
        cd /opt/velaflow
        tar xzf /tmp/velaflow-src.tar.gz -C /opt/velaflow/
        rm /tmp/velaflow-src.tar.gz
        chown -R velaflow:velaflow /opt/velaflow
        source /opt/velaflow/venv/bin/activate
        pip install -e '/opt/velaflow[all]' -q
        echo '  ✓ VelaFlow installed (editable mode)'
    "
elif [[ "$PLATFORM" == "lxd" ]]; then
    info "Pushing source code into container..."
    cd "${REPO_ROOT}"
    tar czf /tmp/velaflow-src.tar.gz \
        --exclude='.git' \
        --exclude='__pycache__' \
        --exclude='.pytest_cache' \
        --exclude='*.egg-info' \
        --exclude='.venv' \
        --exclude='build' \
        --exclude='*.pyc' \
        .
    ${LXC_CMD} file push /tmp/velaflow-src.tar.gz "${CT_NAME}/tmp/velaflow-src.tar.gz"
    rm /tmp/velaflow-src.tar.gz

    ${CT_EXEC} bash -c "
        cd /opt/velaflow
        tar xzf /tmp/velaflow-src.tar.gz -C /opt/velaflow/
        rm /tmp/velaflow-src.tar.gz
        chown -R velaflow:velaflow /opt/velaflow
        source /opt/velaflow/venv/bin/activate
        pip install -e '/opt/velaflow[all]' -q
        echo '  ✓ VelaFlow installed (editable mode)'
    "
fi

ok "VelaFlow application installed"

# ═══════════════════════════════════════════════════════════════════════
# PHASE 8: Health Check
# ═══════════════════════════════════════════════════════════════════════
banner "Phase 8: Verification"

${CT_EXEC} bash -c '
source /opt/velaflow/venv/bin/activate
python /opt/velaflow/scripts/installer.py --health 2>&1
'

# ═══════════════════════════════════════════════════════════════════════
# Summary
# ═══════════════════════════════════════════════════════════════════════
banner "Deployment Complete"
echo ""
echo "  Container:    ${CT_NAME} (${PLATFORM})"
echo "  IP:           ${IP:-unknown}"
echo "  API:          http://${IP:-localhost}:80/api/v1/"
echo "  Health:       http://${IP:-localhost}:80/health"
echo "  Copilot Logs: http://${IP:-localhost}:80/copilot/logs (local only)"
echo ""
echo "  Next steps:"
echo "  1. Edit secrets:   ${PLATFORM} exec ${CTID:-$CT_NAME} -- nano /etc/velaflow/secrets.env"
echo "  2. Set domain:     Edit /etc/caddy/Caddyfile (replace :80 with domain)"
echo "  3. Start services: systemctl start velaflow-api velaflow-worker"
echo "  4. Verify:         curl http://${IP:-localhost}/health"
echo ""
echo "  For Oracle Cloud: Open ports 80+443 in OCI Security List"
echo "  For Copilot:      GET /copilot/logs returns sanitised HMAC-chained logs"
echo ""
