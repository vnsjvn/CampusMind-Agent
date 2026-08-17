import base64
import json
import os
import unittest
import uuid

import httpx
from sqlalchemy import text
from sqlalchemy.engine import make_url


@unittest.skipUnless(os.getenv("RUN_REAL_INTEGRATION") == "1", "set RUN_REAL_INTEGRATION=1 to test real services")
class RealDependencyIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.core.bootstrap import create_schema, seed_data
        from app.core.config import get_settings
        from app.core.database import SessionLocal

        cls.settings = get_settings()
        errors = []
        if make_url(cls.settings.database_url).get_backend_name() != "mysql":
            errors.append("DATABASE_URL must use MySQL")
        if cls.settings.ai_provider.lower() != "ollama":
            errors.append("AI_PROVIDER must be ollama")
        if not cls.settings.redis_memory_required:
            errors.append("REDIS_MEMORY_REQUIRED must be true")
        if not cls.settings.knowledge_vector_enabled or not cls.settings.knowledge_vector_required:
            errors.append("KNOWLEDGE_VECTOR_ENABLED and KNOWLEDGE_VECTOR_REQUIRED must both be true")
        if not cls.settings.openai_api_key:
            errors.append("OPENAI_API_KEY is required by the current Chroma embedding implementation")
        if errors:
            raise AssertionError("; ".join(errors))

        create_schema()
        db = SessionLocal()
        try:
            seed_data(db)
        finally:
            db.close()

    def test_mysql_connection_and_schema(self):
        from app.core.database import SessionLocal

        db = SessionLocal()
        try:
            self.assertEqual(db.execute(text("SELECT 1")).scalar_one(), 1)
            table = db.execute(text("SHOW TABLES LIKE 'long_term_memories'")).first()
            self.assertIsNotNone(table)
            runtime_events_column = db.execute(
                text("SHOW COLUMNS FROM agent_run_traces LIKE 'runtime_events_json'")
            ).first()
            self.assertIsNotNone(runtime_events_column)
            operational_alerts = db.execute(text("SHOW TABLES LIKE 'operational_alerts'")).first()
            self.assertIsNotNone(operational_alerts)
            tool_job_report = db.execute(text("SHOW COLUMNS FROM tool_jobs LIKE 'report_id'")).mappings().first()
            self.assertEqual(tool_job_report["Null"], "YES")
            self.assertIsNotNone(db.execute(text("SHOW COLUMNS FROM tool_jobs LIKE 'operational_alert_id'")).first())
            self.assertIsNotNone(db.execute(text("SHOW COLUMNS FROM tool_jobs LIKE 'payload'")).first())
        finally:
            db.close()

    def test_mysql_trace_metrics_query(self):
        from app.core.database import SessionLocal
        from app.services.report import ReportService

        db = SessionLocal()
        try:
            metrics = ReportService(db).agent_trace_metrics(limit=50)
            self.assertEqual(metrics["requestedLimit"], 50)
            self.assertIn("runtime", metrics["latencyMs"])
            self.assertIn("completion", metrics["tokens"])
            self.assertIn("successRate", metrics["tools"])
            self.assertIn("retryRunRate", metrics["planning"])
            self.assertIn("failureRunRate", metrics["planning"])
            self.assertIn("byStep", metrics["planning"])
            self.assertLessEqual(metrics["windowSize"], 50)
        finally:
            db.close()

    def test_mysql_response_review_trace(self):
        from app.agents.events import RuntimeEventType
        from app.core.database import SessionLocal
        from app.core.enums import IntentType, RiskLevel
        from app.models.entities import AgentRunTrace, ChatSession, UserAccount
        from app.services.response_review import ResponseReviewer
        from app.services.skills import MindBridgeSkillLibrary
        from app.services.trace import AgentTraceService

        marker = uuid.uuid4().hex
        db = SessionLocal()
        user_id = session_id = trace_id = None
        try:
            user = UserAccount(username=f"review-{marker}", display_name="Review Integration", password_hash="x")
            db.add(user)
            db.flush()
            session = ChatSession(public_id=f"review-{marker}", title="review", user_id=user.id)
            db.add(session)
            db.flush()
            trace = AgentRunTrace(
                user_id=user.id, session_id=session.id, report_id=None, intent="CHAT", risk_level="LOW",
                original_input="review", sanitized_input="review", memory_brief="", agent_steps_json="[]",
                runtime_events_json="[]", retrieved_knowledge_json="[]", response_messages_json="[]",
                assessment_json="{}",
            )
            db.add(trace)
            db.commit()
            user_id, session_id, trace_id = user.id, session.id, trace.id
            skills = MindBridgeSkillLibrary.response_skills(IntentType.CHAT, RiskLevel.LOW, "hello")
            review = ResponseReviewer.review("safe response " * 5, skills, RiskLevel.LOW)
            AgentTraceService(db).append_event(
                trace.id, RuntimeEventType.RESPONSE_REVIEWED, "ResponseReviewer", session.public_id,
                review.to_payload(),
            )
            db.refresh(trace)
            events = json.loads(trace.runtime_events_json or "[]")
            self.assertEqual(events[-1]["type"], "RESPONSE_REVIEWED")
            self.assertTrue(events[-1]["payload"]["valid"])
        finally:
            db.rollback()
            if trace_id is not None:
                db.query(AgentRunTrace).filter(AgentRunTrace.id == trace_id).delete(synchronize_session=False)
            if session_id is not None:
                db.query(ChatSession).filter(ChatSession.id == session_id).delete(synchronize_session=False)
            if user_id is not None:
                db.query(UserAccount).filter(UserAccount.id == user_id).delete(synchronize_session=False)
            db.commit()
            db.close()

    def test_mysql_response_review_escalation_is_idempotent(self):
        from app.core.database import SessionLocal
        from app.models.entities import ToolAuditRecord
        from app.services.response_review import ResponseReview
        from app.services.review_escalation import ResponseReviewEscalationService
        from app.services.report import ReportService

        report_marker = 1_500_000_000 + int(uuid.uuid4().hex[:7], 16)
        db = SessionLocal()
        record_id = None
        try:
            review = ResponseReview(
                valid=False,
                decision="FLAG",
                requires_escalation=True,
                finding_codes=("MISSING_REQUIRED_TERM:high_risk_safety_plan",),
                checked_skills=("high_risk_safety_plan@1.0.0",),
            )
            service = ResponseReviewEscalationService(db)
            first = service.create_if_required(report_marker, None, "integration-session", review)
            second = service.create_if_required(report_marker, None, "integration-session", review)
            record_id = first.id

            self.assertEqual(first.id, second.id)
            self.assertEqual(first.status, "PENDING_REVIEW")
            self.assertEqual(
                db.query(ToolAuditRecord).filter(
                    ToolAuditRecord.report_id == report_marker,
                    ToolAuditRecord.tool_name == "HUMAN_RESPONSE_REVIEW",
                ).count(),
                1,
            )
            resolved = service.resolve(
                first.id,
                "APPROVED",
                "integration-admin",
                "integration checked",
                self.settings.mcp_staff_approval_token,
                self.settings,
            )
            repeated = service.resolve(
                first.id,
                "APPROVED",
                "integration-admin",
                "integration checked",
                self.settings.mcp_staff_approval_token,
                self.settings,
            )
            self.assertEqual(resolved.status, "REVIEW_APPROVED")
            self.assertEqual(resolved.id, repeated.id)
            approved_page = ReportService(db).response_reviews("REVIEW_APPROVED", limit=100, offset=0)
            self.assertTrue(any(item["id"] == resolved.id for item in approved_page["items"]))
        finally:
            db.rollback()
            if record_id is not None:
                db.query(ToolAuditRecord).filter(ToolAuditRecord.id == record_id).delete(synchronize_session=False)
            db.commit()
            db.close()

    def test_redis_real_round_trip(self):
        import redis

        client = redis.Redis.from_url(self.settings.redis_url, decode_responses=True)
        key = f"mindbridge:integration-probe:{uuid.uuid4().hex}"
        try:
            self.assertTrue(client.ping())
            client.setex(key, 30, "ok")
            self.assertEqual(client.get(key), "ok")
        finally:
            client.delete(key)
            client.close()

    def test_mysql_runtime_failure_trace(self):
        from app.core.database import SessionLocal
        from app.models.entities import AgentRunTrace, ChatSession, UserAccount
        from app.services.trace import AgentTraceService

        marker = uuid.uuid4().hex
        db = SessionLocal()
        trace_id = session_id = user_id = None
        try:
            user = UserAccount(
                username=f"runtime-failure-{marker}",
                display_name="Runtime Failure Integration",
                password_hash="integration-only",
            )
            db.add(user)
            db.flush()
            session = ChatSession(public_id=f"runtime-failure-{marker}", title="failure trace", user_id=user.id)
            db.add(session)
            db.commit()
            db.refresh(user)
            db.refresh(session)
            user_id, session_id = user.id, session.id

            trace = AgentTraceService(db).save_failed_run(
                user,
                session,
                "real mysql failure trace",
                "real mysql failure trace",
                TimeoutError("must not be persisted"),
            )
            trace_id = trace.id
            events = json.loads(trace.runtime_events_json or "[]")
            self.assertEqual(events[-1]["type"], "RUN_FAILED")
            self.assertTrue(events[-1]["payload"]["retryable"])
            self.assertNotIn("must not be persisted", trace.runtime_events_json)
        finally:
            db.rollback()
            if trace_id is not None:
                db.query(AgentRunTrace).filter(AgentRunTrace.id == trace_id).delete(synchronize_session=False)
            if session_id is not None:
                db.query(ChatSession).filter(ChatSession.id == session_id).delete(synchronize_session=False)
            if user_id is not None:
                db.query(UserAccount).filter(UserAccount.id == user_id).delete(synchronize_session=False)
            db.commit()
            db.close()

    def test_mysql_tool_lifecycle_is_linked_to_trace(self):
        from datetime import datetime

        from app.core.database import SessionLocal
        from app.core.enums import ToolJobStatus
        from app.models.entities import AgentRunTrace, ChatSession, DeadLetterRecord, ToolJob, UserAccount
        from app.services.tool_queue import ToolQueueWorker

        marker = uuid.uuid4().hex
        report_marker = 1_000_000_000 + int(marker[:7], 16)
        db = SessionLocal()
        user_id = session_id = trace_id = job_id = None
        worker = ToolQueueWorker(self.settings)
        try:
            user = UserAccount(username=f"tool-trace-{marker}", display_name="Tool Trace Integration", password_hash="x")
            db.add(user)
            db.flush()
            session = ChatSession(public_id=f"tool-trace-{marker}", title="tool trace", user_id=user.id)
            db.add(session)
            db.flush()
            trace = AgentRunTrace(
                user_id=user.id, session_id=session.id, report_id=report_marker,
                intent="RISK", risk_level="HIGH", original_input="integration",
                sanitized_input="integration", memory_brief="", agent_steps_json="[]",
                runtime_events_json=json.dumps([{
                    "type": "RESPONSE_PLANNED", "producer": "CounselorAgent",
                    "payload": {"taskPlan": {"intent": "RISK", "steps": [
                        {"id": "response", "status": "COMPLETED"},
                        {"id": "tools", "status": "PENDING"},
                    ]}},
                }]), retrieved_knowledge_json="[]",
                response_messages_json="[]", assessment_json="{}",
            )
            job = ToolJob(
                report_id=report_marker, kind="EXCEL_REPORT", status=ToolJobStatus.PENDING.value,
                attempts=3, max_attempts=3, run_after=datetime.utcnow(),
            )
            db.add_all([trace, job])
            db.commit()
            user_id, session_id, trace_id, job_id = user.id, session.id, trace.id, job.id

            self.assertTrue(worker._claim(db, job.id))
            worker._fail_or_dead_letter(db, job.id, RuntimeError("must not enter trace"))
            db.refresh(trace)
            events = json.loads(trace.runtime_events_json or "[]")
            self.assertEqual(
                [event["type"] for event in events],
                ["RESPONSE_PLANNED", "TOOL_STARTED", "TOOL_DEAD_LETTERED"],
            )
            tools_step = next(step for step in events[0]["payload"]["taskPlan"]["steps"] if step["id"] == "tools")
            self.assertEqual(tools_step["status"], "FAILED")
            self.assertFalse(events[0]["payload"]["planVerification"]["valid"])
            self.assertNotIn("must not enter trace", trace.runtime_events_json)
        finally:
            worker.stop()
            db.rollback()
            if job_id is not None:
                db.query(DeadLetterRecord).filter(DeadLetterRecord.job_id == job_id).delete(synchronize_session=False)
                db.query(ToolJob).filter(ToolJob.id == job_id).delete(synchronize_session=False)
            if trace_id is not None:
                db.query(AgentRunTrace).filter(AgentRunTrace.id == trace_id).delete(synchronize_session=False)
            if session_id is not None:
                db.query(ChatSession).filter(ChatSession.id == session_id).delete(synchronize_session=False)
            if user_id is not None:
                db.query(UserAccount).filter(UserAccount.id == user_id).delete(synchronize_session=False)
            db.commit()
            db.close()

    def test_mysql_trace_contains_context_budget_snapshot(self):
        import redis

        from app.agents.factory import create_agent_runtime
        from app.core.database import SessionLocal
        from app.models.entities import AgentRunTrace, ChatSession, LongTermMemory, UserAccount
        from app.services.trace import AgentTraceService

        marker = uuid.uuid4().hex
        public_id = f"trace-latency-{marker}"
        db = SessionLocal()
        user_id = session_id = trace_id = None
        try:
            user = UserAccount(
                username=f"trace-latency-{marker}",
                display_name="Trace Latency Integration",
                password_hash="integration-only",
            )
            db.add(user)
            db.flush()
            user_id = user.id
            session = ChatSession(public_id=public_id, title="trace latency", user_id=user.id)
            db.add(session)
            db.commit()
            session_id = session.id
            run = create_agent_runtime(db, self.settings).run(
                user,
                session,
                "请解释 Python 函数",
                "请解释 Python 函数",
            )
            trace = AgentTraceService(db).save_run(
                user=user,
                session=session,
                original_input="请解释 Python 函数",
                sanitized_input="请解释 Python 函数",
                memory_brief=run.memory_brief,
                agent_run=run,
                report_id=None,
            )
            trace_id = trace.id
            events = json.loads(trace.runtime_events_json or "[]")
            response_event = next(item for item in events if item["type"] == "RESPONSE_PLANNED")
            payload = response_event["payload"]
            self.assertIn("budget", payload)
            self.assertIn("knowledge", payload)
            self.assertIn("skills", payload)
            self.assertTrue(payload["hasPlannerState"])
            self.assertEqual(payload["taskPlan"]["intent"], "CHAT")
            self.assertTrue(all(step["status"] == "COMPLETED" for step in payload["taskPlan"]["steps"]))
            self.assertTrue(payload["planVerification"]["valid"])
            self.assertLessEqual(payload["budget"]["used"], payload["budget"]["limit"])
            self.assertGreaterEqual(response_event["payload"]["durationMs"], 0)
            completed = next(item for item in events if item["type"] == "RUN_COMPLETED")
            self.assertGreaterEqual(completed["payload"]["totalDurationMs"], response_event["payload"]["durationMs"])
        finally:
            db.rollback()
            if trace_id is not None:
                db.query(AgentRunTrace).filter(AgentRunTrace.id == trace_id).delete(synchronize_session=False)
            if session_id is not None:
                db.query(LongTermMemory).filter(LongTermMemory.session_id == session_id).delete(synchronize_session=False)
                db.query(ChatSession).filter(ChatSession.id == session_id).delete(synchronize_session=False)
            if user_id is not None:
                db.query(UserAccount).filter(UserAccount.id == user_id).delete(synchronize_session=False)
            db.commit()
            db.close()
            client = redis.Redis.from_url(self.settings.redis_url, decode_responses=True)
            try:
                client.delete(
                    f"mindbridge:short-term-memory:{public_id}",
                    f"mindbridge:memory-summary:{public_id}",
                )
            finally:
                client.close()

    def test_real_mysql_staff_mcp_approval_flow(self):
        from app.core.database import SessionLocal
        from app.mcp_tools.server import mindbridge_alert_ack, mindbridge_case_note_add
        from app.models.entities import CaseNote, ChatSession, PsychologicalReport, RiskCase, UserAccount
        from app.services.tool_approval import StaffToolApproval

        approved, reason = StaffToolApproval.verify(
            self.settings,
            self.settings.mcp_staff_approval_token,
        )
        self.assertTrue(approved, reason)
        marker = uuid.uuid4().hex
        db = SessionLocal()
        user_id = session_id = report_id = case_id = None
        try:
            user = UserAccount(
                username=f"mcp-approval-{marker}",
                display_name="MCP Approval Integration",
                password_hash="integration-only",
            )
            db.add(user)
            db.flush()
            user_id = user.id
            session = ChatSession(public_id=f"mcp-{marker}", title="MCP approval integration", user_id=user.id)
            db.add(session)
            db.flush()
            session_id = session.id
            report = PsychologicalReport(
                user_id=user.id,
                session_id=session.id,
                content="integration test medium-risk report",
                intent="CONSULT",
                emotion="ANXIETY",
                emotion_score=2.0,
                risk_level="MEDIUM",
                confidence=0.9,
                summary="temporary MCP approval integration record",
            )
            db.add(report)
            db.flush()
            report_id = report.id
            case = RiskCase(
                report_id=report.id,
                risk_level="MEDIUM",
                status="OPEN",
                owner="integration-test",
                summary=report.summary,
                handoff_summary="temporary integration handoff",
            )
            db.add(case)
            db.commit()
            case_id = case.id

            ack_result = mindbridge_alert_ack(
                case_id,
                "integration-counselor",
                self.settings.mcp_staff_approval_token,
                "integration approval",
            )
            note_result = mindbridge_case_note_add(
                case_id,
                "integration-counselor",
                "integration follow-up note",
                self.settings.mcp_staff_approval_token,
            )

            db.rollback()
            persisted_case = db.get(RiskCase, case_id)
            notes = db.query(CaseNote).filter(CaseNote.case_id == case_id).all()
            self.assertIn("success", ack_result)
            self.assertIn("success", note_result)
            self.assertEqual(persisted_case.status, "ACKNOWLEDGED")
            self.assertEqual(persisted_case.acknowledged_by, "integration-counselor")
            self.assertTrue(any(note.note == "integration follow-up note" for note in notes))
        finally:
            db.rollback()
            if case_id is not None:
                db.query(CaseNote).filter(CaseNote.case_id == case_id).delete(synchronize_session=False)
                db.query(RiskCase).filter(RiskCase.id == case_id).delete(synchronize_session=False)
            if report_id is not None:
                db.query(PsychologicalReport).filter(PsychologicalReport.id == report_id).delete(synchronize_session=False)
            if session_id is not None:
                db.query(ChatSession).filter(ChatSession.id == session_id).delete(synchronize_session=False)
            if user_id is not None:
                db.query(UserAccount).filter(UserAccount.id == user_id).delete(synchronize_session=False)
            db.commit()
            db.close()

    def test_chroma_and_real_embedding(self):
        from app.services.vector_store import ChromaKnowledgeStore

        store = ChromaKnowledgeStore(self.settings)
        self.assertTrue(store.can_embed, store.error)
        embedding = store.embed_texts(["MindBridge 真实向量探针"])[0]
        self.assertGreater(len(embedding), 100)
        self.assertGreaterEqual(store.count(), 0)

    def test_ollama_model_is_installed_and_can_answer(self):
        tags = httpx.get(f"{self.settings.ollama_base_url}/api/tags", timeout=10)
        tags.raise_for_status()
        names = {item.get("name", "") for item in tags.json().get("models", [])}
        requested = self.settings.ollama_model
        self.assertTrue(requested in names or requested.split(":", 1)[0] in {name.split(":", 1)[0] for name in names})

        response = httpx.post(
            f"{self.settings.ollama_base_url}/api/chat",
            json={
                "model": requested,
                "messages": [{"role": "user", "content": "只回复 OK"}],
                "stream": False,
                "options": {"num_predict": 8, "temperature": 0},
            },
            timeout=120,
        )
        response.raise_for_status()
        self.assertTrue(response.json().get("message", {}).get("content", "").strip())

    def test_complete_fastapi_chat_request(self):
        from fastapi.testclient import TestClient

        from app.main import create_app

        token = base64.b64encode(b"student:student123").decode("ascii")
        with TestClient(create_app()) as client:
            response = client.post(
                "/api/chat/stream",
                headers={"Authorization": f"Basic {token}"},
                json={"message": "请只用一句话解释什么是 Python 函数。"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("event: meta", response.text)
        self.assertIn("event: token", response.text)
        self.assertIn("event: done", response.text)
        from app.core.database import SessionLocal
        from app.models.entities import AgentRunTrace

        db = SessionLocal()
        try:
            trace = (
                db.query(AgentRunTrace)
                .filter(AgentRunTrace.original_input == "请只用一句话解释什么是 Python 函数。")
                .order_by(AgentRunTrace.id.desc())
                .first()
            )
            self.assertIsNotNone(trace)
            events = json.loads(trace.runtime_events_json or "[]")
            response_event = next(item for item in events if item["type"] == "RESPONSE_PLANNED")
            self.assertIn("budget", response_event["payload"])
            self.assertIn("knowledge", response_event["payload"])
            self.assertIn("skills", response_event["payload"])
            generation_event = next(item for item in events if item["type"] == "LLM_STREAM_COMPLETED")
            generation = generation_event["payload"]
            self.assertEqual(generation["provider"], "ollama")
            self.assertEqual(generation["model"], self.settings.ollama_model)
            self.assertGreaterEqual(generation["modelFirstTokenMs"], 0)
            self.assertGreaterEqual(generation["modelGenerationMs"], generation["modelFirstTokenMs"])
            self.assertGreater(generation["promptTokens"], 0)
            self.assertGreater(generation["completionTokens"], 0)
            self.assertGreater(generation["outputCharacters"], 0)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
