"""Pipeline único de processamento: áudio → transcrição → resumo. Ver ADR-001."""
import logging
from dataclasses import dataclass
from pathlib import Path

from domain.entities.meeting import Meeting, MeetingStatus
from domain.entities.meeting_type import MeetingType
from domain.interfaces.meeting_repository import IMeetingRepository
from use_cases.summarize_meeting import SummarizeMeetingInput, SummarizeMeetingUC
from use_cases.transcribe_meeting import TranscribeMeetingInput, TranscribeMeetingUC

logger = logging.getLogger(__name__)


@dataclass
class ProcessMeetingInput:
    meeting_id: str
    audio_path: str
    title: str
    meeting_type: MeetingType = MeetingType.GENERAL
    with_diarization: bool = True


class ProcessMeetingUC:
    """Grava o status no repositório a cada etapa. Idempotente por meeting_id:
    reprocessar o mesmo id sobrescreve o mesmo documento."""

    def __init__(
        self,
        transcribe_uc: TranscribeMeetingUC,
        summarize_uc: SummarizeMeetingUC,
        repository: IMeetingRepository,
    ) -> None:
        self._transcribe = transcribe_uc
        self._summarize = summarize_uc
        self._repo = repository

    def execute(self, data: ProcessMeetingInput) -> Meeting:
        meeting = self._repo.find_by_id(data.meeting_id) or Meeting(id=data.meeting_id, title=data.title)
        meeting.title = data.title or meeting.title
        meeting.audio_path = data.audio_path
        try:
            return self._run(meeting, data)
        except Exception as exc:
            logger.exception("[%s] falha inesperada no processamento", meeting.id)
            return self._fail(meeting, f"Erro inesperado: {exc}")

    def _run(self, meeting: Meeting, data: ProcessMeetingInput) -> Meeting:
        audio = Path(data.audio_path)
        if not audio.exists() or audio.stat().st_size == 0:
            return self._fail(meeting, f"Áudio ausente ou vazio: {data.audio_path}")

        self._set(meeting, MeetingStatus.TRANSCRIBING)
        t = self._transcribe.execute(
            TranscribeMeetingInput(audio_path=data.audio_path, with_diarization=data.with_diarization)
        )
        if not t.success:
            return self._fail(meeting, t.error_message)
        if not t.transcript.full_text.strip():
            return self._fail(meeting, "Transcrição vazia: nenhum trecho de fala reconhecido.")

        meeting.transcript_text = t.transcript.full_text
        meeting.transcript_formatted = t.transcript.formatted
        meeting.transcript_raw = t.transcript.text_raw
        meeting.participants = t.transcript.speakers
        meeting.duration_minutes = t.transcript.duration_minutes

        self._set(meeting, MeetingStatus.SUMMARIZING)
        s = self._summarize.execute(
            SummarizeMeetingInput(meeting=meeting, meeting_type=data.meeting_type)
        )
        if not s.success:
            return self._fail(meeting, s.error_message)

        meeting.summary = s.summary
        self._set(meeting, MeetingStatus.DONE)
        return meeting

    def _set(self, meeting: Meeting, status: MeetingStatus) -> None:
        meeting.status = status
        meeting.error = ""
        self._repo.save(meeting)
        logger.info("[%s] status=%s", meeting.id, status.value)

    def _fail(self, meeting: Meeting, error: str) -> Meeting:
        meeting.status = MeetingStatus.ERROR
        meeting.error = error
        self._repo.save(meeting)
        logger.error("[%s] %s", meeting.id, error)
        return meeting
