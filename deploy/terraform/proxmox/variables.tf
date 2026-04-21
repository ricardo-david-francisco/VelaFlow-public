variable "proxmox_api_url" {
  description = "Proxmox VE API endpoint, e.g. https://pve.home.lan:8006/api2/json."
  type        = string
}

variable "proxmox_api_token_id" {
  description = "Proxmox API token ID (user@realm!tokenid)."
  type        = string
}

variable "proxmox_api_token_secret" {
  description = "Proxmox API token secret (UUID)."
  type        = string
  sensitive   = true
}

variable "proxmox_tls_insecure" {
  description = "Skip TLS verification against the Proxmox API (use only on a trusted LAN with self-signed certs)."
  type        = bool
  default     = false
}

variable "proxmox_node" {
  description = "Proxmox node to place the LXC on."
  type        = string
  default     = "pve"
}

variable "hostname" {
  description = "LXC hostname."
  type        = string
  default     = "velaflow"
}

variable "ostemplate" {
  description = "Proxmox OS template reference, e.g. local:vztmpl/ubuntu-24.04-standard_24.04-2_amd64.tar.zst."
  type        = string
}

# Resource profile tuned for Intel N95 (4C / 8 GB RAM).
variable "cores" {
  type    = number
  default = 2
}

variable "memory_mb" {
  type    = number
  default = 2048
}

variable "swap_mb" {
  type    = number
  default = 512
}

variable "rootfs_storage" {
  type    = string
  default = "local-lvm"
}

variable "rootfs_size" {
  type    = string
  default = "16G"
}

variable "network_bridge" {
  type    = string
  default = "vmbr0"
}

variable "network_ip" {
  description = "Either 'dhcp' or a CIDR like '192.168.1.50/24'."
  type        = string
  default     = "dhcp"
}

variable "network_gateway" {
  description = "Required when network_ip is static; leave empty for DHCP."
  type        = string
  default     = ""
}

variable "ssh_public_key_path" {
  description = "Path to the SSH public key baked into the LXC at first boot."
  type        = string
}

variable "container_ssh_host" {
  description = "Address the bootstrap provisioner uses to SSH into the new LXC (typically its LAN IP). Must be reachable from the workstation running terraform apply."
  type        = string
}

variable "ssh_user" {
  type    = string
  default = "root"
}

variable "ssh_private_key_path" {
  type      = string
  sensitive = true
}

variable "velaflow_image" {
  type    = string
  default = "ghcr.io/ricardo-david-francisco/velaflow:latest"
}

variable "velaflow_env" {
  type      = map(string)
  sensitive = true
  default   = {}
}

variable "data_dir" {
  type    = string
  default = "/var/lib/velaflow"
}

variable "api_port" {
  type    = number
  default = 8765
}

variable "public_port" {
  type    = number
  default = 443
}
