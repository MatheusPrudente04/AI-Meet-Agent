"""
transcript_quality_filter.py — Filtro anti-alucinação (Estágio 4).

Métrica por segmento do faster-whisper:
  - avg_logprob < -1.0      → confiança muito baixa → unreliable
  - compression_ratio > 2.4 → texto repetitivo (loop de alucinação) → unreliable

no_speech_prob não descarta. O Whisper só trata como silêncio quando no_speech_prob > 0.6 **e**
avg_logprob < -1.0, e esse caso já cai na primeira regra. Sozinho, ele não serve: depois do VAD ele vem alto mesmo em fala nítida. Numa
reunião gravada pelo bot em container (2026-11-15) todos os 15 trechos tinham avg_logprob
≈ -0.27 (texto certo) e no_speech_prob de 0.62 a 0.88; com o filtro antigo a transcrição
saía vazia.
"""

def is_reliable(seg: dict) -> bool:
    if seg.get("avg_logprob", 0) < -1.0:
        return False
    if seg.get("compression_ratio", 0) > 2.4:
        return False
    return True


def filter_segments(segments: list[dict]) -> list[dict]:
    result = []
    for seg in segments:
        result.append({
            "start": seg.get("start", 0),
            "end": seg.get("end", 0),
            "text": seg.get("text", "").strip(),
            "speaker": seg.get("speaker", "Speaker 0"),
            "confidence": round(float(seg.get("avg_logprob", -2)), 3),
            "reliable": is_reliable(seg),
            "no_speech_prob": round(float(seg.get("no_speech_prob", 0)), 4),
            "words": [
                {
                    "word": w.get("word", ""),
                    "start": w.get("start", 0),
                    "end": w.get("end", 0),
                    "probability": round(float(w.get("probability", 0)), 2),
                }
                for w in (seg.get("words") or [])
            ],
        })
    return result
