"""
api/main.py — FastAPI.

  POST /meetings/upload        → recebe áudio e despacha o processamento
  GET  /meetings               → lista reuniões com status
  GET  /meetings/{id}/status   → status, erro e resumo da reunião
  POST /meetings/{id}/chat     → pergunta sobre a reunião
  POST /capture/chunk|finalize → chunks de áudio enviados pela extensão do bot
"""
from fastapi import Depends, FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from api import chat
from api.routers import capture, meetings
from api.security import require_api_key
from config.logging_setup import setup_logging
from config.settings import get_settings
from presentation.container import get_container

setup_logging()
settings = get_settings()

app = FastAPI(title="Meet Agent API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_auth = [Depends(require_api_key)]
app.include_router(meetings.router, prefix="/meetings", tags=["meetings"], dependencies=_auth)
app.include_router(chat.router, prefix="/meetings", tags=["chat"], dependencies=_auth)
# /capture/* tem autenticação própria: token por sessão (api/security.py).
app.include_router(capture.router, prefix="/capture", tags=["capture"])


@app.get("/health")
def health(response: Response):
    """Confere as dependências de verdade: 503 se o banco ou a fila não respondem."""
    checks = {}
    try:
        get_container().repository.find_by_id("__health__")
        checks["storage"] = "ok"
    except Exception as exc:
        checks["storage"] = f"erro: {type(exc).__name__}"
    if settings.is_collab:
        try:
            import redis
            redis.Redis.from_url(settings.redis_url, socket_timeout=2, socket_connect_timeout=2).ping()
            checks["queue"] = "ok"
        except Exception as exc:
            checks["queue"] = f"erro: {type(exc).__name__}"
    ok = all(v == "ok" for v in checks.values())
    if not ok:
        response.status_code = 503
    return {"status": "ok" if ok else "degraded", "mode": settings.app_mode, **checks}
