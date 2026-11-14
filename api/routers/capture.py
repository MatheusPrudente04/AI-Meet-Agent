"""
api/routers/capture.py — Endpoints para a extensão tabCapture escrever os chunks de áudio.

POST /capture/chunk?session=<id>&seq=<n>  → grava chunk binário em data/audio/<id>.webm
POST /capture/finalize?session=<id>       → marca como concluído e dispara pipeline
GET  /capture/status?session=<id>         → retorna estado e tamanho do arquivo
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, HTTPException

from config.settings import get_settings

router = APIRouter()

_finalized: set[str] = set()


def _safe_session(session: str) -> str:
    """Remove qualquer char fora de alfanumérico+hífen para evitar path traversal."""
    clean = re.sub(r"[^a-zA-Z0-9\-]", "", session)
    if not clean:
        raise HTTPException(status_code=400, detail="session inválido")
    return clean


def _audio_path(session: str) -> Path:
    settings = get_settings()
    audio_dir = Path(settings.audio_storage_path)
    audio_dir.mkdir(parents=True, exist_ok=True)
    return audio_dir / f"{session}.webm"


@router.post("/chunk")
async def receive_chunk(request: Request, session: str, seq: int) -> dict:
    safe = _safe_session(session)
    if safe in _finalized:
        raise HTTPException(status_code=409, detail="sessão já finalizada")

    data = await request.body()
    if not data:
        return {"ok": True, "bytes": 0, "note": "chunk vazio ignorado"}

    path = _audio_path(safe)
    with open(path, "ab") as f:
        f.write(data)

    return {"ok": True, "session": safe, "seq": seq, "bytes": len(data)}


@router.post("/finalize")
async def finalize_capture(session: str) -> dict:
    safe = _safe_session(session)
    _finalized.add(safe)

    path = _audio_path(safe)
    size = path.stat().st_size if path.exists() else 0

    return {"ok": True, "session": safe, "size_bytes": size}


@router.get("/status")
async def capture_status(session: str) -> dict:
    safe = _safe_session(session)
    path = _audio_path(safe)
    size = path.stat().st_size if path.exists() else 0
    return {
        "session": safe,
        "finalized": safe in _finalized,
        "exists": path.exists(),
        "size_bytes": size,
    }
