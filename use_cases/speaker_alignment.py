"""Atribui nomes reais aos segmentos da transcrição (ADR-009 fase 2).

A evidência vem das legendas do Meet: cada evento diz "no instante t, a legenda
mostrava <nome> falando". O texto não é usado, só nome e instante.
"""
from collections import Counter
from dataclasses import dataclass, field

from domain.entities.transcript import Segment

# A legenda aparece depois que a pessoa começa a falar.
CAPTION_LAG_S = 1.0
# Folga nas bordas do segmento, para absorver imprecisão dos dois relógios.
EDGE_TOLERANCE_S = 1.0


@dataclass
class SpeakerEvents:
    """Eventos de uma gravação, com tempo em segundos desde o início do áudio."""
    speaking: list[tuple[float, str]] = field(default_factory=list)
    participants: list[str] = field(default_factory=list)


def assign_speakers(segments: list[Segment], events: SpeakerEvents) -> int:
    """Troca o falante de cada segmento pelo nome com mais evidência no intervalo.

    Sem evidência o segmento fica como está ("Speaker N"): nome errado é pior
    que nome ausente. Exceção: com um único participante além do bot (que
    nunca fala), toda fala é dele. Retorna quantos segmentos receberam nome.
    """
    sole = events.participants[0] if len(events.participants) == 1 else None
    named = 0
    for seg in segments:
        lo, hi = seg.start - EDGE_TOLERANCE_S, seg.end + EDGE_TOLERANCE_S
        votes = Counter(name for t, name in events.speaking if lo <= t - CAPTION_LAG_S <= hi)
        if votes:
            seg.speaker = votes.most_common(1)[0][0]
            named += 1
        elif sole:
            seg.speaker = sole
            named += 1
    return named
