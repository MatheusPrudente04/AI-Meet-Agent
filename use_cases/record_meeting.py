"""
use_cases/record_meeting.py

Orquestra o bot de reunião — entra, aguarda, retorna áudio.
Adaptado para PlaywrightBotRecorder (S1-S7) com BotSession atualizada.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Callable


@dataclass
class SendBotInput:
    meeting_url: str
    title: str = "Reunião"
    on_finished: Optional[Callable[[str, str], None]] = None


@dataclass
class SendBotOutput:
    session_id: str
    success: bool
    error_message: str = ""


@dataclass
class BotStatusOutput:
    session_id: str
    status: str
    audio_path: str = ""
    error_message: str = ""


class RecordMeetingUC:
    """
    Use case: envia o bot para a reunião e gerencia o ciclo de vida.
    Não sabe se é Playwright ou outro recorder — recebe via injeção.
    """

    def __init__(self, recorder) -> None:
        self._recorder = recorder
        self._sessions: dict = {}

    def send_bot(self, input_data: SendBotInput) -> SendBotOutput:
        import asyncio

        try:
            loop = asyncio.new_event_loop()
            session = loop.run_until_complete(
                self._recorder.join_async(input_data.meeting_url)
            )
            loop.close()
            self._sessions[session.id] = session

            on_finished = input_data.on_finished
            if on_finished and session.audio_path:
                on_finished(str(session.audio_path), input_data.title)

            return SendBotOutput(session_id=session.id, success=True)

        except Exception as e:
            return SendBotOutput(
                session_id="",
                success=False,
                error_message=str(e),
            )

    def get_status(self, session_id: str) -> BotStatusOutput:
        session = self._sessions.get(session_id)
        if not session:
            return BotStatusOutput(session_id=session_id, status="not_found")

        if session.audio_path and session.audio_path.exists():
            status = "done"
            audio = str(session.audio_path)
        else:
            status = "recording"
            audio = ""

        return BotStatusOutput(session_id=session_id, status=status, audio_path=audio)

    def list_active_sessions(self) -> list[BotStatusOutput]:
        return [
            self.get_status(sid)
            for sid, s in self._sessions.items()
            if not (s.audio_path and s.audio_path.exists())
        ]
