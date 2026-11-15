from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

from domain.entities.meeting import MeetingStatus


class TaskSchema(BaseModel):
    description: str
    responsible: str
    deadline: Optional[str] = None
    done: bool = False


class DecisionSchema(BaseModel):
    description: str
    context: str = ""


class SummarySchema(BaseModel):
    overview: str
    topics: list[str]
    tasks: list[TaskSchema]
    decisions: list[DecisionSchema]


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class MeetingStatusResponse(BaseModel):
    meeting_id: str
    status: MeetingStatus = Field(description="pending → recording → transcribing → summarizing → done | error")
    error: str = Field("", description="Motivo, quando status = error")
    title: str
    platform: str = Field("", description="meet, teams ou zoom; vazio para áudio enviado")
    started_at: datetime
    duration_minutes: float = 0.0
    participants: list[str] = []
    summary: Optional[SummarySchema] = Field(None, description="Ausente na listagem e enquanto não houver resumo")


class MeetingDetailResponse(MeetingStatusResponse):
    transcript: str = Field("", description="Transcrição com o nome de quem falou em cada trecho")
    chat_history: list[ChatMessage] = []
    has_audio: bool = Field(False, description="Há gravação em disco: a reunião pode ser reprocessada")


class UploadResponse(BaseModel):
    meeting_id: str
    status: MeetingStatus
    message: str


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, examples=["Quais tarefas ficaram com a Maria?"])
    history: Optional[list[ChatMessage]] = Field(
        None, description="Omitido (recomendado) = usa e atualiza a conversa guardada na reunião.",
    )


class ChatResponse(BaseModel):
    answer: str
    meeting_id: str


class PlatformSchema(BaseModel):
    key: str
    label: str
    example: str = Field(description="Formato de link aceito")
    validated: bool = Field(description="Falso = suporte ainda não validado em reunião real")
    speaker_names: bool = Field(description="A transcrição traz o nome de quem falou")


class ConfigResponse(BaseModel):
    login_enabled: bool = Field(description="Falso em APP_MODE=solo: não há usuários")
    registration_open: bool
    email_enabled: bool = Field(description="POST /meetings/{id}/tasks/email disponível")
    calendar_enabled: bool = Field(description="Título do bot pode ser omitido: vem da agenda")
    max_upload_mb: int
    audio_formats: list[str]
    platforms: list[PlatformSchema]
