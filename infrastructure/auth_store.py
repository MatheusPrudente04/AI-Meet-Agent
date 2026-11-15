"""Usuários e sessões no MongoDB (ADR-011).

Coleções:
  users     {_id, name, email (único), password_hash, created_at}
  sessions  {_id: sha256(token), user_id, created_at, expires_at, user_agent, ip}
            — o índice TTL em expires_at apaga as sessões vencidas sozinho.
"""
from typing import Optional

from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

from domain.entities.user import User


def _to_user(doc: Optional[dict]) -> Optional[User]:
    if not doc:
        return None
    return User(id=doc["_id"], name=doc["name"], email=doc["email"],
                password_hash=doc["password_hash"], created_at=doc["created_at"])


class MongoAuthStore:
    def __init__(self, mongo_uri: str, db_name: str) -> None:
        db = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)[db_name]
        self._users, self._sessions = db["users"], db["sessions"]
        self._indexed = False

    def _ensure_indexes(self) -> None:
        # Na primeira escrita, e não no construtor: a aplicação sobe mesmo com o banco fora.
        if not self._indexed:
            self._users.create_index("email", unique=True)
            self._sessions.create_index("expires_at", expireAfterSeconds=0)
            self._indexed = True

    def add_user(self, user: User) -> bool:
        """False se o e-mail já existe."""
        self._ensure_indexes()
        try:
            self._users.insert_one({
                "_id": user.id, "name": user.name, "email": user.email,
                "password_hash": user.password_hash, "created_at": user.created_at,
            })
            return True
        except DuplicateKeyError:
            return False

    def user_by_email(self, email: str) -> Optional[User]:
        return _to_user(self._users.find_one({"email": email}))

    def user_by_id(self, user_id: str) -> Optional[User]:
        return _to_user(self._users.find_one({"_id": user_id}))

    def count_users(self) -> int:
        return self._users.count_documents({})

    def add_session(self, session: dict) -> None:
        self._ensure_indexes()
        self._sessions.insert_one(session)

    def session(self, token_hash: str) -> Optional[dict]:
        return self._sessions.find_one({"_id": token_hash})

    def delete_session(self, token_hash: str) -> None:
        self._sessions.delete_one({"_id": token_hash})
