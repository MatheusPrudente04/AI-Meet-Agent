"""
playwright_bot_recorder.py — Bot Playwright para gravar reuniões do Google Meet.

Usa a extensão Chrome (tabCapture MV3) para capturar o áudio da aba diretamente,
sem interceptar WebRTC. A extensão envia chunks via POST para /capture/chunk e
finaliza com POST /capture/finalize; o áudio fica em data/audio/<session>.webm.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

from playwright.async_api import BrowserContext, Page, Worker, async_playwright

logger = logging.getLogger(__name__)

# Exportado em nível de módulo para facilitar uso em testes
MEET_STATUS_JS = """
(() => {
    const url = location.href;
    const left = url.includes('/d/') || url.includes('lookup') ||
                 document.title.toLowerCase().includes('você saiu') ||
                 document.title.toLowerCase().includes('left the call') ||
                 !!document.querySelector('[data-call-ended]');

    const tiles = document.querySelectorAll('[data-participant-id]');
    const participantCount = tiles.length;

    const leaveBtn = Array.from(document.querySelectorAll('button[aria-label]')).find(b => {
        const lbl = (b.getAttribute('aria-label') || '').toLowerCase();
        return lbl.includes('sair da chamada') || lbl.includes('leave call') ||
               lbl.includes('leave meeting');
    });
    const leaveVisible = !!(leaveBtn && leaveBtn.offsetParent !== null);

    return { left, participantCount, leaveVisible };
})()
"""


# ID fixo da extensão, derivado do campo "key" do manifest.json.
EXTENSION_ID = "cjehgonhdknnomnbbdcmjkhomfnghcfm"


class BotNotLoggedInError(Exception):
    pass


@dataclass
class BotSession:
    id: str
    meeting_url: str
    audio_path: Optional[Path] = None
    caption_path: Optional[Path] = None
    speaker_log_path: Optional[Path] = None


class PlaywrightBotRecorder:
    ALONE_TIMEOUT = 120.0
    SILENCE_TIMEOUT = 300.0
    END_POLL_INTERVAL = 5.0
    MEET_STATUS_JS = MEET_STATUS_JS  # referência ao módulo para conveniência

    def __init__(
        self,
        chrome_profile_dir: str,
        audio_dir: str,
        bot_name: str = "Meet Agent",
        headless: bool = False,
        on_audio_ready: Optional[Callable[..., Awaitable[None]]] = None,
        capture_port: int = 8000,
    ) -> None:
        self._profile_dir = str(chrome_profile_dir)
        self._audio_dir = Path(audio_dir)
        self._audio_dir.mkdir(parents=True, exist_ok=True)
        self._bot_name = bot_name
        self._headless = headless
        self._on_audio_ready = on_audio_ready
        self._capture_endpoint = f"http://127.0.0.1:{capture_port}"

        self._ext_sw: Optional[Worker] = None
        self._context: Optional[BrowserContext] = None
        self._shutdown_event = asyncio.Event()
        self.session: Optional[BotSession] = None

    def request_stop(self) -> None:
        """Solicita encerramento gracioso (chamado por SIGINT/SIGTERM)."""
        logger.info("Encerramento gracioso solicitado")
        self._shutdown_event.set()

    # ------------------------------------------------------------------
    # Ponto de entrada principal
    # ------------------------------------------------------------------

    async def join_async(self, meeting_url: str) -> BotSession:
        session_id = str(uuid.uuid4())
        self.session = BotSession(id=session_id, meeting_url=meeting_url)

        ext_dir = Path(__file__).parent / "extension"

        # O perfil guarda em cache o service worker da extensão e continua
        # executando a versão antiga mesmo depois de o arquivo mudar em disco.
        shutil.rmtree(Path(self._profile_dir) / "Default" / "Service Worker", ignore_errors=True)

        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                self._profile_dir,
                headless=self._headless,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    f"--load-extension={ext_dir}",
                    f"--disable-extensions-except={ext_dir}",
                    # Sem isto o tabCapture exige clique no ícone da extensão (activeTab).
                    f"--allowlisted-extension-id={EXTENSION_ID}",
                    "--autoplay-policy=no-user-gesture-required",
                    # NÃO usar --use-fake-ui-for-media-stream: ele faz o getUserMedia
                    # da captura de aba falhar com "Requested device not found".
                    "--disable-features=VizDisplayCompositor",
                ],
                permissions=["microphone", "camera"],
                ignore_default_args=["--mute-audio"],
            )
            self._context = context

            page = await context.new_page()
            await page.goto(meeting_url, wait_until="domcontentloaded", timeout=30_000)

            await self._verify_login(page)
            await self._dismiss_device_dialogs(page)
            await self._mute_devices(page)
            await self._click_join_button(page)
            await self._wait_until_admitted(page)
            await self._mute_devices(page)

            logger.info("Admitido — ativando legendas")
            await self._enable_captions(page)

            logger.info("Localizando service worker da extensão")
            self._ext_sw = await self._find_ext_sw(context)
            await self._start_extension_capture(session_id, page)

            await self._wait_until_meeting_ends(page)

            await self._stop_extension_capture(page)
            await asyncio.sleep(2)  # dá tempo ao offscreen de fazer POST /finalize

            await context.close()

        audio_path = self._audio_dir / f"{session_id}.webm"
        if audio_path.exists() and audio_path.stat().st_size > 0:
            self.session.audio_path = audio_path
            logger.info(
                "Áudio salvo: %s (%.1f KB)",
                audio_path,
                audio_path.stat().st_size / 1024,
            )
        else:
            logger.warning(
                "Arquivo de áudio não encontrado ou vazio após reunião: %s", audio_path
            )

        if self._on_audio_ready and self.session.audio_path:
            await self._on_audio_ready(
                self.session.audio_path,
                self.session.speaker_log_path,
                self.session.caption_path,
            )

        return self.session

    # ------------------------------------------------------------------
    # Setup e lifecycle
    # ------------------------------------------------------------------

    async def _verify_login(self, page: Page) -> None:
        """Verifica se o Chrome está logado no Google. Lança BotNotLoggedInError caso não."""
        try:
            await page.wait_for_selector(
                '[data-is-muted], [aria-label*="microfone"], [aria-label*="microphone"], '
                'button[data-mute-button], [data-call-ended]',
                timeout=10_000,
            )
            return
        except Exception:
            pass
        # Se chegou em tela de login, o perfil não está autenticado
        if "accounts.google.com" in page.url or "signin" in page.url.lower():
            raise BotNotLoggedInError(
                f"Perfil Chrome não autenticado. Configure em: {self._profile_dir}"
            )

    async def _dismiss_device_dialogs(self, page: Page) -> None:
        """Descarta diálogos de câmera/microfone que possam bloquear a entrada."""
        dismiss_labels = ["Fechar", "Dispensar", "Ignorar", "Dismiss", "Não agora", "Not now"]
        for label in dismiss_labels:
            try:
                btn = page.locator(f'button:has-text("{label}")')
                if await btn.count() > 0:
                    await btn.first.click(timeout=2_000)
                    await asyncio.sleep(0.3)
            except Exception:
                pass

    async def _mute_devices(self, page: Page) -> None:
        """Desliga microfone e câmera: o bot só escuta.

        Os rótulos "Desativar ..." só existem com o dispositivo ligado, então
        chamar de novo não religa nada.
        """
        for label in (
            "Desativar microfone", "Turn off microphone",
            "Desativar câmera", "Turn off camera",
        ):
            try:
                btn = page.locator(f'[aria-label*="{label}"]')
                if await btn.count() > 0:
                    await btn.first.click(timeout=2_000)
                    logger.info("Dispositivo silenciado: %s", label)
            except Exception as exc:
                logger.warning("Falha ao silenciar (%s): %s", label, exc)

    async def _click_join_button(self, page: Page) -> None:
        """Clica no botão de entrar na reunião."""
        join_texts = [
            "Participar agora",
            "Join now",
            "Entrar agora",
            "Pedir para participar",
            "Ask to join",
            "Entrar",
            "Join",
        ]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self._shutdown_event.is_set():
                return
            for text in join_texts:
                try:
                    btn = page.locator(f'button:has-text("{text}")')
                    if await btn.count() > 0:
                        await btn.first.click(timeout=3_000)
                        logger.info("Botão de entrada clicado: '%s'", text)
                        return
                except Exception:
                    pass
            await asyncio.sleep(1)
        logger.warning("Botão de entrada não encontrado após 30s — continuando")

    async def _wait_until_admitted(self, page: Page, timeout: float = 120.0) -> None:
        """Aguarda até o usuário ser admitido (lobby desaparece)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._shutdown_event.is_set():
                return
            try:
                status = await page.evaluate(self.MEET_STATUS_JS)
                if status.get("leaveVisible"):
                    logger.info("Admitido na reunião (botão Sair visível)")
                    return
            except Exception:
                pass
            await asyncio.sleep(2)
        logger.warning("Timeout aguardando admissão (%.0fs) — continuando", timeout)

    # ------------------------------------------------------------------
    # Legendas
    # ------------------------------------------------------------------

    async def _enable_captions(self, page: Page) -> None:
        """Ativa legendas ao vivo e garante o idioma Português."""
        cc_selectors = [
            '[aria-label*="Ativar legendas"]',
            '[aria-label*="Turn on captions"]',
            '[aria-label*="Ligar legendas"]',
            '[aria-label*="legendas ao vivo"]',
            'button[jsname="r8qRAd"]',
        ]
        for sel in cc_selectors:
            try:
                btn = page.locator(sel)
                if await btn.count() > 0:
                    await btn.first.click(timeout=3_000)
                    logger.info("Legendas ativadas via: %s", sel)
                    await asyncio.sleep(1.5)
                    break
            except Exception:
                pass

        await self._ensure_captions_language(page)

    async def _ensure_captions_language(self, page: Page) -> None:
        """Configura o idioma das legendas para Português (todas as rotas possíveis)."""
        # Rota 1: menu de configurações de legenda
        settings_selectors = [
            '[aria-label*="Configurações de legenda"]',
            '[aria-label*="Caption settings"]',
            '[aria-label*="Configurações de legendas"]',
        ]
        for sel in settings_selectors:
            try:
                btn = page.locator(sel)
                if await btn.count() > 0:
                    await btn.first.click(timeout=2_000)
                    await asyncio.sleep(0.5)
                    break
            except Exception:
                pass

        # Rota 2: seletor de idioma direto
        lang_selectors = [
            'li:has-text("Português (Brasil)")',
            'li:has-text("Português")',
            '[aria-label*="Português"]',
            'option[value="pt-BR"]',
            'option[value="pt"]',
        ]
        for sel in lang_selectors:
            try:
                el = page.locator(sel)
                if await el.count() > 0:
                    await el.first.click(timeout=2_000)
                    logger.info("Idioma de legendas: Português (%s)", sel)
                    return
            except Exception:
                pass

        logger.debug("Idioma de legendas não configurado (seletores não encontrados)")

    # ------------------------------------------------------------------
    # Extensão tabCapture
    # ------------------------------------------------------------------

    async def _find_ext_sw(self, context: BrowserContext) -> Worker:
        """Localiza e retorna o service worker da extensão (aguarda até 15s)."""
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            for sw in context.service_workers:
                if "background.js" in sw.url or "chrome-extension://" in sw.url:
                    logger.info("Service worker encontrado: %s", sw.url)
                    return sw
            await asyncio.sleep(0.5)
        raise RuntimeError(
            "Service worker da extensão não encontrado. "
            f"Certifique-se de que a extensão está em: {Path(__file__).parent / 'extension'}"
        )

    async def _start_extension_capture(self, session_id: str, page: Page) -> None:
        """Inicia a captura de áudio via tabCapture.

        Anti "message port closed": despacha o comando pela ABA (satisfaz a
        regra de activeTab do Chrome) e confirma o sucesso lendo o STATUS no
        storage via __meetAgentStatus — NÃO depende da resposta da porta de
        mensagem (frágil em MV3 com Playwright).
        """
        # 1) Despacha o START pela aba (via content.js → SW). A resposta aqui é
        #    só "dispatched"; a confirmação real vem do status no passo 2.
        await page.evaluate(
            """async ([session, endpoint, chunkMs]) => {
                return await new Promise((resolve) => {
                    function onResp(ev) {
                        if (ev.source !== window) return;
                        if (!ev.data || ev.data.type !== 'MEET_AGENT_RESPONSE') return;
                        if (ev.data.action !== 'START') return;
                        window.removeEventListener('message', onResp);
                        resolve(ev.data.response);
                    }
                    window.addEventListener('message', onResp);
                    window.postMessage(
                        { type: 'MEET_AGENT_CAPTURE', action: 'START', session, endpoint, chunkMs },
                        '*'
                    );
                    setTimeout(() => {
                        window.removeEventListener('message', onResp);
                        resolve({ ok: false, error: 'dispatch timeout' });
                    }, 5000);
                });
            }""",
            [session_id, self._capture_endpoint, 2000],
        )

        # 2) CONFIRMA via status (storage) — canal que não exige contexto de aba.
        #    Faz polling curto: o getMediaStreamId + offscreen levam ~1-3s.
        if not self._ext_sw:
            raise RuntimeError("Service worker da extensão não encontrado")

        deadline = time.monotonic() + 15
        last_status: dict = {}
        while time.monotonic() < deadline:
            await asyncio.sleep(1)
            try:
                last_status = await self._ext_sw.evaluate("__meetAgentStatus()") or {}
            except Exception as exc:
                logger.debug("Erro ao ler status da extensão: %s", exc)
                continue

            phase = last_status.get("phase")
            if last_status.get("recording") or phase == "recording":
                logger.info("Captura confirmada via status: %s", last_status)
                return
            if phase == "error" or last_status.get("error"):
                raise RuntimeError(
                    f"Falha ao iniciar captura de áudio: {last_status.get('error')}"
                )
            logger.debug("Aguardando captura iniciar... status=%s", last_status)

        raise RuntimeError(
            f"Timeout confirmando início da captura. Último status: {last_status}"
        )

    async def _get_extension_status(self) -> dict:
        """Retorna o status atual da captura (chunks gravados, recording, etc.)."""
        if not self._ext_sw:
            return {"recording": False, "chunks": 0}
        try:
            return await self._ext_sw.evaluate("__meetAgentStatus()") or {}
        except Exception:
            return {"recording": False, "chunks": 0}

    async def _stop_extension_capture(self, page: Page) -> None:
        """Para a gravação via postMessage → content.js → SW."""
        try:
            await page.evaluate("""
                window.postMessage({ type: 'MEET_AGENT_CAPTURE', action: 'STOP' }, '*');
            """)
            await asyncio.sleep(3)
            logger.info("Captura parada")
        except Exception as exc:
            logger.warning("Erro ao parar captura via page: %s — tentando SW", exc)
            if self._ext_sw:
                try:
                    await self._ext_sw.evaluate("__meetAgentStop()")
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Monitoramento do fim da reunião
    # ------------------------------------------------------------------

    async def _leave_btn_visible(self, page: Page) -> bool:
        try:
            status = await page.evaluate(self.MEET_STATUS_JS)
            return bool(status.get("leaveVisible", False))
        except Exception:
            return False

    async def _wait_until_meeting_ends(self, page: Page) -> None:
        """Loop de monitoramento. Sai quando a reunião termina por qualquer sinal."""
        logger.info("Monitorando reunião (poll=%.0fs)", self.END_POLL_INTERVAL)

        alone_since: Optional[float] = None
        unknown_since: Optional[float] = None
        last_chunk_count = 0
        last_chunk_time = time.monotonic()
        ever_had_participants = False
        leave_btn_was_visible = await self._leave_btn_visible(page)

        while not self._shutdown_event.is_set():
            await asyncio.sleep(self.END_POLL_INTERVAL)

            if self._shutdown_event.is_set():
                logger.info("Shutdown solicitado — saindo do loop de monitoramento")
                break

            try:
                status = await page.evaluate(self.MEET_STATUS_JS)
            except Exception as exc:
                logger.debug("Erro ao avaliar status: %s", exc)
                continue

            now = time.monotonic()

            # Sinal 1: URL/DOM indica que saiu
            if status.get("left"):
                logger.info("Sinal DOM: saiu da chamada")
                break

            # Sinal 2: botão "Sair" desapareceu (host encerrou a reunião)
            leave_now = bool(status.get("leaveVisible"))
            if leave_btn_was_visible and not leave_now:
                logger.info("Botão 'Sair da chamada' desapareceu — reunião encerrada")
                break
            leave_btn_was_visible = leave_now

            # Sinal 3: sozinho por muito tempo
            count = status.get("participantCount", -1)

            if count == -1:
                if unknown_since is None:
                    unknown_since = now
                elif ever_had_participants and (now - unknown_since) > self.ALONE_TIMEOUT * 5:
                    logger.info(
                        "Contagem de participantes inacessível por %.0fs — encerrando",
                        now - unknown_since,
                    )
                    break
            else:
                unknown_since = None
                if count > 1:
                    ever_had_participants = True
                    alone_since = None
                elif count <= 1 and ever_had_participants:
                    if alone_since is None:
                        alone_since = now
                        logger.info(
                            "Sozinho na reunião — aguardando %.0fs antes de sair",
                            self.ALONE_TIMEOUT,
                        )
                    elif (now - alone_since) > self.ALONE_TIMEOUT:
                        logger.info(
                            "ALONE_TIMEOUT atingido (%.0fs) — encerrando",
                            now - alone_since,
                        )
                        break

            # Sinal 4: nenhum chunk novo por SILENCE_TIMEOUT
            try:
                ext_status = await self._get_extension_status()
                chunks = ext_status.get("chunks", 0)
                if chunks > last_chunk_count:
                    last_chunk_count = chunks
                    last_chunk_time = now
                elif (now - last_chunk_time) > self.SILENCE_TIMEOUT:
                    logger.info(
                        "SILENCE_TIMEOUT: nenhum chunk em %.0fs — encerrando",
                        now - last_chunk_time,
                    )
                    break
            except Exception:
                pass

        logger.info("Loop de monitoramento encerrado")