"""
container.py — único ponto de composição (injeção de dependência).

Os modos diferem só em dois pontos (ADR-004):
  collab (produção)      → MongoDB + fila Celery
  solo (desenvolvimento) → JSON local + thread
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from functools import lru_cache
from typing import Optional

from config.settings import get_settings
from domain.entities.meeting import Meeting
from domain.entities.user import User
from infrastructure.llm.langchain_llm_service import LangChainLLMService
from use_cases.chat_with_meeting import ChatWithMeetingUC
from use_cases.process_meeting import ProcessMeetingInput, ProcessMeetingUC
from use_cases.summarize_meeting import SummarizeMeetingUC
from use_cases.transcribe_meeting import TranscribeMeetingUC

logger = logging.getLogger(__name__)


def _build_transcriber(settings):
    if settings.use_local_whisper:
        from infrastructure.transcriber.whisper_local_transcriber import WhisperLocalTranscriber
        return WhisperLocalTranscriber()

    if settings.use_real_diarization:
        from infrastructure.transcriber.whisper_with_diarization import WhisperWithDiarization
        return WhisperWithDiarization()

    from infrastructure.transcriber.whisper_transcriber import WhisperTranscriber
    return WhisperTranscriber()


def _build_repository(settings):
    if settings.is_collab:
        from infrastructure.mongo_meeting_repository import MongoMeetingRepository
        return MongoMeetingRepository(settings.mongo_uri, settings.mongo_db)

    from infrastructure.json_meeting_repository import JsonMeetingRepository
    return JsonMeetingRepository(settings.storage_path)


class Container:
    def __init__(self) -> None:
        settings = get_settings()
        llm = LangChainLLMService()

        self._is_collab = settings.is_collab
        # Opcional (ENABLE_CALENDAR): dá título à reunião a partir da agenda.
        self.calendar = None
        if settings.enable_calendar:
            from infrastructure.calendar.google_calendar_service import GoogleCalendarService
            self.calendar = GoogleCalendarService(settings.google_credentials_path, settings.google_token_path)
        self.repository = _build_repository(settings)
        # Login de usuários (ADR-011): só em collab. Em solo não há login.
        self.auth = None
        if settings.is_collab:
            from infrastructure.auth_store import MongoAuthStore
            from use_cases.auth import AuthService
            self.auth = AuthService(
                MongoAuthStore(settings.mongo_uri, settings.mongo_db),
                session_days=settings.session_days,
                allow_registration=settings.allow_registration,
            )
        self.transcribe_meeting = TranscribeMeetingUC(_build_transcriber(settings))
        self.summarize_meeting = SummarizeMeetingUC(llm, max_context_chars=settings.llm_context_chars)
        self.chat_with_meeting = ChatWithMeetingUC(
            llm, repository=self.repository, max_context_chars=settings.llm_context_chars,
        )
        from infrastructure.recorder.speaker_events import load_speaker_events
        self.process_meeting = ProcessMeetingUC(
            self.transcribe_meeting, self.summarize_meeting, self.repository,
            load_speaker_events=lambda audio: load_speaker_events(audio, exclude=(settings.bot_name, "Você", "You")),
        )

    def register_user(self, name: str, email: str, password: str) -> User:
        """Cadastra. O primeiro usuário herda as reuniões gravadas antes de existir login."""
        user = self.auth.register(name, email, password)
        if self.auth.is_first_user():
            for meeting in self.repository.list_all(owner_id=""):
                meeting.owner_id = user.id
                self.repository.save(meeting)
        return user

    def meeting_for(self, meeting_id: str, owner_id: Optional[str]) -> Optional[Meeting]:
        """A reunião, se existir e for de `owner_id` (None = sem restrição de dono)."""
        meeting = self.repository.find_by_id(meeting_id)
        if meeting is None or (owner_id is not None and meeting.owner_id != owner_id):
            return None
        return meeting

    def enqueue_processing(
        self, meeting_id: str, audio_path: str, title: str, wait: bool = False, owner_id: str = "",
    ) -> None:
        """Registra a reunião como pendente e despacha o processamento.

        wait=True é para processos que encerram logo em seguida (CLI, run_bot):
        em solo roda na própria chamada, porque uma thread não sobrevive ao fim
        do interpretador. Em collab é ignorado — a fila é quem executa.
        """
        meeting = self.repository.find_by_id(meeting_id) or Meeting(id=meeting_id, title=title, owner_id=owner_id)
        meeting.audio_path = audio_path
        self.repository.save(meeting)

        if self._is_collab:
            from worker.tasks import process_meeting_task
            process_meeting_task.delay(meeting_id, audio_path, title)
            return

        data = ProcessMeetingInput(meeting_id, audio_path, title)
        if wait:
            self.process_meeting.execute(data)
        else:
            threading.Thread(
                target=self.process_meeting.execute, args=(data,),
                name=f"process-{meeting_id[:8]}", daemon=True,
            ).start()


    def set_task_done(self, meeting_id: str, index: int, done: bool, owner_id: Optional[str] = None) -> Meeting:
        """Marca ou desmarca uma tarefa da reunião e grava."""
        meeting = self.meeting_for(meeting_id, owner_id)
        if meeting is None:
            raise LookupError("Reunião não encontrada")
        tasks = meeting.summary.tasks if meeting.summary else []
        if not 0 <= index < len(tasks):
            raise LookupError("Tarefa não encontrada")
        tasks[index].done = done
        self.repository.save(meeting)
        return meeting

    def _title_from_calendar(self, meeting_url: str) -> str:
        if self.calendar is None:
            return ""
        try:
            event = self.calendar.find_by_meet_url(meeting_url)
            return event.title if event else ""
        except Exception as exc:  # agenda é um extra: nunca impede o bot de entrar
            logger.warning("Agenda indisponível: %s", exc)
            return ""

    def dispatch_bot(self, meeting_url: str, title: str = "", owner_id: str = "") -> str:
        """Manda o bot para a reunião sem bloquear. Retorna o id da reunião.
        Sem título, usa o do evento na agenda (se ligada) ou a data."""
        from infrastructure.recorder.platforms import detect_platform
        platform = detect_platform(meeting_url)  # ValueError com mensagem para o usuário
        title = (title or "").strip() or self._title_from_calendar(meeting_url) \
            or f"Reunião {datetime.now():%d/%m/%Y %H:%M}"

        meeting_id = str(uuid.uuid4())
        self.repository.save(Meeting(id=meeting_id, title=title, owner_id=owner_id, platform=platform.key))

        if self._is_collab:
            from worker.tasks import join_meeting_task
            join_meeting_task.delay(meeting_id, meeting_url, title)
        else:
            subprocess.Popen([
                sys.executable, "-m", "infrastructure.recorder.run_bot",
                "--url", meeting_url, "--title", title, "--meeting-id", meeting_id,
            ])
        return meeting_id


@lru_cache(maxsize=1)
def get_container() -> Container:
    return Container()
