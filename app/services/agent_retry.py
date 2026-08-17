from __future__ import annotations

from dataclasses import dataclass

import httpx
from sqlalchemy.exc import DisconnectionError, OperationalError


@dataclass(frozen=True)
class AgentRetryDecision:
    retryable: bool
    reason_code: str


class AgentRetryPolicy:
    """Classifies Agent execution failures without relying on exception messages."""

    RETRYABLE_HTTP_STATUSES = {408, 425, 429}
    RETRYABLE_MYSQL_CODES = {1040, 1205, 1213, 2006, 2013}

    @classmethod
    def classify(cls, exc: Exception) -> AgentRetryDecision:
        if isinstance(exc, (TimeoutError, ConnectionError, httpx.TimeoutException, httpx.NetworkError)):
            return AgentRetryDecision(True, "TRANSIENT_NETWORK")
        if isinstance(exc, DisconnectionError):
            return AgentRetryDecision(True, "DATABASE_DISCONNECTED")
        if isinstance(exc, OperationalError):
            database_code = cls._database_error_code(exc)
            if database_code in cls.RETRYABLE_MYSQL_CODES:
                return AgentRetryDecision(True, f"DATABASE_{database_code}")
            return AgentRetryDecision(False, "DATABASE_NON_TRANSIENT")

        status_code = cls._status_code(exc)
        if status_code in cls.RETRYABLE_HTTP_STATUSES:
            return AgentRetryDecision(True, f"HTTP_{status_code}")
        if status_code is not None and status_code >= 500:
            return AgentRetryDecision(True, "HTTP_5XX")
        if status_code is not None:
            return AgentRetryDecision(False, f"HTTP_{status_code}")

        if isinstance(exc, PermissionError):
            return AgentRetryDecision(False, "PERMISSION_DENIED")
        if isinstance(exc, (ValueError, TypeError, KeyError, AssertionError)):
            return AgentRetryDecision(False, "INVALID_EXECUTION_STATE")
        if isinstance(exc, RuntimeError):
            return AgentRetryDecision(False, "AGENT_REJECTED_OR_BUSINESS_ERROR")
        return AgentRetryDecision(False, "UNCLASSIFIED_NON_RETRYABLE")

    @staticmethod
    def _status_code(exc: Exception) -> int | None:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int) and not isinstance(status_code, bool):
            return status_code
        direct_status = getattr(exc, "status_code", None)
        return direct_status if isinstance(direct_status, int) and not isinstance(direct_status, bool) else None

    @staticmethod
    def _database_error_code(exc: OperationalError) -> int | None:
        arguments = getattr(getattr(exc, "orig", None), "args", ())
        if arguments and isinstance(arguments[0], int) and not isinstance(arguments[0], bool):
            return arguments[0]
        return None
