# ADR 0001 — DuckDB + SQLite today, Databricks as a future migration

- **Status**: Accepted
- **Date**: 2026-04-21
- **Context**: R21 architecture review ([`V3-CRITIQUE`](../../V3-CRITIQUE-Scaling%20VelaFlow%20for%20Senior%20Databricks%20Roles.txt))

## Context

VelaFlow is a privacy-first, self-hosted, multi-tenant productivity platform.
It ships as a single LXC / container that the operator runs on any €5 VPS or a
Proxmox home lab. The advertised operator cost is **€0 / month** on the free
tier and the same container is used across every paid tier.

A public review suggested replacing the analytical engine with Databricks:
migrate systemd-timer batch polls to Databricks Autoloader, replace the
SQLite data catalog with Unity Catalog, and index Gold-layer Delta tables
with Databricks Vector Search.

That suggestion is valid **at a scale we do not have yet** and **on a cost
structure we explicitly reject today**. This ADR records why the current
stack is the correct choice for the current stage, and documents the
migration path we would take if scale or funding change.

## Decision

We keep:

- **DuckDB** as the analytical engine for the Medallion pipeline (Bronze /
  Silver / Gold) running in-process inside the VelaFlow container.
- **SQLite** as the data catalog for tenant-scoped tables, schemas and lineage.
- **systemd timers** (or the embedded `TenantScheduler` when running
  containerised) for periodic ingestion from Todoist / Notion / Calendar.

We reject Databricks Autoloader, Unity Catalog and Databricks Vector Search
**at this stage** for the reasons below.

## Consequences

### Why the current choice is right now

1. **Hard cost ceiling**. A Databricks workspace is not free. The project's
   tier-1 promise is that a free user can self-host and pay nothing. Moving
   the analytical engine to a managed PaaS breaks that promise.
2. **Single-tenant-per-container deployment model**. A realistic VelaFlow
   operator runs on one VM with a handful of tenants (family, friends, early
   paying users). DuckDB handles this workload on a 512 MB container. Spark
   does not.
3. **Data sovereignty**. The operator controls the disk. All Bronze / Silver
   / Gold files live on a volume the operator owns, with tenant-scoped
   encryption at rest. Databricks would move that data plane into a third
   party.
4. **Complexity / operator-hours**. Unity Catalog governance at enterprise
   scale is a real job; it is not something a solo operator should babysit
   while serving a dozen tenants.

### Why the critique is still right in the limit

If VelaFlow ever reaches a few thousand active tenants with heavy streaming
ingest, DuckDB-in-process stops being the right tool. At that scale we would
migrate, and we already know what the migration looks like:

| Today (DuckDB + SQLite)                  | At scale (Databricks)                         |
|------------------------------------------|------------------------------------------------|
| systemd timers polling every 15 min      | **Databricks Autoloader** on cloud object store |
| SQLite catalog                           | **Unity Catalog** (metastore + data access policies) |
| Per-tenant row filter in DuckDB          | **Catalog + schema + row-level RBAC** in Unity |
| Field-level Fernet encryption            | Kept — plus Unity column masking policies      |
| Medallion SQL in DuckDB                  | Medallion SQL on Delta tables in Spark         |
| Local Vector Store (DuckDB VSS)          | **Databricks Vector Search** on Delta Gold     |

The migration is **deliberately compatible**: the Medallion contract (table
names, columns, processing order) is identical on either engine, so a port
is a driver change, not a rewrite.

### Trigger conditions for the migration

We would start the migration when **any** of these is true:

- Peak concurrent tenants > 500.
- Daily Bronze ingestion volume > 5 GB per tenant on average.
- A funded customer requires Unity Catalog audit trails (common in regulated
  industries).

Until then, the current stack is the deliberate, informed choice.

## References

- `src/brain/engine/processor.py` — DuckDB Medallion processor.
- `src/brain/catalog/` — SQLite data catalog.
- `src/brain/rag.py` — DuckDB VSS vector store.
- `docs/scaling-path.md` — full migration design.
