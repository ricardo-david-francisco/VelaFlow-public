variable "host" {
  description = "SSH-reachable hostname or IP of an existing Linux host."
  type        = string
}

variable "ssh_user" {
  type    = string
  default = "ubuntu"
}

variable "ssh_port" {
  type    = number
  default = 22
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

variable "admin_cidr" {
  description = "Informational only — generic-vm does not manage firewall rules. Configure your own (ufw / cloud security group) to match."
  type        = string
  default     = "0.0.0.0/0"
}
