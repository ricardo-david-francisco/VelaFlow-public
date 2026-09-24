# ADR 0003 — Terraform as the production deployment path; `install.sh` is dev-only

- **Status**: Accepted
- **Date**: 2026-04-21 *(revised same day after R21 review feedback)*
- **Context**: R21 architecture review

## Context

`scripts/install.sh` is a Bash installer that bootstraps the VelaFlow
container on a fresh Ubuntu / Debian LXC. It is fast to read and fast to
run on a single developer host. The review correctly flagged that a Bash
installer is imperative, not declarative, and is unfit as a production
deployment surface.

The review's suggestion to "standardise on Azure AKS / VNet Injection"
was **rejected**. VelaFlow's operating promise is that a single operator
can self-host on modest hardware (Proxmox LXC on an Intel N95 / 8 GB
mini-PC) with a strictly **€0 infrastructure bill, forever**. Pinning the
production deploy surface to Azure (or any other paid cloud) would
silently delete that promise. The right abstraction is *portable host
IaC* whose targets are all zero-cost.

## Invariant: zero-cost hosting

The entire public VelaFlow service must be runnable for **€0 of
infrastructure spend** indefinitely. Every deployment target shipped in
this repo must honour that invariant. The only paid surface the project
has is the **VIP** user tier, which is priced above its marginal cost so
that free/standard signups never turn into a maintainer loss. This
invariant is the reason Databricks, AWS, Azure, and GCP paid products
are not — and will not be — primary deployment targets.

## Decision

- **Primary production path**: `deploy/terraform/` with one reusable
  module and three equal-status, **all zero-cost** target
  implementations, each of which installs an identical VelaFlow
  container + systemd unit + nginx TLS reverse proxy on its host via the
  shared module:

  | Target                           | Host type                                             | Purpose                                   | Cost                                |
  |----------------------------------|-------------------------------------------------------|-------------------------------------------|-------------------------------------|
  | `deploy/terraform/proxmox/`      | Unprivileged LXC on Proxmox VE (N95 homelab)          | Smallest supported hardware floor         | **€0** — self-hosted hardware       |
  | `deploy/terraform/generic-vm/`   | Any SSH-reachable Linux host already owned            | "I already have a server" path            | **€0** when reusing existing capacity |
  | `deploy/terraform/oracle-cloud/` | OCI **Always Free** Ampere A1.Flex VM (4 OCPU / 24 GB) | Zero-cost public-cloud fallback          | **€0** — forever                    |

  None of the three is a "preferred" target. They exist because VelaFlow
  must not care *where* the host comes from, as long as it is Linux,
  reachable over SSH, and free.

- **Shared module**: `deploy/terraform/modules/velaflow-host/` renders a
  cloud-init document (Podman + systemd unit + nginx vhost + data-dir
  permissions) and either (a) writes it via SSH + remote-exec onto a
  live host (proxmox, generic-vm) or (b) hands it to the caller as a
  string so the caller can attach it natively to a cloud VM's
  `user_data` (oracle-cloud). Either way the VelaFlow install itself is
  strictly declarative.

- **Secondary / dev path**: `scripts/install.sh` remains supported for
  rapid single-host bring-up on a dev LXC or laptop. The script header
  and the README make this scope explicit.

- **Preflight**: `scripts/terraform_preflight.py` performs CLI-free
  structural validation of any Terraform target (HCL balance,
  `required_version`, `tfvars.example` coverage, module source
  resolution, template UTF-8). The same checks run under pytest via
  `tests/test_terraform_modules.py` so a malformed `.tf` fails CI.

## Consequences

### Wins

- Reproducible, peer-reviewable infrastructure.
- `terraform plan` shows drift; `terraform destroy` is explicit and auditable.
- The operator's choice of host is a Terraform target swap, not an
  architectural migration. Moving VelaFlow from a Proxmox LXC to a VPS
  to Oracle Cloud Always Free (and back) is a `terraform apply` in a
  different target directory.
- `install.sh` keeps the two-minute "try it on my laptop" story without
  being the production surface.

### Costs

- Two deployment paths now need to be maintained. We accept this: the
  Bash path is a thin wrapper (~120 lines) and changes rarely, and the
  Terraform path is the only one that changes per production release.
- Each target's provider has its own upgrade cadence. Pinning is done
  in each target's `versions.tf`, not globally.

### Non-goals

- **No paid-cloud deployment target (AWS, Azure, GCP, Databricks) is
  shipped in this release.** The three free-tier targets
  (Proxmox, `generic-vm`, Oracle Cloud Always Free) cover the entire
  expected user base at Stage 0 and prove portability across the three
  dominant host shapes (container, VM, cloud VM). Paid-cloud targets
  are deliberately held back to keep the "free forever" invariant
  honest — an operator cannot accidentally pick a target that costs
  the maintainer money.
- Paid-cloud targets are not ruled out forever. They are **future
  options**: if user demand grows past what free tiers can serve, a
  target such as Databricks Asset Bundles, AWS ECS, Azure Container
  Apps, or GKE can be added as an **opt-in Stage 1 / Stage 2** target
  per `docs/scaling-path.md`, funded from VIP revenue. The architectural
  ground for that option is already cleared: the existing shared
  module renders cloud-init, and a cloud-native target would reuse the
  same configuration schema.

## References

- `deploy/terraform/README.md`
- `deploy/terraform/modules/velaflow-host/main.tf`
- `deploy/terraform/proxmox/main.tf`
- `deploy/terraform/generic-vm/main.tf`
- `deploy/terraform/oracle-cloud/main.tf`
- `tests/test_terraform_preflight.py`
- `scripts/install.sh` — unchanged, now explicitly dev-only.
