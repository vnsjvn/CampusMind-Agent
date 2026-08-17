from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def now() -> datetime:
    return datetime.utcnow()


class UserAccount(Base):
    __tablename__ = "user_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(128))
    password_hash: Mapped[str] = mapped_column(String(128))
    roles_csv: Mapped[str] = mapped_column(String(256), default="ROLE_USER")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    sessions: Mapped[list["ChatSession"]] = relationship(back_populates="user")

    @property
    def roles(self) -> list[str]:
        return [role for role in self.roles_csv.split(",") if role]

    @roles.setter
    def roles(self, value: list[str] | set[str]) -> None:
        self.roles_csv = ",".join(sorted(value))


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(160))
    user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    user: Mapped[UserAccount] = relationship(back_populates="sessions")
    messages: Mapped[list["ChatMessage"]] = relationship(back_populates="session", cascade="all, delete-orphan")

    def touch(self) -> None:
        self.updated_at = now()


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id"))
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id"))
    role: Mapped[str] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    session: Mapped[ChatSession] = relationship(back_populates="messages")


class LongTermMemory(Base):
    """Durable, compact memory for a user's conversation session.

    Raw messages remain in ``chat_messages``. This table stores the bounded,
    prompt-ready representation so long conversations do not have to be
    replayed on every turn.
    """

    __tablename__ = "long_term_memories"
    __table_args__ = (UniqueConstraint("user_id", "session_id", name="uq_long_term_memory_user_session"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    source_message_count: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(256), index=True)
    source_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    embedding_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class PsychologicalReport(Base):
    __tablename__ = "psychological_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id"))
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id"))
    content: Mapped[str] = mapped_column(Text)
    intent: Mapped[str] = mapped_column(String(32))
    emotion: Mapped[str] = mapped_column(String(32))
    emotion_score: Mapped[float] = mapped_column(Float)
    risk_level: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[float] = mapped_column(Float)
    summary: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class RiskCase(Base):
    __tablename__ = "risk_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    risk_level: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    owner: Mapped[str] = mapped_column(String(128), default="unassigned")
    summary: Mapped[str] = mapped_column(Text)
    handoff_summary: Mapped[str] = mapped_column(Text, default="")
    acknowledged_by: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class CaseNote(Base):
    __tablename__ = "case_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(Integer, index=True)
    actor: Mapped[str] = mapped_column(String(128))
    note: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class AlertRecord(Base):
    __tablename__ = "alert_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(Integer, index=True)
    channel: Mapped[str] = mapped_column(String(64))
    recipient: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class OperationalAlert(Base):
    __tablename__ = "operational_alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    source: Mapped[str] = mapped_column(String(64), default="planning", index=True)
    severity: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), default="OPEN", index=True)
    observed_rate: Mapped[float] = mapped_column(Float, default=0.0)
    threshold_rate: Mapped[float] = mapped_column(Float, default=0.0)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class ExcelRecord(Base):
    __tablename__ = "excel_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(Integer, index=True)
    file_path: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ToolJob(Base):
    __tablename__ = "tool_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    operational_alert_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    depends_on_job_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    run_after: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class DeadLetterRecord(Base):
    __tablename__ = "dead_letter_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    report_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    operational_alert_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    reason: Mapped[str] = mapped_column(Text)
    payload: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class AgentRunTrace(Base):
    __tablename__ = "agent_run_traces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user_accounts.id"), index=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id"), index=True)
    report_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    intent: Mapped[str] = mapped_column(String(32), index=True)
    risk_level: Mapped[str] = mapped_column(String(32), default="LOW", index=True)
    original_input: Mapped[str] = mapped_column(Text)
    sanitized_input: Mapped[str] = mapped_column(Text)
    memory_brief: Mapped[str] = mapped_column(Text, default="")
    agent_steps_json: Mapped[str] = mapped_column(Text, default="[]")
    runtime_events_json: Mapped[Optional[str]] = mapped_column(Text, default="[]", nullable=True)
    retrieved_knowledge_json: Mapped[str] = mapped_column(Text, default="[]")
    response_messages_json: Mapped[str] = mapped_column(Text, default="[]")
    assessment_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ToolAuditRecord(Base):
    __tablename__ = "tool_audit_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    report_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    tool_name: Mapped[str] = mapped_column(String(64), index=True)
    policy: Mapped[str] = mapped_column(String(128), default="")
    allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)
