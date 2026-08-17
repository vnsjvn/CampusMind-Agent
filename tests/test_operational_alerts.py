import unittest
import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.core.database import Base
from app.models.entities import AgentRunTrace
from app.services.operational_alerts import OperationalAlertMonitor, OperationalAlertService


class OperationalAlertServiceTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()
        self.service = OperationalAlertService(self.db)

    def tearDown(self):
        self.db.close()

    def test_alerts_are_idempotently_updated_and_resolved(self):
        critical = {
            "status": "CRITICAL",
            "alerts": [
                {"code": "PLAN_RETRY_RATE_HIGH", "severity": "WARNING", "observedRate": 0.2, "thresholdRate": 0.1},
                {"code": "PLAN_FAILURE_RATE_HIGH", "severity": "CRITICAL", "observedRate": 0.08, "thresholdRate": 0.05},
            ],
        }

        first = self.service.synchronize_planning_health(critical)
        second = self.service.synchronize_planning_health(critical)

        self.assertEqual(len(first), 2)
        self.assertEqual(len(second), 2)
        self.assertTrue(all(item["status"] == "OPEN" for item in second))
        self.assertTrue(all(item["occurrenceCount"] == 2 for item in second))

        resolved = self.service.synchronize_planning_health({"status": "HEALTHY", "alerts": []})
        self.assertTrue(all(item["status"] == "RESOLVED" for item in resolved))
        self.assertTrue(all(item["resolvedAt"] is not None for item in resolved))

    def test_insufficient_data_does_not_resolve_an_open_alert(self):
        self.service.synchronize_planning_health({
            "status": "WARNING",
            "alerts": [
                {"code": "PLAN_RETRY_RATE_HIGH", "severity": "WARNING", "observedRate": 0.2, "thresholdRate": 0.1},
            ],
        })

        alerts = self.service.synchronize_planning_health({"status": "INSUFFICIENT_DATA", "alerts": []})

        self.assertEqual(alerts[0]["status"], "OPEN")
        self.assertEqual(alerts[0]["occurrenceCount"], 1)

    def test_monitor_evaluates_trace_metrics_and_persists_alert(self):
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine)
        db = factory()
        db.add(AgentRunTrace(
            user_id=1,
            session_id=1,
            report_id=None,
            intent="CONSULT",
            risk_level="LOW",
            original_input="input",
            sanitized_input="input",
            memory_brief="",
            agent_steps_json="[]",
            runtime_events_json=json.dumps([
                {"type": "PLAN_STEP_FAILED", "producer": "KnowledgeAgent", "payload": {
                    "stepId": "retrieval", "attempt": 2, "maxAttempts": 2,
                }},
            ]),
            retrieved_knowledge_json="[]",
            response_messages_json="[]",
            assessment_json="{}",
        ))
        db.commit()
        db.close()
        settings = Settings(
            _env_file=None,
            operational_alert_monitor_trace_limit=50,
            trace_planning_min_sample_size=1,
            trace_planning_failure_critical_rate=0.5,
        )

        result = OperationalAlertMonitor(settings, factory).run_once()

        check = factory()
        try:
            alerts = OperationalAlertService(check).list_alerts()
            self.assertTrue(result["executed"])
            self.assertEqual(result["healthStatus"], "CRITICAL")
            self.assertEqual(alerts[0]["code"], "PLAN_FAILURE_RATE_HIGH")
            self.assertEqual(alerts[0]["status"], "OPEN")
        finally:
            check.close()


if __name__ == "__main__":
    unittest.main()
