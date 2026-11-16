"""Cabe ou não cabe no LLM (ADR-010).

Medido em reuniões reais: ~295 tokens por minuto de reunião e ~2,6 caracteres
por token. Uma reunião de 2 h passa de 35 mil tokens e não cabe num pedido só.
Aqui ficam as duas saídas: dividir (resumo em partes) e selecionar (chat).
"""
import re
import unicodedata


def split_by_lines(text: str, max_chars: int) -> list[str]:
    """Divide em partes de até `max_chars`, sem quebrar uma fala no meio.
    Uma linha maior que o limite vira uma parte sozinha (não é cortada)."""
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines():
        if current and size + len(line) + 1 > max_chars:
            parts.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        parts.append("\n".join(current))
    return parts or [""]


def _words(text: str) -> set[str]:
    plain = unicodedata.normalize("NFKD", text.casefold()).encode("ascii", "ignore").decode()
    return {w for w in re.findall(r"[a-z0-9]+", plain) if len(w) > 3}


def select_relevant(text: str, question: str, max_chars: int, part_chars: int = 4000) -> str:
    """Trechos da transcrição com mais palavras em comum com a pergunta, do
    mais ao menos relacionado, até `max_chars`. O mais relacionado vai
    primeiro porque o modelo local ignora com facilidade o meio do contexto;
    a ordem da reunião continua legível pelos horários de cada fala.

    ponytail: casamento por palavra, sem sinônimos; trocar por busca vetorial
    se as respostas em reuniões longas começarem a perder contexto.
    """
    parts = split_by_lines(text, part_chars)
    wanted = _words(question)
    ranked = sorted(range(len(parts)), key=lambda i: (-len(wanted & _words(parts[i])), i))
    chosen: list[int] = []
    used = 0
    for i in ranked:
        if used + len(parts[i]) > max_chars and chosen:
            continue
        chosen.append(i)
        used += len(parts[i])
    return "\n[...]\n".join(parts[i] for i in chosen)
