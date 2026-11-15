"""
run_bot.py — executa o bot em uma reunião pela linha de comando.

  python -m infrastructure.recorder.run_bot --url "https://meet.google.com/abc-defg-hij"

Em produção quem chama o bot é a task Celery `join_meeting`; este CLI é o modo
solo e a ferramenta de diagnóstico. Ctrl+C (uma vez) encerra salvando o áudio.
"""
from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import uuid
from datetime import datetime

from config.logging_setup import bind_meeting, setup_logging

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

setup_logging()


def main() -> int:
    p = argparse.ArgumentParser(description="Bot gravador do Google Meet")
    p.add_argument("--url", required=True, help="Link da reunião do Meet")
    p.add_argument("--title", default="", help="Título da reunião")
    p.add_argument("--meeting-id", default="", help="Id da reunião (gerado se omitido)")
    p.add_argument("--headless", action="store_true", help="Rodar sem janela")
    args = p.parse_args()

    from domain.entities.meeting import MeetingStatus
    from infrastructure.recorder.bot_job import build_recorder, run_bot_job

    overrides = {"headless": True} if args.headless else {}
    recorder = build_recorder(**overrides)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: recorder.request_stop())

    meeting_id = args.meeting_id or str(uuid.uuid4())
    title = args.title or f"Reunião {datetime.now():%d/%m/%Y %H:%M}"
    bind_meeting(meeting_id)
    meeting = run_bot_job(meeting_id, args.url, title, recorder=recorder)

    print(f"{meeting.id} {meeting.status.value} {meeting.error}".strip(), flush=True)
    return 1 if meeting.status == MeetingStatus.ERROR else 0


if __name__ == "__main__":
    raise SystemExit(main())
