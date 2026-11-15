"""Google Calendar: link de Meet/Zoom/Teams no evento, casamento pelo link e login só no host."""
from datetime import datetime, timedelta, timezone

import pytest

from domain.interfaces.calendar_service import CalendarServiceError
from infrastructure.calendar.google_calendar_service import GoogleCalendarService, _meeting_link


def test_link_de_cada_plataforma():
    assert _meeting_link({"conferenceData": {"entryPoints": [
        {"entryPointType": "phone", "uri": "tel:+55"},
        {"entryPointType": "video", "uri": "https://meet.google.com/abc-defg-hij"}]}}) == "https://meet.google.com/abc-defg-hij"
    assert _meeting_link({"location": "Sala 3 / https://empresa.zoom.us/j/123456789?pwd=x"}) \
        == "https://empresa.zoom.us/j/123456789?pwd=x"
    assert _meeting_link({"description": 'Entrar: <a href="https://teams.microsoft.com/l/meetup-join/19%3a">link</a>'}) \
        == "https://teams.microsoft.com/l/meetup-join/19%3a"
    assert _meeting_link({"description": "https://discord.gg/abc e https://exemplo.com"}) == ""  # nada que o bot entre


class FakeService:
    def __init__(self, items):
        self.items = items

    def events(self):
        svc = self

        class E:
            def list(self, **kw):
                class R:
                    def execute(self_inner): return {"items": svc.items}
                return R()
        return E()


def _calendar(items):
    cal = GoogleCalendarService()
    cal._service = FakeService(items)
    return cal


def _event(title, **link):
    now = datetime.now(timezone.utc)
    return {"id": title, "summary": title, "start": {"dateTime": now.isoformat()},
            "end": {"dateTime": (now + timedelta(hours=1)).isoformat()}, **link}


def test_acha_o_evento_pelo_mesmo_link_escrito_diferente():
    cal = _calendar([_event("Daily", hangoutLink="https://meet.google.com/abc-defg-hij"),
                     _event("Zoom do cliente", location="https://zoom.us/j/987654321")])
    assert cal.find_by_meet_url("https://MEET.google.com/abc-defg-hij/?authuser=1").title == "Daily"
    assert cal.find_by_meet_url("https://zoom.us/j/987654321?pwd=abc").title == "Zoom do cliente"
    assert cal.find_by_meet_url("https://meet.google.com/zzz-zzzz-zzz") is None
    assert [e.title for e in cal.get_upcoming_events()] == ["Daily", "Zoom do cliente"]


def test_sem_token_no_container_da_erro_claro_em_vez_de_esperar_navegador(tmp_path):
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    cal = GoogleCalendarService(str(creds), str(tmp_path / "token.json"))
    with pytest.raises(CalendarServiceError, match="calendar_setup"):
        cal.get_upcoming_events()
