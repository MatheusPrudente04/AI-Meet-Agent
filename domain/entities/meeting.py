from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class MeetingStatus(str, Enum):
    """Etapa do processamento. Única fonte de status do sistema."""
    PENDING = "pending"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    SUMMARIZING = "summarizing"
    DONE = "done"
    ERROR = "error"


@dataclass
class Task:
    description: str
    responsible: str = "Não definido"
    deadline: Optional[str] = None
    done: bool = False

    def __str__(self) -> str:
        status = "✅" if self.done else "⬜"
        return f"{status} {self.description} — {self.responsible} ({self.deadline})"


@dataclass
class Decision:
    description: str
    context: str = ""

    def __str__(self) -> str:
        return f"• {self.description}"


@dataclass
class Summary:
    overview: str = ""
    topics: list[str] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)

    @property
    def has_tasks(self) -> bool:
        return len(self.tasks) > 0

    @property
    def pending_tasks(self) -> list[Task]:
        return [t for t in self.tasks if not t.done]

    @property
    def formatted(self) -> str:
        lines = []

        if self.overview:
            lines.append("## Visão Geral")
            lines.append(self.overview)

        if self.topics:
            lines.append("\n## Tópicos Discutidos")
            for topic in self.topics:
                lines.append(f"- {topic}")

        if self.tasks:
            lines.append("\n## Tarefas")
            for task in self.tasks:
                lines.append(str(task))

        if self.decisions:
            lines.append("\n## Decisões Tomadas")
            for decision in self.decisions:
                lines.append(str(decision))

        return "\n".join(lines)


@dataclass
class Meeting:
    id: str
    title: str
    # Dono da reunião (id do usuário). Vazio = anterior ao login multiusuário (ADR-011).
    owner_id: str = ""
    # Plataforma de origem ("meet", "teams", "zoom"); vazio para áudio enviado.
    platform: str = ""
    # Link normalizado da reunião (bot); identifica "a mesma reunião" entre usuários.
    meeting_url: str = ""
    # Outro usuário já mandou o bot a este link: esta reunião espera a gravação dele
    # e recebe uma cópia do resultado (ADR-011). Vazio depois de copiada.
    source_id: str = ""
    started_at: datetime = field(default_factory=datetime.now)
    audio_path: Optional[str] = None
    transcript_text: str = ""
    transcript_formatted: str = ""
    summary: Optional[Summary] = None
    participants: list[str] = field(default_factory=list)
    duration_minutes: float = 0.0
    transcript_raw: str = ""
    status: MeetingStatus = MeetingStatus.PENDING
    error: str = ""
    # Conversa do chat sobre esta reunião: [{"role": "user"|"assistant", "content": str}]
    chat_history: list[dict] = field(default_factory=list)
    # Medições do processamento (tempos em segundos, tamanho da transcrição). Ver ADR-010.
    metrics: dict = field(default_factory=dict)

    @property
    def participants_header(self) -> str:
        """Lista de presentes, para o LLM saber quem estava na reunião mesmo
        que alguém não tenha falado. Vazio quando não se sabe."""
        if not self.participants:
            return ""
        return f"PARTICIPANTES DA REUNIÃO: {', '.join(self.participants)}\n\n"

    @property
    def transcript_for_llm(self) -> str:
        return self.participants_header + (self.transcript_formatted or self.transcript_text)

    @property
    def is_transcribed(self) -> bool:
        return bool(self.transcript_text)

    @property
    def is_summarized(self) -> bool:
        return self.summary is not None
