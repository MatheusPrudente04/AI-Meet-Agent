"""
container.py — único ponto de composição (injeção de dependência).

Os modos diferem só em dois pontos (ADR-004):
  collab (produção)      → MongoDB + fila Celery
  solo (desenvolvimento) → JSON local + thread
"""
from __future__ import annotations

import threading
from functools import lru_cache

from config.settings import get_settings
from domain.entities.meeting import Meeting
from infrastructure.llm.langchain_llm_service import LangChainLLMService
from use_cases.chat_with_meeting import ChatWithMeetingUC
from use_cases.process_meeting import ProcessMeetingInput, ProcessMeetingUC
from use_cases.summarize_meeting import SummarizeMeetingUC
from use_cases.transcribe_meeting import TranscribeMeetingUC


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
        self.repository = _build_repository(settings)
        self.transcribe_meeting = TranscribeMeetingUC(_build_transcriber(settings))
        self.summarize_meeting = SummarizeMeetingUC(llm)
        self.chat_with_meeting = ChatWithMeetingUC(llm)
        self.process_meeting = ProcessMeetingUC(
            self.transcribe_meeting, self.summarize_meeting, self.repository
        )

    def enqueue_processing(self, meeting_id: str, audio_path: str, title: str, wait: bool = False) -> None:
        """Registra a reunião como pendente e despacha o processamento.

        wait=True é para processos que encerram logo em seguida (CLI, run_bot):
        em solo roda na própria chamada, porque uma thread não sobrevive ao fim
        do interpretador. Em collab é ignorado — a fila é quem executa.
        """
        meeting = self.repository.find_by_id(meeting_id) or Meeting(id=meeting_id, title=title)
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


@lru_cache(maxsize=1)
def get_container() -> Container:
    return Container()
