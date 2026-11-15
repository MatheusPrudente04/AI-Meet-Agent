import logging
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

from config.settings import get_settings

logger = logging.getLogger(__name__)

TASK_TEMPLATE = """# CRIAÇÃO DE TAREFAS

Olá, colaborador! Tudo bem?

Agente de Reuniões aqui!

Como combinado anteriormente na reunião **{meeting_title}**, realizada no dia **{meeting_date}**, seguem abaixo algumas tarefas criadas e associadas a essa reunião.

Espero que consiga realizar todas as atividades com êxito e tenha um ótimo trabalho!

---

# TAREFAS CRIADAS

## 📌 Tarefa 1

* **Nome da tarefa:** {task_name}
* **Horário de criação:** {created_at}
* **Prazo de entrega:** {deadline}

### Descrição

{description}

---

Desde já, agradeço pela dedicação de sempre e pelo excelente trabalho realizado.

Caso tenha qualquer dúvida sobre alguma das tarefas, consulte seu gestor responsável ou entre em contato comigo através da reunião definida abaixo:

🔗 **Link da reunião/chat:**
{meeting_link}

---

Atenciosamente,

**Seu Agente Especializado em Reuniões**
"""


class EmailSender:
    def __init__(self) -> None:
        self._settings = get_settings()

    def send_task_email(self, to_email: str, task_data: dict, meeting_title: str = "", meeting_date: str = "", meeting_link: str = "") -> bool:
        settings = self._settings
        if not settings.smtp_user or not settings.smtp_password:
            raise RuntimeError("SMTP não configurado. Preencha SMTP_USER e SMTP_PASSWORD no .env")

        content = TASK_TEMPLATE.format(
            meeting_title=meeting_title or "Reunião",
            meeting_date=meeting_date or datetime.now().strftime("%d/%m/%Y às %H:%M"),
            task_name=task_data.get("nome", "Sem nome"),
            created_at=task_data.get("criada_em", datetime.now().strftime("%d/%m/%Y %H:%M")),
            deadline=task_data.get("prazo", "Indefinido"),
            description=task_data.get("descricao", "Sem descrição"),
            meeting_link=meeting_link or "Não informado",
        )

        return self._send(to_email, f"📋 Tarefa criada - {task_data.get('nome', 'Nova tarefa')}", content)

    def send_tasks_email(self, to_email: str, meeting) -> bool:
        """Envia as tarefas da reunião num único e-mail."""
        tasks = meeting.summary.tasks if meeting.summary else []
        if not tasks:
            raise RuntimeError("A reunião não tem tarefas para enviar.")
        lines = [
            f"Tarefas da reunião \"{meeting.title}\" ({meeting.started_at:%d/%m/%Y %H:%M}):",
            "",
            *(f"{'[x]' if t.done else '[ ]'} {t.description} — responsável: {t.responsible}"
              f"{f' — prazo: {t.deadline}' if t.deadline else ''}" for t in tasks),
            "",
            "Enviado pelo Meet Agent.",
        ]
        return self._send(to_email, f"Tarefas — {meeting.title}", "\n".join(lines))

    def send_password_reset(self, to_email: str, name: str, code: str, minutes: int) -> bool:
        body = (
            f"Olá, {name}.\n\n"
            f"Seu código para criar uma nova senha no Meet Agent é: {code}\n\n"
            f"Ele vale por {minutes} minutos e só pode ser usado uma vez.\n"
            "Se não foi você que pediu, ignore este e-mail: sua senha continua a mesma.\n"
        )
        return self._send(to_email, "Meet Agent — código para nova senha", body)

    def _send(self, to_email: str, subject: str, content: str) -> bool:
        settings = self._settings
        if not settings.email_enabled:
            raise RuntimeError("SMTP não configurado. Preencha SMTP_USER e SMTP_PASSWORD no .env")

        msg = MIMEMultipart()
        msg["From"] = settings.smtp_from_email or settings.smtp_user
        msg["To"] = to_email
        msg["Subject"] = subject

        msg.attach(MIMEText(content, "plain", "utf-8"))

        try:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as server:
                server.starttls(context=ssl.create_default_context())
                # Só PLAIN: o login() padrão tenta outro método depois de uma recusa, o Gmail já
                # fechou a conexão, e o erro real (535) vira "Connection unexpectedly closed".
                server.user, server.password = settings.smtp_user, settings.smtp_password
                server.auth("PLAIN", server.auth_plain)
                server.sendmail(msg["From"], msg["To"], msg.as_string())
        except smtplib.SMTPAuthenticationError as exc:
            logger.error("SMTP recusou o login de %s em %s: %s", settings.smtp_user, settings.smtp_host, exc.smtp_code)
            raise RuntimeError(
                "O servidor de e-mail recusou o usuário e a senha do SMTP (SMTP_USER/SMTP_PASSWORD no .env). "
                "No Gmail, use uma senha de app da mesma conta de SMTP_USER, com verificação em duas etapas ligada."
            ) from exc
        except (smtplib.SMTPException, OSError) as exc:
            logger.error("Falha no envio por SMTP (%s:%s): %s", settings.smtp_host, settings.smtp_port, exc)
            raise RuntimeError(f"Não foi possível falar com o servidor de e-mail: {exc}") from exc

        return True
