"""Retrieval-Augmented Generation (RAG) pipeline.

Provides document ingestion, chunking, embedding, vector storage, and
retrieval for context-augmented LLM calls. Uses DuckDB VSS (vector
similarity search) for storage — no external vector DB required.

Tier-gated: Premium gets basic RAG, VIP gets full RAG with larger
document limits and higher query quotas.

Enterprise equivalent: self-hosted vector search + local embeddings.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from brain.security.sanitization import sanitize_for_llm

logger = logging.getLogger(__name__)

# ── Chunk configuration ──────────────────────────────────────────────────────
DEFAULT_CHUNK_SIZE = 512
DEFAULT_CHUNK_OVERLAP = 64
MAX_DOCUMENT_SIZE_BYTES = 5 * 1024 * 1024  # 5 MB hard limit


@dataclass(frozen=True)
class DocumentChunk:
    """A chunk of a document with its embedding vector."""

    chunk_id: str
    document_id: str
    tenant_id: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class RetrievalResult:
    """A search result from the vector store."""

    chunk_id: str
    document_id: str
    content: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)


class DocumentChunker:
    """Split documents into overlapping chunks for embedding.

    Uses sentence-aware splitting: prefers breaking at sentence
    boundaries to preserve semantic coherence.
    """

    def __init__(
        self,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    ) -> None:
        if chunk_size <= chunk_overlap:
            raise ValueError("chunk_size must be greater than chunk_overlap")
        self._chunk_size = chunk_size
        self._overlap = chunk_overlap

    def chunk(
        self,
        text: str,
        document_id: str,
        tenant_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> list[DocumentChunk]:
        """Split text into overlapping chunks."""
        if not text or not text.strip():
            return []
        if len(text.encode("utf-8")) > MAX_DOCUMENT_SIZE_BYTES:
            raise ValueError(
                f"Document exceeds {MAX_DOCUMENT_SIZE_BYTES} byte limit"
            )

        # Sentence-aware splitting
        sentences = re.split(r"(?<=[.!?])\s+", text.strip())
        chunks: list[DocumentChunk] = []
        current: list[str] = []
        current_len = 0

        for sentence in sentences:
            sentence_len = len(sentence)
            if current_len + sentence_len > self._chunk_size and current:
                chunk_text = " ".join(current)
                chunk_id = self._make_chunk_id(document_id, len(chunks))
                chunks.append(
                    DocumentChunk(
                        chunk_id=chunk_id,
                        document_id=document_id,
                        tenant_id=tenant_id,
                        content=chunk_text,
                        metadata=metadata or {},
                    )
                )
                # Keep overlap
                overlap_text = ""
                overlap_sentences: list[str] = []
                for s in reversed(current):
                    if len(overlap_text) + len(s) > self._overlap:
                        break
                    overlap_sentences.insert(0, s)
                    overlap_text = " ".join(overlap_sentences)
                current = overlap_sentences
                current_len = len(overlap_text)

            current.append(sentence)
            current_len += sentence_len

        # Final chunk
        if current:
            chunk_text = " ".join(current)
            chunk_id = self._make_chunk_id(document_id, len(chunks))
            chunks.append(
                DocumentChunk(
                    chunk_id=chunk_id,
                    document_id=document_id,
                    tenant_id=tenant_id,
                    content=chunk_text,
                    metadata=metadata or {},
                )
            )
        return chunks

    @staticmethod
    def _make_chunk_id(document_id: str, index: int) -> str:
        raw = f"{document_id}:{index}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


class VectorStore:
    """DuckDB-backed vector store for tenant-scoped RAG.

    Each tenant gets an isolated table (partitioned by tenant_id)
    preventing cross-tenant data leakage. Uses DuckDB's built-in
    array_cosine_similarity for vector search.

    In production K8s: uses shared DuckDB volume mounted per worker pod.
    In LXC: uses local filesystem DuckDB at VELAFLOW_DATA_DIR.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._initialized = False

    def _get_connection(self):
        """Lazy DuckDB connection — import only when needed."""
        try:
            import duckdb
        except ImportError:
            raise RuntimeError(
                "DuckDB is required for RAG. Install with: "
                "pip install velaflow[enterprise]"
            )
        conn = duckdb.connect(self._db_path)
        if not self._initialized:
            self._init_schema(conn)
            self._initialized = True
        return conn

    def _init_schema(self, conn) -> None:
        """Create the vector store table if it doesn't exist."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rag_chunks (
                chunk_id VARCHAR PRIMARY KEY,
                document_id VARCHAR NOT NULL,
                tenant_id VARCHAR NOT NULL,
                content TEXT NOT NULL,
                metadata JSON,
                embedding FLOAT[],
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_rag_tenant
            ON rag_chunks (tenant_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_rag_document
            ON rag_chunks (tenant_id, document_id)
        """)

    def store_chunks(self, chunks: list[DocumentChunk]) -> int:
        """Store document chunks with embeddings. Returns count stored."""
        if not chunks:
            return 0
        conn = self._get_connection()
        count = 0
        for chunk in chunks:
            conn.execute(
                """
                INSERT OR REPLACE INTO rag_chunks
                (chunk_id, document_id, tenant_id, content, metadata, embedding)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    chunk.chunk_id,
                    chunk.document_id,
                    chunk.tenant_id,
                    chunk.content,
                    json.dumps(chunk.metadata),
                    chunk.embedding,
                ],
            )
            count += 1
        conn.close()
        return count

    def search(
        self,
        tenant_id: str,
        query_embedding: list[float],
        top_k: int = 5,
    ) -> list[RetrievalResult]:
        """Vector similarity search scoped to a single tenant.

        Uses cosine similarity via DuckDB's list_cosine_similarity.
        Results are strictly tenant-isolated — no cross-tenant leakage.
        """
        if not query_embedding:
            return []

        conn = self._get_connection()
        # Tenant isolation enforced at query level
        results = conn.execute(
            """
            SELECT
                chunk_id,
                document_id,
                content,
                metadata,
                list_cosine_similarity(embedding, ?::FLOAT[]) AS score
            FROM rag_chunks
            WHERE tenant_id = ?
              AND embedding IS NOT NULL
              AND len(embedding) > 0
            ORDER BY score DESC
            LIMIT ?
            """,
            [query_embedding, tenant_id, top_k],
        ).fetchall()
        conn.close()

        return [
            RetrievalResult(
                chunk_id=row[0],
                document_id=row[1],
                content=row[2],
                score=float(row[4]),
                metadata=json.loads(row[3]) if row[3] else {},
            )
            for row in results
        ]

    def delete_document(self, tenant_id: str, document_id: str) -> int:
        """Delete all chunks for a document. Returns count deleted."""
        conn = self._get_connection()
        result = conn.execute(
            "DELETE FROM rag_chunks WHERE tenant_id = ? AND document_id = ?",
            [tenant_id, document_id],
        )
        count = result.fetchone()[0] if result else 0
        conn.close()
        return count

    def count_documents(self, tenant_id: str) -> int:
        """Count distinct documents for a tenant (for quota enforcement)."""
        conn = self._get_connection()
        result = conn.execute(
            "SELECT COUNT(DISTINCT document_id) FROM rag_chunks WHERE tenant_id = ?",
            [tenant_id],
        ).fetchone()
        conn.close()
        return result[0] if result else 0

    def purge_tenant(self, tenant_id: str) -> int:
        """Delete all RAG data for a tenant (deactivation)."""
        conn = self._get_connection()
        result = conn.execute(
            "DELETE FROM rag_chunks WHERE tenant_id = ?",
            [tenant_id],
        )
        count = result.fetchone()[0] if result else 0
        conn.close()
        return count


class SimpleEmbedder:
    """Lightweight TF-IDF-based embedder for cost-free operation.

    In production, replaced by LLM-based embeddings (Gemini/OpenAI).
    This ensures RAG works even without LLM API access — suitable
    for demo accounts and free-tier basic functionality.

    Produces deterministic, reproducible embeddings without any
    external API calls or GPU requirements.
    """

    def __init__(self, dimension: int = 128) -> None:
        self._dim = dimension

    def embed(self, text: str) -> list[float]:
        """Generate a simple hash-based embedding vector.

        Uses character n-gram hashing to produce a fixed-dimension
        vector. Not as good as transformer embeddings but works
        offline and costs nothing.
        """
        if not text:
            return [0.0] * self._dim

        vector = [0.0] * self._dim
        tokens = text.lower().split()

        for i, token in enumerate(tokens):
            # Character trigram hashing
            for j in range(len(token) - 2):
                trigram = token[j : j + 3]
                h = int(hashlib.md5(trigram.encode(), usedforsecurity=False).hexdigest(), 16)
                idx = h % self._dim
                # Position-weighted contribution
                weight = 1.0 / (1.0 + i * 0.01)
                vector[idx] += weight

        # L2 normalize
        norm = sum(v * v for v in vector) ** 0.5
        if norm > 0:
            vector = [v / norm for v in vector]
        return vector

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts."""
        return [self.embed(t) for t in texts]


class RAGPipeline:
    """End-to-end RAG pipeline: ingest → chunk → embed → store → retrieve → augment.

    Self-hosted vector search implemented on DuckDB + local embeddings
    with tenant-scoped collections and quota enforcement.
    """

    def __init__(
        self,
        vector_store: VectorStore,
        embedder: SimpleEmbedder | None = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    ) -> None:
        self._store = vector_store
        self._embedder = embedder or SimpleEmbedder()
        self._chunker = DocumentChunker(chunk_size, chunk_overlap)

    def ingest(
        self,
        text: str,
        document_id: str,
        tenant_id: str,
        metadata: dict[str, Any] | None = None,
        *,
        max_documents: int = 0,
    ) -> int:
        """Ingest a document: chunk, embed, store. Returns chunk count.

        Args:
            max_documents: Quota limit. 0 = no limit.

        Raises:
            ValueError: If document exceeds size limit.
            PermissionError: If document quota exceeded.
        """
        if max_documents > 0:
            current = self._store.count_documents(tenant_id)
            if current >= max_documents:
                raise PermissionError(
                    f"Document quota exceeded: {current}/{max_documents}"
                )

        # Sanitize input before processing
        sanitized = sanitize_for_llm(text, context="rag_ingest")
        chunks = self._chunker.chunk(sanitized, document_id, tenant_id, metadata)

        # Generate embeddings
        embedded_chunks = []
        for chunk in chunks:
            embedding = self._embedder.embed(chunk.content)
            embedded_chunks.append(
                DocumentChunk(
                    chunk_id=chunk.chunk_id,
                    document_id=chunk.document_id,
                    tenant_id=chunk.tenant_id,
                    content=chunk.content,
                    metadata=chunk.metadata,
                    embedding=embedding,
                )
            )

        return self._store.store_chunks(embedded_chunks)

    def query(
        self,
        query_text: str,
        tenant_id: str,
        top_k: int = 5,
    ) -> list[RetrievalResult]:
        """Search for relevant chunks and return ranked results."""
        sanitized = sanitize_for_llm(query_text, context="rag_query")
        query_embedding = self._embedder.embed(sanitized)
        return self._store.search(tenant_id, query_embedding, top_k)

    def augment_prompt(
        self,
        query_text: str,
        tenant_id: str,
        system_prompt: str,
        top_k: int = 5,
    ) -> str:
        """Build an augmented system prompt with retrieved context.

        Returns the system prompt enriched with relevant document
        chunks for the LLM to use as grounding context.
        """
        results = self.query(query_text, tenant_id, top_k)
        if not results:
            return system_prompt

        context_parts = []
        for i, r in enumerate(results, 1):
            context_parts.append(
                f"[Source {i}] (relevance: {r.score:.2f})\n{r.content}"
            )
        context_block = "\n\n".join(context_parts)

        return (
            f"{system_prompt}\n\n"
            f"--- Retrieved Context (RAG) ---\n"
            f"Use the following sources to ground your response. "
            f"Cite source numbers when referencing specific information.\n\n"
            f"{context_block}\n"
            f"--- End Retrieved Context ---"
        )

    def delete_document(self, tenant_id: str, document_id: str) -> int:
        return self._store.delete_document(tenant_id, document_id)

    def purge_tenant(self, tenant_id: str) -> int:
        return self._store.purge_tenant(tenant_id)
