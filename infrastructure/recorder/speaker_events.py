"""Leitura do arquivo <id>.speakers.jsonl gravado pelo bot durante a reunião.

Uma linha por evento, com relógio do navegador do bot (epoch em ms):
  {"ts": 1794690000000, "type": "start"}                      início do áudio
  {"ts": ..., "type": "speaker", "name": "Maria"}             legenda mostrava Maria
  {"ts": ..., "type": "participants", "names": ["Maria", …]}  quem estava na sala
  {"ts": ..., "type": "self", "name": "bot meet"}             nome do próprio bot
  {"ts": ..., "type": "captions", "on": true}                 diagnóstico: legendas ligadas?
"""
import json
import re
import logging
from pathlib import Path

from use_cases.speaker_alignment import SpeakerEvents

logger = logging.getLogger(__name__)


def speakers_path(audio_path: str) -> Path:
    return Path(audio_path).with_suffix(".speakers.jsonl")


def load_speaker_events(audio_path: str, exclude: tuple[str, ...] = ()) -> SpeakerEvents:
    """`exclude` remove o próprio bot da lista. Arquivo ausente → eventos vazios."""
    path = speakers_path(audio_path)
    events = SpeakerEvents()
    if not path.exists():
        return events

    hidden = {n.strip().casefold() for n in exclude}
    start_ts = None
    raw: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            raw.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("Linha inválida ignorada em %s", path.name)

    for ev in raw:
        if ev.get("type") == "start" and start_ts is None:
            start_ts = ev.get("ts")
        if ev.get("type") == "self" and isinstance(ev.get("name"), str):
            hidden.add(ev["name"].strip().casefold())

    seen: dict[str, None] = {}
    for ev in raw:
        if ev.get("type") not in ("speaker", "participants"):
            continue
        names = [ev.get("name")] if ev.get("type") == "speaker" else ev.get("names") or []
        for name in names:
            if isinstance(name, str) and name.strip() and name.strip().casefold() not in hidden:
                seen.setdefault(name.strip())
        if ev.get("type") == "speaker" and start_ts is not None:
            name = (ev.get("name") or "").strip()
            t = (ev.get("ts", 0) - start_ts) / 1000
            if name and name.casefold() not in hidden and t >= 0:
                events.speaking.append((t, name))

    events.participants = real_names(list(seen))
    if start_ts is None and raw:
        logger.warning("%s sem evento 'start': presença registrada, falas não atribuídas", path.name)
    return events


# Controles do Meet que aparecem dentro do bloco de vídeo, sobretudo quando alguém apresenta a
# tela ("Aumentar zoom", "Liberar a apresentação de X da tela principal"). O bot os lia como
# nomes: a reunião "Teste 004" (2026-11-15) saiu com 4 participantes, eram 2.
UI_LABEL = re.compile(
    r"\b(zoom|apresenta|presentation|present(ing)?\b|tela principal|main screen|fixar|desafixar|liberar|"
    r"\bpin\b|unpin|mais opç|more options|silenciar|ativar som|mute|unmute|remover|remove|"
    r"planos de fundo|backgrounds?|efeitos|effects|legenda|caption)", re.IGNORECASE)


def real_names(names: list[str]) -> list[str]:
    """Tira da lista o que é rótulo de botão do Meet. Não descarta nome que contém outro nome:
    "Ana" e "Ana Souza" podem ser duas pessoas."""
    return [n for n in names if not UI_LABEL.search(n)]
