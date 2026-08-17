from __future__ import annotations

from dataclasses import dataclass

from app.core.enums import RiskLevel
from app.services.skills import MindBridgeSkill


@dataclass(frozen=True)
class ResponseReview:
    valid: bool
    decision: str
    requires_escalation: bool
    finding_codes: tuple[str, ...]
    checked_skills: tuple[str, ...]

    def to_payload(self) -> dict:
        return {
            "valid": self.valid,
            "decision": self.decision,
            "requiresEscalation": self.requires_escalation,
            "findingCodes": list(self.finding_codes),
            "checkedSkills": list(self.checked_skills),
        }


class ResponseReviewer:
    """Deterministic final-output audit; never sends response content to another model."""

    @staticmethod
    def review(
        output: str,
        skills: list[MindBridgeSkill],
        risk: RiskLevel,
        post_processor_issues: list[str] | None = None,
    ) -> ResponseReview:
        normalized = output.strip()
        findings: list[str] = []
        if not normalized:
            findings.append("EMPTY_OUTPUT")
        for skill in skills:
            schema = skill.output_schema or {}
            minimum = int(schema.get("minLength", 0))
            maximum = int(schema.get("maxLength", 0))
            if minimum and len(normalized) < minimum:
                findings.append(f"MIN_LENGTH:{skill.name}")
            if maximum and len(normalized) > maximum:
                findings.append(f"MAX_LENGTH:{skill.name}")
            if any(term not in normalized for term in schema.get("requiredTerms", [])):
                findings.append(f"MISSING_REQUIRED_TERM:{skill.name}")
            if any(term in normalized for term in schema.get("forbiddenTerms", [])):
                findings.append(f"FORBIDDEN_TERM:{skill.name}")
            question_limit = schema.get("maxQuestions")
            if question_limit is not None and _question_count(normalized) > int(question_limit):
                findings.append(f"MAX_QUESTIONS:{skill.name}")
        if post_processor_issues:
            findings.append("POST_PROCESSOR_ISSUE")
        unique_findings = tuple(dict.fromkeys(findings))
        valid = not unique_findings
        return ResponseReview(
            valid=valid,
            decision="PASS" if valid else "FLAG",
            requires_escalation=not valid and risk == RiskLevel.HIGH,
            finding_codes=unique_findings,
            checked_skills=tuple(f"{skill.name}@{skill.version}" for skill in skills),
        )


def _question_count(text: str) -> int:
    return text.count("?") + text.count("？")
