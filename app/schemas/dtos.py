from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    sessionId: Optional[str] = None


class ChatStreamEvent(BaseModel):
    sessionId: Optional[str] = None
    content: Optional[str] = None
    message: Optional[str] = None
    type: str


class KnowledgeIngestRequest(BaseModel):
    source: str
    content: str


class KnowledgeIngestResponse(BaseModel):
    source: str
    chunks: int


class ResponseReviewResolutionRequest(BaseModel):
    decision: Literal["APPROVED", "REJECTED"]
    approvalToken: str = Field(min_length=1)
    note: str = Field(default="", max_length=1000)


class ResponseReviewResolutionResponse(BaseModel):
    id: int
    reportId: Optional[int] = None
    status: str
    decision: str
    resolvedBy: str
    updatedAt: datetime


class ReportResponse(BaseModel):
    id: int
    sessionId: str
    username: str
    displayName: str
    content: str
    intent: str
    emotion: str
    emotionScore: float
    riskLevel: str
    confidence: float
    summary: str
    createdAt: datetime


class ConversationMessageResponse(BaseModel):
    role: str
    content: str
    createdAt: datetime


class ConversationResponse(BaseModel):
    sessionId: str
    title: str
    messages: list[ConversationMessageResponse]


class ToolRecordResponse(BaseModel):
    id: int
    reportId: int
    status: str
    message: str
    createdAt: datetime
    channel: Optional[str] = None
    recipient: Optional[str] = None
    filePath: Optional[str] = None


class RiskCaseResponse(BaseModel):
    id: int
    reportId: int
    riskLevel: str
    status: str
    owner: str
    summary: str
    handoffSummary: str
    acknowledgedBy: Optional[str] = None
    acknowledgedAt: Optional[datetime] = None
    createdAt: datetime
    updatedAt: datetime


class CaseNoteResponse(BaseModel):
    id: int
    caseId: int
    actor: str
    note: str
    createdAt: datetime


class ToolJobResponse(BaseModel):
    id: int
    reportId: Optional[int] = None
    operationalAlertId: Optional[int] = None
    kind: str
    status: str
    attempts: int
    maxAttempts: int
    dependsOnJobId: Optional[int] = None
    runAfter: datetime
    lastError: str
    createdAt: datetime
    updatedAt: datetime


class DeadLetterResponse(BaseModel):
    id: int
    jobId: Optional[int] = None
    reportId: Optional[int] = None
    operationalAlertId: Optional[int] = None
    kind: str
    reason: str
    payload: str
    createdAt: datetime


class AgentRunTraceResponse(BaseModel):
    id: int
    sessionId: str
    reportId: Optional[int] = None
    username: str
    intent: str
    riskLevel: str
    originalInput: str
    sanitizedInput: str
    memoryBrief: str
    agentSteps: list[dict[str, Any]]
    runtimeEvents: list[dict[str, Any]]
    retrievedKnowledge: list[dict[str, Any]]
    responseMessages: list[dict[str, Any]]
    assessment: dict[str, Any]
    createdAt: datetime


class ToolAuditResponse(BaseModel):
    id: int
    jobId: Optional[int] = None
    reportId: Optional[int] = None
    toolName: str
    policy: str
    allowed: bool
    status: str
    reason: str
    payload: dict[str, Any]
    createdAt: datetime
    updatedAt: datetime


class AiMessage(BaseModel):
    role: str
    content: str


def authority(role: str) -> dict[str, Any]:
    return {"authority": role}
