from __future__ import annotations

from datetime import datetime
import logging
import threading

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.entities import OperationalAlert
from app.services.report import ReportService


logger = logging.getLogger(__name__)


class OperationalAlertService:
    SOURCE = "planning"

    def __init__(self, db: Session):
        self.db = db

    def synchronize_planning_health(self, health: dict) -> list[dict]:
        if health.get("status") == "INSUFFICIENT_DATA":
            return self.list_alerts(source=self.SOURCE)

        now = datetime.utcnow()
        active_codes: set[str] = set()
        for item in health.get("alerts") or []:
            code = str(item.get("code") or "").strip()
            if not code.startswith("PLAN_"):
                continue
            active_codes.add(code)
            row = (
                self.db.query(OperationalAlert)
                .filter(OperationalAlert.code == code)
                .with_for_update()
                .one_or_none()
            )
            if row is None:
                row = OperationalAlert(code=code, source=self.SOURCE, first_seen_at=now)
                self.db.add(row)
            else:
                row.occurrence_count += 1
            row.severity = str(item.get("severity") or "WARNING")
            row.status = "OPEN"
            row.observed_rate = float(item.get("observedRate") or 0.0)
            row.threshold_rate = float(item.get("thresholdRate") or 0.0)
            row.last_seen_at = now
            row.resolved_at = None

        open_rows = self.db.query(OperationalAlert).filter(
            OperationalAlert.source == self.SOURCE,
            OperationalAlert.status == "OPEN",
        ).all()
        for row in open_rows:
            if row.code not in active_codes:
                row.status = "RESOLVED"
                row.resolved_at = now
        self.db.commit()
        return self.list_alerts(source=self.SOURCE)

    def list_alerts(self, source: str | None = None, limit: int = 100) -> list[dict]:
        query = self.db.query(OperationalAlert)
        if source:
            query = query.filter(OperationalAlert.source == source)
        rows = query.order_by(OperationalAlert.last_seen_at.desc()).limit(max(1, min(limit, 500))).all()
        return [self._to_dict(row) for row in rows]

    @staticmethod
    def _to_dict(row: OperationalAlert) -> dict:
        return {
            "id": row.id,
            "code": row.code,
            "source": row.source,
            "severity": row.severity,
            "status": row.status,
            "observedRate": row.observed_rate,
            "thresholdRate": row.threshold_rate,
            "occurrenceCount": row.occurrence_count,
            "firstSeenAt": row.first_seen_at,
            "lastSeenAt": row.last_seen_at,
            "resolvedAt": row.resolved_at,
        }


class OperationalAlertMonitor:
    LOCK_NAME = "mindbridge_operational_alert_monitor"

    def __init__(self, settings: Settings, session_factory):
        self.settings = settings
        self.session_factory = session_factory
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.settings.operational_alert_monitor_enabled:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="operational-alert-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._thread = None

    def run_once(self) -> dict:
        db = self.session_factory()
        locked = False
        lock_connection = None
        try:
            bind = db.get_bind()
            if bind.dialect.name == "mysql":
                lock_connection = bind.connect()
            locked = self._acquire_lock(lock_connection)
            if not locked:
                return {"executed": False, "reason": "LOCK_NOT_ACQUIRED"}
            limit = max(1, min(int(self.settings.operational_alert_monitor_trace_limit), 1000))
            metrics = ReportService(db, self.settings).agent_trace_metrics(limit)
            alerts = OperationalAlertService(db).synchronize_planning_health(metrics["planning"]["health"])
            return {
                "executed": True,
                "healthStatus": metrics["planning"]["health"]["status"],
                "alertCount": len(alerts),
            }
        finally:
            if locked:
                self._release_lock(lock_connection)
            if lock_connection is not None:
                lock_connection.close()
            db.close()

    def _loop(self) -> None:
        interval = max(1.0, float(self.settings.operational_alert_monitor_interval_seconds))
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                logger.exception("Operational alert evaluation failed")
            self._stop.wait(interval)

    def _acquire_lock(self, connection) -> bool:
        if connection is None:
            return True
        return connection.execute(text("SELECT GET_LOCK(:name, 0)"), {"name": self.LOCK_NAME}).scalar() == 1

    def _release_lock(self, connection) -> None:
        if connection is not None:
            connection.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": self.LOCK_NAME})
