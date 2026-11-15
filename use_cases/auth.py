"""Cadastro, login e sessão de usuário (ADR-011).

A senha é guardada como scrypt com sal por usuário. A sessão é um token
aleatório entregue ao cliente; o banco guarda só o SHA-256 dele, então quem
ler o banco não consegue se passar por ninguém.

O armazenamento é injetado (`store`): em produção é o MongoDB
(infrastructure/auth_store.py).
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from domain.entities.user import User

MIN_PASSWORD = 8
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_SCRYPT = dict(n=2**14, r=8, p=1, dklen=32)


class AuthError(Exception):
    """Entrada recusada; a mensagem pode ser mostrada ao usuário."""


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


def _utcnow() -> datetime:
    """UTC sem fuso: é como o MongoDB guarda e devolve datas, e o que o índice TTL compara."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


_DUMMY_HASH = hash_password(secrets.token_hex(8))


class AuthService:
    def __init__(self, store, session_days: int = 30, allow_registration: bool = True) -> None:
        self._store = store
        self._session_days = session_days
        self._allow_registration = allow_registration

    def register(self, name: str, email: str, password: str) -> User:
        if not self._allow_registration:
            raise AuthError("O cadastro de novos usuários está fechado.")
        name, email = name.strip(), email.strip().lower()
        if not name:
            raise AuthError("Informe o nome.")
        if not _EMAIL.fullmatch(email):
            raise AuthError("E-mail inválido.")
        if len(password) < MIN_PASSWORD:
            raise AuthError(f"A senha precisa ter pelo menos {MIN_PASSWORD} caracteres.")
        user = User(id=str(uuid.uuid4()), name=name, email=email, password_hash=hash_password(password))
        if not self._store.add_user(user):
            raise AuthError("Já existe uma conta com este e-mail.")
        return user

    def login(self, email: str, password: str, user_agent: str = "", ip: str = "") -> tuple[str, User]:
        """Devolve (token, usuário). A mensagem de erro é a mesma para e-mail
        desconhecido e senha errada, para não revelar quem tem conta."""
        user = self._store.user_by_email(email.strip().lower())
        # Sem usuário, confere contra um hash qualquer: o tempo de resposta não revela quem tem conta.
        if not verify_password(password, user.password_hash if user else _DUMMY_HASH) or user is None:
            raise AuthError("E-mail ou senha incorretos.")
        return self.open_session(user, user_agent, ip), user

    def open_session(self, user: User, user_agent: str = "", ip: str = "") -> str:
        token = secrets.token_urlsafe(32)
        now = _utcnow()
        self._store.add_session({
            "_id": _token_hash(token),
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
        session = self._store.session(_token_hash(token))
        if session is None or session["expires_at"] < _utcnow():
            return None
        return self._store.user_by_id(session["user_id"])

    def logout(self, token: str) -> None:
        self._store.delete_session(_token_hash(token))

    def is_first_user(self) -> bool:
        return self._store.count_users() == 1
