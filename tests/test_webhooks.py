"""Tests for the webhook routes (n8n integration)."""

from __future__ import annotations

import pytest
from brain.api.routes.webhooks import (
    WebhookCatalogRequest,
    WebhookCatalogResponse,
    WebhookLLMRequest,
    WebhookLLMResponse,
    WebhookPipelineRequest,
    WebhookResponse,
    WebhookTenantRequest,
)
from brain.queue.tasks import TaskQueue, MessageType


class TestWebhookModels:
    """Verify Pydantic models for webhook requests/responses."""

    def test_pipeline_request_defaults(self) -> None:
        req = WebhookPipelineRequest()
        assert req.todoist_tasks == []
        assert req.weekend_mode is False

    def test_pipeline_request_with_data(self) -> None:
        req = WebhookPipelineRequest(
            todoist_tasks=[{"id": "1", "content": "Test"}],
            weekend_mode=True,
        )
        assert len(req.todoist_tasks) == 1
        assert req.weekend_mode is True

    def test_response_defaults(self) -> None:
        resp = WebhookResponse(message_id="wh_abc123")
        assert resp.status == "queued"
        assert resp.message == ""

    def test_response_custom(self) -> None:
        resp = WebhookResponse(
            message_id="wh_abc123",
            status="queued",
            message="Pipeline run queued",
        )
        assert resp.message_id == "wh_abc123"

    def test_catalog_request(self) -> None:
        req = WebhookCatalogRequest(action="list_tables", schema_name="gold")
        assert req.action == "list_tables"
        assert req.schema_name == "gold"

    def test_catalog_response(self) -> None:
        resp = WebhookCatalogResponse(action="list_tables", data={"tables": []})
        assert resp.action == "list_tables"

    def test_llm_request(self) -> None:
        req = WebhookLLMRequest(prompt="Summarize my tasks")
        assert req.prompt == "Summarize my tasks"
        assert req.use_local is False

    def test_llm_request_local(self) -> None:
        req = WebhookLLMRequest(prompt="Private summary", use_local=True)
        assert req.use_local is True

    def test_tenant_request(self) -> None:
        req = WebhookTenantRequest(action="provision", config={"tier": "premium"})
        assert req.action == "provision"
        assert req.config["tier"] == "premium"


class TestWebhookQueueIntegration:
    """Verify that webhook handlers enqueue messages correctly."""

    def test_pipeline_enqueue(self) -> None:
        q = TaskQueue()
        assert q.depth == 0
        from brain.queue.tasks import QueueMessage

        msg = QueueMessage(
            message_id="wh_test1",
            message_type=MessageType.PIPELINE_RUN,
            tenant_id="tn_test",
            payload={"todoist_tasks": [{"id": "1"}]},
        )
        q.enqueue(msg)
        assert q.depth == 1
        dequeued = q.dequeue(timeout=1)
        assert dequeued is not None
        assert dequeued.message_type == MessageType.PIPELINE_RUN
        assert dequeued.tenant_id == "tn_test"

    def test_digest_enqueue(self) -> None:
        q = TaskQueue()
        from brain.queue.tasks import QueueMessage

        msg = QueueMessage(
            message_id="wh_test2",
            message_type=MessageType.DIGEST_GENERATE,
            tenant_id="tn_test",
        )
        q.enqueue(msg)
        assert q.depth == 1
        dequeued = q.dequeue(timeout=1)
        assert dequeued is not None
        assert dequeued.message_type == MessageType.DIGEST_GENERATE

    def test_llm_enqueue(self) -> None:
        q = TaskQueue()
        from brain.queue.tasks import QueueMessage

        msg = QueueMessage(
            message_id="wh_llm1",
            message_type=MessageType.DIGEST_GENERATE,
            tenant_id="tn_test",
            payload={"prompt": "Summarize tasks", "type": "llm_generate"},
        )
        q.enqueue(msg)
        assert q.depth == 1
        dequeued = q.dequeue(timeout=1)
        assert dequeued.payload["type"] == "llm_generate"

    def test_tenant_operation_enqueue(self) -> None:
        q = TaskQueue()
        from brain.queue.tasks import QueueMessage

        msg = QueueMessage(
            message_id="wh_tenant1",
            message_type=MessageType.PIPELINE_RUN,
            tenant_id="tn_admin",
            payload={"action": "provision", "type": "tenant_operation"},
        )
        q.enqueue(msg)
        dequeued = q.dequeue(timeout=1)
        assert dequeued.payload["type"] == "tenant_operation"
