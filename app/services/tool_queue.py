from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import SessionLocal
from app.core.enums import RiskLevel, ToolJobKind, ToolJobStatus, ToolStatus
from app.models.entities import DeadLetterRecord, ExcelRecord, PsychologicalReport, ToolJob
from app.services.tool_governance import ToolGovernanceService, ToolPolicyRegistry
from app.services.tools import ToolOrchestrationService
from app.services.tool_registry import ToolExecutorRegistry
from app.services.trace import AgentTraceService
from app.agents.events import RuntimeEventType


logger = logging.getLogger(__name__)


class ToolQueueService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings

    def enqueue_report(self, report_id: int, risk_level: str | None) -> list[ToolJob]:
        # Serialize enqueue operations for one report in MySQL. This prevents
        # concurrent requests from creating duplicate active jobs.
        report = self.db.query(PsychologicalReport).filter(PsychologicalReport.id == report_id).with_for_update().first()
        if report is None:
            raise ValueError(f"report {report_id} not found")
        excel_job = self._find_or_create(ToolJobKind.EXCEL_REPORT.value, report_id)
        jobs = [excel_job]
        case_job = None
        if risk_level in {RiskLevel.MEDIUM.value, RiskLevel.HIGH.value}:
            case_job = self._find_or_create(ToolJobKind.CASE_CREATE.value, report_id)
            jobs.append(case_job)
        if risk_level == RiskLevel.HIGH.value:
            alert_job = self._find_or_create(ToolJobKind.ALERT_SEND.value, report_id, case_job.id if case_job else None)
            jobs.append(alert_job)
        self.db.commit()
        for job in jobs:
            _append_tool_trace(
                self.db,
                job,
                RuntimeEventType.TOOL_QUEUED,
                {
                    "status": job.status,
                    "attempts": job.attempts,
                    "maxAttempts": job.max_attempts,
                    "dependsOnJobId": job.depends_on_job_id,
                },
            )
        return jobs

    def _find_or_create(self, kind: str, report_id: int, depends_on_job_id: int | None = None) -> ToolJob:
        existing = (
            self.db.query(ToolJob)
            .filter(ToolJob.report_id == report_id, ToolJob.kind == kind)
            .filter(ToolJob.status.in_([ToolJobStatus.PENDING.value, ToolJobStatus.RUNNING.value, ToolJobStatus.SUCCESS.value]))
            .first()
        )
        if existing is not None:
            return existing
        job = ToolJob(
            report_id=report_id,
            kind=kind,
            status=ToolJobStatus.PENDING.value,
            attempts=0,
            max_attempts=self._max_attempts_for(kind),
            depends_on_job_id=depends_on_job_id,
            run_after=datetime.utcnow(),
            last_error="",
        )
        self.db.add(job)
        self.db.flush()
        return job

    def _max_attempts_for(self, kind: str) -> int:
        policy = ToolPolicyRegistry.policy_for(kind)
        policy_limit = policy.max_attempts if policy is not None and policy.retryable else 1
        return max(1, min(self.settings.tool_queue_max_attempts, policy_limit))


class RateLimiter:
    def __init__(self, limit_per_minute: int):
        self.limit = max(0, limit_per_minute)
        self.events: deque[float] = deque()
        self.lock = threading.Lock()

    def allow(self) -> tuple[bool, float]:
        if self.limit <= 0:
            return True, 0.0
        now_ts = time.monotonic()
        with self.lock:
            while self.events and now_ts - self.events[0] >= 60.0:
                self.events.popleft()
            if len(self.events) < self.limit:
                self.events.append(now_ts)
                return True, 0.0
            retry_after = max(1.0, 60.0 - (now_ts - self.events[0]))
            return False, retry_after


class ToolQueueWorker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.stop_event = threading.Event()
        self.dispatcher: threading.Thread | None = None
        self.excel_executor = ThreadPoolExecutor(
            max_workers=max(1, settings.tool_queue_excel_workers),
            thread_name_prefix="mindbridge-excel",
        )
        self.email_executor = ThreadPoolExecutor(
            max_workers=max(1, settings.tool_queue_email_workers),
            thread_name_prefix="mindbridge-email",
        )
        self.email_limiter = RateLimiter(settings.alert_email_rate_limit_per_minute)

    def start(self) -> None:
        if not self.settings.tool_queue_enabled or self.dispatcher is not None:
            return
        self._recover_running_jobs()
        self.dispatcher = threading.Thread(target=self._loop, name="mindbridge-tool-dispatcher", daemon=True)
        self.dispatcher.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.dispatcher is not None:
            self.dispatcher.join(timeout=5)
        self.excel_executor.shutdown(wait=False, cancel_futures=True)
        self.email_executor.shutdown(wait=False, cancel_futures=True)

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._dispatch_once()
            except Exception:
                logger.exception("Tool queue dispatch failed")
            self.stop_event.wait(self.settings.tool_queue_poll_interval_seconds)

    def _dispatch_once(self) -> None:
        db = SessionLocal()
        try:
            now = datetime.utcnow()
            jobs = (
                db.query(ToolJob)
                .filter(ToolJob.status == ToolJobStatus.PENDING.value, ToolJob.run_after <= now)
                .order_by(ToolJob.created_at.asc())
                .limit(self.settings.tool_queue_batch_size)
                .all()
            )
            for job in jobs:
                if not self._claim(db, job.id):
                    continue
                executor = self._executor_for(job)
                executor.submit(self._run_job, job.id)
        finally:
            db.close()

    def _executor_for(self, job: ToolJob) -> ThreadPoolExecutor:
        if job.kind in {ToolJobKind.EXCEL_REPORT.value, ToolJobKind.CASE_CREATE.value}:
            return self.excel_executor
        return self.email_executor

    def _run_job(self, job_id: int) -> None:
        db = SessionLocal()
        try:
            job = db.get(ToolJob, job_id)
            if job is None or job.status != ToolJobStatus.RUNNING.value:
                return
            dependency = self._dependency_status(db, job)
            if dependency == ToolJobStatus.DEAD.value:
                job.attempts = job.max_attempts
                db.add(job)
                db.commit()
                raise RuntimeError(f"dependency job {job.depends_on_job_id} is dead")
            if not self._dependency_ready(db, job):
                self._requeue(db, job, self._dependency_wait_reason(job), 2.0)
                return
            if job.kind in {ToolJobKind.RISK_ALERT.value, ToolJobKind.ALERT_SEND.value}:
                allowed, retry_after = self.email_limiter.allow()
                if not allowed:
                    self._requeue(db, job, "邮件预警限流中，稍后重试", retry_after)
                    return
            job.attempts += 1
            job.updated_at = datetime.utcnow()
            db.add(job)
            db.commit()
            self._execute(db, job)
            job.status = ToolJobStatus.SUCCESS.value
            job.last_error = ""
            job.updated_at = datetime.utcnow()
            db.add(job)
            db.commit()
            _append_tool_trace(
                db,
                job,
                RuntimeEventType.TOOL_COMPLETED,
                {"status": job.status, "attempts": job.attempts},
            )
        except Exception as exc:
            try:
                self._fail_or_dead_letter(db, job_id, exc)
            except Exception:
                logger.exception("Failed to record tool job failure")
        finally:
            db.close()

    def _execute(self, db: Session, job: ToolJob) -> None:
        report = db.get(PsychologicalReport, job.report_id)
        if report is None:
            raise RuntimeError(f"report {job.report_id} not found")
        governance = ToolGovernanceService(db)
        audit = governance.start_job(job, report)
        governance.require_allowed(job, report)
        tools = ToolOrchestrationService(db, self.settings)
        registry = ToolExecutorRegistry(tools)
        policy = ToolPolicyRegistry.policy_for(job.kind)
        started_at = time.monotonic()
        try:
            registry.execute(job.kind, report=report)
            elapsed = time.monotonic() - started_at
            if policy is not None and elapsed > policy.timeout_seconds:
                raise TimeoutError(
                    f"tool {job.kind} exceeded execution SLA: {elapsed:.3f}s > {policy.timeout_seconds:.3f}s"
                )
        except Exception as exc:
            governance.finish(audit, "FAILED", f"{type(exc).__name__}: {exc}")
            raise
        governance.finish(audit, "SUCCESS", "tool execution completed")

    def _claim(self, db: Session, job_id: int) -> bool:
        """Atomically claim one pending job; safe across worker processes."""
        result = db.execute(
            update(ToolJob)
            .where(ToolJob.id == job_id, ToolJob.status == ToolJobStatus.PENDING.value)
            .values(status=ToolJobStatus.RUNNING.value, updated_at=datetime.utcnow())
        )
        db.commit()
        if result.rowcount == 1:
            job = db.get(ToolJob, job_id)
            if job is not None:
                _append_tool_trace(
                    db,
                    job,
                    RuntimeEventType.TOOL_STARTED,
                    {"status": job.status, "attempt": job.attempts + 1},
                )
        return result.rowcount == 1

    def _dependency_status(self, db: Session, job: ToolJob) -> str | None:
        if not job.depends_on_job_id:
            return None
        dependency = db.get(ToolJob, job.depends_on_job_id)
        return dependency.status if dependency is not None else ToolJobStatus.DEAD.value

    def _dependency_ready(self, db: Session, job: ToolJob) -> bool:
        if job.kind not in {ToolJobKind.RISK_ALERT.value, ToolJobKind.ALERT_SEND.value}:
            return True
        if job.depends_on_job_id:
            dependency = db.get(ToolJob, job.depends_on_job_id)
            return dependency is not None and dependency.status == ToolJobStatus.SUCCESS.value
        if job.kind == ToolJobKind.ALERT_SEND.value:
            from app.models.entities import RiskCase

            return db.query(RiskCase).filter(RiskCase.report_id == job.report_id).first() is not None
        return (
            db.query(ExcelRecord)
            .filter(ExcelRecord.report_id == job.report_id, ExcelRecord.status == ToolStatus.SUCCESS.value)
            .first()
            is not None
        )

    def _dependency_wait_reason(self, job: ToolJob) -> str:
        if job.kind == ToolJobKind.ALERT_SEND.value:
            return "等待风险个案创建成功后再发送预警"
        return "等待 Excel 台账写入成功后再发送预警"

    def _requeue(self, db: Session, job: ToolJob, reason: str, delay_seconds: float) -> None:
        job.status = ToolJobStatus.PENDING.value
        job.last_error = reason
        job.run_after = datetime.utcnow() + timedelta(seconds=max(1.0, delay_seconds))
        job.updated_at = datetime.utcnow()
        db.add(job)
        db.commit()
        _append_tool_trace(
            db,
            job,
            RuntimeEventType.TOOL_REQUEUED,
            {
                "status": job.status,
                "attempts": job.attempts,
                "retryAfterSeconds": max(1.0, delay_seconds),
                "reasonCode": "DEPENDENCY_OR_RATE_LIMIT",
            },
        )

    def _fail_or_dead_letter(self, db: Session, job_id: int, exc: Exception) -> None:
        job = db.get(ToolJob, job_id)
        if job is None:
            return
        message = f"{type(exc).__name__}: {exc}"
        job.last_error = message
        job.updated_at = datetime.utcnow()
        policy = ToolPolicyRegistry.policy_for(job.kind)
        if policy is not None and not policy.retryable:
            job.attempts = job.max_attempts
        if job.attempts >= job.max_attempts:
            job.status = ToolJobStatus.DEAD.value
            db.add(
                DeadLetterRecord(
                    job_id=job.id,
                    report_id=job.report_id,
                    operational_alert_id=job.operational_alert_id,
                    kind=job.kind,
                    reason=message,
                    payload=json.dumps(
                        {
                            "reportId": job.report_id,
                            "operationalAlertId": job.operational_alert_id,
                            "kind": job.kind,
                            "attempts": job.attempts,
                            "jobPayload": _safe_json_object(job.payload),
                        },
                        ensure_ascii=False,
                    ),
                )
            )
        else:
            job.status = ToolJobStatus.PENDING.value
            delay = min(
                self.settings.tool_queue_retry_max_delay_seconds,
                self.settings.tool_queue_retry_delay_seconds * (2 ** max(0, job.attempts - 1)),
            )
            job.run_after = datetime.utcnow() + timedelta(seconds=delay)
        db.add(job)
        db.commit()
        if job.status == ToolJobStatus.DEAD.value:
            _append_tool_trace(
                db,
                job,
                RuntimeEventType.TOOL_DEAD_LETTERED,
                {
                    "status": job.status,
                    "attempts": job.attempts,
                    "maxAttempts": job.max_attempts,
                    "errorType": type(exc).__name__,
                },
            )
        else:
            _append_tool_trace(
                db,
                job,
                RuntimeEventType.TOOL_REQUEUED,
                {
                    "status": job.status,
                    "attempts": job.attempts,
                    "maxAttempts": job.max_attempts,
                    "retryAfterSeconds": delay,
                    "reasonCode": "EXECUTION_FAILED",
                    "errorType": type(exc).__name__,
                },
            )

    def _recover_running_jobs(self) -> None:
        db = SessionLocal()
        try:
            rows = db.query(ToolJob).filter(ToolJob.status == ToolJobStatus.RUNNING.value).all()
            for job in rows:
                job.status = ToolJobStatus.PENDING.value
                job.last_error = "服务重启后恢复未完成任务"
                job.run_after = datetime.utcnow()
                job.updated_at = datetime.utcnow()
                db.add(job)
            db.commit()
        finally:
            db.close()


_worker: ToolQueueWorker | None = None


def _append_tool_trace(
    db: Session,
    job: ToolJob,
    event_type: RuntimeEventType,
    payload: dict,
) -> None:
    if job.report_id is None:
        return
    try:
        plan_status, job_summary = _tool_plan_status(db, job.report_id)
        AgentTraceService(db).append_report_event(
            job.report_id,
            event_type,
            "ToolQueueWorker",
            {
                "jobId": job.id,
                "tool": job.kind,
                "planStatus": plan_status,
                "jobSummary": job_summary,
                **payload,
            },
        )
    except Exception:
        db.rollback()
        logger.exception("Failed to append tool event to Agent Trace for job_id=%s", job.id)


def _safe_json_object(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {"value": value}
    except (TypeError, ValueError):
        return {"unparseable": True}


def _tool_plan_status(db: Session, report_id: int) -> tuple[str, dict[str, int]]:
    jobs = db.query(ToolJob).filter(ToolJob.report_id == report_id).all()
    counts = Counter(job.status for job in jobs)
    summary = {
        "total": len(jobs),
        "pending": counts[ToolJobStatus.PENDING.value],
        "running": counts[ToolJobStatus.RUNNING.value],
        "completed": counts[ToolJobStatus.SUCCESS.value],
        "failed": counts[ToolJobStatus.DEAD.value],
    }
    if not jobs:
        return "PENDING", summary
    if summary["failed"]:
        return "FAILED", summary
    if summary["completed"] == summary["total"]:
        return "COMPLETED", summary
    if summary["running"]:
        return "RUNNING", summary
    if any(job.attempts > 0 for job in jobs if job.status == ToolJobStatus.PENDING.value):
        return "RETRYING", summary
    if summary["completed"]:
        return "IN_PROGRESS", summary
    return "QUEUED", summary


def get_tool_queue_worker(settings: Settings) -> ToolQueueWorker:
    global _worker
    if _worker is None:
        _worker = ToolQueueWorker(settings)
    return _worker
