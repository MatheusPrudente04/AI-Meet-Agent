"""Estado do credenciamento da conta do bot (coleção `bot_account`, um documento só).

status: unknown → creating → (waiting_login) → ready | not_configured | error
"""
from datetime import datetime, timezone

from pymongo import MongoClient

STATUSES = ("unknown", "creating", "waiting_login", "ready", "not_configured", "error")


class BotAccountStore:
    def __init__(self, uri: str, db_name: str) -> None:
        self._col = MongoClient(uri, serverSelectionTimeoutMS=3000)[db_name]["bot_account"]

    def get(self) -> dict:
        return self._col.find_one({"_id": "bot"}) or {"_id": "bot", "status": "unknown", "message": ""}

    def set(self, status: str, message: str = "") -> None:
        assert status in STATUSES, status
        self._col.update_one({"_id": "bot"}, {"$set": {
            "status": status, "message": message, "updated_at": datetime.now(timezone.utc).replace(tzinfo=None),
        }}, upsert=True)
