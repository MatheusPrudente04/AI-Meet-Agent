from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from domain.entities.meeting import MeetingStatus


class TaskSchema(BaseModel):
    description: str
    responsible: str
    deadline: Optional[str] = None


class DecisionSchema(BaseModel):
    description: str
    context: str = ""


class SummarySchema(BaseModel):
    overview: str
    topics: list[str]
    tasks: list[TaskSchema]
    decisions: list[DecisionSchema]


class MeetingStatusResponse(BaseModel):
    meeting_id: str
    status: MeetingStatus
    error: str = ""
    title: str
    started_at: datetime
    duration_minutes: float = 0.0
    participants: list[str] = []
    summary: Optional[SummarySchema] = None


class UploadResponse(BaseModel):
    meeting_id: str
    status: MeetingStatus
    message: str


class ChatRequest(BaseModel):
    question: str
    history: list[dict] = []


class ChatResponse(BaseModel):
    answer: str
    meeting_id: str
