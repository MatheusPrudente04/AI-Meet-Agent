"""Autoriza o Meet Agent a ler a agenda do Google (uma vez, no computador, com navegador).

    python -m infrastructure.calendar.calendar_setup

Antes: no Google Cloud Console, ative a Google Calendar API e crie um cliente OAuth do tipo
"App para computador"; baixe o JSON e salve em data/google/credentials.json (fora do git).
O login abre no navegador; o token fica em data/google/token.json, que os containers leem pelo
volume ./data. Depois, ENABLE_CALENDAR=true no .env e `docker compose up -d`.
"""
import sys

from config.settings import get_settings
from domain.interfaces.calendar_service import CalendarServiceError
from infrastructure.calendar.google_calendar_service import GoogleCalendarService


def main() -> int:
    s = get_settings()
    service = GoogleCalendarService(s.google_credentials_path, s.google_token_path, interactive=True)
    try:
        events = service.get_upcoming_events(limit=5)
    except CalendarServiceError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1
    print(f"Agenda autorizada. Token em {s.google_token_path}.")
    print("Próximas reuniões com link (Meet, Zoom ou Teams):" if events else
          "Nenhuma reunião com link nas próximas da agenda (a autorização funcionou).")
    for e in events:
        print(f"  {e.start:%d/%m %H:%M}  {e.title}  {e.meet_url}")
    if not s.enable_calendar:
        print("\nFalta ligar: ENABLE_CALENDAR=true no .env e `docker compose up -d`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
