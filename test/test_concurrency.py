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
