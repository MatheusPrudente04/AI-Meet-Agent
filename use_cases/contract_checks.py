"""Regras do contrato do agente (SPEC_CONTRACT.md) que dão para conferir sem outro
modelo (ADR-014). Cada função devolve as violações encontradas; vazio = passou.

Não confere significado (proposta tratada como decisão, por exemplo): isso fica
no prompt e na avaliação (scripts/eval_agent.py).
"""
from __future__ import annotations

import re
import unicodedata

_PT = {"de", "que", "não", "foi", "para", "com", "uma", "os", "as", "na", "no", "reunião", "da", "do", "é", "em",
       "se", "por", "mais", "como", "mas", "ao", "pela", "pelo", "são", "está", "sobre"}
_EN = {"the", "and", "was", "were", "is", "of", "to", "meeting", "with", "that", "not", "for", "it", "on", "be",
       "this", "are", "by"}
# §4: fala citada entre aspas precisa estar na transcrição. Citações curtas
# ("sim", "ok") são ignoradas: aparecem como expressão, não como fala.
_QUOTE = re.compile(r"[\"“”«]([^\"“”«»]{12,400})[\"“”»]")
_MIN_QUOTE_WORDS = 4
# §4: "atribuir declarações a pessoas que não as fizeram".
_ATTRIBUTION = re.compile(
    r"\b(?i:segundo|conforme|de acordo com)\s+(?:o |a )?([A-ZÀ-Ý][a-zà-ÿ]+)"
    r"|\b([A-ZÀ-Ý][a-zà-ÿ]+)\s+(?:disse|afirmou|declarou|mencionou|falou|propôs|sugeriu|comentou|explicou)\b"
)
# Linha da transcrição reproduzida na resposta: "[01:45] Bruno: texto".
_LINE = re.compile(r"\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s*([^:\]\n]{1,60}):\s*([^\n]+)")
_LINE_WORDS = 5
_NOT_NAMES = {"Ninguém", "Alguém", "Ele", "Ela", "Eles", "Elas", "Você", "Isso", "Isto", "Quem", "Nenhum",
              "Nenhuma", "Todos", "Todas", "Cada", "Também", "Ainda", "Já", "Não", "Como", "Quando", "Mas", "E",
              "O", "A", "Os", "As", "Um", "Uma", "Que", "Se", "Participante", "Transcrição", "Reunião"}

FALLBACK = (
    "Não existem elementos suficientes na reunião para responder com fidelidade a essa pergunta. "
    "Recomendo consultar a transcrição ou reformular a pergunta."
)


def is_portuguese(text: str) -> bool:
    words = re.findall(r"[a-zà-ÿ]+", text.lower())
    return sum(w in _PT for w in words) >= sum(w in _EN for w in words)


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


_NOT_MENTIONED = re.compile(r"^\W*isso não foi mencionado[^.\n]*[.\n]", re.IGNORECASE)


def contradicts_itself(answer: str) -> bool:
    """§3.5/§16: abre dizendo que o assunto não foi mencionado e depois responde sobre ele."""
    m = _NOT_MENTIONED.match(answer)
    return bool(m) and len(answer[m.end():].strip()) > 40


def check_answer(answer: str, context: str, question: str = "") -> list[str]:
    violations = []
    if not answer.strip():
        return ["resposta vazia"]
    if contradicts_itself(answer):
        violations.append("diz que o assunto não foi mencionado e em seguida responde sobre ele")
    if is_portuguese(question or "a reunião") and not is_portuguese(answer):
        violations.append("a resposta não está em português do Brasil")

    ctx = _normalize(context)
    for quote in _QUOTE.findall(answer):
        if len(quote.split()) >= _MIN_QUOTE_WORDS and _normalize(quote) not in ctx:
            violations.append(f"citação entre aspas que não está na transcrição: \"{quote[:80]}\"")

    # Linha citada com horário e autor: os dois precisam bater com a transcrição.
    for ts, speaker, text in _LINE.findall(answer):
        words = _normalize(text).split()[:_LINE_WORDS]
        if len(words) >= 3 and _normalize(f"{ts} {speaker}") + " " + " ".join(words) not in ctx:
            violations.append(f"trecho [{ts}] {speaker.strip()} não confere com a transcrição (horário, autor ou texto)")

    ctx_low = context.lower()
    for m in _ATTRIBUTION.finditer(answer):
        name = m.group(1) or m.group(2)
        if name not in _NOT_NAMES and name.lower() not in ctx_low:
            violations.append(f"atribui fala a \"{name}\", que não aparece na reunião")
    return list(dict.fromkeys(violations))


DECISION_STATUSES = ("aprovada", "condicionada", "proposta", "pendente", "rejeitada", "inconclusiva")
_STATUS_ALIASES = {"aprovado": "aprovada", "aprovada": "aprovada", "condicionado": "condicionada",
                   "proposto": "proposta", "rejeitado": "rejeitada", "inconclusivo": "inconclusiva",
                   "em discussão": "proposta", "em discussao": "proposta"}


def decision_status(raw: str) -> str:
    """Estado da decisão (§7). Desconhecido vira 'inconclusiva': nunca 'aprovada' por omissão."""
    key = (raw or "").strip().lower()
    key = _STATUS_ALIASES.get(key, key)
    return key if key in DECISION_STATUSES else "inconclusiva"


def responsible_or_undefined(name: str, transcript: str) -> str:
    """§7: só há responsável se o nome aparece na reunião; senão 'Não definido'."""
    name = (name or "").strip()
    if not name or name.lower() in ("não definido", "nao definido", "null", "none", "-"):
        return "Não definido"
    return name if name.lower() in transcript.lower() else "Não definido"
