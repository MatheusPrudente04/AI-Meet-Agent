"""API: autenticação, limites e sessão de captura (ADR-007)."""
import io
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from api.security import capture_token
from domain.entities.meeting import Meeting, MeetingStatus
from infrastructure.json_meeting_repository import JsonMeetingRepository

from presentation.container import Container
from use_cases.auth import AuthService

KEY = "chave-de-teste"
AUTH = {"X-API-Key": KEY}


class MemoryAuthStore:
    """Mesmo contrato do MongoAuthStore, em memória."""

    def __init__(self):
        self.users, self.sessions = {}, {}

    def add_user(self, user):
        if any(u.email == user.email for u in self.users.values()):
            return False
        self.users[user.id] = user
        return True

    def user_by_email(self, email):
        return next((u for u in self.users.values() if u.email == email), None)

    def user_by_id(self, user_id):
        return self.users.get(user_id)

    def count_users(self):
        return len(self.users)

    def add_session(self, session):
        self.sessions[session["_id"]] = session

    def session(self, token_hash):
        return self.sessions.get(token_hash)

    def delete_session(self, token_hash):
        self.sessions.pop(token_hash, None)


class FakeContainer:
    """Container sem fila nem LLM; as regras de dono são as de verdade."""
    meeting_for = Container.meeting_for
    register_user = Container.register_user
    set_task_done = Container.set_task_done
    auth = None  # como em solo; os testes de login ligam com `with_login`

    def __init__(self, repo, queued):
        self.repository, self.queued = repo, queued

    def enqueue_processing(self, meeting_id, audio_path, title, wait=False, owner_id=""):
        self.queued.append((meeting_id, audio_path, title))
        self.repository.save(Meeting(id=meeting_id, title=title, owner_id=owner_id, audio_path=audio_path))

    def dispatch_bot(self, url, title="", owner_id=""):
        from infrastructure.recorder.platforms import detect_platform
        platform = detect_platform(url)
        self.repository.save(Meeting(id="id-bot", title=title, owner_id=owner_id, platform=platform.key))
        return "id-bot"


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        api_key=KEY, audio_storage_path=str(tmp_path / "audio"), max_upload_mb=1,
        capture_max_chunk_mb=1, capture_max_session_mb=2, app_mode="solo",
    )
    repo = JsonMeetingRepository(str(tmp_path / "meetings"))
    queued = []
    container = FakeContainer(repo, queued)
    import api.main as main
    for mod in ("api.security", "api.routers.capture", "api.routers.meetings"):
        monkeypatch.setattr(f"{mod}.get_settings", lambda: settings)
    for mod in ("api.security", "api.routers.auth", "api.routers.capture", "api.routers.meetings", "api.chat"):
        monkeypatch.setattr(f"{mod}.get_container", lambda: container)
    return SimpleNamespace(client=TestClient(main.app), repo=repo, queued=queued, settings=settings,
                           tmp=tmp_path, container=container)


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
    _meeting_with_tasks(ctx)
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


# ── Sprint 5: login multiusuário, plataformas e contrato da API ──────────

@pytest.fixture
def with_login(ctx):
    ctx.store = MemoryAuthStore()
    ctx.container.auth = AuthService(ctx.store)
    return ctx


def signup(ctx, email="ana@empresa.com", name="Ana", password="senha-forte"):
    r = ctx.client.post("/auth/register", json={"name": name, "email": email, "password": password})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}, r.json()["user"]


def test_cadastro_login_e_logout(with_login):
    c = with_login.client
    ana, user = signup(with_login, email="Ana@Empresa.com")
    assert user["email"] == "ana@empresa.com" and "password" not in str(user)
    assert c.get("/auth/me", headers=ana).json()["name"] == "Ana"

    assert c.post("/auth/register", json={"name": "X", "email": "ana@empresa.com", "password": "outra-senha"}).status_code == 409
    assert c.post("/auth/register", json={"name": "X", "email": "x@y.com", "password": "curta"}).status_code == 422
    assert c.post("/auth/register", json={"name": "X", "email": "sem-arroba", "password": "senha-forte"}).status_code == 409

    assert c.post("/auth/login", json={"email": "ana@empresa.com", "password": "errada-mesmo"}).status_code == 401
    assert c.post("/auth/login", json={"email": "ninguem@x.com", "password": "senha-forte"}).status_code == 401
    r = c.post("/auth/login", json={"email": "ANA@empresa.com", "password": "senha-forte"})
    outra = {"Authorization": f"Bearer {r.json()['token']}"}
    assert r.status_code == 200 and c.get("/auth/me", headers=outra).status_code == 200

    assert c.post("/auth/logout", headers=outra).status_code == 204
    assert c.get("/auth/me", headers=outra).status_code == 401      # sessão encerrada
    assert c.get("/auth/me", headers=ana).status_code == 200        # a outra sessão continua
    assert c.get("/auth/me", headers=AUTH).status_code == 401       # chave de serviço não é usuário


def test_banco_nao_guarda_senha_nem_token(with_login):
    ana, _ = signup(with_login)
    token = ana["Authorization"].split()[1]
    stored = str(vars(next(iter(with_login.store.users.values())))) + str(with_login.store.sessions)
    assert "senha-forte" not in stored and token not in stored


def test_sessao_vencida_e_recusada(with_login):
    from datetime import datetime, timedelta
    ana, _ = signup(with_login)
    for s in with_login.store.sessions.values():
        s["expires_at"] = datetime.now() - timedelta(days=1)
    assert with_login.client.get("/meetings/", headers=ana).status_code == 401


def test_cada_usuario_ve_so_as_proprias_reunioes(with_login):
    c = with_login.client
    with_login.repo.save(Meeting(id="antiga", title="Antes do login", transcript_text="oi"))
    ana, ana_user = signup(with_login)
    bia, _ = signup(with_login, email="bia@empresa.com", name="Bia")

    # a primeira conta herda o que existia; a segunda começa vazia
    assert [m["meeting_id"] for m in c.get("/meetings/", headers=ana).json()] == ["antiga"]
    assert c.get("/meetings/", headers=bia).json() == []

    r = c.post("/meetings/bot", headers=bia, json={"meeting_url": "https://meet.google.com/abc-defg-hij", "title": "Da Bia"})
    assert r.status_code == 200 and with_login.repo.find_by_id("id-bot").platform == "meet"
    assert [m["title"] for m in c.get("/meetings/", headers=bia).json()] == ["Da Bia"]

    # reunião alheia responde 404 em toda rota, sem revelar que existe
    for method, path, body in [
        ("get", "/meetings/id-bot", None), ("get", "/meetings/id-bot/status", None),
        ("get", "/meetings/id-bot/export.md", None), ("delete", "/meetings/id-bot", None),
        ("post", "/meetings/id-bot/process", None), ("post", "/meetings/id-bot/chat", {"question": "oi"}),
        ("patch", "/meetings/id-bot/tasks/0", {"done": True}),
        ("post", "/meetings/id-bot/tasks/email", {"to": "a@b.com"}),
    ]:
        assert c.request(method, path, headers=ana, json=body).status_code == 404, path
    assert with_login.repo.find_by_id("id-bot") is not None

    assert len(c.get("/meetings/", headers=AUTH).json()) == 2       # chave de serviço enxerga tudo
    assert c.get("/meetings/").status_code == 401


def test_detalhe_e_exclusao(with_login):
    c = with_login.client
    ana, user = signup(with_login)
    audio = with_login.tmp / "audio" / "m1.webm"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"x")
    with_login.repo.save(Meeting(
        id="m1", title="T", owner_id=user["id"], audio_path=str(audio), transcript_formatted="[00:00] Ana: oi",
        chat_history=[{"role": "user", "content": "quem falou?"}, {"role": "assistant", "content": "Ana"}],
    ))
    d = c.get("/meetings/m1", headers=ana).json()
    assert d["transcript"] == "[00:00] Ana: oi" and d["has_audio"] and d["chat_history"][1]["content"] == "Ana"
    assert c.delete("/meetings/m1", headers=ana).status_code == 204
    assert with_login.repo.find_by_id("m1") is None and not audio.exists()


def test_links_de_reuniao_aceitos():
    from infrastructure.recorder.platforms import UnsupportedMeetingUrl, detect_platform
    assert detect_platform("https://meet.google.com/abc-defg-hij").key == "meet"
    assert detect_platform("https://teams.microsoft.com/l/meetup-join/19%3ameeting").key == "teams"
    zoom = detect_platform("https://us02web.zoom.us/j/12345678901?pwd=abc")
    assert zoom.join_url("https://us02web.zoom.us/j/12345678901?pwd=abc") == "https://app.zoom.us/wc/12345678901/join?pwd=abc"
    for bad in ("http://meet.google.com/abc", "https://evil.com/meet.google.com", "https://notzoom.us/j/12345678901",
                "https://zoom.us/my/sala", "https://discord.com/channels/1/2", "", "javascript:alert(1)"):
        with pytest.raises(UnsupportedMeetingUrl):
            detect_platform(bad)


def test_config_publica_lista_plataformas(ctx):
    r = ctx.client.get("/config")
    assert r.status_code == 200 and [p["key"] for p in r.json()["platforms"]] == ["meet", "teams", "zoom"]
    assert ctx.client.post("/meetings/bot", headers=AUTH, json={"meeting_url": "https://discord.com/channels/1/2"}).status_code == 422


def test_openapi_em_dia():
    """docs/api/openapi.json é o contrato do front: regenere com `python -m api.export_openapi`."""
    import json
    from api.export_openapi import PATH, render

    def shape(spec):  # rotas e modelos; o texto exato varia com a versão do FastAPI
        return ({(p, m) for p, ops in spec["paths"].items() for m in ops}, set(spec["components"]["schemas"]))

    assert shape(json.loads(PATH.read_text(encoding="utf-8"))) == shape(json.loads(render()))
