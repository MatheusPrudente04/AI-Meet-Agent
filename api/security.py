"""Autenticação da API (ADR-007 e ADR-011).

Duas credenciais abrem as rotas de reunião:
- `Authorization: Bearer <token>` — sessão de um usuário (POST /auth/login). Ele
  enxerga só as próprias reuniões.
- `X-API-Key` — chave de serviço, para scripts e integrações. Enxerga tudo.

`/capture/*` usa um token por sessão de gravação, derivado da chave: o navegador
do bot nunca recebe a chave em si.

Em solo (desenvolvimento) não há login; sem `API_KEY` a API fica aberta.
"""
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

from fastapi import Depends, Header, HTTPException
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from config.settings import get_settings
from domain.entities.user import User
from presentation.container import get_container

logger = logging.getLogger(__name__)

bearer_scheme = HTTPBearer(auto_error=False, description="Token devolvido por POST /auth/login ou /auth/register.")
api_key_scheme = APIKeyHeader(name="X-API-Key", auto_error=False, description="Chave de serviço (API_KEY do .env).")


@dataclass
class Principal:
    """Quem está chamando. `user` None = chave de serviço (ou API aberta em solo)."""
    user: Optional[User] = None
    token: str = ""

    @property
    def owner_id(self) -> Optional[str]:
        """Filtro de dono para as reuniões: None = sem restrição."""
        return self.user.id if self.user else None


def capture_token(session: str, api_key: str) -> str:
    return hmac.new(api_key.encode(), session.encode(), hashlib.sha256).hexdigest()


def current_principal(
    bearer: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    x_api_key: Optional[str] = Depends(api_key_scheme),
) -> Principal:
    auth = get_container().auth
    if bearer and auth:
        user = auth.user_for_token(bearer.credentials)
        if user:
            return Principal(user, bearer.credentials)
        raise HTTPException(401, "Sessão inválida ou expirada")
    key = get_settings().api_key
    if x_api_key and key and hmac.compare_digest(x_api_key.encode(), key.encode()):
        return Principal()
    if not key and not auth:  # solo sem chave: desenvolvimento
        return Principal()
    raise HTTPException(401, "Credencial ausente ou inválida")


def current_user(principal: Principal = Depends(current_principal)) -> Principal:
    if principal.user is None:
        raise HTTPException(401, "Esta rota exige a sessão de um usuário")
    return principal


def require_capture_token(session: str, x_capture_token: str = Header(default="")) -> None:
    key = get_settings().api_key
    if key and not hmac.compare_digest(x_capture_token.encode(), capture_token(session, key).encode()):
        raise HTTPException(status_code=401, detail="Token de captura ausente ou inválido")


@lru_cache(maxsize=1)
def _redis():
    import redis
    return redis.Redis.from_url(get_settings().redis_url, socket_timeout=1)


def rate_limited(principal: Principal = Depends(current_principal)) -> Principal:
    """Até RATE_LIMIT_PER_MINUTE pedidos caros (envio, bot, chat) por usuário por minuto
    (ADR-015). Contador no Redis, compartilhado entre os processos da API. A chave de
    serviço e o modo solo (sem Redis) não têm limite."""
    settings = get_settings()
    limit = settings.rate_limit_per_minute
    if principal.user is None or limit <= 0 or not settings.is_collab:
        return principal
    key = f"ratelimit:{principal.user.id}:{int(time.time() // 60)}"
    try:
        pipe = _redis().pipeline()
        count, _ = pipe.incr(key).expire(key, 120).execute()
    except Exception as exc:  # Redis fora: o limite é proteção, não pode derrubar a API
        logger.warning("Limite de pedidos indisponível: %s", exc)
        return principal
    if count > limit:
        raise HTTPException(429, f"Muitos pedidos: o limite é {limit} por minuto. Tente de novo em instantes.",
                            headers={"Retry-After": str(60 - int(time.time()) % 60)})
    return principal
