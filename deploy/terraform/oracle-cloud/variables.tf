variable "tenancy_ocid" {
  description = "OCI tenancy OCID."
  type        = string
}

variable "compartment_ocid" {
  description = "Compartment OCID in which to place all resources."
  type        = string
}

variable "region" {
  type    = string
  default = "eu-frankfurt-1"
}

variable "availability_domain" {
  description = "Availability domain name, e.g. 'AD-1' or the full OCI AD name."
  type        = string
}

variable "ssh_public_key" {
  description = "SSH public key content (not a path)."
  type        = string
}

variable "ssh_private_key_path" {
  description = "Path to the matching private key. Not used by OCI's cloud-init path but required by the shared module's variable contract."
  type        = string
  sensitive   = true
  default     = ""
}

variable "allowed_admin_cidr" {
  description = "CIDR allowed to hit TCP/22 (SSH) on the instance."
  type        = string
  default     = "0.0.0.0/0"
}

# Always Free caps: Ampere A1.Flex — up to 4 OCPU / 24 GB RAM.
variable "ocpus" {
  type    = number
  default = 2
}

variable "memory_gb" {
  type    = number
  default = 12
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
