terraform {
  required_version = ">= 1.6.0"

  required_providers {
    proxmox = {
      source  = "Telmate/proxmox"
      version = "~> 2.9"
    }
  }
}

provider "proxmox" {
  pm_api_url          = var.proxmox_api_url
  pm_api_token_id     = var.proxmox_api_token_id
  pm_api_token_secret = var.proxmox_api_token_secret
  pm_tls_insecure     = var.proxmox_tls_insecure
}

# Unprivileged LXC container on the Proxmox homelab node.
# Podman runs rootless inside; no nested virt required.
resource "proxmox_lxc" "velaflow" {
  target_node  = var.proxmox_node
  hostname     = var.hostname
  ostemplate   = var.ostemplate
  unprivileged = true
  start        = true
  onboot       = true

  cores  = var.cores
  memory = var.memory_mb
  swap   = var.swap_mb

  rootfs {
    storage = var.rootfs_storage
    size    = var.rootfs_size
  }

  network {
    name   = "eth0"
    bridge = var.network_bridge
    ip     = var.network_ip      # e.g. "dhcp" or "192.168.1.50/24"
    gw     = var.network_gateway # required when ip is static
  }

  ssh_public_keys = file(var.ssh_public_key_path)

  features {
    nesting = true # required for Podman's user-namespace + cgroup v2 usage
  }

  lifecycle {
    ignore_changes = [ostemplate]
  }
}

module "velaflow_host" {
  source = "../modules/velaflow-host"

  host                 = var.container_ssh_host
  ssh_user             = var.ssh_user
  ssh_private_key_path = var.ssh_private_key_path

  velaflow_image = var.velaflow_image
  velaflow_env   = var.velaflow_env
  data_dir       = var.data_dir
  api_port       = var.api_port
  public_port    = var.public_port

  depends_on = [proxmox_lxc.velaflow]
}

output "lxc_id" {
  value       = proxmox_lxc.velaflow.vmid
  description = "Proxmox VMID of the LXC hosting VelaFlow."
}

output "public_url" {
  value = module.velaflow_host.public_url
}
