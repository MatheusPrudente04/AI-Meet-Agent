"""
presentation/streamlit/app.py

Modo solo  : upload direto → processa na hora → dashboard.
Modo collab: envia o bot para a reunião (processo separado) → a reunião aparece
             na lista com o status do processamento.

Telas:
  - 📋 Reuniões   → dashboard da reunião (sem chat integrado)
  - 💬 Chat       → chat interativo dedicado com Ollama + LangChain
"""

import sys, uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import streamlit as st
from config.settings import get_settings
from presentation.container import get_container
from domain.entities.meeting import Meeting, MeetingStatus, Summary
from use_cases.process_meeting import ProcessMeetingInput
from use_cases.chat_with_meeting import ChatWithMeetingInput

settings = get_settings()
AUDIO_DIR = Path(settings.audio_storage_path)

@st.cache_resource
def get_app_container():
    return get_container()

container = get_app_container()

st.set_page_config(page_title="Meet Agent", page_icon="🎤", layout="wide")

MAX_CHAT_HISTORY = 50

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

def _init():
    defaults = {
        "page": "meetings",
        "meeting": None,
        "chat_history": [],
        "chat_meeting_id": None,
        "chat_history_store": {},
        "mode_selected": False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init()


def select_meeting(meeting) -> None:
    """Abre (ou fecha, com None) uma reunião. O id vai para a URL: se a sessão do
    navegador cair — aba em segundo plano durante a reunião, F5 — a tela volta sozinha."""
    st.session_state.meeting = meeting
    if meeting is None:
        st.query_params.pop("m", None)
    else:
        st.query_params["m"] = meeting.id


if st.session_state.meeting is None and "m" in st.query_params:
    st.session_state.meeting = container.repository.find_by_id(st.query_params["m"])


_STATUS_LABEL = {
    MeetingStatus.PENDING: "Na fila", MeetingStatus.RECORDING: "Bot gravando a reunião",
    MeetingStatus.TRANSCRIBING: "Transcrevendo o áudio", MeetingStatus.SUMMARIZING: "Gerando o resumo",
}


_ACTIVE = (MeetingStatus.PENDING, MeetingStatus.RECORDING,
           MeetingStatus.TRANSCRIBING, MeetingStatus.SUMMARIZING)


@st.fragment(run_every="5s")
def watch_progress(meeting_id: str, shown_status: MeetingStatus) -> None:
    """Relê o status a cada 5 s. A cada mudança recarrega a página inteira,
    para a barra lateral e o conteúdo acompanharem."""
    current = container.repository.find_by_id(meeting_id)
    if current is None:
        return
    if current.status != shown_status:
        st.session_state.meeting = current
        st.rerun()
    st.info(f"{_STATUS_ICON[current.status]} {_STATUS_LABEL.get(current.status, current.status.value)}…")


@st.fragment(run_every="5s")
def active_meetings_panel() -> None:
    """Reuniões em andamento, visíveis mesmo sem nenhuma reunião aberta."""
    active = [m for m in container.repository.list_all() if m.status in _ACTIVE]
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
        col_txt.info(f"{_STATUS_ICON[m.status]} **{m.title}** — {_STATUS_LABEL[m.status]}…")
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
    st.divider()

    page_label = st.radio(
        "Navegação",
        options=list(_PAGES.keys()),
        index=0 if st.session_state.page == "meetings" else 1,
        label_visibility="collapsed",
    )
    st.session_state.page = _PAGES[page_label]
    st.divider()

    if st.session_state.page == "meetings":
        st.subheader("Reuniões salvas")

        if st.button("🔄 Atualizar", use_container_width=True, type="secondary"):
            st.rerun()

        try:
            all_meetings = container.repository.list_all()
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
                meet_url = st.text_input(
                    "Link do Google Meet",
                    placeholder="https://meet.google.com/xxx-yyyy-zzz",
                )
                meeting_title = st.text_input(
                    "Título da reunião",
                    placeholder="Sprint Planning — Semana 22",
                )

                if st.button(
                    "🚀 Enviar bot para a reunião",
                    disabled=not (meet_url and meeting_title),
                    type="primary",
                    use_container_width=True,
                ):
                    try:
                        new_id = container.dispatch_bot(meet_url.strip(), meeting_title)
                    except ValueError as e:
                        st.error(str(e))
                    else:
                        select_meeting(container.repository.find_by_id(new_id))
                        st.rerun()

            with col_status:
                st.markdown("### 📋 Como funciona")
                st.markdown("""
                1. Cole o link do Google Meet
                2. Clique em **Enviar bot**
                3. O **Meet Agent 🤖** entra na reunião como participante
                4. O bot grava o áudio da reunião enquanto estiver ativo
                5. Quando a reunião terminar, ele sai automaticamente
                6. O áudio é transcrito e o resumo fica disponível aqui

                **Nota:** Use uma conta Google dedicada para o bot.
                Configure com: `python -m infrastructure.recorder.bot_setup`
                """)

    else:
        meeting: Meeting = st.session_state.meeting
        if meeting is None:
            st.warning("Reunião não selecionada.")
            st.stop()
        summary = meeting.summary

        st.subheader(f"📋 {meeting.title}")
        date_str = meeting.started_at.strftime("%d de %B de %Y às %H:%M").lstrip("0")
        st.caption(date_str)

        if meeting.status == MeetingStatus.ERROR:
            st.error(f"Falha: {meeting.error}")
            if meeting.audio_path and Path(meeting.audio_path).exists():
                if st.button("🔁 Reprocessar áudio gravado"):
                    container.enqueue_processing(meeting.id, meeting.audio_path, meeting.title)
                    select_meeting(container.repository.find_by_id(meeting.id))
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
                st.info("📝 Nota: Mudanças em tarefas não são persistidas nesta versão.")
                for idx, task in enumerate(summary.tasks):
                    task_label = f"**{task.description}**  \n*{task.responsible}* · {task.deadline}"
                    st.checkbox(
                        task_label,
                        value=getattr(task, "done", False),
                        key=f"task_{idx}_{task.description[:10]}",
                        disabled=True,
                    )

            if summary.decisions:
                st.markdown("### Decisões")
                for dec in summary.decisions:
                    with st.expander(dec.description):
                        st.write(dec.context or "Sem contexto adicional.")

            render_extra_sections(summary)

        st.divider()
        with st.expander("📄 Transcrição completa"):
            st.text(meeting.transcript_formatted or meeting.transcript_text)
            st.download_button(
                "⬇️ Baixar .txt",
                data=meeting.transcript_formatted or meeting.transcript_text,
                file_name=f"{meeting.title}_transcricao.txt",
            )

elif st.session_state.page == "chat":

    st.title("💬 Chat Interativo com IA")
    st.caption("Ollama · PromptBuilder · LangChain")

    try:
        meetings = container.repository.list_all()
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

    if st.session_state.chat_meeting_id != chat_meeting.id:
        st.session_state.chat_meeting_id = chat_meeting.id
        if chat_meeting.id not in st.session_state.chat_history_store:
            st.session_state.chat_history_store[chat_meeting.id] = []
        st.rerun()

    chat_history = st.session_state.chat_history_store.setdefault(chat_meeting.id, [])

    st.markdown("---")
    col_meta, _ = st.columns([2, 3])
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

    if prompt := st.chat_input("Ex: Quais foram as principais decisões?"):
        if len(chat_history) >= MAX_CHAT_HISTORY:
            chat_history = chat_history[-MAX_CHAT_HISTORY:]

        chat_history.append({"role": "user", "content": prompt})
        st.session_state.chat_history_store[chat_meeting.id] = chat_history

        with st.chat_message("user"):
            st.write(prompt)

        with st.chat_message("assistant"):
            placeholder = st.empty()
            with st.spinner("Pensando..."):
                try:
                    result = container.chat_with_meeting.execute(
                        ChatWithMeetingInput(
                            meeting=chat_meeting,
                            question=prompt,
                            history=chat_history[:-1],
                        )
                    )
                    answer = result.answer if result.success else f"⚠️ {result.error_message}"
                except Exception as e:
                    answer = f" Erro: {str(e)}"
                    result = None

            placeholder.write(answer)

            # Lacuna 4 — tarefas criadas pelo chat: exibe E persiste.
            if result is not None and result.success and result.extracted_tasks:
                if chat_meeting.summary is None:
                    chat_meeting.summary = Summary()
                chat_meeting.summary.tasks.extend(result.extracted_tasks)
                try:
                    container.repository.save(chat_meeting)
                    for new_task in result.extracted_tasks:
                        st.success(f"Tarefa criada: {new_task.description} "
                                   f"— {new_task.responsible}"
                                   + (f" · {new_task.deadline}" if new_task.deadline else ""))
                    st.toast(f" {len(result.extracted_tasks)} tarefa(s) "
                             "salva(s) na reunião!", icon="")
                except Exception as e:
                    st.warning(f"Tarefa extraída, mas falhou ao salvar: {e}")

        chat_history.append({"role": "assistant", "content": answer})
        st.session_state.chat_history_store[chat_meeting.id] = chat_history
        st.rerun()