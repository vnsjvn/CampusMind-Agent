import unittest

from app.services.skill_output import SkillOutputPostProcessor
from app.services.skills import MindBridgeSkillLibrary


async def chunks(*values):
    for value in values:
        yield value


class SkillOutputPostProcessorTests(unittest.IsolatedAsyncioTestCase):
    async def test_filters_cross_chunk_forbidden_text_and_limits_questions(self):
        skill = MindBridgeSkillLibrary.registry().get_required("supportive_response_baseline")
        processor = SkillOutputPostProcessor([skill])

        output = "".join([part async for part in processor.process(chunks("你的风险等", "级是HIGH。你好吗？为什么？"))])

        self.assertNotIn("你的风险等级是", output)
        self.assertLessEqual(output.count("？") + output.count("?"), 1)
        self.assertEqual(processor.issues, [])

    async def test_high_risk_output_appends_required_safety_contact(self):
        registry = MindBridgeSkillLibrary.registry()
        skills = [
            registry.get_required("supportive_response_baseline"),
            registry.get_required("high_risk_safety_plan"),
        ]
        processor = SkillOutputPostProcessor(skills)

        output = "".join([part async for part in processor.process(chunks("我听见你现在非常痛苦。请先去到有人陪伴的安全地点。"))])

        self.assertIn("联系", output)
        self.assertEqual(processor.issues, [])


if __name__ == "__main__":
    unittest.main()
