import unittest

from sqlalchemy import create_engine, inspect, text

from app.core.bootstrap import ensure_schema_compatibility


class SchemaCompatibilityTests(unittest.TestCase):
    def test_existing_queue_tables_receive_operational_task_columns(self):
        engine = create_engine("sqlite:///:memory:")
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE agent_run_traces (id INTEGER PRIMARY KEY)"))
            connection.execute(text(
                "CREATE TABLE tool_jobs (id INTEGER PRIMARY KEY, report_id INTEGER NULL, kind VARCHAR(64))"
            ))
            connection.execute(text(
                "CREATE TABLE dead_letter_records (id INTEGER PRIMARY KEY, report_id INTEGER NULL, kind VARCHAR(64))"
            ))

        ensure_schema_compatibility(engine)
        inspector = inspect(engine)

        self.assertIn("runtime_events_json", {item["name"] for item in inspector.get_columns("agent_run_traces")})
        tool_columns = {item["name"] for item in inspector.get_columns("tool_jobs")}
        self.assertIn("operational_alert_id", tool_columns)
        self.assertIn("payload", tool_columns)
        dead_columns = {item["name"] for item in inspector.get_columns("dead_letter_records")}
        self.assertIn("operational_alert_id", dead_columns)


if __name__ == "__main__":
    unittest.main()
