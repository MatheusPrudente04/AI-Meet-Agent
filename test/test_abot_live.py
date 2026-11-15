"""
test_abot_live.py — Testes do bot gravador (tabCapture).

  CAMADA 1 — Unitários (rodam SEMPRE, sem navegador):
    FakePage simula o Playwright e valida toda a lógica do recorder —
    verificação de login, os 4 sinais de fim de reunião e entrega via callback.

  CAMADA 2 — Teste VIVO (opcional, gated por MEET_TEST_URL):
    Entra numa sala real do Meet e valida o ciclo completo.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any, Coroutine, TypeVar

_T = TypeVar("_T")
# O teste vivo só roda com MEET_TEST_URL exportada no shell. Não carregamos o
# .env aqui: um `pytest` comum não pode mandar o bot para uma reunião real.

import pytest

pytest.importorskip("playwright", reason="Playwright não instalado — pulando")

from infrastructure.recorder.playwright_bot_recorder import (
    BotNotLoggedInError,
    BotSession,
    MEET_STATUS_JS,
    PlaywrightBotRecorder,
)


def run(coro: Coroutine[Any, Any, _T]) -> _T:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Doubles de Playwright
# ---------------------------------------------------------------------------

class FakeLocator:
    def __init__(self, count: int = 0):
        self._count = count

    @property
    def first(self):
        return self

    async def count(self) -> int:
        return self._count

    async def click(self, **_):
        return None

    async def wait_for(self, **_):
        return None


class _TimeoutError(Exception):
    pass


class FakePage:
    """Simula playwright.async_api.Page para testes unitários sem navegador.

    eval_map: chave = trecho do script; valor = dict ou callable que retorna dict.
    """

    def __init__(
        self,
        url: str = "https://meet.google.com/abc-defg-hij",
        eval_map: dict | None = None,
        selector_timeout: bool = False,
    ):
        self.url = url
        self.eval_map = eval_map or {}
        self._selector_timeout = selector_timeout
        self.evaluated: list[str] = []

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(count=0)

    async def wait_for_selector(self, selector: str, **kwargs):
        if self._selector_timeout:
            from playwright.async_api import TimeoutError as PWTimeout
            raise PWTimeout("selector not found")
        return None

    closed = False

    def is_closed(self) -> bool:
        return self.closed

    async def evaluate(self, script: str):
        self.evaluated.append(script)
        for key, value in sorted(
            self.eval_map.items(), key=lambda x: len(x[0]), reverse=True
        ):
            if key in script:
                return value() if callable(value) else value
        # default — reunião ativa sem sinal de fim
        return {"left": False, "participantCount": 2, "leaveVisible": True}


@pytest.fixture
def recorder(tmp_path) -> PlaywrightBotRecorder:
    r = PlaywrightBotRecorder(
        chrome_profile_dir=str(tmp_path / "profile"),
        audio_dir=str(tmp_path / "audio"),
        headless=True,
    )
    r.session = BotSession(id="test-session-id", meeting_url="https://meet.google.com/abc-defg-hij")
    return r


# ===========================================================================
# CAMADA 1 — Unitários
# ===========================================================================

class TestVerificacaoDeLogin:
    def test_redirect_para_accounts_levanta_erro(self, recorder):
        page = FakePage(url="https://accounts.google.com/signin", selector_timeout=True)
        with pytest.raises(BotNotLoggedInError):
            run(recorder._verify_login(page))

    def test_url_signin_levanta_erro(self, recorder):
        page = FakePage(url="https://accounts.google.com/v3/signin/identifier", selector_timeout=True)
        with pytest.raises(BotNotLoggedInError):
            run(recorder._verify_login(page))

    def test_sessao_valida_passa_sem_erro(self, recorder):
        page = FakePage()  # wait_for_selector não levanta TimeoutError
        run(recorder._verify_login(page))


class TestFimDeReuniao:
    """Valida os 4 sinais de fim de reunião. END_POLL_INTERVAL reduzido para
    que o loop termine instantaneamente no ambiente de teste."""

    @pytest.fixture(autouse=True)
    def _rapido(self, monkeypatch):
        monkeypatch.setattr(PlaywrightBotRecorder, "END_POLL_INTERVAL", 0.01)

    @pytest.fixture(autouse=True)
    def _sem_extensao(self, monkeypatch):
        """Monkeypatcha _get_extension_status para retornar chunks estáveis."""
        async def _fake_status(self_):
            return {"recording": True, "chunks": 99}
        monkeypatch.setattr(PlaywrightBotRecorder, "_get_extension_status", _fake_status)

    def test_sinal_1_left_true(self, recorder):
        page = FakePage(eval_map={
            MEET_STATUS_JS: {"left": True, "participantCount": 0, "leaveVisible": False},
        })
        run(asyncio.wait_for(recorder._wait_until_meeting_ends(page), timeout=2))

    def test_sinal_2_leave_btn_sumiu(self, recorder):
        """Botão 'Sair' estava visível, sumiu → reunião encerrada pelo host."""
        calls = [0]

        def _status():
            calls[0] += 1
            # Primeira chamada: leaveVisible=True. Próximas: False.
            return {
                "left": False,
                "participantCount": 2,
                "leaveVisible": calls[0] == 1,
            }

        page = FakePage(eval_map={MEET_STATUS_JS: _status})
        run(asyncio.wait_for(recorder._wait_until_meeting_ends(page), timeout=2))

    def test_sinal_3_alone_timeout(self, recorder, monkeypatch):
        monkeypatch.setattr(PlaywrightBotRecorder, "ALONE_TIMEOUT", 0.005)
        # ever_had_participants precisa ser True antes de contar sozinho
        calls = [0]

        def _status():
            calls[0] += 1
            count = 2 if calls[0] <= 2 else 1  # 2 participantes inicialmente, depois 1
            return {"left": False, "participantCount": count, "leaveVisible": True}

        page = FakePage(eval_map={MEET_STATUS_JS: _status})
        run(asyncio.wait_for(recorder._wait_until_meeting_ends(page), timeout=2))

    def test_sinal_4_silence_timeout(self, recorder, monkeypatch):
        monkeypatch.setattr(PlaywrightBotRecorder, "SILENCE_TIMEOUT", 0.01)

        async def _sem_chunks(self_):
            return {"recording": True, "chunks": 0}

        monkeypatch.setattr(PlaywrightBotRecorder, "_get_extension_status", _sem_chunks)
        page = FakePage()
        run(asyncio.wait_for(recorder._wait_until_meeting_ends(page), timeout=2))

    def test_reuniao_ativa_nao_encerra(self, recorder, monkeypatch):
        """2 participantes, com chunks crescendo → deve permanecer em loop."""
        chunk_counter = [0]

        async def _chunks_crescendo(self_):
            chunk_counter[0] += 1
            return {"recording": True, "chunks": chunk_counter[0]}

        monkeypatch.setattr(PlaywrightBotRecorder, "_get_extension_status", _chunks_crescendo)
        page = FakePage()  # default: left=False, participantCount=2, leaveVisible=True
        with pytest.raises(asyncio.TimeoutError):
            run(asyncio.wait_for(recorder._wait_until_meeting_ends(page), timeout=0.2))

    def test_shutdown_event_para_loop(self, recorder):
        page = FakePage()
        recorder._shutdown_event.set()  # pré-sinaliza antes mesmo de iniciar
        # Deve retornar imediatamente sem aguardar timeout
        run(asyncio.wait_for(recorder._wait_until_meeting_ends(page), timeout=1))


class TestShutdownGracioso:
    def test_request_stop_seta_evento(self, recorder):
        assert not recorder._shutdown_event.is_set()
        recorder.request_stop()
        assert recorder._shutdown_event.is_set()


# ===========================================================================
# CAMADA 2 — Teste vivo (opcional)
# ===========================================================================

MEET_TEST_URL = os.environ.get("MEET_TEST_URL")


@pytest.mark.skipif(not MEET_TEST_URL, reason="defina MEET_TEST_URL para o teste vivo")
class TestBotVivo:
    def test_ciclo_completo_em_sala_real(self, monkeypatch, tmp_path):
        """Entra na sala, grava e sai sozinho. Para validar ÁUDIO real,
        entre na mesma sala com uma segunda conta e fale algo."""
        monkeypatch.setattr(PlaywrightBotRecorder, "ALONE_TIMEOUT", 30)
        profile = Path(
            os.environ.get("BOT_PROFILE_DIR",
            os.environ.get("BOT_CHROME_PROFILE", "./bot_chrome_profile"))
        )
        assert profile.exists(), (
            f"Perfil '{profile}' não existe — rode: "
            "python -m infrastructure.recorder.bot_setup"
        )

        recorder = PlaywrightBotRecorder(
            chrome_profile_dir=str(profile),
            audio_dir=str(tmp_path),
            headless=os.environ.get("BOT_HEADLESS", "0") == "1",
        )
        assert MEET_TEST_URL is not None
        session = run(recorder.join_async(MEET_TEST_URL))

        assert session.audio_path is not None and session.audio_path.exists()
        assert session.audio_path.stat().st_size > 0, "webm vazio"
        print(
            f"\nÁudio: {session.audio_path} "
            f"({session.audio_path.stat().st_size / 1024:.1f} KB)"
        )


class TestAdmissao:
    def test_sala_de_espera_nao_conta_como_admitido(self, recorder):
        """A espera também mostra "Sair da chamada"; só `inCall` libera a gravação."""
        import asyncio
        calls = [0]

        def status():
            calls[0] += 1
            return {"left": False, "participantCount": 1, "leaveVisible": True, "inCall": calls[0] >= 3}

        page = FakePage()
        page.eval_map = {MEET_STATUS_JS: status}

        async def run():
            real_sleep = asyncio.sleep
            asyncio.sleep = lambda *_: real_sleep(0)
            try:
                await recorder._wait_until_admitted(page, timeout=5)
            finally:
                asyncio.sleep = real_sleep

        asyncio.run(run())
        assert calls[0] == 3

    def test_pagina_fechada_encerra_o_monitoramento(self, recorder):
        import asyncio
        recorder.END_POLL_INTERVAL = 0
        page = FakePage()
        page.closed = True
        asyncio.run(asyncio.wait_for(recorder._wait_until_meeting_ends(page), timeout=2))
