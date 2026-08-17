from __future__ import annotations

import hmac

from app.core.config import Settings


class StaffToolApproval:
    @staticmethod
    def verify(settings: Settings, supplied_token: str) -> tuple[bool, str]:
        configured = settings.mcp_staff_approval_token.strip()
        if not configured:
            return False, "MCP_STAFF_APPROVAL_TOKEN 未配置，人工工具默认拒绝"
        if len(configured) < 16:
            return False, "MCP_STAFF_APPROVAL_TOKEN 长度必须至少为16个字符"
        supplied = supplied_token.strip()
        if not supplied or not hmac.compare_digest(configured, supplied):
            return False, "人工审批凭证无效"
        return True, "人工审批凭证有效"
