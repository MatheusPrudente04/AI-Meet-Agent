"""Filtro anti-alucinação do Whisper (transcript_quality_filter)."""
from infrastructure.transcriber.transcript_quality_filter import filter_segments, is_reliable


def test_fala_nitida_com_no_speech_alto_e_mantida():
    # Caso real (bot em container, 2026-11-15): texto certo, no_speech_prob alto depois do VAD.
    assert is_reliable({"avg_logprob": -0.27, "no_speech_prob": 0.88, "compression_ratio": 1.5})


def test_silencio_baixa_confianca_e_loop_sao_descartados():
    assert not is_reliable({"avg_logprob": -1.3, "no_speech_prob": 0.9, "compression_ratio": 1.2})
    assert not is_reliable({"avg_logprob": -1.3, "no_speech_prob": 0.1, "compression_ratio": 1.2})
    assert not is_reliable({"avg_logprob": -0.2, "no_speech_prob": 0.1, "compression_ratio": 3.0})


def test_filter_marca_cada_segmento():
    out = filter_segments([{"text": " oi ", "avg_logprob": -0.3, "no_speech_prob": 0.7},
                           {"text": "ruído", "avg_logprob": -1.5}])
    assert [(s["text"], s["reliable"]) for s in out] == [("oi", True), ("ruído", False)]
