import unittest
from types import SimpleNamespace

from app.schemas.dtos import AiMessage
from app.services.memory import compact_history_for_prompt, summarize_history_for_memory


def settings(**overrides):
    values = {
        "memory_compaction_enabled": True,
        "memory_compaction_recent_messages": 4,
        "memory_summary_max_chars": 220,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class MemoryCompactionTests(unittest.TestCase):
    def test_compaction_keeps_recent_messages_and_adds_internal_summary(self):
        history = [AiMessage(role="user" if i % 2 == 0 else "assistant", content=f"第{i}条消息 13800138000") for i in range(10)]

        compacted, brief = compact_history_for_prompt(history, settings(), "我最近还是睡不着")

        self.assertEqual(compacted[0].role, "system")
        self.assertIn("历史摘要", compacted[0].content)
        self.assertEqual(len(compacted), 5)
        self.assertTrue(compacted[-1].content.startswith("第9条消息"))
        self.assertNotIn("13800138000", brief)
        self.assertIn("[已脱敏]", brief)

    def test_compaction_can_be_disabled(self):
        history = [AiMessage(role="user", content="我最近压力很大")]

        compacted, brief = compact_history_for_prompt(history, settings(memory_compaction_enabled=False), "")

        self.assertEqual(compacted, history)
        self.assertIn("学生近期关注", brief)

    def test_summary_is_bounded(self):
        history = [AiMessage(role="user", content="压力" * 200)]

        brief = summarize_history_for_memory(history, max_chars=80)

        self.assertLessEqual(len(brief), 80)


if __name__ == "__main__":
    unittest.main()
