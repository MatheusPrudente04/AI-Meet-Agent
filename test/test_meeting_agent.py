"""Agente em LangGraph e regras do contrato (ADR-014), sem modelo de verdade."""
import json

import pytest

from domain.entities.meeting_type import MeetingType
from domain.interfaces.llm_service import LLMServiceError
from infrastructure.llm.meeting_agent import MeetingAgent, parse_summary
from use_cases.contract_checks import FALLBACK, check_answer, decision_status

CTX = """[00:00] Ana: Bom dia, o objetivo é integrar pedidos com o financeiro.
[01:45] Bruno: Sobre migrar o banco de dados para a nuvem, acho que vale a pena estudar.
[02:05] Ana: Vamos deixar a migração para a nuvem para discutir em outra reunião."""


class FakeModel:
    """Devolve as respostas em ordem e guarda as mensagens recebidas."""

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def complete(self, messages, as_json=False):
        self.calls.append(messages)
        return self.answers.pop(0)


def agent(*answers):
    model = FakeModel(*answers)
    return MeetingAgent(model=model, contract="CONTRATO DE TESTE"), model


# ── Checagens do contrato ────────────────────────────────────────────

def test_resposta_fiel_passa():
    ok = 'Ana disse que o objetivo é integrar pedidos. Ela afirmou: "Vamos deixar a migração para a nuvem para discutir em outra reunião."'
    assert check_answer(ok, CTX, "O que foi dito?") == []


def test_citacao_inventada_e_fala_de_quem_nao_estava():
    assert any("citação" in v for v in check_answer('Ana disse: "a migração está aprovada para março"', CTX))
    assert any("Carlos" in v for v in check_answer("Segundo Carlos, o prazo é março.", CTX))
    assert check_answer("Ninguém disse quem fará a revisão.", CTX) == []


def test_linha_com_horario_ou_autor_trocado():
    certo = "- [01:45] Bruno: Sobre migrar o banco de dados para a nuvem, acho"
    trocado = "- [02:05] Bruno: Sobre migrar o banco de dados para a nuvem, acho"
    assert check_answer(certo, CTX) == []
    assert any("[02:05] Bruno" in v for v in check_answer(trocado, CTX))


def test_contradicao_e_idioma():
    assert any("não foi mencionado" in v for v in check_answer(
        "Isso não foi mencionado durante a reunião. Mas Ana disse que o objetivo é integrar pedidos.", CTX))
    assert check_answer("Isso não foi mencionado durante a reunião.", CTX) == []
    assert any("português" in v for v in check_answer("The meeting was about the integration and it was fine.", CTX, "Como foi?"))
    assert check_answer("The meeting was about integrating orders.", CTX, "What was the meeting about?") == []


def test_status_da_decisao_nunca_aprovada_por_omissao():
    assert decision_status("Aprovado") == "aprovada"
    assert decision_status("em discussão") == "proposta"
    assert decision_status("") == decision_status("talvez") == "inconclusiva"


# ── Grafo do chat ────────────────────────────────────────────────────

def test_chat_leva_o_contrato_e_o_contexto():
    a, model = agent("A reunião tratou da integração de pedidos com o financeiro.")
    assert a.chat("Do que tratou?", CTX, history=[{"role": "user", "content": "oi"}]).startswith("A reunião")
    system = model.calls[0][0]["content"]
    assert system.startswith("CONTRATO DE TESTE") and CTX in system
    assert [m["role"] for m in model.calls[0]] == ["system", "user", "user"]


def test_chat_corrige_uma_vez_com_as_violacoes():
    a, model = agent('Segundo Carlos, "a migração está aprovada para março".', "A migração não foi aprovada.")
    assert a.chat("A migração foi aprovada?", CTX, history=[]) == "A migração não foi aprovada."
    correction = model.calls[1][-1]["content"]
    assert "violou o contrato" in correction and "Carlos" in correction


def test_chat_desiste_com_resposta_padrao():
    a, model = agent("Segundo Carlos, sim.", "Segundo Carlos, sim mesmo.")
    assert a.chat("Foi aprovada?", CTX, history=[]) == FALLBACK
    assert len(model.calls) == 2


# ── Grafo do resumo ──────────────────────────────────────────────────

SUMMARY = {"overview": "Integração", "topics": ["Financeiro"],
           "tasks": [{"description": "Revisar contratos", "responsible": "Carlos"},
                     {"description": "Enviar calendário", "responsible": "Ana", "deadline": "sexta"}],
           "decisions": [{"description": "Migrar para a nuvem", "status": "talvez"},
                         {"description": "Integrar antes do dia 25", "status": "aprovado"}]}


def test_resumo_aplica_o_contrato():
    a, model = agent(json.dumps(SUMMARY))
    s = a.summarize(CTX, MeetingType.PLANNING)
    assert [(t.description, t.responsible) for t in s.tasks] == [("Revisar contratos", "Não definido"),
                                                                  ("Enviar calendário", "Ana")]
    assert [d.status for d in s.decisions] == ["inconclusiva", "aprovada"]
    system = model.calls[0][0]["content"]
    assert system.startswith("CONTRATO DE TESTE") and "planejamento" in system.lower()


def test_resumo_refaz_json_invalido_e_desiste_na_segunda():
    a, model = agent("isto não é json", json.dumps(SUMMARY))
    assert a.summarize(CTX).overview == "Integração"
    assert "inválido" in model.calls[1][-1]["content"]
    with pytest.raises(LLMServiceError, match="JSON inválido"):
        agent("nada", "nada de novo")[0].summarize(CTX)


def test_json_com_chave_final_cortada():
    assert parse_summary('{"overview": "ok", "topics": [], "tasks": [], "decisions": []', "").overview == "ok"


def test_contrato_padrao_e_o_spec_do_repositorio():
    from infrastructure.llm.meeting_agent import load_contract
    assert "MEETING-INTELLIGENCE-001" in load_contract("SPEC_CONTRACT.md")
    with pytest.raises(LLMServiceError, match="não encontrado"):
        load_contract("nao-existe.md")


def test_chat_recebe_decisoes_classificadas_do_resumo():
    from domain.entities.meeting import Decision, Meeting, Summary
    from use_cases.chat_with_meeting import ChatWithMeetingInput, ChatWithMeetingUC
    a, model = agent("Só a integração foi aprovada.")
    m = Meeting(id="m", title="t", transcript_text=CTX, transcript_formatted=CTX,
                summary=Summary(decisions=[Decision("Migrar para a nuvem", status="proposta")]))
    ChatWithMeetingUC(a).execute(ChatWithMeetingInput(meeting=m, question="O que foi aprovado?", history=[]))
    assert "- [proposta] Migrar para a nuvem" in model.calls[0][0]["content"]
