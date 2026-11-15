import threading

from fastapi import APIRouter, Depends, HTTPException

from api.schemas.meeting import ChatRequest, ChatResponse
from api.security import Principal, rate_limited
from config.settings import get_settings
from presentation.container import get_container
from use_cases.chat_with_meeting import ChatWithMeetingInput

router = APIRouter()
# Chamadas de chat ao LLM ao mesmo tempo, por processo da API (ADR-015). O excedente
# espera uma vaga; o total entre processos é API_WORKERS × CHAT_MAX_CONCURRENT.
_llm_slots = threading.BoundedSemaphore(get_settings().chat_max_concurrent)
_WAIT_S = 60  # cada espera ocupa uma thread do pool da API (40 por processo)


@router.post("/{meeting_id}/chat", response_model=ChatResponse, summary="Pergunta sobre a reunião", responses={
    404: {"description": "Reunião não existe ou é de outro usuário"},
    422: {"description": "Reunião ainda não foi transcrita"},
    429: {"description": "Acima de RATE_LIMIT_PER_MINUTE pedidos por minuto (cabeçalho Retry-After)"},
    503: {"description": "O LLM está ocupado com outras perguntas; tente de novo"},
})
def chat(meeting_id: str, body: ChatRequest, principal: Principal = Depends(rate_limited)):
    """A pergunta e a resposta entram em `chat_history` da reunião (GET /meetings/{id})."""
    container = get_container()
    meeting = container.meeting_for(meeting_id, principal.owner_id)

    if not meeting:
        raise HTTPException(404, "Reunião não encontrada")
    if not meeting.is_transcribed:
        raise HTTPException(422, "Reunião ainda não foi transcrita")

    history = None if body.history is None else [m.model_dump() for m in body.history]
    if not _llm_slots.acquire(timeout=_WAIT_S):
        raise HTTPException(503, "O assistente está ocupado com outras perguntas. Tente de novo em instantes.",
                            headers={"Retry-After": "30"})
    try:
        result = container.chat_with_meeting.execute(
            ChatWithMeetingInput(meeting=meeting, question=body.question, history=history)
        )
    finally:
        _llm_slots.release()
    if not result.success:
        raise HTTPException(500, result.error_message)

    return ChatResponse(answer=result.answer, meeting_id=meeting_id)
