from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable


class RuntimeEventType(str, Enum):
    USER_MESSAGE_RECEIVED = "USER_MESSAGE_RECEIVED"
    MEMORY_LOADED = "MEMORY_LOADED"
    INTENT_ROUTED = "INTENT_ROUTED"
    KNOWLEDGE_RETRIEVED = "KNOWLEDGE_RETRIEVED"
    RISK_ASSESSED = "RISK_ASSESSED"
    RESPONSE_PLANNED = "RESPONSE_PLANNED"
    PLAN_STEP_RETRYING = "PLAN_STEP_RETRYING"
    PLAN_STEP_FAILED = "PLAN_STEP_FAILED"
    RUN_COMPLETED = "RUN_COMPLETED"
    RUN_FAILED = "RUN_FAILED"
    LLM_STREAM_COMPLETED = "LLM_STREAM_COMPLETED"
    LLM_STREAM_FAILED = "LLM_STREAM_FAILED"
    TOOL_QUEUED = "TOOL_QUEUED"
    TOOL_STARTED = "TOOL_STARTED"
    TOOL_REQUEUED = "TOOL_REQUEUED"
    TOOL_COMPLETED = "TOOL_COMPLETED"
    TOOL_DEAD_LETTERED = "TOOL_DEAD_LETTERED"
    RESPONSE_REVIEWED = "RESPONSE_REVIEWED"
    RESPONSE_REVIEW_RESOLVED = "RESPONSE_REVIEW_RESOLVED"


@dataclass(frozen=True)
class RuntimeEvent:
    sequence: int
    type: RuntimeEventType
    producer: str
    correlation_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)


EventHandler = Callable[[RuntimeEvent], None]


class RuntimeEventBus:
    """In-process FIFO event bus used to coordinate one bounded Agent run."""

    def __init__(self, correlation_id: str):
        self.correlation_id = correlation_id
        self._subscribers: dict[RuntimeEventType, list[EventHandler]] = defaultdict(list)
        self._queue: deque[RuntimeEvent] = deque()
        self.history: list[RuntimeEvent] = []

    def subscribe(self, event_type: RuntimeEventType, handler: EventHandler) -> None:
        self._subscribers[event_type].append(handler)

    def publish(self, event_type: RuntimeEventType, producer: str, payload: dict[str, Any] | None = None) -> RuntimeEvent:
        event = RuntimeEvent(
            sequence=len(self.history) + len(self._queue) + 1,
            type=event_type,
            producer=producer,
            correlation_id=self.correlation_id,
            payload=payload or {},
        )
        self._queue.append(event)
        return event

    def dispatch(self, max_events: int) -> list[RuntimeEvent]:
        while self._queue:
            if len(self.history) >= max_events:
                raise RuntimeError(f"runtime event limit exceeded: {max_events}")
            event = self._queue.popleft()
            self.history.append(event)
            for handler in self._subscribers.get(event.type, []):
                handler(event)
        return list(self.history)
