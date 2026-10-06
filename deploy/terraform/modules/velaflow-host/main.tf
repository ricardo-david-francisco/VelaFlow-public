# Shared VelaFlow host-bootstrap module.
#
# Responsibilities:
#   1. Render cloud-init (+ embedded systemd unit + nginx vhost) from
#      `templates/cloud-init.yaml.tftpl`. Caller may consume this string
#      natively (e.g. OCI's `metadata.user_data`) or let the module push
#      it via SSH (`enable_bootstrap = true`).
#   2. When `enable_bootstrap = true`, run the cloud-init against an
#      already-booted Linux host over SSH, idempotently.
#
# The module never opens firewall rules of its own — each caller
# (proxmox/, generic-vm/, oracle-cloud/) is responsible for its own
# network primitives.

locals {
  cloud_init = templatefile("${path.module}/templates/cloud-init.yaml.tftpl", {
    velaflow_image = var.velaflow_image
    velaflow_env   = var.velaflow_env
    data_dir       = var.data_dir
    api_port       = var.api_port
    public_port    = var.public_port
  })
}

resource "null_resource" "bootstrap" {
  count = var.enable_bootstrap ? 1 : 0

  triggers = {
    host       = var.host
    image      = var.velaflow_image
    cloud_init = sha256(local.cloud_init)
  }

  connection {
    type        = "ssh"
    host        = var.host
    user        = var.ssh_user
    port        = var.ssh_port
    private_key = file(var.ssh_private_key_path)
    timeout     = "5m"
  }

  provisioner "file" {
    content     = local.cloud_init
    destination = "/tmp/velaflow-bootstrap.yaml"
  }

  provisioner "remote-exec" {
    inline = [
      "set -euo pipefail",
      "command -v cloud-init >/dev/null || (sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq cloud-init)",
      "sudo install -d -m 0755 /var/lib/velaflow-bootstrap",
      "sudo mv /tmp/velaflow-bootstrap.yaml /var/lib/velaflow-bootstrap/cloud-init.yaml",
      "sudo cloud-init schema --config-file /var/lib/velaflow-bootstrap/cloud-init.yaml --annotate",
      "sudo cloud-init single --name write_files --frequency always --file /var/lib/velaflow-bootstrap/cloud-init.yaml",
      "sudo cloud-init single --name runcmd --frequency always --file /var/lib/velaflow-bootstrap/cloud-init.yaml",
      "sudo systemctl is-active --quiet velaflow.service && echo 'velaflow active' || (echo 'velaflow failed to start'; sudo journalctl -u velaflow.service --no-pager -n 50; exit 1)",
    ]
  }
}
