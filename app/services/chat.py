from __future__ import annotations

import json
import logging
import time

from sqlalchemy.orm import Session

from app.agents.harness import MindBridgeAgentHarness
from app.agents.events import RuntimeEventType
from app.core.config import Settings
from app.core.enums import RiskLevel
from app.models.entities import UserAccount
from app.schemas.dtos import ChatRequest, ChatStreamEvent
from app.services.ai import AiClient, AiStreamMetrics
from app.services.skill_output import SkillOutputPostProcessor
from app.services.skills import MindBridgeSkillLibrary
from app.services.response_review import ResponseReviewer
from app.services.review_escalation import ResponseReviewEscalationService


logger = logging.getLogger(__name__)


class ChatService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.ai = AiClient(settings)
        self.agent_harness = MindBridgeAgentHarness(db, settings)

    async def stream_chat(self, user: UserAccount, request: ChatRequest):
        outcome = self.agent_harness.run(user, request)
        yield sse("meta", ChatStreamEvent(type="meta", sessionId=outcome.session.public_id).model_dump(by_alias=True))
        assistant = []
        risk_level = RiskLevel(outcome.risk_level) if outcome.risk_level else RiskLevel.LOW
        selected_skills = MindBridgeSkillLibrary.response_skills(outcome.intent, risk_level, request.message)
        post_processor = SkillOutputPostProcessor(selected_skills)
        metrics = AiStreamMetrics()
        stream_started_at = time.monotonic()
        first_output_ms = None
        output_chunks = 0
        try:
            async for token in post_processor.process(self.ai.stream(outcome.response_messages, metrics)):
                if first_output_ms is None:
                    first_output_ms = round((time.monotonic() - stream_started_at) * 1000, 3)
                output_chunks += 1
                assistant.append(token)
                yield sse("token", ChatStreamEvent(type="token", sessionId=outcome.session.public_id, content=token).model_dump())
        except Exception as exc:
            if outcome.trace_id is not None:
                try:
                    from app.services.trace import AgentTraceService

                    AgentTraceService(self.db).append_event(
                        outcome.trace_id,
                        RuntimeEventType.LLM_STREAM_FAILED,
                        "ChatService",
                        outcome.session.public_id,
                        stream_failure_payload(exc, metrics, stream_started_at, first_output_ms, output_chunks, assistant),
                    )
                except Exception:
                    logger.exception("Failed to persist LLM failure trace for trace_id=%s", outcome.trace_id)
            logger.warning(
                "LLM stream failed for session=%s provider=%s model=%s error=%s",
                outcome.session.public_id,
                metrics.provider,
                metrics.model,
                type(exc).__name__,
            )
            raise
        output_text = "".join(assistant)
        if outcome.trace_id is not None:
            from app.services.trace import AgentTraceService

            AgentTraceService(self.db).append_event(
                outcome.trace_id,
                RuntimeEventType.LLM_STREAM_COMPLETED,
                "ChatService",
                outcome.session.public_id,
                {
                    "provider": metrics.provider,
                    "model": metrics.model,
                    "modelFirstTokenMs": metrics.first_token_ms,
                    "modelGenerationMs": metrics.generation_ms,
                    "promptTokens": metrics.prompt_tokens,
                    "completionTokens": metrics.completion_tokens,
                    "rawChunks": metrics.raw_chunks,
                    "rawCharacters": metrics.raw_characters,
                    "outputFirstTokenMs": first_output_ms,
                    "outputDurationMs": round((time.monotonic() - stream_started_at) * 1000, 3),
                    "outputChunks": output_chunks,
                    "outputCharacters": len(output_text),
                    "postProcessorIssues": list(post_processor.issues),
                },
            )
        review = ResponseReviewer.review(output_text, selected_skills, risk_level, post_processor.issues)
        review_task = ResponseReviewEscalationService(self.db).create_if_required(
            outcome.report_id,
            outcome.trace_id,
            outcome.session.public_id,
            review,
        )
        if outcome.trace_id is not None:
            from app.services.trace import AgentTraceService

            AgentTraceService(self.db).append_event(
                outcome.trace_id,
                RuntimeEventType.RESPONSE_REVIEWED,
                "ResponseReviewer",
                outcome.session.public_id,
                {
                    **review.to_payload(),
                    "outputCharacters": len(output_text),
                    "reviewTaskId": review_task.id if review_task is not None else None,
                },
            )
        if not review.valid:
            logger.warning(
                "Response review flagged session=%s findings=%s escalation=%s",
                outcome.session.public_id,
                review.finding_codes,
                review.requires_escalation,
            )
        if post_processor.issues:
            logger.warning("Skill output validation issues for session=%s: %s", outcome.session.public_id, post_processor.issues)
        if assistant:
            self.agent_harness.save_assistant_message(user, outcome.session, output_text)
        try:
            await self.agent_harness.dispatch_tools(outcome.tool_plan)
        except Exception as exc:
            logger.warning(
                "Post-response tool dispatch failed for session=%s report_id=%s: %s",
                outcome.session.public_id,
                outcome.report_id,
                exc,
                exc_info=True,
            )
        yield sse("done", ChatStreamEvent(type="done", sessionId=outcome.session.public_id).model_dump())


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def stream_failure_payload(
    exc: Exception,
    metrics: AiStreamMetrics,
    started_at: float,
    first_output_ms: float | None,
    output_chunks: int,
    assistant: list[str],
) -> dict:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    error_type = type(exc).__name__
    retryable = (
        "timeout" in error_type.lower()
        or "connect" in error_type.lower()
        or status_code == 429
        or (isinstance(status_code, int) and status_code >= 500)
    )
    return {
        "provider": metrics.provider,
        "model": metrics.model,
        "errorType": error_type,
        "httpStatus": status_code,
        "retryable": retryable,
        "modelFirstTokenMs": metrics.first_token_ms,
        "failedAfterMs": round((time.monotonic() - started_at) * 1000, 3),
        "rawChunks": metrics.raw_chunks,
        "rawCharacters": metrics.raw_characters,
        "outputFirstTokenMs": first_output_ms,
        "outputChunks": output_chunks,
        "outputCharacters": sum(len(chunk) for chunk in assistant),
        "partialOutput": bool(assistant),
    }
