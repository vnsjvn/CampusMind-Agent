from __future__ import annotations

from dataclasses import dataclass, field

from app.core.enums import IntentType, RiskLevel
from app.schemas.dtos import AiMessage
from app.services.ai import PromptTemplates


@dataclass(frozen=True)
class PromptBuildContext:
    intent: IntentType
    risk: RiskLevel
    display_name: str
    user_input: str
    recent_history: list[AiMessage] = field(default_factory=list)
    memory_brief: str = ""
    knowledge_context: str = ""
    knowledge_query: str = ""
    skill_context: str = ""
    tool_context: str = ""
    planner_state: str = ""
    response_agent: str = ""
    response_plan: str = ""


class AgentPromptBuilder:
    """Builds prompts in one auditable order with safety instructions first."""

    def build(self, context: PromptBuildContext) -> list[AiMessage]:
        messages = [
            PromptTemplates.answer_system_prompt(
                context.intent,
                context.risk,
                context.knowledge_context,
                context.display_name,
                context.skill_context,
            )
        ]
        runtime_sections = [
            ("执行 Agent", context.response_agent),
            ("长期与摘要记忆", context.memory_brief),
            ("知识检索 Query", context.knowledge_query),
            ("工具结果", context.tool_context),
            ("Planner 状态", context.planner_state),
            ("回复策略", context.response_plan),
        ]
        runtime_context = "\n\n".join(f"{title}：\n{value}" for title, value in runtime_sections if value)
        if runtime_context:
            messages.append(AiMessage(role="system", content=runtime_context))
        messages.extend(self._without_duplicate_current_input(context.recent_history, context.user_input))
        if not messages or messages[-1].role != "user" or messages[-1].content != context.user_input:
            messages.append(AiMessage(role="user", content=context.user_input))
        return messages

    def _without_duplicate_current_input(self, history: list[AiMessage], user_input: str) -> list[AiMessage]:
        if history and history[-1].role == "user" and history[-1].content == user_input:
            return list(history[:-1])
        return list(history)
