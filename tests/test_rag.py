"""Tests for the RAG pipeline (Retrieval-Augmented Generation)."""

from __future__ import annotations

import os
import pytest

from brain.rag import (
    DocumentChunk,
    DocumentChunker,
    RAGPipeline,
    RetrievalResult,
    SimpleEmbedder,
    VectorStore,
)


class TestDocumentChunker:
    """Verify sentence-aware chunking logic."""

    def test_short_text_single_chunk(self) -> None:
        chunker = DocumentChunker(chunk_size=512, chunk_overlap=64)
        chunks = chunker.chunk("Hello world.", "doc1", "tenant1")
        assert len(chunks) == 1
        assert chunks[0].content == "Hello world."
        assert chunks[0].document_id == "doc1"
        assert chunks[0].tenant_id == "tenant1"

    def test_empty_text_no_chunks(self) -> None:
        chunker = DocumentChunker(chunk_size=512, chunk_overlap=64)
        chunks = chunker.chunk("", "doc1", "tenant1")
        assert chunks == []

    def test_whitespace_only_no_chunks(self) -> None:
        chunker = DocumentChunker(chunk_size=512, chunk_overlap=64)
        chunks = chunker.chunk("   \n  \t  ", "doc1", "tenant1")
        assert chunks == []

    def test_long_text_multiple_chunks(self) -> None:
        # Create text with many sentences, longer than chunk_size
        text = " ".join(f"Sentence number {i}." for i in range(50))
        chunker = DocumentChunker(chunk_size=100, chunk_overlap=20)
        chunks = chunker.chunk(text, "doc2", "t1")
        assert len(chunks) > 1
        # All chunks should have the same document_id and tenant_id
        for c in chunks:
            assert c.document_id == "doc2"
            assert c.tenant_id == "t1"

    def test_chunk_ids_are_unique(self) -> None:
        text = "Sentence one. Sentence two. Sentence three. Sentence four."
        chunker = DocumentChunker(chunk_size=30, chunk_overlap=5)
        chunks = chunker.chunk(text, "doc3", "t1")
        chunk_ids = [c.chunk_id for c in chunks]
        assert len(chunk_ids) == len(set(chunk_ids))

    def test_oversized_document_rejected(self) -> None:
        # 6MB exceeds the 5MB limit
        text = "x" * (6 * 1024 * 1024)
        chunker = DocumentChunker()
        with pytest.raises(ValueError, match="exceeds"):
            chunker.chunk(text, "big", "t1")


class TestSimpleEmbedder:
    """Verify offline embedding logic."""

    def test_embed_returns_correct_dimension(self) -> None:
        embedder = SimpleEmbedder()
        vec = embedder.embed("hello world")
        assert len(vec) == 128

    def test_embed_is_normalized(self) -> None:
        embedder = SimpleEmbedder()
        vec = embedder.embed("test text")
        norm = sum(x * x for x in vec) ** 0.5
        assert abs(norm - 1.0) < 0.01

    def test_similar_texts_have_high_similarity(self) -> None:
        embedder = SimpleEmbedder()
        v1 = embedder.embed("the quick brown fox")
        v2 = embedder.embed("the quick brown dog")
        cosine = sum(a * b for a, b in zip(v1, v2))
        assert cosine > 0.5

    def test_different_texts_lower_similarity(self) -> None:
        embedder = SimpleEmbedder()
        v1 = embedder.embed("the quick brown fox jumps over the lazy dog")
        v2 = embedder.embed("quantum mechanics particle wave duality")
        cosine = sum(a * b for a, b in zip(v1, v2))
        # Not necessarily negative, but should be lower than very similar texts
        v3 = embedder.embed("the quick brown fox jumps over the lazy cat")
        cosine_similar = sum(a * b for a, b in zip(v1, v3))
        assert cosine_similar > cosine

    def test_empty_text_returns_zero_vector(self) -> None:
        embedder = SimpleEmbedder()
        vec = embedder.embed("")
        assert all(v == 0.0 for v in vec)


class TestVectorStore:
    """Verify DuckDB-backed vector storage."""

    @pytest.fixture()
    def store(self, tmp_path) -> VectorStore:
        db_path = str(tmp_path / "test_rag.duckdb")
        return VectorStore(db_path)

    def test_store_and_search(self, store) -> None:
        embedder = SimpleEmbedder()
        chunks = [
            DocumentChunk(
                chunk_id="c1", document_id="d1", tenant_id="t1",
                content="Python is great", metadata={},
                embedding=embedder.embed("Python is great"),
            ),
            DocumentChunk(
                chunk_id="c2", document_id="d1", tenant_id="t1",
                content="Java is verbose", metadata={},
                embedding=embedder.embed("Java is verbose"),
            ),
        ]
        store.store_chunks(chunks)
        results = store.search("t1", embedder.embed("Python programming"), top_k=2)
        assert len(results) > 0
        # The Python chunk should rank first
        assert results[0].content == "Python is great"

    def test_tenant_isolation(self, store) -> None:
        embedder = SimpleEmbedder()
        chunks_t1 = [
            DocumentChunk(
                chunk_id="c1", document_id="d1", tenant_id="t1",
                content="Tenant one data", metadata={},
                embedding=embedder.embed("Tenant one data"),
            ),
        ]
        chunks_t2 = [
            DocumentChunk(
                chunk_id="c2", document_id="d2", tenant_id="t2",
                content="Tenant two data", metadata={},
                embedding=embedder.embed("Tenant two data"),
            ),
        ]
        store.store_chunks(chunks_t1)
        store.store_chunks(chunks_t2)

        results_t1 = store.search("t1", embedder.embed("data"), top_k=10)
        results_t2 = store.search("t2", embedder.embed("data"), top_k=10)

        assert all(r.document_id == "d1" for r in results_t1)
        assert all(r.document_id == "d2" for r in results_t2)

    def test_delete_document(self, store) -> None:
        embedder = SimpleEmbedder()
        chunks = [
            DocumentChunk(
                chunk_id="c1", document_id="d1", tenant_id="t1",
                content="To be deleted", metadata={},
                embedding=embedder.embed("To be deleted"),
            ),
        ]
        store.store_chunks(chunks)
        assert store.count_documents("t1") == 1
        deleted = store.delete_document("t1", "d1")
        assert deleted > 0
        assert store.count_documents("t1") == 0

    def test_purge_tenant(self, store) -> None:
        embedder = SimpleEmbedder()
        for i in range(3):
            store.store_chunks([
                DocumentChunk(
                    chunk_id=f"c{i}", document_id=f"d{i}", tenant_id="t1",
                    content=f"Document {i}", metadata={},
                    embedding=embedder.embed(f"Document {i}"),
                ),
            ])
        assert store.count_documents("t1") == 3
        store.purge_tenant("t1")
        assert store.count_documents("t1") == 0

    def test_empty_search_returns_empty(self, store) -> None:
        embedder = SimpleEmbedder()
        results = store.search("nonexistent", embedder.embed("anything"), top_k=5)
        assert results == []


class TestRAGPipeline:
    """End-to-end RAG pipeline tests."""

    @pytest.fixture()
    def pipeline(self, tmp_path) -> RAGPipeline:
        db_path = str(tmp_path / "pipeline_rag.duckdb")
        store = VectorStore(db_path)
        return RAGPipeline(store)

    def test_ingest_and_query(self, pipeline) -> None:
        count = pipeline.ingest(
            "Machine learning models are trained on data.",
            "doc1", "t1", {},
        )
        assert count > 0
        results = pipeline.query("machine learning", "t1", top_k=3)
        assert len(results) > 0
        assert "machine learning" in results[0].content.lower() or "data" in results[0].content.lower()

    def test_ingest_quota_limit(self, pipeline) -> None:
        pipeline.ingest("First document.", "doc1", "t1", {})
        with pytest.raises(PermissionError, match="quota exceeded"):
            pipeline.ingest("Second document.", "doc2", "t1", {}, max_documents=1)

    def test_augment_prompt(self, pipeline) -> None:
        pipeline.ingest(
            "VelaFlow is a multi-tenant AI platform built on medallion architecture.",
            "doc1", "t1", {},
        )
        augmented = pipeline.augment_prompt(
            "What is VelaFlow?", "t1",
            "You are a helpful assistant.", top_k=3,
        )
        assert "VelaFlow" in augmented
        assert "helpful assistant" in augmented

    def test_delete_document(self, pipeline) -> None:
        pipeline.ingest("Test content.", "doc1", "t1", {})
        deleted = pipeline.delete_document("t1", "doc1")
        assert deleted > 0

    def test_purge_tenant(self, pipeline) -> None:
        pipeline.ingest("Content A.", "doc1", "t1", {})
        pipeline.ingest("Content B.", "doc2", "t1", {})
        pipeline.purge_tenant("t1")
        results = pipeline.query("content", "t1", top_k=5)
        assert results == []
