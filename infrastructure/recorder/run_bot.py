"""
run_bot.py — Entrypoint dedicado do bot.

Uso:
  python -m infrastructure.recorder.run_bot --url "https://meet.google.com/abc-defg-hij"

Comportamento ao receber Ctrl+C (SIGINT) ou SIGTERM:
  O bot finaliza graciosamente — salva o áudio gravado até o momento antes de fechar.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Bot gravador do Google Meet")
    p.add_argument("--url", required=True, help="Link da reunião do Meet")
    p.add_argument("--title", default="", help="Título da reunião")
    p.add_argument("--profile", default=None, help="Dir do perfil Chrome (override)")
    p.add_argument("--audio-dir", default=None, help="Dir de saída do áudio (override)")
    p.add_argument("--headless", action="store_true", help="Rodar sem janela")
    return p.parse_args()


async def main() -> int:
    args = parse_args()

    from config.settings import get_settings
    from infrastructure.recorder.playwright_bot_recorder import (
        BotNotLoggedInError,
        PlaywrightBotRecorder,
    )

    settings = get_settings()

    recorder = PlaywrightBotRecorder(
        chrome_profile_dir=args.profile or settings.bot_chrome_profile,
        audio_dir=args.audio_dir or settings.audio_storage_path,
        bot_name=settings.bot_name,
        headless=args.headless or settings.bot_headless,
        on_audio_ready=_make_delivery_callback(args.title),
        capture_port=settings.api_port,
    )

    # Registra handlers de sinal para encerramento gracioso.
    # Ctrl+C (SIGINT) e SIGTERM pedem ao bot para salvar o áudio antes de fechar.
    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, recorder.request_stop)
    else:
        # Windows não suporta add_signal_handler — usa fallback via KeyboardInterrupt
        original_sigint = signal.getsignal(signal.SIGINT)

        def _win_handler(sig, frame):
            recorder.request_stop()
            signal.signal(signal.SIGINT, original_sigint)

        signal.signal(signal.SIGINT, _win_handler)

    MAX_PROCESS_RETRIES = 2
    PROCESS_RETRY_WAIT = 60

    last_exc: Exception | None = None
    for attempt in range(MAX_PROCESS_RETRIES + 1):
        if attempt > 0:
            if recorder._shutdown_event.is_set():
                break
            _log(f"Reiniciando processo (tentativa {attempt + 1}/{MAX_PROCESS_RETRIES + 1}) em {PROCESS_RETRY_WAIT}s...")
            await asyncio.sleep(PROCESS_RETRY_WAIT)

        try:
            session = await recorder.join_async(args.url)
            # Imprime session_id na primeira linha — a UI captura para rastrear status
            try:
                print(session.id, flush=True)
            except BrokenPipeError:
                pass
            return 0
        except BotNotLoggedInError as exc:
            try:
                print(f"ERRO_LOGIN: {exc}", file=sys.stderr, flush=True)
            except BrokenPipeError:
                pass
            return 2
        except Exception as exc:
            if recorder._shutdown_event.is_set():
                # Shutdown gracioso disparado — não é um erro real
                _log("Bot encerrado por solicitação externa — verificando áudio salvo...")
                session = recorder.session
                if session and session.audio_path and session.audio_path.exists():
                    size_kb = session.audio_path.stat().st_size / 1024
                    _log(f"Áudio salvo: {session.audio_path} ({size_kb:.1f} KB)")
                    try:
                        print(session.id, flush=True)
                    except BrokenPipeError:
                        pass
                else:
                    _log("Nenhum áudio foi salvo (gravação não iniciada ou interrompida cedo demais)")
                return 0
            last_exc = exc
            _log(f"Processo falhou (tentativa {attempt + 1}): {exc}")
            if attempt == MAX_PROCESS_RETRIES:
                try:
                    print(f"ERRO: {exc}", file=sys.stderr, flush=True)
                except BrokenPipeError:
                    pass
                return 1

    return 1


def _log(message: str) -> None:
    logging.getLogger("run_bot").info("%s", message)


def _make_delivery_callback(title: str):
    async def deliver(audio_path) -> None:
        from datetime import datetime
        from presentation.container import get_container

        meeting_title = title or f"Reunião {datetime.now():%d/%m/%Y %H:%M}"
        # wait=True: este processo termina logo depois; em solo o processamento roda aqui.
        await asyncio.to_thread(
            get_container().enqueue_processing,
            audio_path.stem, audio_path.as_posix(), meeting_title, True,
        )
        _log(f"Processamento despachado: {audio_path.stem}")

    return deliver


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
