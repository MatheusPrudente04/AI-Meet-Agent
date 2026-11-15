"""Formato único de log, com o id da reunião em toda linha (Sprint 3).

Quem inicia o trabalho de uma reunião chama `bind_meeting(id)`; daí em diante
qualquer log daquele contexto — inclusive de bibliotecas — sai com o id, e dá
para seguir uma reunião com `grep <id>`.
"""
import logging
from contextvars import ContextVar

_meeting_id: ContextVar[str] = ContextVar("meeting_id", default="-")

FORMAT = "%(asctime)s %(levelname)s [%(meeting_id)s] %(name)s: %(message)s"


def bind_meeting(meeting_id: str) -> None:
    _meeting_id.set(meeting_id)


class _MeetingIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.meeting_id = _meeting_id.get()
        return True


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(_MeetingIdFilter())
    handler.setFormatter(logging.Formatter(FORMAT))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # Uma linha por requisição HTTP de biblioteca é ruído.
    logging.getLogger("httpx").setLevel(logging.WARNING)
