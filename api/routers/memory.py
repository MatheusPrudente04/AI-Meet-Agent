"""Notas que o usuário pediu para o agente lembrar (memória entre reuniões, §14)."""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.security import Principal, current_user
from presentation.container import get_container

router = APIRouter()


class MemoryNote(BaseModel):
    id: str
    text: str
    meeting_id: str = ""
    created_at: datetime


@router.get("/", response_model=list[MemoryNote], summary="Notas que o agente guardou para mim")
def list_notes(principal: Principal = Depends(current_user)):
    """Criadas no chat ("lembre que prefiro atas em tópicos"). Valem em todas as reuniões do usuário."""
    return [MemoryNote(id=n["_id"], text=n["text"], meeting_id=n.get("meeting_id", ""), created_at=n["created_at"])
            for n in get_container().memory.list(principal.user.id)]


@router.delete("/{note_id}", status_code=204, summary="Apaga uma nota",
               responses={404: {"description": "Nota não existe ou é de outro usuário"}})
def delete_note(note_id: str, principal: Principal = Depends(current_user)):
    if not get_container().memory.delete(principal.user.id, note_id):
        raise HTTPException(404, "Nota não encontrada")
