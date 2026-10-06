output "cloud_init_yaml" {
  description = "Rendered cloud-init document. Callers that consume cloud-init natively (e.g. OCI user_data) can feed this output into their VM resource."
  value       = local.cloud_init
  sensitive   = true
}

output "bootstrap_id" {
  description = "ID of the null_resource that performed remote bootstrap (null when enable_bootstrap = false)."
  value       = var.enable_bootstrap ? null_resource.bootstrap[0].id : null
}

output "api_internal_url" {
  description = "Where the container is reachable on the host's loopback (behind nginx)."
  value       = "http://127.0.0.1:${var.api_port}"
}

output "public_url" {
  description = "Public HTTPS URL served by nginx (assumes TLS material present at /etc/velaflow/tls/)."
  value       = "https://${var.host}:${var.public_port}"
}
