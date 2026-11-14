"""
worker/tasks.py — Tasks Celery para processamento assíncrono.

Ativado apenas em modo colaborativo (APP_MODE=collab).
Cada áudio enviado vira uma task independente processada por um worker.

WINDOWS SUPPORT: Se Celery não funcionar (ex: no Windows), usa threading em memória.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import uuid
import threading
import queue
from datetime import datetime
from pathlib import Path
from typing import Dict, Any

from config.settings import get_settings

settings = get_settings()

# Store de status em memória (fallback para Windows)
_task_status: Dict[str, str] = {}
_task_status_lock = threading.Lock()
_task_queue: queue.Queue = queue.Queue()
_worker_started = False  # Flag para evitar múltiplas inicializações
_worker_lock = threading.Lock()

try:
    from celery import Celery
    celery_app = Celery(
        "meet_agent",
        broker=settings.redis_url,
        backend=settings.redis_url,
    )
    celery_app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="America/Sao_Paulo",
        task_track_started=True,
    )
    HAS_CELERY = True
except ImportError:
    celery_app = None  # modo solo — Celery não instalado
    HAS_CELERY = False


def _obs(message: str) -> None:
    """Log padronizado para worker."""
    print(f"🔍 {message}")


def _set_status(meeting_id: str, status_value: str) -> None:
    """
    Atualiza status no Redis diretamente — sem importar o router da API.
    FIX: o worker não pode importar _set_status de api/routers/meetings.py
    pois isso cria dependência circular (worker → api → container → worker).
    
    WINDOWS SUPPORT: Se Redis não funcionar, usa memória local.
    """
    if HAS_CELERY:
        # Modo collab com Celery — usa Redis
        try:
            import redis as redis_lib
            r = redis_lib.from_url(settings.redis_url)
            r.set(f"status:{meeting_id}", status_value, ex=86400)
        except Exception:
            # Fallback: atualiza também em memória
            with _task_status_lock:
                _task_status[meeting_id] = status_value
    else:
        # Modo Windows: store em memória
        with _task_status_lock:
            _task_status[meeting_id] = status_value


def get_task_status(meeting_id: str) -> str:
    """Obtém status da task (do Redis ou da memória)."""
    if HAS_CELERY:
        try:
            import redis as redis_lib
            r = redis_lib.from_url(settings.redis_url)
            status = r.get(f"status:{meeting_id}")
            if status:
                return status.decode() if isinstance(status, bytes) else status
        except Exception:
            pass
    
    # Fallback: consulta memória
    with _task_status_lock:
        return _task_status.get(meeting_id, "unknown")


def _build_transcript_from_captions(caption_path: str) -> dict:
    """Converte legendas capturadas em um pseudo-transcript."""
    import json

    captions = json.loads(Path(caption_path).read_text(encoding="utf-8"))
    speakers: dict[str, list[str]] = {}
    timeline: list[str] = []

    for entry in captions:
        speaker = entry.get("speaker", "Desconhecido")
        text = entry.get("text", "").strip()
        if not text:
            continue
        if speaker not in speakers:
            speakers[speaker] = []
        speakers[speaker].append(text)
        timeline.append(f"[{speaker}] {text}")

    full_text = "\n".join(timeline)
    formatted = "\n\n".join(
        f"### {s}\n" + "\n".join(lines)
        for s, lines in speakers.items()
    )
    return {
        "full_text": full_text or "[Legendas não disponíveis]",
        "formatted": formatted or "[Legendas não disponíveis]",
        "speakers": list(speakers.keys()) or ["Desconhecido"],
        "duration_minutes": 0.0,
        "from_captions": True,
    }


def process_meeting_task_fn(
    meeting_id: str,
    audio_path: str,
    title: str,
    caption_path: str | None = None,
) -> dict:
    """
    Lógica real de processamento — separada do decorator Celery
    para ser testável sem broker.
    """
    from domain.entities.meeting import Meeting
    from use_cases.transcribe_meeting import TranscribeMeetingInput
    from use_cases.summarize_meeting import SummarizeMeetingInput
    from presentation.container import get_container

    container = get_container()

    # Verifica se o áudio está vazio
    audio_file = Path(audio_path)
    audio_vazio = audio_file.stat().st_size == 0

    if audio_vazio:
        if caption_path and Path(caption_path).exists():
            _set_status(meeting_id, "transcribing")
            _obs(" Áudio vazio — usando legendas como fallback para transcrição.")
            transcript_data = _build_transcript_from_captions(caption_path)
            transcript_text = transcript_data["full_text"]
            transcript_formatted = transcript_data["formatted"]
            speakers = transcript_data["speakers"]
            duration = transcript_data["duration_minutes"]
            _obs(f"Transcrição via legendas: {len(transcript_text)} caracteres")
        else:
            _set_status(meeting_id, "error")
            msg = "Áudio vazio (0 bytes) e nenhuma legenda disponível. Reunião pode estar vazia ou o Meet não estava ativo."
            _obs(f" {msg}")
            return {"status": "error", "error": msg}
    else:
        # 1. Transcrição normal via Whisper
        _set_status(meeting_id, "transcribing")
        t_result = container.transcribe_meeting.execute(
            TranscribeMeetingInput(audio_path=audio_path, with_diarization=True)
        )
        if not t_result.success:
            _set_status(meeting_id, "error")
            return {"status": "error", "error": t_result.error_message}
        transcript = t_result.transcript
        transcript_text = transcript.full_text
        transcript_formatted = transcript.formatted
        speakers = transcript.speakers
        duration = transcript.duration_minutes

    meeting = Meeting(
        id=meeting_id,
        title=title,
        started_at=datetime.now(),
        audio_path=audio_path,
        transcript_text=transcript_text,
        transcript_formatted=transcript_formatted,
        participants=speakers,
        duration_minutes=duration,
    )

    # 2. Resumo
    _set_status(meeting_id, "summarizing")
    s_result = container.summarize_meeting.execute(
        SummarizeMeetingInput(meeting=meeting)
    )
    if not s_result.success:
        _set_status(meeting_id, "error")
        return {"status": "error", "error": s_result.error_message}

    # 3. Persistir reunião no repositório
    meeting.summary = s_result.summary
    container.repository.save(meeting)

    _set_status(meeting_id, "done")
    return {"status": "done", "meeting_id": meeting_id}


class _FallbackTask:
    """Substituto do Celery Task para ambientes sem broker (desenvolvimento/Windows).
    Implementado como classe para que .delay() seja um método real — evita o erro
    'delay não reconhecido em FunctionType' que o Pyright levanta quando o atributo
    é colado em uma função Python pura via process_meeting_task.delay = ...
    """
    def __call__(
        self,
        meeting_id: str,
        audio_path: str,
        title: str,
        caption_path: str | None = None,
        **_kwargs: object,
    ) -> dict:
        _task_queue.put((meeting_id, audio_path, title, caption_path))
        return {"status": "queued", "meeting_id": meeting_id}

    def delay(
        self,
        meeting_id: str,
        audio_path: str,
        title: str,
        caption_path: str | None = None,
        **_kwargs: object,
    ) -> dict:
        return self(meeting_id, audio_path, title, caption_path)


# Tipo único para ambos os modos — Pyright aceita sem conflito
process_meeting_task: _FallbackTask = _FallbackTask()

if celery_app:
    @celery_app.task(
        name="process_meeting",
        bind=True,
        max_retries=3,
        default_retry_delay=30,
    )
    def _celery_process_meeting_task(self, meeting_id: str, audio_path: str, title: str, caption_path: str | None = None):
        try:
            return process_meeting_task_fn(meeting_id, audio_path, title, caption_path)
        except Exception as exc:
            raise self.retry(exc=exc)

    process_meeting_task = _celery_process_meeting_task  # type: ignore[assignment]


def _worker_loop():
    """
    Loop do worker que processa tasks da fila em memória.
    Executa em thread separada (background).
    Apenas usado quando Celery não está disponível (Windows).
    """
    while True:
        try:
            meeting_id, audio_path, title, caption_path = _task_queue.get(timeout=1)
            result = process_meeting_task_fn(meeting_id, audio_path, title, caption_path)
            print(f"Task {meeting_id} concluída: {result['status']}")
        except queue.Empty:
            continue
        except Exception as e:
            print(f"Erro ao processar task: {e}")


def start_background_worker() -> None:
    """
    Inicia worker em background thread (apenas para Windows/desenvolvimento).
    Chamado uma única vez na inicialização.
    Protegido contra múltiplas inicializações com flag thread-safe.
    """
    global _worker_started
    
    if HAS_CELERY:
        return  # Não precisa do fallback com Celery
    
    with _worker_lock:
        if _worker_started:
            return  # Já foi iniciado
        
        _worker_started = True
        worker_thread = threading.Thread(target=_worker_loop, daemon=True)
        worker_thread.start()
        print(" Worker em background (modo Windows) iniciado com sucesso")