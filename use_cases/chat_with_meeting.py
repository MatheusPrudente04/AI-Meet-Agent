from dataclasses import dataclass, field

from domain.entities.meeting import Meeting, Task
from domain.interfaces.llm_service import ILLMService, LLMServiceError
from use_cases.llm_context import select_relevant

# Quantas mensagens anteriores acompanham a pergunta.
HISTORY_TURNS = 10
# Em reunião longa, quanto de transcrição acompanha a pergunta. Poucos trechos
# bem escolhidos respondem melhor (e mais rápido) que a janela cheia: com ela
# cheia o modelo local deixou passar um dado que estava no meio do contexto.
EXCERPT_CHARS = 8000


@dataclass
class ChatWithMeetingInput:
    meeting: Meeting
    question: str
    # None = usa a conversa guardada na reunião.
    history: list[dict] | None = None


@dataclass
class ChatWithMeetingOutput:
    answer: str
    success: bool
    error_message: str = ""
    extracted_tasks: list[Task] = field(default_factory=list)


class ChatWithMeetingUC:
    """Conversa sobre a reunião. Com repositório, a conversa fica guardada na
    reunião. Transcrição longa demais vai ao LLM como resumo + trechos
    relevantes à pergunta (ADR-010)."""

    def __init__(self, llm_service: ILLMService, repository=None, max_context_chars: int = 30000) -> None:
        self._llm = llm_service
        self._repo = repository
        self._max_chars = max_context_chars

    def execute(self, input_data: ChatWithMeetingInput) -> ChatWithMeetingOutput:
        meeting = input_data.meeting

        if not meeting.is_transcribed:
            return ChatWithMeetingOutput(
                answer="",
                success=False,
                error_message="Não há transcrição disponível para esta reunião.",
            )

        history = meeting.chat_history if input_data.history is None else input_data.history
        try:
            answer = self._llm.chat(
                question=input_data.question,
                context=self._context(meeting, input_data.question),
                history=history[-HISTORY_TURNS:],
            )
        except LLMServiceError as e:
            return ChatWithMeetingOutput(answer="", success=False, error_message=str(e))

        if self._repo:
            meeting.chat_history = [
                *meeting.chat_history,
                {"role": "user", "content": input_data.question},
                {"role": "assistant", "content": answer},
            ]
            self._repo.save(meeting)
        return ChatWithMeetingOutput(answer=answer, success=True)

    def _context(self, meeting: Meeting, question: str) -> str:
        full = meeting.transcript_for_llm
        if len(full) <= self._max_chars:
            return full
        summary = meeting.summary.formatted if meeting.summary else ""
        excerpts = select_relevant(
            meeting.transcript_formatted or meeting.transcript_text,
            question,
            max_chars=min(EXCERPT_CHARS, self._max_chars),
        )
        return (
            f"{meeting.participants_header}"
            f"RESUMO DA REUNIÃO:\n{summary}\n\n"
            "A reunião é longa: abaixo estão só os trechos da transcrição mais ligados à pergunta. "
            "Se a resposta não estiver neles nem no resumo, diga que não encontrou nos trechos consultados.\n\n"
            f"{excerpts}"
        )
