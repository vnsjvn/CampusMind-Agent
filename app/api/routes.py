from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.agents.factory import agent_framework_status
from app.agents.runtime import AgentRuntimeService
from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.core.security import current_user, require_admin
from app.models.entities import UserAccount
from app.schemas.dtos import KnowledgeIngestRequest, KnowledgeIngestResponse, ChatRequest, ResponseReviewResolutionRequest, ResponseReviewResolutionResponse, authority
from app.services.chat import ChatService
from app.services.knowledge import KnowledgeService
from app.services.model_assets import finetuned_model_status
from app.services.report import ReportService
from app.services.skills import MindBridgeSkillLibrary
from app.services.tool_governance import ToolPolicyRegistry
from app.services.tool_registry import ToolExecutorRegistry
from app.services.review_escalation import ResponseReviewEscalationService
from app.services.operational_alerts import OperationalAlertService

router = APIRouter()


@router.get("/actuator/health")
def health():
    return {"status": "UP"}


@router.get("/api/profile")
def profile(user: Annotated[UserAccount, Depends(current_user)]):
    return {
        "id": user.id,
        "username": user.username,
        "displayName": user.display_name,
        "roles": [authority(role) for role in user.roles],
    }


@router.post("/api/chat/stream")
async def chat_stream(
    request: ChatRequest,
    user: Annotated[UserAccount, Depends(current_user)],
    db: Annotated[Session, Depends(get_db)],
):
    if "ROLE_ADMIN" in user.roles:
        raise HTTPException(403, "管理员账号只能查看后台记录，不能发起学生对话。")
    service = ChatService(db, get_settings())
    return StreamingResponse(service.stream_chat(user, request), media_type="text/event-stream")


@router.get("/api/agent/status")
def agent_status(user: Annotated[UserAccount, Depends(current_user)]):
    settings = get_settings()
    provider = settings.ai_provider.lower()
    model = settings.ollama_model if provider == "ollama" else settings.openai_model if provider == "openai" else "mock"
    framework = agent_framework_status(settings)
    return {
        "provider": provider,
        "model": model,
        "realModelEnabled": provider in {"ollama", "openai"},
        "agentFramework": framework,
        "finetunedModel": finetuned_model_status(settings),
        "agents": [
            {"name": "MemoryAgent", "status": "READY", "description": "短期上下文与长期记忆摘要"},
            {"name": "SupervisorAgent", "status": "READY", "description": "意图识别与路由"},
            {"name": "KnowledgeAgent", "status": "READY", "description": "RAG 检索与证据补充"},
            {"name": "RiskGuardianAgent", "status": "READY", "description": "心理风险识别与分级"},
            {"name": "CompanionAgent", "status": "READY", "description": "普通陪伴式回复"},
            {"name": "CounselorAgent", "status": "READY", "description": "咨询式支持回复"},
        ],
        "skills": MindBridgeSkillLibrary.status_items(),
        "tools": ToolPolicyRegistry.status_items(),
        "toolExecutors": ToolExecutorRegistry.catalog_items(),
        "runtimeHarness": {
            "name": "MindBridgeAgentHarness",
            "status": "READY",
            "description": "统一管理单轮 Agent run 的输入脱敏、上下文注入、风险报告、工具计划和 trace 输出",
        },
        "loop": {
            "type": "bounded-agent-loop",
            "maxSteps": AgentRuntimeService.max_steps,
            "scheduler": "langgraph-controller" if framework["active"] == "langgraph" else "custom-runtime",
        },
    }


@router.get("/api/reports/me")
def my_reports(user: Annotated[UserAccount, Depends(current_user)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).latest_reports(user.id)


@router.get("/api/admin/reports")
def admin_reports(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).latest_reports()


@router.get("/api/admin/excel-records")
def admin_excel(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).excel_records()


@router.get("/api/admin/alerts")
def admin_alerts(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).alert_records()


@router.get("/api/admin/cases")
def admin_cases(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).risk_cases()


@router.get("/api/admin/cases/{case_id}/notes")
def admin_case_notes(case_id: int, _: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).case_notes(case_id)


@router.get("/api/admin/tool-jobs")
def admin_tool_jobs(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).tool_jobs()


@router.get("/api/admin/dead-letters")
def admin_dead_letters(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).dead_letters()


@router.get("/api/admin/agent-traces")
def admin_agent_traces(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).agent_run_traces()


@router.get("/api/admin/agent-trace-metrics")
def admin_agent_trace_metrics(
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    limit: int = 500,
):
    return ReportService(db, settings).agent_trace_metrics(limit)


@router.post("/api/admin/operational-alerts/evaluate")
def admin_evaluate_operational_alerts(
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_settings)],
    limit: int = 500,
):
    metrics = ReportService(db, settings).agent_trace_metrics(limit)
    alerts = OperationalAlertService(db).synchronize_planning_health(metrics["planning"]["health"])
    return {"health": metrics["planning"]["health"], "alerts": alerts}


@router.get("/api/admin/operational-alerts")
def admin_operational_alerts(
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    return OperationalAlertService(db).list_alerts()


@router.get("/api/admin/tool-audits")
def admin_tool_audits(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return ReportService(db).tool_audits()


@router.get("/api/admin/response-reviews")
def admin_response_reviews(
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    status: str = "ALL",
    limit: int = 20,
    offset: int = 0,
):
    try:
        return ReportService(db).response_reviews(status, limit, offset)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/api/admin/response-reviews/{review_id}/resolve", response_model=ResponseReviewResolutionResponse)
def resolve_response_review(
    review_id: int,
    request: ResponseReviewResolutionRequest,
    admin: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    try:
        record = ResponseReviewEscalationService(db).resolve(
            review_id,
            request.decision,
            admin.username,
            request.note,
            request.approvalToken,
            get_settings(),
        )
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return ResponseReviewResolutionResponse(
        id=record.id,
        reportId=record.report_id,
        status=record.status,
        decision=request.decision,
        resolvedBy=admin.username,
        updatedAt=record.updated_at,
    )


@router.get("/api/admin/conversations/{session_id}")
def admin_conversation(session_id: str, _: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    try:
        return ReportService(db).conversation(session_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/api/admin/knowledge")
def ingest_knowledge(
    request: KnowledgeIngestRequest,
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
):
    chunks = KnowledgeService(db, get_settings()).ingest(request.source, request.content)
    return KnowledgeIngestResponse(source=request.source, chunks=chunks)


@router.get("/api/admin/knowledge/status")
def knowledge_status(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    return KnowledgeService(db, get_settings()).status()


@router.post("/api/admin/knowledge/rebuild-vector")
def rebuild_knowledge_vector(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    try:
        indexed = KnowledgeService(db, get_settings()).rebuild_vector_index()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"indexedChunks": indexed}


@router.post("/api/admin/knowledge/backup")
def backup_knowledge_vector(_: Annotated[UserAccount, Depends(require_admin)], db: Annotated[Session, Depends(get_db)]):
    try:
        snapshot = KnowledgeService(db, get_settings()).backup_vector_index()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"snapshot": snapshot}


@router.post("/api/admin/knowledge/file")
async def ingest_file(
    _: Annotated[UserAccount, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
    file: UploadFile = File(...),
):
    chunks = KnowledgeService(db, get_settings()).ingest_file(file.filename or "uploaded-file", await file.read())
    return KnowledgeIngestResponse(source=file.filename or "uploaded-file", chunks=chunks)
