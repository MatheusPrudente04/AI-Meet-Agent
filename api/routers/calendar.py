"""Próximas reuniões da agenda do Google (ENABLE_CALENDAR), para mandar o bot com um clique."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.security import Principal, current_principal
from presentation.container import get_container

router = APIRouter()


class CalendarEventSchema(BaseModel):
    id: str
    title: str
    start: datetime
    end: datetime
    meeting_url: str = Field(description="Meet, Zoom ou Teams; mandar para POST /meetings/bot")
    participants: list[str] = []


@router.get("/events", response_model=list[CalendarEventSchema], summary="Próximas reuniões da agenda",
            responses={409: {"description": "Agenda desligada (ENABLE_CALENDAR) ou não autorizada"}})
def upcoming(limit: int = 8, principal: Principal = Depends(current_principal)):
    """Só eventos com link de reunião. A agenda é uma só por instalação (a conta autorizada no
    calendar_setup), não uma por usuário."""
    container = get_container()
    if container.calendar is None:
        raise HTTPException(409, "Agenda desligada: ENABLE_CALENDAR=true no .env")
    events, error = container.upcoming_events(max(1, min(limit, 20)))
    if error:
        raise HTTPException(409, error)
    return [CalendarEventSchema(id=e.id, title=e.title, start=e.start, end=e.end, meeting_url=e.meet_url,
                                participants=e.participants) for e in events]
