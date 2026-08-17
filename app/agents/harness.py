from __future__ import annotations

import uuid
from dataclasses import dataclass
import logging

from sqlalchemy.orm import Session

from app.agents.factory import create_agent_runtime
from app.agents.runtime import AgentStep
from app.core.config import Settings
from app.core.enums import IntentType, MessageRole
from app.models.entities import ChatMessage, ChatSession, PsychologicalReport, UserAccount
from app.schemas.dtos import AiMessage, ChatRequest
from app.services.assessment import PsychologyAssessment
from app.services.knowledge import SearchResult
from app.services.mcp_client import MindBridgeMcpToolClient
from app.services.memory import LayeredMemoryService
from app.services.privacy import PrivacySanitizer
from app.services.tool_queue import ToolQueueService
from app.services.trace import AgentTraceService
from app.services.skills import MindBridgeSkillLibrary


logger = logging.getLogger(__name__)


@dataclass
class AgentToolPlan:
    report_id: int | None
    risk_level: str | None
    allowed_tools: tuple[str, ...] = ()

    @property
    def requires_tools(self) -> bool:
        return self.report_id is not None


@dataclass
class AgentHarnessOutcome:
    session: ChatSession
    original_input: str
    model_input: str
    intent: IntentType
    risk_level: str | None
    assessment: PsychologyAssessment | None
    response_messages: list[AiMessage]
    agent_steps: list[AgentStep]
    retrieved_knowledge: list[SearchResult]
    report_id: int | None
    tool_plan: AgentToolPlan
    trace_id: int | None


class MindBridgeAgentHarness:
    """Runtime harness for one MindBridge agent turn.

    The harness owns business orchestration around the agent runtime. HTTP/SSE
    code can stay thin while this class manages input preparation, persistence,
    risk report creation, tool planning, and trace data.
    """

    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.privacy = PrivacySanitizer()
        self.memory = LayeredMemoryService(db, settings)

    def run(self, user: UserAccount, request: ChatRequest) -> AgentHarnessOutcome:
        original_input = request.message.strip()
        model_input = self.privacy.sanitize(original_input)
        session = self._resolve_session(user, request.sessionId, original_input)
        try:
            agent_run = create_agent_runtime(self.db, self.settings).run(user, session, original_input, model_input)
        except Exception as exc:
            context = getattr(exc, "mindbridge_runtime_context", None)
            self.db.rollback()
            try:
                AgentTraceService(self.db).save_failed_run(
                    user, session, original_input, model_input, exc, context
                )
            except Exception:
                self.db.rollback()
                logger.exception("Failed to persist Agent Runtime failure trace for session=%s", session.public_id)
            raise
        self.save_message(user, session, MessageRole.USER, original_input)

        report = self._create_report(user, session, original_input, agent_run)
        risk_level = report.risk_level if report is not None else None
        trace = AgentTraceService(self.db).save_run(
            user=user,
            session=session,
            original_input=original_input,
            sanitized_input=model_input,
            memory_brief=agent_run.memory_brief,
            agent_run=agent_run,
            report_id=report.id if report is not None else None,
        )
        allowed_tools = (
            MindBridgeSkillLibrary.allowed_tools_for_response(agent_run.intent, agent_run.risk_level, original_input)
            if report is not None
            else ()
        )
        tool_plan = AgentToolPlan(
            report_id=report.id if report is not None else None,
            risk_level=risk_level,
            allowed_tools=allowed_tools,
        )
        return AgentHarnessOutcome(
            session=session,
            original_input=original_input,
            model_input=model_input,
            intent=agent_run.intent,
            risk_level=risk_level,
            assessment=agent_run.assessment,
            response_messages=agent_run.response_messages,
            agent_steps=agent_run.steps,
            retrieved_knowledge=agent_run.retrieved_knowledge,
            report_id=report.id if report is not None else None,
            tool_plan=tool_plan,
            trace_id=trace.id,
        )

    def save_assistant_message(self, user: UserAccount, session: ChatSession, content: str) -> None:
        self.save_message(user, session, MessageRole.ASSISTANT, content)

    async def dispatch_tools(self, tool_plan: AgentToolPlan) -> list[str]:
        if tool_plan.report_id is None:
            return []
        if self.settings.tool_queue_enabled:
            ToolQueueService(self.db, self.settings).enqueue_report(tool_plan.report_id, tool_plan.risk_level)
            return ["queued"]
        return await MindBridgeMcpToolClient(self.settings).handle_report(
            tool_plan.report_id,
            tool_plan.risk_level,
            tool_plan.allowed_tools,
        )

    def save_message(self, user: UserAccount, session: ChatSession, role: MessageRole, content: str) -> None:
        self.db.add(ChatMessage(user_id=user.id, session_id=session.id, role=role.value, content=content))
        session.touch()
        self.db.add(session)
        self.db.commit()
        self.memory.append(session.public_id, role.value, content)

    def _resolve_session(self, user: UserAccount, public_id: str | None, text: str) -> ChatSession:
        if public_id:
            session = self.db.query(ChatSession).filter(ChatSession.public_id == public_id, ChatSession.user_id == user.id).first()
            if session is None:
                raise ValueError("Session not found")
            return session
        session = ChatSession(public_id=uuid.uuid4().hex, user_id=user.id, title=text[:36])
        self.db.add(session)
        self.db.commit()
        self.db.refresh(session)
        return session

    def _create_report(self, user: UserAccount, session: ChatSession, text: str, agent_run) -> PsychologicalReport | None:
        if not agent_run.requires_report or agent_run.assessment is None:
            return None
        report = PsychologicalReport(
            user_id=user.id,
            session_id=session.id,
            content=text,
            intent=agent_run.intent.value,
            emotion=agent_run.assessment.emotion.value,
            emotion_score=agent_run.assessment.emotion_score,
            risk_level=agent_run.assessment.risk.value,
            confidence=agent_run.assessment.confidence,
            summary=agent_run.assessment.summary,
        )
        self.db.add(report)
        self.db.commit()
        self.db.refresh(report)
        return report
