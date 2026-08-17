import unittest
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.models.entities import ChatMessage, ChatSession, UserAccount
from app.services.memory import LayeredMemoryService, MySqlLongTermMemoryStore


class FakeShortTermStore:
    def __init__(self):
        self.messages = {}
        self.summaries = {}

    def load_recent(self, public_id):
        return list(self.messages.get(public_id, []))

    def messages_from_rows(self, rows):
        from app.schemas.dtos import AiMessage

        return [AiMessage(role=row.role, content=row.content) for row in rows]

    def replace(self, public_id, messages):
        self.messages[public_id] = list(messages)

    def load_summary(self, public_id):
        return self.summaries.get(public_id, "")

    def save_summary(self, public_id, summary):
        self.summaries[public_id] = summary

    def append(self, public_id, role, content):
        from app.schemas.dtos import AiMessage

        self.messages.setdefault(public_id, []).append(AiMessage(role=role, content=content))


class LayeredMemoryTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.user = UserAccount(username="memory-user", display_name="Memory User", password_hash="x")
        self.db.add(self.user)
        self.db.flush()
        self.session = ChatSession(public_id="session-1", title="test", user_id=self.user.id)
        self.db.add(self.session)
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def test_long_term_memory_upsert_is_one_row_and_versioned(self):
        store = MySqlLongTermMemoryStore(self.db)

        first = store.upsert(self.user.id, self.session.id, "first", 2)
        second = store.upsert(self.user.id, self.session.id, "second", 4)

        self.assertEqual(first.id, second.id)
        self.assertEqual(second.summary, "second")
        self.assertEqual(second.source_message_count, 4)
        self.assertEqual(second.version, 2)

    def test_load_seeds_redis_from_mysql_and_reads_durable_summary(self):
        self.db.add(ChatMessage(user_id=self.user.id, session_id=self.session.id, role="user", content="最近睡不好"))
        self.db.commit()
        MySqlLongTermMemoryStore(self.db).upsert(self.user.id, self.session.id, "学生近期睡眠困难", 1)
        settings = SimpleNamespace(
            redis_memory_max_messages=10,
            long_term_memory_enabled=True,
        )
        service = LayeredMemoryService.__new__(LayeredMemoryService)
        service.db = self.db
        service.settings = settings
        service.short_term = FakeShortTermStore()
        service.long_term = MySqlLongTermMemoryStore(self.db)

        history, summary, source = service.load(self.user.id, self.session.id, self.session.public_id)

        self.assertEqual([item.content for item in history], ["最近睡不好"])
        self.assertEqual(summary, "学生近期睡眠困难")
        self.assertEqual(source, "recent=mysql_fallback,summary=mysql")
        self.assertEqual(service.short_term.load_recent(self.session.public_id), history)
        self.assertEqual(service.short_term.load_summary(self.session.public_id), summary)


if __name__ == "__main__":
    unittest.main()
