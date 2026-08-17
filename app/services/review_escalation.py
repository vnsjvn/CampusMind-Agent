from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.entities import ToolAuditRecord
from app.services.response_review import ResponseReview
from app.services.tool_approval import StaffToolApproval
from app.core.config import Settings
from app.agents.events import RuntimeEventType
from app.services.trace import AgentTraceService


class ResponseReviewEscalationService:
    TOOL_NAME = "HUMAN_RESPONSE_REVIEW"
    PENDING_STATUS = "PENDING_REVIEW"

    def __init__(self, db: Session):
        self.db = db

    def create_if_required(
        self,
        report_id: int | None,
        trace_id: int | None,
        session_public_id: str,
        review: ResponseReview,
    ) -> ToolAuditRecord | None:
        if not review.requires_escalation or report_id is None:
            return None
        existing = (
            self.db.query(ToolAuditRecord)
            .filter(
                ToolAuditRecord.report_id == report_id,
                ToolAuditRecord.tool_name == self.TOOL_NAME,
                ToolAuditRecord.status == self.PENDING_STATUS,
            )
            .first()
        )
        if existing is not None:
            return existing
        record = ToolAuditRecord(
            job_id=None,
            report_id=report_id,
            tool_name=self.TOOL_NAME,
            policy="response_reviewer.high_risk_escalation",
            allowed=True,
            status=self.PENDING_STATUS,
            reason=",".join(review.finding_codes),
            payload=json.dumps(
                {
                    "traceId": trace_id,
                    "sessionId": session_public_id,
                    "decision": review.decision,
                    "findingCodes": list(review.finding_codes),
                },
                ensure_ascii=False,
            ),
        )
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        return record

    def resolve(
        self,
        review_id: int,
        decision: str,
        actor: str,
        note: str,
        approval_token: str,
        settings: Settings,
    ) -> ToolAuditRecord:
        approved, reason = StaffToolApproval.verify(settings, approval_token)
        if not approved:
            raise PermissionError(reason)
        record = (
            self.db.query(ToolAuditRecord)
            .filter(ToolAuditRecord.id == review_id)
            .with_for_update()
            .first()
        )
        if record is None or record.tool_name != self.TOOL_NAME:
            raise ValueError("response review task not found")
        target_status = f"REVIEW_{decision}"
        if record.status == target_status:
            return record
        if record.status != self.PENDING_STATUS:
            raise RuntimeError(f"response review task already resolved: {record.status}")
        try:
            payload = json.loads(record.payload or "{}")
        except json.JSONDecodeError:
            payload = {}
        resolved_at = datetime.utcnow()
        payload["resolution"] = {
            "decision": decision,
            "resolvedBy": actor,
            "note": note.strip(),
            "resolvedAt": resolved_at.isoformat(),
        }
        record.status = target_status
        record.payload = json.dumps(payload, ensure_ascii=False)
        record.updated_at = resolved_at
        self.db.add(record)
        self.db.commit()
        self.db.refresh(record)
        trace_id = payload.get("traceId")
        if isinstance(trace_id, int):
            AgentTraceService(self.db).append_event(
                trace_id,
                RuntimeEventType.RESPONSE_REVIEW_RESOLVED,
                "HumanReviewer",
                str(payload.get("sessionId") or ""),
                {
                    "reviewTaskId": record.id,
                    "decision": decision,
                    "resolvedBy": actor,
                    "noteProvided": bool(note.strip()),
                },
            )
        return record
