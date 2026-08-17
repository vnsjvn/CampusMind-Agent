from __future__ import annotations

from typing import TypedDict
import time

from sqlalchemy.orm import Session

from app.agents.runtime import AgentContext, AgentRunResult, AgentRuntimeService
from app.agents.events import RuntimeEventBus, RuntimeEventType
from app.core.config import Settings
from app.core.enums import IntentType
from app.models.entities import ChatSession, UserAccount


class GraphState(TypedDict):
    context: AgentContext


class LangGraphAgentRuntimeService(AgentRuntimeService):
    """LangGraph implementation of the bounded MindBridge agent loop."""

    framework_name = "langgraph"

    def __init__(self, db: Session, settings: Settings):
        super().__init__(db, settings)
        self.graph = self._build_graph()

    def run(self, user: UserAccount, session: ChatSession, original_input: str, model_input: str) -> AgentRunResult:
        context = AgentContext(user=user, session=session, original_input=original_input, model_input=model_input)
        context.event_bus = RuntimeEventBus(session.public_id)
        self._emit(context, RuntimeEventType.USER_MESSAGE_RECEIVED, "LangGraphRuntime", {"userId": user.id})
        graph_limit = self.max_steps * 3 + 2
        try:
            state = self.graph.invoke({"context": context}, {"recursion_limit": graph_limit})
        except Exception as exc:
            context.events = list(context.event_bus.history) if context.event_bus else []
            setattr(exc, "mindbridge_runtime_context", context)
            raise
        result_context = state["context"]
        self._emit(
            result_context,
            RuntimeEventType.RUN_COMPLETED,
            "CoordinatorAgent",
            {
                "steps": len(result_context.steps),
                "totalDurationMs": round((time.monotonic() - result_context.started_monotonic) * 1000, 3),
            },
        )
        result_context.events = list(result_context.event_bus.history) if result_context.event_bus else []
        return AgentRunResult(
            intent=result_context.intent or IntentType.CHAT,
            risk_level=result_context.risk_level,
            assessment=result_context.assessment,
            retrieved_knowledge=result_context.retrieved_knowledge,
            response_messages=result_context.response_messages,
            steps=result_context.steps,
            memory_brief=result_context.memory_brief,
            events=result_context.events,
        )

    def _build_graph(self):
        from langgraph.graph import END, StateGraph

        graph = StateGraph(GraphState)
        graph.add_node("controller", self._controller_node)
        graph.add_node("memory", self._memory_node)
        graph.add_node("supervisor", self._supervisor_node)
        graph.add_node("knowledge", self._knowledge_node)
        graph.add_node("risk_guardian", self._risk_guardian_node)
        graph.add_node("companion", self._companion_node)
        graph.add_node("counselor", self._counselor_node)

        # 设置图的入口节点为 controller
        # 每次执行图时，都会先进入 controller
        graph.set_entry_point("controller")
        # 给 controller 添加条件边
        # 每次到 controller，都会调用 _select_next_agent 判断下一步去哪
        graph.add_conditional_edges(
            "controller",
            # 路由函数，根据当前 context 状态返回下一个节点名称
            self._select_next_agent,
            # 路由函数返回值和实际节点之间的映射关系
            {
                "memory": "memory",
                "supervisor": "supervisor",
                "knowledge": "knowledge",
                "risk_guardian": "risk_guardian",
                "companion": "companion",
                "counselor": "counselor",
                "end": END,
            },
        )
        graph.add_edge("memory", "controller")
        graph.add_edge("supervisor", "controller")
        graph.add_edge("knowledge", "controller")
        graph.add_edge("risk_guardian", "controller")
        graph.add_edge("companion", "controller")
        graph.add_edge("counselor", "controller")
        return graph.compile()

    def _controller_node(self, state: GraphState) -> GraphState:
        return state

    def _memory_node(self, state: GraphState) -> GraphState:
        self._run_agent(state, self.memory_agent, RuntimeEventType.MEMORY_LOADED, "MemoryAgent")
        return state

    def _supervisor_node(self, state: GraphState) -> GraphState:
        self._run_agent(state, self.supervisor_agent, RuntimeEventType.INTENT_ROUTED, "SupervisorAgent")
        return state

    def _knowledge_node(self, state: GraphState) -> GraphState:
        self._run_agent(state, self.knowledge_agent, RuntimeEventType.KNOWLEDGE_RETRIEVED, "KnowledgeAgent")
        return state

    def _risk_guardian_node(self, state: GraphState) -> GraphState:
        self._run_agent(state, self.risk_guardian_agent, RuntimeEventType.RISK_ASSESSED, "RiskGuardianAgent")
        return state

    def _companion_node(self, state: GraphState) -> GraphState:
        self._run_agent(state, self.companion_agent, RuntimeEventType.RESPONSE_PLANNED, "CompanionAgent")
        return state

    def _counselor_node(self, state: GraphState) -> GraphState:
        self._run_agent(state, self.counselor_agent, RuntimeEventType.RESPONSE_PLANNED, "CounselorAgent")
        return state

    def _run_agent(self, state: GraphState, agent, completed_event: RuntimeEventType, producer: str) -> None:
        context = state["context"]
        if context.finished or len(context.steps) >= self.max_steps:
            context.finished = True
            return
        before = len(context.steps)
        plan_step = self._plan_step_for_agent(producer)
        started_at = time.monotonic()
        self._run_with_plan_retry(
            agent, before + 1, context, producer, plan_step,
            f"{producer} did not execute its assigned plan step",
        )
        payload = {}
        if completed_event == RuntimeEventType.INTENT_ROUTED:
            payload["intent"] = (context.intent or IntentType.CHAT).value
        elif completed_event == RuntimeEventType.KNOWLEDGE_RETRIEVED:
            payload["retrieved"] = len(context.retrieved_knowledge)
        elif completed_event == RuntimeEventType.RISK_ASSESSED:
            payload["risk"] = context.risk_level.value
        elif completed_event == RuntimeEventType.RESPONSE_PLANNED:
            payload.update(context.context_snapshot)
        payload["durationMs"] = round((time.monotonic() - started_at) * 1000, 3)
        if context.task_plan is not None and plan_step is not None:
            context.task_plan.complete(plan_step)
            if context.context_snapshot:
                self._refresh_plan_snapshot(context)
                if completed_event == RuntimeEventType.RESPONSE_PLANNED:
                    payload.update(context.context_snapshot)
        self._emit(context, completed_event, producer, payload)

    def _emit(self, context: AgentContext, event_type: RuntimeEventType, producer: str, payload: dict | None = None) -> None:
        if context.event_bus is None:
            return
        context.event_bus.publish(event_type, producer, payload)
        context.event_bus.dispatch(self._event_limit())

    # controller 的核心路由逻辑，根据 context 当前状态决定下一个执行哪个 agent
    def _select_next_agent(self, state: GraphState) -> str:
        context = state["context"]
        if context.finished or len(context.steps) >= self.max_steps:
            return "end"
        if not context.memory_loaded:
            return "memory"
        if not context.intent_routed:
            return "supervisor"
        if context.intent == IntentType.CHAT:
            return "companion" if not context.response_planned else "end"
        if not context.knowledge_handled:
            return "knowledge"
        if not context.risk_assessed:
            return "risk_guardian"
        if not context.response_planned:
            return "counselor"
        return "end"
