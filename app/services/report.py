from __future__ import annotations

import math
from collections import Counter, defaultdict

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.entities import AlertRecord, AgentRunTrace, CaseNote, ChatMessage, ChatSession, DeadLetterRecord, ExcelRecord, PsychologicalReport, RiskCase, ToolAuditRecord, ToolJob, UserAccount
from app.schemas.dtos import AgentRunTraceResponse, CaseNoteResponse, ConversationMessageResponse, ConversationResponse, DeadLetterResponse, ReportResponse, RiskCaseResponse, ToolAuditResponse, ToolJobResponse, ToolRecordResponse


class ReportService:
    def __init__(self, db: Session, settings: Settings | None = None):
        self.db = db
        self.settings = settings or Settings(_env_file=None)

    def latest_reports(self, user_id: int | None = None) -> list[ReportResponse]:
        query = self.db.query(PsychologicalReport).order_by(PsychologicalReport.created_at.desc())
        if user_id is not None:
            query = query.filter(PsychologicalReport.user_id == user_id)
        return [self._report_response(item) for item in query.limit(100).all()]

    def excel_records(self) -> list[ToolRecordResponse]:
        rows = self.db.query(ExcelRecord).order_by(ExcelRecord.created_at.desc()).limit(100).all()
        return [
            ToolRecordResponse(id=row.id, reportId=row.report_id, status=row.status, message=row.message, createdAt=row.created_at, filePath=row.file_path)
            for row in rows
        ]

    def alert_records(self) -> list[ToolRecordResponse]:
        rows = self.db.query(AlertRecord).order_by(AlertRecord.created_at.desc()).limit(100).all()
        return [
            ToolRecordResponse(
                id=row.id,
                reportId=row.report_id,
                status=row.status,
                message=row.message,
                createdAt=row.created_at,
                channel=row.channel,
                recipient=row.recipient,
            )
            for row in rows
        ]

    def risk_cases(self) -> list[RiskCaseResponse]:
        rows = self.db.query(RiskCase).order_by(RiskCase.updated_at.desc()).limit(100).all()
        return [
            RiskCaseResponse(
                id=row.id,
                reportId=row.report_id,
                riskLevel=row.risk_level,
                status=row.status,
                owner=row.owner,
                summary=row.summary,
                handoffSummary=row.handoff_summary,
                acknowledgedBy=row.acknowledged_by,
                acknowledgedAt=row.acknowledged_at,
                createdAt=row.created_at,
                updatedAt=row.updated_at,
            )
            for row in rows
        ]

    def case_notes(self, case_id: int) -> list[CaseNoteResponse]:
        rows = self.db.query(CaseNote).filter(CaseNote.case_id == case_id).order_by(CaseNote.created_at.asc()).all()
        return [
            CaseNoteResponse(id=row.id, caseId=row.case_id, actor=row.actor, note=row.note, createdAt=row.created_at)
            for row in rows
        ]

    def tool_jobs(self) -> list[ToolJobResponse]:
        rows = self.db.query(ToolJob).order_by(ToolJob.created_at.desc()).limit(100).all()
        return [
            ToolJobResponse(
                id=row.id,
                reportId=row.report_id,
                operationalAlertId=row.operational_alert_id,
                kind=row.kind,
                status=row.status,
                attempts=row.attempts,
                maxAttempts=row.max_attempts,
                dependsOnJobId=row.depends_on_job_id,
                runAfter=row.run_after,
                lastError=row.last_error,
                createdAt=row.created_at,
                updatedAt=row.updated_at,
            )
            for row in rows
        ]

    def dead_letters(self) -> list[DeadLetterResponse]:
        rows = self.db.query(DeadLetterRecord).order_by(DeadLetterRecord.created_at.desc()).limit(100).all()
        return [
            DeadLetterResponse(
                id=row.id,
                jobId=row.job_id,
                reportId=row.report_id,
                operationalAlertId=row.operational_alert_id,
                kind=row.kind,
                reason=row.reason,
                payload=row.payload,
                createdAt=row.created_at,
            )
            for row in rows
        ]


    def conversation(self, public_id: str) -> ConversationResponse:
        session = self.db.query(ChatSession).filter(ChatSession.public_id == public_id).first()
        if session is None:
            raise ValueError("Session not found")
        rows = self.db.query(ChatMessage).filter(ChatMessage.session_id == session.id).order_by(ChatMessage.created_at.asc()).all()
        return ConversationResponse(
            sessionId=session.public_id,
            title=session.title,
            messages=[ConversationMessageResponse(role=row.role, content=row.content, createdAt=row.created_at) for row in rows],
        )

    def agent_run_traces(self) -> list[AgentRunTraceResponse]:
        rows = self.db.query(AgentRunTrace).order_by(AgentRunTrace.created_at.desc()).limit(100).all()
        responses = []
        for row in rows:
            user = self.db.get(UserAccount, row.user_id)
            session = self.db.get(ChatSession, row.session_id)
            responses.append(
                AgentRunTraceResponse(
                    id=row.id,
                    sessionId=session.public_id if session else "",
                    reportId=row.report_id,
                    username=user.username if user else "",
                    intent=row.intent,
                    riskLevel=row.risk_level,
                    originalInput=row.original_input,
                    sanitizedInput=row.sanitized_input,
                    memoryBrief=row.memory_brief,
                    agentSteps=_loads(row.agent_steps_json, []),
                    runtimeEvents=_loads(row.runtime_events_json, []),
                    retrievedKnowledge=_loads(row.retrieved_knowledge_json, []),
                    responseMessages=_loads(row.response_messages_json, []),
                    assessment=_loads(row.assessment_json, {}),
                    createdAt=row.created_at,
                )
            )
        return responses

    def agent_trace_metrics(self, limit: int = 500) -> dict:
        window = max(1, min(int(limit), 1000))
        rows = self.db.query(AgentRunTrace).order_by(AgentRunTrace.created_at.desc()).limit(window).all()
        event_counts: Counter[str] = Counter()
        failure_stages: Counter[str] = Counter()
        agent_durations: dict[str, list[float]] = defaultdict(list)
        runtime_durations: list[float] = []
        first_token_durations: list[float] = []
        output_durations: list[float] = []
        prompt_tokens: list[float] = []
        completion_tokens: list[float] = []
        runtime_completed = end_to_end_completed = failed = 0
        planning_retry_events = planning_failed_steps = 0
        planning_runs_with_retry = planning_runs_with_failure = 0
        planning_by_step: dict[str, Counter[str]] = defaultdict(Counter)
        planning_by_agent: dict[str, Counter[str]] = defaultdict(Counter)

        for row in rows:
            events = _loads(row.runtime_events_json, [])
            types = {str(event.get("type", "")) for event in events}
            runtime_completed += int("RUN_COMPLETED" in types)
            end_to_end_completed += int("LLM_STREAM_COMPLETED" in types)
            review_failed = any(
                event.get("type") == "RESPONSE_REVIEWED" and not (event.get("payload") or {}).get("valid", False)
                for event in events
            )
            run_failed = (
                "RUN_FAILED" in types or "LLM_STREAM_FAILED" in types
                or "PLAN_STEP_FAILED" in types or review_failed
            )
            failed += int(run_failed)
            planning_runs_with_retry += int("PLAN_STEP_RETRYING" in types)
            planning_runs_with_failure += int("PLAN_STEP_FAILED" in types)
            for event in events:
                event_type = str(event.get("type", ""))
                event_counts[event_type] += 1
                payload = event.get("payload") or {}
                producer = str(event.get("producer", "Unknown"))
                _collect_number(agent_durations[producer], payload.get("durationMs"))
                if event_type == "RUN_COMPLETED":
                    _collect_number(runtime_durations, payload.get("totalDurationMs"))
                elif event_type == "LLM_STREAM_COMPLETED":
                    _collect_number(first_token_durations, payload.get("modelFirstTokenMs"))
                    _collect_number(output_durations, payload.get("outputDurationMs"))
                    _collect_number(prompt_tokens, payload.get("promptTokens"))
                    _collect_number(completion_tokens, payload.get("completionTokens"))
                elif event_type == "RUN_FAILED":
                    failure_stages[str(payload.get("failedStage") or "RuntimeUnknown")] += 1
                elif event_type == "LLM_STREAM_FAILED":
                    failure_stages["LLMStream"] += 1
                elif event_type == "RESPONSE_REVIEWED" and not payload.get("valid", False):
                    failure_stages["ResponseReview"] += 1
                elif event_type in {"PLAN_STEP_RETRYING", "PLAN_STEP_FAILED"}:
                    metric = "retries" if event_type == "PLAN_STEP_RETRYING" else "failures"
                    step_id = str(payload.get("stepId") or "unknown")
                    planning_by_step[step_id][metric] += 1
                    planning_by_agent[producer][metric] += 1
                    if metric == "retries":
                        planning_retry_events += 1
                    else:
                        planning_failed_steps += 1
                        failure_stages[f"Plan:{step_id}"] += 1

        tool_completed = event_counts["TOOL_COMPLETED"]
        tool_dead = event_counts["TOOL_DEAD_LETTERED"]
        terminal_tools = tool_completed + tool_dead
        planning_health = _planning_health(
            len(rows), planning_runs_with_retry, planning_runs_with_failure, self.settings
        )
        return {
            "windowSize": len(rows),
            "requestedLimit": window,
            "runs": {
                "runtimeCompleted": runtime_completed,
                "endToEndCompleted": end_to_end_completed,
                "failed": failed,
                "inProgressOrLegacy": max(0, len(rows) - end_to_end_completed - failed),
            },
            "latencyMs": {
                "runtime": _metric_stats(runtime_durations),
                "modelFirstToken": _metric_stats(first_token_durations),
                "userOutput": _metric_stats(output_durations),
                "agents": {
                    producer: _metric_stats(values)
                    for producer, values in sorted(agent_durations.items())
                    if values
                },
            },
            "tokens": {
                "prompt": _metric_stats(prompt_tokens),
                "completion": _metric_stats(completion_tokens),
            },
            "tools": {
                "queued": event_counts["TOOL_QUEUED"],
                "started": event_counts["TOOL_STARTED"],
                "requeued": event_counts["TOOL_REQUEUED"],
                "completed": tool_completed,
                "deadLettered": tool_dead,
                "successRate": round(tool_completed / terminal_tools, 4) if terminal_tools else None,
            },
            "planning": {
                "retryEvents": planning_retry_events,
                "failedSteps": planning_failed_steps,
                "runsWithRetry": planning_runs_with_retry,
                "runsWithFailure": planning_runs_with_failure,
                "retryRunRate": round(planning_runs_with_retry / len(rows), 4) if rows else None,
                "failureRunRate": round(planning_runs_with_failure / len(rows), 4) if rows else None,
                "byStep": _planning_breakdown(planning_by_step),
                "byAgent": _planning_breakdown(planning_by_agent),
                "health": planning_health,
            },
            "failureStages": dict(sorted(failure_stages.items())),
        }

    def tool_audits(self) -> list[ToolAuditResponse]:
        rows = self.db.query(ToolAuditRecord).order_by(ToolAuditRecord.created_at.desc()).limit(100).all()
        return [self._tool_audit_response(row) for row in rows]

    def response_reviews(self, status: str = "ALL", limit: int = 20, offset: int = 0) -> dict:
        allowed = {"ALL", "PENDING_REVIEW", "REVIEW_APPROVED", "REVIEW_REJECTED"}
        normalized_status = status.upper()
        if normalized_status not in allowed:
            raise ValueError(f"unsupported response review status: {status}")
        bounded_limit = max(1, min(int(limit), 100))
        bounded_offset = max(0, int(offset))
        base = self.db.query(ToolAuditRecord).filter(
            ToolAuditRecord.tool_name == "HUMAN_RESPONSE_REVIEW"
        )
        query = base
        if normalized_status != "ALL":
            query = query.filter(ToolAuditRecord.status == normalized_status)
        total = query.count()
        rows = (
            query.order_by(ToolAuditRecord.updated_at.desc(), ToolAuditRecord.id.desc())
            .offset(bounded_offset)
            .limit(bounded_limit)
            .all()
        )
        counts = dict(
            self.db.query(ToolAuditRecord.status, func.count(ToolAuditRecord.id))
            .filter(ToolAuditRecord.tool_name == "HUMAN_RESPONSE_REVIEW")
            .group_by(ToolAuditRecord.status)
            .all()
        )
        return {
            "items": [self._tool_audit_response(row).model_dump() for row in rows],
            "total": total,
            "limit": bounded_limit,
            "offset": bounded_offset,
            "status": normalized_status,
            "statusCounts": {key: int(counts.get(key, 0)) for key in sorted(allowed - {"ALL"})},
        }

    @staticmethod
    def _tool_audit_response(row: ToolAuditRecord) -> ToolAuditResponse:
        return ToolAuditResponse(
            id=row.id,
            jobId=row.job_id,
            reportId=row.report_id,
            toolName=row.tool_name,
            policy=row.policy,
            allowed=row.allowed,
            status=row.status,
            reason=row.reason,
            payload=_loads(row.payload, {}),
            createdAt=row.created_at,
            updatedAt=row.updated_at,
        )

    def _report_response(self, report: PsychologicalReport) -> ReportResponse:
        user = self.db.get(UserAccount, report.user_id)
        session = self.db.get(ChatSession, report.session_id)
        return ReportResponse(
            id=report.id,
            sessionId=session.public_id if session else "",
            username=user.username if user else "",
            displayName=user.display_name if user else "",
            content=report.content,
            intent=report.intent,
            emotion=report.emotion,
            emotionScore=report.emotion_score,
            riskLevel=report.risk_level,
            confidence=report.confidence,
            summary=report.summary,
            createdAt=report.created_at,
        )


def _collect_number(target: list[float], value) -> None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        target.append(float(value))


def _metric_stats(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "avg": None, "p95": None, "max": None}
    ordered = sorted(values)
    p95_index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return {
        "count": len(ordered),
        "avg": round(sum(ordered) / len(ordered), 3),
        "p95": round(ordered[p95_index], 3),
        "max": round(ordered[-1], 3),
    }


def _planning_breakdown(values: dict[str, Counter[str]]) -> dict[str, dict[str, int]]:
    return {
        key: {
            "retries": int(counts.get("retries", 0)),
            "failures": int(counts.get("failures", 0)),
        }
        for key, counts in sorted(values.items())
    }


def _planning_health(
    sample_size: int, runs_with_retry: int, runs_with_failure: int, settings: Settings
) -> dict:
    minimum = max(1, int(settings.trace_planning_min_sample_size))
    retry_threshold = min(1.0, max(0.0, float(settings.trace_planning_retry_warning_rate)))
    failure_threshold = min(1.0, max(0.0, float(settings.trace_planning_failure_critical_rate)))
    retry_rate = runs_with_retry / sample_size if sample_size else 0.0
    failure_rate = runs_with_failure / sample_size if sample_size else 0.0
    alerts = []
    if sample_size >= minimum:
        if retry_rate >= retry_threshold:
            alerts.append({
                "code": "PLAN_RETRY_RATE_HIGH", "severity": "WARNING",
                "observedRate": round(retry_rate, 4), "thresholdRate": retry_threshold,
            })
        if failure_rate >= failure_threshold:
            alerts.append({
                "code": "PLAN_FAILURE_RATE_HIGH", "severity": "CRITICAL",
                "observedRate": round(failure_rate, 4), "thresholdRate": failure_threshold,
            })
    status = "INSUFFICIENT_DATA"
    if sample_size >= minimum:
        status = "CRITICAL" if any(item["severity"] == "CRITICAL" for item in alerts) else (
            "WARNING" if alerts else "HEALTHY"
        )
    return {
        "status": status,
        "sampleSize": sample_size,
        "minimumSampleSize": minimum,
        "alerts": alerts,
    }


def _loads(raw: str, default):
    import json

    try:
        return json.loads(raw or "")
    except Exception:
        return default
