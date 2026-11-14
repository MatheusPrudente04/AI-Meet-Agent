"""Ciclo de vida de uma gravação: entrar, gravar, despachar o processamento.

É o corpo tanto da task Celery `join_meeting` (collab) quanto do CLI `run_bot`.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from config.settings import get_settings
from domain.entities.meeting import Meeting, MeetingStatus
from infrastructure.recorder.playwright_bot_recorder import PlaywrightBotRecorder

logger = logging.getLogger(__name__)


def build_recorder(settings=None, **overrides) -> PlaywrightBotRecorder:
    settings = settings or get_settings()
    kwargs = dict(
        chrome_profile_dir=settings.bot_chrome_profile,
        audio_dir=settings.audio_storage_path,
        bot_name=settings.bot_name,
        headless=settings.bot_headless,
        capture_endpoint=settings.capture_url,
    )
    kwargs.update(overrides)
    return PlaywrightBotRecorder(**kwargs)


def run_bot_job(
    meeting_id: str,
    meeting_url: str,
    title: str,
    recorder: Optional[PlaywrightBotRecorder] = None,
) -> Meeting:
    from presentation.container import get_container

    container = get_container()
    repo = container.repository
    settings = get_settings()

    audio = Path(settings.audio_storage_path) / f"{meeting_id}.webm"

    meeting = repo.find_by_id(meeting_id) or Meeting(id=meeting_id, title=title)
    meeting.status, meeting.error = MeetingStatus.PENDING, ""
    meeting.audio_path = audio.as_posix()  # permite reprocessar mesmo se o bot for morto
    repo.save(meeting)

    def on_recording_started() -> None:
        meeting.status = MeetingStatus.RECORDING
        meeting.started_at = datetime.now()
        repo.save(meeting)
        logger.info("[%s] status=recording", meeting_id)

    recorder = recorder or build_recorder(settings)
    recorder._on_recording_started = on_recording_started

    bot_error = ""
    try:
        asyncio.run(recorder.join_async(meeting_url, session_id=meeting_id))
    except Exception as exc:
        logger.exception("[%s] bot falhou", meeting_id)
        bot_error = str(exc)

    # O áudio chega em chunks durante a reunião: se o bot caiu no meio,
    # o que foi gravado até ali ainda é processado.
    if audio.exists() and audio.stat().st_size > 0:
        container.enqueue_processing(meeting_id, audio.as_posix(), title, wait=True)
    else:
        meeting.status = MeetingStatus.ERROR
        meeting.error = f"Bot não gravou áudio. {bot_error}".strip()
        repo.save(meeting)
        logger.error("[%s] %s", meeting_id, meeting.error)

    return repo.find_by_id(meeting_id) or meeting
