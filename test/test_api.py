"""API: autenticação, limites e sessão de captura (ADR-007)."""
import io
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.security import capture_token
from domain.entities.meeting import Meeting, MeetingStatus
from infrastructure.json_meeting_repository import JsonMeetingRepository

KEY = "chave-de-teste"
AUTH = {"X-API-Key": KEY}


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        api_key=KEY, audio_storage_path=str(tmp_path / "audio"), max_upload_mb=1,
        capture_max_chunk_mb=1, capture_max_session_mb=2, app_mode="solo",
    )
    repo = JsonMeetingRepository(str(tmp_path / "meetings"))
    queued = []
    container = SimpleNamespace(
        repository=repo,
        enqueue_processing=lambda *a, **k: queued.append(a),
        dispatch_bot=lambda url, title: "id-bot",
    )
    import api.main as main
    for mod in ("api.security", "api.routers.capture", "api.routers.meetings"):
        monkeypatch.setattr(f"{mod}.get_settings", lambda: settings)
    for mod in ("api.routers.capture", "api.routers.meetings", "api.chat"):
        monkeypatch.setattr(f"{mod}.get_container", lambda: container)
    return SimpleNamespace(client=TestClient(main.app), repo=repo, queued=queued, settings=settings, tmp=tmp_path)


def test_rotas_exigem_chave_e_health_nao(ctx):
    assert ctx.client.get("/health").status_code != 401
    assert ctx.client.get("/meetings/").status_code == 401
    assert ctx.client.get("/meetings/", headers={"X-API-Key": "errada"}).status_code == 401
    assert ctx.client.post("/meetings/x/chat", json={"question": "oi"}).status_code == 401
    assert ctx.client.get("/meetings/", headers=AUTH).status_code == 200


def test_upload_grava_em_disco_e_enfileira(ctx):
    r = ctx.client.post("/meetings/upload", headers=AUTH, files={"file": ("a.wav", io.BytesIO(b"x" * 10))})
    assert r.status_code == 200 and len(ctx.queued) == 1
    assert (ctx.tmp / "audio" / f"{r.json()['meeting_id']}.wav").read_bytes() == b"x" * 10


def test_upload_acima_do_teto_devolve_413_e_nao_deixa_arquivo(ctx):
    big = io.BytesIO(b"x" * (1024 * 1024 + 1))
    r = ctx.client.post("/meetings/upload", headers=AUTH, files={"file": ("a.wav", big)})
    assert r.status_code == 413 and not ctx.queued
    assert list((ctx.tmp / "audio").glob("*")) == []


def test_upload_recusa_extensao_estranha_e_arquivo_vazio(ctx):
    assert ctx.client.post("/meetings/upload", headers=AUTH, files={"file": ("a.sh", b"x")}).status_code == 415
    assert ctx.client.post("/meetings/upload", headers=AUTH, files={"file": ("a.wav", b"")}).status_code == 422


def chunk(ctx, session="m1", data=b"abc", token=None):
    token = capture_token(session, KEY) if token is None else token
    return ctx.client.post(f"/capture/chunk?session={session}&seq=0", content=data, headers={"X-Capture-Token": token})


def test_captura_exige_token_da_propria_sessao(ctx):
    ctx.repo.save(Meeting(id="m1", title="t"))
    assert chunk(ctx, token="").status_code == 401
    assert chunk(ctx, token=capture_token("outra", KEY)).status_code == 401
    assert chunk(ctx, token=KEY).status_code == 401  # a chave da API não serve de token
    assert chunk(ctx).status_code == 200
    assert (ctx.tmp / "audio" / "m1.webm").read_bytes() == b"abc"


def test_captura_so_aceita_reuniao_existente_e_aberta(ctx):
    assert chunk(ctx, session="fantasma").status_code == 404
    ctx.repo.save(Meeting(id="m1", title="t", status=MeetingStatus.RECORDING))
    assert chunk(ctx).status_code == 200
    ctx.repo.save(Meeting(id="m1", title="t", status=MeetingStatus.TRANSCRIBING))
    assert chunk(ctx).status_code == 409
    status = ctx.client.get("/capture/status?session=m1", headers={"X-Capture-Token": capture_token("m1", KEY)})
    assert status.json()["finalized"] is True


def test_captura_limita_chunk_e_sessao(ctx):
    ctx.repo.save(Meeting(id="m1", title="t"))
    mb = b"x" * (1024 * 1024)
    assert chunk(ctx, data=mb + b"x").status_code == 413          # chunk acima de 1 MB
    assert chunk(ctx, data=mb).status_code == 200
    assert chunk(ctx, data=mb).status_code == 200
    assert chunk(ctx, data=b"x").status_code == 413               # sessão acima de 2 MB
    assert chunk(ctx, session="../m1").status_code in (400, 401)


def test_sem_chave_configurada_a_api_fica_aberta_em_desenvolvimento(ctx):
    ctx.settings.api_key = ""
    assert ctx.client.get("/meetings/").status_code == 200


def test_log_carrega_o_id_da_reuniao(capsys):
    import logging
    from config.logging_setup import bind_meeting, setup_logging
    root = logging.getLogger()
    saved = root.handlers[:], root.level
    try:
        setup_logging()
        bind_meeting("abc-123")
        logging.getLogger("qualquer.lib").info("mensagem")
        assert "[abc-123] qualquer.lib: mensagem" in capsys.readouterr().err
    finally:
        root.handlers, _ = saved[0], root.setLevel(saved[1])
        bind_meeting("-")


# ── Sprint 4: tarefas, exportação, e-mail, agenda ────────────────────────

def _meeting_with_tasks(ctx):
    from domain.entities.meeting import Summary, Task
    m = Meeting(id="m1", title="Planejamento", transcript_text="Ana: oi", participants=["Ana"],
                summary=Summary(overview="geral", tasks=[Task("Revisar fluxo", "Ana", "sexta"), Task("Planejar QA")]),
                status=MeetingStatus.DONE)
    ctx.repo.save(m)
    return m


def test_marcar_tarefa_sobrevive_a_recarregar(ctx, monkeypatch):
    from presentation.container import Container
    _meeting_with_tasks(ctx)
    fake = SimpleNamespace(repository=ctx.repo)
    fake.set_task_done = lambda *a: Container.set_task_done(fake, *a)
    monkeypatch.setattr("api.routers.meetings.get_container", lambda: fake)
    r = ctx.client.patch("/meetings/m1/tasks/1", headers=AUTH, json={"done": True})
    assert r.status_code == 200 and [t["done"] for t in r.json()["summary"]["tasks"]] == [False, True]
    assert [t.done for t in ctx.repo.find_by_id("m1").summary.tasks] == [False, True]
    assert ctx.client.patch("/meetings/m1/tasks/9", headers=AUTH, json={"done": True}).status_code == 404
    assert ctx.client.patch("/meetings/nada/tasks/0", headers=AUTH, json={"done": True}).status_code == 404


def test_exportacao_markdown(ctx):
    _meeting_with_tasks(ctx)
    r = ctx.client.get("/meetings/m1/export.md", headers=AUTH)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert "# Planejamento" in r.text and "- [ ] **Revisar fluxo** — Ana (sexta)" in r.text and "Ana: oi" in r.text


def test_email_de_tarefas(ctx, monkeypatch):
    import infrastructure.email.email_sender as mod
    _meeting_with_tasks(ctx)
    sent = []

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self, context=None): pass
        def login(self, user, password): sent.append(("login", user))
        def sendmail(self, frm, to, body): sent.append((frm, to, body))

    smtp = SimpleNamespace(smtp_user="", smtp_password="", smtp_from_email="", smtp_host="h", smtp_port=1, email_enabled=False)
    monkeypatch.setattr(mod, "get_settings", lambda: smtp)
    monkeypatch.setattr(mod.smtplib, "SMTP", FakeSMTP)

    # sem SMTP configurado: recusa com mensagem clara, não tenta enviar
    r = ctx.client.post("/meetings/m1/tasks/email", headers=AUTH, json={"to": "ana@empresa.com"})
    assert r.status_code == 409 and "SMTP" in r.json()["detail"] and not sent

    smtp.smtp_user, smtp.smtp_password, smtp.email_enabled = "bot@empresa.com", "segredo", True
    assert ctx.client.post("/meetings/m1/tasks/email", headers=AUTH, json={"to": "nao-e-email"}).status_code == 422
    r = ctx.client.post("/meetings/m1/tasks/email", headers=AUTH, json={"to": "ana@empresa.com"})
    assert r.status_code == 200 and sent[1][:2] == ("bot@empresa.com", "ana@empresa.com")
    import email
    body = email.message_from_string(sent[1][2]).get_payload()[0].get_payload(decode=True).decode()
    assert "Revisar fluxo — responsável: Ana — prazo: sexta" in body and "Planejar QA" in body


def test_agenda_da_titulo_ao_bot_e_falha_da_agenda_nao_impede(tmp_path):
    from presentation.container import Container
    from infrastructure.json_meeting_repository import JsonMeetingRepository

    class Cal:
        def __init__(self, title=None, boom=False): self.title, self.boom = title, boom
        def find_by_meet_url(self, url):
            if self.boom:
                raise RuntimeError("sem token")
            return SimpleNamespace(title=self.title) if self.title else None

    c = Container.__new__(Container)
    c.repository, c._is_collab = JsonMeetingRepository(str(tmp_path)), True
    import worker.tasks as tasks
    dispatched = []
    orig = tasks.join_meeting_task.delay
    tasks.join_meeting_task.delay = lambda *a: dispatched.append(a)
    try:
        url = "https://meet.google.com/abc-defg-hij"
        c.calendar = Cal("Sprint Review")
        assert c.repository.find_by_id(c.dispatch_bot(url)).title == "Sprint Review"
        assert c.repository.find_by_id(c.dispatch_bot(url, "Meu título")).title == "Meu título"
        c.calendar = Cal(boom=True)
        assert c.repository.find_by_id(c.dispatch_bot(url)).title.startswith("Reunião ")
        c.calendar = None
        assert c.repository.find_by_id(c.dispatch_bot(url)).title.startswith("Reunião ")
        assert len(dispatched) == 4
    finally:
        tasks.join_meeting_task.delay = orig
