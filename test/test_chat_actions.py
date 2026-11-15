"""Ferramentas do chat: e-mail com confirmação e documentos (PDF, Word, Markdown)."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from domain.entities.meeting import Meeting, MeetingStatus
from domain.interfaces.llm_service import ChatReply
from infrastructure.exporters.document_writer import write_document
from infrastructure.json_meeting_repository import JsonMeetingRepository
from presentation.container import Container
from use_cases.chat_with_meeting import ChatWithMeetingInput, ChatWithMeetingUC


class ToolLLM:
    def __init__(self, tool):
        self.tool = tool

    def chat(self, question, context, history, **_):
        return ChatReply("", self.tool)


@pytest.fixture
def env(tmp_path):
    repo = JsonMeetingRepository(str(tmp_path / "meetings"))
    meeting = Meeting(id="m1", title="Daily", transcript_text="Ana: entrega na sexta.", status=MeetingStatus.DONE)
    repo.save(meeting)
    docs = tmp_path / "documents"

    def ask(question, tool, history=None):
        uc = ChatWithMeetingUC(ToolLLM(tool), repository=repo, documents_dir=docs, write_document=write_document)
        m = repo.find_by_id("m1")
        if history:
            m.chat_history = history
        return uc.execute(ChatWithMeetingInput(meeting=m, question=question)), repo.find_by_id("m1")
    return SimpleNamespace(repo=repo, docs=docs, ask=ask, tmp=tmp_path)


def email(to, body="A entrega ficou para sexta."):
    return {"name": "send_email", "arguments": {"to": to, "subject": "Daily", "body": body}}


def test_email_so_para_endereco_que_o_usuario_escreveu(env):
    out, m = env.ask("Mande a decisão por e-mail para a Ana", email(["ana@empresa.com"]))
    assert out.action is None and "endereço" in out.answer  # o modelo inventou o endereço

    out, m = env.ask("Mande para ana@empresa.com", email(["ana@empresa.com", "chefe@empresa.com"]))
    assert out.action is None  # um dos dois não foi escrito pelo usuário

    out, m = env.ask("Mande para Ana@Empresa.com", email(["ana@empresa.com"]))
    assert out.action["status"] == "pending" and out.action["to"] == ["ana@empresa.com"]
    assert m.chat_history[-1]["action"]["id"] == out.action["id"]  # guardado na conversa


def test_endereco_dito_antes_na_conversa_vale(env):
    history = [{"role": "user", "content": "o e-mail da Ana é ana@empresa.com"},
               {"role": "assistant", "content": "Anotado."}]
    out, _ = env.ask("Agora mande o resumo para ela", email(["ana@empresa.com"]), history=history)
    assert out.action and out.action["to"] == ["ana@empresa.com"]


def _container(env, monkeypatch, sent):
    import presentation.container as container_mod
    import infrastructure.email.email_sender as email_mod
    c = Container.__new__(Container)
    c.repository, c.documents_dir = env.repo, env.docs
    monkeypatch.setattr(container_mod, "get_settings", lambda: SimpleNamespace(email_enabled=True))

    class FakeSender:
        def send_message(self, to, subject, body):
            sent.append((to, subject, body))
    monkeypatch.setattr(email_mod, "EmailSender", FakeSender)
    return c


def test_email_so_sai_com_confirmacao_e_uma_vez(env, monkeypatch):
    out, m = env.ask("Mande para ana@empresa.com", email(["ana@empresa.com"]))
    sent = []
    c = _container(env, monkeypatch, sent)
    assert sent == []  # preparar não envia
    action = c.resolve_email_action(m, out.action["id"], send=True)
    assert action["status"] == "sent" and sent == [(["ana@empresa.com"], "Daily", "A entrega ficou para sexta.")]
    with pytest.raises(ValueError):
        c.resolve_email_action(env.repo.find_by_id("m1"), out.action["id"], send=True)
    assert len(sent) == 1
    assert env.repo.find_by_id("m1").chat_history[-1]["action"]["status"] == "sent"


def test_email_descartado_nao_sai(env, monkeypatch):
    out, m = env.ask("Mande para ana@empresa.com", email(["ana@empresa.com"]))
    sent = []
    c = _container(env, monkeypatch, sent)
    assert c.resolve_email_action(m, out.action["id"], send=False)["status"] == "canceled"
    assert sent == []


@pytest.mark.parametrize("fmt, alias", [("pdf", "pdf"), ("docx", "word"), ("md", "markdown"), ("xlsx", "excel"),
                                       ("csv", "csv"), ("html", "dashboard")])
def test_documento_gerado_baixavel_e_apagado_com_a_reuniao(env, monkeypatch, fmt, alias):
    tool = {"name": "create_document", "arguments": {"format": alias, "title": "Ata da Daily",
                                                     "content": "## Decisões\n- Entrega na **sexta**"}}
    out, m = env.ask(f"Gere a ata em {alias}", tool)
    assert out.action["format"] == fmt and out.action["filename"] == f"ata-da-daily.{fmt}"
    c = _container(env, monkeypatch, [])
    path, action = c.document_file(m, out.action["id"])
    assert path.stat().st_size > 0 and path.parent == env.docs / "m1"
    c.delete_meeting(m)
    assert not path.exists()


def test_formato_desconhecido_explica_e_nao_gera(env):
    tool = {"name": "create_document", "arguments": {"format": "pptx", "title": "T", "content": "x"}}
    out, _ = env.ask("Gere uma planilha", tool)
    assert out.action is None and "PDF" in out.answer and not env.docs.exists()


def test_resposta_em_texto_continua_igual(env):
    class TextLLM:
        def chat(self, *a, **k):
            return "Ana disse que a entrega é na sexta."
    uc = ChatWithMeetingUC(TextLLM(), repository=env.repo)
    out = uc.execute(ChatWithMeetingInput(meeting=env.repo.find_by_id("m1"), question="Quando é a entrega?"))
    assert out.action is None and out.answer.startswith("Ana")


def test_historico_mostra_acao_como_nota_do_sistema(env):
    seen = []

    class SpyLLM:
        def chat(self, question, context, history, **_):
            seen.extend(history)
            return "ok"
    m = env.repo.find_by_id("m1")
    m.chat_history = [{"role": "user", "content": "gere um pdf"},
                      {"role": "assistant", "content": "Gerei o documento Ata (PDF).", "action": {"type": "document"}}]
    ChatWithMeetingUC(SpyLLM(), repository=env.repo).execute(ChatWithMeetingInput(meeting=m, question="e agora?"))
    assert "Gerei" not in seen[1]["content"] and "create_document" in seen[1]["content"]


def test_numeros_da_reuniao_vao_no_contexto():
    from domain.entities.meeting import Decision, Summary, Task
    from use_cases.chat_with_meeting import _stats_block
    m = Meeting(id="x", title="t", transcript_formatted="[00:00] Ana: oi\n[00:10] Bruno: olá\n[00:20] Ana: tchau",
                summary=Summary(tasks=[Task("a", "Carla")], decisions=[Decision("d", status="aprovada")]))
    block = _stats_block(m)
    assert "Ana 2, Bruno 1" in block and "Carla 1" in block and "aprovada 1" in block
    assert "aprovada 1" in _stats_block(Meeting(id="y", title="t", summary=Summary(decisions=[Decision("d", status="aprovada")])))
