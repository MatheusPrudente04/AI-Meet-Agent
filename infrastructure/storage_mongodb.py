import asyncio
from datetime import datetime
from typing import Optional

from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

from domain.entities.meeting import Meeting, Summary, Task, Decision
from infrastructure.mongodb_documents import (
    MeetingDocument,
    SummaryEmbedded,
    TaskEmbedded,
    DecisionEmbedded,
)
from repositories.meeting_repository import IMeetingRepository


def _run_async(coro):
    """Executa corrotina de forma síncrona, compatível com uvicorn/FastAPI e Streamlit.

    Regras:
    - Loop ativo (FastAPI/uvicorn): roda em thread separada com asyncio.run()
      para evitar 'Cannot run the event loop while another loop is running'.
    - Sem loop (Streamlit, CLI, testes): roda em thread separada com asyncio.run().

    Por que sempre usar thread separada:
    asyncio.run() cria um novo event loop e o FECHA ao terminar. O motor client
    do Beanie fica vinculado ao loop em que foi criado. Se chamado diretamente no
    main thread, o motor client do call anterior aponta para um loop fechado e
    qualquer call seguinte lança 'Event loop is closed'. Rodar em thread própria
    garante que cada call tem seu próprio loop isolado do anterior.
    """
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _task_to_embedded(t: Task) -> TaskEmbedded:
    return TaskEmbedded(
        description=t.description,
        responsible=t.responsible,
        deadline=t.deadline,
        done=t.done,
    )


def _decision_to_embedded(d: Decision) -> DecisionEmbedded:
    return DecisionEmbedded(description=d.description, context=d.context)


def _summary_to_embedded(s: Summary) -> SummaryEmbedded:
    return SummaryEmbedded(
        overview=s.overview,
        topics=s.topics,
        tasks=[_task_to_embedded(t) for t in s.tasks],
        decisions=[_decision_to_embedded(d) for d in s.decisions],
        created_at=s.created_at,
    )


def _embedded_to_task(e: TaskEmbedded) -> Task:
    return Task(
        description=e.description,
        responsible=e.responsible,
        deadline=e.deadline,
        done=e.done,
    )


def _embedded_to_decision(e: DecisionEmbedded) -> Decision:
    return Decision(description=e.description, context=e.context)


def _embedded_to_summary(e: SummaryEmbedded) -> Summary:
    return Summary(
        overview=e.overview,
        topics=e.topics,
        tasks=[_embedded_to_task(t) for t in e.tasks],
        decisions=[_embedded_to_decision(d) for d in e.decisions],
        created_at=e.created_at,
    )


def _doc_to_entity(doc: MeetingDocument) -> Meeting:
    summary = None
    if doc.summary:
        summary = _embedded_to_summary(doc.summary)
    return Meeting(
        id=doc.id or "",
        title=doc.title,
        started_at=doc.started_at,
        audio_path=doc.audio_path,
        transcript_text=doc.transcript_text,
        transcript_formatted=doc.transcript_formatted,
        transcript_raw=doc.transcript_raw if hasattr(doc, 'transcript_raw') else "",
        participants=doc.participants,
        duration_minutes=doc.duration_minutes,
        summary=summary,
    )


def _entity_to_doc(meeting: Meeting) -> MeetingDocument:
    summary = None
    if meeting.summary:
        summary = _summary_to_embedded(meeting.summary)
    return MeetingDocument(
        id=meeting.id,
        title=meeting.title,
        started_at=meeting.started_at,
        audio_path=meeting.audio_path,
        transcript_text=meeting.transcript_text,
        transcript_formatted=meeting.transcript_formatted,
        transcript_raw=meeting.transcript_raw,
        participants=meeting.participants,
        duration_minutes=meeting.duration_minutes,
        summary=summary,
    )


class MongoDBMeetingRepository(IMeetingRepository):
    def __init__(self, mongo_uri: str, db_name: str = "meetagent") -> None:
        self._uri = mongo_uri
        self._db_name = db_name
        self._initialized = False

    # ── Init ────────────────────────────────────────────────────────────────

    async def _init_beanie(self) -> None:
        """Cria motor client e inicializa Beanie no event loop corrente.

        Chamado DENTRO de cada corrotina (mesmo loop que vai fazer as queries).
        Não pode ser separado em outro asyncio.run() — o motor client fica
        vinculado ao loop em que foi criado.
        """
        client = AsyncIOMotorClient(self._uri)
        await init_beanie(
            database=client[self._db_name],
            document_models=[MeetingDocument],
        )

    async def _ensure_init_async(self) -> None:
        """Para o caminho async (FastAPI): inicializa uma única vez no loop do ASGI."""
        if not self._initialized:
            await self._init_beanie()
            self._initialized = True

    def _run_sync(self, coro):
        """Executa coro síncrono: init + operação no MESMO asyncio.run() → mesmo loop."""
        return _run_async(coro)

    # ── Sync (Streamlit / CLI / worker thread) ───────────────────────────────
    # Cada método síncrono empacota init + operação em UMA corrotina só,
    # garantindo que o motor client seja criado no mesmo loop da query.

    def save(self, meeting: Meeting) -> None:
        async def _coro():
            await self._init_beanie()
            await self._save(meeting)
        self._run_sync(_coro())

    def find_by_id(self, meeting_id: str) -> Optional[Meeting]:
        async def _coro():
            await self._init_beanie()
            return await self._find_by_id(meeting_id)
        return self._run_sync(_coro())

    def list_all(self) -> list[Meeting]:
        async def _coro():
            await self._init_beanie()
            return await self._list_all()
        return self._run_sync(_coro())

    def delete(self, meeting_id: str) -> None:
        async def _coro():
            await self._init_beanie()
            await self._delete(meeting_id)
        self._run_sync(_coro())

    # ── Async (FastAPI / scripts) ────────────────────────────────────────────

    async def save_async(self, meeting: Meeting) -> None:
        await self._ensure_init_async()
        await self._save(meeting)

    async def find_by_id_async(self, meeting_id: str) -> Optional[Meeting]:
        await self._ensure_init_async()
        return await self._find_by_id(meeting_id)

    async def list_all_async(self) -> list[Meeting]:
        await self._ensure_init_async()
        return await self._list_all()

    async def delete_async(self, meeting_id: str) -> None:
        await self._ensure_init_async()
        await self._delete(meeting_id)

    # ── Operações internas ───────────────────────────────────────────────────

    async def _save(self, meeting: Meeting):
        doc = _entity_to_doc(meeting)
        await doc.save()

    async def _find_by_id(self, meeting_id: str) -> Optional[Meeting]:
        doc = await MeetingDocument.get(meeting_id)
        if doc is None:
            return None
        return _doc_to_entity(doc)

    async def _list_all(self) -> list[Meeting]:
        docs = await MeetingDocument.find_all().sort("-started_at").to_list()
        return [_doc_to_entity(d) for d in docs]

    async def _delete(self, meeting_id: str):
        doc = await MeetingDocument.get(meeting_id)
        if doc:
            await doc.delete()
