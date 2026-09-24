#!/bin/bash
# VelaFlow Enterprise — LXC Container Setup
# Sets up the primary LXC container for the multi-tenant API
# Run as root on the Proxmox host
set -euo pipefail

CONTAINER_ID="${1:-200}"
CONTAINER_NAME="velaflow-enterprise"
STORAGE="local-lvm"
TEMPLATE="local:vztmpl/debian-12-standard_12.2-1_amd64.tar.zst"
MEMORY=2048
CORES=2
DISK="16G"

echo "=== VelaFlow Enterprise LXC Setup ==="
echo "Container: ${CONTAINER_ID} (${CONTAINER_NAME})"

# Create unprivileged container
pct create "${CONTAINER_ID}" "${TEMPLATE}" \
    --hostname "${CONTAINER_NAME}" \
    --storage "${STORAGE}" \
    --rootfs "${STORAGE}:${DISK}" \
    --memory "${MEMORY}" \
    --cores "${CORES}" \
    --net0 name=eth0,bridge=vmbr0,ip=dhcp \
    --unprivileged 1 \
    --features nesting=1 \
    --ostype debian \
    --start 0

# Enable nesting for nested LXC (premium tier) with hardened profile
cat >> "/etc/pve/lxc/${CONTAINER_ID}.conf" <<EOF

# Security hardening — use generated AppArmor profile
lxc.apparmor.profile: generated

# Drop dangerous capabilities
lxc.cap.drop: sys_rawio sys_module sys_ptrace mac_admin mac_override sys_boot sys_time
EOF

pct start "${CONTAINER_ID}"
sleep 5

# Install Python 3.11+ and dependencies
pct exec "${CONTAINER_ID}" -- bash -c "
    apt-get update
    apt-get install -y python3 python3-pip python3-venv git curl redis-server

    # Create application directory
    mkdir -p /opt/velaflow
    cd /opt/velaflow

    # Create virtual environment
    python3 -m venv .venv
    source .venv/bin/activate

    # Install VelaFlow
    pip install --upgrade pip
    pip install 'fastapi>=0.115.0' 'uvicorn[standard]>=0.32.0' 'pydantic>=2.10.0'
    pip install 'python-dotenv>=1.0.0' 'requests>=2.33.0'

    # Create data directories
    mkdir -p /opt/velaflow/data/medallion
    mkdir -p /etc/velaflow

    # Create systemd service
    cat > /etc/systemd/system/velaflow-api.service << 'EOF'
[Unit]
Description=VelaFlow Enterprise API
After=network.target redis-server.service
Wants=redis-server.service

[Service]
Type=exec
User=www-data
Group=www-data
WorkingDirectory=/opt/velaflow
EnvironmentFile=/etc/velaflow/secrets.env
ExecStart=/opt/velaflow/.venv/bin/uvicorn brain.api.app:create_app \
    --factory --host 127.0.0.1 --port 8000 --workers 2
Restart=always
RestartSec=5

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/velaflow/data
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

    # Create worker service
    cat > /etc/systemd/system/velaflow-worker.service << 'EOF'
[Unit]
Description=VelaFlow Enterprise Worker
After=network.target redis-server.service velaflow-api.service

[Service]
Type=exec
User=www-data
Group=www-data
WorkingDirectory=/opt/velaflow
EnvironmentFile=/etc/velaflow/secrets.env
ExecStart=/opt/velaflow/.venv/bin/python -c \
    \"from brain.queue.worker import QueueWorker; from brain.queue.tasks import TaskQueue; from brain.storage.local import LocalStorageBackend; from brain.config import Settings; w = QueueWorker(TaskQueue(), LocalStorageBackend('/opt/velaflow/data/medallion'), Settings.from_env()); w.start()\"
Restart=always
RestartSec=5

NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/velaflow/data
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

    # Enable Redis
    systemctl enable redis-server
    systemctl start redis-server

    # Set permissions
    chown -R www-data:www-data /opt/velaflow/data

    systemctl daemon-reload
    echo 'VelaFlow Enterprise LXC setup complete.'
    echo 'Next: copy source to /opt/velaflow/src/ and create /etc/velaflow/secrets.env'
"

echo "=== Container ${CONTAINER_ID} ready ==="
echo "Start services with: pct exec ${CONTAINER_ID} -- systemctl start velaflow-api velaflow-worker"
