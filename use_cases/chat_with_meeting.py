import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from domain.entities.meeting import Meeting, Task
from domain.interfaces.llm_service import ChatReply, ILLMService, LLMServiceError
from use_cases.llm_context import select_relevant
from use_cases.meeting_memory import build_memory, new_note

_EMAIL = re.compile(r"[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+")
DOCUMENT_FORMATS = ("pdf", "docx", "xlsx", "csv", "html", "md")
_FORMAT_ALIASES = {"word": "docx", "doc": "docx", "markdown": "md", "excel": "xlsx", "xls": "xlsx",
                   "planilha": "xlsx", "htm": "html", "dashboard": "html", "web": "html"}

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
    # Ação que o agente preparou (e-mail a confirmar ou documento gerado); também fica na
    # mensagem do assistente em chat_history["action"].
    action: Optional[dict] = None


class ChatWithMeetingUC:
    """Conversa sobre a reunião. Com repositório, a conversa fica guardada na
    reunião. Transcrição longa demais vai ao LLM como resumo + trechos
    relevantes à pergunta (ADR-010)."""

    def __init__(self, llm_service: ILLMService, repository=None, max_context_chars: int = 30000,
                 documents_dir: Optional[Path] = None,
                 write_document: Optional[Callable[..., Path]] = None, memory_store=None) -> None:
        self._memory = memory_store  # notas do remember; sem ele, sem memória entre reuniões
        self._llm = llm_service
        self._repo = repository
        self._max_chars = max_context_chars
        self._documents_dir = documents_dir
        self._write_document = write_document

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
            reply = self._llm.chat(
                question=input_data.question,
                context=self._context(meeting, input_data.question),
                history=[_for_llm(m) for m in history[-HISTORY_TURNS:]],
            )
        except LLMServiceError as e:
            return ChatWithMeetingOutput(answer="", success=False, error_message=str(e))
        if isinstance(reply, str):  # implementações da porta que não usam ferramentas
            reply = ChatReply(reply)

        answer, action = reply.answer, None
        if reply.tool:
            user_texts = [input_data.question, *(m["content"] for m in history if m.get("role") == "user")]
            try:
                answer, action = self._prepare_action(meeting, reply.tool, user_texts)
            except Exception as e:  # documento que não deu para gerar: diz o motivo, não derruba o chat
                answer = f"Não consegui preparar isso: {e}"

        if self._repo:
            reply_msg = {"role": "assistant", "content": answer, **({"action": action} if action else {})}
            meeting.chat_history = [*meeting.chat_history, {"role": "user", "content": input_data.question}, reply_msg]
            self._repo.save(meeting)
        return ChatWithMeetingOutput(answer=answer, success=True, action=action)

    def _prepare_action(self, meeting: Meeting, tool: dict, user_texts: list[str]) -> tuple[str, Optional[dict]]:
        args = tool.get("arguments") or {}
        action_id = uuid.uuid4().hex[:12]

        if tool.get("name") == "send_email":
            to = args.get("to") or []
            to = [to] if isinstance(to, str) else to
            to = list(dict.fromkeys(a.strip() for a in to if _EMAIL.fullmatch(a.strip() or "")))
            typed = " ".join(user_texts).lower()
            # O modelo não escolhe destinatário: só vale endereço que o usuário escreveu.
            if not to or any(a.lower() not in typed for a in to):
                return "Para enviar o e-mail, escreva aqui o endereço de cada destinatário.", None
            subject = str(args.get("subject") or f"Reunião: {meeting.title}")[:200]
            body = str(args.get("body") or "").strip()
            if not body:
                return "Não consegui montar o texto do e-mail. Diga o que ele deve conter.", None
            action = {"id": action_id, "type": "email", "status": "pending", "to": to,
                      "subject": subject, "body": body[:20000]}
            return (f"Preparei o e-mail para {', '.join(to)}. Confira o texto abaixo e confirme o envio; "
                    "nada é enviado antes disso."), action

        if tool.get("name") == "create_document":
            if self._documents_dir is None or self._write_document is None:
                return "A geração de documentos não está disponível neste ambiente.", None
            fmt = str(args.get("format") or "pdf").lower().lstrip(".")
            fmt = _FORMAT_ALIASES.get(fmt, fmt)
            if fmt not in DOCUMENT_FORMATS:
                return (f"Formato {fmt} não suportado. Posso gerar PDF, Word (docx), Excel (xlsx), CSV, "
                        "HTML (com gráficos) ou Markdown."), None
            title = str(args.get("title") or meeting.title or "Documento da reunião")[:150]
            content = str(args.get("content") or "").strip()
            if not content:
                return "Não consegui montar o conteúdo do documento. Diga o que ele deve conter.", None
            # Dashboard sem nenhuma tabela (o modelo escreveu só prosa): o sistema põe os números
            # que ele mesmo contou, para a página ter gráfico e com o número certo.
            extra = meeting_stats(meeting) if fmt == "html" else []
            self._write_document(document_path(self._documents_dir, meeting.id, action_id, fmt), fmt, title, content,
                                 **({"fallback_tables": extra} if extra else {}))
            slug = re.sub(r"[^\w-]+", "-", title.lower()).strip("-")[:60] or "documento"
            action = {"id": action_id, "type": "document", "status": "created", "format": fmt,
                      "title": title, "filename": f"{slug}.{fmt}"}
            return f"Gerei o documento {title} ({fmt.upper()}). Baixe pelo botão abaixo.", action

        if tool.get("name") == "remember":
            if self._memory is None:
                return "A memória entre reuniões não está disponível neste ambiente.", None
            note = new_note(meeting.owner_id, str(args.get("note") or ""), meeting.id)
            if note is None:
                return "Não entendi o que devo lembrar. Pode repetir?", None
            self._memory.add(note)
            return (f"Anotado: {note['text']} Vou levar isso em conta nas suas próximas conversas, em "
                    "qualquer reunião. Você pode ver e apagar as notas em Minha conta."), \
                {"id": note["_id"], "type": "memory", "status": "created", "title": note["text"]}

        return "Não sei executar essa ação.", None

    def _context(self, meeting: Meeting, question: str) -> str:
        # A reunião aberta diz o próprio título e data: sem isso, com registros de outras reuniões
        # no contexto, o modelo trocou "esta reunião" pela anterior (teste de 2026-11-15).
        opened = (f"REUNIÃO ABERTA (é sobre esta que o usuário pergunta): \"{meeting.title}\", "
                  f"{meeting.started_at:%d/%m/%Y}\n\n")
        memory = self._memory_block(meeting, question)
        return memory + opened + self._meeting_context(meeting, question) if memory else \
            self._meeting_context(meeting, question)

    def _memory_block(self, meeting: Meeting, question: str) -> str:
        """§14: registros das outras reuniões do mesmo dono e as notas dele."""
        if self._memory is None or self._repo is None:
            return ""
        others = [m for m in self._repo.list_all(meeting.owner_id) if m.id != meeting.id]
        return build_memory(meeting, others, self._memory.list(meeting.owner_id), question)

    def _meeting_context(self, meeting: Meeting, question: str) -> str:
        full = meeting.transcript_for_llm
        if len(full) <= self._max_chars:
            return _stats_block(meeting) + _decisions_block(meeting) + full
        summary = meeting.summary.formatted if meeting.summary else ""
        excerpts = select_relevant(
            meeting.transcript_formatted or meeting.transcript_text,
            question,
            max_chars=min(EXCERPT_CHARS, self._max_chars),
        )
        return (
            f"{meeting.participants_header}"
            f"{_stats_block(meeting)}"
            f"RESUMO DA REUNIÃO:\n{summary}\n\n"
            "A reunião é longa: abaixo estão só os trechos da transcrição mais ligados à pergunta. "
            "Se a resposta não estiver neles nem no resumo, diga que não encontrou nos trechos consultados.\n\n"
            f"{excerpts}"
        )


_SPEAKER_LINE = re.compile(r"^\[\d{1,2}:\d{2}(?::\d{2})?\]\s*([^:\]\n]{1,60}):", re.MULTILINE)


def meeting_stats(meeting: Meeting) -> list[tuple[str, list[list[str]]]]:
    """Contagens calculadas pelo sistema, como tabelas (título, linhas com cabeçalho).
    Modelo de linguagem conta mal: num teste, "falas por participante" saiu 3/2/1 com 6/4/3."""
    tables = []
    counts: dict[str, int] = {}
    for name in _SPEAKER_LINE.findall(meeting.transcript_formatted or ""):
        counts[name.strip()] = counts.get(name.strip(), 0) + 1
    if counts:
        tables.append(("Falas por participante", [["Participante", "Falas"],
                       *([n, str(c)] for n, c in sorted(counts.items(), key=lambda kv: -kv[1]))]))
    tasks = meeting.summary.tasks if meeting.summary else []
    if tasks:
        owners: dict[str, int] = {}
        for t in tasks:
            owners[t.responsible] = owners.get(t.responsible, 0) + 1
        tables.append(("Tarefas por responsável", [["Responsável", "Tarefas"], *([n, str(c)] for n, c in owners.items())]))
    decisions = meeting.summary.decisions if meeting.summary else []
    if decisions:
        by_status: dict[str, int] = {}
        for d in decisions:
            by_status[d.status or "sem estado"] = by_status.get(d.status or "sem estado", 0) + 1
        tables.append(("Decisões por estado", [["Estado", "Decisões"], *([k, str(v)] for k, v in by_status.items())]))
    return tables


def _stats_block(meeting: Meeting) -> str:
    lines = [f"{title.lower()}: " + ", ".join(f"{r[0]} {r[1]}" for r in rows[1:]) for title, rows in meeting_stats(meeting)]
    if meeting.duration_minutes:
        lines.append(f"duração: {meeting.duration_minutes:.0f} min")
    if not lines:
        return ""
    return ("NÚMEROS DA REUNIÃO (contados pelo sistema; use estes em tabelas e gráficos, não conte de novo):\n"
            + "\n".join(f"- {l}" for l in lines) + "\n\n")


def _decisions_block(meeting: Meeting) -> str:
    """Estado de cada decisão, como o resumo classificou (§7 do contrato): o chat
    responde "o que foi aprovado" com a mesma classificação do resumo."""
    decisions = [d for d in (meeting.summary.decisions if meeting.summary else []) if d.status]
    if not decisions:
        return ""
    lines = "\n".join(f"- [{d.status}] {d.description}" for d in decisions)
    return f"DECISÕES REGISTRADAS NO RESUMO (estado de cada uma; confira na transcrição):\n{lines}\n\n"


def document_path(documents_dir: Path, meeting_id: str, action_id: str, fmt: str) -> Path:
    """Onde fica o arquivo de um documento gerado no chat. Montado só com ids do sistema,
    nunca com texto do modelo ou do usuário."""
    return Path(documents_dir) / meeting_id / f"{action_id}.{fmt}"


def _for_llm(message: dict) -> dict:
    """Mensagem da conversa como o modelo a vê. A confirmação de uma ação vira uma nota do
    sistema: com o texto "Gerei o documento…" no histórico, o modelo passou a responder assim
    sem chamar a ferramenta (nenhum arquivo era gerado)."""
    action = message.get("action")
    if message["role"] != "assistant" or not action:
        return {"role": message["role"], "content": message["content"]}
    tool = {"document": "create_document", "email": "send_email", "memory": "remember"}.get(action["type"], "?")
    return {"role": "assistant", "content": f"[O sistema executou a ferramenta {tool} para o pedido acima.]"}
