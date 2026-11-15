"""worker/tasks.py — task Celery do processamento de reuniões (modo collab)."""
import logging
import os

from celery import Celery
from celery.signals import setup_logging as celery_setup_logging
from celery.signals import worker_ready

from config.logging_setup import bind_meeting, setup_logging
from config.settings import get_settings

settings = get_settings()

celery_app = Celery("meet_agent", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="America/Sao_Paulo",
    task_track_started=True,
    # Uma fila por etapa (ADR-015): transcrição e LLM não esperam uma pela outra, e cada
    # uma escala com seus próprios workers. O bot grava uma reunião por worker (ADR-006).
    task_routes={
        "join_meeting": {"queue": "bot"},
        "setup_bot": {"queue": "bot"},   # usa o mesmo perfil do Chrome: uma coisa por vez
        "process_meeting": {"queue": "transcribe"},
        "summarize_meeting": {"queue": "llm"},
    },
    # Prioridade 0 a 9 (0 primeiro): é como um usuário com muitos envios não passa na
    # frente do primeiro envio de outro (container._priority).
    broker_transport_options={
        "priority_steps": list(range(10)),
        "queue_order_strategy": "priority",
        "sep": ":",
        # Mais que o trabalho mais longo (transcrição de reunião longa); com acks_late,
        # uma tarefa só volta para a fila se o worker morrer, não por demorar.
        "visibility_timeout": 6 * 3600,
    },
    # Um worker reserva só a tarefa que está executando: a próxima fica na fila,
    # disponível para outro worker livre e para a ordem de prioridade.
    worker_prefetch_multiplier=1,
    task_acks_late=True,
)


@celery_setup_logging.connect
def _use_project_logging(**_):
    """Substitui o formato do Celery pelo do projeto (id da reunião em toda linha)."""
    setup_logging()


@celery_app.task(name="join_meeting")
def join_meeting_task(meeting_id: str, meeting_url: str, title: str) -> dict:
    from domain.entities.meeting import MeetingStatus
    from infrastructure.recorder.bot_job import run_bot_job
    from presentation.container import get_container

    bind_meeting(meeting_id)
    # Com acks_late, a tarefa de um worker que morreu volta para a fila; se a reunião
    # já passou dessa etapa, não reentra (a gravação de antes seria sobrescrita).
    current = get_container().repository.find_by_id(meeting_id)
    if current is not None and current.status is not MeetingStatus.PENDING:
        logging.getLogger(__name__).warning("[%s] bot já atuou nesta reunião (status=%s); ignorando",
                                            meeting_id, current.status.value)
        return {"meeting_id": meeting_id, "status": current.status.value, "error": current.error}
    meeting = run_bot_job(meeting_id, meeting_url, title)
    return {"meeting_id": meeting.id, "status": meeting.status.value, "error": meeting.error}


# Sem retry automático: falhas ficam no documento (status=error) e reprocessar o
# mesmo id é seguro (ProcessMeetingUC é idempotente) — inclusive a reentrega do acks_late.
@celery_app.task(name="process_meeting")
def process_meeting_task(meeting_id: str, audio_path: str, title: str) -> dict:
    """Etapa 1: transcrição. Ao terminar, põe a reunião na fila do LLM."""
    from domain.entities.meeting import MeetingStatus
    from presentation.container import get_container
    from use_cases.process_meeting import ProcessMeetingInput

    bind_meeting(meeting_id)
    container = get_container()
    meeting = container.process_meeting.transcribe(ProcessMeetingInput(meeting_id, audio_path, title))
    if meeting.status is not MeetingStatus.ERROR:
        container.enqueue(meeting, "llm")
    return {"meeting_id": meeting.id, "status": meeting.status.value, "error": meeting.error}


@celery_app.task(name="summarize_meeting")
def summarize_meeting_task(meeting_id: str) -> dict:
    """Etapa 2: resumo."""
    from presentation.container import get_container

    bind_meeting(meeting_id)
    meeting = get_container().process_meeting.summarize(meeting_id)
    return {"meeting_id": meeting.id, "status": meeting.status.value, "error": meeting.error}


@celery_app.task(name="setup_bot")
def setup_bot_task(check_only: bool = False) -> str:
    """Credencia a conta do Google do bot (botão "Credenciar bot") ou só confere (check_only)."""
    import asyncio
    from infrastructure.recorder.bot_setup import credential
    from presentation.container import get_container

    settings = get_settings()
    store = get_container().bot_account
    return asyncio.run(credential(store.set, settings.bot_google_email, settings.bot_google_password,
                                  check_only=check_only))


@worker_ready.connect
def _check_bot_account(**_):
    """Worker do bot no ar: confere se o perfil está logado, para a interface saber se pode
    mandar o bot (o perfil pode ter sido logado à mão ou ter perdido a sessão)."""
    if os.environ.get("MEET_AGENT_BOT_WORKER"):
        setup_bot_task.delay(check_only=True)
