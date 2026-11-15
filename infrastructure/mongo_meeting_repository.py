from typing import Optional

from pymongo import MongoClient

from domain.entities.meeting import Meeting
from domain.interfaces.meeting_repository import IMeetingRepository
from infrastructure.meeting_serialization import meeting_from_dict, meeting_to_dict


class MongoMeetingRepository(IMeetingRepository):
    """Persistência em MongoDB (PyMongo síncrono) — modo collab. Ver ADR-003."""

    def __init__(self, mongo_uri: str, db_name: str) -> None:
        client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
        self._col = client[db_name]["meetings"]
        self._indexed = False

    def save(self, meeting: Meeting) -> None:
        if not self._indexed:  # na primeira escrita: a aplicação sobe mesmo com o banco fora
            self._col.create_index([("owner_id", 1), ("started_at", -1)])
            self._col.create_index("meeting_url")
            self._col.create_index("source_id")
            # Posição na fila (ADR-015): consultada a cada atualização de status; só as que esperam.
            self._col.create_index("queue", partialFilterExpression={"queue": {"$gt": ""}})
            self._indexed = True
        doc = meeting_to_dict(meeting)
        doc["_id"] = doc.pop("id")
        self._col.replace_one({"_id": meeting.id}, doc, upsert=True)

    def find_by_id(self, meeting_id: str) -> Optional[Meeting]:
        doc = self._col.find_one({"_id": meeting_id})
        return meeting_from_dict(doc) if doc else None

    def list_all(self, owner_id: Optional[str] = None) -> list[Meeting]:
        # ponytail: carrega as transcrições inteiras; usar projeção quando a lista ficar lenta
        # "" também casa com documentos antigos, que não têm o campo.
        query = {} if owner_id is None else {"owner_id": owner_id or {"$in": ["", None]}}
        return [meeting_from_dict(d) for d in self._col.find(query).sort("started_at", -1)]

    def count_by_status(self) -> dict[str, int]:
        return {d["_id"] or "pending": d["n"] for d in self._col.aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}])}

    def find_by(self, **fields) -> list[Meeting]:
        return [meeting_from_dict(d) for d in self._col.find(fields)]

    def delete(self, meeting_id: str) -> None:
        self._col.delete_one({"_id": meeting_id})
