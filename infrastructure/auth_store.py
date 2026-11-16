"""Usuários, sessões e códigos de recuperação no MongoDB (ADR-011).

Coleções:
  users            {_id, name, email (único), password_hash, created_at, role,
                    failed_logins, locked_until}
  sessions         {_id: sha256(token), user_id, created_at, expires_at, user_agent, ip}
  password_resets  {_id: user_id, code_hash, expires_at, attempts}
Os índices TTL em `expires_at` apagam sessões e códigos vencidos sozinhos.
"""
from datetime import datetime
from typing import Optional

from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

from domain.entities.user import User

_USER_FIELDS = ("name", "email", "password_hash", "created_at", "role", "failed_logins", "locked_until")


def _to_user(doc: Optional[dict]) -> Optional[User]:
    if not doc:
        return None
    return User(id=doc["_id"], **{k: doc[k] for k in _USER_FIELDS if k in doc})


class MongoAuthStore:
    def __init__(self, mongo_uri: str, db_name: str) -> None:
        db = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)[db_name]
        self._users, self._sessions, self._resets = db["users"], db["sessions"], db["password_resets"]
        self._indexed = False

    def _ensure_indexes(self) -> None:
        # Na primeira escrita, e não no construtor: a aplicação sobe mesmo com o banco fora.
        if not self._indexed:
            self._users.create_index("email", unique=True)
            self._sessions.create_index("expires_at", expireAfterSeconds=0)
            self._sessions.create_index("user_id")
            self._resets.create_index("expires_at", expireAfterSeconds=0)
            self._indexed = True

    # ── Usuários ──────────────────────────────────────────────────────

    def add_user(self, user: User) -> bool:
        """False se o e-mail já existe."""
        self._ensure_indexes()
        try:
            self._users.insert_one({"_id": user.id, **{k: getattr(user, k) for k in _USER_FIELDS}})
            return True
        except DuplicateKeyError:
            return False

    def user_by_email(self, email: str) -> Optional[User]:
        return _to_user(self._users.find_one({"email": email}))

    def user_by_id(self, user_id: str) -> Optional[User]:
        return _to_user(self._users.find_one({"_id": user_id}))

    def list_users(self) -> list[User]:
        return [_to_user(d) for d in self._users.find()]

    def count_users(self) -> int:
        return self._users.count_documents({})

    def count_active_sessions(self) -> int:
        from use_cases.auth import utcnow
        return self._sessions.count_documents({"expires_at": {"$gt": utcnow()}})

    def update_user(self, user_id: str, fields: dict) -> None:
        self._users.update_one({"_id": user_id}, {"$set": fields})

    # ── Sessões ───────────────────────────────────────────────────────

    def add_session(self, session: dict) -> None:
        self._ensure_indexes()
        self._sessions.insert_one(session)

    def session(self, sid: str) -> Optional[dict]:
        return self._sessions.find_one({"_id": sid})

    def sessions_of(self, user_id: str) -> list[dict]:
        return list(self._sessions.find({"user_id": user_id}))

    def delete_session(self, sid: str) -> None:
        self._sessions.delete_one({"_id": sid})

    def delete_sessions(self, user_id: Optional[str] = None, keep: Optional[str] = None,
                        expired_before: Optional[datetime] = None) -> int:
        query: dict = {}
        if user_id is not None:
            query["user_id"] = user_id
        if keep:
            query["_id"] = {"$ne": keep}
        if expired_before is not None:
            query["expires_at"] = {"$lt": expired_before}
        return self._sessions.delete_many(query).deleted_count

    # ── Recuperação de senha ──────────────────────────────────────────

    def put_reset(self, reset: dict) -> None:
        self._ensure_indexes()
        self._resets.replace_one({"_id": reset["_id"]}, reset, upsert=True)

    def get_reset(self, user_id: str) -> Optional[dict]:
        return self._resets.find_one({"_id": user_id})

    def update_reset(self, user_id: str, fields: dict) -> None:
        self._resets.update_one({"_id": user_id}, {"$set": fields})

    def delete_reset(self, user_id: str) -> None:
        self._resets.delete_one({"_id": user_id})
