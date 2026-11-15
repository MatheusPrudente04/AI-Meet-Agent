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
from pathlib import Path
from typing import Optional

from config.settings import get_settings
from domain.entities.meeting import Meeting, MeetingStatus
from domain.entities.user import User
from infrastructure.llm.meeting_agent import MeetingAgent
from use_cases.chat_with_meeting import ChatWithMeetingUC
from use_cases.process_meeting import ProcessMeetingInput, ProcessMeetingUC
from use_cases.summarize_meeting import SummarizeMeetingUC
from use_cases.transcribe_meeting import TranscribeMeetingUC

logger = logging.getLogger(__name__)

_ACTIVE = (MeetingStatus.PENDING, MeetingStatus.RECORDING, MeetingStatus.TRANSCRIBING, MeetingStatus.SUMMARIZING)
_FINAL = (MeetingStatus.DONE, MeetingStatus.ERROR)
# O que a reunião de quem chegou depois copia da gravação de quem mandou o bot primeiro.
_SHARED = ("status", "error", "started_at", "audio_path", "transcript_text", "transcript_formatted",
           "transcript_raw", "summary", "participants", "duration_minutes", "metrics")


class MeetingInProgress(Exception):
    """O usuário já mandou o bot a este link e a reunião ainda não acabou."""

    def __init__(self, meeting_id: str) -> None:
        super().__init__("O bot já está a caminho desta reunião ou gravando. Acompanhe a reunião já criada.")
        self.meeting_id = meeting_id


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
        llm = MeetingAgent()

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
            try:
                self.auth.ensure_admin()
            except Exception as exc:  # banco fora na partida: tenta de novo na próxima
                logger.warning("Não foi possível conferir o admin: %s", exc)
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

    def send_password_reset(self, email: str) -> None:
        """Envia o código de nova senha. Não diz se o e-mail tem conta.
        RuntimeError se o SMTP não estiver configurado."""
        from infrastructure.email.email_sender import EmailSender
        from use_cases.auth import RESET_MINUTES
        if not get_settings().email_enabled:
            raise RuntimeError("Recuperação de senha indisponível: o e-mail (SMTP) não está configurado.")
        result = self.auth.request_reset(email)
        if result:
            user, code = result
            EmailSender().send_password_reset(user.email, user.name, code, RESET_MINUTES)

    def meeting_for(self, meeting_id: str, owner_id: Optional[str]) -> Optional[Meeting]:
        """A reunião, se existir e for de `owner_id` (None = sem restrição de dono)."""
        meeting = self.repository.find_by_id(meeting_id)
        if meeting is None or (owner_id is not None and meeting.owner_id != owner_id):
            return None
        return self._follow(meeting)

    def meetings_of(self, owner_id: Optional[str]) -> list[Meeting]:
        return [self._follow(m) for m in self.repository.list_all(owner_id)]

    def _follow(self, meeting: Meeting) -> Meeting:
        """Reunião que espera a gravação de outro usuário: acompanha o status dela e,
        quando ela termina, recebe uma cópia própria do resultado (chat e tarefas à parte)."""
        if not meeting.source_id:
            return meeting
        source = self.repository.find_by_id(meeting.source_id)
        if source is None:
            meeting.status = MeetingStatus.ERROR
            meeting.error = "A gravação foi apagada antes de ficar pronta."
            meeting.source_id = ""
        else:
            before = [getattr(meeting, f) for f in _SHARED]
            for f in _SHARED:
                setattr(meeting, f, getattr(source, f))
            if source.status in _FINAL:
                meeting.source_id = ""  # a partir daqui a reunião é independente
            elif [getattr(meeting, f) for f in _SHARED] == before:
                return meeting
        self.repository.save(meeting)
        return meeting

    def delete_meeting(self, meeting: Meeting) -> None:
        """Apaga a reunião. A gravação fica se outra reunião ainda usa o mesmo áudio."""
        if meeting.status in _ACTIVE:
            raise MeetingInProgress(meeting.id)
        for follower in self.repository.find_by(source_id=meeting.id):
            self._follow(follower)  # copia o resultado antes de a fonte sumir
        self.repository.delete(meeting.id)
        if meeting.audio_path and not self.repository.find_by(audio_path=meeting.audio_path):
            audio = Path(meeting.audio_path)  # com a gravação, os arquivos auxiliares (.speakers.jsonl…)
            for f in audio.parent.glob(f"{audio.name.split('.')[0]}.*"):
                f.unlink(missing_ok=True)

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
        from infrastructure.recorder.platforms import detect_platform, meeting_key
        platform = detect_platform(meeting_url)  # ValueError com mensagem para o usuário
        key = meeting_key(meeting_url)
        title = (title or "").strip() or self._title_from_calendar(meeting_url) \
            or f"Reunião {datetime.now():%d/%m/%Y %H:%M}"

        # Mesma reunião já com o bot a caminho ou gravando: não manda um segundo bot.
        # ponytail: dois envios no mesmo instante ainda podem criar dois bots; trava por link se acontecer
        active = [self._follow(m) for m in self.repository.find_by(meeting_url=key)]
        active = [m for m in active if m.status in (MeetingStatus.PENDING, MeetingStatus.RECORDING)]
        mine = next((m for m in active if m.owner_id == owner_id), None)
        if mine:
            raise MeetingInProgress(mine.id)
        meeting = Meeting(id=str(uuid.uuid4()), title=title, owner_id=owner_id,
                          platform=platform.key, meeting_url=key)
        source = next((m for m in active if not m.source_id), None)
        if source:
            meeting.source_id = source.id
            self.repository.save(self._follow(meeting))
            return meeting.id
        meeting_id = meeting.id
        self.repository.save(meeting)

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
