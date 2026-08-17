
from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from app.agents.runtime import AgentRunResult
from app.agents.runtime import AgentContext
from app.agents.events import RuntimeEvent, RuntimeEventType
from app.models.entities import AgentRunTrace, ChatSession, UserAccount
from app.core.enums import IntentType
from app.services.planning import TaskPlanVerifier


class AgentTraceService:
    def __init__(self, db: Session):
        self.db = db

    def save_run(
        self,
        user: UserAccount,
        session: ChatSession,
        original_input: str,
        sanitized_input: str,
        memory_brief: str,
        agent_run: AgentRunResult,
        report_id: int | None,
    ) -> AgentRunTrace:
        trace = AgentRunTrace(
            user_id=user.id,
            session_id=session.id,
            report_id=report_id,
            intent=agent_run.intent.value,
            risk_level=agent_run.risk_level.value,
            original_input=original_input,
            sanitized_input=sanitized_input,
            memory_brief=memory_brief,
            agent_steps_json=_json(agent_run.steps),
            runtime_events_json=_json(agent_run.events),
            retrieved_knowledge_json=_json(agent_run.retrieved_knowledge),
            response_messages_json=_json(agent_run.response_messages),
            assessment_json=_json(agent_run.assessment or {}),
        )
        self.db.add(trace)
        self.db.commit()
        self.db.refresh(trace)
        return trace

    def save_failed_run(
        self,
        user: UserAccount,
        session: ChatSession,
        original_input: str,
        sanitized_input: str,
        exc: Exception,
        context: AgentContext | None = None,
    ) -> AgentRunTrace:
        events = list(context.events) if context is not None else []
        elapsed_ms = None
        if context is not None:
            import time

            elapsed_ms = round((time.monotonic() - context.started_monotonic) * 1000, 3)
        failure = RuntimeEvent(
            sequence=len(events) + 1,
            type=RuntimeEventType.RUN_FAILED,
            producer="AgentRuntime",
            correlation_id=session.public_id,
            payload={
                "failedStage": _failed_stage(context),
                "errorType": type(exc).__name__,
                "retryable": _is_retryable(exc),
                "totalDurationMs": elapsed_ms,
                "completedSteps": len(context.steps) if context is not None else 0,
            },
        )
        events.append(failure)
        trace = AgentRunTrace(
            user_id=user.id,
            session_id=session.id,
            report_id=None,
            intent=context.intent.value if context is not None and context.intent is not None else "UNKNOWN",
            risk_level=context.risk_level.value if context is not None else "LOW",
            original_input=original_input,
            sanitized_input=sanitized_input,
            memory_brief=context.memory_brief if context is not None else "",
            agent_steps_json=_json(context.steps if context is not None else []),
            runtime_events_json=_json(events),
            retrieved_knowledge_json=_json(context.retrieved_knowledge if context is not None else []),
            response_messages_json=_json(context.response_messages if context is not None else []),
            assessment_json=_json(context.assessment if context is not None and context.assessment is not None else {}),
        )
        self.db.add(trace)
        self.db.commit()
        self.db.refresh(trace)
        return trace

    def append_event(
        self,
        trace_id: int,
        event_type: RuntimeEventType,
        producer: str,
        correlation_id: str,
        payload: dict[str, Any],
    ) -> None:
        trace = self.db.query(AgentRunTrace).filter(AgentRunTrace.id == trace_id).with_for_update().first()
        self._append_to_trace(trace, event_type, producer, correlation_id, payload)

    def append_report_event(
        self,
        report_id: int,
        event_type: RuntimeEventType,
        producer: str,
        payload: dict[str, Any],
    ) -> None:
        trace = (
            self.db.query(AgentRunTrace)
            .filter(AgentRunTrace.report_id == report_id)
            .order_by(AgentRunTrace.id.desc())
            .with_for_update()
            .first()
        )
        session = self.db.get(ChatSession, trace.session_id) if trace is not None else None
        self._append_to_trace(trace, event_type, producer, session.public_id if session is not None else "", payload)

    def _append_to_trace(
        self,
        trace: AgentRunTrace | None,
        event_type: RuntimeEventType,
        producer: str,
        correlation_id: str,
        payload: dict[str, Any],
    ) -> None:
        if trace is None:
            return
        try:
            events = json.loads(trace.runtime_events_json or "[]")
        except (TypeError, json.JSONDecodeError):
            events = []
        _update_task_plan(events, event_type, payload)
        event = RuntimeEvent(
            sequence=len(events) + 1,
            type=event_type,
            producer=producer,
            correlation_id=correlation_id,
            payload=payload,
        )
        events.append(_to_jsonable(event))
        trace.runtime_events_json = json.dumps(events, ensure_ascii=False, default=str)
        self.db.add(trace)
        self.db.commit()


def _json(value: Any) -> str:
    return json.dumps(_to_jsonable(value), ensure_ascii=False, default=str)


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _to_jsonable(asdict(value))
    if hasattr(value, "model_dump"):
        return _to_jsonable(value.model_dump())
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    return value


def _failed_stage(context: AgentContext | None) -> str:
    if context is None:
        return "RuntimeInitialization"
    if not context.memory_loaded:
        return "MemoryAgent"
    if not context.intent_routed:
        return "SupervisorAgent"
    if context.intent != IntentType.CHAT and not context.knowledge_handled:
        return "KnowledgeAgent"
    if not context.risk_assessed:
        return "RiskGuardianAgent"
    if not context.response_planned:
        return "CompanionAgent" if context.intent == IntentType.CHAT else "CounselorAgent"
    return "CoordinatorAgent"


def _is_retryable(exc: Exception) -> bool:
    error_type = type(exc).__name__.lower()
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    return (
        "timeout" in error_type
        or "connect" in error_type
        or "operationalerror" in error_type
        or status_code == 429
        or (isinstance(status_code, int) and status_code >= 500)
    )


def _update_task_plan(events: list[dict], event_type: RuntimeEventType, payload: dict[str, Any]) -> None:
    if not event_type.value.startswith("TOOL_"):
        return
    plan_status = payload.get("planStatus")
    if not plan_status:
        return
    for event in reversed(events):
        task_plan = (event.get("payload") or {}).get("taskPlan")
        if not isinstance(task_plan, dict):
            continue
        for step in task_plan.get("steps", []):
            if step.get("id") == "tools":
                step["status"] = plan_status
                step["updatedByEvent"] = event_type.value
                step["jobSummary"] = payload.get("jobSummary", {})
                phase = "TERMINAL" if plan_status in {"COMPLETED", "FAILED"} else "ASYNC_TOOLS"
                task_plan["verification"] = TaskPlanVerifier.verify_dict(task_plan, phase)
                event["payload"]["planVerification"] = task_plan["verification"]
                return
