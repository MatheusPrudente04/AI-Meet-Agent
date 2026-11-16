from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class User:
    id: str
    name: str
    email: str  # sempre em minúsculas; é a chave de login
    password_hash: str = ""
    created_at: datetime = field(default_factory=datetime.now)
    role: str = "user"  # "admin" pode limpar sessões de qualquer usuário (ADR-011)
    failed_logins: int = 0
    locked_until: Optional[datetime] = None  # UTC; login bloqueado até esse instante

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"
