from enum import Enum
from functools import lru_cache
from pydantic_settings import BaseSettings
from pydantic import Field, model_validator


class AppMode(str, Enum):
    SOLO = "solo"
    COLLAB = "collab"


class Settings(BaseSettings):

    # ── Modo de operação ──────────────────────────────────────────────
    app_mode: AppMode = AppMode.SOLO

    # ── OpenAI ───────────────────────────────────────────────────────
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    whisper_model: str = "whisper-1"        # usado pelo WhisperTranscriber (API)
    whisper_language: str = "pt"

    # ── Whisper Local ─────────────────────────────────────────────────
    # Usado pelo WhisperLocalTranscriber (roda na máquina, sem API)
    # WHISPER_TRANSCRIBER=local no .env ativa este modo
    whisper_transcriber: str = "api"        # "api" | "local"
    whisper_local_model: str = "medium"     # tiny | base | small | medium | large | large-v2 | large-v3
    whisper_device: str = "cpu"             # "cpu" | "cuda"

    # ── Storage ───────────────────────────────────────────────────────
    storage_path: str = "data/meetings"
    mongo_uri: str = "mongodb://mongo:27017/meetagent"
    mongo_db: str = "meetagent"
    audio_storage_path: str = "data/audio"

    # ── Cache e API ───────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"
    api_port: int = 8000
    allowed_origins: list[str] = Field(default=["http://localhost:8501"])

    # ── Áudio ─────────────────────────────────────────────────────────
    max_audio_size_mb: int = 25

    # ── Diarização ────────────────────────────────────────────────────
    use_real_diarization: bool = False
    huggingface_token: str = ""
    diarization_device: str = "cpu"

    # ── Google Calendar ───────────────────────────────────────────────
    google_credentials_path: str = "credentials.json"
    google_token_path: str = "token.json"
    enable_calendar: bool = False

    # ── Provider LLM ─────────────────────────────────────────────────
    llm_provider: str = "openai"            # "openai" | "ollama" | "openrouter"

    # Ollama (local)
    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_model: str = "nemotron-mini"

    # OpenRouter
    openrouter_api_key: str = ""
    openrouter_model: str = "openai/gpt-4o"
    openrouter_site_url: str = ""
    openrouter_site_name: str = "Meet Agent"

    # ── Bot de reunião ────────────────────────────────────────────────
    bot_chrome_profile: str = "./bot_chrome_profile"
    bot_name: str = "Meet Agent"
    bot_headless: bool = False
    # Endereço da API visto pelo navegador do bot (vazio = http://127.0.0.1:API_PORT)
    capture_endpoint: str = ""

    # ── SMTP / E-mail ─────────────────────────────────────────────────
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""

    # ── Properties ───────────────────────────────────────────────────

    @property
    def is_solo(self) -> bool:
        return self.app_mode == AppMode.SOLO

    @property
    def is_collab(self) -> bool:
        return self.app_mode == AppMode.COLLAB

    @property
    def is_openrouter(self) -> bool:
        return self.llm_provider == "openrouter"

    @property
    def is_ollama(self) -> bool:
        return self.llm_provider == "ollama"

    @property
    def capture_url(self) -> str:
        return self.capture_endpoint or f"http://127.0.0.1:{self.api_port}"

    @property
    def use_local_whisper(self) -> bool:
        return self.whisper_transcriber == "local"

    @model_validator(mode="after")
    def _validate_collab(self) -> "Settings":
        """Em produção (collab), falha na partida em vez de falhar no meio de uma reunião."""
        if not self.is_collab:
            return self
        missing = []
        if not self.mongo_uri:
            missing.append("MONGO_URI")
        if not self.redis_url:
            missing.append("REDIS_URL")
        if not self.use_local_whisper and not self.openai_api_key:
            missing.append("OPENAI_API_KEY (ou WHISPER_TRANSCRIBER=local)")
        if self.llm_provider == "openai" and not self.openai_api_key:
            missing.append("OPENAI_API_KEY (LLM_PROVIDER=openai)")
        if self.llm_provider == "openrouter" and not self.openrouter_api_key:
            missing.append("OPENROUTER_API_KEY")
        if missing:
            raise ValueError("APP_MODE=collab exige no .env: " + ", ".join(dict.fromkeys(missing)))
        return self

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",  # Ignora campos extras do .env
    }


@lru_cache
def get_settings() -> Settings:
    return Settings()