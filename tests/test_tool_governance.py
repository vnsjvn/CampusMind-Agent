import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.core.enums import RiskLevel, ToolJobKind
from app.services.tool_governance import ToolPolicyRegistry


def report(risk: RiskLevel):
    return SimpleNamespace(risk_level=risk.value)


class ToolGovernanceTests(unittest.TestCase):
    def test_high_risk_alert_is_allowed_only_for_high_risk(self):
        allowed, _, _ = ToolPolicyRegistry.authorize(ToolJobKind.ALERT_SEND.value, report(RiskLevel.HIGH))
        blocked, reason, _ = ToolPolicyRegistry.authorize(ToolJobKind.ALERT_SEND.value, report(RiskLevel.LOW))

        self.assertTrue(allowed)
        self.assertFalse(blocked)
        self.assertIn("不允许", reason)

    def test_medium_case_create_is_allowed_but_low_is_blocked(self):
        allowed, _, _ = ToolPolicyRegistry.authorize(ToolJobKind.CASE_CREATE.value, report(RiskLevel.MEDIUM))
        blocked, _, _ = ToolPolicyRegistry.authorize(ToolJobKind.CASE_CREATE.value, report(RiskLevel.LOW))

        self.assertTrue(allowed)
        self.assertFalse(blocked)

    def test_unknown_tool_is_blocked(self):
        allowed, reason, policy = ToolPolicyRegistry.authorize("DELETE_EVERYTHING", report(RiskLevel.HIGH))

        self.assertFalse(allowed)
        self.assertIsNone(policy)
        self.assertIn("未知工具", reason)

    def test_skill_policy_can_block_globally_allowed_tool(self):
        with patch("app.services.tool_governance.MindBridgeSkillLibrary.allowed_tools_for_response", return_value=()):
            allowed, reason, _ = ToolPolicyRegistry.authorize(
                ToolJobKind.ALERT_SEND.value,
                report(RiskLevel.HIGH),
            )

        self.assertFalse(allowed)
        self.assertIn("Skill Policy 未授权", reason)

    def test_staff_tool_requires_scope_and_explicit_approval(self):
        medium = report(RiskLevel.MEDIUM)
        blocked_scope, scope_reason, _ = ToolPolicyRegistry.authorize("CASE_NOTE_ADD", medium)
        blocked_approval, approval_reason, _ = ToolPolicyRegistry.authorize(
            "CASE_NOTE_ADD", medium, caller_scope="STAFF", approved=False
        )
        allowed, _, _ = ToolPolicyRegistry.authorize(
            "CASE_NOTE_ADD", medium, caller_scope="STAFF", approved=True
        )

        self.assertFalse(blocked_scope)
        self.assertIn("无权", scope_reason)
        self.assertFalse(blocked_approval)
        self.assertIn("审批", approval_reason)
        self.assertTrue(allowed)

    def test_tool_policy_exposes_retry_timeout_and_cost(self):
        alert = ToolPolicyRegistry.policy_for(ToolJobKind.ALERT_SEND.value)
        note = ToolPolicyRegistry.policy_for("CASE_NOTE_ADD")

        self.assertTrue(alert.retryable)
        self.assertEqual(alert.max_attempts, 5)
        self.assertEqual(alert.cost_units, 3)
        self.assertFalse(note.retryable)
        self.assertEqual(note.max_attempts, 1)
        self.assertGreater(note.timeout_seconds, 0)


if __name__ == "__main__":
    unittest.main()
