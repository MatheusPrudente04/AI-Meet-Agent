import re
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from api.schemas.meeting import (
    DecisionSchema, MeetingStatusResponse, SummarySchema, TaskSchema, UploadResponse,
)
from config.settings import get_settings
from domain.entities.meeting import Meeting, MeetingStatus
from infrastructure.email.email_sender import EmailSender
from infrastructure.exporters.markdown_exporter import MarkdownExporter
from presentation.container import get_container

router = APIRouter()


@router.get("/", response_model=list[MeetingStatusResponse])
def list_meetings():
    return [_to_response(m, with_summary=False) for m in get_container().repository.list_all()]


AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".webm", ".ogg", ".flac", ".mp4", ".aac"}
_BLOCK = 1024 * 1024


@router.post("/upload", response_model=UploadResponse)
def upload_meeting(file: UploadFile = File(...), title: str = "Reunião sem título"):
    """Recebe áudio, salva e despacha o processamento. Acompanhe por GET /{id}/status."""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in AUDIO_SUFFIXES:
        raise HTTPException(415, f"Formato não aceito. Use um de: {', '.join(sorted(AUDIO_SUFFIXES))}")

    settings = get_settings()
    limit = settings.max_upload_mb * 1024 * 1024
    meeting_id = str(uuid.uuid4())
    audio_dir = Path(settings.audio_storage_path)
    audio_dir.mkdir(parents=True, exist_ok=True)
    audio_path = audio_dir / f"{meeting_id}{suffix}"

    # Copia em blocos: o arquivo nunca fica inteiro em memória e o teto vale
    # mesmo que o cliente minta no Content-Length.
    written = 0
    with open(audio_path, "wb") as out:
        while block := file.file.read(_BLOCK):
            written += len(block)
            if written > limit:
                out.close()
                audio_path.unlink(missing_ok=True)
                raise HTTPException(413, f"Arquivo acima do limite de {settings.max_upload_mb} MB")
            out.write(block)
    if written == 0:
        audio_path.unlink(missing_ok=True)
        raise HTTPException(422, "Arquivo vazio")

    get_container().enqueue_processing(meeting_id, str(audio_path), title)
    return UploadResponse(
        meeting_id=meeting_id,
        status=MeetingStatus.PENDING,
        message="Áudio recebido. Processamento iniciado.",
    )


class SendBotRequest(BaseModel):
    meeting_url: str
    title: str = ""


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


class TaskDoneRequest(BaseModel):
    done: bool


@router.patch("/{meeting_id}/tasks/{index}", response_model=MeetingStatusResponse)
def set_task_done(meeting_id: str, index: int, body: TaskDoneRequest):
    try:
        meeting = get_container().set_task_done(meeting_id, index, body.done)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    return _to_response(meeting, with_summary=True)


@router.get("/{meeting_id}/export.md", response_class=PlainTextResponse)
def export_markdown(meeting_id: str):
    meeting = get_container().repository.find_by_id(meeting_id)
    if not meeting:
        raise HTTPException(404, "Reunião não encontrada")
    return PlainTextResponse(MarkdownExporter().render(meeting), media_type="text/markdown; charset=utf-8")


class EmailTasksRequest(BaseModel):
    to: str


@router.post("/{meeting_id}/tasks/email")
def email_tasks(meeting_id: str, body: EmailTasksRequest):
    meeting = get_container().repository.find_by_id(meeting_id)
    if not meeting:
        raise HTTPException(404, "Reunião não encontrada")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", body.to):
        raise HTTPException(422, "E-mail inválido")
    try:
        EmailSender().send_tasks_email(body.to, meeting)
    except RuntimeError as exc:  # SMTP desligado ou reunião sem tarefas
        raise HTTPException(409, str(exc))
    return {"ok": True, "to": body.to}


def _to_response(m: Meeting, with_summary: bool) -> MeetingStatusResponse:
    summary = None
    if with_summary and m.summary:
        summary = SummarySchema(
            overview=m.summary.overview,
            topics=m.summary.topics,
            tasks=[
                TaskSchema(description=t.description, responsible=t.responsible, deadline=t.deadline, done=t.done)
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
