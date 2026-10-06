terraform {
  required_version = ">= 1.6.0"
}

# Generic VM / bare-metal / externally-provisioned Linux host.
# Terraform does not create the host; the operator points this target at
# an existing SSH-reachable machine (e.g. a VPS, a Proxmox VM already
# running, a physical Mini-PC on the homelab, an OCI VM provisioned by
# something else).

module "velaflow_host" {
  source = "../modules/velaflow-host"

  host                 = var.host
  ssh_user             = var.ssh_user
  ssh_port             = var.ssh_port
  ssh_private_key_path = var.ssh_private_key_path

  velaflow_image = var.velaflow_image
  velaflow_env   = var.velaflow_env
  data_dir       = var.data_dir
  api_port       = var.api_port
  public_port    = var.public_port
  admin_cidr     = var.admin_cidr
}

output "public_url" {
  value = module.velaflow_host.public_url
}
