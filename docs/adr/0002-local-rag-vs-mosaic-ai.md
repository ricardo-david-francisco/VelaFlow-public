# ADR 0002 — Local RAG on DuckDB VSS, not a managed vector DB

- **Status**: Accepted
- **Date**: 2026-04-21
- **Context**: R21 architecture review

## Context

The earlier VelaFlow release used [NotebookLM](../notebooklm-setup.md) via a
Playwright-driven browser automation as a pragmatic Retrieval-Augmented
Generation surface. The review correctly pointed out that the browser-control
path is brittle: a single CSS change on the vendor side breaks the flow.

## Decision

We add a **native, 100 %-local RAG pipeline** gated behind the `USE_RAG`
permission. The gate is **VIP-only** (plus `demo` for evaluation and
`admin` for ops). The `premium` tier explicitly does **not** receive
native RAG — it keeps the NotebookLM export workflow as its RAG-like
surface. The native pipeline is implemented as:

- **Vector store**: DuckDB with `list_cosine_similarity` over a `FLOAT[]`
  column. Reuses the existing DuckDB dependency — zero new infrastructure.
- **Embedder**: a deterministic trigram-hashing `SimpleEmbedder` as the
  default (fits the 512 MB container budget). Operators can swap in
  `sentence-transformers` via the optional `velaflow[premium]` extra.
- **Chunker**: sentence-aware splitter with configurable overlap.
- **HTTP surface**: `POST /api/v1/rag/ingest`, `POST /api/v1/rag/query`,
  `GET /api/v1/rag/stats`, `DELETE /api/v1/rag/documents/{document_id}`.
- **Tenant isolation**: every DuckDB query is `WHERE tenant_id = ?`; the
  test suite contains an explicit cross-tenant leakage assertion.
- **Per-tier document quota**: `demo = 5`, `vip = 1000`, `admin =
  unlimited`. Quota overruns return HTTP `429`.

NotebookLM remains the **premium-tier** RAG surface (browser-automated
corpus upload + Q&A export). It is also available to VIP operators who
want to continue using it alongside the native pipeline.

### Why VIP-only

The VIP tier is priced at roughly the same point as ChatGPT Plus. For
that price it must deliver a differentiator that a generic LLM
subscription cannot: **the user's own private corpus, indexed on the
user's own hardware, never shared with any third party, queried via the
operator's own LLM endpoint**. Giving the same capability to premium
collapses the pricing gap and removes the reason to upgrade.

## Consequences

### Wins

- Retrieval stays on the operator's disk. No third-party sees query text.
- The pipeline is deterministic and hermetic, so the CI suite tests it end
  to end without any external service.
- The API contract (`/rag/ingest`, `/rag/query`) is embedder-agnostic.
  Moving to a real transformer model, or later to Mosaic AI / Databricks
  Vector Search, is a dependency-injection swap in
  `brain.api.dependencies.get_rag_pipeline`.

### Costs

- The default `SimpleEmbedder` is a baseline, not a production-grade model.
  Users on the VIP tier who enable the `sentence-transformers` extra get
  materially better recall at the cost of ~80 MB of additional RAM.

### Scaling path

When DuckDB VSS stops scaling (see ADR 0001's trigger conditions), the
vector store is swapped for **Databricks Vector Search on Gold Delta
tables**, using the same `RAGPipeline.query` entry point. No API change is
required.

## References

- `src/brain/rag.py`
- `src/brain/api/routes/rag.py`
- `tests/test_rag.py`, `tests/test_api_rag.py`
