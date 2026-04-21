#!/usr/bin/env bash
# =============================================================================
# VelaFlow — Proxmox LXC Provisioning Script
# Run this ON your Proxmox host (not inside the container)
#
# Usage: bash setup-lxc.sh [CONTAINER_ID]
# Example: bash setup-lxc.sh 200
# =============================================================================
set -euo pipefail

# --- Configuration ---
CTID="${1:-200}"
HOSTNAME="velaflow"
TEMPLATE="local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst"
STORAGE="local-lvm"
DISK_SIZE="10"
RAM="1024"
SWAP="512"
CORES="2"
BRIDGE="vmbr0"
NAMESERVER="8.8.8.8"

echo "============================================"
echo "  VelaFlow — LXC Setup"
echo "  Container ID: ${CTID}"
echo "============================================"

# Check if template exists; download if not
if ! pveam list local | grep -q "debian-12-standard"; then
    echo "[1/6] Downloading Debian 12 template..."
    pveam update
    pveam download local debian-12-standard_12.7-1_amd64.tar.zst
else
    echo "[1/6] Debian 12 template already available."
fi

# Create unprivileged container
echo "[2/6] Creating LXC container ${CTID}..."
pct create "${CTID}" "${TEMPLATE}" \
    --hostname "${HOSTNAME}" \
    --storage "${STORAGE}" \
    --rootfs "${STORAGE}:${DISK_SIZE}" \
    --memory "${RAM}" \
    --swap "${SWAP}" \
    --cores "${CORES}" \
    --net0 "name=eth0,bridge=${BRIDGE},ip=dhcp" \
    --nameserver "${NAMESERVER}" \
    --unprivileged 1 \
    --features "nesting=1,keyctl=1" \
    --onboot 1 \
    --start 0

# Enable nesting for Docker support
echo "[3/6] Configuring container features..."
cat >> "/etc/pve/lxc/${CTID}.conf" <<EOF

# Security hardening — keep AppArmor enabled (Proxmox default profile)
# Only set unconfined if Docker-in-LXC is actually needed.
lxc.apparmor.profile: generated

# Drop dangerous capabilities to prevent container escape
lxc.cap.drop: sys_admin sys_rawio sys_module sys_ptrace net_raw mac_admin mac_override sys_boot sys_time
EOF

# Start container
echo "[4/6] Starting container..."
pct start "${CTID}"
sleep 5

# Get container IP
echo "[5/6] Waiting for network..."
for i in $(seq 1 30); do
    IP=$(pct exec "${CTID}" -- hostname -I 2>/dev/null | awk '{print $1}')
    if [ -n "${IP}" ]; then
        break
    fi
    sleep 2
done

if [ -z "${IP:-}" ]; then
    echo "WARNING: Could not detect container IP. Check DHCP."
    IP="<unknown>"
fi

echo "[6/6] Container ready!"
echo ""
echo "============================================"
echo "  Container ${CTID} is running"
echo "  IP: ${IP}"
echo "============================================"
echo ""
echo "Next steps:"
echo "  1. Enter the container:"
echo "     pct enter ${CTID}"
echo ""
echo "  2. Run the install script:"
echo "     bash /tmp/install.sh"
echo ""
echo "  3. Or copy files first:"
echo "     pct push ${CTID} scripts/install.sh /tmp/install.sh"
echo ""
