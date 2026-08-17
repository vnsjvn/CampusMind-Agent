from __future__ import annotations

from dataclasses import dataclass

from app.core.enums import IntentType


@dataclass
class TaskPlanStep:
    id: str
    capability: str
    depends_on: tuple[str, ...] = ()
    status: str = "PENDING"
    error: str | None = None
    attempts: int = 0


class TaskPlan:
    def __init__(self, intent: IntentType, steps: list[TaskPlanStep]):
        self.intent = intent
        self.steps = steps

    def require_ready(self, step_id: str) -> None:
        step = self._step(step_id)
        incomplete = [dependency for dependency in step.depends_on if self._step(dependency).status != "COMPLETED"]
        if incomplete:
            raise RuntimeError(f"task plan dependency violation for {step_id}: {','.join(incomplete)}")

    def complete(self, step_id: str) -> None:
        step = self._step(step_id)
        self.require_ready(step_id)
        step.status = "COMPLETED"
        step.error = None

    def start(self, step_id: str) -> None:
        step = self._step(step_id)
        self.require_ready(step_id)
        if step.status not in {"PENDING", "RETRYING"}:
            raise RuntimeError(f"task plan step {step_id} cannot start from {step.status}")
        step.status = "RUNNING"
        step.error = None
        step.attempts += 1

    def fail(self, step_id: str, error: Exception | str) -> None:
        step = self._step(step_id)
        if step.status != "RUNNING":
            raise RuntimeError(f"task plan step {step_id} cannot fail from {step.status}")
        if isinstance(error, Exception):
            message = str(error).strip() or error.__class__.__name__
        else:
            message = str(error).strip() or "unknown planning error"
        step.status = "FAILED"
        step.error = message[:500]

    def retry(self, step_id: str) -> None:
        step = self._step(step_id)
        if step.status != "FAILED":
            raise RuntimeError(f"task plan step {step_id} cannot retry from {step.status}")
        step.status = "RETRYING"

    def to_dict(self) -> dict:
        return {
            "intent": self.intent.value,
            "steps": [
                {
                    "id": step.id,
                    "capability": step.capability,
                    "dependsOn": list(step.depends_on),
                    "status": step.status,
                    "error": step.error,
                    "attempts": step.attempts,
                }
                for step in self.steps
            ],
        }

    def to_prompt(self) -> str:
        return "\n".join(
            f"- {step.id}: {step.capability}; depends_on={','.join(step.depends_on) or 'none'}; "
            f"status={step.status}; attempts={step.attempts}; error={step.error or 'none'}"
            for step in self.steps
        )

    def _step(self, step_id: str) -> TaskPlanStep:
        for step in self.steps:
            if step.id == step_id:
                return step
        raise KeyError(f"unknown task plan step: {step_id}")


class TaskPlanner:
    @staticmethod
    def create(intent: IntentType) -> TaskPlan:
        steps = [
            TaskPlanStep("memory", "load bounded short and long-term memory"),
            TaskPlanStep("route", "select intent-specific execution path", ("memory",)),
        ]
        if intent == IntentType.CHAT:
            steps.append(TaskPlanStep("response", "build companion response context", ("route",)))
        else:
            steps.extend([
                TaskPlanStep("retrieval", "retrieve and rerank knowledge", ("route",)),
                TaskPlanStep("risk", "assess psychological risk", ("retrieval",)),
                TaskPlanStep("response", "build safety-aware response context", ("risk",)),
                TaskPlanStep("tools", "dispatch governed asynchronous tools", ("response",)),
            ])
        plan = TaskPlan(intent, steps)
        plan.complete("memory")
        plan.complete("route")
        TaskPlanVerifier.require_valid_definition(plan)
        return plan


class TaskPlanVerifier:
    VALID_STATUSES = {
        "PENDING", "QUEUED", "RUNNING", "IN_PROGRESS", "RETRYING", "COMPLETED", "FAILED"
    }

    @classmethod
    def require_valid_definition(cls, plan: TaskPlan) -> None:
        result = cls.verify(plan, phase="DEFINITION")
        if not result["valid"]:
            raise ValueError("invalid task plan: " + "; ".join(result["errors"]))

    @classmethod
    def verify(cls, plan: TaskPlan, phase: str = "RUNTIME") -> dict:
        return cls.verify_dict(plan.to_dict(), phase)

    @classmethod
    def verify_dict(cls, plan: dict, phase: str = "RUNTIME") -> dict:
        errors: list[str] = []
        warnings: list[str] = []
        steps = plan.get("steps") if isinstance(plan, dict) else None
        if not isinstance(steps, list) or not steps:
            return {"valid": False, "phase": phase, "errors": ["plan has no steps"], "warnings": []}
        ids = [str(step.get("id", "")) for step in steps]
        if any(not step_id for step_id in ids):
            errors.append("step id is required")
        if len(ids) != len(set(ids)):
            errors.append("step ids must be unique")
        by_id = {str(step.get("id")): step for step in steps if step.get("id")}
        for step_id, step in by_id.items():
            status = step.get("status")
            if status not in cls.VALID_STATUSES:
                errors.append(f"invalid status for {step_id}: {status}")
            for dependency in step.get("dependsOn", []):
                if dependency not in by_id:
                    errors.append(f"missing dependency for {step_id}: {dependency}")
        if cls._has_cycle(by_id):
            errors.append("plan dependency graph contains a cycle")

        intent = str(plan.get("intent", ""))
        required = ["memory", "route", "response"] if intent == IntentType.CHAT.value else [
            "memory", "route", "retrieval", "risk", "response", "tools"
        ]
        missing = [step_id for step_id in required if step_id not in by_id]
        if missing:
            errors.append("missing required steps: " + ",".join(missing))
        if intent != IntentType.CHAT.value and all(step_id in by_id for step_id in ("retrieval", "risk", "response")):
            if "retrieval" not in by_id["risk"].get("dependsOn", []):
                errors.append("risk must depend on retrieval")
            if "risk" not in by_id["response"].get("dependsOn", []):
                errors.append("response must depend on risk")

        completed = {step_id for step_id, step in by_id.items() if step.get("status") == "COMPLETED"}
        for step_id in completed:
            incomplete = [dep for dep in by_id[step_id].get("dependsOn", []) if dep not in completed]
            if incomplete:
                errors.append(f"completed step {step_id} has incomplete dependencies: {','.join(incomplete)}")
        if phase in {"RUNTIME", "ASYNC_TOOLS", "TERMINAL"}:
            synchronous = [step_id for step_id in required if step_id != "tools"]
            incomplete_sync = [step_id for step_id in synchronous if by_id.get(step_id, {}).get("status") != "COMPLETED"]
            if incomplete_sync:
                errors.append("runtime steps incomplete: " + ",".join(incomplete_sync))
        if phase == "TERMINAL" and "tools" in by_id:
            tool_status = by_id["tools"].get("status")
            if tool_status == "FAILED":
                errors.append("asynchronous tool plan failed")
            elif tool_status != "COMPLETED":
                warnings.append(f"asynchronous tool plan is not terminal: {tool_status}")
        return {"valid": not errors, "phase": phase, "errors": errors, "warnings": warnings}

    @staticmethod
    def _has_cycle(by_id: dict[str, dict]) -> bool:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step_id: str) -> bool:
            if step_id in visiting:
                return True
            if step_id in visited:
                return False
            visiting.add(step_id)
            for dependency in by_id[step_id].get("dependsOn", []):
                if dependency in by_id and visit(dependency):
                    return True
            visiting.remove(step_id)
            visited.add(step_id)
            return False

        return any(visit(step_id) for step_id in by_id)
