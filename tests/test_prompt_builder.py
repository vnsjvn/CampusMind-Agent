import unittest

from app.core.enums import IntentType, RiskLevel
from app.schemas.dtos import AiMessage
from app.services.prompt_builder import AgentPromptBuilder, PromptBuildContext


class PromptBuilderTests(unittest.TestCase):
    def test_prompt_has_safety_first_and_current_input_last_once(self):
        prompt = AgentPromptBuilder().build(PromptBuildContext(
            intent=IntentType.RISK,
            risk=RiskLevel.HIGH,
            display_name="同学",
            user_input="当前输入",
            recent_history=[AiMessage(role="assistant", content="上一轮"), AiMessage(role="user", content="当前输入")],
            memory_brief="长期摘要",
            knowledge_context="安全知识",
            skill_context="high_risk_safety_plan",
            response_agent="CounselorAgent",
            response_plan="优先确认安全",
        ))

        self.assertEqual(prompt[0].role, "system")
        self.assertIn("high_risk_safety_plan", prompt[0].content)
        self.assertEqual(prompt[-1], AiMessage(role="user", content="当前输入"))
        self.assertEqual(sum(item.content == "当前输入" for item in prompt), 1)
        self.assertIn("长期摘要", prompt[1].content)


if __name__ == "__main__":
    unittest.main()
