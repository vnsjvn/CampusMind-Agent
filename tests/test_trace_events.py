import unittest
import time
import json
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agents.events import RuntimeEvent, RuntimeEventType
from app.agents.runtime import AgentRunResult
from app.core.database import Base
from app.core.enums import IntentType, RiskLevel
from app.models.entities import AgentRunTrace, ChatSession, UserAccount
from app.services.report import ReportService
from app.services.trace import AgentTraceService
from app.services.ai import AiStreamMetrics
from app.services.chat import stream_failure_payload
from app.agents.harness import MindBridgeAgentHarness
from app.core.config import Settings
from app.schemas.dtos import ChatRequest


class RuntimeEventTraceTests(unittest.TestCase):
    def test_trace_metrics_aggregate_latency_tokens_tools_and_failures(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            user = UserAccount(username="metrics-user", display_name="Metrics", password_hash="x")
            db.add(user)
            db.flush()
            session = ChatSession(public_id="metrics-session", title="metrics", user_id=user.id)
            db.add(session)
            db.flush()
            common = dict(
                user_id=user.id, session_id=session.id, report_id=None, intent="CHAT", risk_level="LOW",
                original_input="input", sanitized_input="input", memory_brief="", agent_steps_json="[]",
                retrieved_knowledge_json="[]", response_messages_json="[]", assessment_json="{}",
            )
            success_events = [
                {"type": "RESPONSE_PLANNED", "producer": "CompanionAgent", "payload": {"durationMs": 10}},
                {"type": "RUN_COMPLETED", "producer": "CoordinatorAgent", "payload": {"totalDurationMs": 20}},
                {"type": "LLM_STREAM_COMPLETED", "producer": "ChatService", "payload": {
                    "modelFirstTokenMs": 5, "outputDurationMs": 50, "promptTokens": 100, "completionTokens": 25,
                }},
                {"type": "TOOL_COMPLETED", "producer": "ToolQueueWorker", "payload": {}},
            ]
            failed_events = [
                {"type": "PLAN_STEP_RETRYING", "producer": "KnowledgeAgent", "payload": {
                    "stepId": "retrieval", "attempt": 1, "maxAttempts": 2, "errorType": "TimeoutError",
                }},
                {"type": "PLAN_STEP_FAILED", "producer": "KnowledgeAgent", "payload": {
                    "stepId": "retrieval", "attempt": 2, "maxAttempts": 2, "errorType": "TimeoutError",
                }},
                {"type": "RUN_FAILED", "producer": "AgentRuntime", "payload": {"failedStage": "KnowledgeAgent"}},
                {"type": "TOOL_DEAD_LETTERED", "producer": "ToolQueueWorker", "payload": {}},
            ]
            db.add_all([
                AgentRunTrace(**common, runtime_events_json=json.dumps(success_events)),
                AgentRunTrace(**common, runtime_events_json=json.dumps(failed_events)),
            ])
            db.commit()

            metrics = ReportService(db, Settings(
                _env_file=None,
                trace_planning_min_sample_size=2,
                trace_planning_retry_warning_rate=0.4,
                trace_planning_failure_critical_rate=0.4,
            )).agent_trace_metrics()

            self.assertEqual(metrics["windowSize"], 2)
            self.assertEqual(metrics["runs"]["endToEndCompleted"], 1)
            self.assertEqual(metrics["runs"]["failed"], 1)
            self.assertEqual(metrics["latencyMs"]["modelFirstToken"]["p95"], 5.0)
            self.assertEqual(metrics["tokens"]["completion"]["avg"], 25.0)
            self.assertEqual(metrics["tools"]["successRate"], 0.5)
            self.assertEqual(metrics["planning"]["retryEvents"], 1)
            self.assertEqual(metrics["planning"]["failedSteps"], 1)
            self.assertEqual(metrics["planning"]["retryRunRate"], 0.5)
            self.assertEqual(metrics["planning"]["failureRunRate"], 0.5)
            self.assertEqual(metrics["planning"]["byStep"]["retrieval"], {"retries": 1, "failures": 1})
            self.assertEqual(metrics["planning"]["byAgent"]["KnowledgeAgent"], {"retries": 1, "failures": 1})
            self.assertEqual(metrics["planning"]["health"]["status"], "CRITICAL")
            self.assertEqual(
                [item["code"] for item in metrics["planning"]["health"]["alerts"]],
                ["PLAN_RETRY_RATE_HIGH", "PLAN_FAILURE_RATE_HIGH"],
            )
            self.assertEqual(metrics["failureStages"], {"KnowledgeAgent": 1, "Plan:retrieval": 1})
        finally:
            db.close()

    def test_harness_persists_runtime_failure_and_reraises_original_error(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()

        class FailingRuntime:
            def run(self, *args, **kwargs):
                raise TimeoutError("private dependency details")

        try:
            user = UserAccount(username="harness-failure-user", display_name="Harness Failure", password_hash="x")
            db.add(user)
            db.commit()
            harness = MindBridgeAgentHarness(
                db,
                Settings(_env_file=None, ai_provider="mock", redis_enabled=False),
            )

            with patch("app.agents.harness.create_agent_runtime", return_value=FailingRuntime()):
                with self.assertRaises(TimeoutError):
                    harness.run(user, ChatRequest(message="runtime failure input"))

            response = ReportService(db).agent_run_traces()[0]
            self.assertEqual(response.runtimeEvents[-1]["type"], "RUN_FAILED")
            self.assertEqual(response.runtimeEvents[-1]["payload"]["errorType"], "TimeoutError")
        finally:
            db.close()

    def test_failed_runtime_is_persisted_without_exception_message(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            user = UserAccount(username="failed-trace-user", display_name="Failed Trace", password_hash="x")
            db.add(user)
            db.flush()
            session = ChatSession(public_id="failed-trace-session", title="failed", user_id=user.id)
            db.add(session)
            db.commit()

            trace = AgentTraceService(db).save_failed_run(
                user,
                session,
                "original private input",
                "sanitized input",
                TimeoutError("secret provider details"),
            )
            events = ReportService(db).agent_run_traces()[0].runtimeEvents
            failure = events[-1]

            self.assertEqual(trace.intent, "UNKNOWN")
            self.assertEqual(failure["type"], "RUN_FAILED")
            self.assertEqual(failure["payload"]["failedStage"], "RuntimeInitialization")
            self.assertTrue(failure["payload"]["retryable"])
            self.assertNotIn("secret provider details", trace.runtime_events_json)
        finally:
            db.close()

    def test_failure_payload_is_sanitized_and_marks_retryable_http_errors(self):
        class Response:
            status_code = 502

        class ProviderFailure(Exception):
            response = Response()

        payload = stream_failure_payload(
            ProviderFailure("secret upstream response"),
            AiStreamMetrics(provider="ollama", model="mindbridge", raw_chunks=2, raw_characters=12),
            time.monotonic(),
            5.0,
            1,
            ["partial"],
        )

        self.assertEqual(payload["errorType"], "ProviderFailure")
        self.assertEqual(payload["httpStatus"], 502)
        self.assertTrue(payload["retryable"])
        self.assertTrue(payload["partialOutput"])
        self.assertNotIn("secret upstream response", str(payload))

    def test_runtime_events_are_persisted_and_returned_by_admin_report_service(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            user = UserAccount(username="trace-user", display_name="Trace User", password_hash="x")
            db.add(user)
            db.flush()
            session = ChatSession(public_id="trace-session", title="trace", user_id=user.id)
            db.add(session)
            db.commit()
            event = RuntimeEvent(
                sequence=1,
                type=RuntimeEventType.USER_MESSAGE_RECEIVED,
                producer="LangGraphRuntime",
                correlation_id=session.public_id,
                payload={"budget": {"limit": 12000, "used": 800}},
            )
            run = AgentRunResult(
                intent=IntentType.CHAT,
                risk_level=RiskLevel.LOW,
                assessment=None,
                retrieved_knowledge=[],
                response_messages=[],
                steps=[],
                memory_brief="无相关历史记忆。",
                events=[event],
            )

            trace = AgentTraceService(db).save_run(
                user=user,
                session=session,
                original_input="你好",
                sanitized_input="你好",
                memory_brief=run.memory_brief,
                agent_run=run,
                report_id=None,
            )
            AgentTraceService(db).append_event(
                trace.id,
                RuntimeEventType.LLM_STREAM_COMPLETED,
                "ChatService",
                session.public_id,
                {"modelFirstTokenMs": 12.5, "completionTokens": 8},
            )
            response = ReportService(db).agent_run_traces()[0]

            self.assertIn("USER_MESSAGE_RECEIVED", trace.runtime_events_json)
            self.assertEqual(response.runtimeEvents[0]["type"], "USER_MESSAGE_RECEIVED")
            self.assertEqual(response.runtimeEvents[0]["correlation_id"], "trace-session")
            self.assertEqual(response.runtimeEvents[0]["payload"]["budget"]["used"], 800)
            self.assertEqual(response.runtimeEvents[1]["type"], "LLM_STREAM_COMPLETED")
            self.assertEqual(response.runtimeEvents[1]["payload"]["completionTokens"], 8)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
