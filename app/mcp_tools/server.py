from app.core.bootstrap import create_schema
from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.entities import PsychologicalReport, RiskCase
from app.services.tools import ToolOrchestrationService
from app.services.tool_registry import ToolExecutorRegistry
from app.services.tool_governance import ToolPolicyRegistry
from app.services.tool_approval import StaffToolApproval

try:
    from mcp.server.fastmcp import FastMCP
except Exception as exc:  # pragma: no cover
    raise RuntimeError("请先安装 requirements.txt 中的 mcp 依赖") from exc


mcp = FastMCP("mindbridge-python-tools")


def _require_policy(tool_name: str, report: PsychologicalReport | None, caller_scope: str = "SYSTEM", approved: bool = False) -> None:
    allowed, reason, _ = ToolPolicyRegistry.authorize(tool_name, report, caller_scope, approved)
    if not allowed:
        raise RuntimeError(reason)


@mcp.tool()
def mindbridge_excel_report(report_id: int) -> str:
    """Write one psychological risk report into the MindBridge Excel ledger."""
    create_schema()
    db = SessionLocal()
    try:
        report = db.get(PsychologicalReport, report_id)
        if report is None:
            return f"report {report_id} not found"
        _require_policy("EXCEL_REPORT", report)
        registry = ToolExecutorRegistry(ToolOrchestrationService(db, get_settings()))
        record = registry.execute("EXCEL_REPORT", report=report)
        return f"success: {record.file_path}"
    finally:
        db.close()


@mcp.tool()
def mindbridge_case_create(report_id: int) -> str:
    """Create or return the active MindBridge risk case for one psychological report."""
    create_schema()
    db = SessionLocal()
    try:
        report = db.get(PsychologicalReport, report_id)
        if report is None:
            return f"report {report_id} not found"
        _require_policy("CASE_CREATE", report)
        registry = ToolExecutorRegistry(ToolOrchestrationService(db, get_settings()))
        case = registry.execute("CASE_CREATE", report=report)
        return f"success: caseId={case.id}, reportId={case.report_id}, status={case.status}"
    finally:
        db.close()


@mcp.tool()
def mindbridge_alert_send(case_id: int) -> str:
    """Send or record the counselor alert for one MindBridge risk case."""
    create_schema()
    db = SessionLocal()
    try:
        case = db.get(RiskCase, case_id)
        if case is None:
            return f"case {case_id} not found"
        report = db.get(PsychologicalReport, case.report_id)
        _require_policy("ALERT_SEND", report)
        registry = ToolExecutorRegistry(ToolOrchestrationService(db, get_settings()))
        record = registry.execute("ALERT_SEND", case_id=case_id)
        return f"{record.status}: caseId={case_id}, {record.channel} -> {record.recipient}: {record.message}"
    finally:
        db.close()


@mcp.tool()
def mindbridge_alert_ack(case_id: int, actor: str, approval_token: str, note: str = "") -> str:
    """Mark a MindBridge risk case as acknowledged by a counselor or administrator."""
    create_schema()
    db = SessionLocal()
    try:
        settings = get_settings()
        case = db.get(RiskCase, case_id)
        if case is None:
            return f"case {case_id} not found"
        report = db.get(PsychologicalReport, case.report_id)
        approved, reason = StaffToolApproval.verify(settings, approval_token)
        if not approved:
            raise RuntimeError(reason)
        _require_policy("ALERT_ACK", report, "STAFF", approved=True)
        registry = ToolExecutorRegistry(ToolOrchestrationService(db, settings))
        case = registry.execute("ALERT_ACK", case_id=case_id, actor=actor, note=note)
        return f"success: caseId={case.id}, status={case.status}, acknowledgedBy={case.acknowledged_by}"
    finally:
        db.close()


@mcp.tool()
def mindbridge_case_note_add(case_id: int, actor: str, note: str, approval_token: str) -> str:
    """Append a follow-up note to a MindBridge risk case."""
    create_schema()
    db = SessionLocal()
    try:
        settings = get_settings()
        case = db.get(RiskCase, case_id)
        if case is None:
            return f"case {case_id} not found"
        report = db.get(PsychologicalReport, case.report_id)
        approved, reason = StaffToolApproval.verify(settings, approval_token)
        if not approved:
            raise RuntimeError(reason)
        _require_policy("CASE_NOTE_ADD", report, "STAFF", approved=True)
        registry = ToolExecutorRegistry(ToolOrchestrationService(db, settings))
        record = registry.execute("CASE_NOTE_ADD", case_id=case_id, actor=actor, note=note)
        return f"success: noteId={record.id}, caseId={record.case_id}"
    finally:
        db.close()


@mcp.tool()
def mindbridge_alert_notify(report_id: int) -> str:
    """Send a high-risk alert email and record the notification result for one psychological report."""
    create_schema()
    db = SessionLocal()
    try:
        report = db.get(PsychologicalReport, report_id)
        if report is None:
            return f"report {report_id} not found"
        _require_policy("RISK_ALERT", report)
        registry = ToolExecutorRegistry(ToolOrchestrationService(db, get_settings()))
        record = registry.execute("RISK_ALERT", report=report)
        return f"{record.status}: {record.channel} -> {record.recipient}: {record.message}"
    finally:
        db.close()


if __name__ == "__main__":
    mcp.run()
