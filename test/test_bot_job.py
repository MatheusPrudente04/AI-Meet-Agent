"""Ciclo de vida do bot (ADR-006): status, áudio parcial e falha sem áudio."""
from pathlib import Path

import pytest

from domain.entities.meeting import MeetingStatus
from infrastructure.json_meeting_repository import JsonMeetingRepository
from infrastructure.recorder import bot_job


class FakeRecorder:
    def __init__(self, audio_dir: Path, write_audio=True, error=None):
        self.audio_dir, self.write_audio, self.error = audio_dir, write_audio, error
        self._on_recording_started = None

    async def join_async(self, meeting_url, session_id=None):
        if self.write_audio:
            self._on_recording_started()
            (self.audio_dir / f"{session_id}.webm").write_bytes(b"audio")
        if self.error:
            raise self.error


class FakeContainer:
    def __init__(self, repo):
        self.repository, self.enqueued = repo, []

    def enqueue_processing(self, meeting_id, audio_path, title, wait=False):
        self.enqueued.append((meeting_id, audio_path, wait))


@pytest.fixture
def env(tmp_path, monkeypatch):
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    container = FakeContainer(JsonMeetingRepository(str(tmp_path / "meetings")))
    settings = type("S", (), {"audio_storage_path": str(audio_dir)})()
    monkeypatch.setattr(bot_job, "get_settings", lambda: settings)
    monkeypatch.setattr("presentation.container.get_container", lambda: container)
    return container, audio_dir


def test_gravacao_ok_marca_recording_e_despacha_processamento(env):
    container, audio_dir = env
    bot_job.run_bot_job("m1", "https://meet.google.com/x", "T", recorder=FakeRecorder(audio_dir))
    saved = container.repository.find_by_id("m1")
    assert saved.status == MeetingStatus.RECORDING  # o pipeline (fake aqui) é quem avança daqui
    assert container.enqueued == [("m1", (audio_dir / "m1.webm").as_posix(), True)]


def test_bot_cai_no_meio_ainda_processa_audio_parcial(env):
    container, audio_dir = env
    bot_job.run_bot_job("m1", "u", "T", recorder=FakeRecorder(audio_dir, error=RuntimeError("caiu")))
    assert len(container.enqueued) == 1


def test_falha_sem_audio_vira_erro_persistido(env):
    container, audio_dir = env
    recorder = FakeRecorder(audio_dir, write_audio=False, error=RuntimeError("Bot não foi admitido"))
    bot_job.run_bot_job("m1", "u", "T", recorder=recorder)
    saved = container.repository.find_by_id("m1")
    assert saved.status == MeetingStatus.ERROR and "não foi admitido" in saved.error
    assert saved.audio_path.endswith("m1.webm") and container.enqueued == []
