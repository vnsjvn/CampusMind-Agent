import unittest
from types import SimpleNamespace

from app.core.config import Settings
from app.core.enums import IntentType, RiskLevel
from app.schemas.dtos import AiMessage
from app.services.context_builder import AgentContextBuilder


class AgentContextBuilderTests(unittest.TestCase):
    def test_builder_preserves_current_input_and_applies_section_budgets(self):
        settings = Settings(
            _env_file=None,
            context_max_chars=1000,
            context_history_max_chars=100,
            context_memory_max_chars=40,
            context_knowledge_max_chars=80,
            context_skill_max_chars=400,
        )
        builder = AgentContextBuilder(settings)

        result = builder.build_response(
            intent=IntentType.CONSULT,
            risk=RiskLevel.MEDIUM,
            display_name="同学",
            user_input="我最近焦虑得睡不着",
            recent_history=[AiMessage(role="assistant", content="历史" * 100)],
            memory_brief="记忆" * 100,
            retrieved_knowledge=[SimpleNamespace(source="knowledge.md", content="知识" * 100)],
            response_agent="CounselorAgent",
            response_plan="先共情",
        )

        self.assertEqual(result.messages[-1].content, "我最近焦虑得睡不着")
        self.assertLessEqual(result.budget.history_chars, 100)
        self.assertLessEqual(result.budget.memory_chars, 40)
        self.assertLessEqual(result.budget.knowledge_chars, 80)
        self.assertIn("memory", result.budget.truncated_sections)
        self.assertIn("knowledge", result.budget.truncated_sections)
        self.assertIn("supportive_response_baseline", [skill.name for skill in result.selected_skills])

    def test_skill_selection_can_use_original_text_without_leaking_it_to_prompt(self):
        builder = AgentContextBuilder(Settings(_env_file=None))

        result = builder.build_response(
            intent=IntentType.CONSULT,
            risk=RiskLevel.LOW,
            display_name="同学",
            user_input="我的手机号是[PHONE]，最近很难受",
            skill_input="我的手机号是13800138000，最近很焦虑",
            recent_history=[],
            memory_brief="",
            retrieved_knowledge=[],
        )

        self.assertIn("anxiety_grounding_support", [skill.name for skill in result.selected_skills])
        prompt_text = "\n".join(message.content for message in result.messages)
        self.assertNotIn("13800138000", prompt_text)
        self.assertIn("[PHONE]", prompt_text)


if __name__ == "__main__":
    unittest.main()
