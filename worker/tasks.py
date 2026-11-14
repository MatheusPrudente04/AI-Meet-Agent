"""worker/tasks.py — task Celery do processamento de reuniões (modo collab)."""
from celery import Celery

from config.settings import get_settings

settings = get_settings()

celery_app = Celery("meet_agent", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="America/Sao_Paulo",
    task_track_started=True,
    # O bot tem fila própria: um worker de bot grava uma reunião por vez (ADR-006).
    task_routes={"join_meeting": {"queue": "bot"}},
)


@celery_app.task(name="join_meeting")
def join_meeting_task(meeting_id: str, meeting_url: str, title: str) -> dict:
    from infrastructure.recorder.bot_job import run_bot_job

    meeting = run_bot_job(meeting_id, meeting_url, title)
    return {"meeting_id": meeting.id, "status": meeting.status.value, "error": meeting.error}


@celery_app.task(name="process_meeting")
def process_meeting_task(meeting_id: str, audio_path: str, title: str) -> dict:
    # Sem retry automático: falhas ficam no documento (status=error) e
    # reprocessar o mesmo id é seguro.
    from presentation.container import get_container
    from use_cases.process_meeting import ProcessMeetingInput

    meeting = get_container().process_meeting.execute(
        ProcessMeetingInput(meeting_id, audio_path, title)
    )
    return {"meeting_id": meeting.id, "status": meeting.status.value, "error": meeting.error}
