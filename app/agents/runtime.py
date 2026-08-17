from __future__ import annotations

from dataclasses import dataclass, field
import time

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.enums import IntentType, MessageRole, RiskLevel
from app.models.entities import ChatSession, UserAccount
from app.schemas.dtos import AiMessage
from app.services.ai import AiClient, PromptTemplates, has_consult_signal, has_high_risk_signal
from app.services.assessment import PsychologicalAssessmentService, PsychologyAssessment
from app.services.knowledge import KnowledgeService, SearchResult
from app.services.memory import LayeredMemoryService, compact_history_for_prompt
from app.services.prompt_builder import AgentPromptBuilder
from app.services.context_builder import AgentContextBuilder
from app.services.planning import TaskPlan, TaskPlanner, TaskPlanVerifier
from app.services.agent_retry import AgentRetryPolicy
from app.agents.events import RuntimeEvent, RuntimeEventBus, RuntimeEventType


GENERAL_TASK_WORDS = [
    "java", "python", "javascript", "代码", "编程", "程序", "算法", "数据库", "spring", "maven",
    "前端", "后端", "项目", "接口", "bug", "报错", "作业", "论文", "翻译", "总结", "解释",
    "怎么写", "如何", "是什么", "为什么", "给我", "帮我", "推荐", "查询", "天气", "路线",
]


@dataclass
class AgentStep:
    step: int
    agent: str
    action: str
    observation: str


@dataclass
class AgentContext:
    user: UserAccount
    session: ChatSession
    original_input: str
    model_input: str
    memory_loaded: bool = False
    intent_routed: bool = False
    knowledge_handled: bool = False
    risk_assessed: bool = False
    response_planned: bool = False
    finished: bool = False
    memory_brief: str = "无相关历史记忆。"
    intent: IntentType | None = None
    risk_level: RiskLevel = RiskLevel.LOW
    assessment: PsychologyAssessment | None = None
    knowledge_query: str = ""
    retrieved_knowledge: list[SearchResult] = field(default_factory=list)
    model_history: list[AiMessage] = field(default_factory=list)
    response_messages: list[AiMessage] = field(default_factory=list)
    response_agent: str = ""
    response_plan: str = ""
    context_snapshot: dict = field(default_factory=dict)
    task_plan: TaskPlan | None = field(default=None, repr=False)
    steps: list[AgentStep] = field(default_factory=list)
    events: list[RuntimeEvent] = field(default_factory=list)
    event_bus: RuntimeEventBus | None = field(default=None, repr=False)
    started_monotonic: float = field(default_factory=time.monotonic, repr=False)


@dataclass
class AgentRunResult:
    intent: IntentType
    risk_level: RiskLevel
    assessment: PsychologyAssessment | None
    retrieved_knowledge: list[SearchResult]
    response_messages: list[AiMessage]
    steps: list[AgentStep]
    memory_brief: str
    events: list[RuntimeEvent] = field(default_factory=list)

    @property
    def requires_report(self) -> bool:
        return self.intent != IntentType.CHAT


class AgentRuntimeService:
    max_steps = 8

    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.ai = AiClient(settings)
        self.knowledge = KnowledgeService(db, settings)
        self.memory = LayeredMemoryService(db, settings)
        self.assessment = PsychologicalAssessmentService(self.ai)
        self.prompt_builder = AgentPromptBuilder()
        self.context_builder = AgentContextBuilder(settings, self.prompt_builder)

    def run(self, user: UserAccount, session: ChatSession, original_input: str, model_input: str) -> AgentRunResult:
        context = AgentContext(user=user, session=session, original_input=original_input, model_input=model_input)
        bus = RuntimeEventBus(session.public_id)
        context.event_bus = bus
        try:
            self._register_event_handlers(bus, context)
            bus.publish(RuntimeEventType.USER_MESSAGE_RECEIVED, "AgentRuntime", {"userId": user.id})
            context.events = bus.dispatch(self._event_limit())
        except Exception as exc:
            context.events = list(bus.history)
            setattr(exc, "mindbridge_runtime_context", context)
            raise
        return AgentRunResult(
            intent=context.intent or IntentType.CHAT,
            risk_level=context.risk_level,
            assessment=context.assessment,
            retrieved_knowledge=context.retrieved_knowledge,
            response_messages=context.response_messages,
            steps=context.steps,
            memory_brief=context.memory_brief,
            events=context.events,
        )

    def _register_event_handlers(self, bus: RuntimeEventBus, context: AgentContext) -> None:
        def run_agent(agent, event: RuntimeEvent, completed: RuntimeEventType, producer: str, payload=None) -> None:
            plan_step = self._plan_step_for_agent(producer)
            started_at = time.monotonic()
            self._run_with_plan_retry(
                agent, len(context.steps) + 1, context, producer, plan_step,
                f"{producer} rejected event {event.type.value}",
            )
            event_payload = payload() if payload else {}
            if context.task_plan is not None and plan_step is not None:
                context.task_plan.complete(plan_step)
                if context.context_snapshot:
                    self._refresh_plan_snapshot(context)
            event_payload["durationMs"] = round((time.monotonic() - started_at) * 1000, 3)
            bus.publish(completed, producer, event_payload)

        bus.subscribe(
            RuntimeEventType.USER_MESSAGE_RECEIVED,
            lambda event: run_agent(self.memory_agent, event, RuntimeEventType.MEMORY_LOADED, "MemoryAgent"),
        )
        bus.subscribe(
            RuntimeEventType.MEMORY_LOADED,
            lambda event: run_agent(
                self.supervisor_agent,
                event,
                RuntimeEventType.INTENT_ROUTED,
                "SupervisorAgent",
                lambda: {"intent": (context.intent or IntentType.CHAT).value},
            ),
        )

        def route_intent(event: RuntimeEvent) -> None:
            if context.intent == IntentType.CHAT:
                run_agent(
                    self.companion_agent,
                    event,
                    RuntimeEventType.RESPONSE_PLANNED,
                    "CompanionAgent",
                    lambda: context.context_snapshot,
                )
            else:
                run_agent(
                    self.knowledge_agent,
                    event,
                    RuntimeEventType.KNOWLEDGE_RETRIEVED,
                    "KnowledgeAgent",
                    lambda: {"retrieved": len(context.retrieved_knowledge)},
                )

        bus.subscribe(RuntimeEventType.INTENT_ROUTED, route_intent)
        bus.subscribe(
            RuntimeEventType.KNOWLEDGE_RETRIEVED,
            lambda event: run_agent(
                self.risk_guardian_agent,
                event,
                RuntimeEventType.RISK_ASSESSED,
                "RiskGuardianAgent",
                lambda: {"risk": context.risk_level.value},
            ),
        )
        bus.subscribe(
            RuntimeEventType.RISK_ASSESSED,
            lambda event: run_agent(
                self.counselor_agent,
                event,
                RuntimeEventType.RESPONSE_PLANNED,
                "CounselorAgent",
                lambda: context.context_snapshot,
            ),
        )

        def complete_run(_: RuntimeEvent) -> None:
            context.finished = True
            bus.publish(
                RuntimeEventType.RUN_COMPLETED,
                "CoordinatorAgent",
                {
                    "steps": len(context.steps),
                    "totalDurationMs": round((time.monotonic() - context.started_monotonic) * 1000, 3),
                },
            )

        bus.subscribe(RuntimeEventType.RESPONSE_PLANNED, complete_run)

    def _run_with_plan_retry(
        self, agent, step_number: int, context: AgentContext, producer: str,
        plan_step: str | None, rejected_message: str,
    ) -> None:
        max_attempts = max(1, int(self.settings.agent_plan_max_attempts))
        delay = max(0.0, float(self.settings.agent_plan_retry_delay_seconds))
        while True:
            if context.task_plan is not None and plan_step is not None:
                context.task_plan.start(plan_step)
            try:
                if not agent(step_number, context):
                    rejection = RuntimeError(rejected_message)
                    if context.task_plan is not None and plan_step is not None:
                        context.task_plan.fail(plan_step, rejection)
                    raise rejection
                return
            except Exception as exc:
                if context.task_plan is None or plan_step is None:
                    raise
                step = context.task_plan._step(plan_step)
                if step.status == "RUNNING":
                    context.task_plan.fail(plan_step, exc)
                retry_decision = AgentRetryPolicy.classify(exc)
                if step.attempts >= max_attempts or not retry_decision.retryable:
                    self._emit_plan_lifecycle(
                        context, RuntimeEventType.PLAN_STEP_FAILED, producer, plan_step, step, exc, 0.0,
                        retry_decision.retryable, retry_decision.reason_code,
                    )
                    if context.context_snapshot:
                        self._refresh_plan_snapshot(context)
                    raise
                context.task_plan.retry(plan_step)
                retry_delay = delay * (2 ** (step.attempts - 1))
                self._emit_plan_lifecycle(
                    context, RuntimeEventType.PLAN_STEP_RETRYING, producer, plan_step, step, exc, retry_delay,
                    retry_decision.retryable, retry_decision.reason_code,
                )
                if delay:
                    time.sleep(retry_delay)

    def _emit_plan_lifecycle(
        self, context: AgentContext, event_type: RuntimeEventType, producer: str,
        plan_step: str, step, exc: Exception, retry_delay: float,
        retryable: bool, reason_code: str,
    ) -> None:
        if context.event_bus is None:
            return
        context.event_bus.publish(
            event_type,
            producer,
            {
                "stepId": plan_step,
                "attempt": step.attempts,
                "maxAttempts": max(1, int(self.settings.agent_plan_max_attempts)),
                "errorType": type(exc).__name__,
                "error": step.error,
                "retryable": retryable,
                "reasonCode": reason_code,
                "retryDelayMs": round(retry_delay * 1000, 3),
            },
        )
        context.event_bus.dispatch(self._event_limit())

    def _event_limit(self) -> int:
        return self.max_steps * max(1, int(self.settings.agent_plan_max_attempts)) + 8

    def memory_agent(self, step: int, context: AgentContext) -> bool:
        if context.memory_loaded:
            return False
        history, durable_summary, source = self.memory.load(
            context.user.id, context.session.id, context.session.public_id
        )
        compacted_history, deterministic_brief = compact_history_for_prompt(history, self.settings, context.model_input)
        context.model_history = self._bounded_model_history(
            [*compacted_history, AiMessage(role="user", content=context.model_input)]
        )
        summary_fallback = self._merge_memory_briefs(durable_summary, deterministic_brief)
        context.memory_brief = self._summarize_memory(history, context.model_input, summary_fallback)
        self.memory.persist_summary(
            context.user.id,
            context.session.id,
            context.session.public_id,
            context.memory_brief,
            len(history),
        )
        context.memory_loaded = True
        context.steps.append(AgentStep(step, "MemoryAgent", "READ_MEMORY", f"loaded {len(history)} messages from {source}"))
        return True

    def supervisor_agent(self, step: int, context: AgentContext) -> bool:
        if not context.memory_loaded or context.intent_routed:
            return False
        context.intent = self._classify(context.model_input, context.model_history)
        context.task_plan = TaskPlanner.create(context.intent)
        context.intent_routed = True
        if context.intent == IntentType.CHAT:
            context.knowledge_handled = True
            context.risk_assessed = True
        context.steps.append(AgentStep(step, "SupervisorAgent", "ROUTE_INTENT", f"intent={context.intent.value}"))
        return True

    def knowledge_agent(self, step: int, context: AgentContext) -> bool:
        if not context.intent_routed or context.knowledge_handled or context.intent == IntentType.CHAT:
            return False
        query = self._rewrite_query(context)
        retrieved = self.knowledge.retrieve(query, self.settings.knowledge_top_k)
        context.knowledge_query = query
        context.retrieved_knowledge = retrieved
        context.knowledge_handled = True
        context.steps.append(AgentStep(step, "KnowledgeAgent", "RETRIEVE_KNOWLEDGE", f"query={query}; retrieved={len(retrieved)}"))
        return True

    def risk_guardian_agent(self, step: int, context: AgentContext) -> bool:
        if not context.knowledge_handled or context.risk_assessed or context.intent == IntentType.CHAT:
            return False
        assessment = self.assessment.assess(context.model_input, context.model_history)
        if context.intent == IntentType.RISK and assessment.risk != RiskLevel.HIGH:
            assessment.risk = RiskLevel.HIGH
            assessment.emotion_score = max(assessment.emotion_score, 4.0)
        context.assessment = assessment
        context.risk_level = assessment.risk
        context.risk_assessed = True
        context.steps.append(AgentStep(step, "RiskGuardianAgent", "ASSESS_RISK", f"risk={assessment.risk.value}, emotion={assessment.emotion.value}"))
        return True

    def companion_agent(self, step: int, context: AgentContext) -> bool:
        if not context.intent_routed or context.intent != IntentType.CHAT or context.response_planned:
            return False
        context.risk_level = RiskLevel.LOW
        context.response_agent = "CompanionAgent"
        context.response_plan = "围绕用户当前问题直接、自然地回答。"
        built_context = self.context_builder.build_response(
            intent=IntentType.CHAT,
            risk=RiskLevel.LOW,
            display_name=context.user.display_name,
            user_input=context.model_input,
            recent_history=context.model_history,
            memory_brief=context.memory_brief,
            retrieved_knowledge=[],
            response_agent=context.response_agent,
            response_plan=context.response_plan,
            planner_state=context.task_plan.to_prompt() if context.task_plan else "",
        )
        context.response_messages = built_context.messages
        self._capture_context_snapshot(context, built_context)
        context.response_planned = True
        context.finished = True
        context.steps.append(AgentStep(step, "CompanionAgent", "PLAN_RESPONSE", "normal companion response planned"))
        return True

    def counselor_agent(self, step: int, context: AgentContext) -> bool:
        if not context.risk_assessed or context.intent == IntentType.CHAT or context.response_planned:
            return False
        context.response_agent = "CounselorAgent"
        context.response_plan = "先共情，再给出具体支持步骤；高风险时优先安全。"
        built_context = self.context_builder.build_response(
            intent=context.intent or IntentType.CONSULT,
            risk=context.risk_level,
            display_name=context.user.display_name,
            user_input=context.model_input,
            skill_input=context.original_input,
            recent_history=context.model_history,
            memory_brief=context.memory_brief,
            retrieved_knowledge=context.retrieved_knowledge,
            knowledge_query=context.knowledge_query,
            response_agent=context.response_agent,
            response_plan=context.response_plan,
            planner_state=context.task_plan.to_prompt() if context.task_plan else "",
        )
        context.response_messages = built_context.messages
        self._capture_context_snapshot(context, built_context)
        context.response_planned = True
        context.finished = True
        selected_versions = ",".join(f"{skill.name}@{skill.version}" for skill in built_context.selected_skills)
        budget = built_context.budget
        context.steps.append(AgentStep(
            step,
            "CounselorAgent",
            "PLAN_RESPONSE",
            f"support response planned with risk={context.risk_level.value}; skills={selected_versions}; "
            f"context={budget.used}/{budget.limit}; truncated={','.join(budget.truncated_sections) or 'none'}",
        ))
        return True

    @staticmethod
    def _capture_context_snapshot(context: AgentContext, built_context) -> None:
        budget = built_context.budget
        context.context_snapshot = {
            "budget": {
                "limit": budget.limit,
                "used": budget.used,
                "remaining": max(0, budget.limit - budget.used),
                "sections": {
                    "history": budget.history_chars,
                    "memory": budget.memory_chars,
                    "knowledge": budget.knowledge_chars,
                    "skill": budget.skill_chars,
                },
                "truncated": list(budget.truncated_sections),
            },
            "skills": [
                {"name": skill.name, "version": skill.version}
                for skill in built_context.selected_skills
            ],
            "knowledge": {
                "count": len(context.retrieved_knowledge),
                "sources": list(dict.fromkeys(item.source for item in context.retrieved_knowledge)),
            },
            "hasToolContext": False,
            "hasPlannerState": context.task_plan is not None,
            "taskPlan": context.task_plan.to_dict() if context.task_plan else None,
            "planVerification": TaskPlanVerifier.verify(context.task_plan) if context.task_plan else None,
            "inputSanitized": context.model_input != context.original_input,
        }

    @staticmethod
    def _plan_step_for_agent(producer: str) -> str | None:
        return {
            "KnowledgeAgent": "retrieval",
            "RiskGuardianAgent": "risk",
            "CompanionAgent": "response",
            "CounselorAgent": "response",
        }.get(producer)

    @staticmethod
    def _refresh_plan_snapshot(context: AgentContext) -> None:
        if context.task_plan is None:
            return
        context.context_snapshot["taskPlan"] = context.task_plan.to_dict()
        context.context_snapshot["planVerification"] = TaskPlanVerifier.verify(context.task_plan)
        context.context_snapshot["hasPlannerState"] = True

    def _classify(self, text: str, history: list[AiMessage]) -> IntentType:
        lowered = text.lower()
        if has_high_risk_signal(lowered):
            return IntentType.RISK
        if not has_consult_signal(lowered) and any(word in lowered for word in GENERAL_TASK_WORDS):
            return IntentType.CHAT
        try:
            label = self.ai.complete(PromptTemplates.intent_prompt(history, text)).upper()
            if "RISK" in label:
                return IntentType.RISK
            if "CONSULT" in label:
                return IntentType.CONSULT
            if "CHAT" in label:
                return IntentType.CHAT
        except Exception:
            pass
        return IntentType.CONSULT if has_consult_signal(lowered) else IntentType.CHAT

    def _rewrite_query(self, context: AgentContext) -> str:
        try:
            query = self.ai.complete([
                AiMessage(role="system", content="你是 MindBridge 的 KnowledgeAgent。把学生输入改写成适合检索校园心理知识库的中文查询词，只输出查询词。"),
                AiMessage(role="user", content=f"记忆摘要：\n{context.memory_brief}\n\n当前输入：\n{context.model_input}"),
            ]).strip()
            return (query or context.model_input)[:60]
        except Exception:
            return context.model_input

    def _bounded_model_history(self, history: list[AiMessage]) -> list[AiMessage]:
        limit = max(2, self.settings.chat_history_limit * 2)
        if len(history) <= limit:
            return history
        if history[0].role == "system":
            return [history[0], *history[-(limit - 1):]]
        return history[-limit:]

    def _summarize_memory(self, history: list[AiMessage], current_input: str, fallback: str) -> str:
        max_chars = max(120, self.settings.memory_summary_max_chars)
        if not history:
            return "无相关历史记忆。"
        try:
            summary = self.ai.complete([
                AiMessage(role="system", content="你是 MindBridge 的 MemoryAgent。只输出 1-3 条中文记忆要点，不输出风险等级或诊断。"),
                AiMessage(role="user", content=f"当前输入：\n{current_input}\n\n最近历史：\n{history[-12:]}"),
            ]).strip()
            return summary[:max_chars] or fallback
        except Exception:
            return fallback or "无相关历史记忆。"

    def _merge_memory_briefs(self, durable: str, recent: str) -> str:
        parts = [part.strip() for part in (durable, recent) if part and part.strip()]
        if not parts:
            return "无相关历史记忆。"
        merged = "\n".join(dict.fromkeys(parts))
        return merged[: max(120, self.settings.memory_summary_max_chars)]
