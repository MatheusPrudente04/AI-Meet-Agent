"""Avaliação do agente contra o contrato (SPEC_CONTRACT.md) — S8-2, ADR-014.

    python -m scripts.eval_agent            # agente configurado no container.py
    python -m scripts.eval_agent --show     # imprime também as respostas

Usa o LLM de verdade (o do .env): leva alguns minutos com o Ollama local.
Uma reunião sintética curta tem, de propósito, uma decisão aprovada, uma proposta
adiada, uma tarefa com dono, uma tarefa sem dono e uma divergência. Cada pergunta
testa uma regra do contrato. As checagens são por palavra-chave: servem para
comparar versões do agente, não como prova; leia as respostas com --show.
"""
from __future__ import annotations

import argparse
import re
import sys
import time

from domain.entities.meeting import Meeting

TRANSCRIPT = """[00:00] Ana: Bom dia. O objetivo de hoje é decidir como integrar o sistema de pedidos com o financeiro.
[00:20] Bruno: Eu proponho usar uma fila de mensagens entre os dois sistemas, mas preciso validar com a infraestrutura.
[00:45] Carla: O financeiro fecha o mês no dia 25, então qualquer mudança precisa estar pronta antes disso.
[01:10] Ana: Então está decidido: a integração entra em produção antes do dia 25.
[01:20] Bruno: Concordo.
[01:45] Bruno: Sobre migrar o banco de dados para a nuvem, acho que vale a pena estudar, mas não precisa ser agora.
[02:05] Ana: Vamos deixar a migração para a nuvem para discutir em outra reunião.
[02:20] Ana: Carla, você fica responsável por enviar o calendário de fechamento até sexta-feira.
[02:30] Carla: Combinado, envio até sexta.
[02:45] Bruno: Alguém precisa revisar os contratos com o fornecedor da fila.
[03:00] Ana: Ainda não sabemos quem vai fazer isso.
[03:10] Carla: Eu discordo de colocar a integração antes do fechamento, é arriscado mexer perto do dia 25.
[03:25] Ana: Entendo, mas a decisão está mantida."""

PARTICIPANTS = ["Ana", "Bruno", "Carla"]
NAMES = "|".join(PARTICIPANTS)
NOT_FOUND = ["não foi mencionad", "não há ", "não houve", "não consta", "não foi discutid", "não foi informad",
             "não foi definid", "não existem elementos", "não foi citad", "não foi abordad", "não identifiquei",
             "não foi estabelecid", "não há informaç", "não foram mencionad", "não foi apresentad"]

# (pergunta, regra do contrato, checagens)
#   any: ao menos um termo · all: todos · none: nenhum regex pode casar
QUESTIONS = [
    ("Quais decisões foram aprovadas na reunião?", "§7 decisão aprovada",
     {"all": ["25"], "none": [r"nuvem[^.\n]{0,60}\b(foi aprovad|aprovad[oa]\b)",  # nuvem como aprovada
                              r"\b(duas|três|2|3)\s+decis", r"\n\s*\d+\.[^\n]*nuvem"]}),
    ("A migração do banco de dados para a nuvem foi aprovada?", "§4 proposta ≠ decisão",
     {"any": ["não foi aprovad", "não foi decidid", "adiad", "outra reunião", "não houve aprovação", "proposta",
              "não chegou a ser aprovad", "não foi tomada"],
      "none": [r"^\s*sim\b", r"(?<!não )\bfoi aprovada\b"]}),
    ("Quem ficou responsável por revisar os contratos com o fornecedor da fila?", "§7 responsável só se atribuído",
     {"any": NOT_FOUND + ["ninguém", "não sabemos", "não se sabe", "não definiu", "indefinid", "não foi atribuíd",
                          "ainda não", "sem responsável"],
      "none": [rf"\b({NAMES})\s+(ficou|é|será|está|foi)\s+(o |a )?respons"]}),
    ("Qual foi o orçamento aprovado para a integração?", "§4/§16 não preencher lacunas",
     {"any": NOT_FOUND + ["não foi aprovado nenhum orçamento", "nenhum orçamento", "não se falou", "não trata"],
      "none": [r"R\$\s*\d"]}),
    ("Quem ficou responsável pelo calendário de fechamento e até quando?", "§7 compromisso explícito",
     {"all": ["Carla", "sexta"]}),
    ("Todos concordaram com a data da integração?", "§4 não presumir concordância",
     {"all": ["Carla"], "any": ["discord", "arrisc", "não"]}),
    ("O que você achou da reunião?", "§11 avaliação crítica",
     {"any": ["risco", "arrisc", "pendente", "não ficou", "esclarec", "melhorar", "lacuna", "indefinid"],
      "min_chars": 300}),
]

from use_cases.contract_checks import contradicts_itself, is_portuguese


def check(answer: str, rules: dict) -> list[str]:
    """Regras violadas pela resposta (vazio = passou)."""
    low = answer.lower()
    fails = []
    if not is_portuguese(answer):
        fails.append("não está em português")
    if contradicts_itself(answer):
        fails.append("diz que não foi mencionado e responde mesmo assim")
    if "all" in rules and not all(t.lower() in low for t in rules["all"]):
        fails.append(f"faltou um de {rules['all']}")
    if "any" in rules and not any(t.lower() in low for t in rules["any"]):
        fails.append("faltou o termo esperado")
    for pattern in rules.get("none", []):
        if re.search(pattern, answer, re.IGNORECASE | re.MULTILINE):
            fails.append(f"casou o proibido /{pattern}/")
    if len(answer) < rules.get("min_chars", 0):
        fails.append(f"curta demais ({len(answer)} caracteres)")
    return fails


def check_summary(summary) -> list[str]:
    fails = []
    for d in summary.decisions:
        if "nuvem" in d.description.lower():
            status = getattr(d, "status", "")
            if status == "aprovada" or (not status and not re.search(r"adiad|outra reunião|discutir|propost",
                                                                     d.description + d.context, re.I)):
                fails.append(f"proposta registrada como decisão aprovada: {d.description!r}")
    for t in summary.tasks:
        if "contrat" in t.description.lower() and t.responsible in PARTICIPANTS:
            fails.append(f"tarefa sem dono atribuída a {t.responsible}")
    if not any(t.responsible == "Carla" and "calend" in t.description.lower() for t in summary.tasks):
        fails.append("faltou a tarefa da Carla (calendário de fechamento)")
    return fails


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--show", action="store_true", help="imprime as respostas")
    args = parser.parse_args()

    from presentation.container import get_container
    from use_cases.chat_with_meeting import ChatWithMeetingInput
    from use_cases.summarize_meeting import SummarizeMeetingInput

    c = get_container()
    meeting = Meeting(id="eval", title="Integração pedidos x financeiro", transcript_text=TRANSCRIPT,
                      transcript_formatted=TRANSCRIPT, participants=PARTICIPANTS)
    print(f"Agente: {type(c.chat_with_meeting._llm).__name__}\n")

    total = passed = 0
    t0 = time.time()
    s = c.summarize_meeting.execute(SummarizeMeetingInput(meeting=meeting))
    took = time.time() - t0
    fails = [s.error_message] if not s.success else check_summary(s.summary)
    meeting.summary = s.summary if s.success else None  # como em produção: o chat vem depois do resumo
    total, passed = total + 1, passed + (not fails)
    print(f"{'OK   ' if not fails else 'FALHA'} resumo ({took:.0f}s) {'; '.join(fails)}")
    if args.show and s.success:
        print("   decisões:", [(d.description, getattr(d, 'status', '')) for d in s.summary.decisions])
        print("   tarefas:", [(t.description, t.responsible) for t in s.summary.tasks])

    for question, rule, rules in QUESTIONS:
        t0 = time.time()
        r = c.chat_with_meeting.execute(ChatWithMeetingInput(meeting=meeting, question=question, history=[]))
        took = time.time() - t0
        fails = [r.error_message] if not r.success else check(r.answer, rules)
        total, passed = total + 1, passed + (not fails)
        print(f"{'OK   ' if not fails else 'FALHA'} {rule} ({took:.0f}s) {'; '.join(fails)}")
        if args.show:
            print("   P:", question, "\n   R:", r.answer.replace("\n", "\n      "), "\n")

    print(f"\n{passed}/{total} checagens passaram")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
