"""Alerta quando /health falha (S11-4). Para rodar pelo cron do servidor, a cada 5 minutos:

    */5 * * * * cd /srv/meet-agent && .venv/bin/python -m scripts.health_alert https://api.exemplo.com/health

Manda e-mail para ALERT_EMAIL (pelo SMTP do .env) só quando o estado muda: um quando cai, outro
quando volta. O último estado fica em data/health_alert.state. Sai com código 1 enquanto
estiver fora, para quem preferir monitorar pelo código de saída.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

STATE = Path("data/health_alert.state")


def check(url: str) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return True, r.read().decode()
    except urllib.error.HTTPError as exc:  # 503 traz no corpo o que falhou
        return False, f"HTTP {exc.code}: {exc.read().decode(errors='replace')}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def run(url: str, state: Path = STATE, send=None) -> bool:
    ok, detail = check(url)
    previous = state.read_text().strip() if state.exists() else "ok"
    current = "ok" if ok else "fora"
    if current != previous:
        subject = "API de volta" if ok else "API fora do ar"
        body = f"{datetime.now():%d/%m/%Y %H:%M} — {url}\n\n{_pretty(detail)}\n"
        if send is None:
            from config.settings import get_settings
            from infrastructure.email.email_sender import EmailSender
            to = get_settings().alert_email
            if not to:
                raise SystemExit("ALERT_EMAIL não configurado no .env")
            send = lambda s, b: EmailSender().send_alert(to, s, b)  # noqa: E731
        send(subject, body)  # falhou o envio: o estado não muda e tenta de novo na próxima
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(current)
    return ok


def _pretty(detail: str) -> str:
    try:
        return json.dumps(json.loads(detail.split(": ", 1)[-1]), ensure_ascii=False, indent=2)
    except ValueError:
        return detail


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("uso: python -m scripts.health_alert <url do /health>")
    sys.exit(0 if run(sys.argv[1]) else 1)
