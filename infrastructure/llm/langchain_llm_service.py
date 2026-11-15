# Implementação de LLM service usando LangChain para processamento de reuniões
# Suporta OpenAI, Ollama (local) e OpenRouter
import json
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage, BaseMessage
from pydantic import SecretStr

from domain.entities.meeting import Summary, Task, Decision
from domain.entities.meeting_type import MeetingType
from domain.interfaces.llm_service import ILLMService, LLMServiceError
from config.settings import get_settings
from infrastructure.llm.prompt_builder import PromptBuilder

CHAT_SYSTEM_PROMPT = """Você é um assistente de reuniões. Você participou da reunião abaixo e pode responder perguntas sobre ela.

TRANSCRIÇÃO DA REUNIÃO:
{transcript}

CONTRATO — REGRAS ABSOLUTAS:
1. Responda SEMPRE em português do Brasil
2. NÃO alucine — responda SOMENTE com base no que foi falado na transcrição
3. Valide ortografia e gramática da sua resposta (corrija erros óbvios)
4. Se a informação não estiver na transcrição, diga exatamente:
   "Isso não foi mencionado durante a reunião."
5. Cite o contexto quando relevante (ex: "Conforme mencionado por [nome]...")
6. Seja direto, objetivo e factual
7. Não adicione opiniões, suposições ou informações externas"""


class LangChainLLMService(ILLMService):
    """Serviço de LLM com LangChain — suporta OpenAI, Ollama e OpenRouter."""

    def __init__(self, llm: ChatOpenAI | None = None) -> None:
        settings = get_settings()
        self._prompt_builder = PromptBuilder()
        self._current_transcript: str = ""
        self._use_ollama = False
        self._use_ollama_native = False

        if llm:
            self._llm = llm

        elif settings.llm_provider == "ollama":
            self._llm = ChatOpenAI(
                model=settings.ollama_model,
                api_key=SecretStr("ollama"),
                base_url=settings.ollama_base_url,
                temperature=0.2,
            )
            self._use_ollama_native = True
            
        elif settings.llm_provider == "openrouter":
            self._llm = ChatOpenAI(
                model=settings.openrouter_model,
                api_key=SecretStr(settings.openrouter_api_key),
                base_url="https://openrouter.ai/api/v1",
                temperature=0.2,
                default_headers={
                    "HTTP-Referer": settings.openrouter_site_url,
                    "X-Title": settings.openrouter_site_name,
                },
            )

        else:
            if not settings.openai_api_key or settings.openai_api_key.strip() == "":
                import warnings
                warnings.warn(
                    "OPENAI_API_KEY não configurada. Tentando usar Ollama local...",
                    stacklevel=2,
                )
                self._llm = ChatOpenAI(
                    model=settings.ollama_model,
                    api_key=SecretStr("ollama"),
                    base_url=settings.ollama_base_url,
                    temperature=0.2,
                )
                self._use_ollama = True
            else:
                self._llm = ChatOpenAI(
                    model=settings.openai_model,
                    api_key=SecretStr(settings.openai_api_key),
                    temperature=0.2,
                )

    def _call_ollama(self, messages: list[dict], as_json: bool) -> str:
        """Chama o Ollama pela API nativa, pedindo uma janela do tamanho do prompt.

        Sem `num_ctx` o Ollama usa 4096 tokens e descarta o começo do prompt
        sem avisar — justamente onde está a transcrição (ADR-010). A janela
        sobe em degraus fixos para o modelo não ser recarregado a cada pedido.
        """
        import requests as _requests
        settings = get_settings()
        num_predict = 2000
        # ~2,6 caracteres por token em português (medido); 2,4 dá folga.
        needed = int(sum(len(m["content"]) for m in messages) / 2.4) + num_predict
        if needed > settings.ollama_num_ctx_max:
            raise LLMServiceError(
                f"Contexto de ~{needed} tokens excede o limite configurado "
                f"({settings.ollama_num_ctx_max}). Reduza LLM_CONTEXT_CHARS."
            )
        num_ctx = next(c for c in (4096, 8192, 16384, 32768, 65536, 131072) if c >= needed)
        payload = {
            "model": settings.ollama_model,
            "messages": messages,
            "stream": False,
            "options": {"num_predict": num_predict, "temperature": 0.1, "num_ctx": num_ctx},
        }
        if as_json:
            payload["format"] = "json"
        base = settings.ollama_base_url.rstrip("/").removesuffix("/v1")
        resp = _requests.post(f"{base}/api/chat", json=payload, timeout=600)
        resp.raise_for_status()
        return resp.json()["message"]["content"]

    def summarize(self, transcript: str, meeting_type: MeetingType = MeetingType.GENERAL) -> Summary:
        """Analisa transcrição e retorna Summary com tópicos, tarefas, decisões."""
        try:
            system_prompt = self._prompt_builder.build_summarize_system(meeting_type)
            user_message  = self._prompt_builder.build_summarize_user(transcript)

            if self._use_ollama_native:
                content = self._call_ollama([
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ], as_json=True)
            else:
                messages: list[BaseMessage] = [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=user_message),
                ]
                response = self._llm.invoke(messages)
                content  = response.content if isinstance(response.content, str) else str(response.content)

            return self._parse_summary(content)

        except LLMServiceError:
            raise
        except Exception as e:
            raise LLMServiceError(f"Falha na sumarização: {str(e)}") from e

    def chat(self, question: str, context: str, history: list[dict], summary_context: str = "", user_id: str | None = None) -> str:
        """Responde pergunta sobre reunião mantendo contexto de conversa."""
        try:
            system = CHAT_SYSTEM_PROMPT.format(transcript=context)
            if self._use_ollama_native:
                return self._call_ollama(
                    [{"role": "system", "content": system},
                     *({"role": t["role"], "content": t["content"]} for t in history),
                     {"role": "user", "content": question}],
                    as_json=False,
                )
            messages: list[BaseMessage] = [SystemMessage(content=system)]
            for turn in history:
                if turn["role"] == "user":
                    messages.append(HumanMessage(content=turn["content"]))
                else:
                    messages.append(AIMessage(content=turn["content"]))
            messages.append(HumanMessage(content=question))

            response = self._llm.invoke(messages)
            content  = response.content if isinstance(response.content, str) else str(response.content)
            return content

        except LLMServiceError:
            raise
        except Exception as e:
            raise LLMServiceError(f"Falha no chat: {str(e)}") from e

    def _parse_summary(self, raw: str) -> Summary:
        """Parseia JSON da LLM removendo markdown fences se necessário."""
        try:
            cleaned = raw.strip()

            # Remove ```json ... ``` se o LLM incluir
            if cleaned.startswith("```"):
                cleaned = cleaned.split("```")[1]
                if cleaned.startswith("json"):
                    cleaned = cleaned.split("\n", 1)[1]
            if cleaned.endswith("```"):
                cleaned = cleaned.rsplit("```", 1)[0]
            cleaned = cleaned.strip()

            try:
                data: dict = json.loads(cleaned)
            except json.JSONDecodeError:
                # modelo local às vezes corta a chave final
                data = json.loads(cleaned + "}")

            tasks = [
                Task(
                    description=t.get("description", ""),
                    responsible=t.get("responsible", "Não definido"),
                    deadline=t.get("deadline", "Não definido"),
                )
                for t in data.get("tasks", [])
                if (t.get("description") or "").strip()  # o modelo às vezes devolve item vazio
            ]

            decisions = [
                Decision(
                    description=d.get("description", ""),
                    context=d.get("context", ""),
                )
                for d in data.get("decisions", [])
                if (d.get("description") or "").strip()
            ]

            return Summary(
                overview=data.get("overview", ""),
                topics=data.get("topics", []),
                tasks=tasks,
                decisions=decisions,
            )

        except (json.JSONDecodeError, KeyError) as e:
            raise LLMServiceError(
                f"JSON inválido da LLM: {str(e)}\nResposta: {raw[:300]}"
            ) from e