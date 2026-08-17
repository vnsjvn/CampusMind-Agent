import unittest
import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.core.enums import RiskLevel
from app.models.entities import ToolAuditRecord
from app.services.response_review import ResponseReview
from app.services.review_escalation import ResponseReviewEscalationService
from app.core.config import Settings
from app.services.report import ReportService


class ResponseReviewEscalationTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()

    def tearDown(self):
        self.db.close()

    def test_high_risk_flag_creates_one_idempotent_pending_review(self):
        review = ResponseReview(
            valid=False,
            decision="FLAG",
            requires_escalation=True,
            finding_codes=("MISSING_REQUIRED_TERM:high_risk_safety_plan",),
            checked_skills=("high_risk_safety_plan@1.0.0",),
        )
        service = ResponseReviewEscalationService(self.db)

        first = service.create_if_required(7, 11, "session-1", review)
        second = service.create_if_required(7, 11, "session-1", review)

        self.assertEqual(first.id, second.id)
        self.assertEqual(self.db.query(ToolAuditRecord).count(), 1)
        self.assertEqual(first.status, "PENDING_REVIEW")
        self.assertNotIn("response content", first.payload)

    def test_passed_or_non_report_review_does_not_create_task(self):
        passed = ResponseReview(True, "PASS", False, (), ())
        flagged = ResponseReview(False, "FLAG", True, ("EMPTY_OUTPUT",), ())
        service = ResponseReviewEscalationService(self.db)

        self.assertIsNone(service.create_if_required(1, 2, "session", passed))
        self.assertIsNone(service.create_if_required(None, 2, "session", flagged))
        self.assertEqual(self.db.query(ToolAuditRecord).count(), 0)

    def test_resolution_requires_token_and_is_idempotent(self):
        review = ResponseReview(False, "FLAG", True, ("EMPTY_OUTPUT",), ())
        service = ResponseReviewEscalationService(self.db)
        record = service.create_if_required(9, None, "session-9", review)
        settings = Settings(_env_file=None, mcp_staff_approval_token="review-token-123456789")

        with self.assertRaises(PermissionError):
            service.resolve(record.id, "APPROVED", "admin", "checked", "wrong", settings)
        resolved = service.resolve(
            record.id, "APPROVED", "admin", "checked", "review-token-123456789", settings
        )
        repeated = service.resolve(
            record.id, "APPROVED", "admin", "checked", "review-token-123456789", settings
        )

        self.assertEqual(resolved.status, "REVIEW_APPROVED")
        self.assertEqual(resolved.id, repeated.id)
        self.assertEqual(json.loads(resolved.payload)["resolution"]["resolvedBy"], "admin")
        with self.assertRaises(RuntimeError):
            service.resolve(
                record.id, "REJECTED", "admin", "changed", "review-token-123456789", settings
            )

    def test_review_query_filters_and_paginates_only_review_records(self):
        service = ResponseReviewEscalationService(self.db)
        review = ResponseReview(False, "FLAG", True, ("EMPTY_OUTPUT",), ())
        first = service.create_if_required(21, None, "s-21", review)
        second = service.create_if_required(22, None, "s-22", review)
        service.resolve(
            second.id, "APPROVED", "admin", "", "review-token-123456789",
            Settings(_env_file=None, mcp_staff_approval_token="review-token-123456789"),
        )
        self.db.add(ToolAuditRecord(
            report_id=99, tool_name="EXCEL_REPORT", policy="other", allowed=True,
            status="SUCCESS", reason="", payload="{}",
        ))
        self.db.commit()

        pending = ReportService(self.db).response_reviews("PENDING_REVIEW", limit=1, offset=0)
        all_reviews = ReportService(self.db).response_reviews("ALL", limit=1, offset=1)

        self.assertEqual(pending["total"], 1)
        self.assertEqual(pending["items"][0]["id"], first.id)
        self.assertEqual(all_reviews["total"], 2)
        self.assertEqual(len(all_reviews["items"]), 1)
        self.assertEqual(all_reviews["statusCounts"]["REVIEW_APPROVED"], 1)
        with self.assertRaises(ValueError):
            ReportService(self.db).response_reviews("INVALID")


if __name__ == "__main__":
    unittest.main()
