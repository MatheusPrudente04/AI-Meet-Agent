"""
api/routers/capture.py — Endpoints para a extensão tabCapture escrever os chunks de áudio.

POST /capture/chunk?session=<id>&seq=<n>  → grava chunk binário em data/audio/<id>.webm
POST /capture/finalize?session=<id>       → informa o tamanho final
GET  /capture/status?session=<id>         → retorna estado e tamanho do arquivo

A sessão é o id da reunião. Só se aceita áudio de reunião que existe e ainda
está em `pending` ou `recording` (ADR-007): o estado vem do documento, não da
memória do processo.
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from api.security import require_capture_token
from config.settings import get_settings
from domain.entities.meeting import MeetingStatus
from presentation.container import get_container

router = APIRouter(dependencies=[Depends(require_capture_token)])

_OPEN = (MeetingStatus.PENDING, MeetingStatus.RECORDING)
MB = 1024 * 1024


def _safe_session(session: str) -> str:
    """Remove qualquer char fora de alfanumérico+hífen para evitar path traversal."""
    clean = re.sub(r"[^a-zA-Z0-9\-]", "", session)
    if not clean or clean != session:
        raise HTTPException(status_code=400, detail="session inválido")
    return clean


def _audio_path(session: str) -> Path:
    audio_dir = Path(get_settings().audio_storage_path)
    audio_dir.mkdir(parents=True, exist_ok=True)
    return audio_dir / f"{session}.webm"


def _is_open(session: str) -> bool:
    meeting = get_container().repository.find_by_id(session)
    if meeting is None:
        raise HTTPException(status_code=404, detail="sessão desconhecida")
    return meeting.status in _OPEN


@router.post("/chunk")
async def receive_chunk(request: Request, session: str, seq: int) -> dict:
    safe = _safe_session(session)
    settings = get_settings()

    declared = request.headers.get("content-length")
    if declared is None or not declared.isdigit():
        raise HTTPException(status_code=411, detail="Content-Length obrigatório")
    if int(declared) > settings.capture_max_chunk_mb * MB:
        raise HTTPException(status_code=413, detail="chunk acima do limite")

    if not await run_in_threadpool(_is_open, safe):
        raise HTTPException(status_code=409, detail="sessão já finalizada")

    data = await request.body()
    if not data:
        return {"ok": True, "bytes": 0, "note": "chunk vazio ignorado"}

    path = _audio_path(safe)
    current = path.stat().st_size if path.exists() else 0
    if current + len(data) > settings.capture_max_session_mb * MB:
        raise HTTPException(status_code=413, detail="sessão acima do limite de gravação")

    with open(path, "ab") as f:
        f.write(data)

    return {"ok": True, "session": safe, "seq": seq, "bytes": len(data)}


@router.post("/finalize")
def finalize_capture(session: str) -> dict:
    path = _audio_path(_safe_session(session))
    return {"ok": True, "session": session, "size_bytes": path.stat().st_size if path.exists() else 0}


@router.get("/status")
def capture_status(session: str) -> dict:
    safe = _safe_session(session)
    path = _audio_path(safe)
    return {
        "session": safe,
        "finalized": not _is_open(safe),
        "exists": path.exists(),
        "size_bytes": path.stat().st_size if path.exists() else 0,
    }
