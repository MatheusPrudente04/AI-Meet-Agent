"""
infrastructure/calendar/google_calendar_service.py

Integração com Google Calendar API v3.
Pré-carrega título e participantes antes da reunião começar.

Requer:
    pip install google-auth google-auth-oauthlib google-api-python-client

Configuração OAuth:
    1. Console Cloud → APIs & Serviços → Credenciais
    2. Criar credencial OAuth 2.0 para Desktop
    3. Baixar credentials.json e colocar na raiz do projeto
    4. Na primeira execução, abre o browser para autorizar
    5. Salva token.json automaticamente para reusos futuros

Variáveis de ambiente:
    GOOGLE_CREDENTIALS_PATH=credentials.json  (padrão)
    GOOGLE_TOKEN_PATH=token.json              (padrão)
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import re
from pathlib import Path
from typing import Optional

from domain.entities.calendar_event import CalendarEvent
from domain.interfaces.calendar_service import ICalendarService, CalendarServiceError

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


class GoogleCalendarService(ICalendarService):
    """
    Implementação de ICalendarService usando Google Calendar API v3.
    Autentica via OAuth 2.0 com token persistido em arquivo local.
    """

    def __init__(
        self,
        credentials_path: str = "data/google/credentials.json",
        token_path: str = "data/google/token.json",
        interactive: bool = False,
    ) -> None:
        self._credentials_path = credentials_path
        self._token_path = token_path
        # Só o comando de login (calendar_setup, no host) abre o navegador. Num container, sem
        # token, o fluxo ficaria esperando um navegador que não existe: erro claro em vez disso.
        self._interactive = interactive
        self._service = None  # lazy

    def get_current_event(self) -> Optional[CalendarEvent]:
        """Retorna reunião em andamento agora (±5 min de tolerância)."""
        now = datetime.now(timezone.utc)
        window_start = (now - timedelta(minutes=5)).isoformat()
        window_end   = (now + timedelta(minutes=5)).isoformat()

        events = self._fetch_events(
            time_min=window_start,
            time_max=window_end,
            max_results=5,
        )

        meet_events = [e for e in events if e.meet_url]
        return meet_events[0] if meet_events else None

    def get_upcoming_events(self, limit: int = 5) -> list[CalendarEvent]:
        """Próximos eventos com link de reunião (Meet, Zoom ou Teams)."""
        now = datetime.now(timezone.utc).isoformat()
        events = self._fetch_events(
            time_min=now,
            max_results=limit * 2,
        )
        return [e for e in events if e.meet_url][:limit]

    def find_by_meet_url(self, meet_url: str) -> Optional[CalendarEvent]:
        """Evento cujo link é a mesma reunião (mesma normalização do bot: maiúsculas, parâmetros)."""
        key = _key(meet_url)
        if not key:
            return None

        now = datetime.now(timezone.utc)
        window_start = (now - timedelta(hours=2)).isoformat()
        window_end   = (now + timedelta(hours=24)).isoformat()

        events = self._fetch_events(
            time_min=window_start,
            time_max=window_end,
            max_results=20,
        )

        for event in events:
            if event.meet_url and _key(event.meet_url) == key:
                return event
        return None

    # Privados

    def _get_service(self):
        """Carrega credenciais e retorna o serviço da API (lazy)."""
        if self._service:
            return self._service

        try:
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from google.auth.transport.requests import Request
            from googleapiclient.discovery import build
        except ImportError:
            raise CalendarServiceError(
                "Bibliotecas do Google não instaladas.\n"
                "Execute: pip install google-auth google-auth-oauthlib google-api-python-client"
            )

        creds = None
        token_path = Path(self._token_path)

        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not Path(self._credentials_path).exists():
                    raise CalendarServiceError(
                        f"credentials.json não encontrado em '{self._credentials_path}'.\n"
                        "Baixe de: console.cloud.google.com → APIs → Credenciais"
                    )
                if not self._interactive:
                    raise CalendarServiceError(
                        "A agenda ainda não foi autorizada. No computador, uma vez: "
                        "python -m infrastructure.calendar.calendar_setup"
                    )
                flow = InstalledAppFlow.from_client_secrets_file(
                    self._credentials_path, SCOPES
                )
                creds = flow.run_local_server(port=0)

            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(creds.to_json(), encoding="utf-8")

        self._service = build("calendar", "v3", credentials=creds)
        return self._service

    def _fetch_events(
        self,
        time_min: str,
        time_max: str | None = None,
        max_results: int = 10,
    ) -> list[CalendarEvent]:
        """Busca eventos no calendário primário."""
        try:
            service = self._get_service()
            kwargs = dict(
                calendarId="primary",
                timeMin=time_min,
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
            )
            if time_max:
                kwargs["timeMax"] = time_max

            result = service.events().list(**kwargs).execute()
            items = result.get("items", [])
            return [self._parse_event(item) for item in items]

        except CalendarServiceError:
            raise
        except Exception as e:
            if "quota" in str(e).lower() or "403" in str(e):
                raise CalendarServiceError(
                    "Quota da Google Calendar API excedida. Tente novamente em alguns minutos."
                ) from e
            raise CalendarServiceError(f"Erro ao buscar eventos: {str(e)}") from e

    def _parse_event(self, item: dict) -> CalendarEvent:
        """Converte resposta da API em CalendarEvent."""
        start_raw = item.get("start", {})
        end_raw   = item.get("end", {})

        start = self._parse_dt(start_raw.get("dateTime") or start_raw.get("date", ""))
        end   = self._parse_dt(end_raw.get("dateTime") or end_raw.get("date", ""))

        attendees = [
            a.get("email", "")
            for a in item.get("attendees", [])
            if not a.get("self", False)
        ]

        meet_url = _meeting_link(item)

        return CalendarEvent(
            id=item.get("id", ""),
            title=item.get("summary", "Reunião sem título"),
            start=start,
            end=end,
            participants=attendees,
            meet_url=meet_url,
            description=item.get("description", ""),
        )

    @staticmethod
    def _parse_dt(value: str) -> datetime:
        if not value:
            return datetime.now(timezone.utc)
        try:
            if "T" in value:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            d = datetime.strptime(value, "%Y-%m-%d")
            return d.replace(tzinfo=timezone.utc)
        except ValueError:
            return datetime.now(timezone.utc)

_URL = re.compile(r"https://[^\s<>\"')]+")


def _key(url: str) -> str:
    from infrastructure.recorder.platforms import UnsupportedMeetingUrl, meeting_key
    try:
        return meeting_key(url)
    except (UnsupportedMeetingUrl, ValueError):
        return ""


def _meeting_link(item: dict) -> str:
    """Link da reunião do evento: a videochamada do Google (Meet) e, senão, o primeiro link de
    Zoom ou Teams no local ou na descrição (é onde esses convites colocam o link)."""
    for ep in item.get("conferenceData", {}).get("entryPoints", []):
        if ep.get("entryPointType") == "video" and _key(ep.get("uri", "")):
            return ep["uri"]
    if _key(item.get("hangoutLink", "")):
        return item["hangoutLink"]
    for text in (item.get("location", ""), item.get("description", "")):
        for url in _URL.findall(text or ""):
            if _key(url):
                return url
    return ""
