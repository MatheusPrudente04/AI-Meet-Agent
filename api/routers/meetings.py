import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from api.schemas.meeting import (
    DecisionSchema, MeetingStatusResponse, SummarySchema, TaskSchema, UploadResponse,
)
from config.settings import get_settings
from domain.entities.meeting import Meeting, MeetingStatus
from presentation.container import get_container

router = APIRouter()


@router.get("/", response_model=list[MeetingStatusResponse])
def list_meetings():
    return [_to_response(m, with_summary=False) for m in get_container().repository.list_all()]


@router.post("/upload", response_model=UploadResponse)
def upload_meeting(file: UploadFile = File(...), title: str = "Reunião sem título"):
    """Recebe áudio, salva e despacha o processamento. Acompanhe por GET /{id}/status."""
    meeting_id = str(uuid.uuid4())
    audio_dir = Path(get_settings().audio_storage_path)
    audio_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "audio.wav").suffix
    audio_path = audio_dir / f"{meeting_id}{suffix}"
    audio_path.write_bytes(file.file.read())

    get_container().enqueue_processing(meeting_id, str(audio_path), title)
    return UploadResponse(
        meeting_id=meeting_id,
        status=MeetingStatus.PENDING,
        message="Áudio recebido. Processamento iniciado.",
    )


class SendBotRequest(BaseModel):
    meeting_url: str
    title: str = "Reunião"


@router.post("/bot", response_model=UploadResponse)
def send_bot(body: SendBotRequest):
    """Enfileira o bot para entrar na reunião. Responde na hora com o id."""
    try:
        meeting_id = get_container().dispatch_bot(body.meeting_url, body.title)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return UploadResponse(
        meeting_id=meeting_id, status=MeetingStatus.PENDING, message="Bot enviado para a reunião.",
    )


@router.post("/{meeting_id}/process", response_model=UploadResponse)
def reprocess(meeting_id: str):
    """Reprocessa o áudio já gravado (após erro, ou se o bot caiu no meio)."""
    meeting = get_container().repository.find_by_id(meeting_id)
    if not meeting:
        raise HTTPException(404, "Reunião não encontrada")
    if not meeting.audio_path or not Path(meeting.audio_path).exists():
        raise HTTPException(409, "Reunião sem áudio gravado")
    get_container().enqueue_processing(meeting_id, meeting.audio_path, meeting.title)
    return UploadResponse(
        meeting_id=meeting_id, status=MeetingStatus.PENDING, message="Reprocessamento iniciado.",
    )


@router.get("/{meeting_id}/status", response_model=MeetingStatusResponse)
def get_status(meeting_id: str):
    meeting = get_container().repository.find_by_id(meeting_id)
    if not meeting:
        raise HTTPException(404, "Reunião não encontrada")
    return _to_response(meeting, with_summary=True)


def _to_response(m: Meeting, with_summary: bool) -> MeetingStatusResponse:
    summary = None
    if with_summary and m.summary:
        summary = SummarySchema(
            overview=m.summary.overview,
            topics=m.summary.topics,
            tasks=[
                TaskSchema(description=t.description, responsible=t.responsible, deadline=t.deadline)
                for t in m.summary.tasks
            ],
            decisions=[
                DecisionSchema(description=d.description, context=d.context)
                for d in m.summary.decisions
            ],
        )
    return MeetingStatusResponse(
        meeting_id=m.id,
        status=m.status,
        error=m.error,
        title=m.title,
        started_at=m.started_at,
        duration_minutes=m.duration_minutes,
        participants=m.participants,
        summary=summary,
    )
