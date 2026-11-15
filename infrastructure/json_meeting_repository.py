import json
from pathlib import Path
from typing import Optional

from domain.entities.meeting import Meeting
from domain.interfaces.meeting_repository import IMeetingRepository
from infrastructure.meeting_serialization import meeting_from_dict, meeting_to_dict


class JsonMeetingRepository(IMeetingRepository):
    """Persistência local em JSON — modo solo (desenvolvimento)."""

    def __init__(self, storage_path: str) -> None:
        self._base = Path(storage_path)
        self._base.mkdir(parents=True, exist_ok=True)

    def save(self, meeting: Meeting) -> None:
        with open(self._base / f"{meeting.id}.json", "w", encoding="utf-8") as f:
            json.dump(meeting_to_dict(meeting), f, ensure_ascii=False, indent=2,
                      default=lambda d: d.isoformat())

    def find_by_id(self, meeting_id: str) -> Optional[Meeting]:
        path = self._base / f"{meeting_id}.json"
        if not path.exists():
            return None
        return meeting_from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list_all(self, owner_id: Optional[str] = None) -> list[Meeting]:
        meetings = [
            meeting_from_dict(json.loads(p.read_text(encoding="utf-8")))
            for p in self._base.glob("*.json")
        ]
        if owner_id is not None:
            meetings = [m for m in meetings if m.owner_id == owner_id]
        return sorted(meetings, key=lambda m: m.started_at, reverse=True)

    def find_by(self, **fields) -> list[Meeting]:
        return [m for m in self.list_all() if all(getattr(m, k) == v for k, v in fields.items())]

    def delete(self, meeting_id: str) -> None:
        (self._base / f"{meeting_id}.json").unlink(missing_ok=True)
