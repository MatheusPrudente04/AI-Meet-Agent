"""
api/main.py — FastAPI.

  POST /meetings/upload        → recebe áudio e despacha o processamento
  GET  /meetings               → lista reuniões com status
  GET  /meetings/{id}/status   → status, erro e resumo da reunião
  POST /meetings/{id}/chat     → pergunta sobre a reunião
  POST /capture/chunk|finalize → chunks de áudio enviados pela extensão do bot
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api import chat
from api.routers import capture, meetings
from config.settings import get_settings

settings = get_settings()

app = FastAPI(title="Meet Agent API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(meetings.router, prefix="/meetings", tags=["meetings"])
app.include_router(chat.router, prefix="/meetings", tags=["chat"])
app.include_router(capture.router, prefix="/capture", tags=["capture"])


@app.get("/health")
def health():
    return {"status": "ok", "mode": settings.app_mode}
