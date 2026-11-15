"""
playwright_bot_recorder.py — Bot Playwright que grava reuniões (Meet, Teams, Zoom).

O fluxo do Meet vive aqui; o das outras plataformas, em platforms.py (ADR-012).

Usa a extensão Chrome (tabCapture MV3) para capturar o áudio da aba diretamente,
sem interceptar WebRTC. A extensão envia chunks via POST para /capture/chunk e
finaliza com POST /capture/finalize; o áudio fica em data/audio/<session>.webm.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from playwright.async_api import BrowserContext, Page, Worker, async_playwright

from infrastructure.recorder.platforms import PLATFORMS, detect_platform

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

    // A sala de espera também mostra "Sair da chamada" e o bloco do próprio bot.
    // Dentro da chamada existe o botão de legendas (ou mais de um participante).
    const captionsBtn = Array.from(document.querySelectorAll('button[aria-label]')).some(b =>
        /legenda|caption/i.test(b.getAttribute('aria-label') || ''));
    const inCall = leaveVisible && (captionsBtn || participantCount > 1);

    return { left, participantCount, leaveVisible, inCall };
})()
"""


# ID fixo da extensão, derivado do campo "key" do manifest.json.
EXTENSION_ID = "cjehgonhdknnomnbbdcmjkhomfnghcfm"


# Diagnóstico (BOT_DEBUG_DOM): amostra o que o Meet mostra sobre participantes e
# legendas, para escolher seletores a partir da tela real em vez de supor.
DOM_SAMPLE_JS = """
(() => {
    const clip = (s, n) => (s || '').replace(/\\s+/g, ' ').trim().slice(0, n);
    const tiles = Array.from(document.querySelectorAll('[data-participant-id]')).map(el => ({
        id: el.getAttribute('data-participant-id'),
        text: clip(el.innerText, 160),
        aria: el.getAttribute('aria-label'),
    }));
    const live = Array.from(document.querySelectorAll('[aria-live], [role="region"], [role="log"]'))
        .filter(el => clip(el.innerText, 10).length > 0)
        .slice(0, 10)
        .map(el => ({
            role: el.getAttribute('role'),
            ariaLive: el.getAttribute('aria-live'),
            ariaLabel: el.getAttribute('aria-label'),
            cls: clip(el.className && el.className.toString(), 80),
            text: clip(el.innerText, 500),
            html: (el.outerHTML || '').slice(0, 3000),
        }));
    return { tiles, live };
})()
"""


# Quem está falando e quem está na sala (ADR-009 fase 2). Estrutura observada no
# Meet em 2026-11: a região de legendas tem um bloco por fala, com a foto e o
# nome num cabeçalho e o texto no último filho. Não dependemos de classes CSS.
CAPTIONS_REGION = '[role="region"][aria-label="Legendas"], [role="region"][aria-label="Captions"]'
SPEAKERS_JS = """
(() => {
    const region = document.querySelector('%s');
    const captions = [];
    if (region) {
        for (const img of region.querySelectorAll('img')) {
            const header = img.parentElement;
            const block = header && header.parentElement;
            if (!block || block.children.length < 2) continue;
            const name = (header.innerText || '').trim();
            const text = (block.children[block.children.length - 1].innerText || '').trim();
            if (name) captions.push({ name, text });
        }
    }
    // Nome do bloco de vídeo: a linha que se repete (rótulo + legenda do bloco);
    // senão, a primeira linha que não é nome de ícone (ex.: "frame_person").
    const participants = [];
    const selfNames = [];
    for (const tile of document.querySelectorAll('[data-participant-id]')) {
        const lines = (tile.innerText || '').split('\\n').map(l => l.trim()).filter(Boolean);
        const name = lines.find((l, i) => lines.indexOf(l) !== i) || lines.find(l => !/^[a-z0-9_]+$/.test(l));
        if (!name) continue;
        if (!participants.includes(name)) participants.push(name);
        // Só o próprio bloco oferece "Planos de fundo e efeitos" (ícone visual_effects).
        if ((tile.innerText || '').includes('visual_effects') ||
            tile.querySelector('button[aria-label*="Planos de fundo"], button[aria-label*="ackgrounds"]')) selfNames.push(name);
    }
    // Só vale se for exatamente um bloco: se o Meet mudar a tela, preferimos
    // listar o bot a esconder gente.
    const self = selfNames.length === 1 ? selfNames[0] : null;
    return { captionsOn: !!region, captions, participants, self };
})()
""" % CAPTIONS_REGION


class BotNotLoggedInError(Exception):
    pass


@dataclass
class BotSession:
    id: str
    meeting_url: str
    audio_path: Optional[Path] = None


class PlaywrightBotRecorder:
    ALONE_TIMEOUT = 120.0
    SILENCE_TIMEOUT = 300.0
    END_POLL_INTERVAL = 5.0
    CAPTIONS_RETRY_S = 10.0
    MEET_STATUS_JS = MEET_STATUS_JS  # referência ao módulo para conveniência

    def __init__(
        self,
        chrome_profile_dir: str,
        audio_dir: str,
        bot_name: str = "Meet Agent",
        headless: bool = False,
        capture_endpoint: str = "http://127.0.0.1:8000",
        on_recording_started: Optional[Callable[[], None]] = None,
        debug_dom: bool = False,
        api_key: str = "",
    ) -> None:
        self._profile_dir = str(chrome_profile_dir)
        self._audio_dir = Path(audio_dir)
        self._audio_dir.mkdir(parents=True, exist_ok=True)
        self._bot_name = bot_name
        self._api_key = api_key
        self._headless = headless
        self._capture_endpoint = capture_endpoint.rstrip("/")
        self._on_recording_started = on_recording_started
        self._debug_dom = debug_dom

        self._ext_sw: Optional[Worker] = None
        self._context: Optional[BrowserContext] = None
        self._shutdown_event = asyncio.Event()
        self.session: Optional[BotSession] = None
        self._platform = PLATFORMS[0]  # Meet, até join_async ler o link

    def request_stop(self) -> None:
        """Solicita encerramento gracioso (chamado por SIGINT/SIGTERM)."""
        logger.info("Encerramento gracioso solicitado")
        self._shutdown_event.set()

    # ------------------------------------------------------------------
    # Ponto de entrada principal
    # ------------------------------------------------------------------

    async def join_async(self, meeting_url: str, session_id: Optional[str] = None) -> BotSession:
        """Entra na reunião, grava até ela terminar e retorna a sessão.

        Lança exceção se não conseguir entrar ou iniciar a captura. O áudio já
        enviado fica em <audio_dir>/<session_id>.webm mesmo em caso de falha.
        """
        session_id = session_id or str(uuid.uuid4())
        self.session = BotSession(id=session_id, meeting_url=meeting_url)
        self._platform = platform = detect_platform(meeting_url)
        if not platform.validated:
            logger.warning("%s ainda não foi validado em reunião real (ADR-012)", platform.label)

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
            await page.goto(platform.join_url(meeting_url), wait_until="domcontentloaded", timeout=30_000)

            if platform.enter is None:  # Meet
                await self._verify_login(page)
                await self._dismiss_device_dialogs(page)
                await self._mute_devices(page)
                await self._click_join_button(page)
                await self._wait_until_admitted(page)
                await self._mute_devices(page)
            else:
                await platform.enter(page, self._bot_name)
                await self._wait_until_admitted(page)
                if platform.after_admitted and not self._shutdown_event.is_set():
                    await platform.after_admitted(page)

            if self._shutdown_event.is_set():
                await context.close()
                return self.session

            if platform.speaker_names:
                logger.info("Admitido — ativando legendas")
                await self._enable_captions(page)

            logger.info("Localizando service worker da extensão")
            self._ext_sw = await self._find_ext_sw(context)
            started_ms = await self._start_extension_capture(session_id, page)
            if self._on_recording_started:
                self._on_recording_started()

            tracker = asyncio.create_task(self._track_speakers(page, started_ms)) if platform.speaker_names else None
            try:
                await self._wait_until_meeting_ends(page)
            finally:
                if tracker:
                    tracker.cancel()

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
        raise RuntimeError(
            "Botão de entrar não encontrado em 30s. Link inválido, reunião encerrada "
            "ou a tela do Meet mudou."
        )

    async def _read_status(self, page: Page) -> dict:
        """Estado da chamada: {left, participantCount, leaveVisible, inCall}."""
        if not self._platform.status_js:
            return await page.evaluate(self.MEET_STATUS_JS)
        # Fora do Meet o cliente pode estar dentro de um iframe: vale o frame que mostra a chamada.
        status: dict = {}
        for frame in page.frames:
            try:
                status = await frame.evaluate(self._platform.status_js)
            except Exception:
                continue
            if status.get("leaveVisible"):
                break
        return status

    async def _wait_until_admitted(self, page: Page, timeout: float = 120.0) -> None:
        """Aguarda o bot estar dentro da chamada, e não só na sala de espera."""
        deadline = time.monotonic() + timeout
        status: dict = {}
        while time.monotonic() < deadline:
            if self._shutdown_event.is_set():
                return
            try:
                status = await self._read_status(page)
                if status.get("inCall"):
                    logger.info("Admitido na reunião")
                    return
            except Exception:
                pass
            await asyncio.sleep(2)
        if status.get("leaveVisible"):
            # Sem o sinal de "dentro da chamada" (ainda na espera, ou o Meet mudou
            # a tela): grava mesmo assim; as legendas são tentadas durante a reunião.
            logger.warning("Admissão não confirmada em %.0fs — gravando mesmo assim", timeout)
            return
        raise RuntimeError(f"Bot não foi admitido na reunião em {timeout:.0f}s.")

    # ------------------------------------------------------------------
    # Legendas
    # ------------------------------------------------------------------

    async def _enable_captions(self, page: Page) -> bool:
        """Liga as legendas com o atalho `c`. São a fonte de "quem falou"; sem
        elas a reunião é gravada normalmente, só sem nomes na transcrição.

        O atalho alterna: só é pressionado com a região ausente, e esperamos a
        região aparecer antes de qualquer nova tentativa (senão desligaria).
        """
        if await page.locator(CAPTIONS_REGION).count() > 0:
            return True
        try:
            await page.locator("body").click(position={"x": 400, "y": 300}, timeout=2_000)
            await page.keyboard.press("c")
            await page.wait_for_selector(CAPTIONS_REGION, state="attached", timeout=5_000)
            logger.info("Legendas ligadas")
            return True
        except Exception as exc:
            logger.warning("Legendas ainda desligadas (%s) — nova tentativa em instantes",
                           str(exc).splitlines()[0][:120])
            return False

    async def _track_speakers(self, page: Page, start_ms: int) -> None:
        """Registra a cada segundo quem a legenda mostra falando e quem está na
        sala, em <id>.speakers.jsonl. Roda até ser cancelada.

        Também mantém as legendas ligadas: se a região sumir (ou nunca tiver
        aparecido), tenta de novo a cada CAPTIONS_RETRY_S.
        """
        path = self._audio_dir / f"{self.session.id}.speakers.jsonl"

        def write(event: dict) -> None:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")

        write({"ts": start_ms, "type": "start"})
        last_text: dict[str, str] = {}
        last_participants: list[str] = []
        last_self: Optional[str] = None
        captions_on: Optional[bool] = None
        last_retry = 0.0
        first = True
        while True:
            try:
                snap = await page.evaluate(SPEAKERS_JS)
            except Exception as exc:
                if page.is_closed():
                    return
                logger.warning("Leitura de legendas falhou: %s", str(exc).splitlines()[0][:200])
                await asyncio.sleep(self.CAPTIONS_RETRY_S)
                continue

            now_ms = int(time.time() * 1000)
            if snap["captionsOn"] != captions_on:
                captions_on = snap["captionsOn"]
                write({"ts": now_ms, "type": "captions", "on": captions_on})
            if not captions_on and time.monotonic() - last_retry >= self.CAPTIONS_RETRY_S:
                last_retry = time.monotonic()
                await self._enable_captions(page)

            # O último bloco de cada pessoa é a fala corrente; texto mudou = está falando.
            latest = {c["name"]: c["text"] for c in snap["captions"]}
            if not first:
                for name, text in latest.items():
                    if last_text.get(name) != text:
                        write({"ts": now_ms, "type": "speaker", "name": name})
            last_text, first = latest, False

            if snap["self"] and snap["self"] != last_self:
                last_self = snap["self"]
                write({"ts": now_ms, "type": "self", "name": last_self})
            if snap["participants"] and snap["participants"] != last_participants:
                last_participants = snap["participants"]
                write({"ts": now_ms, "type": "participants", "names": last_participants})

            await asyncio.sleep(1)

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

    def _capture_token(self, session_id: str) -> str:
        """Token da sessão (ADR-007): o navegador recebe só ele, nunca a chave."""
        if not self._api_key:
            return ""
        return hmac.new(self._api_key.encode(), session_id.encode(), hashlib.sha256).hexdigest()

    async def _start_extension_capture(self, session_id: str, page: Page) -> int:
        """Inicia a captura de áudio via tabCapture.

        Anti "message port closed": despacha o comando pela ABA (satisfaz a
        regra de activeTab do Chrome) e confirma o sucesso lendo o STATUS no
        storage via __meetAgentStatus — NÃO depende da resposta da porta de
        mensagem (frágil em MV3 com Playwright).
        """
        # 1) Despacha o START pela aba (via content.js → SW). A resposta aqui é
        #    só "dispatched"; a confirmação real vem do status no passo 2.
        await page.evaluate(
            """async ([session, endpoint, chunkMs, token]) => {
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
                        { type: 'MEET_AGENT_CAPTURE', action: 'START', session, endpoint, chunkMs, token },
                        '*'
                    );
                    setTimeout(() => {
                        window.removeEventListener('message', onResp);
                        resolve({ ok: false, error: 'dispatch timeout' });
                    }, 5000);
                });
            }""",
            [session_id, self._capture_endpoint, 2000, self._capture_token(session_id)],
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
                # Instante em que o MediaRecorder começou (relógio desta máquina):
                # é o zero do áudio para alinhar as legendas.
                return int(last_status.get("startedAt") or time.time() * 1000)
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

    async def _dump_dom_sample(self, page: Page, elapsed: float) -> None:
        try:
            sample = await page.evaluate(DOM_SAMPLE_JS)
            sample["t"] = round(elapsed, 1)
            path = self._audio_dir / f"{self.session.id}.dom.jsonl"
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(sample, ensure_ascii=False) + "\n")
        except Exception as exc:
            logger.debug("Amostra de DOM falhou: %s", exc)

    async def _leave_btn_visible(self, page: Page) -> bool:
        try:
            status = await self._read_status(page)
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
        started = time.monotonic()

        while not self._shutdown_event.is_set():
            await asyncio.sleep(self.END_POLL_INTERVAL)

            if self._shutdown_event.is_set():
                logger.info("Shutdown solicitado — saindo do loop de monitoramento")
                break

            if page.is_closed():
                logger.info("Página fechada — reunião encerrada")
                break
            try:
                status = await self._read_status(page)
            except Exception as exc:
                logger.debug("Erro ao avaliar status: %s", exc)
                continue

            now = time.monotonic()

            if self._debug_dom and self.session:
                await self._dump_dom_sample(page, now - started)

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