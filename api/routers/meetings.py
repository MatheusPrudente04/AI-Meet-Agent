import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from api.schemas.meeting import (
    DecisionSchema, MeetingDetailResponse, MeetingStatusResponse, SummarySchema, TaskSchema, UploadResponse,
)
from api.security import Principal, current_principal
from config.settings import get_settings
from domain.entities.meeting import Meeting, MeetingStatus
from infrastructure.email.email_sender import EmailSender
from infrastructure.exporters.markdown_exporter import MarkdownExporter
from presentation.container import get_container

router = APIRouter()
_NOT_FOUND = {404: {"description": "Reunião não existe ou é de outro usuário"}}


def _meeting(meeting_id: str, principal: Principal) -> Meeting:
    meeting = get_container().meeting_for(meeting_id, principal.owner_id)
    if meeting is None:
        raise HTTPException(404, "Reunião não encontrada")
    return meeting


@router.get("/", response_model=list[MeetingStatusResponse], summary="Lista as reuniões do usuário")
def list_meetings(principal: Principal = Depends(current_principal)):
    """Da mais recente para a mais antiga, sem resumo nem transcrição."""
    meetings = get_container().repository.list_all(principal.owner_id)
    return [_to_response(m, with_summary=False) for m in meetings]


AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".webm", ".ogg", ".flac", ".mp4", ".aac"}
_BLOCK = 1024 * 1024


@router.post("/upload", response_model=UploadResponse, summary="Envia um áudio para processar", responses={
    413: {"description": "Arquivo acima de MAX_UPLOAD_MB"}, 415: {"description": "Formato não aceito"},
})
def upload_meeting(
    file: UploadFile = File(...), title: str = "Reunião sem título",
    principal: Principal = Depends(current_principal),
):
    """Recebe áudio, salva e despacha o processamento. Acompanhe por GET /meetings/{id}/status."""
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

    get_container().enqueue_processing(meeting_id, str(audio_path), title, owner_id=principal.owner_id or "")
    return UploadResponse(
        meeting_id=meeting_id,
        status=MeetingStatus.PENDING,
        message="Áudio recebido. Processamento iniciado.",
    )


class SendBotRequest(BaseModel):
    meeting_url: str = Field(examples=["https://meet.google.com/abc-defg-hij"],
                             description="Link de uma das plataformas de GET /config")
    title: str = Field("", description="Vazio = título do evento na agenda (se ligada) ou a data")


@router.post("/bot", response_model=UploadResponse, summary="Manda o bot para uma reunião",
             responses={422: {"description": "Link não reconhecido ou plataforma sem suporte"}})
def send_bot(body: SendBotRequest, principal: Principal = Depends(current_principal)):
    """Enfileira o bot e responde na hora com o id. Quem organiza a reunião
    precisa admitir o bot quando ele pedir para entrar."""
    try:
        meeting_id = get_container().dispatch_bot(body.meeting_url, body.title, owner_id=principal.owner_id or "")
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return UploadResponse(
        meeting_id=meeting_id, status=MeetingStatus.PENDING, message="Bot enviado para a reunião.",
    )


@router.get("/{meeting_id}", response_model=MeetingDetailResponse, responses=_NOT_FOUND,
            summary="Reunião completa: resumo, transcrição e conversa")
def get_meeting(meeting_id: str, principal: Principal = Depends(current_principal)):
    m = _meeting(meeting_id, principal)
    return MeetingDetailResponse(
        **_to_response(m, with_summary=True).model_dump(),
        transcript=m.transcript_formatted or m.transcript_text,
        chat_history=m.chat_history,
        has_audio=bool(m.audio_path and Path(m.audio_path).exists()),
    )


@router.delete("/{meeting_id}", status_code=204, responses=_NOT_FOUND, summary="Apaga a reunião e a gravação")
def delete_meeting(meeting_id: str, principal: Principal = Depends(current_principal)):
    _meeting(meeting_id, principal)
    get_container().repository.delete(meeting_id)
    for f in Path(get_settings().audio_storage_path).glob(f"{meeting_id}.*"):
        f.unlink(missing_ok=True)


@router.post("/{meeting_id}/process", response_model=UploadResponse, summary="Reprocessa o áudio já gravado",
             responses={**_NOT_FOUND, 409: {"description": "Reunião sem áudio gravado"}})
def reprocess(meeting_id: str, principal: Principal = Depends(current_principal)):
    """Refaz transcrição e resumo (após erro, ou se o bot caiu no meio)."""
    meeting = _meeting(meeting_id, principal)
    if not meeting.audio_path or not Path(meeting.audio_path).exists():
        raise HTTPException(409, "Reunião sem áudio gravado")
    get_container().enqueue_processing(meeting_id, meeting.audio_path, meeting.title)
    return UploadResponse(
        meeting_id=meeting_id, status=MeetingStatus.PENDING, message="Reprocessamento iniciado.",
    )


@router.get("/{meeting_id}/status", response_model=MeetingStatusResponse, responses=_NOT_FOUND,
            summary="Andamento do processamento")
def get_status(meeting_id: str, principal: Principal = Depends(current_principal)):
    """Leve, para consultar a cada poucos segundos enquanto status não for `done` ou `error`."""
    return _to_response(_meeting(meeting_id, principal), with_summary=True)


class TaskDoneRequest(BaseModel):
    done: bool


@router.patch("/{meeting_id}/tasks/{index}", response_model=MeetingStatusResponse, responses=_NOT_FOUND,
              summary="Marca ou desmarca uma tarefa")
def set_task_done(meeting_id: str, index: int, body: TaskDoneRequest,
                  principal: Principal = Depends(current_principal)):
    """`index` é a posição da tarefa em `summary.tasks`, a partir de 0."""
    try:
        meeting = get_container().set_task_done(meeting_id, index, body.done, principal.owner_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    return _to_response(meeting, with_summary=True)


@router.get("/{meeting_id}/export.md", response_class=PlainTextResponse, responses=_NOT_FOUND,
            summary="Reunião em Markdown")
def export_markdown(meeting_id: str, principal: Principal = Depends(current_principal)):
    meeting = _meeting(meeting_id, principal)
    return PlainTextResponse(MarkdownExporter().render(meeting), media_type="text/markdown; charset=utf-8")


class EmailTasksRequest(BaseModel):
    to: str


class EmailTasksResponse(BaseModel):
    ok: bool
    to: str


@router.post("/{meeting_id}/tasks/email", response_model=EmailTasksResponse, summary="Envia as tarefas por e-mail",
             responses={**_NOT_FOUND, 409: {"description": "SMTP não configurado ou reunião sem tarefas"}})
def email_tasks(meeting_id: str, body: EmailTasksRequest, principal: Principal = Depends(current_principal)):
    meeting = _meeting(meeting_id, principal)
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
        platform=m.platform,
        started_at=m.started_at,
        duration_minutes=m.duration_minutes,
        participants=m.participants,
        summary=summary,
    )
