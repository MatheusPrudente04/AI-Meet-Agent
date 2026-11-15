"""Agente de reuniões em LangGraph, regido pelo contrato SPEC_CONTRACT.md (ADR-014).

Dois grafos atrás da porta ILLMService (casos de uso, API e interface não mudam):

  chat:   responder (pode pedir uma ferramenta) → verificar ─ ok ──► fim
                         └─ violou → responder de novo (com as violações) → verificar
                         └─ violou de novo → resposta padrão de "elementos insuficientes"
          Ferramentas: send_email e create_document. O texto do e-mail ou do documento passa
          pela mesma verificação de contrato que uma resposta. Quem executa é o caso de uso.

  resumo: gerar → validar JSON ─ ok ─► normalizar (status da decisão, responsáveis) → fim
                     └─ inválido → gerar de novo (com o erro) ─ inválido de novo → erro

O contrato vai inteiro no prompt de sistema. As regras que dão para conferir sem
outro modelo ficam em use_cases/contract_checks.py.
"""
from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from config.settings import get_settings
from domain.entities.meeting import Decision, Summary, Task
from domain.entities.meeting_type import MeetingType
from domain.interfaces.llm_service import ChatReply, ILLMService, LLMServiceError
from infrastructure.llm.model_client import ModelClient
from infrastructure.llm.prompt_builder import PromptBuilder
from use_cases.contract_checks import FALLBACK, check_answer, decision_status, responsible_or_undefined

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 2  # primeira resposta + uma correção


@lru_cache(maxsize=1)
def load_contract(path: str) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = Path(__file__).resolve().parents[2] / p
    if not p.exists():
        raise LLMServiceError(f"Contrato do agente não encontrado: {p} (AGENT_CONTRACT_PATH)")
    return p.read_text(encoding="utf-8").strip()


CHAT_RULES = """REGRAS DE OPERAÇÃO NESTE SISTEMA (complementam o contrato acima):
1. Você responde sobre UMA reunião, usando apenas o CONTEXTO DA REUNIÃO abaixo. Em reuniões longas o
   contexto traz o resumo e só os trechos da transcrição ligados à pergunta.
2. Você não tem acesso a outras reuniões. Se perguntarem sobre elas, informe essa limitação (§14).
3. Se o assunto não aparece no contexto, responda só: "Isso não foi mencionado durante a reunião."
   Nunca diga isso e depois responda sobre o assunto.
4. Ao citar uma fala entre aspas, copie exatamente o texto da transcrição. Só atribua falas a quem as fez.
5. Separe o que foi dito do que é interpretação sua (§4). Proposta não é decisão; silêncio não é concordância.
6. Pergunta direta pede resposta direta. Pedido de "análise completa" ou "ata" segue o formato do §13.
   Pergunta sobre o que você achou da reunião segue o §11.
7. Responda em português do Brasil, salvo se a pergunta for feita em outro idioma.
8. Ferramentas: use `send_email` só quando o usuário pedir para enviar um e-mail e informar o
   endereço; nunca invente endereço. Se pedirem um e-mail sem dizer o endereço, não use a
   ferramenta: peça o endereço de cada destinatário. Use `create_document` só quando ele pedir um arquivo (PDF,
   Word, Excel, CSV, planilha, HTML, dashboard, gráfico, documento, ata em arquivo). O texto do e-mail ou do documento segue as mesmas regras de
   fidelidade acima. Para qualquer outra pergunta, responda em texto, sem ferramenta.

CONTEXTO DA REUNIÃO:
{context}"""

SUMMARY_RULES = """Você vai produzir o registro estruturado da reunião em JSON, seguindo o contrato acima,
em especial os §4 (fidelidade), §6 (requisitos) e §7 (decisões). Responda APENAS com o JSON."""


TOOLS = [
    {"type": "function", "function": {
        "name": "send_email",
        "description": "Prepara um e-mail sobre a reunião para os destinatários que o usuário informou. "
                       "O usuário confirma antes do envio.",
        "parameters": {"type": "object", "required": ["to", "subject", "body"], "properties": {
            "to": {"type": "array", "items": {"type": "string"},
                   "description": "Endereços exatamente como o usuário escreveu"},
            "subject": {"type": "string"},
            "body": {"type": "string", "description": "Texto do e-mail, fiel à reunião"},
        }}}},
    {"type": "function", "function": {
        "name": "create_document",
        "description": "Gera um arquivo para download com conteúdo da reunião (ata, decisões, tarefas, resumo, "
                       "planilha, dashboard com gráficos).",
        "parameters": {"type": "object", "required": ["format", "title", "content"], "properties": {
            "format": {"type": "string", "enum": ["pdf", "docx", "xlsx", "csv", "html", "md"],
                       "description": "pdf; docx (Word); xlsx (Excel) ou csv (planilha); html (página com "
                                      "tabelas e gráficos, para dashboard); md (Markdown)"},
            "title": {"type": "string"},
            "content": {"type": "string", "description": (
                "Conteúdo em Markdown, fiel à reunião: títulos ##, listas -, tabelas | col | col |. "
                "Para xlsx, csv e html, ponha os dados em tabelas Markdown; no html, cada tabela com "
                "uma coluna numérica (ex.: tarefas por responsável) vira um gráfico de barras.")},
        }}}},
]


def _tool_text(tool: Optional[dict]) -> str:
    """O texto que a ferramenta vai entregar a alguém: é ele que passa pela verificação.
    Títulos (linhas com #) e linhas que são só um texto entre aspas ficam de fora: são
    cabeçalho do documento, não fala de alguém da reunião. Citações no meio do texto continuam."""
    if not tool:
        return ""
    args = tool.get("arguments") or {}
    text = str(args.get("body") or args.get("content") or "")
    keep = [line for line in text.splitlines()
            if not line.lstrip().startswith("#") and not _QUOTED_LINE.fullmatch(line.strip())]
    # Aspas sem ninguém falando antes (título, nome de documento) perdem as aspas; as que vêm
    # depois de "disse", "afirmou", "Nome:" etc. continuam sendo conferidas com a transcrição.
    return _QUOTE_ANY.sub(lambda m: m.group(0) if _SPEECH.search(m.string[max(0, m.start() - 40):m.start()])
                          else m.group(1), "\n".join(keep))


_CLAIMS_ACTION = re.compile(r"\b(gerei|criei|preparei|enviei|anexei)\b[^.\n]{0,40}\b(documento|arquivo|pdf|"
                            r"word|docx|markdown|e-?mail)", re.IGNORECASE)
_QUOTED_LINE = re.compile(r"[*_]*[\"“][^\"“”]+[\"”][*_]*")
_QUOTE_ANY = re.compile(r"[\"“]([^\"“”\n]+)[\"”]")
_SPEECH = re.compile(r"(disse|diz|afirmou|falou|comentou|declarou|perguntou|respondeu|explicou|"
                     r"segundo \w+|nas palavras d\w+|:)\s*$", re.IGNORECASE)


class ChatState(TypedDict, total=False):
    question: str
    context: str
    history: list[dict]
    answer: str
    tool: Optional[dict]
    violations: list[str]
    attempts: int


class SummaryState(TypedDict, total=False):
    transcript: str
    meeting_type: MeetingType
    raw: str
    error: str
    attempts: int
    summary: Optional[Summary]


class MeetingAgent(ILLMService):
    def __init__(self, model: Optional[ModelClient] = None, contract: Optional[str] = None) -> None:
        settings = get_settings()
        self._model = model or ModelClient()
        self._contract = contract if contract is not None else load_contract(settings.agent_contract_path)
        self._prompts = PromptBuilder()
        self._chat_graph = self._build_chat_graph()
        self._summary_graph = self._build_summary_graph()

    # ── Porta ILLMService ─────────────────────────────────────────────

    def chat(self, question: str, context: str, history: list[dict], summary_context: str = "",
             user_id: str | None = None) -> ChatReply:
        state = self._chat_graph.invoke({"question": question, "context": context, "history": history,
                                         "attempts": 0, "violations": [], "tool": None})
        return ChatReply(state["answer"], state.get("tool"))

    def summarize(self, transcript: str, meeting_type: MeetingType = MeetingType.GENERAL) -> Summary:
        state = self._summary_graph.invoke({"transcript": transcript, "meeting_type": meeting_type, "attempts": 0})
        if state.get("summary") is None:
            raise LLMServiceError(f"JSON inválido do modelo após {MAX_ATTEMPTS} tentativas: {state.get('error')}")
        return state["summary"]

    # ── Chat ──────────────────────────────────────────────────────────

    def _build_chat_graph(self):
        g = StateGraph(ChatState)
        g.add_node("answer", self._answer)
        g.add_node("verify", self._verify)
        g.add_node("fallback", lambda s: {"answer": FALLBACK, "tool": None})
        g.add_edge(START, "answer")
        g.add_edge("answer", "verify")
        g.add_conditional_edges("verify", self._after_verify, ["answer", "fallback", END])
        g.add_edge("fallback", END)
        return g.compile()

    def _answer(self, s: ChatState) -> ChatState:
        system = f"{self._contract}\n\n{CHAT_RULES.format(context=s['context'])}"
        messages = [{"role": "system", "content": system},
                    *({"role": t["role"], "content": t["content"]} for t in s.get("history", [])),
                    {"role": "user", "content": s["question"]}]
        if s.get("violations"):
            messages += [
                {"role": "assistant", "content": s["answer"] or _tool_text(s.get("tool"))},
                {"role": "user", "content": "Sua resposta anterior violou o contrato: " + "; ".join(s["violations"])
                 + ". Responda de novo ao mesmo pedido, corrigindo isso e sem mencionar esta correção."},
            ]
        answer, tool = self._model.complete_tools(messages, TOOLS)
        return {"answer": answer.strip(), "tool": tool, "attempts": s.get("attempts", 0) + 1}

    def _verify(self, s: ChatState) -> ChatState:
        text = _tool_text(s.get("tool")) or s["answer"]
        violations = check_answer(text, s["context"], s["question"])
        if not s.get("tool") and _CLAIMS_ACTION.search(s["answer"]):
            violations.append("diz que gerou o arquivo ou preparou o e-mail sem usar a ferramenta; "
                              "use create_document ou send_email")
        if violations:
            logger.warning("Resposta violou o contrato (tentativa %d): %s", s["attempts"], "; ".join(violations))
        return {"violations": violations}

    @staticmethod
    def _after_verify(s: ChatState) -> str:
        if not s["violations"]:
            return END
        return "answer" if s["attempts"] < MAX_ATTEMPTS else "fallback"

    # ── Resumo ────────────────────────────────────────────────────────

    def _build_summary_graph(self):
        g = StateGraph(SummaryState)
        g.add_node("generate", self._generate)
        g.add_node("parse", self._parse)
        g.add_edge(START, "generate")
        g.add_edge("generate", "parse")
        g.add_conditional_edges(
            "parse",
            lambda s: END if s.get("summary") is not None or s["attempts"] >= MAX_ATTEMPTS else "generate",
            ["generate", END],
        )
        return g.compile()

    def _generate(self, s: SummaryState) -> SummaryState:
        system = f"{self._contract}\n\n{SUMMARY_RULES}\n\n{self._prompts.build_summarize_system(s['meeting_type'])}"
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": self._prompts.build_summarize_user(s["transcript"])}]
        if s.get("error"):
            messages += [{"role": "assistant", "content": s["raw"]},
                         {"role": "user", "content": f"O JSON acima é inválido ({s['error']}). "
                                                     "Devolva apenas o JSON completo e válido."}]
        return {"raw": self._model.complete(messages, as_json=True), "attempts": s.get("attempts", 0) + 1}

    def _parse(self, s: SummaryState) -> SummaryState:
        try:
            return {"summary": parse_summary(s["raw"], s["transcript"]), "error": ""}
        except (ValueError, TypeError, AttributeError) as exc:
            logger.warning("JSON do resumo inválido (tentativa %d): %s", s["attempts"], exc)
            return {"summary": None, "error": str(exc)[:200]}


def parse_summary(raw: str, transcript: str) -> Summary:
    """JSON do modelo → Summary, aplicando o contrato: status de decisão válido e
    responsável só quando aparece na reunião. ValueError se não for JSON utilizável."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1].removeprefix("json").strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        data = json.loads(cleaned + "}")  # modelo local às vezes corta a chave final
    if not isinstance(data, dict):
        raise ValueError("o JSON não é um objeto")
    return Summary(
        overview=str(data.get("overview") or ""),
        topics=[str(t) for t in data.get("topics") or []],
        tasks=[
            Task(description=t["description"].strip(),
                 responsible=responsible_or_undefined(t.get("responsible") or "", transcript),
                 deadline=t.get("deadline") or "Não definido")
            for t in data.get("tasks") or [] if isinstance(t, dict) and (t.get("description") or "").strip()
        ],
        decisions=[
            Decision(description=d["description"].strip(), context=d.get("context") or "",
                     status=decision_status(d.get("status") or ""))
            for d in data.get("decisions") or [] if isinstance(d, dict) and (d.get("description") or "").strip()
        ],
    )
