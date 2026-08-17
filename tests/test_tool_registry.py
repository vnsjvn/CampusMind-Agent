import unittest
from types import SimpleNamespace

from app.services.tool_registry import ToolExecutorRegistry
from app.services.tool_governance import ToolPolicyRegistry


class FakeTools:
    def __init__(self):
        self.calls = []

    def write_excel(self, report):
        self.calls.append(("excel", report.id))
        return SimpleNamespace(status="SUCCESS", message="ok")

    def create_case(self, report):
        self.calls.append(("case", report.id))
        return SimpleNamespace(id=9)

    def send_case_alert(self, case):
        self.calls.append(("alert", case.id))
        return SimpleNamespace(status="SUCCESS", message="ok")

    def notify(self, report):
        self.calls.append(("notify", report.id))
        return SimpleNamespace(status="SUCCESS", message="ok")

    def acknowledge_case(self, case_id, actor, note=""):
        self.calls.append(("ack", case_id, actor, note))
        return SimpleNamespace(id=case_id)

    def add_case_note(self, case_id, actor, note):
        self.calls.append(("note", case_id, actor, note))
        return SimpleNamespace(id=1, case_id=case_id)


class ToolExecutorRegistryTests(unittest.TestCase):
    def test_registry_dispatches_registered_executor(self):
        tools = FakeTools()
        registry = ToolExecutorRegistry(tools)

        registry.execute("ALERT_SEND", report=SimpleNamespace(id=3))

        self.assertEqual(tools.calls, [("case", 3), ("alert", 9)])
        self.assertEqual(
            next(item for item in registry.status_items() if item["name"] == "ALERT_SEND")["capability"],
            "risk_alert.send",
        )

    def test_registry_rejects_unknown_and_duplicate_tools(self):
        registry = ToolExecutorRegistry(FakeTools())

        with self.assertRaises(RuntimeError):
            registry.execute("DELETE_EVERYTHING", report=SimpleNamespace(id=1))
        with self.assertRaises(ValueError):
            registry.register("EXCEL_REPORT", "duplicate", ("report",), lambda **_: None)

    def test_every_governed_queue_tool_has_an_executor(self):
        registry = ToolExecutorRegistry(FakeTools())

        self.assertEqual(
            {item["name"] for item in registry.status_items() if item["name"] in ToolPolicyRegistry.POLICIES},
            set(ToolPolicyRegistry.POLICIES),
        )

    def test_registry_validates_human_tool_arguments(self):
        registry = ToolExecutorRegistry(FakeTools())

        with self.assertRaisesRegex(RuntimeError, "missing required arguments: actor, note"):
            registry.execute("CASE_NOTE_ADD", case_id=1)

    def test_catalog_exposes_queue_and_human_mcp_tools(self):
        catalog = {item["name"]: item for item in ToolExecutorRegistry.catalog_items()}

        self.assertEqual(len(catalog), 6)
        self.assertEqual(catalog["ALERT_ACK"]["requiredArguments"], ["case_id", "actor"])
        self.assertEqual(catalog["CASE_NOTE_ADD"]["capability"], "risk_case.note.add")


if __name__ == "__main__":
    unittest.main()
