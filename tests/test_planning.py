import unittest
from types import SimpleNamespace

from app.agents.runtime import AgentRuntimeService
from app.agents.events import RuntimeEventBus, RuntimeEventType
from app.core.enums import IntentType
from app.services.planning import TaskPlanVerifier, TaskPlanner


class TaskPlannerTests(unittest.TestCase):
    def test_chat_plan_uses_short_dependency_chain(self):
        plan = TaskPlanner.create(IntentType.CHAT)

        self.assertEqual([step.id for step in plan.steps], ["memory", "route", "response"])
        plan.require_ready("response")
        plan.complete("response")
        self.assertEqual(plan.to_dict()["steps"][-1]["status"], "COMPLETED")

    def test_consult_plan_rejects_out_of_order_execution(self):
        plan = TaskPlanner.create(IntentType.CONSULT)

        with self.assertRaises(RuntimeError):
            plan.require_ready("risk")
        plan.complete("retrieval")
        plan.complete("risk")
        plan.complete("response")
        self.assertIn("depends_on=response", plan.to_prompt())

    def test_step_lifecycle_records_running_and_failure_details(self):
        plan = TaskPlanner.create(IntentType.CONSULT)

        plan.start("retrieval")
        self.assertEqual(plan.to_dict()["steps"][2]["status"], "RUNNING")
        plan.fail("retrieval", RuntimeError("vector provider unavailable"))

        failed = plan.to_dict()["steps"][2]
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(failed["error"], "vector provider unavailable")
        with self.assertRaises(RuntimeError):
            plan.start("risk")

    def test_step_cannot_start_twice(self):
        plan = TaskPlanner.create(IntentType.CHAT)
        plan.start("response")

        with self.assertRaises(RuntimeError):
            plan.start("response")

    def test_failed_step_can_be_retried_and_counts_attempts(self):
        plan = TaskPlanner.create(IntentType.CONSULT)
        plan.start("retrieval")
        plan.fail("retrieval", "temporary failure")
        plan.retry("retrieval")
        plan.start("retrieval")
        plan.complete("retrieval")

        retried = plan.to_dict()["steps"][2]
        self.assertEqual(retried["status"], "COMPLETED")
        self.assertEqual(retried["attempts"], 2)
        self.assertIsNone(retried["error"])

    def test_runtime_retries_transient_agent_failure(self):
        runtime = AgentRuntimeService.__new__(AgentRuntimeService)
        runtime.settings = SimpleNamespace(agent_plan_max_attempts=2, agent_plan_retry_delay_seconds=0)
        context = SimpleNamespace(
            task_plan=TaskPlanner.create(IntentType.CHAT),
            context_snapshot={},
            event_bus=RuntimeEventBus("retry-session"),
        )
        calls = 0

        def flaky_agent(_step, _context):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutError("temporary model timeout")
            return True

        runtime._run_with_plan_retry(flaky_agent, 3, context, "CompanionAgent", "response", "rejected")
        context.task_plan.complete("response")

        response = context.task_plan.to_dict()["steps"][-1]
        self.assertEqual(calls, 2)
        self.assertEqual(response["attempts"], 2)
        self.assertEqual(response["status"], "COMPLETED")
        retry_event = context.event_bus.history[0]
        self.assertEqual(retry_event.type, RuntimeEventType.PLAN_STEP_RETRYING)
        self.assertEqual(retry_event.payload["stepId"], "response")
        self.assertEqual(retry_event.payload["attempt"], 1)
        self.assertEqual(retry_event.payload["errorType"], "TimeoutError")

    def test_runtime_emits_terminal_plan_failure(self):
        runtime = AgentRuntimeService.__new__(AgentRuntimeService)
        runtime.settings = SimpleNamespace(agent_plan_max_attempts=1, agent_plan_retry_delay_seconds=0)
        context = SimpleNamespace(
            task_plan=TaskPlanner.create(IntentType.CHAT),
            context_snapshot={},
            event_bus=RuntimeEventBus("failed-session"),
        )

        with self.assertRaises(TimeoutError):
            runtime._run_with_plan_retry(
                lambda _step, _context: (_ for _ in ()).throw(TimeoutError("model unavailable")),
                3, context, "CompanionAgent", "response", "rejected",
            )

        failure = context.event_bus.history[0]
        self.assertEqual(failure.type, RuntimeEventType.PLAN_STEP_FAILED)
        self.assertEqual(failure.payload["maxAttempts"], 1)
        self.assertEqual(failure.payload["error"], "model unavailable")

    def test_runtime_does_not_retry_validation_failure(self):
        runtime = AgentRuntimeService.__new__(AgentRuntimeService)
        runtime.settings = SimpleNamespace(agent_plan_max_attempts=3, agent_plan_retry_delay_seconds=0)
        context = SimpleNamespace(
            task_plan=TaskPlanner.create(IntentType.CHAT),
            context_snapshot={},
            event_bus=RuntimeEventBus("validation-session"),
        )
        calls = 0

        def invalid_agent(_step, _context):
            nonlocal calls
            calls += 1
            raise ValueError("invalid structured output")

        with self.assertRaises(ValueError):
            runtime._run_with_plan_retry(invalid_agent, 3, context, "CompanionAgent", "response", "rejected")

        self.assertEqual(calls, 1)
        failure = context.event_bus.history[0]
        self.assertFalse(failure.payload["retryable"])
        self.assertEqual(failure.payload["reasonCode"], "INVALID_EXECUTION_STATE")

    def test_verifier_rejects_cycle_and_missing_safety_dependency(self):
        invalid = {
            "intent": "RISK",
            "steps": [
                {"id": "memory", "dependsOn": ["route"], "status": "COMPLETED"},
                {"id": "route", "dependsOn": ["memory"], "status": "COMPLETED"},
                {"id": "retrieval", "dependsOn": ["route"], "status": "COMPLETED"},
                {"id": "risk", "dependsOn": [], "status": "COMPLETED"},
                {"id": "response", "dependsOn": [], "status": "COMPLETED"},
                {"id": "tools", "dependsOn": ["response"], "status": "PENDING"},
            ],
        }

        result = TaskPlanVerifier.verify_dict(invalid)

        self.assertFalse(result["valid"])
        self.assertTrue(any("cycle" in error for error in result["errors"]))
        self.assertTrue(any("risk must depend" in error for error in result["errors"]))


if __name__ == "__main__":
    unittest.main()
