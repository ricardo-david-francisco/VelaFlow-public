#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# VelaFlow — Oracle Cloud Infrastructure (OCI) Deployment
#
# Deploys VelaFlow on an Oracle Cloud Always-Free instance using LXD
# containers. Supports nested LXC for KEDA/K8s premium-tier scaling.
#
# Prerequisites:
#   - Oracle Cloud Always-Free instance (ARM A1 or AMD E2.1.Micro)
#   - Ubuntu 22.04+ (Canonical image from OCI Marketplace)
#   - SSH access to the instance
#
# What this script does:
#   1. Installs LXD (snap) + initialises storage pool
#   2. Delegates to deploy-hardened.sh for LXC creation
#   3. Configures OCI-specific networking (iptables NAT for LXC)
#   4. Sets up UFW on the HOST for external access
#   5. Outputs OCI Security List rules to add in the console
#
# Usage:
#   ssh ubuntu@<OCI_IP>
#   git clone https://github.com/your-repo/velaflow /opt/velaflow
#   cd /opt/velaflow
#   sudo bash deploy/cloud/setup-oracle.sh --domain velaflow.example.com
#
# Resources:
#   Always-Free ARM A1: 4 OCPU, 24 GB RAM (allocate 4 GB + 2 cores to LXC)
#   Always-Free AMD:    1 OCPU, 1 GB RAM  (too small — use ARM A1)
# ═══════════════════════════════════════════════════════════════════════
set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────
DOMAIN="${1:---domain}"
CT_NAME="velaflow"
MEMORY_MB=4096
CORES=2
DISK_GB=20

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --domain) DOMAIN="$2"; shift 2 ;;
        --name)   CT_NAME="$2"; shift 2 ;;
        --memory) MEMORY_MB="$2"; shift 2 ;;
        --cores)  CORES="$2"; shift 2 ;;
        *) shift ;;
    esac
done

if [[ -z "${DOMAIN}" || "${DOMAIN}" == "--domain" ]]; then
    DOMAIN=""
fi

# ── Root check ────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: Run as root. Use: sudo bash $0 --domain <your-domain>" >&2
    exit 1
fi

echo "═══════════════════════════════════════════════════════════════"
echo " VelaFlow — Oracle Cloud Deployment"
echo " Domain: ${DOMAIN:-'(not set — will use IP)'}"
echo "═══════════════════════════════════════════════════════════════"

# ═══════════════════════════════════════════════════════════════════════
# STEP 1: Host hardening
# ═══════════════════════════════════════════════════════════════════════
echo ""
echo "[1/6] Hardening OCI host..."

# System updates
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq

# Install essentials
apt-get install -y -qq \
    ufw fail2ban \
    unattended-upgrades apt-listchanges \
    iptables-persistent \
    curl wget git

# UFW on the HOST
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp  comment "SSH"
ufw allow 80/tcp  comment "HTTP"
ufw allow 443/tcp comment "HTTPS"
ufw --force enable
echo "  ✓ Host firewall: 22/80/443 only"

# fail2ban on the HOST
cat > /etc/fail2ban/jail.local <<'JAIL'
[DEFAULT]
bantime  = 3600
findtime = 600
maxretry = 5
backend  = systemd

[sshd]
enabled  = true
port     = ssh
maxretry = 3
bantime  = 7200
JAIL

systemctl enable fail2ban
systemctl restart fail2ban
echo "  ✓ fail2ban configured on host"

# Kernel hardening
cat > /etc/sysctl.d/99-oracle-hardening.conf <<'SYSCTL'
net.ipv4.ip_forward = 1
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv4.tcp_syncookies = 1
net.ipv4.conf.all.rp_filter = 1
net.ipv4.icmp_echo_ignore_broadcasts = 1
kernel.dmesg_restrict = 1
kernel.kptr_restrict = 2
fs.suid_dumpable = 0
SYSCTL
sysctl --system > /dev/null 2>&1
echo "  ✓ Kernel hardening applied (IP forward enabled for LXC NAT)"

# ═══════════════════════════════════════════════════════════════════════
# STEP 2: Install LXD
# ═══════════════════════════════════════════════════════════════════════
echo ""
echo "[2/6] Installing LXD..."

if ! command -v lxd &>/dev/null; then
    snap install lxd --channel=latest/stable
    sleep 3
fi

# Auto-init LXD with dir backend (works on free-tier, no ZFS needed)
if ! lxc storage list --format csv 2>/dev/null | grep -q default; then
    cat <<PRESEED | lxd init --preseed
config:
  core.https_address: "[::]:8443"
networks:
- config:
    ipv4.address: 10.10.10.1/24
    ipv4.nat: "true"
    ipv6.address: none
  description: ""
  name: lxdbr0
  type: bridge
storage_pools:
- config:
    source: /var/lib/lxd/storage-pools/default
  description: ""
  name: default
  driver: dir
profiles:
- config: {}
  description: Default LXD profile
  devices:
    eth0:
      name: eth0
      network: lxdbr0
      type: nic
    root:
      path: /
      pool: default
      size: ${DISK_GB}GB
      type: disk
  name: default
PRESEED
fi
echo "  ✓ LXD initialised (bridge: lxdbr0, subnet: 10.10.10.0/24)"

# ═══════════════════════════════════════════════════════════════════════
# STEP 3: Create hardened LXC
# ═══════════════════════════════════════════════════════════════════════
echo ""
echo "[3/6] Creating hardened VelaFlow LXC..."

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

bash "${REPO_ROOT}/deploy/lxc/deploy-hardened.sh" \
    --platform lxd \
    --name "${CT_NAME}" \
    --memory "${MEMORY_MB}" \
    --cores "${CORES}" \
    --disk "${DISK_GB}" \
    ${DOMAIN:+--domain "${DOMAIN}"}

# ═══════════════════════════════════════════════════════════════════════
# STEP 4: NAT port forwarding (host → LXC)
# ═══════════════════════════════════════════════════════════════════════
echo ""
echo "[4/6] Configuring NAT port forwarding..."

LXC_IP=$(lxc list "${CT_NAME}" --format csv -c4 | head -1 | cut -d' ' -f1)

if [[ -z "${LXC_IP}" ]]; then
    echo "WARNING: Could not detect LXC IP. NAT rules not applied."
    echo "Run manually after LXC gets an IP:"
    echo "  iptables -t nat -A PREROUTING -p tcp --dport 80 -j DNAT --to-destination <LXC_IP>:80"
    echo "  iptables -t nat -A PREROUTING -p tcp --dport 443 -j DNAT --to-destination <LXC_IP>:443"
else
    # Port forward 80/443 from host to LXC
    iptables -t nat -A PREROUTING -p tcp --dport 80 -j DNAT --to-destination "${LXC_IP}:80"
    iptables -t nat -A PREROUTING -p tcp --dport 443 -j DNAT --to-destination "${LXC_IP}:443"
    iptables -A FORWARD -p tcp -d "${LXC_IP}" --dport 80 -j ACCEPT
    iptables -A FORWARD -p tcp -d "${LXC_IP}" --dport 443 -j ACCEPT

    # Persist rules
    netfilter-persistent save 2>/dev/null || iptables-save > /etc/iptables/rules.v4
    echo "  ✓ NAT: host:80 → ${LXC_IP}:80"
    echo "  ✓ NAT: host:443 → ${LXC_IP}:443"
fi

# ═══════════════════════════════════════════════════════════════════════
# STEP 5: Domain configuration (if provided)
# ═══════════════════════════════════════════════════════════════════════
echo ""
echo "[5/6] Domain configuration..."

if [[ -n "${DOMAIN}" && -n "${LXC_IP}" ]]; then
    # Update Caddyfile inside LXC to use domain
    lxc exec "${CT_NAME}" -- bash -c "
        sed -i 's/^:80 {/${DOMAIN} {/' /etc/caddy/Caddyfile
        systemctl restart caddy
    "

    # Update secrets.env
    lxc exec "${CT_NAME}" -- bash -c "
        sed -i 's|^VELAFLOW_DOMAIN=.*|VELAFLOW_DOMAIN=${DOMAIN}|' /etc/velaflow/secrets.env
        sed -i 's|^CORS_ALLOWED_ORIGINS=.*|CORS_ALLOWED_ORIGINS=https://${DOMAIN}|' /etc/velaflow/secrets.env
    "
    echo "  ✓ Domain set: ${DOMAIN}"
    echo "  ✓ Caddy will auto-obtain Let's Encrypt TLS certificate"
else
    echo "  ⚠ No domain set — using HTTP on port 80"
    echo "  Set later: lxc exec ${CT_NAME} -- nano /etc/caddy/Caddyfile"
fi

# ═══════════════════════════════════════════════════════════════════════
# STEP 6: Summary
# ═══════════════════════════════════════════════════════════════════════
echo ""
echo "[6/6] Deployment complete!"

PUBLIC_IP=$(curl -s ifconfig.me 2>/dev/null || echo "<your-public-ip>")

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo " VelaFlow — Oracle Cloud Deployment Complete"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo " Public IP:     ${PUBLIC_IP}"
echo " LXC IP:        ${LXC_IP:-unknown}"
echo " Domain:        ${DOMAIN:-'(not set)'}"
echo ""
if [[ -n "${DOMAIN}" ]]; then
echo " API:           https://${DOMAIN}/api/v1/"
echo " Health:        https://${DOMAIN}/health"
echo " Copilot Logs:  (local only — SSH tunnel required)"
else
echo " API:           http://${PUBLIC_IP}/api/v1/"
echo " Health:        http://${PUBLIC_IP}/health"
fi
echo ""
echo " ┌─────────────────────────────────────────────────────────┐"
echo " │  OCI Console — Required Security List Rules             │"
echo " │                                                         │"
echo " │  Ingress Rule 1: 0.0.0.0/0 → TCP/80  (HTTP)           │"
echo " │  Ingress Rule 2: 0.0.0.0/0 → TCP/443 (HTTPS)          │"
echo " │  Ingress Rule 3: 0.0.0.0/0 → TCP/22  (SSH)            │"
echo " │                                                         │"
echo " │  Go to: Networking → Virtual Cloud Networks → Subnets  │"
echo " │  → Security Lists → Default Security List → Add Rules  │"
echo " └─────────────────────────────────────────────────────────┘"
echo ""
echo " Next steps:"
echo "   1. Add OCI Security List rules (above)"
echo "   2. Point DNS A record: ${DOMAIN:-your-domain} → ${PUBLIC_IP}"
echo "   3. Edit secrets: lxc exec ${CT_NAME} -- nano /etc/velaflow/secrets.env"
echo "   4. Start: lxc exec ${CT_NAME} -- systemctl start velaflow-api velaflow-worker"
echo "   5. Verify: curl http://${PUBLIC_IP}/health"
echo ""
echo " Copilot log access (via SSH tunnel):"
echo "   ssh -L 8080:${LXC_IP:-10.10.10.x}:80 ubuntu@${PUBLIC_IP}"
echo "   Then open: http://localhost:8080/copilot/logs"
echo ""
