
from __future__ import annotations

import json
import logging
from datetime import datetime
from importlib import import_module
from typing import Protocol

from app.core.config import Settings
from sqlalchemy.orm import Session

from app.models.entities import ChatMessage, LongTermMemory
from app.schemas.dtos import AiMessage
from app.services.privacy import PrivacySanitizer


logger = logging.getLogger(__name__)


class MemoryCompactionSettings(Protocol):
    memory_compaction_enabled: bool
    memory_compaction_recent_messages: int
    memory_summary_max_chars: int


class RedisShortTermMemoryStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.privacy = PrivacySanitizer()
        self.client = self._connect()

    def load_recent(self, session_public_id: str) -> list[AiMessage]:
        if self.client is None:
            return []
        try:
            return self._read(session_public_id, self.settings.redis_memory_max_messages)
        except Exception as exc:
            logger.warning("Redis memory read unavailable: %s", exc)
            return []

    @property
    def available(self) -> bool:
        return self.client is not None

    def load_summary(self, session_public_id: str) -> str:
        if self.client is None:
            return ""
        try:
            return self.client.get(self._summary_key(session_public_id)) or ""
        except Exception as exc:
            logger.warning("Redis memory summary read unavailable: %s", exc)
            return ""

    def save_summary(self, session_public_id: str, summary: str) -> None:
        if self.client is None or not summary:
            return
        try:
            self.client.setex(
                self._summary_key(session_public_id),
                self.settings.redis_memory_ttl_seconds,
                self.privacy.sanitize(summary),
            )
        except Exception as exc:
            logger.warning("Redis memory summary write unavailable: %s", exc)

    def messages_from_rows(self, rows: list[ChatMessage]) -> list[AiMessage]:
        return [self._message_from_row(row) for row in rows]

    def append(self, session_public_id: str, role: str, content: str) -> None:
        if self.client is None:
            return
        key = self._key(session_public_id)
        payload = self._serialize(role, content)
        try:
            self.client.rpush(key, payload)
            self.client.ltrim(key, -self.settings.redis_memory_max_messages, -1)
            self.client.expire(key, self.settings.redis_memory_ttl_seconds)
        except Exception as exc:
            logger.warning("Redis memory append unavailable: %s", exc)

    def replace(self, session_public_id: str, messages: list[AiMessage]) -> None:
        if self.client is None:
            return
        key = self._key(session_public_id)
        pipe = self.client.pipeline()
        pipe.delete(key)
        if messages:
            pipe.rpush(key, *[self._serialize(message.role, message.content) for message in messages])
            pipe.ltrim(key, -self.settings.redis_memory_max_messages, -1)
            pipe.expire(key, self.settings.redis_memory_ttl_seconds)
        try:
            pipe.execute()
        except Exception as exc:
            logger.warning("Redis memory replace unavailable: %s", exc)

    def _read(self, session_public_id: str, limit: int) -> list[AiMessage]:
        raw_items = self.client.lrange(self._key(session_public_id), -limit, -1)
        messages = []
        for raw in raw_items:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            role = str(data.get("role", "")).lower()
            content = str(data.get("content", ""))
            if role and content:
                messages.append(AiMessage(role=role, content=self.privacy.sanitize(content)))
        return messages

    def _connect(self):
        try:
            redis_module = import_module("redis")
        except ModuleNotFoundError as exc:
            raise RuntimeError("请先安装 requirements.txt 中的 redis 依赖") from exc
        client = redis_module.Redis.from_url(
            self.settings.redis_url,
            decode_responses=True,
            socket_timeout=self.settings.redis_socket_timeout_seconds,
            socket_connect_timeout=self.settings.redis_socket_timeout_seconds,
        )
        try:
            client.ping()
        except Exception as exc:
            if self.settings.redis_memory_required:
                raise RuntimeError(f"Redis memory is required but unavailable: {exc}") from exc
            logger.warning("Redis memory disabled: %s", exc)
            return None
        return client

    def _message_from_row(self, row: ChatMessage) -> AiMessage:
        return AiMessage(role=row.role.lower(), content=self.privacy.sanitize(row.content))

    def _serialize(self, role: str, content: str) -> str:
        return json.dumps(
            {
                "role": role.lower(),
                "content": self.privacy.sanitize(content),
                "createdAt": datetime.utcnow().isoformat(),
            },
            ensure_ascii=False,
        )

    def _key(self, session_public_id: str) -> str:
        return f"mindbridge:short-term-memory:{session_public_id}"

    def _summary_key(self, session_public_id: str) -> str:
        return f"mindbridge:memory-summary:{session_public_id}"


class MySqlLongTermMemoryStore:
    """SQLAlchemy-backed durable memory (MySQL in production, SQLite in tests)."""

    def __init__(self, db: Session):
        self.db = db

    def load(self, user_id: int, session_id: int) -> LongTermMemory | None:
        return (
            self.db.query(LongTermMemory)
            .filter(LongTermMemory.user_id == user_id, LongTermMemory.session_id == session_id)
            .first()
        )

    def upsert(self, user_id: int, session_id: int, summary: str, source_message_count: int) -> LongTermMemory:
        row = self.load(user_id, session_id)
        if row is None:
            row = LongTermMemory(
                user_id=user_id,
                session_id=session_id,
                summary=summary,
                source_message_count=source_message_count,
                version=1,
            )
        else:
            row.summary = summary
            row.source_message_count = source_message_count
            row.version += 1
            row.updated_at = datetime.utcnow()
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)
        return row


class LayeredMemoryService:
    """Coordinates Redis recent context with durable SQL long-term memory."""

    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings
        self.short_term = RedisShortTermMemoryStore(settings)
        self.long_term = MySqlLongTermMemoryStore(db)

    def load(self, user_id: int, session_id: int, session_public_id: str) -> tuple[list[AiMessage], str, str]:
        history = self.short_term.load_recent(session_public_id)
        history_source = "redis"
        if not history:
            rows = (
                self.db.query(ChatMessage)
                .filter(ChatMessage.session_id == session_id, ChatMessage.user_id == user_id)
                .order_by(ChatMessage.created_at.desc())
                .limit(self.settings.redis_memory_max_messages)
                .all()
            )
            rows.reverse()
            history = self.short_term.messages_from_rows(rows)
            history_source = "mysql_fallback"
            if history:
                self.short_term.replace(session_public_id, history)

        summary = self.short_term.load_summary(session_public_id)
        summary_source = "redis"
        if not summary and self.settings.long_term_memory_enabled:
            row = self.long_term.load(user_id, session_id)
            summary = row.summary if row is not None else ""
            summary_source = "mysql"
            if summary:
                self.short_term.save_summary(session_public_id, summary)
        return history, summary, f"recent={history_source},summary={summary_source}"

    def persist_summary(
        self,
        user_id: int,
        session_id: int,
        session_public_id: str,
        summary: str,
        source_message_count: int,
    ) -> None:
        if not summary:
            return
        if self.settings.long_term_memory_enabled:
            self.long_term.upsert(user_id, session_id, summary, source_message_count)
        self.short_term.save_summary(session_public_id, summary)

    def append(self, session_public_id: str, role: str, content: str) -> None:
        self.short_term.append(session_public_id, role, content)


def compact_history_for_prompt(
    history: list[AiMessage],
    settings: MemoryCompactionSettings,
    current_input: str = "",
) -> tuple[list[AiMessage], str]:
    """Return bounded prompt history plus a student-safe memory brief.

    The summary is deterministic and avoids diagnostic labels. It is intended
    for prompt context and auditability, not for student-facing display.
    """

    sanitized = [AiMessage(role=item.role, content=PrivacySanitizer().sanitize(item.content)) for item in history]
    if not sanitized:
        return [], "无相关历史记忆。"

    recent_count = max(2, int(getattr(settings, "memory_compaction_recent_messages", 8)))
    max_chars = max(120, int(getattr(settings, "memory_summary_max_chars", 500)))
    brief = summarize_history_for_memory(sanitized, current_input, max_chars)

    if not getattr(settings, "memory_compaction_enabled", True) or len(sanitized) <= recent_count:
        return sanitized, brief

    recent = sanitized[-recent_count:]
    summary_message = AiMessage(
        role="system",
        content=(
            "历史摘要（仅供 MindBridge 内部上下文使用；不要向学生展示；"
            "不要据此输出诊断、风险等级或后台标签）：\n" + brief
        ),
    )
    return [summary_message, *recent], brief


def summarize_history_for_memory(history: list[AiMessage], current_input: str = "", max_chars: int = 500) -> str:
    privacy = PrivacySanitizer()
    user_points = []
    assistant_points = []
    for message in history:
        content = " ".join(privacy.sanitize(message.content).split())
        if not content:
            continue
        if message.role == "user":
            user_points.append(content)
        elif message.role == "assistant":
            assistant_points.append(content)

    parts = []
    if user_points:
        parts.append("学生近期关注：" + "；".join(_clip(item, 80) for item in user_points[-4:]))
    if assistant_points:
        parts.append("已给过的支持：" + "；".join(_clip(item, 70) for item in assistant_points[-3:]))
    if current_input:
        parts.append("本轮输入关注：" + _clip(privacy.sanitize(current_input), 80))
    if not parts:
        return "无相关历史记忆。"
    return _clip("\n".join(parts), max_chars)


def _clip(text: str, limit: int) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)] + "..."
