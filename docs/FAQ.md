# VelaFlow FAQ

Quick answers that do not fit neatly in the README or a design document.
Longer rationale for anything decision-shaped lives in `docs/adr/`.

## Why isn't native RAG in the premium tier?

Native on-box RAG (`/api/v1/rag/*`) is the single differentiator that
justifies the VIP tier against a generic ChatGPT Plus subscription at
the same price point. If premium received it too, there would be no
reason to upgrade. Premium keeps the NotebookLM export workflow, which
covers the same use case with a different (externalised) trust model.

See [`adr/0002-local-rag-vs-mosaic-ai.md`](adr/0002-local-rag-vs-mosaic-ai.md).

## Why three Terraform targets and no "primary"?

VelaFlow's operating promise is **€0 self-host, forever, on modest hardware**.
Pinning the production surface to any one cloud — even a "free tier" one —
silently risks the zero-cost promise if that tier changes. The three targets
under `deploy/terraform/` are equal-status **and all €0**:

- `proxmox/` — the developer's own Proxmox LXC on an Intel N95 / 8 GB mini-PC (**self-hosted hardware**).
- `generic-vm/` — any SSH-reachable Linux host the operator already owns (**reused capacity**).
- `oracle-cloud/` — OCI **Always Free** Ampere A1.Flex (4 OCPU / 24 GB RAM, free forever per OCI's published Always Free tier).

All three call the same `modules/velaflow-host/` module, so the install
on disk is identical. Moving between them is a `terraform apply` in a
different target directory.

## Is hosting VelaFlow really free even if standard/free users sign up?

Yes. All three **shipped** deployment targets are zero-cost indefinitely
at steady state. The only paid tier is **VIP**, whose subscription price
is set *above* marginal cost — so if VIP users join, the project makes a
small profit, and if only free/standard users join, the project still
costs the maintainer €0 to run. No paid cloud services (Databricks, Azure,
AWS, GCP) are used in the shipped deployment. If demand later grows past
what free tiers can serve, a paid-cloud Stage 1/2 target (including
Databricks) can be added as an opt-in, funded by VIP revenue; see
[`scaling-path.md`](scaling-path.md).

## Why not ship Azure / AWS / GCP / Databricks Terraform?

Those clouds are not free. Shipping vendor-specific Terraform for them
**in this release** would implicitly endorse a paid deployment path that
a future operator could pick accidentally, and that would quietly break
the zero-cost invariant above. The `generic-vm/` target works against
any Linux VM produced by any means (including a paid Azure VM, EC2, or
GCE instance) — but that choice is the operator's, not VelaFlow's.

Paid-cloud targets are **not** ruled out forever. They are documented
future scaling options: if user demand grows beyond what free tiers
can serve, a managed-runtime target (e.g. **Databricks Asset Bundles**,
AWS ECS, Azure Container Apps, GKE) can be introduced as a Stage 1 /
Stage 2 opt-in per [`scaling-path.md`](scaling-path.md), funded from VIP
revenue, never out of the maintainer's pocket. The groundwork for that
option is already in place: the shared Terraform module renders cloud-init
and the `brain` application is container-ready, so a cloud-native target
would reuse the same configuration schema.

See [`adr/0003-terraform-iac-vs-bash-install.md`](adr/0003-terraform-iac-vs-bash-install.md).

## Why keep `install.sh` if Terraform is the production path?

A developer spinning up a throw-away LXC for half an hour should not
have to configure a Terraform backend first. `install.sh` is the 30-second
quick-start; its header now says so in uppercase. It is deliberately not
the production path.

## Why DuckDB + SQLite instead of Databricks?

Every trigger that would justify Databricks (tenants > 500, ingest > 5
GB/tenant/day, regulated-customer audit requirements) is not yet
present. The migration mapping to Databricks / Unity Catalog / Mosaic AI
/ Delta Lake is documented end-to-end in
[`adr/0001-duckdb-sqlite-vs-databricks.md`](adr/0001-duckdb-sqlite-vs-databricks.md)
and [`scaling-path.md`](scaling-path.md), so when a trigger fires the
migration is a driver change, not a rewrite.

## What's the hardware floor?

Intel N95 / 4C / 8 GB RAM, running an unprivileged Proxmox LXC. VelaFlow
steady-state RSS is ~500 MB. The optional `velaflow[premium]`
sentence-transformers extra pushes this to ~1.5 GB — skip it on an N95 and
keep the default hashing embedder.

## How do I validate a Terraform target without the Terraform CLI?

```powershell
python scripts/terraform_preflight.py deploy/terraform/proxmox
```

The same checks run under `pytest` via
`tests/test_terraform_modules.py`, so a malformed `.tf` fails CI too.

## How is tenant data isolated inside the RAG store?

Every DuckDB read and write is `WHERE tenant_id = ?`. The test
`tests/test_api_rag.py::TestRAGTenantIsolation` has one tenant ingest a
document containing "tenant one private data" and another tenant query
"private data" — the assertion is that the second tenant's hits must
not include the first tenant's document id. The CI suite fails if that
invariant breaks.

## Who maintains VelaFlow?

One person. The code is written with heavy use of GitHub Copilot; the
architecture, threat model, and every remediation in
[`SECURITY-AUDIT.md`](SECURITY-AUDIT.md) are the maintainer's.
