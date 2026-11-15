from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class User:
    id: str
    name: str
    email: str  # sempre em minúsculas; é a chave de login
    password_hash: str = ""
    created_at: datetime = field(default_factory=datetime.now)
