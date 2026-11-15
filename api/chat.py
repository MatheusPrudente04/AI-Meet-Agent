from fastapi import APIRouter, Depends, HTTPException

from api.schemas.meeting import ChatRequest, ChatResponse
from api.security import Principal, current_principal
from presentation.container import get_container
from use_cases.chat_with_meeting import ChatWithMeetingInput

router = APIRouter()


@router.post("/{meeting_id}/chat", response_model=ChatResponse, summary="Pergunta sobre a reunião", responses={
    404: {"description": "Reunião não existe ou é de outro usuário"},
    422: {"description": "Reunião ainda não foi transcrita"},
})
def chat(meeting_id: str, body: ChatRequest, principal: Principal = Depends(current_principal)):
    """A pergunta e a resposta entram em `chat_history` da reunião (GET /meetings/{id})."""
    container = get_container()
    meeting = container.meeting_for(meeting_id, principal.owner_id)

    if not meeting:
        raise HTTPException(404, "Reunião não encontrada")
    if not meeting.is_transcribed:
        raise HTTPException(422, "Reunião ainda não foi transcrita")

    history = None if body.history is None else [m.model_dump() for m in body.history]
    result = container.chat_with_meeting.execute(
        ChatWithMeetingInput(meeting=meeting, question=body.question, history=history)
    )
    if not result.success:
        raise HTTPException(500, result.error_message)

    return ChatResponse(answer=result.answer, meeting_id=meeting_id)
