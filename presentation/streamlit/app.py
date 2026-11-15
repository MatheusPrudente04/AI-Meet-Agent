"""
presentation/streamlit/app.py

Modo solo  : upload direto → processa na hora → dashboard.
Modo collab: envia o bot para a reunião (processo separado) → a reunião aparece
             na lista com o status do processamento.

Telas:
  - 📋 Reuniões   → dashboard da reunião (sem chat integrado)
  - 💬 Chat       → chat interativo com o agente (LangGraph + contrato)
"""

import sys, uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import streamlit as st
import streamlit.components.v1 as components
from config.settings import get_settings
from config.logging_setup import setup_logging
from presentation.container import BotNotReady, MeetingInProgress, get_container
from domain.entities.meeting import Meeting, MeetingStatus, Summary
from use_cases.process_meeting import ProcessMeetingInput
from use_cases.chat_with_meeting import ChatWithMeetingInput

settings = get_settings()
setup_logging(service="streamlit")
AUDIO_DIR = Path(settings.audio_storage_path)

@st.cache_resource
def get_app_container():
    return get_container()

container = get_app_container()

st.set_page_config(page_title="Meet Agent", page_icon="🎤", layout="wide")

_SECTION_EMOJI = {
    "went_well": "✅", "to_improve": "🔧", "action_items": "📌",
    "risks": "⚠️", "delivered": "📦", "not_delivered": "⏳",
    "stakeholder_feedback": "💬", "feedbacks": "🔄",
    "strengths": "💪", "concerns": "🚩", "recommendation": "🎯",
    "ideas": "💡",
}

_PAGES = {
    "📋 Reuniões": "meetings",
    "💬 Chat Interativo": "chat",
}
if settings.is_collab:
    _PAGES["👤 Minha conta"] = "account"

def _init():
    defaults = {
        "user": None,
        "token": "",
        "cookie": None,  # token a gravar (ou "" para apagar) no cookie na próxima execução
        "page": "meetings",
        "meeting": None,
        "mode_selected": False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init()


COOKIE = "meet_agent_session"


def write_cookie() -> None:
    """Grava ou apaga o cookie da sessão, para o login sobreviver ao F5 (S7-1).
    Feito numa execução sem st.rerun, senão o script não chega ao navegador."""
    token = st.session_state.cookie
    if token is None:
        return
    st.session_state.cookie = None
    max_age = settings.session_days * 86400 if token else 0
    # ponytail: cookie gravado por JS, então sem HttpOnly (o Streamlit não deixa o servidor
    # mandar Set-Cookie). Secure entra sozinho quando a página vem por HTTPS (S11-2).
    st.html(
        f"<script>document.cookie = '{COOKIE}={token}; Max-Age={max_age}; Path=/; SameSite=Strict'"
        f" + ((window.top || window).location.protocol === 'https:' ? '; Secure' : '');</script>",
        unsafe_allow_javascript=True,
    )


def start_session(token: str, user) -> None:
    st.session_state.token, st.session_state.user, st.session_state.cookie = token, user, token


def end_session() -> None:
    st.session_state.token, st.session_state.user, st.session_state.meeting = "", None, None
    st.session_state.cookie = ""
    st.query_params.clear()


def _client_ip() -> str:
    try:
        return st.context.ip_address or ""
    except Exception:
        return ""


def login_screen() -> None:
    """Entrar, criar conta ou recuperar a senha (ADR-011). Só em collab."""
    st.title("🎤 Meet Agent")
    from use_cases.auth import MIN_PASSWORD, AuthError
    tab_login, tab_signup, tab_forgot = st.tabs(["Entrar", "Criar conta", "Esqueci a senha"])
    with tab_login, st.form("login"):
        email = st.text_input("E-mail")
        password = st.text_input("Senha", type="password")
        if st.form_submit_button("Entrar", type="primary", use_container_width=True):
            try:
                start_session(*container.auth.login(email, password, "streamlit", _client_ip()))
                st.rerun()
            except AuthError as e:
                st.error(str(e))
    with tab_forgot:
        if not settings.email_enabled:
            st.info("Recuperação por e-mail indisponível: peça a quem administra o sistema.")
        else:
            with st.form("forgot"):
                st.caption("1. Receba um código no seu e-mail (vale 15 minutos).")
                f_email = st.text_input("E-mail", key="forgot_email")
                if st.form_submit_button("Enviar código", use_container_width=True):
                    try:
                        container.send_password_reset(f_email)
                        st.success("Se o e-mail tiver conta, o código foi enviado.")
                    except Exception as e:
                        st.error(f"Não foi possível enviar: {e}")
            with st.form("reset"):
                st.caption("2. Crie a nova senha com o código.")
                r_email = st.text_input("E-mail", key="reset_email")
                code = st.text_input("Código")
                new_pw = st.text_input(f"Nova senha (mínimo de {MIN_PASSWORD} caracteres)", type="password", key="reset_pw")
                if st.form_submit_button("Criar nova senha", type="primary", use_container_width=True):
                    try:
                        container.auth.reset_password(r_email, code, new_pw)
                        st.success("Senha alterada. Entre com a nova senha na aba Entrar.")
                    except AuthError as e:
                        st.error(str(e))
    with tab_signup:
        if not settings.allow_registration:
            st.info("O cadastro de novos usuários está fechado. Peça acesso a quem administra o sistema.")
        else:
            with st.form("signup"):
                name = st.text_input("Nome")
                email = st.text_input("E-mail", key="signup_email")
                password = st.text_input(f"Senha (mínimo de {MIN_PASSWORD} caracteres)", type="password", key="signup_pw")
                confirm = st.text_input("Repita a senha", type="password")
                if st.form_submit_button("Criar conta", type="primary", use_container_width=True):
                    if password != confirm:
                        st.error("As senhas não conferem.")
                    else:
                        try:
                            user = container.register_user(name, email, password)
                            start_session(container.auth.open_session(user, "streamlit", _client_ip()), user)
                            st.rerun()
                        except AuthError as e:
                            st.error(str(e))


if container.auth is not None:
    write_cookie()
    # Página recarregada: a sessão volta pelo cookie.
    if not st.session_state.token:
        st.session_state.token = st.context.cookies.get(COOKIE, "")
    # A sessão vive no banco: encerrada ou vencida, volta para o login.
    st.session_state.user = container.auth.user_for_token(st.session_state.token)
    if st.session_state.user is None:
        if st.session_state.token:
            end_session()
            write_cookie()
        login_screen()
        st.stop()

# Dono das reuniões desta sessão. None (solo) = sem filtro.
OWNER = st.session_state.user.id if st.session_state.user else None


def my_meetings() -> list:
    return container.meetings_of(OWNER)


def select_meeting(meeting) -> None:
    """Abre (ou fecha, com None) uma reunião. O id vai para a URL: se a sessão do
    navegador cair — aba em segundo plano durante a reunião, F5 — a tela volta sozinha."""
    st.session_state.meeting = meeting
    if meeting is None:
        st.query_params.pop("m", None)
    else:
        st.query_params["m"] = meeting.id


if st.session_state.meeting is None and "m" in st.query_params:
    st.session_state.meeting = container.meeting_for(st.query_params["m"], OWNER)


_STATUS_LABEL = {
    MeetingStatus.PENDING: "Na fila", MeetingStatus.RECORDING: "Bot gravando a reunião",
    MeetingStatus.TRANSCRIBING: "Transcrevendo o áudio", MeetingStatus.SUMMARIZING: "Gerando o resumo",
}


@st.fragment(run_every="3s")
def bot_setup_progress() -> None:
    """Credenciamento em andamento: relê o estado a cada 3 s e recarrega a página ao terminar."""
    bot = container.bot_status()
    if bot["status"] == "creating":
        st.info(f"⏳ **Criando bot...** {bot['message']}")
    elif bot["status"] == "waiting_login":
        st.warning(f"🔐 {bot['message']}. Esta é a tela do navegador do bot: entre na conta do Google dele.")
        components.iframe(bot["vnc_url"], height=560)
    else:
        st.rerun()  # terminou (ou falhou): a página inteira mostra o resultado e libera o envio


def bot_credential_panel() -> bool:
    """Estado da conta do bot. True = credenciado, pode enviar o bot."""
    if container.bot_account is None:  # solo: sem fila nem credenciamento
        return True
    bot = container.bot_status()
    status = bot["status"]
    is_admin = bool(st.session_state.get("user") and st.session_state.user.is_admin)
    if status == "ready":
        col_ok, col_again = st.columns([3, 1])
        col_ok.success("✅ Bot credenciado")
        if is_admin and col_again.button("Reconectar", help="Abre o login da conta do bot de novo"):
            container.request_bot_setup()
            st.rerun()
        return True
    if status in ("creating", "waiting_login"):
        bot_setup_progress()
        return False
    if status == "error":
        st.error(bot["message"])
    else:
        st.info("O bot ainda não está conectado a uma conta do Google. Credencie para começar a enviá-lo "
                "às reuniões (Google Meet, Zoom e Teams).")
    if is_admin:
        if st.button("🤖 Credenciar bot", type="primary", use_container_width=True):
            container.request_bot_setup()
            st.rerun()
    else:
        st.caption("Peça ao administrador para credenciar o bot.")
    return False


def render_chat_action(meeting, action: dict) -> None:
    """E-mail preparado pelo agente (confirmar ou descartar) ou documento gerado (baixar)."""
    key = action["id"]
    if action["type"] == "memory":
        st.caption("🧠 Guardado na sua memória (Minha conta → Memória do agente)")
        return
    if action["type"] == "document":
        try:
            path, _ = container.document_file(meeting, key)
        except LookupError:
            st.caption("📄 Documento não está mais disponível.")
            return
        st.download_button(f"📄 Baixar {action['filename']}", data=path.read_bytes(),
                           file_name=action["filename"], key=f"doc_{key}")
        return
    status = action["status"]
    if status == "sent":
        st.caption(f"✅ E-mail enviado para {', '.join(action['to'])}")
        return
    if status == "canceled":
        st.caption("🗑️ E-mail descartado")
        return
    with st.container(border=True):
        st.markdown(f"**Para:** {', '.join(action['to'])}  \n**Assunto:** {action['subject']}")
        st.text(action["body"])
        col_send, col_drop, _ = st.columns([1, 1, 3])
        for col, label, send in ((col_send, "📧 Enviar", True), (col_drop, "Descartar", False)):
            if col.button(label, key=f"{'send' if send else 'drop'}_{key}", use_container_width=True,
                          type="primary" if send else "secondary"):
                try:
                    container.resolve_email_action(meeting, key, send)
                    st.rerun()
                except (RuntimeError, ValueError, LookupError) as exc:
                    st.error(str(exc))


_ACTIVE = (MeetingStatus.PENDING, MeetingStatus.RECORDING,
           MeetingStatus.TRANSCRIBING, MeetingStatus.SUMMARIZING)


def _progress_label(m) -> str:
    """"Gerando o resumo — 3º na fila", quando a reunião espera um worker (ADR-015)."""
    label = _STATUS_LABEL.get(m.status, m.status.value)
    position = container.queue_position(m)
    return f"{label} — {position}º na fila" if position else label


@st.fragment(run_every="5s")
def watch_progress(meeting_id: str, shown_status: MeetingStatus) -> None:
    """Relê o status a cada 5 s. A cada mudança recarrega a página inteira,
    para a barra lateral e o conteúdo acompanharem."""
    current = container.meeting_for(meeting_id, OWNER)
    if current is None:
        return
    if current.status != shown_status:
        st.session_state.meeting = current
        st.rerun()
    st.info(f"{_STATUS_ICON[current.status]} {_progress_label(current)}…")


@st.fragment(run_every="5s")
def active_meetings_panel() -> None:
    """Reuniões em andamento, visíveis mesmo sem nenhuma reunião aberta."""
    active = [m for m in my_meetings() if m.status in _ACTIVE]
    snapshot = [(m.id, m.status) for m in active]
    if st.session_state.get("_active_snapshot", snapshot) != snapshot:
        st.session_state._active_snapshot = snapshot
        st.rerun()  # algo mudou: atualiza também a barra lateral
    st.session_state._active_snapshot = snapshot
    if not active:
        return
    st.markdown("### ⏱ Em andamento")
    for m in active:
        col_txt, col_btn = st.columns([4, 1])
        col_txt.info(f"{_STATUS_ICON[m.status]} **{m.title}** — {_progress_label(m)}…")
        if col_btn.button("Abrir", key=f"open_active_{m.id}", use_container_width=True):
            select_meeting(m)
            st.rerun()
    st.divider()


_STATUS_ICON = {
    MeetingStatus.PENDING: "⏳", MeetingStatus.RECORDING: "🔴",
    MeetingStatus.TRANSCRIBING: "🎤", MeetingStatus.SUMMARIZING: "📝",
    MeetingStatus.DONE: "✅", MeetingStatus.ERROR: "❌",
}

_SECTION_LABELS = {
    "went_well": "O que foi bem", "to_improve": "O que melhorar",
    "action_items": "Ações", "risks": "Riscos",
    "delivered": "Entregue", "not_delivered": "Não entregue",
    "stakeholder_feedback": "Feedback", "feedbacks": "Feedbacks",
    "strengths": "Pontos Fortes", "concerns": "Pontos de Atenção",
    "recommendation": "Recomendação", "ideas": "Ideias",
}

def render_extra_sections(summary) -> None:
    if not summary or not getattr(summary, "extra_sections", None):
        return
    for key, value in summary.extra_sections.items():
        emoji = _SECTION_EMOJI.get(key, "")
        label = _SECTION_LABELS.get(key, key.replace("_", " ").title())
        st.markdown(f"### {emoji} {label}")
        if isinstance(value, str):
            st.write(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    # action_items, feedbacks, ideas — dicts estruturados
                    st.markdown("- " + " — ".join(
                        f"**{k}**: {v}" for k, v in item.items()))
                else:
                    st.markdown(f"- {item}")
        else:
            st.write(value)


with st.sidebar:
    st.title("🎤 Meet Agent")
    badge = "Solo" if settings.is_solo else "Colaborativo"
    st.caption(f"Modo: **{badge}**")
    if st.session_state.user:
        st.caption(f"👤 {st.session_state.user.name}")
        if st.button("Sair", use_container_width=True):
            container.auth.logout(st.session_state.token)
            end_session()
            st.rerun()
    st.divider()

    if settings.is_collab and st.session_state.user and st.session_state.user.is_admin:
        _PAGES["📈 Observabilidade"] = "observability"
    if st.session_state.page not in _PAGES.values():
        st.session_state.page = "meetings"
    page_label = st.radio(
        "Navegação",
        options=list(_PAGES.keys()),
        index=list(_PAGES.values()).index(st.session_state.page),
        label_visibility="collapsed",
    )
    st.session_state.page = _PAGES[page_label]
    st.divider()

    if st.session_state.page == "meetings":
        st.subheader("Reuniões salvas")

        if st.button("🔄 Atualizar", use_container_width=True, type="secondary"):
            st.rerun()

        try:
            all_meetings = my_meetings()
        except Exception as e:
            st.error(f"Erro ao carregar reuniões: {str(e)}")
            all_meetings = []

        for m in all_meetings:
            label = m.title[:30] + ("..." if len(m.title) > 30 else "")
            if st.button(f"{_STATUS_ICON[m.status]} {label}", key=f"load_{m.id}", use_container_width=True):
                select_meeting(m)
                st.rerun()

        if st.session_state.meeting:
            st.divider()
            if st.button("✖ Fechar reunião", use_container_width=True):
                select_meeting(None)
                st.rerun()


if st.session_state.page == "meetings":

    st.title("🎤 Meet Agent")

    if st.session_state.meeting is None:
        active_meetings_panel()
        if settings.is_solo:
            st.subheader("Nova reunião")
            title = st.text_input("Título", placeholder="Sprint Planning — Semana 22")
            audio_file = st.file_uploader(
                "Arquivo de áudio",
                type=["wav", "mp3", "m4a", "webm", "mkv", "ogg"],
            )
            use_diarization = st.toggle("Identificar speakers", value=True)

            if st.button("🚀 Processar", disabled=not (title and audio_file), type="primary"):
                if not audio_file:
                    st.error("Selecione um arquivo de áudio")
                    st.stop()

                audio_dir = Path(settings.audio_storage_path)
                audio_dir.mkdir(parents=True, exist_ok=True)
                suffix = Path(audio_file.name).suffix
                audio_path = str(audio_dir / f"{uuid.uuid4()}{suffix}")
                with open(audio_path, "wb") as f:
                    f.write(audio_file.read())

                with st.spinner("Transcrevendo e gerando resumo..."):
                    meeting = container.process_meeting.execute(
                        ProcessMeetingInput(
                            meeting_id=str(uuid.uuid4()), audio_path=audio_path,
                            title=title, with_diarization=use_diarization,
                        )
                    )
                if meeting.status == MeetingStatus.ERROR:
                    st.error(meeting.error)
                    st.stop()

                select_meeting(meeting)
                st.rerun()

        else:
            st.subheader("Enviar bot para a reunião")

            col_form, col_status = st.columns([1, 1], gap="large")

            with col_form:
                st.markdown("### 🤖 Enviar bot")
                bot_ready = bot_credential_panel()
                meet_url = st.text_input(
                    "Link da reunião",
                    placeholder="https://meet.google.com/xxx-yyyy-zzz",
                    help="Google Meet. Teams e Zoom são aceitos, mas ainda não foram validados em reunião real.",
                )
                meeting_title = st.text_input(
                    "Título da reunião",
                    placeholder="Sprint Planning — Semana 22",
                )

                if st.button(
                    "🚀 Enviar bot para a reunião",
                    # Com a agenda ligada o título pode vir do evento.
                    disabled=not (bot_ready and meet_url and (meeting_title or settings.enable_calendar)),
                    type="primary",
                    use_container_width=True,
                ):
                    try:
                        new_id = container.dispatch_bot(meet_url.strip(), meeting_title, owner_id=OWNER or "")
                    except MeetingInProgress as e:
                        st.warning(str(e))
                        select_meeting(container.meeting_for(e.meeting_id, OWNER))
                        st.rerun()
                    except (BotNotReady, ValueError) as e:
                        st.error(str(e))
                    else:
                        select_meeting(container.meeting_for(new_id, OWNER))
                        st.rerun()

            with col_status:
                if settings.enable_calendar:
                    st.markdown("### 📅 Próximas reuniões da agenda")
                    events, cal_error = container.upcoming_events()
                    if cal_error:
                        st.warning(cal_error)
                    elif not events:
                        st.caption("Nenhuma reunião com link (Meet, Zoom ou Teams) nas próximas da agenda.")
                    for ev in events:
                        col_ev, col_go = st.columns([3, 1])
                        col_ev.markdown(f"**{ev.title}**  \n{ev.start:%d/%m %H:%M}")
                        if col_go.button("🤖 Mandar", key=f"cal_{ev.id}", disabled=not bot_ready,
                                         use_container_width=True):
                            try:
                                new_id = container.dispatch_bot(ev.meet_url, ev.title, owner_id=OWNER or "")
                            except MeetingInProgress as e:
                                select_meeting(container.meeting_for(e.meeting_id, OWNER))
                            except (BotNotReady, ValueError) as e:
                                st.error(str(e))
                                st.stop()
                            else:
                                select_meeting(container.meeting_for(new_id, OWNER))
                            st.rerun()
                    st.divider()
                st.markdown("### 📋 Como funciona")
                st.markdown("""
                1. Cole o link do Google Meet
                2. Clique em **Enviar bot**
                3. O **Meet Agent 🤖** entra na reunião como participante
                4. O bot grava o áudio da reunião enquanto estiver ativo
                5. Quando a reunião terminar, ele sai automaticamente
                6. O áudio é transcrito e o resumo fica disponível aqui

                **Nota:** use uma conta Google só para o bot. O administrador a conecta uma vez
                pelo botão **Credenciar bot**.
                """)

    else:
        meeting: Meeting = st.session_state.meeting
        if meeting is None:
            st.warning("Reunião não selecionada.")
            st.stop()
        summary = meeting.summary

        col_title, col_delete = st.columns([5, 1])
        col_title.subheader(f"📋 {meeting.title}")
        with col_delete.popover("🗑️ Apagar", use_container_width=True):
            if meeting.status in _ACTIVE:
                st.caption("A reunião ainda está em andamento. Espere terminar para apagar.")
            else:
                st.markdown("Apaga a reunião, o resumo, a transcrição, a conversa e os arquivos gerados. "
                            "**Não dá para desfazer.**")
                if st.button("Apagar definitivamente", type="primary", key=f"del_{meeting.id}",
                             use_container_width=True):
                    try:
                        container.delete_meeting(meeting)
                    except MeetingInProgress:
                        st.error("A reunião voltou a ficar em andamento. Tente quando terminar.")
                    else:
                        select_meeting(None)
                        st.toast(f"Reunião \"{meeting.title}\" apagada.")
                        st.rerun()
        date_str = meeting.started_at.strftime("%d de %B de %Y às %H:%M").lstrip("0")
        st.caption(date_str)

        if meeting.status == MeetingStatus.ERROR:
            st.error(f"Falha: {meeting.error}")
            if meeting.audio_path and Path(meeting.audio_path).exists():
                if st.button("🔁 Reprocessar áudio gravado"):
                    container.enqueue_processing(meeting.id, meeting.audio_path, meeting.title)
                    select_meeting(container.meeting_for(meeting.id, OWNER))
                    st.rerun()
        elif meeting.status != MeetingStatus.DONE:
            watch_progress(meeting.id, meeting.status)

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("⏱ Duração", f"{meeting.duration_minutes:.0f} min")
        c2.metric("👥 Participantes", len(meeting.participants) or "—")
        c3.metric("✅ Tarefas", len(summary.tasks) if summary else 0)
        c4.metric("🔑 Decisões", len(summary.decisions) if summary else 0)

        st.divider()

        if summary:
            st.markdown("### Visão Geral")
            st.write(summary.overview)

            if summary.topics:
                st.markdown("### Tópicos")
                for t in summary.topics:
                    st.markdown(f"- {t}")

            if summary.tasks:
                st.markdown("### Tarefas")
                for idx, task in enumerate(summary.tasks):
                    key = f"task_{meeting.id}_{idx}"
                    st.checkbox(
                        f"**{task.description}**  \n*{task.responsible}* · {task.deadline}",
                        value=task.done,
                        key=key,
                        on_change=lambda i=idx, k=key: container.set_task_done(meeting.id, i, st.session_state[k]),
                    )
                if settings.email_enabled:
                    with st.form(f"email_{meeting.id}", border=False):
                        to = st.text_input("Enviar tarefas por e-mail para", placeholder="pessoa@empresa.com")
                        if st.form_submit_button("✉️ Enviar tarefas") and to:
                            try:
                                from infrastructure.email.email_sender import EmailSender
                                EmailSender().send_tasks_email(to.strip(), meeting)
                                st.success(f"Tarefas enviadas para {to}.")
                            except Exception as e:
                                st.error(f"Não foi possível enviar: {e}")

            if summary.decisions:
                st.markdown("### Decisões")
                _DECISION_ICON = {"aprovada": "✅", "condicionada": "⚖️", "proposta": "💡", "pendente": "⏳",
                                  "rejeitada": "❌", "inconclusiva": "❔"}
                for dec in summary.decisions:
                    label = f"{_DECISION_ICON.get(dec.status, '•')} {dec.description}" + (f" ({dec.status})" if dec.status else "")
                    with st.expander(label):
                        st.write(dec.context or "Sem contexto adicional.")

            render_extra_sections(summary)

        st.divider()
        if meeting.is_transcribed:
            from infrastructure.exporters.markdown_exporter import MarkdownExporter
            st.download_button(
                "⬇️ Exportar reunião (.md)",
                data=MarkdownExporter().render(meeting),
                file_name=f"{meeting.title}.md",
                mime="text/markdown",
            )
        with st.expander("📄 Transcrição completa"):
            st.text(meeting.transcript_formatted or meeting.transcript_text)
            st.download_button(
                "⬇️ Baixar .txt",
                data=meeting.transcript_formatted or meeting.transcript_text,
                file_name=f"{meeting.title}_transcricao.txt",
            )

elif st.session_state.page == "chat":

    st.title("💬 Chat Interativo com IA")
    st.caption("Agente LangGraph · contrato MEETING-INTELLIGENCE-001")

    try:
        meetings = my_meetings()
    except Exception as e:
        st.error(f"Erro ao carregar reuniões: {str(e)}")
        meetings = []

    transcribed = [m for m in meetings if m.is_transcribed]

    if not transcribed:
        st.info("Nenhuma reunião transcrita disponível para chat.")
        st.stop()

    meeting_options = {f"{m.title[:50]} — {m.id[:8]}...": m for m in transcribed}
    selected_label = st.selectbox(
        "Selecione uma reunião para conversar:",
        options=list(meeting_options.keys()),
        index=0,
    )
    chat_meeting = meeting_options[selected_label]

    # A conversa fica guardada na reunião: sobrevive a recarregar a página.
    chat_history = chat_meeting.chat_history

    st.markdown("---")
    col_meta, _, col_clear = st.columns([2, 2, 1])
    with col_clear.popover("🗑️ Limpar conversa", use_container_width=True, disabled=not chat_history):
        st.markdown("Apaga a conversa desta reunião, com os documentos gerados nela e os e-mails não "
                    "enviados. O resumo e a transcrição ficam.")
        if st.button("Limpar", type="primary", key=f"clear_{chat_meeting.id}", use_container_width=True):
            container.clear_chat(chat_meeting)
            st.toast("Conversa apagada.")
            st.rerun()
    with col_meta:
        st.markdown(f"**{chat_meeting.title}**")
        st.caption(
            f"{len(chat_meeting.participants)} participantes · "
            f"{chat_meeting.duration_minutes:.0f} min · "
            f"{chat_meeting.started_at.strftime('%d/%m/%Y %H:%M')}"
        )

    st.divider()

    chat_container = st.container(height=500, border=True)
    with chat_container:
        if not chat_history:
            st.caption("Faça uma pergunta sobre esta reunião.")
        for msg in chat_history:
            with st.chat_message(msg["role"]):
                st.write(msg["content"])
                if msg.get("action"):
                    render_chat_action(chat_meeting, msg["action"])

    if prompt := st.chat_input("Ex: Quais foram as principais decisões?"):
        with chat_container:
            with st.chat_message("user"):
                st.write(prompt)
            with st.chat_message("assistant"):
                with st.spinner("Pensando..."):
                    try:
                        result = container.chat_with_meeting.execute(
                            ChatWithMeetingInput(meeting=chat_meeting, question=prompt)
                        )
                        answer = result.answer if result.success else f"⚠️ {result.error_message}"
                    except Exception as e:
                        answer, result = f"⚠️ Erro: {e}", None
                st.write(answer)
        if result is not None and result.action:
            st.rerun()  # redesenha pela conversa guardada, já com os botões da ação

elif st.session_state.page == "observability":
    from api.routers import observability as obs_api
    from api.security import Principal
    from infrastructure import observability as obs

    st.title("📈 Observabilidade")
    st.caption("Métricas e logs de todos os serviços (API, workers, bot, interface), guardados no Redis.")
    if st.button("🔄 Atualizar"):
        st.rerun()
    data = obs_api.summary(Principal())

    w = data["workers"]
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Transcrição", "🟢 no ar" if w.get("worker_transcribe") == "ok" else "🔴 sem worker",
              f"{data['queues'].get('transcribe', 0)} na fila", delta_color="off")
    c2.metric("Resumo (LLM)", "🟢 no ar" if w.get("worker_llm") == "ok" else "🔴 sem worker",
              f"{data['queues'].get('llm', 0)} na fila", delta_color="off")
    c3.metric("Bot", "🟢 " + data["bot"]["status"] if w.get("bot") == "ok" else "🔴 sem worker",
              f"{data['queues'].get('bot', 0)} na fila", delta_color="off")
    c4.metric("LLM", data["llm"].get("status", "?"), data["llm"].get("model", ""), delta_color="off")
    c5.metric("Disco livre", f"{data['disk']['free_gb']} GB", f"{data['disk']['used_pct']}% usado", delta_color="off")

    c1, c2, c3, c4 = st.columns(4)
    by_status = data["meetings_by_status"]
    c1.metric("Reuniões", sum(by_status.values()), f"{by_status.get('error', 0)} com erro", delta_color="off")
    c2.metric("Usuários", data["accounts"].get("users", "—"),
              f"{data['accounts'].get('active_sessions', 0)} sessões abertas", delta_color="off")
    http = data["http"]
    c3.metric("Pedidos à API", int(sum(http.values())), f"{int(http.get('5xx', 0))} erros 5xx", delta_color="off")
    emails = data["emails"]
    c4.metric("E-mails", int(sum(emails.values())),
              f"{int(sum(data['email_errors'].values()))} falharam", delta_color="off")

    st.subheader("Tempos (últimas amostras)")
    names = {"pipeline_stage_seconds": "Etapa", "llm_seconds": "LLM", "http_request_seconds": "API",
             "meeting_audio_minutes": "Áudio (min)"}
    rows = []
    for key, v in sorted(data["latencies_seconds"].items()):
        name, labels = obs.parse_key(key)
        if name == "http_request_seconds" and v["count"] < 5:
            continue  # rota quase sem uso: ruído
        rows.append({"Medida": names.get(name, name), "Detalhe": ", ".join(f"{k}={x}" for k, x in labels.items()),
                     "Amostras": v["count"], "p50 (s)": v["p50"], "p95 (s)": v["p95"], "Máx (s)": v["max"]})
    st.dataframe(rows, hide_index=True, use_container_width=True) if rows else st.caption("Sem amostras ainda.")

    col_a, col_b, col_c = st.columns(3)
    col_a.markdown("**Pipeline**")
    col_a.json(data["pipeline_by_stage"], expanded=False)
    col_b.markdown("**Documentos gerados**")
    col_b.json(data["documents"], expanded=False)
    col_c.markdown("**Bot (gravações)**")
    col_c.json(data["bot_meetings"], expanded=False)

    st.subheader("Logs")
    f1, f2, f3, f4 = st.columns([2, 2, 1, 2])
    meeting_filter = f1.text_input("Reunião (id)")
    service_filter = f2.selectbox("Serviço", ["", "api", "worker-transcribe", "worker-llm", "bot", "streamlit"])
    level_filter = f3.selectbox("Nível mínimo", ["", "INFO", "WARNING", "ERROR"])
    text_filter = f4.text_input("Texto")
    entries = obs.logs(limit=300, meeting_id=meeting_filter.strip(), service=service_filter, level=level_filter,
                       text=text_filter)
    if entries:
        st.dataframe([{"Hora": datetime.fromtimestamp(float(e["ts"])).strftime("%d/%m %H:%M:%S"),
                       "Serviço": e["service"], "Nível": e["level"], "Reunião": e["meeting_id"][:8],
                       "Mensagem": e["message"][:300]} for e in entries],
                     hide_index=True, use_container_width=True, height=420)
    else:
        st.caption("Nenhuma linha com esses filtros.")

elif st.session_state.page == "account":
    from datetime import datetime as _dt
    from use_cases.auth import MIN_PASSWORD, AuthError, session_id
    user = st.session_state.user
    st.title("👤 Minha conta")
    st.caption(f"{user.name} · {user.email}" + (" · administrador" if user.is_admin else ""))

    st.subheader("🧠 Memória do agente")
    st.caption("O que você pediu para o agente lembrar no chat. Vale em todas as suas reuniões. "
               "Além disso, ele consulta as decisões e tarefas abertas das suas outras reuniões.")
    notes = container.memory.list(user.id)
    if not notes:
        st.caption("Nenhuma nota. No chat, peça por exemplo: \"lembre que prefiro atas em tópicos\".")
    for n in notes:
        col_note, col_del = st.columns([5, 1])
        col_note.write(f"{n['text']}  \n*{n['created_at']:%d/%m/%Y %H:%M}*")
        if col_del.button("Apagar", key=f"note_{n['_id']}", use_container_width=True):
            container.memory.delete(user.id, n["_id"])
            st.rerun()
    st.divider()

    st.subheader("Sessões abertas")
    st.caption("Cada navegador ou aparelho em que você entrou. Encerrar uma sessão pede login de novo nela.")
    current = session_id(st.session_state.token)
    sessions = container.auth.sessions(user)
    for s in sessions:
        col_info, col_btn = st.columns([5, 1])
        agent = (s.get("user_agent") or "desconhecido")[:60]
        col_info.write(f"{'🟢 **Esta sessão** · ' if s['_id'] == current else ''}{agent} · IP {s.get('ip') or '?'} · "
                       f"desde {s['created_at']:%d/%m/%Y %H:%M} UTC")
        if s["_id"] != current and col_btn.button("Encerrar", key=f"end_{s['_id']}", use_container_width=True):
            container.auth.end_session(user, s["_id"])
            st.rerun()
    others = len(sessions) - 1
    if others > 0:
        confirm_others = st.checkbox(f"Confirmo: encerrar as outras {others} sessões")
        if st.button("Encerrar todas as outras sessões", disabled=not confirm_others):
            n = container.auth.end_other_sessions(user, st.session_state.token)
            st.success(f"{n} sessões encerradas. Esta continua aberta.")
            st.rerun()

    st.subheader("Trocar senha")
    with st.form("change_password", clear_on_submit=True):
        cur = st.text_input("Senha atual", type="password")
        new = st.text_input(f"Nova senha (mínimo de {MIN_PASSWORD} caracteres)", type="password")
        again = st.text_input("Repita a nova senha", type="password")
        if st.form_submit_button("Trocar senha", type="primary"):
            if new != again:
                st.error("As senhas não conferem.")
            else:
                try:
                    n = container.auth.change_password(user, cur, new, st.session_state.token)
                    st.success(f"Senha trocada. {n} outras sessões foram encerradas.")
                except AuthError as e:
                    st.error(str(e))

    if user.is_admin:
        st.divider()
        st.subheader("🛡 Administração: limpar sessões")
        st.caption("Apaga só sessões: nenhum usuário nem reunião é afetado, e a sua sessão continua. "
                   "Quem tiver a sessão apagada precisa entrar de novo.")
        users = container.auth.users(user)
        scope_label = st.radio("O que limpar", [
            "Sessões vencidas", "Todas as sessões de um usuário", "Todas as sessões, menos a minha"])
        scope = {"Sessões vencidas": "expired", "Todas as sessões de um usuário": "user",
                 "Todas as sessões, menos a minha": "all_but_mine"}[scope_label]
        target = ""
        if scope == "user":
            chosen = st.selectbox("Usuário", users, format_func=lambda u: f"{u.name} ({u.email})")
            target = chosen.id if chosen else ""
        typed = st.text_input("Digite LIMPAR para confirmar")
        if st.button("Limpar sessões", type="primary", disabled=typed != "LIMPAR"):
            try:
                n = container.auth.purge_sessions(user, scope, st.session_state.token, target)
                st.success(f"{n} sessões apagadas.")
            except AuthError as e:
                st.error(str(e))
