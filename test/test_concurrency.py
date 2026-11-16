"""Filas por etapa, justiça entre usuários e limite de pedidos (ADR-015, Sprint 9)."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import api.security as security
from domain.entities.meeting import Meeting, MeetingStatus
from domain.entities.user import User
from infrastructure.json_meeting_repository import JsonMeetingRepository
from presentation.container import Container
from test_process_meeting import FakeLLM, FakeTranscriber, _uc
from use_cases.process_meeting import ProcessMeetingInput


@pytest.fixture
def repo(tmp_path):
    return JsonMeetingRepository(str(tmp_path / "meetings"))


class QueueContainer:
    """O despacho de verdade (enqueue, prioridade, posição) com as tasks do Celery interceptadas."""
    enqueue_processing = Container.enqueue_processing
    enqueue = Container.enqueue
    _priority = Container._priority
    queue_position = Container.queue_position
    _is_collab = True

    def __init__(self, repo):
        self.repository, self.sent = repo, []


@pytest.fixture
def box(repo, monkeypatch):
    import worker.tasks as tasks
    c = QueueContainer(repo)
    monkeypatch.setattr(tasks.process_meeting_task, "apply_async",
                        lambda args, priority: c.sent.append(("transcribe", args[0], priority)))
    monkeypatch.setattr(tasks.summarize_meeting_task, "apply_async",
                        lambda args, priority: c.sent.append(("llm", args[0], priority)))
    return c


def test_etapas_separadas_param_em_summarizing_e_depois_done(repo, tmp_path):
    audio = tmp_path / "a.webm"
    audio.write_bytes(b"x")
    uc = _uc(repo, FakeTranscriber(), FakeLLM())
    m = uc.transcribe(ProcessMeetingInput("m1", str(audio), "T"))
    assert m.status is MeetingStatus.SUMMARIZING and m.summary is None and m.transcript_text
    m = uc.summarize("m1")
    assert m.status is MeetingStatus.DONE and m.summary.overview == "resumo"
    assert {"transcribe_s", "transcript_chars", "summarize_s"} <= set(repo.find_by_id("m1").metrics)


def test_resumo_sem_transcricao_vira_erro(repo):
    repo.save(Meeting(id="m1", title="T"))
    assert _uc(repo).summarize("m1").status is MeetingStatus.ERROR


def test_dez_envios_de_um_nao_passam_na_frente_do_primeiro_de_outro(box, repo):
    for i in range(10):
        box.enqueue_processing(f"a{i}", "/x.webm", "A", owner_id="ana")
    box.enqueue_processing("b0", "/x.webm", "B", owner_id="bia")

    priorities = {mid: p for _, mid, p in box.sent}
    assert [priorities[f"a{i}"] for i in range(10)] == list(range(10))
    assert priorities["b0"] == 0
    # Ordem do Celery: (prioridade, chegada). A reunião da Bia é a 2ª, logo depois do 1º envio da Ana.
    assert box.queue_position(repo.find_by_id("b0")) == 2
    assert box.queue_position(repo.find_by_id("a0")) == 1
    assert box.queue_position(repo.find_by_id("a9")) == 11


def test_saiu_da_fila_quando_o_worker_pega(box, repo, tmp_path):
    audio = tmp_path / "a.webm"
    audio.write_bytes(b"x")
    box.enqueue_processing("m1", str(audio), "T", owner_id="ana")
    assert repo.find_by_id("m1").queue == "transcribe"
    m = _uc(repo).transcribe(ProcessMeetingInput("m1", str(audio), "T"))
    assert m.queue == "" and box.queue_position(m) == 0
    box.enqueue(m, "llm")
    assert box.sent[-1][0] == "llm" and box.queue_position(repo.find_by_id("m1")) == 1


def test_seguidor_mostra_a_posicao_da_gravacao_que_acompanha(box, repo):
    box.enqueue_processing("src", "/x.webm", "T", owner_id="ana")
    follower = Meeting(id="f", title="T", owner_id="bia", source_id="src")
    assert box.queue_position(follower) == 1


def test_trabalho_em_andamento_conta_para_a_prioridade(box, repo):
    repo.save(Meeting(id="old", title="T", owner_id="ana", status=MeetingStatus.TRANSCRIBING))
    repo.save(Meeting(id="done", title="T", owner_id="ana", status=MeetingStatus.DONE))
    box.enqueue_processing("new", "/x.webm", "T", owner_id="ana")
    assert box.sent[-1] == ("transcribe", "new", 1)


class FakeRedis:
    def __init__(self):
        self.counts = {}

    def pipeline(self):
        redis, ops = self, []

        class Pipe:
            def incr(self, key):
                ops.append(key)
                return self

            def expire(self, key, seconds):
                return self

            def execute(self):
                key = ops[0]
                redis.counts[key] = redis.counts.get(key, 0) + 1
                return [redis.counts[key], True]
        return Pipe()


def _limits(monkeypatch, limit, redis):
    monkeypatch.setattr(security, "get_settings",
                        lambda: SimpleNamespace(rate_limit_per_minute=limit, is_collab=True))
    monkeypatch.setattr(security, "_redis", lambda: redis)


def _user(uid):
    return security.Principal(User(id=uid, name="x", email=f"{uid}@x.com", password_hash=""))


def test_limite_por_minuto_por_usuario(monkeypatch):
    _limits(monkeypatch, 3, FakeRedis())
    for _ in range(3):
        security.rate_limited(_user("ana"))
    with pytest.raises(HTTPException) as exc:
        security.rate_limited(_user("ana"))
    assert exc.value.status_code == 429 and "Retry-After" in exc.value.headers
    security.rate_limited(_user("bia"))  # o limite de um não atinge o outro
    security.rate_limited(security.Principal())  # chave de serviço: sem limite


def test_redis_fora_nao_derruba_a_api(monkeypatch):
    class Down:
        def pipeline(self):
            raise ConnectionError("redis fora")
    _limits(monkeypatch, 1, Down())
    for _ in range(3):
        security.rate_limited(_user("ana"))


class BotContainer:
    """dispatch_bot de verdade, com o Celery interceptado e a trava trocável."""
    dispatch_bot = Container.dispatch_bot
    bot_status = Container.bot_status
    bot_account = None
    _follow = Container._follow
    _title_from_calendar = Container._title_from_calendar
    calendar = None
    _is_collab = True

    def __init__(self, repo, lock):
        self.repository, self._lock = repo, lock

    def _link_lock(self, key):
        return self._lock


def _race(repo, monkeypatch, lock):
    import contextlib
    import threading
    import time
    import worker.tasks as tasks
    bots = []
    monkeypatch.setattr(tasks.join_meeting_task, "delay", lambda *a: bots.append(a))
    slow_find = repo.find_by

    def find_by(**f):  # alarga a janela entre "conferir" e "gravar"
        found = slow_find(**f)
        time.sleep(0.05)
        return found
    monkeypatch.setattr(repo, "find_by", find_by)
    c = BotContainer(repo, lock or contextlib.nullcontext())
    ts = [threading.Thread(target=c.dispatch_bot, args=("https://meet.google.com/abc-defg-hij", "T", who))
          for who in ("ana", "bia")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    return bots, repo.list_all()


def test_sem_trava_dois_envios_simultaneos_criam_dois_bots(repo, monkeypatch):
    bots, _ = _race(repo, monkeypatch, lock=None)
    assert len(bots) == 2  # o defeito que a trava corrige


def test_com_trava_dois_envios_simultaneos_viram_um_bot_e_um_seguidor(repo, monkeypatch):
    import threading
    bots, meetings = _race(repo, monkeypatch, lock=threading.Lock())
    assert len(bots) == 1
    assert sorted(bool(m.source_id) for m in meetings) == [False, True]


def test_health_aponta_etapa_sem_worker(monkeypatch):
    import api.main as main
    import infrastructure.observability as obs
    # Worker ocupado (no meio de uma transcrição) continua vivo: o batimento é de outra thread.
    busy = {"transcribe": [{"host": "w1", "task": {"name": "process_meeting", "meeting_id": "m1"}}]}
    monkeypatch.setattr(obs, "workers", lambda: busy)
    assert main._workers() == {"worker_transcribe": "ok", "worker_llm": "sem worker", "bot": "sem worker"}
    monkeypatch.setattr(obs, "workers", lambda: {})
    assert set(main._workers().values()) == {"sem worker"}
    monkeypatch.setattr(obs, "workers", lambda: None)  # Redis fora
    assert main._workers() == {"workers": "erro: sem leitura do Redis"}


def test_batimento_expira_e_traz_a_tarefa_atual(monkeypatch):
    import infrastructure.observability as obs

    class R:
        def __init__(self): self.kv = {}
        def pipeline(self):
            r = self
            class P:
                def set(self, k, v, ex): r.kv[k] = (v, ex); return self
                def execute(self): return []
            return P()
        def scan_iter(self, match, count): return [k.encode() for k in self.kv if k.startswith(match.rstrip("*"))]
        def get(self, k): return self.kv[k.decode()][0].encode()

    r = R()
    monkeypatch.setattr(obs, "_client", lambda: r)
    obs.heartbeat(["transcribe"], "w1", {"name": "process_meeting", "meeting_id": "m1"})
    assert all(ex == obs.HEARTBEAT_TTL for _, ex in r.kv.values())  # some sozinho se o worker morrer
    assert obs.workers()["transcribe"][0]["task"]["meeting_id"] == "m1"


def test_alerta_so_na_mudanca_de_estado(tmp_path, monkeypatch):
    import scripts.health_alert as alert
    sent, results = [], iter([(False, "HTTP 503: {}"), (False, "HTTP 503: {}"), (True, "{}"), (True, "{}")])
    monkeypatch.setattr(alert, "check", lambda url: next(results))
    state = tmp_path / "s"
    send = lambda subject, body: sent.append(subject)
    assert [alert.run("u", state, send) for _ in range(4)] == [False, False, True, True]
    assert sent == ["API fora do ar", "API de volta"]


# ── Progresso e trava (etapa sem worker) ─────────────────────────────

def test_status_at_acompanha_cada_troca_de_status():
    from datetime import datetime, timedelta
    m = Meeting(id="m", title="t")
    first = m.status_at
    m.status = MeetingStatus.TRANSCRIBING
    assert m.status_at and m.status_at >= first
    old = datetime.now() - timedelta(minutes=5)
    m.status_at = old
    m.status = MeetingStatus.TRANSCRIBING   # mesmo status: não reinicia o relógio
    assert m.status_at == old


def _progress_box(repo, monkeypatch, alive):
    import infrastructure.observability as obs
    monkeypatch.setattr(obs, "workers", lambda: alive)
    monkeypatch.setattr(obs, "samples", lambda: {"pipeline_stage_seconds|stage=transcribe": [100.0, 120.0, 140.0]})

    class Box:
        progress = Container.progress
        resume = Container.resume
        enqueue_processing = Container.enqueue_processing
        enqueue = Container.enqueue
        _priority = Container._priority
        _is_collab = True
        def __init__(self): self.repository, self.sent = repo, []
    return Box()


def test_trava_so_quando_ninguem_processa_nem_espera(repo, monkeypatch):
    from datetime import datetime, timedelta
    m = Meeting(id="m", title="t", status=MeetingStatus.TRANSCRIBING, audio_path="")
    m.status_at = datetime.now() - timedelta(minutes=10)
    repo.save(m)
    working = {"transcribe": [{"host": "w", "task": {"name": "process_meeting", "meeting_id": "m"}}]}
    assert _progress_box(repo, monkeypatch, working).progress(m)["state"] == "working"
    p = _progress_box(repo, monkeypatch, {"transcribe": [{"host": "w", "task": None}]}).progress(m)
    assert p["state"] == "stalled" and p["elapsed_s"] > 500 and p["typical_s"] == 120.0
    m.queue = "transcribe"
    assert _progress_box(repo, monkeypatch, {}).progress(m)["state"] == "queued"
    m.queue, m.status_at = "", datetime.now()
    assert _progress_box(repo, monkeypatch, {}).progress(m)["state"] == "working"  # acabou de começar


def test_retomar_recomeca_a_etapa_em_que_parou(repo, monkeypatch, tmp_path):
    import worker.tasks as tasks
    box = _progress_box(repo, monkeypatch, {})
    sent = []
    monkeypatch.setattr(tasks.process_meeting_task, "apply_async", lambda args, priority: sent.append(("transcribe", args[0])))
    monkeypatch.setattr(tasks.summarize_meeting_task, "apply_async", lambda args, priority: sent.append(("llm", args[0])))
    audio = tmp_path / "a.webm"
    audio.write_bytes(b"x")
    repo.save(Meeting(id="t", title="t", status=MeetingStatus.TRANSCRIBING, audio_path=str(audio)))
    repo.save(Meeting(id="s", title="t", status=MeetingStatus.SUMMARIZING, transcript_text="oi"))
    repo.save(Meeting(id="r", title="t", status=MeetingStatus.RECORDING, audio_path=str(tmp_path / "nada.webm")))
    for mid in ("t", "s", "r"):
        box.resume(repo.find_by_id(mid))
    assert sent == [("transcribe", "t"), ("llm", "s")]
    assert repo.find_by_id("r").status is MeetingStatus.ERROR  # bot morreu sem áudio: erro claro
