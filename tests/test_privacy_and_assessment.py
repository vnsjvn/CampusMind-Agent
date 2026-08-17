import unittest
import json
from types import SimpleNamespace

from app.core.enums import RiskLevel
from app.services.assessment import PsychologicalAssessmentService
from app.services.memory import RedisShortTermMemoryStore
from app.services.privacy import PrivacySanitizer


class ExplodingAi:
    def complete(self, messages):
        raise AssertionError("high risk hard guard should not call the model")


class PrivacyAndAssessmentTests(unittest.TestCase):
    def test_privacy_sanitizer_masks_common_identifiers(self):
        text = PrivacySanitizer().sanitize("电话 13800138000 邮箱 a@example.com 身份证 110101199003071234")

        self.assertNotIn("13800138000", text)
        self.assertNotIn("a@example.com", text)
        self.assertNotIn("110101199003071234", text)
        self.assertEqual(text.count("[已脱敏]"), 3)

    def test_redis_memory_serializes_sanitized_content(self):
        store = RedisShortTermMemoryStore.__new__(RedisShortTermMemoryStore)
        store.privacy = PrivacySanitizer()

        payload = json.loads(store._serialize("user", "电话 13800138000 邮箱 a@example.com"))

        self.assertNotIn("13800138000", payload["content"])
        self.assertNotIn("a@example.com", payload["content"])
        self.assertEqual(payload["content"].count("[已脱敏]"), 2)

    def test_redis_memory_load_recent_returns_deserialized_messages(self):
        store = RedisShortTermMemoryStore.__new__(RedisShortTermMemoryStore)
        store.privacy = PrivacySanitizer()
        store.settings = SimpleNamespace(redis_memory_max_messages=10)
        store.client = SimpleNamespace(
            lrange=lambda *_: ['{"role":"user","content":"最近睡不好"}']
        )

        messages = store.load_recent("session-1")

        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].content, "最近睡不好")

    def test_high_risk_signal_uses_hard_guard_before_model(self):
        result = PsychologicalAssessmentService(ExplodingAi()).assess("我不想活了，想结束生命")

        self.assertEqual(result.risk, RiskLevel.HIGH)
        self.assertGreaterEqual(result.confidence, 0.9)


if __name__ == "__main__":
    unittest.main()
