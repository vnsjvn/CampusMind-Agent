from __future__ import annotations

from collections.abc import AsyncIterator

from app.services.skills import MindBridgeSkill


class SkillOutputPostProcessor:
    """Incrementally enforce merged Skill output constraints without buffering a full LLM reply."""

    def __init__(self, skills: list[MindBridgeSkill]):
        schemas = [skill.output_schema for skill in skills if skill.output_schema]
        self.required_terms = _unique(term for schema in schemas for term in schema.get("requiredTerms", []))
        self.forbidden_terms = _unique(term for schema in schemas for term in schema.get("forbiddenTerms", []))
        self.required_fallbacks = {
            str(term): str(text)
            for schema in schemas
            for term, text in schema.get("requiredFallbacks", {}).items()
        }
        minimums = [int(schema.get("minLength", 0)) for schema in schemas]
        maximums = [int(schema["maxLength"]) for schema in schemas if int(schema.get("maxLength", 0)) > 0]
        question_limits = [int(schema["maxQuestions"]) for schema in schemas if schema.get("maxQuestions") is not None]
        self.min_length = max(minimums, default=0)
        self.max_length = min(maximums, default=0)
        self.max_questions = min(question_limits, default=None)
        self.output = ""
        self.issues: list[str] = []
        self._questions = 0

    async def process(self, source: AsyncIterator[str]) -> AsyncIterator[str]:
        hold = max((len(term) for term in self.forbidden_terms), default=1) - 1
        pending = ""
        try:
            async for chunk in source:
                pending += chunk
                pending = self._remove_forbidden(pending)
                if len(pending) <= hold:
                    continue
                ready, pending = pending[:-hold] if hold else pending, pending[-hold:] if hold else ""
                emitted = self._normalize(ready)
                if emitted:
                    yield emitted
                if self.max_length and len(self.output) >= self._model_output_limit():
                    break
        finally:
            close = getattr(source, "aclose", None)
            if close is not None:
                await close()

        emitted = self._normalize(self._remove_forbidden(pending))
        if emitted:
            yield emitted

        suffix = self._completion_suffix()
        if suffix:
            emitted = self._normalize(suffix, use_reserved_space=True)
            if emitted:
                yield emitted
        self.issues = self._validate_final()

    def _remove_forbidden(self, text: str) -> str:
        for term in self.forbidden_terms:
            text = text.replace(term, "")
        return text

    def _normalize(self, text: str, use_reserved_space: bool = False) -> str:
        limit = self.max_length if use_reserved_space else self._model_output_limit()
        result = []
        for char in text:
            if limit and len(self.output) + len(result) >= limit:
                break
            if char in {"?", "？"}:
                if self.max_questions is not None and self._questions >= self.max_questions:
                    char = "。"
                else:
                    self._questions += 1
            result.append(char)
        emitted = "".join(result)
        self.output += emitted
        return emitted

    def _model_output_limit(self) -> int:
        if not self.max_length:
            return 0
        reserve = sum(len(self.required_fallbacks.get(term, term)) + 1 for term in self.required_terms)
        return max(0, self.max_length - reserve)

    def _completion_suffix(self) -> str:
        additions = []
        for term in self.required_terms:
            if term not in self.output:
                additions.append(self.required_fallbacks.get(term, term))
        if len(self.output) + sum(len(item) for item in additions) < self.min_length:
            additions.append("我们可以先从一个安全、具体的小步骤开始。")
        return ("\n" if additions and self.output else "") + " ".join(additions)

    def _validate_final(self) -> list[str]:
        issues = []
        if len(self.output.strip()) < self.min_length:
            issues.append("minLength")
        if self.max_length and len(self.output) > self.max_length:
            issues.append("maxLength")
        issues.extend(f"required:{term}" for term in self.required_terms if term not in self.output)
        issues.extend(f"forbidden:{term}" for term in self.forbidden_terms if term in self.output)
        if self.max_questions is not None and self._questions > self.max_questions:
            issues.append("maxQuestions")
        return issues


def _unique(values) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in values if str(value)))
