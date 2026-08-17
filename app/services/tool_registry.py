from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.core.enums import ToolJobKind, ToolStatus
from app.models.entities import PsychologicalReport, RiskCase
from app.services.tools import ToolOrchestrationService


ToolExecutor = Callable[..., object]


@dataclass(frozen=True)
class RegisteredTool:
    name: str
    capability: str
    required_arguments: tuple[str, ...]
    executor: ToolExecutor


class ToolExecutorRegistry:
    """Maps governed tool names to concrete business executors."""

    CATALOG = {
        "EXCEL_REPORT": ("report.export", ("report",)),
        "CASE_CREATE": ("risk_case.create", ("report",)),
        "ALERT_SEND": ("risk_alert.send", ("report_or_case_id",)),
        "RISK_ALERT": ("risk_alert.legacy_notify", ("report",)),
        "ALERT_ACK": ("risk_case.acknowledge", ("case_id", "actor")),
        "CASE_NOTE_ADD": ("risk_case.note.add", ("case_id", "actor", "note")),
    }

    def __init__(self, tools: ToolOrchestrationService):
        self._tools: dict[str, RegisteredTool] = {}
        self._orchestration = tools
        self.register(ToolJobKind.EXCEL_REPORT.value, "report.export", ("report",), self._write_excel)
        self.register(ToolJobKind.CASE_CREATE.value, "risk_case.create", ("report",), tools.create_case)
        self.register(ToolJobKind.ALERT_SEND.value, "risk_alert.send", (), self._send_case_alert)
        self.register(ToolJobKind.RISK_ALERT.value, "risk_alert.legacy_notify", ("report",), self._notify)
        self.register("ALERT_ACK", "risk_case.acknowledge", ("case_id", "actor"), tools.acknowledge_case)
        self.register("CASE_NOTE_ADD", "risk_case.note.add", ("case_id", "actor", "note"), tools.add_case_note)

    def register(self, name: str, capability: str, required_arguments: tuple[str, ...], executor: ToolExecutor) -> None:
        normalized = name.strip().upper()
        if not normalized or not capability.strip():
            raise ValueError("tool name and capability are required")
        if normalized in self._tools:
            raise ValueError(f"tool executor already registered: {normalized}")
        self._tools[normalized] = RegisteredTool(normalized, capability.strip(), required_arguments, executor)

    def execute(self, name: str, **arguments) -> object:
        registered = self._tools.get(name.strip().upper())
        if registered is None:
            raise RuntimeError(f"tool executor not registered: {name}")
        missing = [field for field in registered.required_arguments if arguments.get(field) is None]
        if missing:
            raise RuntimeError(f"tool {registered.name} missing required arguments: {', '.join(missing)}")
        return registered.executor(**arguments)

    def status_items(self) -> list[dict[str, str]]:
        return [
            {
                "name": item.name,
                "capability": item.capability,
                "requiredArguments": list(item.required_arguments),
                "status": "READY",
            }
            for item in sorted(self._tools.values(), key=lambda value: value.name)
        ]

    @classmethod
    def catalog_items(cls) -> list[dict]:
        return [
            {
                "name": name,
                "capability": definition[0],
                "requiredArguments": list(definition[1]),
                "status": "REGISTERED",
            }
            for name, definition in sorted(cls.CATALOG.items())
        ]

    def _write_excel(self, report: PsychologicalReport):
        record = self._orchestration.write_excel(report)
        if record.status != ToolStatus.SUCCESS.value:
            raise RuntimeError(record.message)
        return record

    def _send_case_alert(self, report: PsychologicalReport | None = None, case_id: int | None = None):
        case = self._orchestration.db.get(RiskCase, case_id) if case_id is not None else None
        if case is None and report is not None:
            case = self._orchestration.create_case(report)
        if case is None:
            raise RuntimeError("ALERT_SEND requires report or an existing case_id")
        record = self._orchestration.send_case_alert(case)
        if record.status != ToolStatus.SUCCESS.value:
            raise RuntimeError(record.message)
        return record

    def _notify(self, report: PsychologicalReport):
        record = self._orchestration.notify(report)
        if record.status != ToolStatus.SUCCESS.value:
            raise RuntimeError(record.message)
        return record
