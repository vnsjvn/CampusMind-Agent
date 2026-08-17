from __future__ import annotations

from dataclasses import dataclass, field

from app.core.config import Settings
from app.core.enums import IntentType, RiskLevel
from app.schemas.dtos import AiMessage
from app.services.prompt_builder import AgentPromptBuilder, PromptBuildContext
from app.services.skills import MindBridgeSkill, MindBridgeSkillLibrary


@dataclass(frozen=True)
class ContextBudgetUsage:
    limit: int
    used: int
    history_chars: int
    memory_chars: int
    knowledge_chars: int
    skill_chars: int
    truncated_sections: tuple[str, ...] = ()


@dataclass(frozen=True)
class BuiltAgentContext:
    messages: list[AiMessage]
    selected_skills: list[MindBridgeSkill] = field(default_factory=list)
    budget: ContextBudgetUsage | None = None


class AgentContextBuilder:
    """Assemble bounded response context from memory, retrieval, Skills and runtime state."""

    def __init__(self, settings: Settings, prompt_builder: AgentPromptBuilder | None = None):
        self.settings = settings
        self.prompt_builder = prompt_builder or AgentPromptBuilder()

    def build_response(
        self,
        *,
        intent: IntentType,
        risk: RiskLevel,
        display_name: str,
        user_input: str,
        skill_input: str | None = None,
        recent_history: list[AiMessage],
        memory_brief: str,
        retrieved_knowledge: list,
        knowledge_query: str = "",
        tool_context: str = "",
        planner_state: str = "",
        response_agent: str = "",
        response_plan: str = "",
    ) -> BuiltAgentContext:
        selected_skills = MindBridgeSkillLibrary.response_skills(intent, risk, skill_input or user_input)
        raw_skill = "\n\n".join(skill.prompt_context() for skill in selected_skills)
        raw_knowledge = "\n\n".join(
            f"- [{item.source}] {item.content}" for item in retrieved_knowledge
        )
        total_limit = max(1000, self.settings.context_max_chars)
        remaining = max(0, total_limit - len(user_input))
        truncated = []

        skill_context, remaining = self._take(
            raw_skill,
            min(self.settings.context_skill_max_chars, remaining),
            remaining,
            "skill",
            truncated,
        )
        knowledge_context, remaining = self._take(
            raw_knowledge,
            min(self.settings.context_knowledge_max_chars, remaining),
            remaining,
            "knowledge",
            truncated,
        )
        bounded_memory, remaining = self._take(
            memory_brief,
            min(self.settings.context_memory_max_chars, remaining),
            remaining,
            "memory",
            truncated,
        )
        bounded_history = self._fit_history(
            recent_history,
            min(self.settings.context_history_max_chars, remaining),
        )
        history_chars = sum(len(item.content) for item in bounded_history)
        if history_chars < sum(len(item.content) for item in recent_history):
            truncated.append("history")

        prompt_context = PromptBuildContext(
            intent=intent,
            risk=risk,
            display_name=display_name,
            user_input=user_input,
            recent_history=bounded_history,
            memory_brief=bounded_memory,
            knowledge_context=knowledge_context,
            knowledge_query=knowledge_query,
            skill_context=skill_context,
            tool_context=tool_context,
            planner_state=planner_state,
            response_agent=response_agent,
            response_plan=response_plan,
        )
        used = len(user_input) + len(skill_context) + len(knowledge_context) + len(bounded_memory) + history_chars
        return BuiltAgentContext(
            messages=self.prompt_builder.build(prompt_context),
            selected_skills=selected_skills,
            budget=ContextBudgetUsage(
                limit=total_limit,
                used=used,
                history_chars=history_chars,
                memory_chars=len(bounded_memory),
                knowledge_chars=len(knowledge_context),
                skill_chars=len(skill_context),
                truncated_sections=tuple(dict.fromkeys(truncated)),
            ),
        )

    @staticmethod
    def _take(text: str, section_limit: int, remaining: int, name: str, truncated: list[str]) -> tuple[str, int]:
        limit = max(0, min(section_limit, remaining))
        if len(text) > limit:
            truncated.append(name)
        value = text[:limit]
        return value, max(0, remaining - len(value))

    @staticmethod
    def _fit_history(history: list[AiMessage], limit: int) -> list[AiMessage]:
        if limit <= 0:
            return []
        selected = []
        remaining = limit
        for message in reversed(history):
            if remaining <= 0:
                break
            content = message.content[-remaining:]
            selected.append(AiMessage(role=message.role, content=content))
            remaining -= len(content)
        return list(reversed(selected))
