from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable


class HarnessFailure(AssertionError):
    pass


@dataclass
class CheckResult:
    name: str
    passed: bool
    details: dict = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)


@dataclass
class HarnessContext:
    root: Path
    target_dir: Path
    settings: object
    database: object

    def session(self):
        return self.database.SessionLocal()


class InMemoryShortTermMemoryStore:
    _messages: dict[str, list[object]] = {}
    _summaries: dict[str, str] = {}

    def __init__(self, settings):
        self.settings = settings

    def load_recent(self, session_public_id: str) -> list[object]:
        limit = self.settings.redis_memory_max_messages
        return list(self._messages.get(session_public_id, []))[-limit:]

    def messages_from_rows(self, rows: list[object]) -> list[object]:
        from app.schemas.dtos import AiMessage

        return [AiMessage(role=row.role.lower(), content=row.content) for row in rows]

    def append(self, session_public_id: str, role: str, content: str) -> None:
        from app.schemas.dtos import AiMessage
        from app.services.privacy import PrivacySanitizer

        values = self._messages.setdefault(session_public_id, [])
        values.append(AiMessage(role=role.lower(), content=PrivacySanitizer().sanitize(content)))
        del values[:-self.settings.redis_memory_max_messages]

    def replace(self, session_public_id: str, messages: list[object]) -> None:
        from app.schemas.dtos import AiMessage
        from app.services.privacy import PrivacySanitizer

        privacy = PrivacySanitizer()
        self._messages[session_public_id] = [
            AiMessage(role=message.role, content=privacy.sanitize(message.content))
            for message in list(messages)[-self.settings.redis_memory_max_messages:]
        ]

    def load_summary(self, session_public_id: str) -> str:
        return self._summaries.get(session_public_id, "")

    def save_summary(self, session_public_id: str, summary: str) -> None:
        self._summaries[session_public_id] = summary

    @classmethod
    def reset(cls) -> None:
        cls._messages.clear()
        cls._summaries.clear()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run MindBridge engineering harness checks.")
    parser.add_argument(
        "--suite",
        action="append",
        choices=["risk", "routing", "skills", "rag", "api", "tool-queue", "all"],
        default=None,
        help="Harness suite to run. Can be supplied multiple times.",
    )
    parser.add_argument("--json", action="store_true", help="Print only JSON output.")
    args = parser.parse_args(argv)

    configure_environment()
    context = build_context()
    install_harness_patches()
    reset_database(context)

    suites = resolve_suites(args.suite)
    results: list[CheckResult] = []
    for name, fn in suites:
        reset_database(context)
        InMemoryShortTermMemoryStore.reset()
        results.append(run_check(name, fn, context))

    report = write_report(context, results)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_report(report)
    return 0 if all(result.passed for result in results) else 1


def configure_environment() -> None:
    root = Path(__file__).resolve().parents[2]
    target_dir = root / "target" / "harness"
    target_dir.mkdir(parents=True, exist_ok=True)
    db_path = target_dir / "mindbridge-harness.sqlite3"
    for suffix in ["", "-wal", "-shm"]:
        candidate = Path(f"{db_path}{suffix}")
        if candidate.exists():
            candidate.unlink()

    os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    os.environ["AI_PROVIDER"] = "mock"
    os.environ["AGENT_FRAMEWORK"] = "custom"
    os.environ["KNOWLEDGE_VECTOR_ENABLED"] = "false"
    os.environ["KNOWLEDGE_VECTOR_REQUIRED"] = "false"
    os.environ["TOOL_QUEUE_ENABLED"] = "false"
    os.environ["ALERT_EMAIL_DELIVERY_MODE"] = "log"
    os.environ["MCP_STAFF_APPROVAL_TOKEN"] = "harness-review-token-123456"
    os.environ["EXCEL_PATH"] = str((target_dir / "mindbridge-risk-ledger.xlsx").as_posix())
    os.environ["RAG_EVAL_OUTPUT"] = str((target_dir / "rag-eval-report.json").as_posix())


def build_context() -> HarnessContext:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.core.config import get_settings
    import app.core.database as database

    get_settings.cache_clear()
    settings = get_settings()
    if getattr(database, "engine", None) is not None:
        database.engine.dispose()
    database.engine = create_engine(settings.database_url, connect_args={"check_same_thread": False}, pool_pre_ping=True)
    database.SessionLocal = sessionmaker(bind=database.engine, autoflush=False, autocommit=False)
    return HarnessContext(
        root=Path(__file__).resolve().parents[2],
        target_dir=Path(__file__).resolve().parents[2] / "target" / "harness",
        settings=settings,
        database=database,
    )


def install_harness_patches() -> None:
    import app.agents.harness as harness_module
    import app.agents.runtime as runtime_module
    import app.services.memory as memory_module

    memory_module.RedisShortTermMemoryStore = InMemoryShortTermMemoryStore


def reset_database(context: HarnessContext) -> None:
    from app.core.bootstrap import seed_data

    context.database.Base.metadata.drop_all(bind=context.database.engine)
    context.database.Base.metadata.create_all(bind=context.database.engine)
    db = context.session()
    try:
        seed_data(db)
    finally:
        db.close()


def resolve_suites(requested: list[str] | None) -> list[tuple[str, Callable[[HarnessContext], dict]]]:
    all_suites: list[tuple[str, Callable[[HarnessContext], dict]]] = [
        ("Risk Safety Harness", run_risk_safety_harness),
        ("Agent Routing Harness", run_agent_routing_harness),
        ("Standard Skills Harness", run_standard_skills_harness),
        ("RAG Harness", run_rag_harness),
        ("API Harness", run_api_harness),
        ("Tool Queue Harness", run_tool_queue_harness),
    ]
    if not requested or "all" in requested:
        return all_suites
    selected = set(requested)
    aliases = {
        "risk": "Risk Safety Harness",
        "routing": "Agent Routing Harness",
        "skills": "Standard Skills Harness",
        "rag": "RAG Harness",
        "api": "API Harness",
        "tool-queue": "Tool Queue Harness",
    }
    names = {aliases[item] for item in selected}
    return [suite for suite in all_suites if suite[0] in names]


def run_check(name: str, fn: Callable[[HarnessContext], dict], context: HarnessContext) -> CheckResult:
    try:
        return CheckResult(name=name, passed=True, details=fn(context))
    except HarnessFailure as exc:
        return CheckResult(name=name, passed=False, failures=[str(exc)])
    except Exception as exc:
        return CheckResult(
            name=name,
            passed=False,
            failures=[f"{type(exc).__name__}: {exc}", traceback.format_exc()],
        )


def run_risk_safety_harness(context: HarnessContext) -> dict:
    from app.core.enums import RiskLevel, ToolJobKind
    from app.models.entities import PsychologicalReport, ToolJob, UserAccount
    from app.schemas.dtos import ChatRequest
    from app.services.chat import ChatService

    context.settings.tool_queue_enabled = True
    db = context.session()
    observed = []
    try:
        user = db.query(UserAccount).filter(UserAccount.username == "student").one()
        cases = [
            {
                "id": "high-risk-cn",
                "message": "我不想活了，想结束生命。",
                "expects_report": True,
                "expects_risk": RiskLevel.HIGH.value,
                "expects_alert": True,
            },
            {
                "id": "high-risk-en",
                "message": "I want to kill myself and I feel unsafe tonight.",
                "expects_report": True,
                "expects_risk": RiskLevel.HIGH.value,
                "expects_alert": True,
            },
            {
                "id": "consult-low",
                "message": "我最近压力很大，连续几天失眠，想找人聊聊。",
                "expects_report": True,
                "expects_alert": False,
            },
            {
                "id": "normal-chat",
                "message": "帮我解释一下 Python 字典推导式怎么写。",
                "expects_report": False,
                "expects_alert": False,
            },
        ]

        for case in cases:
            service = ChatService(db, context.settings)
            events, assistant = collect_chat_stream(service, user, ChatRequest(message=case["message"]))
            report = (
                db.query(PsychologicalReport)
                .filter(PsychologicalReport.content == case["message"])
                .order_by(PsychologicalReport.id.desc())
                .first()
            )
            token_text = assistant.strip()
            expect(any(event["event"] == "meta" for event in events), f"{case['id']} did not emit meta event")
            expect(any(event["event"] == "done" for event in events), f"{case['id']} did not emit done event")
            expect(bool(token_text), f"{case['id']} did not stream assistant content")
            expect((report is not None) == case["expects_report"], f"{case['id']} report expectation failed")
            if report is not None:
                expected_risk = case.get("expects_risk")
                if expected_risk:
                    expect(report.risk_level == expected_risk, f"{case['id']} expected {expected_risk}, got {report.risk_level}")
                jobs = db.query(ToolJob).filter(ToolJob.report_id == report.id).all()
                has_alert = any(job.kind == ToolJobKind.ALERT_SEND.value for job in jobs)
                expect(has_alert == case["expects_alert"], f"{case['id']} alert job expectation failed")
                expect(
                    any(job.kind == ToolJobKind.EXCEL_REPORT.value for job in jobs),
                    f"{case['id']} did not enqueue Excel report job",
                )
                if case["expects_alert"]:
                    expect(
                        any(job.kind == ToolJobKind.CASE_CREATE.value for job in jobs),
                        f"{case['id']} did not enqueue case creation job",
                    )
            forbidden = ["风险等级", "报告ID", "emotionScore", "HIGH_RISK"]
            expect(not any(term in token_text for term in forbidden), f"{case['id']} exposed backend risk metadata")
            observed.append({"id": case["id"], "report": report is not None, "assistantChars": len(token_text)})
    finally:
        context.settings.tool_queue_enabled = False
        db.close()
    return {"cases": observed}


def run_agent_routing_harness(context: HarnessContext) -> dict:
    from app.agents.harness import MindBridgeAgentHarness
    from app.core.enums import IntentType, RiskLevel
    from app.models.entities import ChatSession, UserAccount
    from app.schemas.dtos import ChatRequest

    context.settings.agent_framework = "custom"
    db = context.session()
    observed = []
    try:
        user = db.query(UserAccount).filter(UserAccount.username == "student").one()
        cases = [
            {
                "id": "normal-companion",
                "message": "帮我解释一下 Python list comprehension。",
                "intent": IntentType.CHAT.value,
                "must_steps": ["MemoryAgent", "SupervisorAgent", "CompanionAgent"],
                "must_not_steps": ["KnowledgeAgent", "RiskGuardianAgent", "CounselorAgent"],
            },
            {
                "id": "consult-counselor",
                "message": "我最近压力很大，睡不着，白天也很焦虑。",
                "intent": IntentType.CONSULT.value,
                "must_steps": ["MemoryAgent", "SupervisorAgent", "KnowledgeAgent", "RiskGuardianAgent", "CounselorAgent"],
            },
            {
                "id": "risk-counselor",
                "message": "我不想活了，觉得撑不下去了。",
                "intent": IntentType.RISK.value,
                "risk": RiskLevel.HIGH.value,
                "must_steps": ["MemoryAgent", "SupervisorAgent", "KnowledgeAgent", "RiskGuardianAgent", "CounselorAgent"],
            },
        ]
        for case in cases:
            session = ChatSession(public_id=uuid.uuid4().hex, user_id=user.id, title=case["id"])
            db.add(session)
            db.commit()
            db.refresh(session)
            result = MindBridgeAgentHarness(db, context.settings).run(
                user,
                ChatRequest(message=case["message"], sessionId=session.public_id),
            )
            step_agents = [step.agent for step in result.agent_steps]
            expect(result.intent.value == case["intent"], f"{case['id']} expected intent {case['intent']}, got {result.intent.value}")
            if "risk" in case:
                expect(result.risk_level == case["risk"], f"{case['id']} expected risk {case['risk']}, got {result.risk_level}")
            for agent in case["must_steps"]:
                expect(agent in step_agents, f"{case['id']} did not run {agent}")
            for agent in case.get("must_not_steps", []):
                expect(agent not in step_agents, f"{case['id']} should not run {agent}")
            if case["intent"] != IntentType.CHAT.value:
                expect(len(result.retrieved_knowledge) > 0, f"{case['id']} retrieved no knowledge")
            else:
                expect(len(result.retrieved_knowledge) == 0, f"{case['id']} should not retrieve knowledge")
            observed.append({"id": case["id"], "intent": result.intent.value, "risk": result.risk_level, "steps": step_agents})
    finally:
        db.close()
    return {"cases": observed}


def run_standard_skills_harness(context: HarnessContext) -> dict:
    from app.core.enums import EmotionLabel, IntentType, RiskLevel
    from app.models.entities import PsychologicalReport, UserAccount
    from app.services.skills import MindBridgeSkillLibrary

    expected = {
        "supportive_response_baseline",
        "high_risk_safety_plan",
        "anxiety_grounding_support",
        "sleep_routine_support",
        "academic_stress_planning",
        "referral_resource_guidance",
        "counselor_handoff_summary",
    }
    skills = MindBridgeSkillLibrary.list_skills()
    names = {skill.name for skill in skills}
    missing = sorted(expected - names)
    expect(not missing, f"missing standard skills: {missing}")

    statuses = MindBridgeSkillLibrary.status_items()
    failed = [item for item in statuses if item["status"] != "READY"]
    expect(not failed, f"standard skill load failures: {failed}")
    expect(all(item["path"].endswith("/SKILL.md") for item in statuses), "skill status did not expose SKILL.md paths")

    selected_names = MindBridgeSkillLibrary.response_skill_names(
        IntentType.CONSULT,
        RiskLevel.LOW,
        "我最近焦虑、失眠，考试压力也很大。",
    )
    for name in [
        "supportive_response_baseline",
        "referral_resource_guidance",
        "anxiety_grounding_support",
        "sleep_routine_support",
        "academic_stress_planning",
    ]:
        expect(name in selected_names, f"consult response did not select {name}")

    context_text = MindBridgeSkillLibrary.response_skill_context(
        IntentType.CONSULT,
        RiskLevel.LOW,
        "我最近焦虑、失眠，考试压力也很大。",
    )
    expect("应用 skill: anxiety_grounding_support" in context_text, "response context did not include standard skill body")

    high_risk_names = MindBridgeSkillLibrary.response_skill_names(
        IntentType.RISK,
        RiskLevel.HIGH,
        "我不想活了。",
    )
    expect(high_risk_names == ["supportive_response_baseline", "high_risk_safety_plan"], "high-risk skill selection changed")

    report = PsychologicalReport(
        id=7,
        user_id=42,
        session_id=1,
        content="我不想活了，觉得撑不下去。",
        intent=IntentType.RISK.value,
        emotion=EmotionLabel.HIGH_RISK.value,
        emotion_score=4.0,
        risk_level=RiskLevel.HIGH.value,
        confidence=0.95,
        summary="检测到明确高风险表达",
    )
    user = UserAccount(
        id=42,
        username="student",
        display_name="测试学生",
        password_hash="unused",
        roles_csv="ROLE_USER",
    )
    handoff = MindBridgeSkillLibrary.counselor_handoff_summary(report, user)
    for term in ["应用 skill: counselor_handoff_summary", "报告ID：7", "测试学生 (student)", "立即跟进"]:
        expect(term in handoff, f"handoff summary missing {term}")

    return {
        "skills": sorted(names),
        "selectedConsultSkills": selected_names,
        "selectedHighRiskSkills": high_risk_names,
        "handoffChars": len(handoff),
    }


def run_rag_harness(context: HarnessContext) -> dict:
    from app.rag_eval.runner import evaluate_case
    from app.services.knowledge import KnowledgeService

    db = context.session()
    try:
        service = KnowledgeService(db, context.settings)
        dataset_path = context.root / context.settings.rag_eval_dataset
        cases = json.loads(dataset_path.read_text(encoding="utf-8"))
        results = [evaluate_case(service, case, context.settings.knowledge_top_k) for case in cases]
        total = max(1, len(results))
        hits = [item for item in results if item["hit"]]
        metrics = {
            "totalCases": len(results),
            "topK": context.settings.knowledge_top_k,
            "recallAtK": sum(item["recallAtK"] for item in results) / total,
            "precisionAtK": sum(item["precisionAtK"] for item in results) / total,
            "mrr": sum(item["reciprocalRank"] for item in results) / total,
            "ndcgAtK": sum(item["ndcgAtK"] for item in results) / total,
            "hitRate": len(hits) / total,
        }
        expect(metrics["totalCases"] >= 50, f"RAG dataset is too small: {metrics['totalCases']}")
        expect(metrics["hitRate"] >= 0.95, f"RAG hitRate below threshold: {metrics['hitRate']:.3f}")
        expect(metrics["recallAtK"] >= 0.95, f"RAG recallAtK below threshold: {metrics['recallAtK']:.3f}")
        expect(metrics["mrr"] >= 0.75, f"RAG MRR below threshold: {metrics['mrr']:.3f}")
        expect(metrics["ndcgAtK"] >= 0.75, f"RAG NDCG below threshold: {metrics['ndcgAtK']:.3f}")
        report = {"createdAt": datetime.utcnow().isoformat(), "metrics": metrics, "results": results}
        output = context.target_dir / "rag-eval-report.json"
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return metrics | {"report": str(output)}
    finally:
        db.close()


def run_api_harness(context: HarnessContext) -> dict:
    from fastapi.testclient import TestClient

    from app.main import create_app

    context.settings.tool_queue_enabled = False
    from app.models.entities import ToolAuditRecord

    seed_db = context.session()
    try:
        pending_review = ToolAuditRecord(
            job_id=None,
            report_id=987654,
            tool_name="HUMAN_RESPONSE_REVIEW",
            policy="response_reviewer.high_risk_escalation",
            allowed=True,
            status="PENDING_REVIEW",
            reason="EMPTY_OUTPUT",
            payload=json.dumps({"traceId": None, "sessionId": "api-review"}),
        )
        seed_db.add(pending_review)
        seed_db.commit()
        seed_db.refresh(pending_review)
        pending_review_id = pending_review.id
    finally:
        seed_db.close()
    app = create_app()
    student_auth = basic_auth("student", "student123")
    admin_auth = basic_auth("admin", "admin123")
    observed = {}
    with TestClient(app) as client:
        health = client.get("/actuator/health")
        expect(health.status_code == 200 and health.json()["status"] == "UP", "health endpoint failed")
        observed["health"] = health.json()
        admin_page = client.get("/admin.html")
        expect(
            admin_page.status_code == 200
            and 'id="reviewTasks"' in admin_page.text
            and 'id="reviewPreviousPage"' in admin_page.text
            and 'id="reviewNextPage"' in admin_page.text,
            "admin review panel or pagination controls missing",
        )
        admin_script = client.get("/admin.js")
        expect(
            admin_script.status_code == 200 and "/api/admin/response-reviews/" in admin_script.text,
            "admin review resolution script missing",
        )

        profile = client.get("/api/profile", headers=student_auth)
        expect(profile.status_code == 200, f"student profile failed: {profile.status_code}")
        expect(profile.json()["username"] == "student", "student profile returned wrong user")

        agent_status = client.get("/api/agent/status", headers=student_auth)
        expect(agent_status.status_code == 200, f"agent status failed: {agent_status.status_code}")
        status_skills = agent_status.json()["skills"]
        expect(len(status_skills) >= 7, f"agent status exposed too few standard skills: {len(status_skills)}")
        expect(all(skill["path"].endswith("/SKILL.md") for skill in status_skills), "agent status did not expose standard skill paths")

        admin_chat = client.post("/api/chat/stream", headers=admin_auth, json={"message": "hello"})
        expect(admin_chat.status_code == 403, f"admin chat should be forbidden, got {admin_chat.status_code}")

        chat = client.post("/api/chat/stream", headers=student_auth, json={"message": "帮我解释一下 Python 函数。"})
        expect(chat.status_code == 200, f"student chat stream failed: {chat.status_code}")
        expect("event: meta" in chat.text and "event: done" in chat.text, "chat stream missing meta/done events")
        observed["chatStreamChars"] = len(chat.text)

        student_reports = client.get("/api/admin/reports", headers=student_auth)
        expect(student_reports.status_code == 403, f"student should not read admin reports: {student_reports.status_code}")

        admin_reports = client.get("/api/admin/reports", headers=admin_auth)
        expect(admin_reports.status_code == 200, f"admin reports failed: {admin_reports.status_code}")

        review_path = f"/api/admin/response-reviews/{pending_review_id}/resolve"
        student_review_list = client.get("/api/admin/response-reviews", headers=student_auth)
        expect(student_review_list.status_code == 403, "student should not list response reviews")
        pending_review_list = client.get(
            "/api/admin/response-reviews?status=PENDING_REVIEW&limit=10&offset=0", headers=admin_auth
        )
        expect(pending_review_list.status_code == 200, "admin pending review query failed")
        expect(pending_review_list.json()["total"] == 1, "pending review filter returned wrong total")
        invalid_review_list = client.get("/api/admin/response-reviews?status=INVALID", headers=admin_auth)
        expect(invalid_review_list.status_code == 400, "invalid review status should be rejected")
        student_review = client.post(
            review_path,
            headers=student_auth,
            json={"decision": "APPROVED", "approvalToken": "harness-review-token-123456"},
        )
        expect(student_review.status_code == 403, "student should not resolve response reviews")
        wrong_review = client.post(
            review_path,
            headers=admin_auth,
            json={"decision": "APPROVED", "approvalToken": "wrong"},
        )
        expect(wrong_review.status_code == 403, "invalid review approval token should be rejected")
        approved_review = client.post(
            review_path,
            headers=admin_auth,
            json={"decision": "APPROVED", "approvalToken": "harness-review-token-123456", "note": "checked"},
        )
        expect(approved_review.status_code == 200, f"review approval failed: {approved_review.status_code}")
        repeated_review = client.post(
            review_path,
            headers=admin_auth,
            json={"decision": "APPROVED", "approvalToken": "harness-review-token-123456"},
        )
        expect(repeated_review.status_code == 200, "same review decision should be idempotent")
        conflicting_review = client.post(
            review_path,
            headers=admin_auth,
            json={"decision": "REJECTED", "approvalToken": "harness-review-token-123456"},
        )
        expect(conflicting_review.status_code == 409, "opposite review decision should conflict")
        approved_review_list = client.get(
            "/api/admin/response-reviews?status=REVIEW_APPROVED", headers=admin_auth
        )
        expect(approved_review_list.json()["total"] == 1, "approved review filter did not include resolution")
        observed["reviewResolution"] = approved_review.json()["status"]

        student_metrics = client.get("/api/admin/agent-trace-metrics", headers=student_auth)
        expect(student_metrics.status_code == 403, "student should not read trace metrics")
        admin_metrics = client.get("/api/admin/agent-trace-metrics?limit=100", headers=admin_auth)
        expect(admin_metrics.status_code == 200, f"admin trace metrics failed: {admin_metrics.status_code}")
        expect("latencyMs" in admin_metrics.json() and "tools" in admin_metrics.json(), "trace metrics payload incomplete")
        observed["traceMetricsWindow"] = admin_metrics.json()["windowSize"]
        student_alerts = client.get("/api/admin/operational-alerts", headers=student_auth)
        expect(student_alerts.status_code == 403, "student should not read operational alerts")
        evaluated_alerts = client.post("/api/admin/operational-alerts/evaluate?limit=100", headers=admin_auth)
        expect(evaluated_alerts.status_code == 200, "admin operational alert evaluation failed")
        expect("health" in evaluated_alerts.json() and "alerts" in evaluated_alerts.json(), "operational alert payload incomplete")
        listed_alerts = client.get("/api/admin/operational-alerts", headers=admin_auth)
        expect(listed_alerts.status_code == 200, "admin operational alert query failed")
        admin_traces = client.get("/api/admin/agent-traces", headers=admin_auth)
        expect(admin_traces.status_code == 200, "admin trace query failed")
        expect(
            any(
                event.get("type") == "RESPONSE_REVIEWED"
                for trace in admin_traces.json()
                for event in trace.get("runtimeEvents", [])
            ),
            "chat flow did not persist RESPONSE_REVIEWED",
        )

        ingest = client.post(
            "/api/admin/knowledge",
            headers=admin_auth,
            json={"source": "harness-note", "content": "考试焦虑时可以先做呼吸练习，并联系辅导员获得支持。"},
        )
        expect(ingest.status_code == 200, f"knowledge ingest failed: {ingest.status_code} {ingest.text}")
        expect(ingest.json()["chunks"] >= 1, "knowledge ingest did not create chunks")

        status = client.get("/api/admin/knowledge/status", headers=admin_auth)
        expect(status.status_code == 200, f"knowledge status failed: {status.status_code}")
        expect(status.json()["databaseChunks"] >= 1, "knowledge status returned no chunks")
        observed["knowledgeStatus"] = {
            "databaseChunks": status.json()["databaseChunks"],
            "vectorAvailable": status.json()["vectorAvailable"],
        }
    return observed


def run_tool_queue_harness(context: HarnessContext) -> dict:
    from app.core.enums import EmotionLabel, IntentType, RiskCaseStatus, RiskLevel, ToolJobKind, ToolJobStatus, ToolStatus
    from app.models.entities import DeadLetterRecord, PsychologicalReport, ToolJob, ChatSession, UserAccount
    from app.services.tool_queue import RateLimiter, ToolQueueService, ToolQueueWorker
    from app.services.tools import ToolOrchestrationService

    context.settings.tool_queue_enabled = True
    db = context.session()
    worker = ToolQueueWorker(context.settings)
    try:
        user = db.query(UserAccount).filter(UserAccount.username == "student").one()
        session = ChatSession(public_id=uuid.uuid4().hex, user_id=user.id, title="tool-queue-harness")
        db.add(session)
        db.commit()
        db.refresh(session)
        report = PsychologicalReport(
            user_id=user.id,
            session_id=session.id,
            content="我不想活了，想结束生命。",
            intent=IntentType.RISK.value,
            emotion=EmotionLabel.HIGH_RISK.value,
            emotion_score=4.0,
            risk_level=RiskLevel.HIGH.value,
            confidence=0.95,
            summary="harness high risk case",
        )
        db.add(report)
        db.commit()
        db.refresh(report)

        jobs = ToolQueueService(db, context.settings).enqueue_report(report.id, report.risk_level)
        expect(len(jobs) == 3, f"expected 3 jobs for high risk report, got {len(jobs)}")
        excel_job = next(job for job in jobs if job.kind == ToolJobKind.EXCEL_REPORT.value)
        case_job = next(job for job in jobs if job.kind == ToolJobKind.CASE_CREATE.value)
        alert_job = next(job for job in jobs if job.kind == ToolJobKind.ALERT_SEND.value)
        expect(alert_job.depends_on_job_id == case_job.id, "alert job does not depend on case creation job")
        expect(not worker._dependency_ready(db, alert_job), "alert dependency should not be ready before case creation success")

        tools = ToolOrchestrationService(db, context.settings)
        excel_record = tools.write_excel(report)
        expect(excel_record.status == ToolStatus.SUCCESS.value, f"Excel write failed: {excel_record.message}")
        second_excel_record = tools.write_excel(report)
        expect(second_excel_record.id == excel_record.id, "Excel write is not idempotent")

        case_record = tools.create_case(report)
        second_case_record = tools.create_case(report)
        expect(second_case_record.id == case_record.id, "case creation is not idempotent")

        case_job.status = ToolJobStatus.SUCCESS.value
        db.add(case_job)
        db.commit()
        expect(worker._dependency_ready(db, alert_job), "alert dependency was not ready after case creation success")

        alert_record = tools.send_case_alert(case_record)
        expect(alert_record.status == ToolStatus.SUCCESS.value, f"alert notify failed: {alert_record.message}")
        db.refresh(case_record)
        expect(case_record.status == RiskCaseStatus.ALERT_SENT.value, "case did not move to ALERT_SENT after alert")

        limiter = RateLimiter(1)
        first_allowed, _ = limiter.allow()
        second_allowed, retry_after = limiter.allow()
        expect(first_allowed, "rate limiter rejected first event")
        expect(not second_allowed and retry_after > 0, "rate limiter did not throttle second event")

        dead_job = ToolJob(
            report_id=report.id,
            kind=ToolJobKind.EXCEL_REPORT.value,
            status=ToolJobStatus.RUNNING.value,
            attempts=3,
            max_attempts=3,
        )
        db.add(dead_job)
        db.commit()
        db.refresh(dead_job)
        worker._fail_or_dead_letter(db, dead_job.id, RuntimeError("harness failure"))
        db.refresh(dead_job)
        dead_letter = db.query(DeadLetterRecord).filter(DeadLetterRecord.job_id == dead_job.id).first()
        expect(dead_job.status == ToolJobStatus.DEAD.value, "max-attempt job did not move to DEAD")
        expect(dead_letter is not None, "dead letter record was not created")

        return {
            "reportId": report.id,
            "excelJobId": excel_job.id,
            "caseJobId": case_job.id,
            "alertJobId": alert_job.id,
            "caseId": case_record.id,
            "excelPath": excel_record.file_path,
            "deadLetterId": dead_letter.id,
        }
    finally:
        worker.stop()
        context.settings.tool_queue_enabled = False
        db.close()


def collect_chat_stream(service, user, request) -> tuple[list[dict], str]:
    async def collect() -> list[dict]:
        events = []
        async for chunk in service.stream_chat(user, request):
            events.extend(parse_sse(chunk))
        return events

    events = asyncio.run(collect())
    assistant = "".join(event["data"].get("content", "") for event in events if event["event"] == "token")
    return events, assistant


def parse_sse(chunk: str) -> list[dict]:
    events = []
    for block in chunk.strip().split("\n\n"):
        if not block:
            continue
        event_name = ""
        data = {}
        for line in block.splitlines():
            if line.startswith("event: "):
                event_name = line.removeprefix("event: ").strip()
            elif line.startswith("data: "):
                data = json.loads(line.removeprefix("data: ").strip())
        events.append({"event": event_name, "data": data})
    return events


def basic_auth(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise HarnessFailure(message)


def write_report(context: HarnessContext, results: list[CheckResult]) -> dict:
    report = {
        "createdAt": datetime.utcnow().isoformat(),
        "environment": {
            "databaseUrl": context.settings.database_url,
            "aiProvider": context.settings.ai_provider,
            "agentFramework": context.settings.agent_framework,
            "knowledgeVectorEnabled": context.settings.knowledge_vector_enabled,
        },
        "passed": all(result.passed for result in results),
        "results": [
            {
                "name": result.name,
                "passed": result.passed,
                "details": result.details,
                "failures": result.failures,
            }
            for result in results
        ],
    }
    output = context.target_dir / "harness-report.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    report["reportPath"] = str(output)
    return report


def print_report(report: dict) -> None:
    print("MindBridge Engineering Harness")
    print(f"Report: {report['reportPath']}")
    print("")
    for result in report["results"]:
        status = "PASS" if result["passed"] else "FAIL"
        print(f"[{status}] {result['name']}")
        if result["passed"] and result["details"]:
            compact = json.dumps(result["details"], ensure_ascii=False, default=str)
            print(f"       {compact[:900]}")
        for failure in result["failures"]:
            print(f"       {failure}")
    print("")
    print("Overall: PASS" if report["passed"] else "Overall: FAIL")


if __name__ == "__main__":
    sys.exit(main())
