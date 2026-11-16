"""Sprint 2B: nomes reais dos participantes (ADR-009 fase 2)."""
import json

from domain.entities.meeting import Meeting, MeetingStatus, Summary
from domain.entities.transcript import Segment, Transcript
from infrastructure.json_meeting_repository import JsonMeetingRepository
from infrastructure.recorder.speaker_events import load_speaker_events, speakers_path
from use_cases.process_meeting import ProcessMeetingInput, ProcessMeetingUC
from use_cases.speaker_alignment import SpeakerEvents, assign_speakers
from use_cases.summarize_meeting import SummarizeMeetingUC
from use_cases.transcribe_meeting import TranscribeMeetingUC


def seg(start, end, speaker="Speaker 0"):
    return Segment(start, end, speaker, "texto")


# ── Alinhamento ──────────────────────────────────────────────────────────

def test_segmento_recebe_o_nome_com_mais_evidencia():
    s = [seg(0, 10)]
    ev = SpeakerEvents(speaking=[(2, "Ana"), (4, "Ana"), (6, "Ana"), (8, "Bruno")])
    assert assign_speakers(s, ev) == 1
    assert s[0].speaker == "Ana"


def test_legenda_atrasada_ainda_cai_no_segmento_certo():
    # Ana fala de 0 a 5; a legenda dela só aparece em 5.8 (atraso). Bruno fala de 5 a 10.
    s = [seg(0, 5), seg(5, 10, "Speaker 1")]
    ev = SpeakerEvents(speaking=[(1.5, "Ana"), (3.0, "Ana"), (5.8, "Ana"), (7.5, "Bruno"), (9.0, "Bruno"), (10.5, "Bruno")])
    assign_speakers(s, ev)
    assert [x.speaker for x in s] == ["Ana", "Bruno"]


def test_troca_rapida_de_falante():
    s = [seg(0, 3), seg(3, 6), seg(6, 9)]
    ev = SpeakerEvents(speaking=[(1.5, "Ana"), (2.5, "Ana"), (4.6, "Bruno"), (5.5, "Bruno"), (7.6, "Ana"), (8.6, "Ana"), (9.5, "Ana")])
    assign_speakers(s, ev)
    assert [x.speaker for x in s] == ["Ana", "Bruno", "Ana"]


def test_sem_evidencia_mantem_o_rotulo_original():
    s = [seg(0, 5), seg(60, 65, "Speaker 1")]
    assert assign_speakers(s, SpeakerEvents(speaking=[(2, "Ana")])) == 1
    assert [x.speaker for x in s] == ["Ana", "Speaker 1"]
    s2 = [seg(0, 5)]
    assert assign_speakers(s2, SpeakerEvents()) == 0 and s2[0].speaker == "Speaker 0"


# ── Arquivo de eventos ───────────────────────────────────────────────────

def write_events(audio, events):
    speakers_path(str(audio)).write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")


def test_loader_converte_relogio_em_segundos_e_remove_o_bot(tmp_path):
    audio = tmp_path / "m1.webm"
    write_events(audio, [
        {"ts": 1000, "type": "participants", "names": ["Ana", "Meet Agent", "Bruno"]},
        {"ts": 5000, "type": "start"},
        {"ts": 4000, "type": "speaker", "name": "Ana"},       # antes do início do áudio: descartado
        {"ts": 7500, "type": "speaker", "name": "Ana"},
        {"ts": 9000, "type": "speaker", "name": "meet agent"},  # o próprio bot
        {"ts": 12000, "type": "speaker", "name": "Carla"},     # falou sem estar na lista de presença
    ])
    ev = load_speaker_events(str(audio), exclude=("Meet Agent",))
    assert ev.speaking == [(2.5, "Ana"), (7.0, "Carla")]
    assert ev.participants == ["Ana", "Bruno", "Carla"]


def test_loader_esconde_o_nome_que_o_bot_informou_como_seu(tmp_path):
    audio = tmp_path / "m3.webm"
    write_events(audio, [
        {"ts": 0, "type": "start"},
        {"ts": 10, "type": "captions", "on": True},
        {"ts": 10, "type": "participants", "names": ["Ana", "bot meet"]},
        {"ts": 20, "type": "self", "name": "bot meet"},
    ])
    assert load_speaker_events(str(audio)).participants == ["Ana"]


def test_unico_participante_recebe_as_falas_sem_evidencia():
    s = [seg(0, 5), seg(60, 65, "Speaker 1")]
    assert assign_speakers(s, SpeakerEvents(participants=["Ana"])) == 2
    assert [x.speaker for x in s] == ["Ana", "Ana"]
    s2 = [seg(0, 5)]
    assert assign_speakers(s2, SpeakerEvents(participants=["Ana", "Bruno"])) == 0


def test_loader_sem_arquivo_ou_sem_start(tmp_path):
    assert load_speaker_events(str(tmp_path / "nada.webm")) == SpeakerEvents()
    audio = tmp_path / "m2.webm"
    speakers_path(str(audio)).write_text('{"ts": 1, "type": "speaker", "name": "Ana"}\nlixo\n', encoding="utf-8")
    ev = load_speaker_events(str(audio))
    assert ev.speaking == [] and ev.participants == ["Ana"]


# ── Pipeline e contexto do LLM ───────────────────────────────────────────

class FakeTranscriber:
    def transcribe_with_diarization(self, audio_path):
        return Transcript(full_text="oi. tudo bem.", segments=[
            Segment(0, 5, "Speaker 0", "oi"), Segment(5, 10, "Speaker 1", "tudo bem")])


class FakeLLM:
    def __init__(self):
        self.seen = ""

    def summarize(self, transcript, meeting_type=None):
        self.seen = transcript
        return Summary(overview="ok")


def test_pipeline_grava_nomes_reais_e_os_envia_ao_llm(tmp_path):
    audio = tmp_path / "m1.webm"
    audio.write_bytes(b"audio")
    write_events(audio, [
        {"ts": 0, "type": "start"},
        {"ts": 500, "type": "participants", "names": ["Ana", "Bruno", "Carla"]},
        {"ts": 3000, "type": "speaker", "name": "Ana"},
        {"ts": 8000, "type": "speaker", "name": "Bruno"},
    ])
    repo, llm = JsonMeetingRepository(str(tmp_path / "db")), FakeLLM()
    uc = ProcessMeetingUC(TranscribeMeetingUC(FakeTranscriber()), SummarizeMeetingUC(llm), repo,
                          load_speaker_events=load_speaker_events)
    uc.execute(ProcessMeetingInput("m1", str(audio), "T"))

    saved = repo.find_by_id("m1")
    assert saved.status == MeetingStatus.DONE
    assert saved.participants == ["Ana", "Bruno", "Carla"]          # Carla esteve, não falou
    assert "Ana: oi" in saved.transcript_formatted and "Bruno: tudo bem" in saved.transcript_formatted
    assert "Speaker" not in saved.transcript_formatted
    assert llm.seen.startswith("PARTICIPANTES DA REUNIÃO: Ana, Bruno, Carla")


def test_sem_eventos_o_pipeline_continua_como_antes(tmp_path):
    audio = tmp_path / "m1.webm"
    audio.write_bytes(b"audio")
    repo = JsonMeetingRepository(str(tmp_path / "db"))
    uc = ProcessMeetingUC(TranscribeMeetingUC(FakeTranscriber()), SummarizeMeetingUC(FakeLLM()), repo,
                          load_speaker_events=load_speaker_events)
    uc.execute(ProcessMeetingInput("m1", str(audio), "T"))
    assert repo.find_by_id("m1").participants == ["Speaker 0", "Speaker 1"]


def test_transcript_for_llm_sem_participantes_nao_inventa_cabecalho():
    assert Meeting(id="x", title="t", transcript_text="abc").transcript_for_llm == "abc"


# ── Acompanhamento no recorder ───────────────────────────────────────────

def test_tracker_registra_fala_so_quando_o_texto_muda(tmp_path):
    import asyncio
    from infrastructure.recorder.playwright_bot_recorder import BotSession, PlaywrightBotRecorder

    base = {"captionsOn": True, "self": "bot meet"}
    snaps = iter([
        # legendas desligadas: o tracker tenta ligar de novo
        {"captionsOn": False, "self": None, "captions": [], "participants": ["Ana", "Bruno"]},
        {**base, "captions": [{"name": "Ana", "text": "oi"}], "participants": ["Ana", "Bruno"]},
        {**base, "captions": [{"name": "Ana", "text": "oi pessoal"}], "participants": ["Ana", "Bruno"]},
        {**base, "captions": [{"name": "Ana", "text": "oi pessoal"}], "participants": ["Ana", "Bruno"]},  # parada
        {**base, "captions": [{"name": "Ana", "text": "oi pessoal"}, {"name": "Bruno", "text": "bom dia"}],
         "participants": ["Ana", "Bruno", "Carla"]},
    ])
    retries = []

    class FakePage:
        async def evaluate(self, js):
            try:
                return next(snaps)
            except StopIteration:
                raise asyncio.CancelledError

    rec = PlaywrightBotRecorder(chrome_profile_dir=str(tmp_path / "p"), audio_dir=str(tmp_path))
    rec.session = BotSession(id="m1", meeting_url="u")

    async def fake_enable(page):
        retries.append(1)
        return True
    rec._enable_captions = fake_enable

    async def run():
        real_sleep = asyncio.sleep
        asyncio.sleep = lambda *_: real_sleep(0)
        try:
            await rec._track_speakers(FakePage(), start_ms=1000)
        except asyncio.CancelledError:
            pass
        finally:
            asyncio.sleep = real_sleep

    asyncio.run(run())
    events = [json.loads(l) for l in (tmp_path / "m1.speakers.jsonl").read_text(encoding="utf-8").splitlines()]
    assert events[0] == {"ts": 1000, "type": "start"}
    assert [e["name"] for e in events if e["type"] == "speaker"] == ["Ana", "Ana", "Bruno"]
    assert len(retries) == 1
    assert [e["on"] for e in events if e["type"] == "captions"] == [False, True]
    assert [e["name"] for e in events if e["type"] == "self"] == ["bot meet"]
    assert [e["names"] for e in events if e["type"] == "participants"] == [["Ana", "Bruno"], ["Ana", "Bruno", "Carla"]]


def test_fumaca_acusa_o_que_faltou(tmp_path):
    from infrastructure.recorder.run_bot import smoke_checks

    audio = tmp_path / "s.webm"
    assert not any(ok for _, ok in smoke_checks(audio))

    audio.write_bytes(b"x")
    (tmp_path / "s.speakers.jsonl").write_text(
        '{"type": "captions", "on": true}\n{"type": "participants", "names": ["Ana"]}\n', encoding="utf-8"
    )
    assert [ok for _, ok in smoke_checks(audio)] == [True, True, True, False]


def test_botoes_do_meet_nao_viram_participantes(tmp_path):
    """Regressão (Teste 004, 2026-11-15): com tela apresentada, rótulos de botão viraram "participantes"."""
    from infrastructure.recorder.speaker_events import load_speaker_events, real_names
    audio = tmp_path / "m.webm"
    (tmp_path / "m.speakers.jsonl").write_text(
        '{"ts": 1000, "type": "start"}\n{"ts": 1000, "type": "self", "name": "bot meet"}\n'
        '{"ts": 1100, "type": "participants", "names": ["Aumentar zoom", "Hugo dos Reis", "Matheus Silva", "bot meet"]}\n'
        '{"ts": 1200, "type": "participants", "names": ["Liberar a apresentação de Hugo dos Reis da tela principal", '
        '"Hugo dos Reis", "Matheus Silva", "bot meet"]}\n', encoding="utf-8")
    assert load_speaker_events(str(audio)).participants == ["Hugo dos Reis", "Matheus Silva"]
    # nome que contém outro nome é outra pessoa, não botão
    assert real_names(["Ana", "Ana Souza", "Fixar Ana", "Mais opções"]) == ["Ana", "Ana Souza"]
