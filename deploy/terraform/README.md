# VelaFlow Terraform — portable host IaC (**free forever**)

VelaFlow's production deploy path is **declarative Terraform**, not
`scripts/install.sh`. `install.sh` remains as a developer quick-start only
(see [`../../docs/adr/0003-terraform-iac-vs-bash-install.md`](../../docs/adr/0003-terraform-iac-vs-bash-install.md)).

> **Zero-cost hosting is a first-class design constraint.** Every target below
> is deployable at **€0/month** indefinitely. VelaFlow must never require the
> maintainer to pay for infrastructure to run the public service. The only
> paid tier that exists is **VIP**, and VIP pricing is set *above* marginal
> cost — free and standard users must always be profitable or neutral.

## Design goal — one module, three free hosts

No cloud is a "primary" target. VelaFlow must be deployable, with the same
Terraform module, onto whichever free Linux host the operator has:

| Target                           | Typical use                                                       | Cost                                  |
|----------------------------------|-------------------------------------------------------------------|---------------------------------------|
| [`proxmox/`](proxmox/)           | Homelab LXC/VM on Proxmox (Intel N95, 8 GB RAM)                   | **€0** — self-hosted hardware        |
| [`generic-vm/`](generic-vm/)     | Any SSH-reachable Linux host already owned (home server, old PC)  | **€0** when reusing existing capacity |
| [`oracle-cloud/`](oracle-cloud/) | OCI **Always Free** Ampere A1.Flex VM (4 OCPU / 24 GB RAM, free forever per OCI Always Free tier) | **€0** — forever              |

No AWS, Azure, GCP, or Databricks targets exist or will exist — they would
introduce paid infrastructure and violate the zero-cost constraint.

All three targets delegate to the same reusable module:

```
modules/velaflow-host/
```

The module is responsible for everything *inside* the host: Podman, systemd
units, the nginx TLS reverse proxy, data directory permissions,
`/etc/velaflow/velaflow.env`. The three thin targets only know how to
create or reference a host and pass its SSH connection details to the
module. The result is that the VelaFlow install is strictly declarative on
every supported host — one Terraform state describes it end-to-end.

## Hardware floor

The smallest supported target is the developer's own Proxmox homelab:

- Intel N95 CPU (4C/4T, no AVX-512)
- 8 GB RAM
- 1 LXC container (unprivileged) or 1 small VM

VelaFlow's steady-state RSS is ~500 MB (API + scheduler + DuckDB). The
optional `velaflow[premium]` sentence-transformers extra pushes this to
~1.5 GB; omit it on the N95 and keep the default hashing embedder.

## Preflight

Before `terraform apply`, run the preflight validator:

```powershell
# from repo root
python scripts/terraform_preflight.py deploy/terraform/proxmox
python scripts/terraform_preflight.py deploy/terraform/generic-vm
python scripts/terraform_preflight.py deploy/terraform/oracle-cloud
```

It checks, without requiring the Terraform CLI:

1. Required `.tf` files exist and parse as valid HCL.
2. `terraform.tfvars.example` covers every non-defaulted input.
3. SSH key paths (if given) exist and have sane permissions.
4. The shared module is referenced correctly.

The same checks run under `pytest` via
[`tests/test_terraform_modules.py`](../../tests/test_terraform_modules.py), so
a malformed `.tf` fails the CI test suite.

## Validated with real Terraform

All three targets pass `terraform fmt -check`, `terraform init -backend=false`,
and `terraform validate` on Terraform v1.6+ with these providers:

- `Telmate/proxmox` ~> 2.9 (Proxmox VE 7/8 — stable release line)
- `hashicorp/null` ~> 3.2 (generic-vm + shared module bootstrap)
- `oracle/oci` ~> 6.0 (oracle-cloud target)

Operators review, adapt networking for their environment, then run
`terraform init && terraform validate && terraform plan` on their own
workstation before `terraform apply`.

## Relation to the scaling ladder

At Stage 2+ (see [`../../docs/scaling-path.md`](../../docs/scaling-path.md)) these
modules can be complemented — not replaced — by a paid-cloud or managed-runtime
target (e.g. **Databricks Asset Bundles**, AWS ECS, Azure Container Apps, GKE)
if user demand grows beyond what free tiers can serve. Stage 2+ is **only**
activated when paid VIP revenue covers the incremental cloud bill; the
shipped free-tier modules remain the Stage 0/1 deployment surface and are
the only IaC the free self-host path ever needs.

