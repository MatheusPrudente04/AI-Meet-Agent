"""
run_bot.py — executa o bot em uma reunião pela linha de comando.

  python -m infrastructure.recorder.run_bot --url "https://meet.google.com/abc-defg-hij"
  python -m infrastructure.recorder.run_bot --url "<link>" --smoke 30   # teste de fumaça

Em produção quem chama o bot é a task Celery `join_meeting`; este CLI é o modo
solo e a ferramenta de diagnóstico. Ctrl+C (uma vez) encerra salvando o áudio.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import uuid
from datetime import datetime

from config.logging_setup import bind_meeting, setup_logging

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

setup_logging()


def smoke_checks(audio_path, speaker_names: bool = True) -> list[tuple[str, bool]]:
    """O que a gravação de fumaça precisa ter deixado em disco. Um item falso
    em legendas, presentes ou bloco do bot indica que o Meet mudou a tela."""
    from infrastructure.recorder.speaker_events import speakers_path

    events = []
    path = speakers_path(str(audio_path))
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    audio_ok = ("áudio gravado", audio_path.exists() and audio_path.stat().st_size > 0)
    if not speaker_names:  # fora do Meet não há leitura de legendas
        return [audio_ok]
    return [
        audio_ok,
        ("legendas ligadas", any(e.get("type") == "captions" and e.get("on") for e in events)),
        ("lista de presentes lida", any(e.get("type") == "participants" and e.get("names") for e in events)),
        ("bloco do próprio bot identificado", any(e.get("type") == "self" and e.get("name") for e in events)),
    ]


def run_smoke(url: str, seconds: float, recorder) -> int:
    """Entra, grava `seconds` e sai, sem transcrever. Exige a API no ar (ela recebe o áudio)."""
    from pathlib import Path

    from config.settings import get_settings
    from domain.entities.meeting import Meeting, MeetingStatus
    from infrastructure.recorder.speaker_events import speakers_path
    from presentation.container import get_container

    repo = get_container().repository
    meeting = Meeting(id=f"smoke-{uuid.uuid4().hex[:8]}", title="Teste de fumaça")
    bind_meeting(meeting.id)
    audio = Path(get_settings().audio_storage_path) / f"{meeting.id}.webm"
    repo.save(meeting)  # a API só aceita áudio de reunião existente

    def on_recording_started() -> None:
        meeting.status = MeetingStatus.RECORDING
        repo.save(meeting)
        asyncio.get_running_loop().call_later(seconds, recorder.request_stop)

    recorder._on_recording_started = on_recording_started
    error = ""
    try:
        asyncio.run(recorder.join_async(url, session_id=meeting.id))
    except Exception as exc:
        error = str(exc)
    checks = smoke_checks(audio, recorder._platform.speaker_names)
    for name, ok in checks:
        print(f"{'OK   ' if ok else 'FALHA'} {name}", flush=True)
    if error:
        print(f"FALHA bot: {error}", flush=True)

    repo.delete(meeting.id)
    for f in (audio, speakers_path(str(audio)), audio.with_suffix(".dom.jsonl")):
        f.unlink(missing_ok=True)
    return 0 if not error and all(ok for _, ok in checks) else 1


def main() -> int:
    p = argparse.ArgumentParser(description="Bot gravador do Google Meet")
    p.add_argument("--url", required=True, help="Link da reunião do Meet")
    p.add_argument("--title", default="", help="Título da reunião")
    p.add_argument("--meeting-id", default="", help="Id da reunião (gerado se omitido)")
    p.add_argument("--headless", action="store_true", help="Rodar sem janela")
    p.add_argument("--smoke", type=float, metavar="SEGUNDOS", help="Teste de fumaça: grava por esse tempo, confere e apaga")
    args = p.parse_args()

    from domain.entities.meeting import MeetingStatus
    from infrastructure.recorder.bot_job import build_recorder, run_bot_job

    overrides = {"headless": True} if args.headless else {}
    recorder = build_recorder(**overrides)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: recorder.request_stop())

    if args.smoke:
        return run_smoke(args.url, args.smoke, recorder)

    meeting_id = args.meeting_id or str(uuid.uuid4())
    title = args.title or f"Reunião {datetime.now():%d/%m/%Y %H:%M}"
    bind_meeting(meeting_id)
    meeting = run_bot_job(meeting_id, args.url, title, recorder=recorder)

    print(f"{meeting.id} {meeting.status.value} {meeting.error}".strip(), flush=True)
    return 1 if meeting.status == MeetingStatus.ERROR else 0


if __name__ == "__main__":
    raise SystemExit(main())
