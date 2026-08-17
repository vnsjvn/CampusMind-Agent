import unittest
from importlib.util import find_spec

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.agents.events import RuntimeEventBus, RuntimeEventType
from app.core.config import Settings
from app.core.database import Base
from app.models.entities import ChatSession, UserAccount


class RuntimeEventBusTests(unittest.TestCase):
    def test_published_events_are_dispatched_fifo_and_can_chain(self):
        bus = RuntimeEventBus("session-1")

        bus.subscribe(
            RuntimeEventType.USER_MESSAGE_RECEIVED,
            lambda _: bus.publish(RuntimeEventType.MEMORY_LOADED, "MemoryAgent"),
        )
        bus.subscribe(
            RuntimeEventType.MEMORY_LOADED,
            lambda _: bus.publish(RuntimeEventType.RUN_COMPLETED, "CoordinatorAgent"),
        )
        bus.publish(RuntimeEventType.USER_MESSAGE_RECEIVED, "AgentRuntime")

        history = bus.dispatch(max_events=4)

        self.assertEqual(
            [event.type for event in history],
            [
                RuntimeEventType.USER_MESSAGE_RECEIVED,
                RuntimeEventType.MEMORY_LOADED,
                RuntimeEventType.RUN_COMPLETED,
            ],
        )
        self.assertEqual([event.sequence for event in history], [1, 2, 3])
        self.assertTrue(all(event.correlation_id == "session-1" for event in history))

    def test_event_limit_prevents_unbounded_feedback_loop(self):
        bus = RuntimeEventBus("session-2")
        bus.subscribe(
            RuntimeEventType.USER_MESSAGE_RECEIVED,
            lambda _: bus.publish(RuntimeEventType.USER_MESSAGE_RECEIVED, "LoopAgent"),
        )
        bus.publish(RuntimeEventType.USER_MESSAGE_RECEIVED, "AgentRuntime")

        with self.assertRaises(RuntimeError):
            bus.dispatch(max_events=3)


@unittest.skipUnless(find_spec("langgraph"), "langgraph is not installed")
class LangGraphRuntimeEventTests(unittest.TestCase):
    def test_active_langgraph_runtime_emits_the_shared_event_timeline(self):
        from app.agents.langgraph_runtime import LangGraphAgentRuntimeService

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        try:
            user = UserAccount(username="event-user", display_name="Event User", password_hash="x")
            db.add(user)
            db.flush()
            session = ChatSession(public_id="event-session", title="event test", user_id=user.id)
            db.add(session)
            db.commit()
            settings = Settings(
                _env_file=None,
                ai_provider="mock",
                agent_framework="langgraph",
                database_url="sqlite:///:memory:",
                knowledge_vector_enabled=False,
                redis_memory_required=False,
            )

            result = LangGraphAgentRuntimeService(db, settings).run(user, session, "你好", "你好")

            self.assertEqual(
                [event.type for event in result.events],
                [
                    RuntimeEventType.USER_MESSAGE_RECEIVED,
                    RuntimeEventType.MEMORY_LOADED,
                    RuntimeEventType.INTENT_ROUTED,
                    RuntimeEventType.RESPONSE_PLANNED,
                    RuntimeEventType.RUN_COMPLETED,
                ],
            )
            self.assertTrue(all(event.correlation_id == "event-session" for event in result.events))
            response_event = next(event for event in result.events if event.type == RuntimeEventType.RESPONSE_PLANNED)
            self.assertIn("budget", response_event.payload)
            self.assertEqual(response_event.payload["knowledge"]["count"], 0)
            self.assertFalse(response_event.payload["hasToolContext"])
            self.assertTrue(response_event.payload["hasPlannerState"])
            self.assertEqual(
                [step["status"] for step in response_event.payload["taskPlan"]["steps"]],
                ["COMPLETED", "COMPLETED", "COMPLETED"],
            )
            self.assertTrue(response_event.payload["planVerification"]["valid"])
            self.assertEqual(response_event.payload["planVerification"]["phase"], "RUNTIME")
            completed = result.events[-1]
            self.assertGreaterEqual(response_event.payload["durationMs"], 0)
            self.assertGreaterEqual(completed.payload["totalDurationMs"], response_event.payload["durationMs"])
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
