"""Cadastro, login, sessões e senha de usuário (ADR-011).

- Senha: scrypt com sal por usuário.
- Sessão: token aleatório entregue ao cliente; o banco guarda só o SHA-256 dele,
  que também é o id público da sessão (não serve para entrar).
- Bloqueio: MAX_FAILURES senhas erradas seguidas bloqueiam a conta por LOCK_MINUTES.
- Recuperação: código de uso único enviado por e-mail, guardado como hash, com
  validade e número de tentativas limitados.
- Papel: a primeira conta é `admin` e pode limpar sessões de qualquer usuário.

Datas em UTC sem fuso, como o MongoDB guarda e o índice TTL compara.
O armazenamento é injetado (`store`); em produção é infrastructure/auth_store.py.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from domain.entities.user import User

logger = logging.getLogger(__name__)

MIN_PASSWORD = 8
MAX_FAILURES = 5
LOCK_MINUTES = 15
RESET_MINUTES = 15
RESET_ATTEMPTS = 5
_RESET_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sem 0/O e 1/I
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_SCRYPT = dict(n=2**14, r=8, p=1, dklen=32)
PURGE_SCOPES = ("expired", "user", "all_but_mine")


class AuthError(Exception):
    """Entrada recusada; a mensagem pode ser mostrada ao usuário."""


class AccountLocked(AuthError):
    """Senhas erradas demais: login bloqueado por alguns minutos."""


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **_SCRYPT)
    except ValueError:
        return False
    return hmac.compare_digest(got.hex(), digest)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def session_id(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"A senha precisa ter pelo menos {MIN_PASSWORD} caracteres.")


_DUMMY_HASH = hash_password(secrets.token_hex(8))


class AuthService:
    def __init__(self, store, session_days: int = 30, allow_registration: bool = True) -> None:
        self._store = store
        self._session_days = session_days
        self._allow_registration = allow_registration

    # ── Conta ─────────────────────────────────────────────────────────

    def register(self, name: str, email: str, password: str) -> User:
        if not self._allow_registration:
            raise AuthError("O cadastro de novos usuários está fechado.")
        name, email = name.strip(), email.strip().lower()
        if not name:
            raise AuthError("Informe o nome.")
        if not _EMAIL.fullmatch(email):
            raise AuthError("E-mail inválido.")
        _check_password(password)
        # ponytail: dois cadastros no mesmo instante num banco vazio podem sair os dois admin; aceitável
        role = "admin" if self._store.count_users() == 0 else "user"
        user = User(id=str(uuid.uuid4()), name=name, email=email,
                    password_hash=hash_password(password), role=role)
        if not self._store.add_user(user):
            raise AuthError("Já existe uma conta com este e-mail.")
        return user

    def is_first_user(self) -> bool:
        return self._store.count_users() == 1

    def ensure_admin(self) -> None:
        """Bancos anteriores ao papel admin: a conta mais antiga vira admin."""
        users = self._store.list_users()
        if users and not any(u.is_admin for u in users):
            oldest = min(users, key=lambda u: u.created_at)
            self._store.update_user(oldest.id, {"role": "admin"})
            logger.info("Conta %s promovida a admin (nenhum admin existia)", oldest.id)

    def users(self, admin: User) -> list[User]:
        self._require_admin(admin)
        return sorted(self._store.list_users(), key=lambda u: u.created_at)

    # ── Login e sessão ────────────────────────────────────────────────

    def login(self, email: str, password: str, user_agent: str = "", ip: str = "") -> tuple[str, User]:
        """Devolve (token, usuário). A mensagem é a mesma para e-mail desconhecido e
        senha errada, para não revelar quem tem conta."""
        user = self._store.user_by_email(email.strip().lower())
        now = utcnow()
        if user and user.locked_until and user.locked_until > now:
            minutes = int((user.locked_until - now).total_seconds() // 60) + 1
            raise AccountLocked(f"Conta bloqueada por excesso de tentativas. Tente de novo em {minutes} min.")
        # Sem usuário, confere contra um hash qualquer: o tempo de resposta não revela quem tem conta.
        ok = verify_password(password, user.password_hash if user else _DUMMY_HASH)
        if user is None or not ok:
            if user:
                self._register_failure(user, now)
            raise AuthError("E-mail ou senha incorretos.")
        if user.failed_logins or user.locked_until:
            self._store.update_user(user.id, {"failed_logins": 0, "locked_until": None})
        return self.open_session(user, user_agent, ip), user

    def _register_failure(self, user: User, now: datetime) -> None:
        # ponytail: contador lido e gravado sem atomicidade; tentativas simultâneas podem somar a menos
        failures = user.failed_logins + 1
        if failures >= MAX_FAILURES:
            self._store.update_user(user.id, {"failed_logins": 0, "locked_until": now + timedelta(minutes=LOCK_MINUTES)})
            logger.warning("Conta %s bloqueada por %d min após %d senhas erradas", user.id, LOCK_MINUTES, failures)
        else:
            self._store.update_user(user.id, {"failed_logins": failures})

    def open_session(self, user: User, user_agent: str = "", ip: str = "") -> str:
        token = secrets.token_urlsafe(32)
        now = utcnow()
        self._store.add_session({
            "_id": session_id(token),
            "user_id": user.id,
            "created_at": now,
            "expires_at": now + timedelta(days=self._session_days),
            "user_agent": user_agent[:300],
            "ip": ip,
        })
        return token

    def user_for_token(self, token: str) -> Optional[User]:
        if not token:
            return None
        session = self._store.session(session_id(token))
        if session is None or session["expires_at"] < utcnow():
            return None
        return self._store.user_by_id(session["user_id"])

    def logout(self, token: str) -> None:
        self._store.delete_session(session_id(token))

    # ── Minhas sessões ────────────────────────────────────────────────

    def sessions(self, user: User) -> list[dict]:
        """Sessões ativas do usuário, da mais recente para a mais antiga."""
        now = utcnow()
        active = [s for s in self._store.sessions_of(user.id) if s["expires_at"] >= now]
        return sorted(active, key=lambda s: s["created_at"], reverse=True)

    def end_session(self, user: User, sid: str) -> None:
        """Encerra uma sessão do próprio usuário. Sessão de outro = não encontrada."""
        session = self._store.session(sid)
        if session is None or session["user_id"] != user.id:
            raise LookupError("Sessão não encontrada")
        self._store.delete_session(sid)

    def end_other_sessions(self, user: User, current_token: str) -> int:
        return self._store.delete_sessions(user_id=user.id, keep=session_id(current_token))

    # ── Administração ─────────────────────────────────────────────────

    def _require_admin(self, user: User) -> None:
        if not user.is_admin:
            raise PermissionError("Apenas administradores")

    def purge_sessions(self, admin: User, scope: str, current_token: str, user_id: str = "") -> int:
        """Apaga sessões. Nunca apaga usuários nem reuniões, e nunca a sessão de quem limpa.

        expired       → só as vencidas (o índice TTL também as apaga, com atraso de até 1 min)
        user          → todas as de `user_id`
        all_but_mine  → todas, menos a sessão atual do admin
        """
        self._require_admin(admin)
        keep = session_id(current_token)
        if scope == "expired":
            count = self._store.delete_sessions(expired_before=utcnow(), keep=keep)
        elif scope == "user":
            if not user_id or self._store.user_by_id(user_id) is None:
                raise AuthError("Usuário não encontrado.")
            count = self._store.delete_sessions(user_id=user_id, keep=keep)
        elif scope == "all_but_mine":
            count = self._store.delete_sessions(keep=keep)
        else:
            raise AuthError(f"Escopo inválido. Use um de: {', '.join(PURGE_SCOPES)}")
        logger.warning("Sessões limpas pelo admin %s: escopo=%s usuário=%s total=%d",
                       admin.id, scope, user_id or "-", count)
        return count

    # ── Senha ─────────────────────────────────────────────────────────

    def change_password(self, user: User, current: str, new: str, current_token: str) -> int:
        """Troca a senha e encerra as outras sessões. Devolve quantas encerrou."""
        if not verify_password(current, user.password_hash):
            raise AuthError("Senha atual incorreta.")
        _check_password(new)
        self._store.update_user(user.id, {"password_hash": hash_password(new)})
        return self.end_other_sessions(user, current_token)

    def request_reset(self, email: str) -> Optional[tuple[User, str]]:
        """(usuário, código) para enviar por e-mail; None se o e-mail não tem conta.
        Quem chama responde igual nos dois casos."""
        user = self._store.user_by_email(email.strip().lower())
        if user is None:
            return None
        code = "".join(secrets.choice(_RESET_ALPHABET) for _ in range(8))
        self._store.put_reset({
            "_id": user.id,
            "code_hash": hashlib.sha256(code.encode()).hexdigest(),
            "expires_at": utcnow() + timedelta(minutes=RESET_MINUTES),
            "attempts": 0,
        })
        return user, code

    def reset_password(self, email: str, code: str, new: str) -> None:
        """Troca a senha com o código recebido; encerra todas as sessões e desbloqueia."""
        invalid = AuthError("Código inválido ou vencido. Peça um novo.")
        user = self._store.user_by_email(email.strip().lower())
        reset = self._store.get_reset(user.id) if user else None
        if reset is None or reset["expires_at"] < utcnow() or reset["attempts"] >= RESET_ATTEMPTS:
            raise invalid
        given = hashlib.sha256(code.strip().upper().encode()).hexdigest()
        if not hmac.compare_digest(given, reset["code_hash"]):
            self._store.update_reset(user.id, {"attempts": reset["attempts"] + 1})
            raise invalid
        _check_password(new)
        self._store.update_user(user.id, {
            "password_hash": hash_password(new), "failed_logins": 0, "locked_until": None,
        })
        self._store.delete_reset(user.id)
        self._store.delete_sessions(user_id=user.id)
        logger.info("Senha redefinida por código para a conta %s", user.id)
