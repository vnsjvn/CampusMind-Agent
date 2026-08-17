from __future__ import annotations

import re
import json
from dataclasses import dataclass, field
from pathlib import Path

from app.core.enums import IntentType, RiskLevel
from app.models.entities import PsychologicalReport, UserAccount


class SkillLoadError(RuntimeError):
    pass


@dataclass(frozen=True)
class SkillValidationIssue:
    level: str
    message: str


@dataclass(frozen=True)
class MindBridgeSkill:
    name: str
    description: str
    body: str
    path: Path
    metadata: dict[str, str] = field(default_factory=dict)
    output_schema: dict = field(default_factory=dict)

    @property
    def version(self) -> str:
        return self.metadata.get("version", "1.0.0")

    @property
    def enabled(self) -> bool:
        return self.metadata.get("enabled", "true").lower() in {"true", "1", "yes", "on"}

    @property
    def priority(self) -> int:
        try:
            return int(self.metadata.get("priority", "100"))
        except ValueError:
            return 100

    @property
    def allowed_tools(self) -> tuple[str, ...]:
        raw = self.metadata.get("allowed_tools", "")
        return tuple(item.strip().upper() for item in raw.split(",") if item.strip())

    def prompt_context(self) -> str:
        schema_context = ""
        if self.output_schema:
            schema_context = f"\n输出约束：{json.dumps(self.output_schema, ensure_ascii=False, separators=(',', ':'))}"
        return f"应用 skill: {self.name}@{self.version}\n{self.body.strip()}{schema_context}"

    def validate_output(self, text: str) -> list[str]:
        if not self.output_schema:
            return []
        issues = []
        normalized = text.strip()
        min_length = int(self.output_schema.get("minLength", 0))
        max_length = int(self.output_schema.get("maxLength", 0))
        if len(normalized) < min_length:
            issues.append(f"输出长度 {len(normalized)} 小于 minLength={min_length}")
        if max_length > 0 and len(normalized) > max_length:
            issues.append(f"输出长度 {len(normalized)} 超过 maxLength={max_length}")
        for term in self.output_schema.get("requiredTerms", []):
            if term not in normalized:
                issues.append(f"缺少必需内容：{term}")
        for term in self.output_schema.get("forbiddenTerms", []):
            if term in normalized:
                issues.append(f"包含禁止内容：{term}")
        max_questions = self.output_schema.get("maxQuestions")
        if max_questions is not None and normalized.count("？") + normalized.count("?") > int(max_questions):
            issues.append(f"问题数量超过 maxQuestions={max_questions}")
        return issues

    def validation_issues(self) -> list[SkillValidationIssue]:
        issues: list[SkillValidationIssue] = []
        if self.path.parent.name != self.name:
            issues.append(SkillValidationIssue("WARN", f"目录名 {self.path.parent.name} 与 skill name {self.name} 不一致"))
        if "## Workflow" not in self.body:
            issues.append(SkillValidationIssue("WARN", "建议包含 ## Workflow 小节，便于人工审阅和模型稳定加载"))
        if len(self.description) < 20:
            issues.append(SkillValidationIssue("WARN", "description 太短，可能无法准确表达触发场景"))
        if not re.fullmatch(r"\d+\.\d+\.\d+", self.version):
            issues.append(SkillValidationIssue("ERROR", "version 必须使用语义化版本，例如 1.0.0"))
        try:
            int(self.metadata.get("priority", "100"))
        except ValueError:
            issues.append(SkillValidationIssue("ERROR", "priority 必须是整数"))
        if self.name == "counselor_handoff_summary" and "```text" not in self.body:
            issues.append(SkillValidationIssue("ERROR", "counselor_handoff_summary 必须包含 text 模板"))
        return issues


class MindBridgeSkillRegistry:
    def __init__(self, root: Path | None = None):
        self.root = root or Path(__file__).resolve().parents[2] / "skills"

    def list_skills(self, include_disabled: bool = False) -> list[MindBridgeSkill]:
        if not self.root.exists():
            return []
        skills = []
        for skill_file in sorted(self.root.glob("*/SKILL.md")):
            skill = self._load_skill_file(skill_file)
            if include_disabled or skill.enabled:
                skills.append(skill)
        return sorted(skills, key=lambda item: (item.priority, item.name))

    def status_items(self) -> list[dict]:
        if not self.root.exists():
            return []
        items = []
        for skill_file in sorted(self.root.glob("*/SKILL.md")):
            try:
                skill = self._load_skill_file(skill_file)
                issues = skill.validation_issues()
            except SkillLoadError as exc:
                items.append(
                    {
                        "name": skill_file.parent.name,
                        "status": "FAILED",
                        "description": str(exc),
                        "path": skill_file.relative_to(self.root.parent).as_posix(),
                        "issues": [{"level": "ERROR", "message": str(exc)}],
                    }
                )
                continue
            has_error = any(issue.level == "ERROR" for issue in issues)
            items.append(
                {
                    "name": skill.name,
                    "status": "FAILED" if has_error else "DISABLED" if not skill.enabled else "READY" if not issues else "WARN",
                    "description": skill.description,
                    "version": skill.version,
                    "enabled": skill.enabled,
                    "priority": skill.priority,
                    "allowedTools": list(skill.allowed_tools),
                    "outputSchema": skill.output_schema,
                    "path": skill.path.relative_to(self.root.parent).as_posix(),
                    "issues": [{"level": issue.level, "message": issue.message} for issue in issues],
                    "metadata": skill.metadata,
                }
            )
        return items

    def get_required(self, name: str) -> MindBridgeSkill:
        for skill in self.list_skills(include_disabled=True):
            if skill.name == name:
                if not skill.enabled:
                    raise SkillLoadError(f"required standard skill is disabled: {name}")
                return skill
        raise SkillLoadError(f"required standard skill not found: {name}")

    def template_for(self, name: str) -> str:
        skill = self.get_required(name)
        match = re.search(r"```text\s*\n(?P<template>.*?)\n```", skill.body, re.DOTALL)
        if match is None:
            raise SkillLoadError(f"standard skill {name} does not define a text template")
        return match.group("template").strip()

    def _load_skill_file(self, path: Path) -> MindBridgeSkill:
        text = path.read_text(encoding="utf-8")
        metadata, body = _split_frontmatter(text, path)
        name = metadata.get("name") or path.parent.name
        description = metadata.get("description", "")
        if not name.strip():
            raise SkillLoadError(f"{path} is missing frontmatter name")
        if not description.strip():
            raise SkillLoadError(f"{path} is missing frontmatter description")
        if not body.strip():
            raise SkillLoadError(f"{path} is missing skill body")
        output_schema = self._load_output_schema(path.parent / "output_schema.json")
        return MindBridgeSkill(
            name=name.strip(),
            description=description.strip(),
            body=body.strip(),
            path=path,
            metadata=metadata,
            output_schema=output_schema,
        )

    @staticmethod
    def _load_output_schema(path: Path) -> dict:
        if not path.exists():
            return {}
        try:
            schema = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SkillLoadError(f"{path} contains invalid JSON: {exc}") from exc
        if not isinstance(schema, dict):
            raise SkillLoadError(f"{path} must contain a JSON object")
        return schema


class MindBridgeSkillLibrary:
    @staticmethod
    def registry() -> MindBridgeSkillRegistry:
        return MindBridgeSkillRegistry()

    @staticmethod
    def list_skills() -> list[MindBridgeSkill]:
        return MindBridgeSkillLibrary.registry().list_skills()

    @staticmethod
    def status_items() -> list[dict]:
        return MindBridgeSkillLibrary.registry().status_items()

    @staticmethod
    def response_skill_context(intent: IntentType, risk: RiskLevel, text: str) -> str:
        return "\n\n".join(skill.prompt_context() for skill in MindBridgeSkillLibrary.response_skills(intent, risk, text))

    @staticmethod
    def response_skills(intent: IntentType, risk: RiskLevel, text: str) -> list[MindBridgeSkill]:
        names = MindBridgeSkillLibrary.response_skill_names(intent, risk, text)
        registry = MindBridgeSkillLibrary.registry()
        return [registry.get_required(name) for name in names]

    @staticmethod
    def response_skill_names(intent: IntentType, risk: RiskLevel, text: str) -> list[str]:
        if intent == IntentType.CHAT:
            return []

        if risk == RiskLevel.HIGH:
            return ["supportive_response_baseline", "high_risk_safety_plan"]

        lowered = text.lower()
        names = ["supportive_response_baseline", "referral_resource_guidance"]
        if _contains_any(lowered, ["焦虑", "惊恐", "恐慌", "panic", "anxious", "崩溃", "呼吸"]):
            names.append("anxiety_grounding_support")
        if _contains_any(lowered, ["失眠", "睡不着", "睡眠", "熬夜", "sleep", "insomnia"]):
            names.append("sleep_routine_support")
        if _contains_any(lowered, ["考试", "挂科", "绩点", "论文", "作业", "学业", "学习", "academic", "exam"]):
            names.append("academic_stress_planning")
        return _dedupe(names)

    @staticmethod
    def allowed_tools_for_response(intent: IntentType, risk: RiskLevel, text: str) -> tuple[str, ...]:
        tools = []
        for skill in MindBridgeSkillLibrary.response_skills(intent, risk, text):
            tools.extend(skill.allowed_tools)
        return tuple(_dedupe(tools))

    @staticmethod
    def high_risk_safety_plan_prompt() -> str:
        return MindBridgeSkillLibrary.registry().get_required("high_risk_safety_plan").prompt_context()

    @staticmethod
    def counselor_handoff_summary(report: PsychologicalReport, user: UserAccount | None) -> str:
        registry = MindBridgeSkillLibrary.registry()
        skill = registry.get_required("counselor_handoff_summary")
        template = registry.template_for("counselor_handoff_summary")
        student = _student_label(user, report.user_id)
        urgency = "立即跟进" if report.risk_level == RiskLevel.HIGH.value else "尽快跟进"
        next_steps = [
            f"{urgency}，确认学生当前位置、身边是否有人陪伴，以及当前是否安全。",
            "联系学生本人或其可用的现实支持人，并记录已采取的联系方式。",
            "必要时联系校园保卫、心理中心值班老师或当地紧急救助。",
            "将后续安排、接手人和下一次复访时间写入个案备注。",
        ]
        rendered = _render_template(
            template,
            {
                "report_id": str(report.id),
                "student": student,
                "risk_level": report.risk_level,
                "emotion": report.emotion,
                "confidence": f"{report.confidence:.2f}",
                "summary": report.summary,
                "next_steps": "\n".join(f"- {step}" for step in next_steps),
                "content_excerpt": _truncate(report.content, 700),
            },
        )
        issues = skill.validate_output(rendered)
        if issues:
            raise SkillLoadError(f"counselor_handoff_summary 输出不符合 Schema：{'；'.join(issues)}")
        return rendered


def _split_frontmatter(text: str, path: Path) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        raise SkillLoadError(f"{path} is missing YAML frontmatter")
    end = text.find("\n---", 4)
    if end == -1:
        raise SkillLoadError(f"{path} has unterminated YAML frontmatter")
    metadata = {}
    for line in text[4:end].splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            raise SkillLoadError(f"{path} has invalid frontmatter line: {line}")
        key, value = stripped.split(":", 1)
        metadata[key.strip()] = value.strip().strip("\"'")
    return metadata, text[end + len("\n---") :].strip()


def _contains_any(text: str, terms: list[str]) -> bool:
    return any(term in text for term in terms)


def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _render_template(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered


def _student_label(user: UserAccount | None, user_id: int) -> str:
    if user is None:
        return f"userId={user_id}"
    if user.display_name:
        return f"{user.display_name} ({user.username})"
    return user.username


def _truncate(text: str, limit: int) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit - 3]}..."
