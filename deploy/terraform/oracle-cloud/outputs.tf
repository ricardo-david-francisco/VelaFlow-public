output "public_ip" {
  value       = oci_core_instance.velaflow.public_ip
  description = "Public IPv4 assigned to the VelaFlow instance."
}

output "vcn_id" {
  value = oci_core_vcn.velaflow.id
}

output "subnet_id" {
  value = oci_core_subnet.velaflow.id
}

output "public_url" {
  value = "https://${oci_core_instance.velaflow.public_ip}"
}
