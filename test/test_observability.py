"""Observabilidade: logs centralizados, métricas e a API (só admin)."""
import logging
from collections import defaultdict

import pytest

import infrastructure.observability as obs


def _s(key):
    return key.decode() if isinstance(key, bytes) else key


class FakeRedis:
    """O pedaço do Redis que a observabilidade usa, em memória."""

    def __init__(self):
        self.h, self.lists, self.stream, self.n = defaultdict(float), defaultdict(list), [], 0

    def hincrby(self, k, f, by): self.h[f] += by
    def hincrbyfloat(self, k, f, by): self.h[f] += by
    def hgetall(self, k): return {f.encode(): str(v).encode() for f, v in self.h.items()}
    def lpush(self, k, v): self.lists[k].insert(0, str(v))
    def ltrim(self, k, a, b): self.lists[k] = self.lists[k][a:b + 1]
    def lrange(self, k, a, b): return [v.encode() for v in self.lists[_s(k)]]
    def llen(self, k): return len(self.lists.get(_s(k), []))
    def scan_iter(self, match, count): return [k.encode() for k in self.lists if k.startswith(_s(match).rstrip("*"))]

    def pipeline(self):
        r = self

        class P:
            def __getattr__(self, name):
                def call(*a):
                    getattr(r, name)(*a)
                    return self
                return call

            def execute(self): return []
        return P()

    def xadd(self, k, fields, maxlen, approximate):
        self.n += 1
        self.stream.append((f"{self.n}-0".encode(), {a.encode(): str(b).encode() for a, b in fields.items()}))

    def xrevrange(self, k, count): return list(reversed(self.stream))[:count]


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(obs, "_client", lambda: fake)
    return fake


def test_contadores_e_duracoes(redis):
    obs.incr("emails_total", {"kind": "chat", "result": "sent"})
    obs.incr("emails_total", {"kind": "chat", "result": "sent"})
    for s in (1.0, 2.0, 10.0):
        obs.observe("llm_seconds", s, {"kind": "tools"})
    c = obs.counters()
    assert c["emails_total|kind=chat,result=sent"] == 2
    assert c["llm_seconds|kind=tools#count"] == 3 and c["llm_seconds|kind=tools#sum"] == 13
    values = obs.samples()["llm_seconds|kind=tools"]
    assert obs.percentile(values, 50) == 2.0 and obs.percentile(values, 95) == 10.0


def test_timed_conta_erro(redis):
    with pytest.raises(ValueError):
        with obs.timed("llm_seconds", {"kind": "text"}):
            raise ValueError("falhou")
    assert obs.counters()["llm_errors_total|kind=text"] == 1


def test_logs_de_todos_os_servicos_com_filtros(redis):
    from config.logging_setup import _MeetingIdFilter, bind_meeting
    handler = obs.RedisLogHandler("worker-transcribe")
    handler.addFilter(_MeetingIdFilter())
    log = logging.getLogger("teste.obs")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False  # só o handler do teste; o da raiz também mandaria para o Redis falso
    try:
        bind_meeting("m1")
        log.info("transcrevendo")
        log.error("falhou o whisper")
        bind_meeting("m2")
        log.info("outra reunião")
    finally:
        log.removeHandler(handler)
        bind_meeting("-")
    assert [r["message"] for r in obs.logs(meeting_id="m1")] == ["falhou o whisper", "transcrevendo"]
    assert [r["message"] for r in obs.logs(level="ERROR")] == ["falhou o whisper"]
    assert obs.logs(service="api") == [] and obs.logs(text="OUTRA")[0]["meeting_id"] == "m2"


def test_redis_fora_nao_derruba_quem_mede(monkeypatch):
    class Down:
        def __getattr__(self, name):
            raise ConnectionError("redis fora")
    monkeypatch.setattr(obs, "_redis", lambda: Down())
    monkeypatch.setattr(obs, "_down_until", 0.0)
    obs.incr("x")
    obs.observe("y", 1.0)
    obs.RedisLogHandler("api").emit(logging.LogRecord("a", logging.INFO, "", 0, "oi", None, None))
    assert obs._client() is None  # desligada por 30 s depois da falha


def test_prometheus_e_api_so_para_admin(redis, monkeypatch):
    from fastapi.testclient import TestClient
    import api.main as main
    import api.routers.observability as router
    from api.security import Principal, current_principal
    from domain.entities.user import User

    class Repo:
        def count_by_status(self): return {"done": 3, "error": 1}

    class C:
        repository, auth, calendar = Repo(), None, None
        def bot_status(self): return {"status": "ready"}

    monkeypatch.setattr(router, "get_container", lambda: C())
    monkeypatch.setattr(main, "_workers", lambda: {"worker_transcribe": "ok", "worker_llm": "sem worker", "bot": "ok"})
    obs.incr("http_requests_total", {"method": "GET", "route": "/meetings/", "status": "200"})
    obs.observe("pipeline_stage_seconds", 42.0, {"stage": "transcribe"})
    redis.lists["transcribe:3"] = ["a", "b"]

    user = User(id="u", name="Bia", email="b@x.com", role="user")
    main.app.dependency_overrides[current_principal] = lambda: Principal(user)
    try:
        client = TestClient(main.app)
        assert client.get("/observability/summary").status_code == 403
        user.role = "admin"
        text = client.get("/observability/metrics").text
        summary = client.get("/observability/summary").json()
    finally:
        main.app.dependency_overrides.clear()
    assert 'meet_agent_http_requests_total{method="GET",route="/meetings/",status="200"} 1' in text
    assert 'meet_agent_pipeline_stage_seconds{quantile="0.95",stage="transcribe"} 42' in text
    assert 'meet_agent_queue_depth{queue="transcribe"} 2' in text and 'meet_agent_meetings{status="done"} 3' in text
    assert 'meet_agent_worker_up{queue="worker_llm"} 0' in text
    assert summary["queues"]["transcribe"] == 2 and summary["meetings_by_status"]["error"] == 1
    assert summary["latencies_seconds"]["pipeline_stage_seconds|stage=transcribe"]["p95"] == 42
