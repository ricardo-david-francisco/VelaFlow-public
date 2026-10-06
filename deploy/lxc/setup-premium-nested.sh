#!/bin/bash
# VelaFlow Premium — Nested LXC Setup
# Creates a nested LXC container inside the primary VelaFlow container
# for premium-tier local LLM inference (true data privacy).
#
# Prerequisites:
# - Primary LXC container created with nesting=1
# - Sufficient resources (4+ GB RAM for LLM inference)
set -euo pipefail

NESTED_NAME="velaflow-premium-llm"
LLM_MODEL="${1:-qwen2:1.5b}"
LLM_PORT=11434

echo "=== VelaFlow Premium Nested LXC Setup ==="
echo "Model: ${LLM_MODEL}"

# Install LXC tools if not present
apt-get update
apt-get install -y lxc debootstrap

# Create nested container
lxc-create -t download -n "${NESTED_NAME}" -- \
    --dist debian --release bookworm --arch amd64

# Configure the nested container
cat >> "/var/lib/lxc/${NESTED_NAME}/config" << EOF
lxc.net.0.type = veth
lxc.net.0.link = lxcbr0
lxc.net.0.flags = up
lxc.net.0.ipv4.address = 10.0.3.100/24
EOF

# Start nested container
lxc-start -n "${NESTED_NAME}"
sleep 5

# Install Ollama inside nested container
lxc-attach -n "${NESTED_NAME}" -- bash -c "
    apt-get update
    apt-get install -y curl ca-certificates
    curl -fsSL https://ollama.ai/install.sh | sh

    # Start Ollama and pull model
    ollama serve &
    sleep 10
    ollama pull '${LLM_MODEL}'

    # Create systemd service for Ollama
    cat > /etc/systemd/system/ollama.service << 'SVCEOF'
[Unit]
Description=Ollama Local LLM Server
After=network.target

[Service]
Type=exec
ExecStart=/usr/local/bin/ollama serve
Restart=always
RestartSec=5
Environment=OLLAMA_HOST=0.0.0.0

[Install]
WantedBy=multi-user.target
SVCEOF

    systemctl daemon-reload
    systemctl enable ollama
    echo 'Ollama setup complete in nested LXC.'
"

echo "=== Premium nested LXC ready ==="
echo "Ollama endpoint: http://10.0.3.100:${LLM_PORT}"
echo "Configure LITELLM_PROXY_URL=http://10.0.3.100:${LLM_PORT} for premium tenants"
