
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.enums import IntentType, RiskLevel, ToolJobKind
from app.models.entities import PsychologicalReport, ToolAuditRecord, ToolJob
from app.services.skills import MindBridgeSkillLibrary


@dataclass(frozen=True)
class ToolPolicy:
    name: str
    capability: str
    description: str
    allowed_risks: tuple[str, ...]
    requires_report: bool = True
    allowed_callers: tuple[str, ...] = ("SYSTEM",)
    approval_required: bool = False
    timeout_seconds: float = 30.0
    retryable: bool = True
    max_attempts: int = 3
    cost_units: int = 1


class ToolPolicyRegistry:
    POLICIES: dict[str, ToolPolicy] = {
        ToolJobKind.EXCEL_REPORT.value: ToolPolicy(
            name=ToolJobKind.EXCEL_REPORT.value,
            capability="report.export",
            description="Write a psychological report row into the counselor-facing Excel ledger.",
            allowed_risks=(RiskLevel.LOW.value, RiskLevel.MEDIUM.value, RiskLevel.HIGH.value),
            timeout_seconds=30.0,
            max_attempts=3,
            cost_units=1,
        ),
        ToolJobKind.CASE_CREATE.value: ToolPolicy(
            name=ToolJobKind.CASE_CREATE.value,
            capability="risk_case.create",
            description="Create or reuse a counselor-facing risk case for medium/high risk reports.",
            allowed_risks=(RiskLevel.MEDIUM.value, RiskLevel.HIGH.value),
            timeout_seconds=10.0,
            max_attempts=3,
            cost_units=1,
        ),
        ToolJobKind.ALERT_SEND.value: ToolPolicy(
            name=ToolJobKind.ALERT_SEND.value,
            capability="risk_alert.send",
            description="Send or log an urgent counselor alert for high risk reports.",
            allowed_risks=(RiskLevel.HIGH.value,),
            timeout_seconds=30.0,
            max_attempts=5,
            cost_units=3,
        ),
        ToolJobKind.RISK_ALERT.value: ToolPolicy(
            name=ToolJobKind.RISK_ALERT.value,
            capability="risk_alert.legacy_notify",
            description="Legacy high-risk alert action; retained for compatibility.",
            allowed_risks=(RiskLevel.HIGH.value,),
            timeout_seconds=30.0,
            max_attempts=5,
            cost_units=3,
        ),
        "ALERT_ACK": ToolPolicy(
            name="ALERT_ACK",
            capability="risk_case.acknowledge",
            description="Acknowledge that a counselor or administrator has taken ownership of a risk case.",
            allowed_risks=(RiskLevel.MEDIUM.value, RiskLevel.HIGH.value),
            allowed_callers=("STAFF",),
            approval_required=True,
            timeout_seconds=10.0,
            retryable=False,
            max_attempts=1,
            cost_units=1,
        ),
        "CASE_NOTE_ADD": ToolPolicy(
            name="CASE_NOTE_ADD",
            capability="risk_case.note.add",
            description="Append a staff-authored follow-up note to a counselor-facing risk case.",
            allowed_risks=(RiskLevel.MEDIUM.value, RiskLevel.HIGH.value),
            allowed_callers=("STAFF",),
            approval_required=True,
            timeout_seconds=10.0,
            retryable=False,
            max_attempts=1,
            cost_units=1,
        ),
    }

    @classmethod
    def policy_for(cls, tool_name: str) -> ToolPolicy | None:
        return cls.POLICIES.get(tool_name)

    @classmethod
    def status_items(cls) -> list[dict[str, Any]]:
        return [
            {
                "name": policy.name,
                "capability": policy.capability,
                "allowedRisks": list(policy.allowed_risks),
                "requiresReport": policy.requires_report,
                "allowedCallers": list(policy.allowed_callers),
                "approvalRequired": policy.approval_required,
                "timeoutSeconds": policy.timeout_seconds,
                "retryable": policy.retryable,
                "maxAttempts": policy.max_attempts,
                "costUnits": policy.cost_units,
                "status": "READY",
            }
            for policy in sorted(cls.POLICIES.values(), key=lambda item: item.name)
        ]

    @classmethod
    def authorize(
        cls,
        tool_name: str,
        report: PsychologicalReport | None,
        caller_scope: str = "SYSTEM",
        approved: bool = False,
    ) -> tuple[bool, str, ToolPolicy | None]:
        policy = cls.policy_for(tool_name)
        if policy is None:
            return False, f"未知工具：{tool_name}", None
        if policy.requires_report and report is None:
            return False, "工具执行需要心理报告，但未找到 report", policy
        normalized_caller = caller_scope.strip().upper()
        if normalized_caller not in policy.allowed_callers:
            return False, f"调用者权限 {normalized_caller or 'UNKNOWN'} 无权执行工具 {tool_name}", policy
        if policy.approval_required and not approved:
            return False, f"工具 {tool_name} 需要有效的人工审批凭证", policy
        risk = report.risk_level if report is not None else ""
        if risk not in policy.allowed_risks:
            return False, f"工具 {tool_name} 不允许处理风险等级 {risk}", policy
        if report is not None and normalized_caller == "SYSTEM":
            risk_level = RiskLevel(report.risk_level)
            intent = IntentType.RISK if risk_level == RiskLevel.HIGH else IntentType.CONSULT
            allowed_tools = MindBridgeSkillLibrary.allowed_tools_for_response(intent, risk_level, getattr(report, "content", ""))
            if tool_name not in allowed_tools:
                return False, f"当前 Skill Policy 未授权工具 {tool_name}", policy
        return True, "允许执行", policy


class ToolGovernanceService:
    def __init__(self, db: Session):
        self.db = db

    def start_job(self, job: ToolJob, report: PsychologicalReport | None) -> ToolAuditRecord:
        allowed, reason, policy = ToolPolicyRegistry.authorize(job.kind, report)
        record = ToolAuditRecord(
            job_id=job.id,
            report_id=job.report_id,
            tool_name=job.kind,
            policy=policy.name if policy else "unknown",
            allowed=allowed,
            status="AUTHORIZED" if allowed else "BLOCKED",
            reason=reason,
            payload=_json(
                {
                    "jobId": job.id,
                    "kind": job.kind,
                    "attempts": job.attempts,
                    "riskLevel": report.risk_level if report is not None else None,
                    "policy": asdict(policy) if policy else None,
                }
            ),
        )
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        return record

    def require_allowed(self, job: ToolJob, report: PsychologicalReport | None) -> None:
        allowed, reason, _ = ToolPolicyRegistry.authorize(job.kind, report)
        if not allowed:
            raise RuntimeError(reason)

    def finish(self, record: ToolAuditRecord, status: str, reason: str = "", payload: dict[str, Any] | None = None) -> ToolAuditRecord:
        record.status = status
        record.reason = reason or record.reason
        if payload is not None:
            record.payload = _json(payload)
        record.updated_at = datetime.utcnow()
        self.db.add(record)
        self.db.commit()
        return record


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
