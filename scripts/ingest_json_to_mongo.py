"""
scripts/ingest_json_to_mongo.py — Ingere um .json de reunião no MongoDB
seguindo o fluxo real da aplicação:

  JSON → entidades de domínio → MongoDBMeetingRepository.save() → MongoDB

Uso:
  python scripts/ingest_json_to_mongo.py data/test_meetings/<id>.json
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

# Garante que o root do projeto está no PYTHONPATH
sys.path.insert(0, str(Path(__file__).parent.parent))


def _parse_meeting(raw: dict):
    """Converte o dict do JSON nas entidades de domínio do projeto."""
    from domain.entities.meeting import Meeting, Summary, Task, Decision

    # ── Summary ────────────────────────────────────────────────────────
    summary = None
    if raw_summary := raw.get("summary"):
        tasks = [
            Task(
                description=t["description"],
                responsible=t.get("responsible", "Não definido"),
                deadline=t.get("deadline"),
                done=t.get("done", False),
            )
            for t in raw_summary.get("tasks", [])
        ]
        decisions = [
            Decision(
                description=d["description"],
                context=d.get("context", ""),
            )
            for d in raw_summary.get("decisions", [])
        ]
        created_at = raw_summary.get("created_at")
        summary = Summary(
            overview=raw_summary.get("overview", ""),
            topics=raw_summary.get("topics", []),
            tasks=tasks,
            decisions=decisions,
            created_at=datetime.fromisoformat(created_at) if created_at else datetime.now(),
        )

    # ── Meeting ────────────────────────────────────────────────────────
    started_at_raw = raw.get("started_at")
    meeting = Meeting(
        id=raw["id"],
        title=raw.get("title", "Sem título"),
        started_at=datetime.fromisoformat(started_at_raw) if started_at_raw else datetime.now(),
        audio_path=raw.get("audio_path"),
        transcript_text=raw.get("transcript_text", ""),
        transcript_formatted=raw.get("transcript_formatted", ""),
        participants=raw.get("participants", []),
        participant_info=raw.get("participant_info", {}),
        duration_minutes=raw.get("duration_minutes", 0.0),
        speech_blocks=raw.get("speech_blocks", []),
        speaker_observations=raw.get("speaker_observations", []),
        summary=summary,
    )
    return meeting


async def main(json_path: Path) -> None:
    from config.settings import get_settings
    from infrastructure.storage_mongodb import MongoDBMeetingRepository

    settings = get_settings()

    print(f"[1/4] Lendo JSON: {json_path}")
    raw = json.loads(json_path.read_text(encoding="utf-8"))
    meeting = _parse_meeting(raw)
    print(f"      id          : {meeting.id}")
    print(f"      título      : {meeting.title}")
    print(f"      participantes: {meeting.participants}")
    print(f"      transcrição : {len(meeting.transcript_text)} chars")
    print(f"      tarefas     : {len(meeting.summary.tasks) if meeting.summary else 0}")
    print(f"      decisões    : {len(meeting.summary.decisions) if meeting.summary else 0}")

    print(f"\n[2/4] Conectando ao MongoDB: {settings.mongo_uri} / db={settings.mongo_db}")
    repo = MongoDBMeetingRepository(
        mongo_uri=settings.mongo_uri,
        db_name=settings.mongo_db,
    )

    print("\n[3/4] Salvando via repositório → MongoDB (camada de infraestrutura)...")
    await repo.save_async(meeting)
    print("      ✓ meeting.save() OK")

    print("\n[4/4] Verificando: lendo de volta do MongoDB...")
    recovered = await repo.find_by_id_async(meeting.id)
    if recovered is None:
        print("      ✗ Documento não encontrado após save! Verifique o MongoDB.")
        sys.exit(1)

    print(f"      ✓ Documento recuperado: id={recovered.id}")
    print(f"      ✓ Título              : {recovered.title}")
    print(f"      ✓ Participantes       : {recovered.participants}")
    if recovered.summary:
        print(f"      ✓ Resumo              : {recovered.summary.overview[:60]}...")
        print(f"      ✓ Tarefas             : {[t.description for t in recovered.summary.tasks]}")
        print(f"      ✓ Decisões            : {[d.description for d in recovered.summary.decisions]}")

    print("\n✅  Pipeline completo — reunião persistida no MongoDB com sucesso.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python scripts/ingest_json_to_mongo.py <caminho_do_json>")
        sys.exit(1)

    target = Path(sys.argv[1])
    if not target.exists():
        print(f"Arquivo não encontrado: {target}")
        sys.exit(1)

    asyncio.run(main(target))
