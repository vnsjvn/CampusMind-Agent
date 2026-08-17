import unittest

from app.core.config import Settings
from app.services.tool_approval import StaffToolApproval


class StaffToolApprovalTests(unittest.TestCase):
    def test_missing_or_short_configuration_is_denied(self):
        missing, _ = StaffToolApproval.verify(Settings(_env_file=None, mcp_staff_approval_token=""), "anything")
        short, _ = StaffToolApproval.verify(Settings(_env_file=None, mcp_staff_approval_token="short"), "short")

        self.assertFalse(missing)
        self.assertFalse(short)

    def test_token_must_match_using_secure_comparison(self):
        settings = Settings(_env_file=None, mcp_staff_approval_token="a-secure-token-12345")

        rejected, _ = StaffToolApproval.verify(settings, "wrong-token")
        accepted, _ = StaffToolApproval.verify(settings, "a-secure-token-12345")

        self.assertFalse(rejected)
        self.assertTrue(accepted)


if __name__ == "__main__":
    unittest.main()
