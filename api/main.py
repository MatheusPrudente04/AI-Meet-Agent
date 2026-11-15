"""api/main.py — FastAPI. A especificação da API está em /docs (Swagger) e em
docs/api/openapi.json (gerada por `python -m api.export_openapi`)."""
from fastapi import Depends, FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from api import chat
from api.routers import auth, capture, meetings
from api.routers.meetings import AUDIO_SUFFIXES
from api.schemas.meeting import ConfigResponse, PlatformSchema
from api.security import current_principal
from config.logging_setup import setup_logging
from config.settings import get_settings
from infrastructure.recorder.platforms import PLATFORMS
from presentation.container import get_container

setup_logging()
settings = get_settings()

DESCRIPTION = """
API do Meet Agent: o bot entra na reunião, grava, transcreve, resume e responde perguntas.

## Autenticação

1. `POST /auth/register` ou `POST /auth/login` devolve um `token`.
2. Envie `Authorization: Bearer <token>` nas rotas de **meetings** e **chat**.
3. Cada usuário enxerga só as próprias reuniões; a reunião de outro usuário responde 404.
4. Cinco senhas erradas seguidas bloqueiam o login por 15 minutos (429).

Se dois usuários mandam o bot ao mesmo link, um só bot grava e cada um recebe a própria
reunião, com chat e tarefas independentes.

Integrações sem usuário podem usar a chave de serviço no cabeçalho `X-API-Key`.

## Fluxo de uma reunião

1. `POST /meetings/bot` (link) ou `POST /meetings/upload` (áudio) devolve o `meeting_id`.
2. Consulte `GET /meetings/{id}/status` a cada 5 s: `pending` → `recording` →
   `transcribing` → `summarizing` → `done` (ou `error`, com o motivo em `error`).
3. Com `done`, `GET /meetings/{id}` traz resumo, tarefas, transcrição e conversa.
4. `POST /meetings/{id}/chat` responde perguntas sobre a reunião.

## Erros

Toda resposta de erro tem o formato `{"detail": "mensagem"}` (ou a lista de validação do
FastAPI em 422). `401` = credencial ausente, inválida ou sessão expirada: refaça o login.
"""

TAGS = [
    {"name": "auth", "description": "Cadastro, login e sessão do usuário."},
    {"name": "meetings", "description": "Reuniões: criar (bot ou upload), acompanhar, ler, apagar."},
    {"name": "chat", "description": "Perguntas sobre uma reunião já transcrita."},
    {"name": "admin", "description": "Só para administradores (a primeira conta): usuários e limpeza de sessões."},
    {"name": "sistema", "description": "Configuração para o front e verificação de saúde. Sem autenticação."},
    {"name": "capture", "description": "Uso interno do bot (áudio em chunks). O front não chama."},
]

app = FastAPI(title="Meet Agent API", version="2.0.0", description=DESCRIPTION, openapi_tags=TAGS)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_auth = [Depends(current_principal)]
app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(auth.admin_router, prefix="/admin", tags=["admin"])
app.include_router(meetings.router, prefix="/meetings", tags=["meetings"], dependencies=_auth)
app.include_router(chat.router, prefix="/meetings", tags=["chat"], dependencies=_auth)
# /capture/* tem autenticação própria: token por sessão (api/security.py).
app.include_router(capture.router, prefix="/capture", tags=["capture"])


@app.get("/config", response_model=ConfigResponse, tags=["sistema"], summary="O que está ligado nesta instalação")
def config():
    """O front lê uma vez na partida para montar as telas (cadastro aberto? e-mail ligado? quais plataformas?)."""
    s = get_settings()
    return ConfigResponse(
        login_enabled=s.is_collab,
        registration_open=s.is_collab and s.allow_registration,
        email_enabled=s.email_enabled,
        calendar_enabled=s.enable_calendar,
        max_upload_mb=s.max_upload_mb,
        audio_formats=sorted(AUDIO_SUFFIXES),
        platforms=[
            PlatformSchema(key=p.key, label=p.label, example=p.example,
                           validated=p.validated, speaker_names=p.speaker_names)
            for p in PLATFORMS if not p.unsupported
        ],
    )


@app.get("/health", tags=["sistema"], summary="Saúde do banco e da fila")
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
