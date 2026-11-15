"""Autenticação da API (ADR-007).

- `X-API-Key` em todas as rotas, exceto `/health`.
- `/capture/*` usa um token por sessão, derivado da chave: o navegador do bot
  nunca recebe a chave em si, só o token da reunião que está gravando.

Sem `API_KEY` configurada (desenvolvimento em solo) a API fica aberta; em
collab o settings exige a chave na partida.
"""
import hashlib
import hmac

from fastapi import Header, HTTPException

from config.settings import get_settings


def capture_token(session: str, api_key: str) -> str:
    return hmac.new(api_key.encode(), session.encode(), hashlib.sha256).hexdigest()


def require_api_key(x_api_key: str = Header(default="")) -> None:
    key = get_settings().api_key
    if key and not hmac.compare_digest(x_api_key.encode(), key.encode()):
        raise HTTPException(status_code=401, detail="Chave de API ausente ou inválida")


def require_capture_token(session: str, x_capture_token: str = Header(default="")) -> None:
    key = get_settings().api_key
    if key and not hmac.compare_digest(x_capture_token.encode(), capture_token(session, key).encode()):
        raise HTTPException(status_code=401, detail="Token de captura ausente ou inválido")
