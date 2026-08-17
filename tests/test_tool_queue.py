import unittest
import json
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.core.enums import ToolJobStatus
from app.models.entities import AgentRunTrace, ChatSession, DeadLetterRecord, ToolJob, UserAccount
from app.services.tool_queue import ToolQueueService, ToolQueueWorker, _tool_plan_status


def worker_settings(**overrides):
    values = {
        "tool_queue_excel_workers": 1,
        "tool_queue_email_workers": 1,
        "alert_email_rate_limit_per_minute": 10,
        "tool_queue_retry_delay_seconds": 10.0,
        "tool_queue_retry_max_delay_seconds": 25.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class ToolQueueTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.worker = ToolQueueWorker(worker_settings())

    def tearDown(self):
        self.worker.stop()
        self.db.close()

    def add_job(self, attempts=0, max_attempts=3):
        job = ToolJob(
            report_id=1,
            kind="EXCEL_REPORT",
            status=ToolJobStatus.PENDING.value,
            attempts=attempts,
            max_attempts=max_attempts,
            run_after=datetime.utcnow(),
        )
        self.db.add(job)
        self.db.commit()
        return job

    def test_claim_is_atomic_and_only_succeeds_once(self):
        job = self.add_job()

        self.assertTrue(self.worker._claim(self.db, job.id))
        self.assertFalse(self.worker._claim(self.db, job.id))

    def test_tool_lifecycle_is_linked_to_agent_trace_by_report(self):
        user = UserAccount(username="tool-trace-user", display_name="Tool Trace", password_hash="x")
        self.db.add(user)
        self.db.flush()
        session = ChatSession(public_id="tool-trace-session", title="tool trace", user_id=user.id)
        self.db.add(session)
        self.db.flush()
        trace = AgentRunTrace(
            user_id=user.id,
            session_id=session.id,
            report_id=77,
            intent="RISK",
            risk_level="HIGH",
            original_input="input",
            sanitized_input="input",
            memory_brief="",
            agent_steps_json="[]",
            runtime_events_json=json.dumps([{
                "type": "RESPONSE_PLANNED",
                "producer": "CounselorAgent",
                "payload": {"taskPlan": {"intent": "RISK", "steps": [
                    {"id": "memory", "status": "COMPLETED"},
                    {"id": "route", "status": "COMPLETED"},
                    {"id": "retrieval", "status": "COMPLETED"},
                    {"id": "risk", "status": "COMPLETED"},
                    {"id": "response", "status": "COMPLETED"},
                    {"id": "tools", "status": "PENDING"},
                ]}},
            }]),
            retrieved_knowledge_json="[]",
            response_messages_json="[]",
            assessment_json="{}",
        )
        job = ToolJob(
            report_id=77,
            kind="EXCEL_REPORT",
            status=ToolJobStatus.PENDING.value,
            attempts=3,
            max_attempts=3,
            run_after=datetime.utcnow(),
        )
        self.db.add_all([trace, job])
        self.db.commit()

        self.assertTrue(self.worker._claim(self.db, job.id))
        self.worker._fail_or_dead_letter(self.db, job.id, RuntimeError("private tool details"))
        self.db.refresh(trace)
        events = json.loads(trace.runtime_events_json)

        self.assertEqual(
            [event["type"] for event in events],
            ["RESPONSE_PLANNED", "TOOL_STARTED", "TOOL_DEAD_LETTERED"],
        )
        self.assertEqual(events[-1]["payload"]["jobId"], job.id)
        tools_step = next(step for step in events[0]["payload"]["taskPlan"]["steps"] if step["id"] == "tools")
        self.assertEqual(tools_step["status"], "FAILED")
        self.assertEqual(tools_step["updatedByEvent"], "TOOL_DEAD_LETTERED")
        self.assertEqual(tools_step["jobSummary"]["failed"], 1)
        self.assertFalse(events[0]["payload"]["planVerification"]["valid"])
        self.assertEqual(events[0]["payload"]["planVerification"]["phase"], "TERMINAL")
        self.assertNotIn("private tool details", trace.runtime_events_json)

    def test_retry_uses_bounded_exponential_backoff(self):
        job = self.add_job(attempts=3, max_attempts=5)
        before = datetime.utcnow()

        self.worker._fail_or_dead_letter(self.db, job.id, RuntimeError("temporary"))
        self.db.refresh(job)

        self.assertEqual(job.status, ToolJobStatus.PENDING.value)
        self.assertGreaterEqual((job.run_after - before).total_seconds(), 24.0)
        self.assertLessEqual((job.run_after - before).total_seconds(), 26.0)

    def test_multi_job_plan_completes_only_after_every_job_succeeds(self):
        first = ToolJob(
            report_id=88, kind="EXCEL_REPORT", status=ToolJobStatus.SUCCESS.value,
            attempts=1, max_attempts=3, run_after=datetime.utcnow(),
        )
        second = ToolJob(
            report_id=88, kind="CASE_CREATE", status=ToolJobStatus.PENDING.value,
            attempts=0, max_attempts=3, run_after=datetime.utcnow(),
        )
        self.db.add_all([first, second])
        self.db.commit()

        status, summary = _tool_plan_status(self.db, 88)
        self.assertEqual(status, "IN_PROGRESS")
        self.assertEqual(summary["completed"], 1)

        second.attempts = 1
        self.db.commit()
        self.assertEqual(_tool_plan_status(self.db, 88)[0], "RETRYING")

        second.status = ToolJobStatus.SUCCESS.value
        self.db.commit()
        self.assertEqual(_tool_plan_status(self.db, 88)[0], "COMPLETED")

    def test_exhausted_job_is_written_to_dead_letter(self):
        job = self.add_job(attempts=3, max_attempts=3)

        self.worker._fail_or_dead_letter(self.db, job.id, RuntimeError("permanent"))

        self.db.refresh(job)
        dead = self.db.query(DeadLetterRecord).filter(DeadLetterRecord.job_id == job.id).one()
        self.assertEqual(job.status, ToolJobStatus.DEAD.value)
        self.assertIn("permanent", dead.reason)

    def test_queue_attempts_are_bounded_by_global_and_tool_policy(self):
        service = ToolQueueService(self.db, SimpleNamespace(tool_queue_max_attempts=4))

        self.assertEqual(service._max_attempts_for("ALERT_SEND"), 4)
        self.assertEqual(service._max_attempts_for("EXCEL_REPORT"), 3)
        self.assertEqual(service._max_attempts_for("CASE_NOTE_ADD"), 1)

    def test_non_retryable_policy_goes_directly_to_dead_letter(self):
        job = ToolJob(
            report_id=1,
            kind="CASE_NOTE_ADD",
            status=ToolJobStatus.RUNNING.value,
            attempts=0,
            max_attempts=3,
            run_after=datetime.utcnow(),
        )
        self.db.add(job)
        self.db.commit()

        self.worker._fail_or_dead_letter(self.db, job.id, RuntimeError("approval action failed"))

        self.db.refresh(job)
        self.assertEqual(job.status, ToolJobStatus.DEAD.value)
        self.assertEqual(job.attempts, job.max_attempts)


if __name__ == "__main__":
    unittest.main()
