"""Plataformas de reunião que o bot sabe abrir (ADR-012).

A captura de áudio é a mesma em todas (áudio da aba, pela extensão). O que muda
por plataforma é: o endereço que abre a reunião no navegador, os passos até
entrar e como saber se o bot está dentro da chamada.

Este módulo não importa o Playwright: a API usa `detect_platform` para validar
o link sem carregar o navegador.

Estado em 2026-11: só o Meet foi validado em reunião real. Teams e Zoom seguem
a tela conhecida do cliente web de cada um e precisam do teste de fumaça
(`run_bot --smoke`) antes de uso; nomes de quem fala só existem no Meet.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)


class UnsupportedMeetingUrl(ValueError):
    """Link recusado; a mensagem pode ser mostrada ao usuário."""


# Mesmo formato do MEET_STATUS_JS do recorder. `participantCount: -1` = desconhecido.
# `left` fica sempre falso: o fim é detectado pelo sumiço do botão de sair.
_STATUS_JS = """
(() => {
    const visible = el => !!(el && el.offsetParent !== null);
    const leaveVisible = Array.from(document.querySelectorAll(%(leave)r)).some(visible);
    const counter = document.querySelector(%(count)r);
    const n = counter ? parseInt((counter.innerText || '').replace(/\\D/g, ''), 10) : NaN;
    const waiting = %(lobby)s.test(document.body ? document.body.innerText : '');
    return { left: false, participantCount: isNaN(n) ? -1 : n, leaveVisible,
             inCall: leaveVisible && !waiting };
})()
"""


async def first_visible(page, selectors: list[str], timeout: float = 15.0):
    """Primeiro elemento visível entre os seletores, em qualquer frame da página."""
    deadline = time.monotonic() + timeout
    while True:
        for frame in page.frames:
            for selector in selectors:
                try:
                    loc = frame.locator(selector).first
                    if await loc.count() > 0 and await loc.is_visible():
                        return loc
                except Exception:
                    pass
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.5)


async def _click(page, selectors: list[str], what: str, timeout: float = 15.0, required: bool = False) -> bool:
    loc = await first_visible(page, selectors, timeout)
    if loc is None:
        if required:
            raise RuntimeError(f"{what}: não encontrado em {timeout:.0f}s. Link inválido ou a tela mudou.")
        return False
    await loc.click(timeout=5_000)
    logger.info("Clicado: %s", what)
    return True


async def _switch_off(page, selectors: list[str], what: str) -> None:
    """Desliga um dispositivo se o botão indicar que está ligado."""
    loc = await first_visible(page, selectors, timeout=3)
    if loc is not None:
        await loc.click(timeout=3_000)
        logger.info("Dispositivo silenciado: %s", what)


# ── Teams ────────────────────────────────────────────────────────────

async def _enter_teams(page, bot_name: str) -> None:
    # A página do link oferece abrir o aplicativo; o bot continua no navegador.
    await _click(page, [
        '[data-tid="joinOnWeb"]',
        'button:has-text("Continue on this browser")', 'button:has-text("Continuar neste navegador")',
        'button:has-text("Join on the web instead")',
    ], "Teams: continuar no navegador", timeout=20)
    name = await first_visible(page, [
        '[data-tid="prejoin-display-name-input"]', 'input[placeholder*="name" i]', 'input[placeholder*="nome" i]',
    ], timeout=40)
    if name is not None:  # conta logada não pede nome
        await name.fill(bot_name)
    # Os interruptores trazem aria-checked="true" enquanto o dispositivo está ligado.
    await _switch_off(page, ['[data-tid="toggle-mute"][aria-checked="true"]'], "microfone")
    await _switch_off(page, ['[data-tid="toggle-video"][aria-checked="true"]'], "câmera")
    await _click(page, [
        '[data-tid="prejoin-join-button"]', 'button:has-text("Join now")', 'button:has-text("Ingressar agora")',
    ], "Teams: entrar", timeout=20, required=True)


# ── Zoom ─────────────────────────────────────────────────────────────

def _zoom_url(url: str) -> str:
    """Link de convite → cliente web (o link normal tenta abrir o aplicativo)."""
    parsed = urlparse(url)
    match = re.search(r"/(?:j|w|s|wc/join|wc)/(\d{9,12})", parsed.path)
    if not match:
        raise UnsupportedMeetingUrl("Link do Zoom sem o número da reunião (esperado: https://zoom.us/j/<número>).")
    pwd = parse_qs(parsed.query).get("pwd", [""])[0]
    return f"https://app.zoom.us/wc/{match.group(1)}/join" + (f"?pwd={pwd}" if pwd else "")


async def _enter_zoom(page, bot_name: str) -> None:
    await _click(page, ["#onetrust-reject-all-handler"], "Zoom: recusar cookies opcionais", timeout=5)
    # O Zoom exige aceitar os termos de uso a cada navegador novo.
    await _click(page, ["#wc_agree1", 'button:has-text("I Agree")', 'button:has-text("Concordo")'],
                 "Zoom: termos de uso", timeout=5)
    name = await first_visible(page, ["#input-for-name", 'input[placeholder*="name" i]', 'input[placeholder*="nome" i]'], timeout=40)
    if name is None:
        raise RuntimeError("Zoom: campo de nome não encontrado. Link inválido ou a tela mudou.")
    await name.fill(bot_name)
    await _switch_off(page, ['#preview-audio-control-button[aria-label="Mute"]'], "microfone")
    await _switch_off(page, ['#preview-video-control-button[aria-label="Stop Video"]'], "câmera")
    await _click(page, [
        "button.preview-join-button", 'button:has-text("Join")', 'button:has-text("Entrar")',
    ], "Zoom: entrar", timeout=20, required=True)


async def _after_zoom(page) -> None:
    # Sem "entrar com áudio do computador" o cliente web não toca o som da reunião.
    await _click(page, [
        "button.join-audio-by-voip__join-btn",
        'button:has-text("Join Audio by Computer")', 'button:has-text("Entrar com áudio do computador")',
    ], "Zoom: áudio do computador", timeout=20)
    await _switch_off(page, ['button.join-audio-container__btn[aria-label^="mute" i]'], "microfone")


# ── Catálogo ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Platform:
    key: str
    label: str
    hosts: tuple[str, ...]
    example: str
    validated: bool  # já passou por reunião real?
    speaker_names: bool = False  # atribui falas por nome (legendas)
    join_url: Callable[[str], str] = lambda url: url
    # None = fluxo do Meet, que vive no recorder.
    enter: Optional[Callable[..., Awaitable[None]]] = None
    after_admitted: Optional[Callable[..., Awaitable[None]]] = None
    status_js: str = ""  # vazio = MEET_STATUS_JS do recorder
    unsupported: str = ""  # motivo, quando o link é reconhecido mas o bot não entra

    def matches(self, host: str) -> bool:
        return any(host == h or host.endswith("." + h) for h in self.hosts)


PLATFORMS = (
    Platform("meet", "Google Meet", ("meet.google.com",), "https://meet.google.com/abc-defg-hij",
             validated=True, speaker_names=True),
    Platform("teams", "Microsoft Teams", ("teams.microsoft.com", "teams.live.com"),
             "https://teams.microsoft.com/l/meetup-join/...", validated=False,
             enter=_enter_teams,
             status_js=_STATUS_JS % dict(
                 leave='#hangup-button, [data-tid="hangup-main-btn"]',
                 count='[data-tid="roster-button-tile"]',
                 lobby=r"/let you in|permitir sua entrada|aguardando|waiting for/i")),
    Platform("zoom", "Zoom", ("zoom.us",), "https://zoom.us/j/1234567890?pwd=...", validated=False,
             join_url=_zoom_url, enter=_enter_zoom, after_admitted=_after_zoom,
             status_js=_STATUS_JS % dict(
                 leave='button[aria-label="Leave"], button[aria-label="Sair"], .footer__leave-btn',
                 count=".footer-button__number-counter",
                 lobby=r"/will let you in|waiting for the host|aguarde|sala de espera/i")),
    Platform("discord", "Discord", ("discord.com", "discord.gg"), "https://discord.com/channels/...",
             validated=False,
             unsupported="O Discord não permite automatizar uma conta de usuário pelo navegador; "
                         "a gravação exige um bot oficial do Discord, ainda não implementado (ADR-012)."),
)


def detect_platform(url: str) -> Platform:
    """Plataforma do link. Lança UnsupportedMeetingUrl com mensagem para o usuário."""
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    platform = next((p for p in PLATFORMS if p.matches(host)), None) if parsed.scheme == "https" else None
    if platform is None:
        names = ", ".join(p.label for p in PLATFORMS if not p.unsupported)
        raise UnsupportedMeetingUrl(f"Informe um link https de reunião ({names}).")
    if platform.unsupported:
        raise UnsupportedMeetingUrl(platform.unsupported)
    platform.join_url(url)  # valida o formato (ex.: número da reunião do Zoom)
    return platform
