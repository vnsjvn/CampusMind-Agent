import unittest

import httpx
from sqlalchemy.exc import OperationalError

from app.services.agent_retry import AgentRetryPolicy


class AgentRetryPolicyTests(unittest.TestCase):
    def test_transient_network_and_database_errors_are_retryable(self):
        self.assertTrue(AgentRetryPolicy.classify(TimeoutError()).retryable)
        self.assertTrue(AgentRetryPolicy.classify(ConnectionError()).retryable)
        self.assertTrue(AgentRetryPolicy.classify(httpx.ReadTimeout("slow ollama")).retryable)
        db_error = OperationalError("SELECT 1", {}, Exception(1213, "deadlock"))
        self.assertTrue(AgentRetryPolicy.classify(db_error).retryable)

    def test_non_transient_database_error_is_not_retryable(self):
        db_error = OperationalError("SELECT broken", {}, Exception(1064, "syntax error"))
        decision = AgentRetryPolicy.classify(db_error)
        self.assertFalse(decision.retryable)
        self.assertEqual(decision.reason_code, "DATABASE_NON_TRANSIENT")

    def test_retryable_http_statuses_are_classified(self):
        class Response:
            status_code = 429

        class HttpFailure(Exception):
            response = Response()

        decision = AgentRetryPolicy.classify(HttpFailure())
        self.assertTrue(decision.retryable)
        self.assertEqual(decision.reason_code, "HTTP_429")

    def test_business_and_validation_errors_are_not_retryable(self):
        self.assertFalse(AgentRetryPolicy.classify(ValueError("bad schema")).retryable)
        self.assertFalse(AgentRetryPolicy.classify(PermissionError("denied")).retryable)
        self.assertFalse(AgentRetryPolicy.classify(RuntimeError("agent rejected")).retryable)


if __name__ == "__main__":
    unittest.main()
