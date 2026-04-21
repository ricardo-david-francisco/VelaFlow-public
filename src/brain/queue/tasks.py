"""Task Queue — Distributed task queue abstraction.

Provides an in-process queue (``TaskQueue``) for single-node LXC deployment.
A Redis-backed implementation is planned for v1.1 to support multi-node HA;
the KEDA manifests at ``deploy/kubernetes/keda-scaler.yaml`` already target
Redis list length for auto-scaling worker pods.
"""

from __future__ import annotations

import collections
import json
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class MessageType(str, Enum):
    PIPELINE_RUN = "pipeline_run"
    DIGEST_GENERATE = "digest_generate"
    LLM_GENERATE = "llm_generate"
    NOTION_SYNC = "notion_sync"
    BOARD_ANALYSIS = "board_analysis"
    SCORING_CONFIG = "scoring_config"
    TENANT_OPERATION = "tenant_operation"
    NOTEBOOKLM_EXTRACT = "notebooklm_extract"
    RAG_QUERY = "rag_query"


@dataclass
class QueueMessage:
    """A message in the task queue."""

    message_id: str
    message_type: MessageType
    tenant_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    retry_count: int = 0
    max_retries: int = 3

    def to_json(self) -> str:
        return json.dumps({
            "message_id": self.message_id,
            "message_type": self.message_type.value,
            "tenant_id": self.tenant_id,
            "payload": self.payload,
            "created_at": self.created_at.isoformat(),
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
        })

    @classmethod
    def from_json(cls, data: str) -> QueueMessage:
        d = json.loads(data)
        return cls(
            message_id=d["message_id"],
            message_type=MessageType(d["message_type"]),
            tenant_id=d["tenant_id"],
            payload=d.get("payload", {}),
            created_at=datetime.fromisoformat(d["created_at"]),
            retry_count=d.get("retry_count", 0),
            max_retries=d.get("max_retries", 3),
        )


class TaskQueue:
    """In-process task queue for local/LXC deployment.

    For production multi-node deployment, replace with RedisTaskQueue
    which uses Redis LPUSH/BRPOP for distributed work distribution.
    KEDA scales worker pods based on Redis list length.

    Usage:
        q = TaskQueue()
        q.enqueue(QueueMessage(...))
        msg = q.dequeue(timeout=5)
    """

    # R15-M5: cap dead-letter queue to prevent unbounded memory growth
    # under a sustained failure flood. Oldest entries are evicted first.
    DEAD_LETTER_MAX_SIZE = 10_000

    def __init__(self) -> None:
        self._queue: queue.Queue[QueueMessage] = queue.Queue()
        self._dead_letter: collections.deque[QueueMessage] = collections.deque(
            maxlen=self.DEAD_LETTER_MAX_SIZE
        )
        self._dead_letter_dropped = 0
        self._processed_count = 0
        self._lock = threading.Lock()

    def enqueue(self, message: QueueMessage) -> None:
        """Add a message to the queue."""
        self._queue.put(message)
        logger.debug(
            "Enqueued %s for tenant %s (depth=%d)",
            message.message_type.value,
            message.tenant_id,
            self.depth,
        )

    def dequeue(self, timeout: float = 5.0) -> QueueMessage | None:
        """Remove and return the next message, or None on timeout."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def requeue(self, message: QueueMessage) -> bool:
        """Re-enqueue a failed message if retries remain."""
        if message.retry_count >= message.max_retries:
            with self._lock:
                # R15-M5: deque(maxlen) auto-evicts oldest on overflow; count drops.
                if len(self._dead_letter) >= self.DEAD_LETTER_MAX_SIZE:
                    self._dead_letter_dropped += 1
                self._dead_letter.append(message)
            logger.warning(
                "Message %s moved to dead letter after %d retries",
                message.message_id,
                message.retry_count,
            )
            return False
        message.retry_count += 1
        self.enqueue(message)
        return True

    def mark_done(self) -> None:
        """Mark the current message as processed."""
        with self._lock:
            self._processed_count += 1
        self._queue.task_done()

    @property
    def depth(self) -> int:
        """Current queue depth (for KEDA scaling metric)."""
        return self._queue.qsize()

    @property
    def processed_count(self) -> int:
        return self._processed_count

    @property
    def dead_letter_count(self) -> int:
        return len(self._dead_letter)


# ── Default queue singleton ──────────────────────────────────────────

_default_queue: TaskQueue | None = None
_default_queue_lock = threading.Lock()


def get_default_queue() -> TaskQueue:
    """Get or create the module-level default queue (thread-safe singleton).

    Used by metrics, health checks, and the API layer to inspect
    queue depth without requiring dependency injection.
    """
    global _default_queue
    with _default_queue_lock:
        if _default_queue is None:
            _default_queue = TaskQueue()
        return _default_queue


def set_default_queue(q: TaskQueue) -> None:
    """Replace the default queue (for testing or Redis swap)."""
    global _default_queue
    with _default_queue_lock:
        _default_queue = q
