from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import Base, engine
from app.core.security import hash_password
from app.models.entities import UserAccount
from app.services.knowledge import KnowledgeService


def create_schema() -> None:
    Base.metadata.create_all(bind=engine)
    ensure_schema_compatibility(engine)


def ensure_schema_compatibility(bind: Engine) -> None:
    """Apply small idempotent compatibility migrations until Alembic is introduced."""
    inspector = inspect(bind)
    if "agent_run_traces" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("agent_run_traces")}
    if "runtime_events_json" not in columns:
        with bind.begin() as connection:
            connection.execute(text("ALTER TABLE agent_run_traces ADD COLUMN runtime_events_json TEXT NULL"))
    inspector = inspect(bind)
    if "tool_jobs" in inspector.get_table_names():
        tool_columns = {column["name"]: column for column in inspector.get_columns("tool_jobs")}
        statements = []
        if "operational_alert_id" not in tool_columns:
            statements.append("ALTER TABLE tool_jobs ADD COLUMN operational_alert_id INTEGER NULL")
        if "payload" not in tool_columns:
            statements.append("ALTER TABLE tool_jobs ADD COLUMN payload TEXT NULL")
        if bind.dialect.name == "mysql" and not tool_columns["report_id"].get("nullable", True):
            statements.append("ALTER TABLE tool_jobs MODIFY COLUMN report_id INTEGER NULL")
        with bind.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))
    inspector = inspect(bind)
    if "dead_letter_records" in inspector.get_table_names():
        dead_columns = {column["name"]: column for column in inspector.get_columns("dead_letter_records")}
        statements = []
        if "operational_alert_id" not in dead_columns:
            statements.append("ALTER TABLE dead_letter_records ADD COLUMN operational_alert_id INTEGER NULL")
        if bind.dialect.name == "mysql" and not dead_columns["report_id"].get("nullable", True):
            statements.append("ALTER TABLE dead_letter_records MODIFY COLUMN report_id INTEGER NULL")
        with bind.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))


def seed_data(db: Session) -> None:
    if db.query(UserAccount).count() == 0:
        admin = UserAccount(
            username="admin",
            display_name="Counselor Admin",
            password_hash=hash_password("admin123"),
        )
        admin.roles = {"ROLE_ADMIN", "ROLE_USER"}
        student = UserAccount(
            username="student",
            display_name="Demo Student",
            password_hash=hash_password("student123"),
        )
        student.roles = {"ROLE_USER"}
        db.add_all([admin, student])
        db.commit()

    service = KnowledgeService(db, get_settings())
    root = Path(__file__).resolve().parents[1]
    for file in sorted((root / "knowledge").glob("*.md")):
        service.ensure_source(file.name, file.read_text(encoding="utf-8"))
