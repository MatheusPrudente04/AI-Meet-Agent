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
        def send_message(self, to, subject, body, attachments=None):
            sent.append((to, subject, body, attachments))
    monkeypatch.setattr(email_mod, "EmailSender", FakeSender)
    return c


def test_email_so_sai_com_confirmacao_e_uma_vez(env, monkeypatch):
    out, m = env.ask("Mande para ana@empresa.com", email(["ana@empresa.com"]))
    sent = []
    c = _container(env, monkeypatch, sent)
    assert sent == []  # preparar não envia
    action = c.resolve_email_action(m, out.action["id"], send=True)
    assert action["status"] == "sent" and sent == [(["ana@empresa.com"], "Daily", "A entrega ficou para sexta.", [])]
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


def test_resumo_e_conversa_nao_se_apagam(env):
    """Regressão (2026-11-15): pergunta feita durante o resumo. A resposta do chat gravava a reunião
    inteira com a cópia antiga e apagava o resumo; o resumo gravado depois apagava a conversa."""
    from domain.entities.meeting import Summary
    repo = env.repo
    stale = repo.find_by_id("m1")              # o chat carregou a reunião antes do resumo sair
    stale.status = MeetingStatus.SUMMARIZING
    stale.summary = None
    worker_copy = repo.find_by_id("m1")         # o worker do resumo tem a sua cópia
    worker_copy.summary, worker_copy.status = Summary(overview="resumo novo"), MeetingStatus.DONE
    repo.save(worker_copy)                      # resumo terminou

    class LLM:
        def chat(self, *a, **k):
            return "resposta"
    ChatWithMeetingUC(LLM(), repository=repo).execute(ChatWithMeetingInput(meeting=stale, question="e aí?"))
    m = repo.find_by_id("m1")
    assert m.summary.overview == "resumo novo" and m.status is MeetingStatus.DONE  # o chat não apagou
    assert [x["content"] for x in m.chat_history] == ["e aí?", "resposta"]

    worker_copy.error = "nada"                  # nova gravação do pipeline com cópia sem a conversa
    repo.save(worker_copy)
    assert len(repo.find_by_id("m1").chat_history) == 2  # o pipeline não apagou a conversa


def test_email_com_arquivo_novo_sai_com_anexo(env, monkeypatch):
    tool = email(["ana@empresa.com"])
    tool["arguments"]["attachment"] = {"format": "pdf", "title": "Ata", "content": "## Decisões\n- Entrega na sexta"}
    out, m = env.ask("Mande a ata em PDF para ana@empresa.com", tool)
    assert [a["filename"] for a in out.action["attachments"]] == ["ata.pdf"]
    sent = []
    _container(env, monkeypatch, sent).resolve_email_action(m, out.action["id"], send=True)
    (_, _, _, files), = sent
    assert files[0][0] == "ata.pdf" and files[0][1].startswith(b"%PDF")


def test_email_anexa_documento_ja_gerado(env, monkeypatch):
    doc = {"name": "create_document", "arguments": {"format": "md", "title": "Ata", "content": "## Decisões\n- ok"}}
    _, m = env.ask("Gere a ata em markdown", doc)
    tool = email(["ana@empresa.com"])
    tool["arguments"]["attach_documents"] = True
    out, m = env.ask("Mande o arquivo para ana@empresa.com", tool)
    assert [a["filename"] for a in out.action["attachments"]] == ["ata.md"]


def test_pediu_anexo_sem_arquivo_avisa_em_vez_de_fingir(env):
    out, _ = env.ask("Mande o PDF para ana@empresa.com", email(["ana@empresa.com"]))
    assert out.action["attachments"] == [] and "Nenhum arquivo foi anexado" in out.answer


def test_anexo_sumiu_nao_envia_so_o_texto(env, monkeypatch):
    tool = email(["ana@empresa.com"])
    tool["arguments"]["attachment"] = {"format": "md", "title": "Ata", "content": "## Decisões\n- ok"}
    out, m = env.ask("Mande para ana@empresa.com", tool)
    for f in (env.docs / "m1").iterdir():
        f.unlink()
    sent = []
    with pytest.raises(RuntimeError, match="anexo"):
        _container(env, monkeypatch, sent).resolve_email_action(m, out.action["id"], send=True)
    assert sent == [] and env.repo.find_by_id("m1").chat_history[-1]["action"]["status"] == "pending"


def test_smtp_recebe_o_anexo_na_mensagem(monkeypatch):
    import smtplib
    from infrastructure.email.email_sender import EmailSender
    got = {}

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def starttls(self, **k): pass
        def auth(self, *a): pass
        def auth_plain(self, *a): pass
        def sendmail(self, frm, to, msg): got["msg"] = msg
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    sender = EmailSender()
    sender._settings = SimpleNamespace(email_enabled=True, smtp_user="a@b.com", smtp_password="x", smtp_from_email="",
                                       smtp_host="h", smtp_port=587)
    sender.send_message(["ana@empresa.com"], "Ata", "texto", [("ata.pdf", b"%PDF-1")])
    assert 'filename="ata.pdf"' in got["msg"] and "Content-Disposition: attachment" in got["msg"]
