from __future__ import annotations

from importlib.util import find_spec

from sqlalchemy.orm import Session

from app.agents.runtime import AgentRuntimeService
from app.core.config import Settings


def create_agent_runtime(db: Session, settings: Settings) -> AgentRuntimeService:
    if wants_langgraph(settings) and langgraph_available():
        from app.agents.langgraph_runtime import LangGraphAgentRuntimeService

        return LangGraphAgentRuntimeService(db, settings)
    return AgentRuntimeService(db, settings)


def agent_framework_status(settings: Settings) -> dict:
    requested = settings.agent_framework.lower()
    available = langgraph_available()
    active = "langgraph" if requested == "langgraph" and available else "custom"
    return {
        "requested": requested,
        "active": active,
        "langgraphAvailable": available,
        "fallback": active != requested,
    }


def wants_langgraph(settings: Settings) -> bool:
    return settings.agent_framework.lower() == "langgraph"


def langgraph_available() -> bool:
    return find_spec("langgraph") is not None
