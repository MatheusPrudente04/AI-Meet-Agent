"""
infrastructure/exporters/markdown_exporter.py

Exporta reunião como arquivo Markdown estruturado.
Funciona sem nenhuma API externa — útil para modo solo
e como base para os outros exporters.
"""

from pathlib import Path
from datetime import datetime
from domain.entities.meeting import Meeting
from domain.interfaces.exporter import IExporter, ExportError


class MarkdownExporter(IExporter):
    """Exporta reunião para arquivo .md local."""

    def __init__(self, output_dir: str = "data/exports") -> None:
        self._output_dir = Path(output_dir)

    def name(self) -> str:
        return "Markdown"

    def export(self, meeting: Meeting) -> str:
        try:
            content = self.render(meeting)
            safe_title = "".join(c for c in meeting.title if c.isalnum() or c in " -_").strip()
            filename = f"{safe_title}_{meeting.started_at.strftime('%Y%m%d_%H%M')}.md"
            self._output_dir.mkdir(parents=True, exist_ok=True)
            path = self._output_dir / filename

            with open(path, "w", encoding="utf-8") as f:
                f.write(content)

            return str(path)

        except Exception as e:
            raise ExportError(f"Markdown export falhou: {str(e)}") from e

    def render(self, meeting: Meeting) -> str:
        """Markdown da reunião, sem gravar arquivo (download pela interface e pela API)."""
        lines = []
        lines.append(f"# {meeting.title}")
        lines.append(f"\n**Data:** {meeting.started_at.strftime('%d/%m/%Y às %H:%M')}")
        lines.append(f"**Duração:** {meeting.duration_minutes:.0f} minutos")

        if meeting.participants:
            lines.append(f"**Participantes:** {', '.join(meeting.participants)}")

        if meeting.summary:
            s = meeting.summary
            lines.append(f"\n## Visão Geral\n\n{s.overview}")

            if s.topics:
                lines.append("\n## Tópicos Discutidos\n")
                for t in s.topics:
                    lines.append(f"- {t}")

            if s.tasks:
                lines.append("\n## Tarefas\n")
                for task in s.tasks:
                    check = "x" if task.done else " "
                    lines.append(
                        f"- [{check}] **{task.description}** — {task.responsible} ({task.deadline})"
                    )

            if s.decisions:
                lines.append("\n## Decisões\n")
                for dec in s.decisions:
                    lines.append(f"- **{dec.description}**" + (f" — _{dec.status}_" if dec.status else ""))
                    if dec.context:
                        lines.append(f"  > {dec.context}")

        if meeting.transcript_formatted or meeting.transcript_text:
            lines.append("\n## Transcrição Completa\n")
            lines.append("```")
            lines.append(meeting.transcript_formatted or meeting.transcript_text)
            lines.append("```")

        lines.append(f"\n---\n*Gerado por Meet Agent em {datetime.now().strftime('%d/%m/%Y %H:%M')}*")
        return "\n".join(lines)