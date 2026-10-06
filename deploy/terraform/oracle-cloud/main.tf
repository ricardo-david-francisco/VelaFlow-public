terraform {
  required_version = ">= 1.6.0"

  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "~> 6.0"
    }
  }
}

provider "oci" {
  tenancy_ocid = var.tenancy_ocid
  region       = var.region
}

# Render the shared cloud-init without invoking remote-exec. OCI consumes
# cloud-init natively on first boot — no SSH bootstrap round-trip needed.
module "velaflow_host" {
  source = "../modules/velaflow-host"

  host                 = "pending" # placeholder; OCI doesn't need SSH push
  ssh_private_key_path = var.ssh_private_key_path

  velaflow_image = var.velaflow_image
  velaflow_env   = var.velaflow_env
  data_dir       = var.data_dir
  api_port       = var.api_port
  public_port    = var.public_port
  admin_cidr     = var.allowed_admin_cidr

  enable_bootstrap = false
}

# VCN + public subnet + security list (minimum viable network).
resource "oci_core_vcn" "velaflow" {
  compartment_id = var.compartment_ocid
  cidr_blocks    = ["10.0.0.0/16"]
  display_name   = "velaflow-vcn"
  dns_label      = "velaflow"
}

resource "oci_core_internet_gateway" "velaflow" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.velaflow.id
  display_name   = "velaflow-igw"
  enabled        = true
}

resource "oci_core_route_table" "velaflow" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.velaflow.id
  display_name   = "velaflow-rt"

  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_internet_gateway.velaflow.id
  }
}

resource "oci_core_security_list" "velaflow" {
  compartment_id = var.compartment_ocid
  vcn_id         = oci_core_vcn.velaflow.id
  display_name   = "velaflow-sl"

  egress_security_rules {
    destination = "0.0.0.0/0"
    protocol    = "all"
  }

  # SSH only from the admin CIDR.
  ingress_security_rules {
    source   = var.allowed_admin_cidr
    protocol = "6" # TCP
    tcp_options {
      min = 22
      max = 22
    }
  }

  # HTTPS open to the world.
  ingress_security_rules {
    source   = "0.0.0.0/0"
    protocol = "6"
    tcp_options {
      min = 443
      max = 443
    }
  }
}

resource "oci_core_subnet" "velaflow" {
  compartment_id    = var.compartment_ocid
  vcn_id            = oci_core_vcn.velaflow.id
  cidr_block        = "10.0.1.0/24"
  display_name      = "velaflow-subnet"
  dns_label         = "vf"
  route_table_id    = oci_core_route_table.velaflow.id
  security_list_ids = [oci_core_security_list.velaflow.id]
}

# Canonical Ubuntu 24.04 on ARM (A1.Flex) — Always Free eligible.
data "oci_core_images" "ubuntu_arm" {
  compartment_id           = var.compartment_ocid
  operating_system         = "Canonical Ubuntu"
  operating_system_version = "24.04"
  shape                    = "VM.Standard.A1.Flex"
  sort_by                  = "TIMECREATED"
  sort_order               = "DESC"
}

resource "oci_core_instance" "velaflow" {
  compartment_id      = var.compartment_ocid
  availability_domain = var.availability_domain
  display_name        = "velaflow"
  shape               = "VM.Standard.A1.Flex"

  shape_config {
    ocpus         = var.ocpus
    memory_in_gbs = var.memory_gb
  }

  source_details {
    source_type = "image"
    source_id   = data.oci_core_images.ubuntu_arm.images[0].id
  }

  create_vnic_details {
    subnet_id        = oci_core_subnet.velaflow.id
    assign_public_ip = true
  }

  metadata = {
    ssh_authorized_keys = var.ssh_public_key
    user_data           = base64encode(module.velaflow_host.cloud_init_yaml)
  }

  freeform_tags = {
    "velaflow:tier"       = "standalone"
    "velaflow:managed_by" = "terraform"
  }
}
