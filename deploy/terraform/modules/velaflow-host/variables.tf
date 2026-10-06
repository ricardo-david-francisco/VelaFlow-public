variable "host" {
  description = "SSH-reachable hostname or IP of the target Linux host."
  type        = string
}

variable "ssh_user" {
  description = "SSH user with sudo/root privileges."
  type        = string
  default     = "ubuntu"
}

variable "ssh_private_key_path" {
  description = "Path to the SSH private key used to reach the host."
  type        = string
  sensitive   = true
}

variable "ssh_port" {
  description = "SSH port on the target host."
  type        = number
  default     = 22
}

variable "velaflow_image" {
  description = "OCI container image for the VelaFlow API."
  type        = string
  default     = "ghcr.io/ricardo-david-francisco/velaflow:latest"
}

variable "velaflow_env" {
  description = "Environment variables written to /etc/velaflow/velaflow.env (values are stored in the Terraform state — use a remote encrypted backend for anything sensitive)."
  type        = map(string)
  sensitive   = true
  default     = {}
}

variable "data_dir" {
  description = "Host path that is bind-mounted into the container as /data."
  type        = string
  default     = "/var/lib/velaflow"
}

variable "api_port" {
  description = "Internal HTTP port the API binds to (nginx terminates TLS and proxies to this)."
  type        = number
  default     = 8765
}

variable "public_port" {
  description = "Public TLS port exposed by nginx."
  type        = number
  default     = 443
}

variable "admin_cidr" {
  description = "CIDR allowed to hit SSH. The module itself does not open firewall rules (the target module is responsible) — this value is propagated to outputs so the caller can program its network layer."
  type        = string
  default     = "0.0.0.0/0"
}

variable "enable_bootstrap" {
  description = "If false, the module only renders cloud-init / nginx config and does NOT attempt remote-exec. Useful when the caller injects cloud-init natively (e.g. OCI)."
  type        = bool
  default     = true
}
