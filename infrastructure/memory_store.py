"""Notas de memória por usuário (ferramenta remember do chat): Mongo em collab, JSON em solo."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


class MongoMemoryStore:
    def __init__(self, uri: str, db_name: str) -> None:
        from pymongo import MongoClient
        self._col = MongoClient(uri, serverSelectionTimeoutMS=3000)[db_name]["memories"]
        self._indexed = False

    def add(self, note: dict) -> None:
        if not self._indexed:
            self._col.create_index([("owner_id", 1), ("created_at", 1)])
            self._indexed = True
        self._col.insert_one(note)

    def list(self, owner_id: str) -> list[dict]:
        return list(self._col.find({"owner_id": owner_id}).sort("created_at", 1))

    def delete(self, owner_id: str, note_id: str) -> bool:
        return self._col.delete_one({"_id": note_id, "owner_id": owner_id}).deleted_count == 1


class JsonMemoryStore:
    def __init__(self, path: str) -> None:
        self._path = Path(path)

    def _read(self) -> list[dict]:
        if not self._path.exists():
            return []
        return [{**n, "created_at": datetime.fromisoformat(n["created_at"])}
                for n in json.loads(self._path.read_text(encoding="utf-8"))]

    def _write(self, notes: list[dict]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(notes, ensure_ascii=False, default=lambda d: d.isoformat()),
                              encoding="utf-8")

    def add(self, note: dict) -> None:
        self._write([*self._read(), note])

    def list(self, owner_id: str) -> list[dict]:
        return [n for n in self._read() if n["owner_id"] == owner_id]

    def delete(self, owner_id: str, note_id: str) -> bool:
        notes = self._read()
        kept = [n for n in notes if not (n["_id"] == note_id and n["owner_id"] == owner_id)]
        self._write(kept)
        return len(kept) < len(notes)
