"""Tests for the task queue and worker."""

from __future__ import annotations

import pytest

from brain.queue.tasks import MessageType, QueueMessage, TaskQueue


class TestQueueMessage:
    def test_json_roundtrip(self):
        msg = QueueMessage(
            message_id="msg_001",
            message_type=MessageType.PIPELINE_RUN,
            tenant_id="tn_001",
            payload={"key": "value"},
        )
        json_str = msg.to_json()
        restored = QueueMessage.from_json(json_str)
        assert restored.message_id == "msg_001"
        assert restored.message_type == MessageType.PIPELINE_RUN
        assert restored.tenant_id == "tn_001"
        assert restored.payload == {"key": "value"}

    def test_default_retry_count(self):
        msg = QueueMessage(
            message_id="msg_002",
            message_type=MessageType.DIGEST_GENERATE,
            tenant_id="tn_002",
        )
        assert msg.retry_count == 0
        assert msg.max_retries == 3


class TestTaskQueue:
    def test_enqueue_dequeue(self):
        q = TaskQueue()
        msg = QueueMessage(
            message_id="m1",
            message_type=MessageType.PIPELINE_RUN,
            tenant_id="tn_001",
        )
        q.enqueue(msg)
        assert q.depth == 1

        result = q.dequeue(timeout=1)
        assert result is not None
        assert result.message_id == "m1"

    def test_dequeue_empty_returns_none(self):
        q = TaskQueue()
        result = q.dequeue(timeout=0.1)
        assert result is None

    def test_depth(self):
        q = TaskQueue()
        assert q.depth == 0
        for i in range(5):
            q.enqueue(QueueMessage(
                message_id=f"m{i}",
                message_type=MessageType.PIPELINE_RUN,
                tenant_id="tn_001",
            ))
        assert q.depth == 5

    def test_requeue_with_retries(self):
        q = TaskQueue()
        msg = QueueMessage(
            message_id="retry_msg",
            message_type=MessageType.PIPELINE_RUN,
            tenant_id="tn_001",
            max_retries=2,
        )

        assert q.requeue(msg) is True
        assert msg.retry_count == 1

        msg2 = q.dequeue(timeout=1)
        assert q.requeue(msg2) is True
        assert msg2.retry_count == 2

        msg3 = q.dequeue(timeout=1)
        # Max retries reached
        assert q.requeue(msg3) is False
        assert q.dead_letter_count == 1

    def test_mark_done(self):
        q = TaskQueue()
        q.enqueue(QueueMessage(
            message_id="done_msg",
            message_type=MessageType.PIPELINE_RUN,
            tenant_id="tn_001",
        ))
        q.dequeue(timeout=1)
        q.mark_done()
        assert q.processed_count == 1

    def test_fifo_order(self):
        q = TaskQueue()
        for i in range(3):
            q.enqueue(QueueMessage(
                message_id=f"ord_{i}",
                message_type=MessageType.PIPELINE_RUN,
                tenant_id="tn_001",
            ))
        ids = [q.dequeue(timeout=1).message_id for _ in range(3)]
        assert ids == ["ord_0", "ord_1", "ord_2"]
